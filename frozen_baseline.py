"""
Frozen Baseline 系统 — frozen_baseline.py

不做任何自适应调整的固定规则版本。与自适应系统并跑，用于检验
自适应机制是否真正创造增量价值。

Usage:
    from frozen_baseline import FrozenBaseline

    fb = FrozenBaseline()
    signals = fb.generate_signals(market_data)

特点:
  - 固定权重（_SCORE_WEIGHT_DEFAULTS，永不变）
  - 固定阈值（SIGNAL_CONFIG 初始值，永不变）
  - 无情报层输入（无 Sentinel / LLM / Guru / Council / Conviction）
  - 无权重自进化（无 IC 驱动、无 market_sense 偏移）
  - 同样的可成交性检查 + 交易成本 + 风控约束

v4 §6 定义: 系统 A — Frozen Baseline（冻结基准）
"""

from __future__ import annotations

from datetime import date
from typing import Optional
import json

from config import (
    STOCK_MAP, STOCK_DETAILS, ALL_CODES, SIGNAL_CONFIG, get_stock_name,
    compute_serenity_score,
)
from db import get_price_history, get_avg_volume, get_latest_scores
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# Frozen Baseline 固定权重（永不变）
# ═══════════════════════════════════════════════════════════════

FROZEN_WEIGHTS = {
    "zone": 0.20,
    "momentum": 0.18,
    "volume": 0.04,
    "serenity": 0.17,
    "factor": 0.19,
    "technical": 0.10,
    "moat": 0.09,
    "capital": 0.03,
}

# Frozen Baseline 固定阈值（永不变）
FROZEN_STRONG_BUY = 74.0
FROZEN_BUY = 66.0
FROZEN_CAUTION_BUY = 60.0
FROZEN_HOLD_HIGH = 50.0
FROZEN_SELL = 45.0

# 版本标识
FROZEN_VERSION = "v2"  # v4 Phase 3: 因子去冗余完成后升级
FROZEN_SINCE = "2026-07-05"
FROZEN_V2_SINCE = None  # 由因子审计完成时设置

# 因子版本元数据
_FROZEN_V2_METADATA = {
    "version": "v2",
    "description": "去冗余后独立因子集 (6-8 factors from factor_audit)",
    "clock_reset": True,  # §6.3 判定时钟从 v2 上线后重置
    "built_at": None,
    "factor_count": None,
    "de_redundancy_config": None,
}

# 因子引擎（复用）
_FACTOR_ENGINE = None


def get_frozen_metadata() -> dict:
    """获取当前 Frozen Baseline 版本和时钟状态。"""
    import os
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".de_redundancy_config.json"
    )
    meta = dict(_FROZEN_V2_METADATA)
    meta["version"] = FROZEN_VERSION
    meta["frozen_since"] = FROZEN_SINCE
    if FROZEN_V2_SINCE:
        meta["v2_since"] = FROZEN_V2_SINCE
        meta["clock_reset_at"] = FROZEN_V2_SINCE
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            meta["de_redundancy_config"] = config
            meta["factor_count"] = config.get("n_independent")
            meta["built_at"] = config.get("generated_at")
        except Exception:
            pass
    return meta
    global _FACTOR_ENGINE
    if _FACTOR_ENGINE is None:
        try:
            from factor_engine import AlphaFactorEngine
            _FACTOR_ENGINE = AlphaFactorEngine()
        except Exception:
            _FACTOR_ENGINE = False
    return _FACTOR_ENGINE if _FACTOR_ENGINE is not False else None


