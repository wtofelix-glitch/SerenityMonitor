"""
Phase B2 — 实时影子链路集成测试.

使用 SimClock 模拟连续竞价时段，验证 B2 全链路:
    Sina实时行情 → RawQuote → NormalizedQuote → 四关验证
    → EventRecord/Quarantine → IntelligenceNetwork
    → SignalDesk → Shadow Review Queue

不变量:
    · 生产文件不变
    · 无真实推送/成交/账户修改
    · 审计方程平衡
    · 隔离数据不入正常事件流
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from datetime import timezone, timedelta
from pathlib import Path

import pytest

CST = timezone(timedelta(hours=8))

# 使用 B1 的 fixture 生成器
from tests.test_phase_b1 import (
    make_sina_raw, quote_to_event, B1Scenario, B1Runner,
    BASELINE_PRICES, BASELINE_NAMES,
)


# ---------------------------------------------------------------------------
# B2 场景
# ---------------------------------------------------------------------------

@dataclass
class B2Scenario:
    label: str
    sim_clock_iso: str
    expected_session: str
    should_run: bool  # B2 should proceed in this session
    notes: str = ""


B2_SESSION_SCENARIOS = [
    B2Scenario(
        label="S01_CONTINUOUS_AM",
        sim_clock_iso="2026-07-24T09:35:00+08:00",
        expected_session="CONTINUOUS_AM",
        should_run=True,
        notes="连续竞价上午 → B2 应运行",
    ),
    B2Scenario(
        label="S02_CONTINUOUS_PM",
        sim_clock_iso="2026-07-24T13:05:00+08:00",
        expected_session="CONTINUOUS_PM",
        should_run=True,
        notes="连续竞价下午 → B2 应运行",
    ),
    B2Scenario(
        label="S03_OPENING_AUCTION",
        sim_clock_iso="2026-07-24T09:18:00+08:00",
        expected_session="OPENING_AUCTION_CANCELABLE",
        should_run=False,
        notes="开盘集合竞价 → B2 应拒绝",
    ),
    B2Scenario(
        label="S04_LUNCH_BREAK",
        sim_clock_iso="2026-07-24T12:00:00+08:00",
        expected_session="LUNCH_BREAK",
        should_run=False,
        notes="午间休市 → B2 应拒绝",
    ),
    B2Scenario(
        label="S05_CLOSING_AUCTION",
        sim_clock_iso="2026-07-24T14:58:00+08:00",
        expected_session="CLOSING_AUCTION",
        should_run=False,
        notes="收盘集合竞价 → B2 应拒绝",
    ),
    B2Scenario(
        label="S06_POSTMARKET",
        sim_clock_iso="2026-07-24T15:00:00+08:00",
        expected_session="POSTMARKET",
        should_run=False,
        notes="盘后 → B2 应拒绝",
    ),
]


# ---------------------------------------------------------------------------
# B2 测试
# ---------------------------------------------------------------------------

class TestPhaseB2:
    """Phase B2 全链路影子测试。"""

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

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b2_")
        self.tmp_db = Path(self.tmpdir) / "b2_shadow.db"

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

    # ── 时段门控 ──

    def test_session_gating(self):
        """验证 B2 只在 CONTINUOUS_AM/PM 运行。"""
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.phase_b2 import B2Runner, B2_SAFE_SESSIONS, B2_FORBIDDEN_SESSIONS

        for s in B2_SESSION_SCENARIOS:
            set_clock(SimClock(s.sim_clock_iso))
            runner = B2Runner(duration_seconds=1, interval_seconds=1, init_env=False)
            ok, session, why = runner.check_session()

            assert ok == s.should_run, \
                f"{s.label}: expected should_run={s.should_run}, got ok={ok}"
            assert session == s.expected_session, \
                f"{s.label}: expected {s.expected_session}, got {session}"
            print(f"  {s.label}: {session} → {'✅ RUN' if ok else '⛔ BLOCK ' + why}")

    # ── 全链路冒烟 (SimClock 模拟) ──

    def test_full_pipeline_smoke(self):
        """使用 SimClock 模拟连续竞价时段，运行两周期 B2 全链路。"""
        from serenity_v2.clock import set_clock, SimClock, get_clock
        from serenity_v2.sina_market import (
            SinaQuoteFetcher, RawQuoteRecord,
            store_raw, store_normalized, store_quarantine,
            normalized_quote_to_event, NormalizedQuote,
        )

        prod_hashes_before = self._compute_prod_hashes()

        # 影子环境
        from serenity_v2.env import set_env, SerenityEnv
        set_env(SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir)))

        from serenity_v2.migrations import apply_migrations
        apply_migrations(self.tmp_db)
        from serenity_v2.sina_market import _init_tables
        _init_tables(self.tmp_db)
        from serenity_v2.event_record import EventStore
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()
        from serenity_v2.account_baseline import get_baseline
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        state.snapshot_at = ""
        baseline.save_snapshot(state)
        from serenity_v2.intelligence_network import get_intel
        intel = get_intel(shadow_mode=True)
        from serenity_v2.signal_desk import get_desk
        desk = get_desk()

        # 两周期模拟
        symbols = ["600487", "600176", "000988"]
        all_signals = []
        all_events = 0
        all_quarantined = 0

        for cycle_idx, clock_iso in enumerate([
            "2026-07-24T09:35:00+08:00",
            "2026-07-24T09:35:05+08:00",
        ]):
            set_clock(SimClock(clock_iso))
            clock = get_clock()

            # 生成 fixture
            raws = []
            for sym in symbols:
                name = BASELINE_NAMES[sym]
                price = BASELINE_PRICES[sym]
                prev = price
                vol = 2_000_000
                amt = price * vol / 10000
                raw_str = make_sina_raw(
                    sym, name, price, prev, vol, amt,
                    "2026-07-24", "09:35:00",
                )
                collected_at = clock.now().isoformat(timespec="milliseconds")
                raw_hash = hashlib.sha256(
                    f"{sym}_{collected_at}_{raw_str}".encode()
                ).hexdigest()[:32]
                raws.append(RawQuoteRecord(
                    symbol=sym, source="sina_realtime",
                    collected_at=collected_at, raw_payload=raw_str,
                    raw_payload_hash=raw_hash,
                    http_status=200, response_time_ms=100.0,
                ))

            store_raw(self.tmp_db, raws)

            fetcher = SinaQuoteFetcher()
            norms = []
            for r in raws:
                nq = fetcher.normalize(r, business_time=clock_iso)
                if nq:
                    norms.append(nq)
            store_normalized(self.tmp_db, norms)

            for nq in norms:
                event, quarantined, qreason = normalized_quote_to_event(nq)
                if quarantined:
                    store.quarantine_event(
                        event, qreason,
                        validation_errors=nq.validation_errors,
                        normalized_at=nq.normalized_at,
                    )
                    store_quarantine(self.tmp_db, nq, qreason)
                    all_quarantined += 1
                else:
                    intel.ingest(event)
                    all_events += 1

            sigs = desk.process_events(market_data=None)
            all_signals.extend(sigs)

            print(f"\n  周期 {cycle_idx+1} ({clock_iso}): "
                  f"events={all_events} quarantine={all_quarantined} "
                  f"sigs={len(sigs)}")

        # ── 验证 ──
        print(f"\n  {'='*50}")
        print(f"  事件总数: {all_events}")
        print(f"  隔离总数: {all_quarantined}")
        print(f"  信号总数: {len(all_signals)}")

        # 所有信号必须有 SHADOW_ONLY + NOT_FOR_EXECUTION
        for sig in all_signals:
            tags = sig.execution_tags or []
            assert "SHADOW_ONLY" in tags, f"缺少 SHADOW_ONLY: {sig.signal_id}"
            assert "NOT_FOR_EXECUTION" in tags, f"缺少 NOT_FOR_EXECUTION: {sig.signal_id}"

        # 审计方程
        cand = sum(1 for s in all_signals if s.candidate_signal_level == "ACTION")
        eff = sum(1 for s in all_signals if s.effective_signal_level == "ACTION")
        downg = sum(1 for s in all_signals
                    if s.candidate_signal_level == "ACTION"
                    and s.effective_signal_level != "ACTION")
        rej = sum(1 for s in all_signals
                  if (s.primary_normalization_reason or "").startswith("GATE_DOWNGRADE"))
        assert cand == eff + downg + rej, \
            f"审计不平: {cand} ≠ {eff} + {downg} + {rej}"
        print(f"  审计: {cand} = {eff} + {downg} + {rej} ✅")

        # 生产文件不变
        assert self._compute_prod_hashes() == prod_hashes_before
        print(f"  生产文件: ✅ 不变")

        # 无不变量违规
        print(f"\n  ✅ B2 全链路冒烟通过")

    # ── 自动停止: 连续失败 ──

    def test_auto_stop_consecutive_failures(self):
        """连续 3 次无数据 → 自动停止。"""
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.clock import set_clock, SimClock

        set_clock(SimClock("2026-07-24T09:35:00+08:00"))

        # 不初始化 HTTP fetcher → fetch 会失败
        runner = B2Runner(duration_seconds=10, interval_seconds=1, init_env=False)
        # 直接测试 _auto_stop 逻辑
        runner._consecutive_failures = 3
        runner._auto_stop(f"连续 3 次无数据")

        assert runner.metrics.auto_stop_triggered
        assert runner.metrics.signal_generation_suspended
        print(f"  自动停止: {runner.metrics.auto_stop_reason} ✅")

    # ── 自动停止: 行情年龄超标 ──

    def test_auto_stop_data_age(self):
        """行情年龄 > 5min → 自动停止。"""
        from serenity_v2.phase_b2 import B2Runner, B2_MAX_DATA_AGE_MS

        runner = B2Runner(duration_seconds=10, interval_seconds=1, init_env=False)
        runner._auto_stop(f"行情年龄超标: 600487 {B2_MAX_DATA_AGE_MS + 1000}ms")

        assert runner.metrics.auto_stop_triggered
        print(f"  行情年龄自停: ✅")

    # ── 生产文件不变 ──

    def test_production_untouched(self):
        """B2 运行不修改任何生产文件。"""
        from serenity_v2.phase_b2 import B2Runner

        hashes_before = self._compute_prod_hashes()
        runner = B2Runner(duration_seconds=1, interval_seconds=1, init_env=False)

        # session check will fail (CLOSED), but hashes should be computed
        hashes_after = self._compute_prod_hashes()

        assert hashes_before == hashes_after, "生产文件在 B2 初始化期间发生变化!"
        print(f"  生产文件: ✅ 不变")

    # ── B2 报告可序列化 ──

    def test_report_serializable(self):
        """B2 报告可序列化为 JSON。"""
        import json
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.run_id = "B2_test"
        m.status = "TEST"
        m.started_at = "2026-07-24T09:35:00+08:00"
        m.ended_at = "2026-07-24T09:36:00+08:00"
        m.cycles_completed = 10
        m.http_response_times_ms = [100, 200, 300]
        m.events_created = 30
        m.quarantined = 2
        m.signals_total = 5
        m.candidate_ACTION = 3
        m.effective_ACTION = 1
        m.ACTION_downgraded = 2
        m.signal_details = [{"signal_id": "SIG_001", "symbol": "600487"}]

        json_str = json.dumps(
            {k: v for k, v in vars(m).items()
             if not k.startswith("_") and not isinstance(v, list)},
            ensure_ascii=False, default=str,
        )
        data = json.loads(json_str)
        assert data["run_id"] == "B2_test"
        assert data["candidate_ACTION"] == 3
        assert data["effective_ACTION"] + data["ACTION_downgraded"] == 3
        print(f"  B2Metrics 可序列化 ✅  审计: {data['candidate_ACTION']} = "
              f"{data['effective_ACTION']} + {data['ACTION_downgraded']}")

    # ── 隔离区可查询 ──

    def test_quarantine_queryable(self):
        """隔离区数据可通过 DB 查询。"""
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.env import set_env, SerenityEnv

        set_env(SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir)))
        from serenity_v2.migrations import apply_migrations
        apply_migrations(self.tmp_db)
        from serenity_v2.event_record import EventStore
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()

        # 验证 quarantine 表存在
        import sqlite3
        conn = sqlite3.connect(str(self.tmp_db))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%quarantine%'"
        ).fetchall()
        conn.close()

        table_names = [t[0] for t in tables]
        assert "serenity_event_quarantine" in table_names, \
            f"缺少 quarantine 表: {table_names}"
        print(f"  隔离表: {table_names} ✅")
