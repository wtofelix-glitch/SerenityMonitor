"""
影子退池监控 — pool_shadow_monitor.py

只记录、不改变交易行为。每天对固定池中每只标的计算三类独立证据：
  1. 硬资格 — 退市/ST/停牌/流动性/数据缺失
  2. OOS 经济贡献 — 扣除成本后的边际收益、回撤贡献、冗余度
  3. 产业逻辑 — 需人工确认，自动只标记"超过 N 天未审查"

状态机: ACTIVE → PROBATION → ENTRY_FROZEN → ARCHIVE
- 自动推进仅限于硬资格类证据
- OOS 经济贡献和产业逻辑只标记原因，不自动退池
- STOCK_MAP 绝不被修改

Usage:
    python3 pool_shadow_monitor.py              # 记录当天状态
    python3 pool_shadow_monitor.py --report     # 输出退池证据报告
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from config import ALL_CODES, STOCK_MAP, get_stock_name

# ═══════════════════════════════════════════════════════════════
# 阈值常量
# ═══════════════════════════════════════════════════════════════

# 硬资格
MIN_DAILY_AMOUNT = 500_000          # 日均成交额 < 50 万 → 流动性不足
MAX_DATA_GAP_DAYS = 10              # 连续缺失 > 10 天 → 数据缺失
MAX_SUSPENSION_DAYS = 20            # 连续停牌 > 20 天 → 标记
ST_PREFIX_BLOCK = True               # ST 标的禁止新开仓

# OOS 经济贡献
MIN_SIGNAL_SAMPLES = 10             # 至少 10 个信号样本才评估
MARGINAL_RETURN_THRESHOLD = -0.03   # 5 日平均净收益 < -3% → 经济贡献为负
DRAWDOWN_CONTRIBUTION_MAX = 0.30    # 贡献回撤 > 30% → 明显拖累组合
CORRELATION_REDUNDANCY_THRESHOLD = 0.85  # 与其他标的相关性 > 0.85 → 冗余

# 产业逻辑
LOGIC_REVIEW_MAX_DAYS = 90          # 超过 90 天未审查 → 标记

# 状态机
PROBATION_MIN_DAYS = 30             # PROBATION 至少 30 天后才能推进
ENTRY_FREEZE_DAYS = 120             # 120 天 OOS 期间冻结出入

# 置信度分层
CONFIDENCE_TIERS = ("OBSERVATION", "LOW_CONFIDENCE", "ACTIONABLE")
ECONOMIC_LOW_CONFIDENCE_MIN_SAMPLES = 30  # ≥30 已结算样本 → LOW_CONFIDENCE，否则 OBSERVATION

# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

UNIVERSE_STATUSES = ("ACTIVE", "PROBATION", "ENTRY_FROZEN", "ARCHIVE")
HARD_QUAL_FLAGS = (
    "st_warning",           # ST 标的
    "suspended_long",       # 长期停牌
    "low_liquidity",        # 流动性不足
    "data_gap",             # 数据长期缺失
    "not_mainboard",        # 非主板标的
    "delisted",             # 已退市
)
ECONOMIC_FLAGS = (
    "negative_marginal_return",   # 边际收益长期为负
    "drawdown_contributor",       # 明显增加组合回撤
    "excess_turnover",            # 增加换手成本
    "redundant_correlation",      # 与其他标的高度冗余
)
LOGIC_FLAGS = (
    "logic_stale",          # 产业逻辑超过审查期
    "structural_change",    # 基本面或政策发生结构性改变
    "bottleneck_lost",      # Serenity 瓶颈地位失效
)


@dataclass
class StockStatus:
    """单只标的的影子状态。"""
    code: str
    name: str
    status: str = "ACTIVE"
    tier: int = 0

    # 硬资格
    hard_qual_flags: list[str] = field(default_factory=list)
    st_detected: bool = False
    days_since_last_trade: int = 0
    avg_daily_amount: float = 0.0
    data_gap_days: int = 0

    # OOS 经济贡献
    economic_flags: list[str] = field(default_factory=list)
    signal_samples: int = 0
    avg_net_return_5d: float = 0.0
    drawdown_contribution_pct: float = 0.0
    max_pairwise_correlation: float = 0.0
    most_correlated_with: str = ""

    # 产业逻辑
    logic_flags: list[str] = field(default_factory=list)
    days_since_logic_review: int = 0

    # 状态机
    probation_days: int = 0
    entry_frozen_days: int = 0

    # 置信度分层 — 最高层决定整体
    confidence: str = "OBSERVATION"

    @property
    def overall_confidence(self) -> str:
        """整体置信度 — 取所有标记中的最高层。

        ACTIONABLE > LOW_CONFIDENCE > OBSERVATION。
        仅硬资格异常可达 ACTIONABLE。
        """
        if self.hard_qual_flags:
            return "ACTIONABLE"
        if self.economic_flags:
            if self.signal_samples >= ECONOMIC_LOW_CONFIDENCE_MIN_SAMPLES:
                return "LOW_CONFIDENCE"
            return "OBSERVATION"
        if self.logic_flags:
            return "OBSERVATION"
        return "OBSERVATION"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "status": self.status,
            "tier": self.tier,
            "confidence": self.overall_confidence,
            "hard_qual_flags": self.hard_qual_flags,
            "economic_flags": self.economic_flags,
            "logic_flags": self.logic_flags,
            "details": {
                "st_detected": self.st_detected,
                "days_since_last_trade": self.days_since_last_trade,
                "avg_daily_amount": round(self.avg_daily_amount, 0),
                "data_gap_days": self.data_gap_days,
                "signal_samples": self.signal_samples,
                "avg_net_return_5d": round(self.avg_net_return_5d, 4),
                "drawdown_contribution_pct": round(self.drawdown_contribution_pct, 2),
                "max_pairwise_correlation": round(self.max_pairwise_correlation, 2),
                "most_correlated_with": self.most_correlated_with,
                "days_since_logic_review": self.days_since_logic_review,
                "probation_days": self.probation_days,
            },
        }


# ═══════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════


class UniverseShadowMonitor:
    """影子退池监控器。"""

    def __init__(self, oos_frozen: bool = True):
        self.oos_frozen = oos_frozen  # 120 天 OOS 期间冻结出入

    # ── 证据计算 ──────────────────────────────────────────

    def compute_all(self, check_date: Optional[date] = None) -> list[StockStatus]:
        """计算所有标的的影子状态。"""
        today = check_date or date.today()
        results: list[StockStatus] = []

        for code in sorted(ALL_CODES):
            info = STOCK_MAP.get(code, {})
            ss = StockStatus(
                code=code,
                name=info.get("name", code),
                tier=info.get("tier", 0),
            )

            # 1. 硬资格
            self._check_hard_qualification(ss, today)

            # 2. OOS 经济贡献
            self._check_economic_contribution(ss, today)

            # 3. 产业逻辑
            self._check_industrial_logic(ss, today)

            # 4. 状态机推进
            self._advance_state(ss, today)

            results.append(ss)

        # 相关性冗余 — 需要全池数据
        self._check_correlation_redundancy(results)

        return results

    def _check_hard_qualification(self, ss: StockStatus, today: date) -> None:
        """硬资格检查 — 只有这类证据才能自动推进状态机。"""
        from db import get_conn

        conn = get_conn()
        try:
            code = ss.code

            # ST 检查：代码前缀是否仍在主板块范围内
            # 实际 ST 检测需要通过行情数据中的 is_st 标记
            # 这里检查主板前缀作为代理
            mainboard_prefixes = ("600", "601", "603", "605", "000", "002")
            if not any(code.startswith(p) for p in mainboard_prefixes):
                ss.hard_qual_flags.append("not_mainboard")

            # 检查最近交易日
            row = conn.execute(
                "SELECT MAX(date) as last_date, AVG(amount) as avg_amt "
                "FROM daily_snapshots WHERE code = ?",
                (code,),
            ).fetchone()

            if row and row["last_date"]:
                last_dt = date.fromisoformat(row["last_date"])
                ss.days_since_last_trade = (today - last_dt).days
                if ss.days_since_last_trade > MAX_SUSPENSION_DAYS:
                    ss.hard_qual_flags.append("suspended_long")

            ss.avg_daily_amount = float(row["avg_amt"] or 0) if row else 0.0
            if 0 < ss.avg_daily_amount < MIN_DAILY_AMOUNT:
                ss.hard_qual_flags.append("low_liquidity")

            # 数据连续性
            gap_row = conn.execute(
                "SELECT MAX(date) as last_score FROM scoring_history WHERE code = ?",
                (code,),
            ).fetchone()
            if gap_row and gap_row["last_score"]:
                last_score_dt = date.fromisoformat(gap_row["last_score"])
                ss.data_gap_days = (today - last_score_dt).days
                if ss.data_gap_days > MAX_DATA_GAP_DAYS:
                    ss.hard_qual_flags.append("data_gap")
            else:
                ss.data_gap_days = 999
                ss.hard_qual_flags.append("data_gap")

        finally:
            conn.close()

    def _check_economic_contribution(self, ss: StockStatus, today: date) -> None:
        """OOS 经济贡献 — 基于已结算的信号样本。"""
        from db import get_conn

        conn = get_conn()
        try:
            # 已结算信号的 5 日净收益
            rows = conn.execute(
                "SELECT return_5d FROM signal_log "
                "WHERE code = ? AND settlement_status = 'settled' "
                "AND return_5d IS NOT NULL "
                "ORDER BY date DESC LIMIT 50",
                (ss.code,),
            ).fetchall()

            if rows:
                returns = [r["return_5d"] for r in rows if r["return_5d"] is not None]
                ss.signal_samples = len(returns)
                if ss.signal_samples >= MIN_SIGNAL_SAMPLES:
                    ss.avg_net_return_5d = sum(returns) / len(returns)
                    # 扣除约 0.3% 交易成本的净收益
                    net_return = ss.avg_net_return_5d - 0.003
                    if net_return < MARGINAL_RETURN_THRESHOLD:
                        ss.economic_flags.append("negative_marginal_return")

            # 回撤贡献 — 从 frozen_comparison_history 的 divergence 推断
            div_rows = conn.execute(
                "SELECT divergence_details_json FROM frozen_comparison_history "
                "ORDER BY date DESC LIMIT 30"
            ).fetchall()
            divergence_count = 0
            for dr in div_rows:
                try:
                    details = json.loads(dr["divergence_details_json"] or "[]")
                    for d in details:
                        if d.get("code") == ss.code:
                            divergence_count += 1
                except (json.JSONDecodeError, TypeError):
                    pass
            # 如果该标的在 Frozen vs Adaptive 对比中频繁出现分歧
            # 且 Adaptive 选择了更差的方案 → 它可能是噪音源
            if divergence_count >= 5:
                ss.drawdown_contribution_pct = min(
                    divergence_count / 30 * 100, 100
                )
                if ss.drawdown_contribution_pct > DRAWDOWN_CONTRIBUTION_MAX:
                    ss.economic_flags.append("drawdown_contributor")

        finally:
            conn.close()

    def _check_industrial_logic(self, ss: StockStatus, today: date) -> None:
        """产业逻辑检查 — 只标记，不自动决定。"""
        from db import get_conn

        conn = get_conn()
        try:
            # 使用 stock_pool_audit 的 POOL_V* 入池日期作为逻辑审查的代理
            try:
                from stock_pool_audit import CURRENT_POOL
                entry_info = CURRENT_POOL.get(ss.code, {})
                entered_str = entry_info.get("entered", "")
                if entered_str:
                    entry_dt = date.fromisoformat(entered_str)
                    ss.days_since_logic_review = (today - entry_dt).days
                else:
                    ss.days_since_logic_review = 0
            except ImportError:
                ss.days_since_logic_review = 0

            # 检查是否超过审查期
            if ss.days_since_logic_review > LOGIC_REVIEW_MAX_DAYS:
                ss.logic_flags.append("logic_stale")

        finally:
            conn.close()

    def _check_correlation_redundancy(self, results: list[StockStatus]) -> None:
        """检测冗余标的 — 需要全池评分序列。"""
        from db import get_conn

        conn = get_conn()
        try:
            # 收集所有标的最近 60 天的评分序列
            score_series: dict[str, list[float]] = defaultdict(list)
            rows = conn.execute(
                "SELECT code, total_score FROM scoring_history "
                "WHERE date >= date('now', '-60 days') "
                "ORDER BY code, date"
            ).fetchall()
            for r in rows:
                score_series[r["code"]].append(r["total_score"] or 0)

            # 计算两两相关性
            codes = [s.code for s in results]
            for i, code_a in enumerate(codes):
                series_a = score_series.get(code_a, [])
                if len(series_a) < 20:
                    continue
                max_corr = 0.0
                max_code = ""
                for code_b in codes[i + 1 :]:
                    series_b = score_series.get(code_b, [])
                    if len(series_b) < 20:
                        continue
                    min_len = min(len(series_a), len(series_b))
                    corr = _pearson_correlation(series_a[-min_len:], series_b[-min_len:])
                    if corr > max_corr:
                        max_corr = corr
                        max_code = code_b
                # 找到对应的 StockStatus
                for s in results:
                    if s.code == code_a:
                        s.max_pairwise_correlation = max_corr
                        s.most_correlated_with = max_code
                        if max_corr > CORRELATION_REDUNDANCY_THRESHOLD:
                            s.economic_flags.append("redundant_correlation")
                        break
        finally:
            conn.close()

    def _advance_state(self, ss: StockStatus, today: date) -> None:
        """状态机推进 — 仅硬资格证据能自动推进到 PROBATION。

        ACTIVE → PROBATION: 硬资格证据触发
        PROBATION → ENTRY_FROZEN: probation_days ≥ 30 且硬资格证据持续
        ENTRY_FROZEN → ARCHIVE: OOS 期结束后人工确认
        """
        from db import get_conn

        conn = get_conn()
        try:
            # 读取上一次记录的状态
            prev = conn.execute(
                "SELECT status, details_json FROM universe_status_log "
                "WHERE code = ? ORDER BY date DESC LIMIT 1",
                (ss.code,),
            ).fetchone()

            if prev:
                prev_status = prev["status"]
                try:
                    details = json.loads(prev["details_json"] or "{}")
                    ss.probation_days = details.get("probation_days", 0)
                except (json.JSONDecodeError, TypeError):
                    ss.probation_days = 0
            else:
                prev_status = "ACTIVE"
                ss.probation_days = 0

            # OOS 冻结期间：标签可前进到 ENTRY_FROZEN，但不能进 ARCHIVE
            if self.oos_frozen and prev_status in ("ACTIVE", "PROBATION"):
                if ss.hard_qual_flags:
                    ss.status = "PROBATION"
                    ss.probation_days += 1
                else:
                    ss.status = "ACTIVE"
                    ss.probation_days = max(0, ss.probation_days - 1)

                if ss.probation_days >= PROBATION_MIN_DAYS and ss.hard_qual_flags:
                    ss.status = "ENTRY_FROZEN"
                else:
                    ss.status = prev_status if not ss.hard_qual_flags else "PROBATION"
            elif not self.oos_frozen:
                # 120 天后：允许推进到 ARCHIVE
                if ss.hard_qual_flags:
                    if prev_status == "ENTRY_FROZEN":
                        ss.status = "ARCHIVE"
                    elif prev_status == "PROBATION" and ss.probation_days >= PROBATION_MIN_DAYS:
                        # 需要人工确认，自动推进只到这里
                        ss.status = "ENTRY_FROZEN"
                    else:
                        ss.status = "PROBATION"
                        ss.probation_days += 1
                else:
                    # 无硬资格证据 → 恢复
                    if prev_status in ("PROBATION",):
                        ss.probation_days = max(0, ss.probation_days - 1)
                        ss.status = "ACTIVE" if ss.probation_days <= 0 else "PROBATION"
                    else:
                        ss.status = prev_status
        finally:
            conn.close()

    # ── 持久化 ────────────────────────────────────────────

    def record_daily(self, check_date: Optional[date] = None) -> list[StockStatus]:
        """计算并记录当天的影子状态。"""
        today = check_date or date.today()
        results = self.compute_all(today)

        from db import get_conn

        conn = get_conn()
        try:
            for ss in results:
                conn.execute(
                    """
                    INSERT INTO universe_status_log
                      (code, date, name, tier, status, confidence,
                       hard_qual_flags_json, economic_flags_json, logic_flags_json,
                       details_json, trigger_reasons, hypothetical_exit_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ss.code,
                        today.isoformat(),
                        ss.name,
                        ss.tier,
                        ss.status,
                        ss.overall_confidence,
                        json.dumps(ss.hard_qual_flags, ensure_ascii=False),
                        json.dumps(ss.economic_flags, ensure_ascii=False),
                        json.dumps(ss.logic_flags, ensure_ascii=False),
                        json.dumps(ss.to_dict()["details"], ensure_ascii=False),
                        json.dumps(self._trigger_reasons(ss), ensure_ascii=False),
                        today.isoformat()
                        if ss.status in ("ENTRY_FROZEN", "ARCHIVE")
                        else None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        return results

    @staticmethod
    def _trigger_reasons(ss: StockStatus) -> list[str]:
        """汇总所有触发原因。"""
        reasons = []
        if ss.hard_qual_flags:
            reasons.append("hard:" + ",".join(ss.hard_qual_flags))
        if ss.economic_flags:
            reasons.append("econ:" + ",".join(ss.economic_flags))
        if ss.logic_flags:
            reasons.append("logic:" + ",".join(ss.logic_flags))
        return reasons

    # ── 报告 ──────────────────────────────────────────────

    def generate_report(self, check_date: Optional[date] = None) -> str:
        """生成退池证据报告。"""
        results = self.compute_all(check_date)
        today = check_date or date.today()

        lines = [
            "═" * 68,
            f"  影子退池监控报告 — {today.isoformat()}",
            "═" * 68,
            "",
            f"  池规模: {len(results)} 只 | OOS 冻结: {'是' if self.oos_frozen else '否'}",
            "",
        ]

        # 按置信度分组
        by_confidence: dict[str, list[StockStatus]] = defaultdict(list)
        for s in results:
            by_confidence[s.overall_confidence].append(s)

        icon_c = {"ACTIONABLE": "🔴", "LOW_CONFIDENCE": "🟡", "OBSERVATION": "🔵"}
        for tier in ("ACTIONABLE", "LOW_CONFIDENCE", "OBSERVATION"):
            group = by_confidence.get(tier, [])
            if not group:
                continue
            tier_desc = {
                "ACTIONABLE": "硬资格异常 — 满足条件时可自动推进状态机",
                "LOW_CONFIDENCE": "经济证据初步（≥30样本）— 需积累更多OOS数据",
                "OBSERVATION": "仅记录观察 — 样本不足或需人工审查",
            }
            lines.append(f"  {icon_c.get(tier, '⚪')} {tier} ({len(group)} 只)")
            lines.append(f"     {tier_desc.get(tier, '')}")
            for s in sorted(group, key=lambda x: x.tier):
                flags_all = s.hard_qual_flags + s.economic_flags + s.logic_flags
                if flags_all:
                    lines.append(f"     {s.name:<10} T{s.tier} [{s.status}] {', '.join(flags_all)}")
                else:
                    lines.append(f"     {s.name:<10} T{s.tier} [{s.status}]")
            lines.append("")

        # 统计
        flagged = [s for s in results if s.hard_qual_flags or s.economic_flags or s.logic_flags]
        hard = [s for s in results if s.hard_qual_flags]
        econ = [s for s in results if s.economic_flags]
        logic = [s for s in results if s.logic_flags]
        actionable = [s for s in results if s.overall_confidence == "ACTIONABLE"]
        low_conf = [s for s in results if s.overall_confidence == "LOW_CONFIDENCE"]
        obs = [s for s in results if s.overall_confidence == "OBSERVATION"]

        lines.extend(
            [
                "─" * 68,
                f"  证据统计:",
                f"    🔴 ACTIONABLE:     {len(actionable)} 只 (仅硬资格异常)",
                f"    🟡 LOW_CONFIDENCE: {len(low_conf)} 只 (经济证据≥30样本)",
                f"    🔵 OBSERVATION:    {len(obs)} 只 (仅记录, 小样本/待审查)",
                f"    硬资格触发: {len(hard)} | 经济: {len(econ)} | 产业: {len(logic)}",
                "",
                "  ⚠️ 当前 OOS 冻结期：退池不执行，仅记录证据",
                "  📋 ACTIVE → PROBATION: 需硬资格证据",
                "  🔒 PROBATION → ENTRY_FROZEN: 30天持续 + 硬资格",
                "  🛑 ENTRY_FROZEN → ARCHIVE: 120天后人工确认",
                "═" * 68,
            ]
        )

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════


def _pearson_correlation(a: list[float], b: list[float]) -> float:
    """计算 Pearson 相关系数。"""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((ai - mean_a) * (bi - mean_b) for ai, bi in zip(a, b))
    den_a = sum((ai - mean_a) ** 2 for ai in a)
    den_b = sum((bi - mean_b) ** 2 for bi in b)
    den = (den_a * den_b) ** 0.5
    return num / den if den > 1e-12 else 0.0


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def main():
    import sys

    if "--report" in sys.argv:
        monitor = UniverseShadowMonitor()
        print(monitor.generate_report())
    else:
        monitor = UniverseShadowMonitor()
        results = monitor.record_daily()
        active = sum(1 for s in results if s.status == "ACTIVE")
        flagged = sum(1 for s in results if s.status != "ACTIVE")
        print(f"影子退池记录: {active} ACTIVE, {flagged} 非活跃 ({date.today().isoformat()})")
        for s in results:
            if s.status != "ACTIVE":
                print(f"  {s.status}: {s.name}({s.code}) T{s.tier}")


if __name__ == "__main__":
    main()