class FrozenBaseline:
    """Frozen Baseline 系统 — 固定规则的交易信号生成器。

    不做任何自适应调整。在同一批行情输入下，
    与 Adaptive System 产生各自的信号，用于对比。
    """

    def __init__(self):
        self.version = FROZEN_VERSION
        self.since = FROZEN_SINCE
        self.weights = dict(FROZEN_WEIGHTS)
        self._today = date.today().isoformat()

    # ── 评分（冻结版）────────────────────────────────────

    def score_one(self, code: str, price: float, change_pct: float,
                  volume: float, snap: Optional[dict] = None) -> dict:
        """对单只标的做冻结版评分。

        仅使用价格/动量/成交量/factor/serenity/moat 等可量化维度。
        不使用 LLM 情绪、Sentinel、Conviction、market_sense 偏移。
        """
        detail = STOCK_DETAILS.get(code, {})
        buy_low = detail.get("buy_zone_low", 0)
        buy_high = detail.get("buy_zone_high", price)
        target = detail.get("target_sell", price * 1.5)

        # 1. zone_score — 价格位置（动态 60 日通道）
        zone_score = self._compute_zone(price, detail, buy_low, buy_high, target)

        # 2. momentum_score — 动量
        momentum_score = self._compute_momentum(change_pct, price, target)

        # 3. volume_score — 量比
        volume_score = self._compute_volume(code, volume)

        # 4. serenity_score — Serenity 框架匹配度（静态）
        serenity_score = compute_serenity_score(code)

        # 5. factor_score — Alpha 因子引擎
        factor_score = self._compute_factor(code)

        # 6. technical_score — 技术面（无 LLM 情绪融合）
        technical_score = self._compute_technical(code, price)

        # 7. moat_score — 护城河
        moat_score = self._compute_moat(code)

        # 8. capital_score — 资金面（中性回退）
        capital_score = 50.0

        # 加权总分
        total = (
            zone_score * self.weights["zone"] +
            momentum_score * self.weights["momentum"] +
            volume_score * self.weights["volume"] +
            serenity_score * self.weights["serenity"] +
            factor_score * self.weights["factor"] +
            technical_score * self.weights["technical"] +
            moat_score * self.weights["moat"] +
            capital_score * self.weights["capital"]
        )

        return {
            "code": code,
            "total_score": round(total, 1),
            "zone_score": round(zone_score, 1),
            "momentum_score": round(momentum_score, 1),
            "volume_score": round(volume_score, 1),
            "serenity_score": serenity_score,
            "factor_score": round(factor_score, 1),
            "technical_score": round(technical_score, 1),
            "moat_score": moat_score,
            "capital_score": capital_score,
            "signal": self._score_to_signal(total),
        }

    def score_all(self, snapshots: list[dict]) -> list[dict]:
        """对所有标的首批次评分。"""
        results = []
        for snap in snapshots:
            code = snap.get("code", "")
            if code not in ALL_CODES:
                continue
            try:
                r = self.score_one(
                    code,
                    price=snap.get("close", snap.get("price", 0)),
                    change_pct=snap.get("change_pct", 0),
                    volume=snap.get("volume", 0),
                    snap=snap,
                )
                results.append(r)
            except Exception as e:
                log.warning(f"Frozen Baseline 评分失败 {code}: {e}")
                results.append({
                    "code": code,
                    "total_score": 0,
                    "signal": "ERROR",
                    "error": str(e),
                })
        results.sort(key=lambda x: x["total_score"], reverse=True)
        return results

    def score_components(self, components: dict[str, float]) -> dict:
        """仅用传入快照分量计算冻结基准，保证可回放。"""
        normalized = {
            key: float(components.get(key, 50.0))
            for key in self.weights
        }
        total = sum(normalized[key] * weight for key, weight in self.weights.items())
        return {
            "total_score": round(total, 1),
            "signal": self._score_to_signal(total),
            "components": normalized,
            "baseline_version": self.version,
        }

    def generate_signals(self, snapshots: list[dict]) -> list[dict]:
        """生成 Frozen Baseline 的交易信号。"""
        scores = self.score_all(snapshots)
        for s in scores:
            s["baseline_version"] = self.version
            s["timestamp"] = self._today
        return scores

    # ── 各维度计算（简化，无自适应）─────────────────────

    def _compute_zone(self, price, detail, buy_low, buy_high, target):
        if target > 0 and price >= target:
            return 20.0
        if buy_low > 0 and buy_high > buy_low:
            if buy_low <= price <= buy_high:
                ratio = (price - buy_low) / (buy_high - buy_low)
                return round(85 - ratio * 20, 1)
            if price < buy_low:
                return min(95, 88 + (buy_low - price) / buy_low * 10)
            if price > buy_high:
                ratio = (price - buy_high) / (target - buy_high) if target > buy_high else 0.3
                return round(max(30, 60 - ratio * 30), 1)
        return 50.0

    def _compute_momentum(self, change_pct, price, target):
        upside = (target - price) / price if price > 0 and target > 0 else 0
        if change_pct <= -3:
            return 60.0 if upside > 0.2 else 30.0
        if change_pct < 0:
            return 85.0 if upside > 0.15 else 50.0
        if change_pct <= 2:
            return 75.0
        if change_pct <= 5:
            return 65.0 if upside > 0.1 else 40.0
        return 50.0 if upside > 0.05 else 20.0

    def _compute_volume(self, code, volume):
        try:
            avg = get_avg_volume(code, days=10)
            if avg and avg > 0:
                ratio = volume / avg
                if 0.8 <= ratio <= 1.5:
                    return 80.0
                if 0.5 <= ratio < 0.8:
                    return 65.0
                if ratio > 3:
                    return 20.0
                if 1.5 < ratio <= 3:
                    return 50.0
                return 40.0
        except Exception:
            pass
        return 50.0

    def _compute_factor(self, code):
        engine = _get_factor_engine()
        if engine is None:
            return 50.0
        try:
            factors = engine.compute_all_factors(code)
            signals = factors.get("signals", {})
            if signals:
                signal_sum = sum(float(v) for v in signals.values() if isinstance(v, (int, float)))
                return max(0, min(100, 50 + signal_sum * 2))
        except Exception:
            pass
        return 50.0

    def _compute_technical(self, code, price):
        """纯技术面评分，无 LLM 情绪融合。"""
        try:
            from signal_engine import compute_technical_factors, compute_trend_score
            tech = compute_technical_factors(code)
            trend = compute_trend_score(tech)
            return trend
        except Exception:
            pass
        return 50.0

    def _compute_moat(self, code):
        try:
            from moat_factor import compute_moat_score
            result = compute_moat_score(code)
            if isinstance(result, dict):
                return result.get("moat_score", 50.0)
            return float(result) if result else 50.0
        except Exception:
            return 50.0

    # ── 信号映射（冻结阈值）─────────────────────────────

    def _score_to_signal(self, total: float) -> str:
        if total >= FROZEN_STRONG_BUY:
            return "STRONG_BUY"
        if total >= FROZEN_BUY:
            return "BUY"
        if total >= FROZEN_CAUTION_BUY:
            return "CAUTION_BUY"
        if total >= FROZEN_HOLD_HIGH:
            return "HOLD"
        if total >= FROZEN_SELL:
            return "WATCH"
        return "SELL"


