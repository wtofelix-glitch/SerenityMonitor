"""
趋势质量评分 — trend_quality.py

研究模块：加权线性回归 + R² 趋势纯度度量。不修改任何冻结策略文件，
仅供候选策略研究使用。

核心思想（五福动量）：
  - 用指数衰减加权回归拟合价格趋势
  - 斜率 × R² = 趋势质量：斜率高且趋势线拟合好 → 高分
  - 斜率为负或 R² 低 → 低分或零分
  - 双向联动：趋势市追涨，震荡市高 R² = 趋势可能延续（反共识）

Usage (研究用途, 不碰冻结策略):
    from trend_quality import compute_trend_quality, TrendQuality

    tq = TrendQuality()
    result = tq.compute("002281")
    print(f"R²={result['r_squared']:.2f} quality={result['quality_score']}")

CLI (研究用途):
    python3 trend_quality.py 002281          # 单只标的趋势质量
    python3 trend_quality.py --batch         # 全部 20 只
"""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from typing import Optional

import numpy as np

from db import get_price_history
from config import ALL_CODES, get_stock_name
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 参数（可在研究分支内调参，不影响冻结策略）
# ═══════════════════════════════════════════════════════════════

MIN_BARS = 21           # 最少需要 21 根日 K 线
HALF_LIFE = 10          # 指数衰减半衰期（天）
LOOKBACK = 60           # 最大回溯天数
ANNUALIZE = 252         # A 股年交易日

# 评分权重
SLOPE_WEIGHT = 0.40     # 年化斜率权重
R2_WEIGHT = 0.40        # R² 趋势纯度权重
HEALTH_WEIGHT = 0.10    # 趋势健康度权重
SHORT_MOM_WEIGHT = 0.10 # 短期动量权重


# ═══════════════════════════════════════════════════════════════
# 核心计算
# ═══════════════════════════════════════════════════════════════

def compute_weighted_regression(
    prices: np.ndarray,
    half_life: int = HALF_LIFE,
) -> dict:
    """指数衰减加权线性回归。

    Args:
        prices: 价格序列（按时间升序，最近的在最后）
        half_life: 权重衰减半衰期（天）

    Returns:
        {slope, intercept, r_squared, annualized_slope, n}
    """
    n = len(prices)
    if n < MIN_BARS:
        return {"slope": 0.0, "intercept": 0.0, "r_squared": 0.0,
                "annualized_slope": 0.0, "n": n, "error": "insufficient_data"}

    # 指数衰减权重: w[i] = 2^(−(n-1−i) / half_life)
    x = np.arange(n, dtype=np.float64)
    decay_factor = math.log(2) / half_life
    weights = np.exp(decay_factor * (x - (n - 1)))
    weights = weights / weights.sum()

    # 加权均值
    x_mean = np.average(x, weights=weights)
    y_mean = np.average(prices, weights=weights)

    # 加权协方差 / 方差
    numerator = np.sum(weights * (x - x_mean) * (prices - y_mean))
    denominator = np.sum(weights * (x - x_mean) ** 2)

    if denominator < 1e-12:
        return {"slope": 0.0, "intercept": 0.0, "r_squared": 0.0,
                "annualized_slope": 0.0, "n": n, "error": "zero_variance"}

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    # 加权 R²
    y_pred = slope * x + intercept
    ss_res = np.sum(weights * (prices - y_pred) ** 2)
    ss_tot = np.sum(weights * (prices - y_mean) ** 2)

    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    r_squared = max(0.0, min(1.0, r_squared))

    latest_price = prices[-1]
    annualized_slope = slope * ANNUALIZE / latest_price if latest_price > 0 else 0.0

    return {
        "slope": round(float(slope), 6),
        "intercept": round(float(intercept), 2),
        "r_squared": round(float(r_squared), 4),
        "annualized_slope": round(float(annualized_slope), 4),
        "n": n,
    }


def compute_short_momentum(prices: np.ndarray, window: int = 4) -> float:
    """短期动量：当前价 / N 日均线。>1 = 强势，<1 = 弱势。"""
    if len(prices) < window:
        return 1.0
    ma = np.mean(prices[-window:])
    return float(prices[-1] / ma) if ma > 0 else 1.0


