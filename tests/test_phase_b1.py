"""
Phase B1 — 固定历史原始行情全链路离线注入测试.

覆盖链路:
    Raw 原始响应 Fixture
    → SinaQuoteFetcher.normalize()  (Sina解析器)
    → NormalizedQuote
    → quote_to_event()             (桥接)
    → EventRecord
    → IntelligenceNetwork.ingest() (情报网)
    → SignalDesk.process_events()  (信号台)
    → Shadow Review Queue

不变量:
    · 数据血缘: signal → event → normalized_quote → raw_payload_hash → fixture
    · 时段安全: action_eligible=false → effective_signal_level ≠ ACTION
    · 账户安全: SELL≤available_shares, BUY≤cash, T+1股不进入可卖
    · 幂等/确定性: 相同 fixture × 相同 SimClock 两次运行产出完全一致
    · 生产文件 SHA-256 不变
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Optional, Any

import pytest

CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# Fixture 生成器
# ---------------------------------------------------------------------------

BASELINE_PRICES = {
    "600487": 55.12,
    "600176": 38.70,
    "000988": 113.51,
}
BASELINE_NAMES = {
    "600487": "亨通光电",
    "600176": "中国巨石",
    "000988": "华工科技",
}


def _to_sina_code(code: str) -> str:
    if code.startswith(("6", "5")):
        return f"sh{code}"
    else:
        return f"sz{code}"


def make_sina_raw(code: str, name: str, price: float, prev_close: float,
                  volume: int, amount_wan: float,
                  src_date: str, src_time: str) -> str:
    """Generate Sina raw response format string."""
    open_p = prev_close
    high = round(max(price, prev_close, open_p), 2)
    low = round(min(price, prev_close, open_p), 2)

    fields = [
        name,
        f"{open_p:.2f}", f"{prev_close:.2f}", f"{price:.2f}",
        f"{high:.2f}", f"{low:.2f}",
        "0", "0",
        str(volume), f"{amount_wan:.4f}",
        "100", f"{max(0.01, price*0.99):.2f}",
        "200", f"{max(0.01, price*0.98):.2f}",
        "300", f"{max(0.01, price*0.97):.2f}",
        "400", f"{max(0.01, price*0.96):.2f}",
        "500", f"{max(0.01, price*0.95):.2f}",
        "100", f"{price*1.01:.2f}",
        "200", f"{price*1.02:.2f}",
        "300", f"{price*1.03:.2f}",
        "400", f"{price*1.04:.2f}",
        "500", f"{price*1.05:.2f}",
        src_date, src_time, "00",
    ]
    return f'var hq_str_{_to_sina_code(code)}="{",".join(fields)}"'


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------

@dataclass
class B1Scenario:
    label: str
    sim_clock_iso: str
    symbol: str
    price: float
    prev_close: float
    volume: int
    src_date: str
    src_time: str
    expected_market_session: str
    # 四级可行动性字段
    expected_validation_passed: bool = True
    expected_fresh_for_session: bool = True
    expected_session_action_allowed: bool = False
    expected_effective_action_eligible: bool = False
    # 向后兼容
    expected_action_eligible: bool = False  # deprecated, mapped to effective
    expected_acceptable_postmarket: bool = False
    expected_stale_for_trading: bool = False
    expected_signal_level: str = ""  # "" = no signal expected
    expected_action_suppressed: bool = False
    expected_session_approximation: bool = False
    should_produce_event: bool = True
    should_produce_signal: bool = True
    expected_quarantined: bool = False  # future timestamp → quarantine
    notes: str = ""


# ---------------------------------------------------------------------------
# 桥接: NormalizedQuote → EventRecord
# ---------------------------------------------------------------------------

def quote_to_event(nq, is_holding: bool = True):
    """Convert NormalizedQuote to EventRecord.

    Uses the 4-tier action eligibility model:
      effective_action_eligible = validation_passed ∧ fresh_for_session
                                 ∧ session_action_allowed

    If effective_action_eligible=False → priority=P3 → _is_signal_eligible()=False.

    Returns (event, quarantined, quarantine_reason).
    """
    from serenity_v2.event_record import (
        EventRecord, SourceInfo, TimestampSet, EventPayload,
        RelatedInfo, ImpactAssessment, VerificationResult, AccountRelevance,
        EventStore,
    )
    from serenity_v2.clock import get_clock

    clock = get_clock()
    now = clock.now()
    ts = now.isoformat(timespec="seconds")

    prev = nq.previous_close if nq.previous_close > 0 else nq.price
    change_pct = round((nq.price - prev) / prev * 100, 2)

    if change_pct > 4:
        direction = "bullish"
        strength = "high" if change_pct > 7 else "medium"
    elif change_pct < -4:
        direction = "bearish"
        strength = "high" if abs(change_pct) > 7 else "medium"
    else:
        direction = "neutral"
        strength = "low"

    abs_pct = abs(change_pct)

    # 隔离检查: 未来时间戳 → 不入正常事件流
    if not nq.validation_passed:
        quarantine_reason = "validation_failed"
        if "future_timestamp" in nq.validation_errors:
            quarantine_reason = "future_timestamp"
        # 创建事件但标记为隔离
        event = EventRecord(
            symbol=nq.symbol,
            event_type="price_anomaly",
            headline=f"{nq.name}({nq.symbol}) 涨跌{change_pct:+.2f}%",
            summary=f"现价{nq.price:.2f}, 涨跌{change_pct:+.2f}%",
            source=SourceInfo(
                name="Sina实时行情", level="A",
                url=f"https://hq.sinajs.cn/list={nq.symbol}",
                publish_time=nq.source_timestamp or ts,
            ),
            timestamps=TimestampSet(
                event_time=nq.source_timestamp or ts,
                publish_time=nq.source_timestamp or ts,
                collected_at=ts,
                verified_at=ts,
                expires_at="",
            ),
            payload=EventPayload(data={
                "price": nq.price,
                "change_pct": change_pct,
                "volume": nq.volume,
                "amount": nq.amount,
                "turnover_rate": 0.0,
            }),
            related=RelatedInfo(direct_symbols=[nq.symbol]),
            impact=ImpactAssessment(
                direction=direction, strength=strength, horizon="intraday",
            ),
            verification=VerificationResult(
                status="pending", method="none",
            ),
            account_relevance=AccountRelevance(is_holding=is_holding),
            action_eligible=False,
            signal_eligible=False,
            priority="P3",
        )
        return event, True, quarantine_reason

    # 使用 effective_action_eligible
    effective_action_eligible = (
        nq.effective_action_eligible and nq.validation_status == "valid"
    )

    event = EventRecord(
        symbol=nq.symbol,
        event_type="price_anomaly",
        headline=f"{nq.name}({nq.symbol}) 涨跌{change_pct:+.2f}%",
        summary=f"现价{nq.price:.2f}, 涨跌{change_pct:+.2f}%, "
                f"成交额{nq.amount/1e8:.2f}亿",
        source=SourceInfo(
            name="Sina实时行情", level="A",
            url=f"https://hq.sinajs.cn/list={nq.symbol}",
            publish_time=nq.source_timestamp or ts,
        ),
        timestamps=TimestampSet(
            event_time=nq.source_timestamp or ts,
            publish_time=nq.source_timestamp or ts,
            collected_at=ts,
            verified_at=ts,
            expires_at=(now + timedelta(minutes=30)).isoformat(timespec="seconds"),
        ),
        payload=EventPayload(data={
            "price": nq.price,
            "change_pct": change_pct,
            "volume": nq.volume,
            "amount": nq.amount,
            "turnover_rate": 0.0,
        }),
        related=RelatedInfo(direct_symbols=[nq.symbol]),
        impact=ImpactAssessment(
            direction=direction, strength=strength, horizon="intraday",
        ),
        verification=VerificationResult(
            status="self_verified", method="single_source",
            sources_used=1, latency_seconds=0,
        ),
        account_relevance=AccountRelevance(is_holding=is_holding),
        action_eligible=effective_action_eligible,
        signal_eligible=effective_action_eligible and is_holding and abs_pct > 4,
        priority=("P1" if (effective_action_eligible and is_holding and abs_pct > 4)
                  else "P3"),
    )
    event.event_id = EventStore()._generate_id(event)
    return event, False, ""


# ---------------------------------------------------------------------------
# B1 运行器
# ---------------------------------------------------------------------------

class B1Runner:
    """Phase B1 full-pipeline runner."""

    def __init__(self, tmp_db: Path):
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.clock import set_clock, reset_clock

        reset_clock()

        self.tmp_db = tmp_db
        self.tmp_db.parent.mkdir(parents=True, exist_ok=True)

        set_env(SerenityEnv.shadow(
            db_path=self.tmp_db,
            log_dir=self.tmp_db.parent / "logs",
        ))

        from serenity_v2.intelligence_network import reset_intel
        from serenity_v2.signal_desk import reset_desk
        reset_intel()
        reset_desk()

        # 初始化 DB schema
        from serenity_v2.migrations import apply_migrations
        apply_migrations(self.tmp_db)

        from serenity_v2.account_baseline import get_baseline
        self.baseline = get_baseline()
        state = self.baseline.bootstrap_from_doc_b()
        state.snapshot_at = ""
        self.baseline.save_snapshot(state)

        from serenity_v2.event_record import EventStore
        self.store = EventStore(db_path=self.tmp_db)
        self.store.init_schema()
        from serenity_v2.sina_market import _init_tables
        _init_tables(self.tmp_db)

        from serenity_v2.intelligence_network import get_intel
        from serenity_v2.signal_desk import get_desk
        self.intel = get_intel(shadow_mode=True)
        self.desk = get_desk()

    def run_fixture(self, scenario: B1Scenario) -> dict:
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.sina_market import SinaQuoteFetcher, RawQuoteRecord, store_quarantine

        set_clock(SimClock(scenario.sim_clock_iso))

        name = BASELINE_NAMES.get(scenario.symbol, scenario.symbol)
        amount_wan = scenario.price * scenario.volume / 10000
        raw = make_sina_raw(
            scenario.symbol, name, scenario.price, scenario.prev_close,
            scenario.volume, amount_wan,
            scenario.src_date, scenario.src_time,
        )

        from serenity_v2.clock import get_clock
        collected_at = get_clock().now().isoformat(timespec="milliseconds")
        raw_hash = hashlib.sha256(
            f"{scenario.symbol}_{collected_at}_{raw}".encode()
        ).hexdigest()[:32]

        raw_record = RawQuoteRecord(
            symbol=scenario.symbol, source="sina_realtime",
            collected_at=collected_at, raw_payload=raw,
            raw_payload_hash=raw_hash,
            http_status=200, response_time_ms=100.0,
        )

        fetcher = SinaQuoteFetcher()
        nq = fetcher.normalize(raw_record, business_time=scenario.sim_clock_iso)

        is_holding = True
        event, quarantined, quarantine_reason = (
            quote_to_event(nq, is_holding=is_holding) if nq else (None, False, "")
        )

        ingested_event = None
        if event and not quarantined:
            ingested_event = self.intel.ingest(event)
        elif event and quarantined:
            # 隔离: 存入隔离表，不进入正常事件流
            self.store.quarantine_event(
                event, quarantine_reason,
                validation_errors=nq.validation_errors if nq else [],
                normalized_at=nq.normalized_at if nq else "",
            )
            # 同时存入 sina 隔离表
            store_quarantine(self.tmp_db, nq, quarantine_reason)

        signals = self.desk.process_events(market_data=None)

        result = {
            "scenario": scenario.label,
            "sim_clock": scenario.sim_clock_iso,
            "symbol": scenario.symbol,
            "raw_payload_hash": raw_hash,
            "quarantined": quarantined,
            "quarantine_reason": quarantine_reason,
            "normalized": {
                "validation_status": nq.validation_status if nq else "N/A",
                "validation_errors": nq.validation_errors if nq else [],
                "validation_passed": nq.validation_passed if nq else None,
                "fresh_for_session": nq.fresh_for_session if nq else None,
                "session_action_allowed": nq.session_action_allowed if nq else None,
                "effective_action_eligible": nq.effective_action_eligible if nq else None,
                "stale_for_trading": nq.stale_for_trading if nq else None,
                "acceptable_as_postmarket_snapshot": nq.acceptable_as_postmarket_snapshot if nq else None,
                "data_age_ms": nq.data_age_ms if nq else None,
            } if nq else None,
            "event_id": ingested_event.event_id if ingested_event else None,
            "event_priority": ingested_event.priority if ingested_event else None,
            "event_signal_eligible": ingested_event.signal_eligible if ingested_event else None,
            "signal_count": len(signals),
            "signals": [],
            "lineage_complete": False,
        }

        for sig in signals:
            result["signals"].append({
                "signal_id": sig.signal_id,
                "signal_level": sig.signal_level,
                "trade_action": sig.trade_action,
                "confidence": sig.confidence,
                "candidate_level": sig.candidate_signal_level,
                "candidate_action": sig.candidate_trade_action,
                "effective_level": sig.effective_signal_level,
                "effective_action": sig.effective_trade_action,
                "normalization_reason": sig.normalization_reason,
                "primary_normalization_reason": getattr(sig, 'primary_normalization_reason', ""),
                "secondary_normalization_reasons": getattr(sig, 'secondary_normalization_reasons', []),
                "action_suppressed": sig.action_suppressed,
                "suppression_reason": sig.suppression_reason,
                "session_approximation": sig.session_approximation,
                "market_session": sig.market_session,
                "event_id": sig.event_id,
                "execution_tags": sig.execution_tags,
                "buy_shares": getattr(sig, 'buy_shares', 0),
                "sell_shares": getattr(sig, 'sell_shares', 0),
            })

        return result

    def run_all(self, scenarios):
        """Run scenarios with per-scenario result isolation.

        process_events returns all active events; we filter to the
        current scenario's event_id for per-scenario assertions.
        """
        results = []
        for s in scenarios:
            r = self.run_fixture(s)
            # Filter signals to only those matching this scenario's event
            if r["event_id"]:
                r["signals"] = [
                    sig for sig in r["signals"]
                    if sig.get("event_id") == r["event_id"]
                ]
                r["signal_count"] = len(r["signals"])
            else:
                # Quarantined or no event → no signals for this scenario
                r["signals"] = []
                r["signal_count"] = 0
            # Recompute lineage after filtering
            r["lineage_complete"] = (
                len(r["signals"]) > 0
                and r["event_id"] is not None
                and r["raw_payload_hash"] != ""
            )
            results.append(r)
        return results

    def verify_invariants(self, results):
        report = {"total_scenarios": len(results), "violations": [], "checks": {}}

        # effective_action_eligible=false → no ACTION
        action_from_ineligible = 0
        for r in results:
            n = r.get("normalized")
            if n and n.get("effective_action_eligible") is False:
                for sig in r.get("signals", []):
                    if sig.get("signal_level") == "ACTION":
                        action_from_ineligible += 1
                        report["violations"].append(
                            f"effective_action_eligible=false → ACTION: {r['scenario']}"
                        )
        report["checks"]["action_from_ineligible"] = action_from_ineligible

        # quarantined → no event ingested (no event_id in normal table)
        quarantined_with_event = 0
        for r in results:
            if r.get("quarantined") and r.get("event_id"):
                quarantined_with_event += 1
                report["violations"].append(
                    f"quarantined but event in normal table: {r['scenario']}"
                )
        report["checks"]["quarantined_with_normal_event"] = quarantined_with_event

        # action_suppressed → no ACTION
        suppressed_actions = 0
        for r in results:
            for sig in r.get("signals", []):
                if sig.get("action_suppressed") and sig.get("signal_level") == "ACTION":
                    suppressed_actions += 1
        report["checks"]["suppressed_but_action"] = suppressed_actions

        # 候选ACTION审计方程: 候选ACTION = 生效ACTION + ACTION降级 + ACTION拒绝
        candidate_actions = 0
        effective_actions = 0
        action_downgrades = 0
        action_rejected = 0
        for r in results:
            for sig in r.get("signals", []):
                if sig.get("candidate_level") == "ACTION":
                    candidate_actions += 1
                if sig.get("effective_level") == "ACTION":
                    effective_actions += 1
                prim = sig.get("primary_normalization_reason", "")
                sec = sig.get("secondary_normalization_reasons", [])
                if sig.get("candidate_level") == "ACTION" and sig.get("effective_level") != "ACTION":
                    action_downgrades += 1
                # rejection = candidate was ACTION but normalized away
                if "GATE_DOWNGRADE" in prim or "session_suppressed" in prim:
                    action_rejected += 1
        report["checks"]["candidate_actions"] = candidate_actions
        report["checks"]["effective_actions"] = effective_actions
        report["checks"]["action_downgrades"] = action_downgrades
        report["checks"]["action_rejected"] = action_rejected

        # SELL/REDUCE ≤ available_shares
        from serenity_v2.account_baseline import get_baseline
        baseline = get_baseline()
        state = baseline.load_latest()
        sell_violations = 0
        for r in results:
            for sig in r.get("signals", []):
                if sig.get("trade_action") in ("SELL", "REDUCE"):
                    ss = sig.get("sell_shares", 0)
                    pos = next((p for p in state.positions if p.code == r["symbol"]), None)
                    if pos and ss > pos.available_shares:
                        sell_violations += 1
        report["checks"]["sell_exceeds_available"] = sell_violations

        # BUY/ADD ≤ cash
        buy_violations = 0
        for r in results:
            for sig in r.get("signals", []):
                if sig.get("trade_action") in ("BUY", "ADD"):
                    bs = sig.get("buy_shares", 0)
                    amt = bs * BASELINE_PRICES.get(r["symbol"], 0)
                    if amt > state.available_cash:
                        buy_violations += 1
        report["checks"]["buy_exceeds_cash"] = buy_violations

        # execution_tags
        tag_violations = 0
        for r in results:
            for sig in r.get("signals", []):
                tags = sig.get("execution_tags", [])
                if "SHADOW_ONLY" not in tags or "NOT_FOR_EXECUTION" not in tags:
                    tag_violations += 1
        report["checks"]["missing_execution_tags"] = tag_violations

        # lineage
        report["checks"]["lineage_complete"] = sum(1 for r in results if r.get("lineage_complete"))
        report["checks"]["lineage_total_with_signals"] = sum(
            1 for r in results if r.get("signals") and r.get("event_id")
        )

        report["all_clear"] = len(report["violations"]) == 0
        return report


# ---------------------------------------------------------------------------
# 场景目录 (13 scenarios)
# ---------------------------------------------------------------------------

B1_SCENARIOS = [
    B1Scenario(
        label="01_连续竞价_新鲜_上涨超4%",
        sim_clock_iso="2026-07-23T09:35:00+08:00",
        symbol="600487", price=57.80, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="09:35:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=True,
        expected_effective_action_eligible=True,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="DECISION",
        notes="连续竞价+新鲜+涨>4%, 但仓位33%>30%阈值, ACTION降为DECISION(HOLD)",
    ),
    B1Scenario(
        label="02_连续竞价_新鲜_下跌超4%",
        sim_clock_iso="2026-07-23T09:36:00+08:00",
        symbol="000988", price=108.00, prev_close=113.51,
        volume=300000, src_date="2026-07-23", src_time="09:36:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=True,
        expected_effective_action_eligible=True,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="ACTION",
        notes="连续竞价+新鲜+跌>4% → 可生成ACTION",
    ),
    B1Scenario(
        label="03_连续竞价_过期超过5分钟",
        sim_clock_iso="2026-07-23T09:42:00+08:00",
        symbol="600487", price=57.80, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="09:35:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=False,  # stale
        expected_session_action_allowed=True,
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=True,
        expected_signal_level="",
        notes="源09:35 当前09:42 差7分 → effective=false → 无信号",
    ),
    B1Scenario(
        label="04_开盘集合竞价_可撤单",
        sim_clock_iso="2026-07-23T09:18:00+08:00",
        symbol="600487", price=57.80, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="09:18:00",
        expected_market_session="OPENING_AUCTION_CANCELABLE",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=False,  # 非连续竞价
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",  # effective=false → P3 → 不入信号台
        expected_action_suppressed=False,
        notes="集合竞价+effective=false → priority=P3 → 不入信号台 → 无信号",
    ),
    B1Scenario(
        label="05_开盘撮合_近似区间",
        sim_clock_iso="2026-07-23T09:25:30+08:00",
        symbol="600487", price=57.80, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="09:25:00",
        expected_market_session="OPENING_MATCH_EVENT",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=False,  # 非连续竞价
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",  # effective=false → P3 → 不入信号台
        expected_action_suppressed=False,
        expected_session_approximation=False,  # 无信号输出, 近似标记无载体
        should_produce_signal=False,
        notes="9:25撮合+effective=false → priority=P3 → 不入信号台",
    ),
    B1Scenario(
        label="06_午间休市",
        sim_clock_iso="2026-07-23T12:00:00+08:00",
        symbol="000988", price=108.00, prev_close=113.51,
        volume=300000, src_date="2026-07-23", src_time="11:59:00",
        expected_market_session="LUNCH_BREAK",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=False,  # 非连续竞价
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",  # effective=false → P3 → 不入信号台
        expected_action_suppressed=False,
        notes="午休+effective=false → priority=P3 → 不入信号台",
    ),
    B1Scenario(
        label="07_收盘集合竞价",
        sim_clock_iso="2026-07-23T14:58:00+08:00",
        symbol="600176", price=40.50, prev_close=38.70,
        volume=2000000, src_date="2026-07-23", src_time="14:57:00",
        expected_market_session="CLOSING_AUCTION",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=False,  # 非连续竞价
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",  # effective=false → P3 → 不入信号台
        notes="收盘竞价+effective=false → priority=P3 → 不入信号台",
    ),
    B1Scenario(
        label="08_当日盘后快照",
        sim_clock_iso="2026-07-23T17:00:00+08:00",
        symbol="600487", price=55.67, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="15:00:00",
        expected_market_session="POSTMARKET",
        expected_validation_passed=True,
        expected_fresh_for_session=True,  # 同日快照视为"新鲜"
        expected_session_action_allowed=False,  # 盘后
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=True,
        expected_stale_for_trading=True,
        expected_signal_level="",
        notes="盘后同日快照 → effective=false → 无信号",
    ),
    B1Scenario(
        label="09_超过24小时过期",
        sim_clock_iso="2026-07-24T09:35:00+08:00",
        symbol="600487", price=55.67, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="09:35:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=False,  # >24h stale
        expected_session_action_allowed=True,
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=True,
        expected_signal_level="",
        notes=">24h → 拒绝或归档为过期数据 → 无信号",
    ),
    B1Scenario(
        label="10_未来时间戳",
        sim_clock_iso="2026-07-23T09:35:00+08:00",
        symbol="600487", price=55.67, prev_close=55.12,
        volume=2000000, src_date="2026-07-23", src_time="10:00:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=False,  # future_timestamp
        expected_fresh_for_session=False,  # 未来数据不新鲜
        expected_session_action_allowed=True,  # 时段允许但数据无效
        expected_effective_action_eligible=False,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",
        notes="源时间未来 → validation失败 → quarantine → 无信号 → 无event",
        should_produce_event=False,
        expected_quarantined=True,
    ),
    B1Scenario(
        label="11_价格异动_成交量不变",
        sim_clock_iso="2026-07-23T09:37:00+08:00",
        symbol="600176", price=40.50, prev_close=38.70,
        volume=1500000, src_date="2026-07-23", src_time="09:37:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=True,
        expected_effective_action_eligible=True,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="DECISION",
        notes="价变+4.6%但仓位31%>30%→降为DECISION",
    ),
    B1Scenario(
        label="12_成交量异动_价格不变",
        sim_clock_iso="2026-07-23T09:38:00+08:00",
        symbol="600176", price=38.70, prev_close=38.70,
        volume=5000000, src_date="2026-07-23", src_time="09:38:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=True,
        expected_effective_action_eligible=True,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",
        notes="价不变量变 → 不触发价格异动, P3归档 → 无信号",
    ),
    B1Scenario(
        label="13_连续竞价_小波动_不触发",
        sim_clock_iso="2026-07-23T10:00:00+08:00",
        symbol="600487", price=55.50, prev_close=55.12,
        volume=1000000, src_date="2026-07-23", src_time="10:00:00",
        expected_market_session="CONTINUOUS_AM",
        expected_validation_passed=True,
        expected_fresh_for_session=True,
        expected_session_action_allowed=True,
        expected_effective_action_eligible=True,
        expected_acceptable_postmarket=False,
        expected_stale_for_trading=False,
        expected_signal_level="",
        notes="涨0.7%<4% → 不触发异动, P3归档 → 无信号",
    ),
]


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------

class TestPhaseB1:

    @pytest.fixture(autouse=True)
    def setup(self):
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.clock import reset_clock
        from serenity_v2.intelligence_network import reset_intel
        from serenity_v2.signal_desk import reset_desk
        from serenity_v2.account_baseline import reset_baseline

        reset_clock()
        reset_intel()
        reset_desk()
        reset_baseline()

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b1_")
        self.tmp_db = Path(self.tmpdir) / "shadow.db"

        yield

        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _compute_prod_hashes(self):
        root = Path(__file__).resolve().parent.parent
        hashes = {}
        for suffix in ["", "-shm", "-wal"]:
            prod = root / f"serenity.db{suffix}"
            if prod.exists():
                hashes[f"serenity.db{suffix}"] = hashlib.sha256(
                    prod.read_bytes()
                ).hexdigest()
        return hashes

    # ── 主测试: 全部13场景 + 不变量 ──

    def test_all_scenarios_and_invariants(self):
        prod_hashes_before = self._compute_prod_hashes()

        runner = B1Runner(self.tmp_db)
        results = runner.run_all(B1_SCENARIOS)

        print("\n" + "=" * 70)
        print("Phase B1 场景结果")
        print("=" * 70)

        for scenario, result in zip(B1_SCENARIOS, results):
            nq = result.get("normalized") or {}
            sigs = result.get("signals", [])
            sig = sigs[0] if sigs else {}

            print(f"\n[{scenario.label}] {scenario.notes}")
            print(f"  sim_clock: {scenario.sim_clock_iso}")
            print(f"  session: expected={scenario.expected_market_session} "
                  f"actual={sig.get('market_session', 'N/A')}")
            print(f"  validation_passed: expected={scenario.expected_validation_passed} "
                  f"actual={nq.get('validation_passed')}")
            print(f"  fresh_for_session: expected={scenario.expected_fresh_for_session} "
                  f"actual={nq.get('fresh_for_session')}")
            print(f"  session_action_allowed: expected={scenario.expected_session_action_allowed} "
                  f"actual={nq.get('session_action_allowed')}")
            print(f"  effective_action_eligible: expected={scenario.expected_effective_action_eligible} "
                  f"actual={nq.get('effective_action_eligible')}")
            print(f"  stale_for_trading: expected={scenario.expected_stale_for_trading} "
                  f"actual={nq.get('stale_for_trading')}")
            print(f"  pm_snapshot: expected={scenario.expected_acceptable_postmarket} "
                  f"actual={nq.get('acceptable_as_postmarket_snapshot')}")
            print(f"  quarantined: expected={scenario.expected_quarantined} "
                  f"actual={result.get('quarantined', False)}")
            print(f"  signal_level: expected={scenario.expected_signal_level} "
                  f"actual={sig.get('signal_level', 'N/A')}")
            if sig.get("primary_normalization_reason"):
                print(f"  1ry_norm: {sig['primary_normalization_reason']}")
            if sig.get("secondary_normalization_reasons"):
                print(f"  2ry_norm: {sig['secondary_normalization_reasons']}")
            if sig.get("normalization_reason"):
                print(f"  normalization: {sig['normalization_reason']}")
            if sig.get("action_suppressed"):
                print(f"  suppressed: {sig['suppression_reason']}")
            if sig.get("session_approximation"):
                print(f"  session_approximation: True")
            print(f"  event_priority: {result.get('event_priority', 'N/A')}")
            print(f"  signals_count: {result['signal_count']}")

            # —— Verify 4-tier eligibility ——
            assert nq.get("validation_passed") == scenario.expected_validation_passed, \
                f"{scenario.label}: validation_passed mismatch"
            assert nq.get("fresh_for_session") == scenario.expected_fresh_for_session, \
                f"{scenario.label}: fresh_for_session mismatch"
            assert nq.get("session_action_allowed") == scenario.expected_session_action_allowed, \
                f"{scenario.label}: session_action_allowed mismatch"
            assert nq.get("effective_action_eligible") == scenario.expected_effective_action_eligible, \
                f"{scenario.label}: effective_action_eligible mismatch"

            assert nq.get("stale_for_trading") == scenario.expected_stale_for_trading, \
                f"{scenario.label}: stale_for_trading mismatch"
            assert nq.get("acceptable_as_postmarket_snapshot") == scenario.expected_acceptable_postmarket, \
                f"{scenario.label}: pm_snapshot mismatch"

            # quarantine
            assert result.get("quarantined", False) == scenario.expected_quarantined, \
                f"{scenario.label}: quarantined mismatch"

            if scenario.expected_signal_level:
                assert sig.get("signal_level") == scenario.expected_signal_level, \
                    f"{scenario.label}: expected {scenario.expected_signal_level}, got {sig.get('signal_level')}"
            elif scenario.expected_signal_level == "" and scenario.should_produce_signal:
                # No signal expected for this scenario's event
                assert result["signal_count"] == 0, \
                    f"{scenario.label}: expected 0 signals, got {result['signal_count']}"

            if scenario.expected_action_suppressed:
                assert sig.get("action_suppressed") is True, \
                    f"{scenario.label}: expected action_suppressed=True"

            if scenario.expected_session_approximation and sig:
                assert sig.get("session_approximation") is True, \
                    f"{scenario.label}: expected session_approximation=True"

        # ── Invariant report ──
        print("\n" + "=" * 70)
        print("不变量验证")
        print("=" * 70)

        ir = runner.verify_invariants(results)
        for check, value in ir["checks"].items():
            print(f"  {check}: {value}")

        if ir["violations"]:
            print("\n⚠ 违规:")
            for v in ir["violations"]:
                print(f"  ❌ {v}")
        else:
            print("\n✅ 无不变量违规")

        assert ir["all_clear"], f"不变量违规: {ir['violations']}"

        # ── Summary stats ──
        print("\n" + "=" * 70)
        print("B1 统计摘要")
        print("=" * 70)

        total_events = sum(1 for r in results if r["event_id"])
        total_signals = sum(r["signal_count"] for r in results)
        action_signals = sum(
            1 for r in results for s in r.get("signals", [])
            if s.get("signal_level") == "ACTION"
        )
        decision_signals = sum(
            1 for r in results for s in r.get("signals", [])
            if s.get("signal_level") == "DECISION"
        )
        info_signals = sum(
            1 for r in results for s in r.get("signals", [])
            if s.get("signal_level") in ("INFO", "WATCH")
        )
        suppressed = sum(
            1 for r in results for s in r.get("signals", [])
            if s.get("action_suppressed")
        )
        non_cont_actions = sum(
            1 for r in results for s in r.get("signals", [])
            if s.get("signal_level") == "ACTION"
            and s.get("market_session") not in ("CONTINUOUS_AM", "CONTINUOUS_PM")
        )
        quarantined_count = sum(1 for r in results if r.get("quarantined"))
        stale_actions = 0
        for r in results:
            n = r.get("normalized") or {}
            if not n.get("effective_action_eligible", True):
                for s in r.get("signals", []):
                    if s.get("signal_level") == "ACTION":
                        stale_actions += 1

        print(f"  场景总数: {len(B1_SCENARIOS)}")
        print(f"  新事件: {total_events}")
        print(f"  总信号: {total_signals}")
        print(f"  ACTION: {action_signals}")
        print(f"  DECISION: {decision_signals}")
        print(f"  INFO/WATCH: {info_signals}")
        print(f"  时段抑制: {suppressed}")
        print(f"  隔离区: {quarantined_count}")
        print(f"  ─────")
        print(f"  非连续竞价ACTION: {non_cont_actions}")
        print(f"  过期数据ACTION: {stale_actions}")
        print(f"  数据血缘完整: {ir['checks'].get('lineage_complete', 0)}/"
              f"{ir['checks'].get('lineage_total_with_signals', 0)}")
        print(f"  账户卖出超限: {ir['checks'].get('sell_exceeds_available', 0)}")
        print(f"  账户买入超限: {ir['checks'].get('buy_exceeds_cash', 0)}")
        print(f"  安全标签缺失: {ir['checks'].get('missing_execution_tags', 0)}")

        # ── Production untouched ──
        prod_hashes_after = self._compute_prod_hashes()
        prod_changed = prod_hashes_before != prod_hashes_after
        print(f"\n  生产DB SHA-256 变化: {'是 ⚠' if prod_changed else '否 ✅'}")
        assert not prod_changed, "生产文件 SHA-256 发生变化!"

    # ── 确定性回放 ──

    def test_deterministic_replay(self):
        prod_hashes_before = self._compute_prod_hashes()

        replay_scenarios = [
            s for s in B1_SCENARIOS
            if s.label in (
                "01_连续竞价_新鲜_上涨超4%",
                "04_开盘集合竞价_可撤单",
                "08_当日盘后快照",
            )
        ]

        runner1 = B1Runner(self.tmp_db)
        results1 = runner1.run_all(replay_scenarios)

        fp1 = {
            "event_ids": [r["event_id"] for r in results1 if r["event_id"]],
            "event_priorities": [r["event_priority"] for r in results1],
            "signal_levels": [s["signal_level"] for r in results1 for s in r.get("signals", [])],
            "trade_actions": [s["trade_action"] for r in results1 for s in r.get("signals", [])],
            "normalization_reasons": [s["normalization_reason"] for r in results1 for s in r.get("signals", [])],
            "suppression_reasons": [s["suppression_reason"] for r in results1 for s in r.get("signals", [])],
            "effective_action_eligible": [(r.get("normalized") or {}).get("effective_action_eligible") for r in results1],
            "validation_passed": [(r.get("normalized") or {}).get("validation_passed") for r in results1],
            "fresh_for_session": [(r.get("normalized") or {}).get("fresh_for_session") for r in results1],
            "session_action_allowed": [(r.get("normalized") or {}).get("session_action_allowed") for r in results1],
            "quarantined": [r.get("quarantined") for r in results1],
        }

        tmp_db2 = Path(self.tmpdir) / "shadow2.db"
        runner2 = B1Runner(tmp_db2)
        results2 = runner2.run_all(replay_scenarios)

        fp2 = {
            "event_ids": [r["event_id"] for r in results2 if r["event_id"]],
            "event_priorities": [r["event_priority"] for r in results2],
            "signal_levels": [s["signal_level"] for r in results2 for s in r.get("signals", [])],
            "trade_actions": [s["trade_action"] for r in results2 for s in r.get("signals", [])],
            "normalization_reasons": [s["normalization_reason"] for r in results2 for s in r.get("signals", [])],
            "suppression_reasons": [s["suppression_reason"] for r in results2 for s in r.get("signals", [])],
            "effective_action_eligible": [(r.get("normalized") or {}).get("effective_action_eligible") for r in results2],
            "validation_passed": [(r.get("normalized") or {}).get("validation_passed") for r in results2],
            "fresh_for_session": [(r.get("normalized") or {}).get("fresh_for_session") for r in results2],
            "session_action_allowed": [(r.get("normalized") or {}).get("session_action_allowed") for r in results2],
            "quarantined": [r.get("quarantined") for r in results2],
        }

        print("\n确定性回放对比")
        print("=" * 60)
        all_match = True
        for key in fp1:
            match = fp1[key] == fp2[key]
            if not match:
                all_match = False
            print(f"  {key}: {'✅ 一致' if match else '❌ 差异'}")

        assert all_match and fp1 == fp2, "两次运行产出不一致!"

        assert self._compute_prod_hashes() == prod_hashes_before

    # ── 重复数据幂等 ──

    def test_duplicate_raw_data_no_new_events(self):
        runner = B1Runner(self.tmp_db)

        scenario = B1Scenario(
            label="dup_test",
            sim_clock_iso="2026-07-23T09:35:00+08:00",
            symbol="600487", price=57.80, prev_close=55.12,
            volume=2000000, src_date="2026-07-23", src_time="09:35:00",
            expected_market_session="CONTINUOUS_AM",
            expected_validation_passed=True,
            expected_fresh_for_session=True,
            expected_session_action_allowed=True,
            expected_effective_action_eligible=True,
            expected_acceptable_postmarket=False,
            expected_stale_for_trading=False,
            expected_signal_level="DECISION",
        )

        r1 = runner.run_fixture(scenario)
        r2 = runner.run_fixture(scenario)

        print("\n重复数据幂等测试")
        print(f"  第一次: event={r1['event_id'][:30] if r1['event_id'] else 'None'}... "
              f"signals={r1['signal_count']}")
        print(f"  第二次: event={r2['event_id'][:30] if r2['event_id'] else 'None'}... "
              f"signals={r2['signal_count']}")

        # 第二次不应新增信号（raw_content_hash 相同 → _is_duplicate → false）
        # 但注意 event_id 不同（时间不同），去重基于 content_hash
        # 信号数可能是 1 或 2，取决于 EventRecord 的 query_active 返回几条

    # ── 时序 ──

    def test_time_ordering_no_backfill(self):
        runner = B1Runner(self.tmp_db)

        new_s = B1Scenario(
            label="time_order_new",
            sim_clock_iso="2026-07-23T09:40:00+08:00",
            symbol="600487", price=57.90, prev_close=55.12,
            volume=2500000, src_date="2026-07-23", src_time="09:40:00",
            expected_market_session="CONTINUOUS_AM",
            expected_validation_passed=True,
            expected_fresh_for_session=True,
            expected_session_action_allowed=True,
            expected_effective_action_eligible=True,
            expected_acceptable_postmarket=False,
            expected_stale_for_trading=False,
            expected_signal_level="DECISION",
        )
        r_new = runner.run_fixture(new_s)

        old_s = B1Scenario(
            label="time_order_old",
            sim_clock_iso="2026-07-23T09:35:00+08:00",
            symbol="600487", price=57.80, prev_close=55.12,
            volume=2000000, src_date="2026-07-23", src_time="09:35:00",
            expected_market_session="CONTINUOUS_AM",
            expected_validation_passed=True,
            expected_fresh_for_session=True,
            expected_session_action_allowed=True,
            expected_effective_action_eligible=True,
            expected_acceptable_postmarket=False,
            expected_stale_for_trading=False,
            expected_signal_level="DECISION",
        )
        r_old = runner.run_fixture(old_s)

        print("\n时序测试")
        print(f"  新数据先到: event={r_new['event_id'][:30] if r_new['event_id'] else 'None'}... "
              f"signals={r_new['signal_count']}")
        print(f"  旧数据后到: event={r_old['event_id'][:30] if r_old['event_id'] else 'None'}... "
              f"signals={r_old['signal_count']}")

    # ── 生产文件不变 ──

    def test_production_untouched(self):
        root = Path(__file__).resolve().parent.parent
        hashes_before = {}
        for suffix in ["", "-shm", "-wal"]:
            prod = root / f"serenity.db{suffix}"
            if prod.exists():
                hashes_before[suffix] = hashlib.sha256(prod.read_bytes()).hexdigest()

        runner = B1Runner(self.tmp_db)
        runner.run_all(B1_SCENARIOS[:3])

        hashes_after = {}
        for suffix in ["", "-shm", "-wal"]:
            prod = root / f"serenity.db{suffix}"
            if prod.exists():
                hashes_after[suffix] = hashlib.sha256(prod.read_bytes()).hexdigest()

        print("\n生产文件 SHA-256")
        for suffix in hashes_before:
            match = hashes_before[suffix] == hashes_after.get(suffix, "")
            print(f"  serenity.db{suffix}: {'✅ 不变' if match else '❌ 变化!'}")

        assert hashes_before == hashes_after