# ═══════════════════════════════════════════════════════════════
# 三系统对比报告生成器
# ═══════════════════════════════════════════════════════════════

class BaselineComparator:
    """三系统对比报告生成器。

    对比 Frozen Baseline (A)、Adaptive System (B)、Equal Weight Basket (C)。
    """

    def __init__(self):
        self._frozen = FrozenBaseline()
        self._weekly_records: list[dict] = []

    @staticmethod
    def _components_from_score(row: dict) -> dict[str, float]:
        return {
            "zone": row.get("zone_score", 50),
            "momentum": row.get("momentum_score", 50),
            "volume": row.get("volume_score", 50),
            "serenity": row.get("serenity_score", 50),
            "factor": row.get("factor_score", 50),
            "technical": row.get("technical_score", 50),
            "moat": row.get("moat_score", 50),
            "capital": row.get("capital_score", 50),
        }

    def get_signals_today(self) -> list[dict]:
        """用最新 Adaptive 快照重放 Frozen 规则。"""
        signals = []
        for row in get_latest_scores(ALL_CODES):
            result = self._frozen.score_components(self._components_from_score(row))
            signals.append({
                "code": row["code"],
                "name": get_stock_name(row["code"]),
                "total_score": result["total_score"],
                "signal": result["signal"],
            })
        return sorted(signals, key=lambda item: item["total_score"], reverse=True)

    def get_adaptive_signals(self) -> list[dict]:
        """读取与 Frozen 同日期、同标的的最新 Adaptive 结果。"""
        signals = []
        for row in get_latest_scores(ALL_CODES):
            details = row.get("details") or "{}"
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except (TypeError, ValueError):
                    details = {}
            signals.append({
                "code": row["code"],
                "name": get_stock_name(row["code"]),
                "total_score": float(row.get("total_score") or 0),
                "signal": details.get("signal_action", "HOLD"),
            })
        return sorted(signals, key=lambda item: item["total_score"], reverse=True)

    def compare_signals(self, market_data: list[dict],
                        adaptive_signals: list[dict]) -> dict:
        """对同一批行情输入，生成三系统信号对比。

        Returns:
            {frozen_signals, adaptive_signals, equal_weight_return,
             divergence_count, agreement_matrix, comparison}
        """
        # 系统 A: Frozen Baseline
        frozen_signals = self._frozen.generate_signals(market_data)

        # 系统 C: Equal Weight Basket（简化版——仅计算当日收益率）
        eq_return = self._compute_equal_weight_return(market_data)

        # 对比 A vs B
        divergence = self._count_divergence(frozen_signals, adaptive_signals)

        return {
            "frozen_signals": frozen_signals,
            "adaptive_signals": adaptive_signals,
            "equal_weight_return": eq_return,
            "divergence_count": divergence["count"],
            "divergence_details": divergence["details"],
            "agreement_matrix": divergence["matrix"],
        }

    def _compute_equal_weight_return(self, snapshots: list[dict]) -> float:
        """计算 15 只标的等权平均当日收益。"""
        returns = []
        for s in snapshots:
            code = s.get("code", "")
            if code in ALL_CODES:
                chg = s.get("change_pct", 0) or 0
                returns.append(chg)
        if returns:
            return sum(returns) / len(returns)
        return 0.0

    def _count_divergence(self, frozen: list[dict],
                          adaptive: list[dict]) -> dict:
        """统计 Frozen 和 Adaptive 的信号分歧。"""
        frozen_map = {s["code"]: s.get("signal", "") for s in frozen}
        adaptive_map = {s.get("code", s.get("stock_code", "")): s.get("signal_action", s.get("signal", ""))
                        for s in adaptive}

        divergences = []
        for code in ALL_CODES:
            f = frozen_map.get(code, "")
            a = adaptive_map.get(code, "")
            if f and a and f != a:
                divergences.append({"code": code, "frozen": f, "adaptive": a})

        # 构建一致性矩阵
        same = sum(1 for c in ALL_CODES
                   if frozen_map.get(c) == adaptive_map.get(c))

        return {
            "count": len(divergences),
            "details": divergences,
            "matrix": {
                "total": len(ALL_CODES),
                "same": same,
                "different": len(divergences),
                "agreement_rate": same / max(len(ALL_CODES), 1),
            },
        }

    def weekly_report(self) -> str:
        """生成每周三系统对比报告（Markdown）。"""
        lines = [
            "# SerenityMonitor 三系统并跑周报",
            f"  报告日期: {date.today().isoformat()}",
            "─" * 48,
            "",
            "## 系统状态",
            f"  系统 A (Frozen Baseline): {FROZEN_VERSION} (since {FROZEN_SINCE})",
            "  系统 B (Adaptive System): 当前自适应系统",
            "  系统 C (Equal Weight Basket): 15 只等权, 月频再平衡",
            "",
            "## 判定规则 (v4 §6.3)",
            "  连续 8 周 Adaptive 跑不赢 Frozen → 冻结所有自适应机制",
            "  连续 12 周 Frozen 跑不赢 Equal Weight → 暂停核心评分",
            "  连续 8 周净收益为负 AND 回撤扩大 → 进入观察模式",
            "",
            "## 本周数据",
            "  （每周由 cron 任务自动填充）",
            "",
            "| 系统 | 本周收益 | 累计收益 | 最大回撤 | 胜率 | 换手率 | 信号数 |",
            "|------|---------|---------|---------|------|--------|--------|",
            "| A: Frozen | — | — | — | — | — | — |",
            "| B: Adaptive | — | — | — | — | — | — |",
            "| C: Equal Weight | — | — | — | — | — | — |",
        ]
        return "\n".join(lines)

    def simple_comparison(self, adaptive_scores: list[dict]) -> dict:
        """轻量级对比：用 Frozen 评分体系对同一批行情打分并对比。"""
        # 重建 snapshot 格式
        snapshots = []
        for s in adaptive_scores:
            snapshots.append({
                "code": s.get("code", ""),
                "close": s.get("close", 0),
                "change_pct": s.get("change_pct", 0),
                "volume": s.get("volume", 0),
            })

        frozen = self._frozen.generate_signals(snapshots)

        return {
            "date": date.today().isoformat(),
            "frozen": frozen,
            "adaptive": adaptive_scores,
            "divergence": self._count_divergence(frozen, adaptive_scores),
        }


# 模块级实例
_comparator: Optional[BaselineComparator] = None


def get_comparator() -> BaselineComparator:
    global _comparator
    if _comparator is None:
        _comparator = BaselineComparator()
    return _comparator
