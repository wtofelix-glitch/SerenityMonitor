"""
成交概率模型 — fill_model.py

模拟涨跌停板的成交概率。在回测中使用，而非假设信号总能按收盘价成交。

核心逻辑：
  - 涨停买入：普通涨停约 10-30% 概率，一字板 <5%
  - 跌停卖出：普通跌停约 10-30% 概率，一字板 <5%
  - 正常行情：假设接近 100%（扣除正常滑点后在 execution_simulator 中处理）

Usage:
    from fill_model import FillModel, LimitStatus

    model = FillModel()
    prob = model.prob_fill_at_limit_up("002281")  # 涨停板买入成交概率
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 成交概率参数
# ═══════════════════════════════════════════════════════════════

# 一字板成交概率
HARD_LIMIT_FILL_PROB = 0.03      # 3%，散户几乎买不到/卖不掉

# 普通涨跌停成交概率范围
NORMAL_LIMIT_FILL_PROB_MIN = 0.10  # 10%
NORMAL_LIMIT_FILL_PROB_MAX = 0.30  # 30%

# 正常行情成交概率（扣掉因流动性导致的未成交）
NORMAL_FILL_PROB = 0.99           # 99%，留 1% 给极端流动性缺失


@dataclass
class FillProbability:
    """成交概率评估结果"""
    prob: float                    # 成交概率 [0, 1]
    can_fill: bool                 # 是否可能成交
    expected_fill_ratio: float     # 预期成交比例（可能部分成交）
    reason: str                    # 说明
    limit_status: str = "normal"


class FillModel:
    """成交概率模型。

    模拟不同市场状态下的成交可能性，用于回测中更真实的执行模拟。
    """

    def __init__(self):
        self._consecutive_limit_days: dict[str, int] = {}  # code → 连续涨跌停天数

    # ── 公共接口 ──────────────────────────────────────────

    def prob_fill_at_limit_up(self, code: str,
                               is_hard: bool = False,
                               volume: Optional[float] = None,
                               turnover_amount: Optional[float] = None) -> FillProbability:
        """涨停板买入成交概率。

        一字板（is_hard=True）：约 3%
        普通涨停：10-30%，取决于封板力度（成交量越大越可能排到）
        """
        if is_hard:
            return FillProbability(
                prob=HARD_LIMIT_FILL_PROB,
                can_fill=False,
                expected_fill_ratio=0.0,
                reason=f"{code} 一字涨停封板，散户买入概率极低 (~{HARD_LIMIT_FILL_PROB:.0%})",
                limit_status="limit_up_hard",
            )

        # 普通涨停：成交量越大 → 越有机会排到
        prob = NORMAL_LIMIT_FILL_PROB_MIN
        if turnover_amount and turnover_amount > 0:
            # 封板成交额越高，散户排到的概率越大
            prob = min(NORMAL_LIMIT_FILL_PROB_MAX,
                       NORMAL_LIMIT_FILL_PROB_MIN + turnover_amount * 1e-9)

        consecutive = self._consecutive_limit_days.get(code, 1)
        # 连续涨停天数越多，越难买入
        prob *= (0.7 ** (consecutive - 1))

        return FillProbability(
            prob=max(0.01, prob),
            can_fill=prob > 0.1,
            expected_fill_ratio=prob,
            reason=f"{code} 涨停买入, 预估成交概率 {prob:.0%}",
            limit_status="limit_up",
        )

    def prob_fill_at_limit_down(self, code: str,
                                 is_hard: bool = False,
                                 volume: Optional[float] = None) -> FillProbability:
        """跌停板卖出成交概率。

        一字板（is_hard=True）：约 3%
        普通跌停：10-30%，取决于成交量
        """
        if is_hard:
            return FillProbability(
                prob=HARD_LIMIT_FILL_PROB,
                can_fill=False,
                expected_fill_ratio=0.0,
                reason=f"{code} 一字跌停封板，卖出概率极低 (~{HARD_LIMIT_FILL_PROB:.0%})",
                limit_status="limit_down_hard",
            )

        prob = NORMAL_LIMIT_FILL_PROB_MIN
        if volume and volume > 0:
            prob = min(NORMAL_LIMIT_FILL_PROB_MAX,
                       NORMAL_LIMIT_FILL_PROB_MIN + volume * 1e-7)

        consecutive = self._consecutive_limit_days.get(code, 1)
        prob *= (0.7 ** (consecutive - 1))

        return FillProbability(
            prob=max(0.01, prob),
            can_fill=prob > 0.1,
            expected_fill_ratio=prob,
            reason=f"{code} 跌停卖出, 预估成交概率 {prob:.0%}",
            limit_status="limit_down",
        )

    def prob_fill_normal(self, code: str) -> FillProbability:
        """正常行情成交概率。"""
        return FillProbability(
            prob=NORMAL_FILL_PROB,
            can_fill=True,
            expected_fill_ratio=1.0,
            reason=f"{code} 正常交易",
            limit_status="normal",
        )

    def prob_fill(self, code: str, limit_status: str,
                  volume: Optional[float] = None,
                  turnover_amount: Optional[float] = None) -> FillProbability:
        """统一接口：根据涨跌停状态返回成交概率。"""
        if limit_status == "limit_up_hard":
            return self.prob_fill_at_limit_up(code, is_hard=True, volume=volume)
        elif limit_status == "limit_up":
            return self.prob_fill_at_limit_up(code, is_hard=False,
                                              turnover_amount=turnover_amount)
        elif limit_status == "limit_down_hard":
            return self.prob_fill_at_limit_down(code, is_hard=True, volume=volume)
        elif limit_status == "limit_down":
            return self.prob_fill_at_limit_down(code, is_hard=False, volume=volume)
        elif limit_status == "suspended":
            return FillProbability(
                prob=0.0, can_fill=False, expected_fill_ratio=0.0,
                reason=f"{code} 停牌中", limit_status="suspended")
        else:
            return self.prob_fill_normal(code)

    # ── 状态追踪 ──────────────────────────────────────────

    def update_consecutive_limits(self, code: str, is_limit: bool) -> None:
        """更新连续涨跌停天数。每日收盘后调用。"""
        if is_limit:
            self._consecutive_limit_days[code] = self._consecutive_limit_days.get(code, 0) + 1
        else:
            self._consecutive_limit_days[code] = 0

    def get_consecutive_limit_days(self, code: str) -> int:
        """获取连续涨跌停天数。"""
        return self._consecutive_limit_days.get(code, 0)

    def reset(self) -> None:
        """重置状态。"""
        self._consecutive_limit_days.clear()
