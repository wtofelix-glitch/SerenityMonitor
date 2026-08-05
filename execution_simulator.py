"""
成交模拟器 — execution_simulator.py

模拟 A 股真实交易环境下的订单成交过程，包括滑点、交易成本。
与 market_microstructure.py 配合使用：先做可成交性检查，再做成交模拟。

Usage:
    from execution_simulator import ExecutionSimulator

    sim = ExecutionSimulator()
    fill = sim.simulate_fill(order, bar, liquidity_state)
    print(f"成交价: {fill.fill_price}, 滑点: {fill.slippage_pct:.3%}")

覆盖的成本项（v4 §4.4）:
    [x] 佣金（0.025% 双向）
    [x] 印花税（0.05% 卖出单向）
    [x] 过户费（0.001% 双向）
    [x] 滑点（基于流动性的动态估计）
    [x] 冲击成本（大单对价格的推升/打压）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 交易成本常量
# ═══════════════════════════════════════════════════════════════

COMMISSION_RATE = 0.00025       # 佣金 0.025%（买卖双向）
STAMP_TAX_RATE = 0.0005         # 印花税 0.05%（卖出单向，2023.8 起）
TRANSFER_FEE_RATE = 0.00001     # 过户费 0.001%（买卖双向）

# 滑点参数
SLIPPAGE_BASIS_POINTS = 0.001   # 基础滑点 0.1%
SLIPPAGE_SMALL_CAP_BONUS = 0.0005  # 中小盘额外滑点 0.05%
SMALL_CAP_MV_THRESHOLD = 5e10   # 市值 < 500 亿视为中小盘

# 冲击成本参数
IMPACT_FACTOR = 0.1             # 冲击成本因子
IMPACT_MIN_THRESHOLD = 0.02     # 仓位/日均成交额 > 2% 时开始计算冲击


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════


@dataclass
class Order:
    """订单"""
    code: str
    action: str                        # "buy" / "sell"
    price: float                       # 委托价格（信号生成时的参考价）
    quantity: int
    order_date: date


@dataclass
class Bar:
    """行情 Bar（用于模拟成交）"""
    code: str
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float                      # 成交额
    change_pct: float = 0.0


@dataclass
class LiquidityState:
    """流动性状态"""
    avg_daily_volume: float            # 近 20 日日均成交量（股）
    avg_daily_amount: float            # 近 20 日日均成交额（元）
    total_mv: float = 0.0              # 总市值
    is_small_cap: bool = False         # 是否中小盘
    spread_estimate: float = 0.001     # 预估买卖价差


@dataclass
class FillResult:
    """成交结果"""
    filled: bool = False
    fill_price: float = 0.0
    fill_quantity: int = 0
    unfilled_quantity: int = 0
    unfilled_reason: str = ""

    # 滑点
    slippage_pct: float = 0.0          # 滑点比例（正=不利方向）
    slippage_amount: float = 0.0       # 滑点金额

    # 成本明细
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    impact_cost: float = 0.0           # 冲击成本
    total_cost: float = 0.0            # 总交易成本
    total_cost_pct: float = 0.0        # 总成本占成交金额比例

    # 信号价格 vs 实际成交
    signal_price: float = 0.0          # 信号生成时的参考价
    price_deviation_pct: float = 0.0   # 成交价 vs 信号价的偏差


# ═══════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════


class ExecutionSimulator:
    """成交模拟器。

    在回测中模拟订单成交过程，包括：
    - 基于流动性状态的成交概率和滑点
    - 佣金/印花税/过户费/冲击成本计算
    """

    def __init__(self, commission_rate: float = COMMISSION_RATE,
                 stamp_tax_rate: float = STAMP_TAX_RATE,
                 transfer_fee_rate: float = TRANSFER_FEE_RATE):
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate

    # ── 成交模拟 ──────────────────────────────────────────

    def simulate_fill(self, order: Order, bar: Bar,
                      liquidity: LiquidityState,
                      execution_price: Optional[float] = None) -> FillResult:
        """模拟订单成交。

        策略：
        - 正常行情：按 bar.close 成交（假设收盘价执行）
        - 加上滑点调整（买卖方向不同）
        - 计算各项交易成本

        Args:
            order: 订单
            bar: 行情 Bar
            liquidity: 流动性状态

        Returns:
            FillResult
        """
        result = FillResult()
        result.signal_price = order.price

        # 1. 估算成交价（含滑点）
        slippage = self.estimate_slippage(
            order.code, order.quantity, order.action, liquidity)
        slippage_direction = 1.0 if order.action == "buy" else -1.0
        effective_slippage = slippage * slippage_direction
        base_price = execution_price if execution_price is not None else bar.close
        result.fill_price = base_price * (1.0 + effective_slippage)
        result.slippage_pct = slippage

        # 2. 估算冲击成本
        impact = self.estimate_impact(
            order.quantity, order.price, liquidity)
        impact_direction = 1.0 if order.action == "buy" else -1.0
        result.fill_price *= (1.0 + impact * impact_direction)
        result.impact_cost = abs(impact) * order.quantity * order.price

        # 3. 计算成交数量
        result.fill_quantity = order.quantity
        result.filled = True

        # 4. 计算价格偏差
        if order.price > 0:
            result.price_deviation_pct = (result.fill_price / order.price - 1.0)

        # 5. 计算交易成本
        trade_amount = result.fill_price * result.fill_quantity

        # 佣金（买卖双向，最低 5 元）
        result.commission = max(5.0, trade_amount * self.commission_rate)

        # 印花税（仅卖出）
        if order.action == "sell":
            result.stamp_tax = trade_amount * self.stamp_tax_rate

        # 过户费（买卖双向）
        result.transfer_fee = trade_amount * self.transfer_fee_rate

        # 滑点金额
        result.slippage_amount = abs(trade_amount * effective_slippage)

        # 总成本
        result.total_cost = (result.commission + result.stamp_tax +
                             result.transfer_fee + result.impact_cost +
                             abs(result.slippage_amount))
        result.total_cost_pct = result.total_cost / trade_amount if trade_amount > 0 else 0.0

        return result

    # ── 滑点估算 ──────────────────────────────────────────

    def estimate_slippage(self, code: str, quantity: int,
                          side: str, liquidity: LiquidityState) -> float:
        """估算滑点比例。

        因素：
        - 基础滑点：0.1%
        - 中小盘附加：+0.05%
        - 大单附加：数量/日均成交量每 1% → +0.01%
        """
        slippage = SLIPPAGE_BASIS_POINTS

        # 中小盘额外滑点
        if liquidity.is_small_cap:
            slippage += SLIPPAGE_SMALL_CAP_BONUS

        # 仓位/日均成交量比例产生的额外滑点
        if liquidity.avg_daily_volume > 0:
            vol_ratio = quantity / liquidity.avg_daily_volume
            slippage += min(0.005, vol_ratio * 0.01)

        # 买卖价差
        slippage += liquidity.spread_estimate * 0.5

        return min(0.01, slippage)  # 上限 1%

    # ── 冲击成本估算 ──────────────────────────────────────

    def estimate_impact(self, quantity: int, price: float,
                        liquidity: LiquidityState) -> float:
        """估算冲击成本比例。

        Almgren-Chriss 简化模型：
        impact = IMPACT_FACTOR * sqrt(participation_rate)
        其中 participation_rate = trade_amount / avg_daily_amount

        仅在参与率 > IMPACT_MIN_THRESHOLD 时计算冲击。
        """
        if liquidity.avg_daily_amount <= 0:
            return 0.0

        trade_amount = quantity * price
        participation = trade_amount / liquidity.avg_daily_amount

        if participation < IMPACT_MIN_THRESHOLD:
            return 0.0

        impact = IMPACT_FACTOR * (participation ** 0.5)
        return min(0.02, impact)  # 上限 2%

    # ── 成本计算（不模拟成交，仅计算理论成本）─────────────

    def compute_trading_cost(self, price: float, quantity: int,
                             action: str, liquidity: Optional[LiquidityState] = None) -> dict:
        """计算一笔交易的理论总成本（不模拟成交）。

        用于信号生成时的成本预估（§9 净期望收益计算）。

        Returns:
            {commission, stamp_tax, transfer_fee, slippage_est, impact_est, total_cost, total_cost_pct}
        """
        trade_amount = price * quantity

        commission = max(5.0, trade_amount * self.commission_rate)
        stamp_tax = trade_amount * self.stamp_tax_rate if action == "sell" else 0.0
        transfer_fee = trade_amount * self.transfer_fee_rate

        slippage_est = 0.0
        impact_est = 0.0
        if liquidity is not None:
            slippage_est = self.estimate_slippage("", quantity, action, liquidity) * trade_amount
            impact_est = self.estimate_impact(quantity, price, liquidity) * trade_amount

        total_cost = commission + stamp_tax + transfer_fee + slippage_est + impact_est

        return {
            "commission": commission,
            "stamp_tax": stamp_tax,
            "transfer_fee": transfer_fee,
            "slippage_est": slippage_est,
            "impact_est": impact_est,
            "total_cost": total_cost,
            "total_cost_pct": total_cost / trade_amount if trade_amount > 0 else 0.0,
            "trade_amount": trade_amount,
        }


# ═══════════════════════════════════════════════════════════════
# 流动性状态构建器
# ═══════════════════════════════════════════════════════════════


def build_liquidity_state(code: str) -> LiquidityState:
    """从数据库构建流动性状态。"""
    try:
        from db import get_avg_volume, get_latest_snapshot
        avg_vol = get_avg_volume(code, days=20) or 0
        snapshots = get_latest_snapshot(code)
        total_mv = 0.0
        avg_amount = 0.0
        if snapshots:
            snap = snapshots[0] if isinstance(snapshots, list) else snapshots
            total_mv = getattr(snap, "total_mv", 0.0) or 0.0
            avg_amount = avg_vol * (getattr(snap, "close", 0.0) or 0.0)
    except Exception:
        avg_vol = 0
        total_mv = 0.0
        avg_amount = 0.0

    is_small_cap = total_mv < SMALL_CAP_MV_THRESHOLD and total_mv > 0
    spread = 0.002 if is_small_cap else 0.001

    return LiquidityState(
        avg_daily_volume=avg_vol,
        avg_daily_amount=avg_amount,
        total_mv=total_mv,
        is_small_cap=is_small_cap,
        spread_estimate=spread,
    )


# ═══════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════

_simulator_instance: Optional[ExecutionSimulator] = None


def get_simulator(
    commission_rate: Optional[float] = None,
    stamp_tax_rate: Optional[float] = None,
    transfer_fee_rate: Optional[float] = None,
) -> ExecutionSimulator:
    """获取默认单例，或按指定成本参数创建独立模拟器。"""
    global _simulator_instance
    if any(rate is not None for rate in (
        commission_rate, stamp_tax_rate, transfer_fee_rate
    )):
        return ExecutionSimulator(
            commission_rate=commission_rate if commission_rate is not None else COMMISSION_RATE,
            stamp_tax_rate=stamp_tax_rate if stamp_tax_rate is not None else STAMP_TAX_RATE,
            transfer_fee_rate=(
                transfer_fee_rate if transfer_fee_rate is not None else TRANSFER_FEE_RATE
            ),
        )
    if _simulator_instance is None:
        _simulator_instance = ExecutionSimulator()
    return _simulator_instance
