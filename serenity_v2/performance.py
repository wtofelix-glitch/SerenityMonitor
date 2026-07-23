"""
Serenity 2.0 — 绩效指标体系 (PerformanceMetrics)

Profit Factor、胜率、盈亏比等是交易结果评价指标，
不是事前刚性风控规则。

职责分离：
  RiskConstraints  → 事前：控制仓位、单笔风险、现金、回撤、可执行性
  PerformanceMetrics → 事后：计算PF、胜率、盈亏比、期望收益

当PF低于目标时 → 触发降级/暂停/重新验证，不宣称事前保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PerformanceMetrics:
    """事后绩效指标。"""

    # --- 核心指标 ---
    profit_factor: float = 0.0          # 总盈利 / 总亏损
    win_rate: float = 0.0               # 盈利交易占比
    avg_win: float = 0.0                # 平均盈利金额
    avg_loss: float = 0.0               # 平均亏损金额
    profit_loss_ratio: float = 0.0      # 盈亏比 (avg_win / avg_loss)
    expected_return: float = 0.0        # 单笔期望收益

    # --- 风险指标 ---
    max_drawdown: float = 0.0           # 最大回撤
    max_drawdown_pct: float = 0.0       # 最大回撤%
    sharpe_ratio: float = 0.0           # 夏普比率（简化版）
    consecutive_losses: int = 0         # 最大连续亏损次数

    # --- 统计基础 ---
    total_trades: int = 0               # 总交易数
    winning_trades: int = 0             # 盈利交易数
    losing_trades: int = 0              # 亏损交易数
    total_profit: float = 0.0           # 总盈利
    total_loss: float = 0.0             # 总亏损
    net_profit: float = 0.0             # 净利润
    capital_utilization: float = 0.0    # 资金利用率

    # --- 验收条件 ---
    sample_size: int = 0
    market_regimes_covered: list[str] = field(default_factory=list)

    @classmethod
    def from_trades(cls, trades: list[dict]) -> "PerformanceMetrics":
        """
        从成交列表计算绩效指标。

        每笔 trade: {"pnl": float, "amount": float, "date": str, ...}
        """
        if not trades:
            return cls()

        profits = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades if t["pnl"] < 0]

        total_profit = sum(profits)
        total_loss = abs(sum(losses))
        net_profit = total_profit - total_loss

        m = cls(
            profit_factor=total_profit / total_loss if total_loss > 0 else (
                float("inf") if total_profit > 0 else 0.0
            ),
            win_rate=len(profits) / len(trades) if trades else 0,
            avg_win=total_profit / len(profits) if profits else 0,
            avg_loss=total_loss / len(losses) if losses else 0,
            profit_loss_ratio=(total_profit / len(profits)) / (total_loss / len(losses))
                if profits and losses else 0,
            expected_return=net_profit / len(trades) if trades else 0,
            total_trades=len(trades),
            winning_trades=len(profits),
            losing_trades=len(losses),
            total_profit=total_profit,
            total_loss=total_loss,
            net_profit=net_profit,
            sample_size=len(trades),
        )

        # 连续亏损
        max_consecutive = 0
        current_consecutive = 0
        for t in trades:
            if t["pnl"] < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        m.consecutive_losses = max_consecutive

        return m

    def is_healthy(self, target_pf: float = 1.5) -> dict[str, bool]:
        """
        检查绩效是否达标。

        返回 {"pf_ok": bool, "sample_ok": bool, "overall": bool}
        """
        return {
            "pf_ok": self.profit_factor >= target_pf,
            "sample_ok": self.sample_size >= 30,  # 最少30笔
            "win_rate_ok": self.win_rate >= 0.35,  # 胜率不低于35%
            "overall": self.profit_factor >= target_pf and self.sample_size >= 30,
        }

    def summary(self) -> str:
        """一行摘要。"""
        return (
            f"PF={self.profit_factor:.2f} | 胜率={self.win_rate:.1%} | "
            f"盈亏比={self.profit_loss_ratio:.2f} | 期望={self.expected_return:+.0f} | "
            f"交易{self.total_trades}笔"
        )


# ---------------------------------------------------------------------------
# 影子模式下的模拟成交记账规则
# ---------------------------------------------------------------------------

SHADOW_ACCOUNTING_RULES = """
影子模式下，没有真实成交，PF计算必须明确规定：

1. 假设成交价格
   - 使用信号生成时的 current_price 作为成交价
   - 可选：使用滑点模型（±0.5%）

2. 滑点
   - 买入：成交价 × (1 + 0.001)  即加0.1%滑点
   - 卖出：成交价 × (1 - 0.001)  即减0.1%滑点

3. 手续费
   - 佣金：成交金额 × 0.0003（万三），最低5元
   - 印花税：卖出成交金额 × 0.001（千一）
   - 过户费：成交金额 × 0.00002

4. T+1约束
   - 当日买入的股票当日不可卖出
   - 模拟中：买入后 available_shares 次日更新

5. 未成交处理
   - 如果信号生成后价格未到达建议区间 → 标记为"未触发"
   - 不计入PF统计

6. 止盈止损触发顺序
   - 按日内高低价时间顺序判断
   - 先到先触发

7. 盘中只有高低价时的路径歧义
   - 默认悲观处理：先触达不利方向
   - 可选乐观处理：先触达有利方向
   - 记录处理方式以备审计
"""


# ---------------------------------------------------------------------------
# 性能降级规则
# ---------------------------------------------------------------------------

@dataclass
class PerformanceGate:
    """
    绩效门控：当指标低于阈值时的处理。

    这不是事前风控，而是信号系统输出的质量反馈。
    """

    min_profit_factor: float = 1.2       # 低于此值：⚠️ 警告
    critical_profit_factor: float = 0.8  # 低于此值：🔴 暂停新信号
    min_sample_size: int = 20            # 最少样本量才做判断
    min_market_regimes: int = 2          # 最少覆盖2种市场环境

    def evaluate(self, metrics: PerformanceMetrics) -> dict:
        """评估绩效门控状态。"""
        if metrics.sample_size < self.min_sample_size:
            return {
                "status": "insufficient_data",
                "message": f"样本不足({metrics.sample_size}/{self.min_sample_size})",
                "allow_new_signals": True,
                "allow_push": False,
            }

        if metrics.profit_factor < self.critical_profit_factor:
            return {
                "status": "critical",
                "message": f"PF={metrics.profit_factor:.2f} < {self.critical_profit_factor}",
                "allow_new_signals": False,
                "allow_push": False,
            }

        if metrics.profit_factor < self.min_profit_factor:
            return {
                "status": "warning",
                "message": f"PF={metrics.profit_factor:.2f} < {self.min_profit_factor}",
                "allow_new_signals": True,
                "allow_push": False,
            }

        return {
            "status": "healthy",
            "message": f"PF={metrics.profit_factor:.2f} ✓",
            "allow_new_signals": True,
            "allow_push": True,
        }