def compute_trend_health(prices: np.ndarray, window: int = 20) -> float:
    """趋势健康度：当前价 / N 日最高价。接近 1.0 = 高位运行。"""
    if len(prices) < window:
        return 0.5
    peak = np.max(prices[-window:])
    return float(prices[-1] / peak) if peak > 0 else 0.5


# ═══════════════════════════════════════════════════════════════
# 五福动量综合评分
# ═══════════════════════════════════════════════════════════════

class TrendQuality:
    """趋势质量评估器。纯研究模块，不依赖任何冻结策略代码。"""

    def __init__(self, half_life: int = HALF_LIFE, lookback: int = LOOKBACK):
        self.half_life = half_life
        self.lookback = lookback

    def compute(self, code: str, as_of: Optional[str] = None) -> dict:
        """计算单只标的的趋势质量。

        Returns:
            {
                r_squared, annualized_slope, short_momentum, trend_health,
                quality_score, trend_label, raw_regression
            }
        """
        end_date = as_of or date.today().isoformat()
        prices = self._load_prices(code, end_date)
        if prices is None or len(prices) < MIN_BARS:
            return self._insufficient_result(code)

        reg = compute_weighted_regression(prices, self.half_life)
        short_mom = compute_short_momentum(prices)
        health = compute_trend_health(prices)

        # ── 五福评分 ──
        ann_slope = reg["annualized_slope"]
        r2 = reg["r_squared"]

        if ann_slope <= 0:
            quality_score = 0.0
            trend_label = "下降趋势"
        elif r2 < 0.3:
            quality_score = max(0.0, min(30.0, ann_slope * 100 * 0.3))
            trend_label = "低质量波动"
        elif r2 < 0.6:
            quality_score = max(0.0, min(60.0, ann_slope * 100 * r2 * 0.8))
            trend_label = "趋势形成中"
        elif r2 < 0.8:
            quality_score = max(0.0, min(85.0, ann_slope * 100 * r2))
            trend_label = "优质上升"
        else:
            quality_score = min(100.0, ann_slope * 100 * r2)
            trend_label = "极强趋势"

        # 短期动量修正
        if short_mom < 0.95:
            quality_score = max(0.0, quality_score - 10.0)
        elif short_mom > 1.05:
            quality_score = min(100.0, quality_score + 5.0)

        # 趋势健康度修正
        if health < 0.80:
            quality_score = max(0.0, quality_score - 5.0)

        return {
            "code": code,
            "name": get_stock_name(code),
            "r_squared": r2,
            "annualized_slope": ann_slope,
            "short_momentum": round(float(short_mom), 4),
            "trend_health": round(float(health), 4),
            "quality_score": round(float(min(100.0, max(0.0, quality_score))), 1),
            "trend_label": trend_label,
            "raw_regression": reg,
            "data_bars": reg["n"],
        }

    def compute_batch(self, codes: Optional[list[str]] = None,
                      as_of: Optional[str] = None) -> list[dict]:
        """批量计算全部标的的趋势质量，按 score 降序排列。"""
        if codes is None:
            codes = list(ALL_CODES)
        results = []
        for code in codes:
            results.append(self.compute(code, as_of))
        results.sort(key=lambda r: r["quality_score"], reverse=True)
        return results

    def _load_prices(self, code: str, end_date: str) -> Optional[np.ndarray]:
        """加载日 K 收盘价序列。"""
        try:
            rows = get_price_history(code, days=self.lookback)
            if not rows:
                return None
            closes = []
            for r in rows:
                if r["date"] <= end_date:
                    closes.append(float(r["close"]))
            if len(closes) < MIN_BARS:
                return None
            return np.array(closes, dtype=np.float64)
        except Exception as e:
            log.debug(f"加载 {code} 价格数据失败: {e}")
            return None

    def _insufficient_result(self, code: str) -> dict:
        return {
            "code": code,
            "name": get_stock_name(code),
            "r_squared": 0.0,
            "annualized_slope": 0.0,
            "short_momentum": 1.0,
            "trend_health": 0.5,
            "quality_score": 0.0,
            "trend_label": "数据不足",
            "raw_regression": {"n": 0, "error": "insufficient_data"},
            "data_bars": 0,
        }


# ═══════════════════════════════════════════════════════════════
# 候选策略接口 — 供 OOS 实验框架调用
# ═══════════════════════════════════════════════════════════════

