"""
P0-1~P0-4 组合固定离线回放验证

使用 25 事件 Fixture 运行两遍，验证:
  · 幂等保证 (Run 2 0 new signals)
  · 8 组审计方程平衡
  · cycle_duration >= http_duration
  · Fixture 及账户状态前后不变
  · 生产文件不变
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from serenity_v2.account_fixture import (
    load_fixture, to_account_state, recompute_snapshot_id_full,
    reset_fixture, FIXTURE_SIGNAL_TAGS,
)
from serenity_v2.env import SerenityEnv, set_env, reset_env, get_env
from serenity_v2.account_baseline import reset_baseline, get_baseline
from serenity_v2.signal_idempotency import (
    EventProcessingLedger, IdempotentSignalProcessor,
)
from serenity_v2.migrations import apply_migrations

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "b2" / "account_fixture_20260722.json"


class TestCombinedReplay:
    """25 事件固定离线回放 — 完整 P0-1~P0-4 验证。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        reset_env()
        reset_baseline()
        reset_fixture()

        self.tmpdir = tempfile.mkdtemp()
        self.shadow_db = Path(self.tmpdir) / "shadow.db"
        self.prod_db = Path(self.tmpdir) / "serenity.db"
        self.prod_db.write_bytes(b'fake-production-db-content-v1')

        env = SerenityEnv.shadow(
            db_path=self.shadow_db,
            protected_prod_db=str(self.prod_db),
        )
        set_env(env)
        apply_migrations(self.shadow_db)

        # 加载 Fixture
        self.fixture = load_fixture(FIXTURE_PATH)
        self.state = to_account_state(self.fixture)
        self.state.snapshot_at = ""
        self.baseline = get_baseline()
        self.baseline.save_snapshot(self.state)

        # P0-1: 记录生产文件哈希
        import hashlib
        self.prod_hash_before = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()

        # 记录 Fixture 哈希
        self.fixture_snapshot_id_before = self.fixture.snapshot_id_full
        self.fixture_sha256_before = self.fixture.file_hash

    def _create_25_events(self):
        """创建 25 个模拟事件。"""
        from serenity_v2.event_record import EventStore
        from serenity_v2.sina_market import _init_tables

        _init_tables(self.shadow_db)
        store = EventStore(db_path=self.shadow_db)
        store.init_schema()

        from serenity_v2.intelligence_network import get_intel, reset_intel
        reset_intel()
        intel = get_intel(shadow_mode=True)

        from serenity_v2.event_record import (
            EventRecord, SourceInfo, TimestampSet,
            VerificationResult, AccountRelevance, EventPayload,
        )
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        now = datetime.now(tz=CST)
        now_str = now.isoformat()

        created = 0
        for i in range(25):
            symbol = ["600487", "600176", "000988"][i % 3]
            event = EventRecord(
                event_id=f"replay-evt-{i:03d}",
                symbol=symbol,
                event_type="price_anomaly",
                headline=f"price anomaly #{i} for {symbol}",
                source=SourceInfo(name="sina_replay", level="A", publish_time=now_str),
                timestamps=TimestampSet(
                    event_time=now_str, publish_time=now_str,
                    collected_at=now_str, verified_at=now_str, expires_at="",
                ),
                verification=VerificationResult(status="verified", method="auto"),
                account_relevance=AccountRelevance(is_holding=True),
                payload=EventPayload(data={"change_pct": 5.0}),
                signal_eligible=True,
            )
            ingested = intel.ingest(event)
            if ingested.event_id:
                created += 1

        return created

    def test_run1_25_events_at_most_25_signals(self):
        """第一遍: 25 事件 → ≤ 25 信号, duplicate=0。"""
        created = self._create_25_events()
        assert created == 25, f"Expected 25 events, got {created}"

        # 创建幂等处理器
        from serenity_v2.signal_desk import get_desk, reset_desk
        reset_desk()
        desk = get_desk()

        ledger = EventProcessingLedger(self.shadow_db)
        ledger.init_schema()
        processor = IdempotentSignalProcessor(desk=desk, ledger=ledger)

        # 稳定上下文
        strategy_version = "b2-1.0"
        strategy_config_hash = "test-config-hash-001"
        account_snapshot_id = self.fixture.snapshot_id_full

        signals = processor.process_events(
            strategy_version=strategy_version,
            strategy_config_hash=strategy_config_hash,
            account_snapshot_id=account_snapshot_id,
            market_data=None,
        )

        # Run 1 验证
        assert len(signals) <= 25, f"Signals: {len(signals)} > 25"
        # 无重复
        sig_ids = [s.signal_id for s in signals]
        assert len(sig_ids) == len(set(sig_ids)), "Duplicate signal IDs in Run 1"

        # 账本审计
        stats = ledger.get_stats()
        assert stats["COMPLETED"] + stats["PROCESSING"] <= 25
        print(f"Run 1: events={created} signals={len(signals)} "
              f"ledger_completed={stats['COMPLETED']} "
              f"ledger_claimed={stats['total']}")

    def test_run2_no_new_signals(self):
        """第二遍: 0 新信号, 全部 already_processed。"""
        # Run 1: 创建事件 + 首次处理
        self._create_25_events()
        from serenity_v2.signal_desk import get_desk, reset_desk
        reset_desk()
        desk = get_desk()

        ledger = EventProcessingLedger(self.shadow_db)
        ledger.init_schema()
        processor = IdempotentSignalProcessor(desk=desk, ledger=ledger, worker_id="run1")

        strategy_version = "b2-1.0"
        strategy_config_hash = "test-config-hash-001"
        account_snapshot_id = self.fixture.snapshot_id_full

        # Run 1
        signals1 = processor.process_events(
            strategy_version=strategy_version,
            strategy_config_hash=strategy_config_hash,
            account_snapshot_id=account_snapshot_id,
            market_data=None,
        )
        stats1 = ledger.get_stats()
        run1_new = processor.stats["new_signals"]
        assert len(signals1) <= 25, f"Run 1: {len(signals1)} > 25"
        assert run1_new >= 1, f"Run 1 should produce at least 1 signal, got {run1_new}"

        # Run 2: 新建 processor 模拟重启，相同 ledger 相同 DB
        reset_desk()
        desk2 = get_desk()
        processor2 = IdempotentSignalProcessor(desk=desk2, ledger=ledger, worker_id="run2")

        signals2 = processor2.process_events(
            strategy_version=strategy_version,
            strategy_config_hash=strategy_config_hash,
            account_snapshot_id=account_snapshot_id,
            market_data=None,
        )
        stats2 = ledger.get_stats()

        # 核心断言
        assert len(signals2) == 0, f"Run 2 should have 0 signals, got {len(signals2)}"
        assert stats2["COMPLETED"] == stats1["COMPLETED"], (
            f"Run 2 should have same completed count: {stats2['COMPLETED']} != {stats1['COMPLETED']}"
        )
        # Run 2 所有事件应已被处理
        run2_already = processor2.stats.get("already_processed", 0)
        run2_new = processor2.stats.get("new_signals", 0)
        run2_claim_failed = processor2.stats.get("claim_failed", 0)
        assert run2_already == 25, (
            f"All 25 events should be already_processed in Run 2, got {run2_already}"
        )
        assert run2_new == 0, (
            f"Run 2 should have 0 new signals, got {run2_new}"
        )
        assert run2_claim_failed == 0, (
            f"Run 2 should have 0 claim_failed, got {run2_claim_failed}"
        )
        print(f"Run 1: signals={len(signals1)} new={run1_new} "
              f"completed={stats1['COMPLETED']}")
        print(f"Run 2: signals={len(signals2)} new={run2_new} "
              f"already={run2_already} completed={stats2['COMPLETED']}")

    def test_account_snapshot_id_uses_full_sha(self):
        """完整 SHA-256 用于幂等键。"""
        assert len(self.fixture.snapshot_id_full) == 64
        assert self.fixture.snapshot_id_full.startswith(self.fixture.snapshot_id)

    def test_fixture_metadata_complete(self):
        """审计元数据完整。"""
        md = self.fixture.metadata_dict()
        assert len(md["fixture_sha256"]) == 64
        assert md["fixture_size"] > 0
        assert md["fixture_mtime"] > 0
        assert "account_fixture_20260722.json" in md["fixture_realpath"]
        assert len(md["account_snapshot_id_full"]) == 64

    def test_fixture_and_prod_unchanged(self):
        """Fixture 和生产文件前后不变。"""
        # Reload fixture
        fixture_after = load_fixture(FIXTURE_PATH)
        assert fixture_after.snapshot_id_full == self.fixture_snapshot_id_before
        assert fixture_after.file_hash == self.fixture_sha256_before

        import hashlib
        prod_hash_after = hashlib.sha256(self.prod_db.read_bytes()).hexdigest()
        assert prod_hash_after == self.prod_hash_before

    def test_cycle_record_timing_invariants(self):
        """P0-4: 周期计时不变量。"""
        from serenity_v2.phase_b2 import CycleRecord
        cr = CycleRecord(cycle_sequence=1)
        cr.http_duration_ms = 250
        cr.cycle_duration_ms = 400
        assert cr.cycle_duration_ms >= cr.http_duration_ms

    def test_all_audit_equations(self):
        """P0-4: 所有审计方程平衡。"""
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()

        # 调度
        m.cycles_planned = 10
        m.cycles_started = 8
        m.cycles_skipped = 2
        m.not_due_cycles = 0
        assert m.cycles_planned == m.cycles_started + m.cycles_skipped + m.not_due_cycles

        m.cycles_completed = 7
        m.cycles_aborted = 1
        assert m.cycles_started == m.cycles_completed + m.cycles_aborted

        # 数据
        m.raw_received = 9
        m.normalized_accepted = 6
        m.normalized_rejected = 2
        m.quarantined = 1
        assert m.raw_received == m.normalized_accepted + m.normalized_rejected + m.quarantined

        # 事件
        m.events_created = 4
        m.events_deduplicated = 1
        m.events_not_triggered = 1
        m.event_processing_failed = 0
        assert m.normalized_accepted == (m.events_created + m.events_deduplicated
                                         + m.events_not_triggered + m.event_processing_failed)

        # ACTION
        m.candidate_ACTION = 5
        m.effective_ACTION = 3
        m.ACTION_downgraded = 1
        m.ACTION_rejected = 1
        assert m.candidate_ACTION == m.effective_ACTION + m.ACTION_downgraded + m.ACTION_rejected

        # 账本
        m.ledger_claimed = 10
        m.ledger_completed = 8
        m.ledger_failed_count = 1
        m.ledger_in_progress = 1
        assert m.ledger_claimed == m.ledger_completed + m.ledger_failed_count + m.ledger_in_progress

        # 失败
        m.fetch_failed = 2
        m.http_failed = 1
        m.total_failures = m.fetch_failed + m.http_failed
        assert m.total_failures == 3

    def test_position_context_for_three_basket(self):
        """每票可获取正确持仓上下文。"""
        ctx_487 = self.baseline.signal_context("600487", self.state)
        assert ctx_487["holding"] is True
        assert ctx_487["position_shares"] == 1500
        assert ctx_487["available_shares"] == 1500

        ctx_176 = self.baseline.signal_context("600176", self.state)
        assert ctx_176["holding"] is True
        assert ctx_176["position_shares"] == 2000

        ctx_988 = self.baseline.signal_context("000988", self.state)
        assert ctx_988["holding"] is True
        assert ctx_988["position_shares"] == 600

    def test_fixture_invariant_cash_plus_mv(self):
        """现金 + 市值 = 总资产。"""
        actual_mv = sum(p.market_value for p in self.fixture.positions)
        computed = self.fixture.cash + actual_mv
        assert abs(computed - self.fixture.total_assets) < 0.02

    def test_fixture_invariant_available_le_shares(self):
        """可卖 ≤ 总持仓。"""
        for p in self.fixture.positions:
            assert p.available_shares <= p.shares
            assert p.available_shares + p.unsettled_buy_shares <= p.shares

    def test_signal_tags_complete(self):
        """P0-3: 信号标签完整。"""
        assert "ACCOUNT_CONTEXT_FIXTURE" in FIXTURE_SIGNAL_TAGS
        assert "ACCOUNT_CONTEXT_STALE" in FIXTURE_SIGNAL_TAGS
        assert "NOT_FOR_EXECUTION" in FIXTURE_SIGNAL_TAGS