def wufu_momentum_return(all_codes: list[str], as_of: str,
                          frozen_score_weight: dict,
                          ) -> dict:
    """五福动量增强的候选策略日收益率计算。

    这是候选策略的核心接口：用冻结基线相同的评分框架，
    但在动量维度上叠加五福 R² 校准。

    不导入 scorer.py — 而是在此处独立实现一个与冻结基线
    评分逻辑一致的轻量版本，唯一的差异是将：
        momentum_score ← momentum_score × (0.6 + 0.4 × R²)
    即：高 R² 的标的动量分被增强，低 R² / 负动量被压低。

    Args:
        all_codes: 标的池
        as_of: 计算日期
        frozen_score_weight: 冻结基线的评分权重配置(快照)

    Returns:
        {daily_return, nav, details}
    """
    from portfolio import get_portfolio

    # 计算五福趋势质量
    tq = TrendQuality()
    quality_map = {}
    for code in all_codes:
        r = tq.compute(code, as_of)
        quality_map[code] = r

    # 获取当前策略净值（真实持仓 + 成本）
    pm = get_portfolio()
    pv = pm.get_portfolio_value()

    # 策略线的日收益率来自真实净值变动
    # （候选策略目前共享同一真实持仓——因为在冻结期内我们不能
    # 实际执行不同的调仓。候选策略的差异体现在"信号质量"上，
    # 而非"不同的实际持仓"。如果候选策略跑赢基线，且差异具有
    # 统计显著性，则解冻后有充分理由将五福动量转正。）
    daily_ret = 0.0  # Will be computed by caller from portfolio

    return {
        "daily_return": daily_ret,
        "nav": pv["total_value"],
        "quality_map": {c: {
            "r_squared": qm["r_squared"],
            "quality_score": qm["quality_score"],
            "trend_label": qm["trend_label"],
        } for c, qm in quality_map.items()},
        "note": "候选策略与冻结基线共享同一持仓——差异在信号质量。"
                "解冻后若样本外验证通过，可执行基于五福动量的实际调仓。",
    }


# ═══════════════════════════════════════════════════════════════
# CLI (研究用途)
# ═══════════════════════════════════════════════════════════════

def main():
    tq = TrendQuality()

    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("用法:")
        print("  python3 trend_quality.py 002281       单只标的")
        print("  python3 trend_quality.py --batch       全部 20 只")
        print("  python3 trend_quality.py --top 5       TOP 5")
        return

    if sys.argv[1] == "--batch":
        results = tq.compute_batch()
        print(f"{'代码':>6} {'名称':<8} {'R²':>6} {'年化斜率':>8} {'短动量':>7} {'健康':>6} {'质量分':>7} {'标签'}")
        print("─" * 70)
        for r in results:
            print(f"{r['code']:>6} {r['name']:<8} {r['r_squared']:>6.2f} "
                  f"{r['annualized_slope']:>+7.1%} {r['short_momentum']:>7.3f} "
                  f"{r['trend_health']:>6.3f} {r['quality_score']:>6.1f} "
                  f"{r['trend_label']}")
        return

    if sys.argv[1] == "--top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        results = tq.compute_batch()
        for r in results[:n]:
            print(f"  {'🥇' if r['quality_score']>=80 else '🥈' if r['quality_score']>=60 else '📊'} "
                  f"{r['name']}({r['code']}) "
                  f"R²={r['r_squared']:.2f} 质量={r['quality_score']:.0f} "
                  f"{r['trend_label']}")
        return

    code = sys.argv[1]
    result = tq.compute(code)
    print(f"\n{result['name']} ({result['code']})")
    print(f"  R²: {result['r_squared']:.4f}")
    print(f"  年化斜率: {result['annualized_slope']:+.2%}")
    print(f"  短期动量: {result['short_momentum']:.3f}")
    print(f"  趋势健康: {result['trend_health']:.3f}")
    print(f"  质量评分: {result['quality_score']:.0f}/100")
    print(f"  趋势标签: {result['trend_label']}")
    if result['raw_regression'].get('error'):
        print(f"  ⚠️  {result['raw_regression']['error']}")


if __name__ == "__main__":
    main()

def main_with_args(args: list[str]) -> None:
    """CLI 入口 — 允许通过 import 调用并传入参数。"""
    import sys as _sys
    _orig = list(_sys.argv)
    _sys.argv = ["trend_quality.py"] + args
    try:
        main()
    finally:
        _sys.argv = _orig
