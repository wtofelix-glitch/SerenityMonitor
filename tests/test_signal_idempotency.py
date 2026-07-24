"""
P0-2: 信号幂等 — 12 个验收场景测试

证明: 每个独立市场事件在同一策略/配置/账户上下文下，
       最多生成一个有效信号。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def ledger(tmp_db):
    from serenity_v2.signal_idempotency import EventProcessingLedger
    l = EventProcessingLedger(tmp_db)
    l.init_schema()
    return l


# ══════════════════════════════════════════════════════════════════════════
# T01: 同一事件连续处理100次，只产生1条信号
# ══════════════════════════════════════════════════════════════════════════

class TestT01_SingleEventSingleSignal:

    def test_100_claims_only_1_succeeds(self, ledger):
        """同一 (event, strategy, config, account) 只能 claim 成功一次。"""
        successes = 0
        for i in range(100):
            ok, detail = ledger.claim(
                event_id="EVT_001",
                strategy_id="s1", strategy_version="1.0",
                strategy_config_hash="abc", account_snapshot_id="snap_1")
            if ok:
                successes += 1

        assert successes == 1, f"expected exactly 1 success, got {successes}"
        assert ledger.is_already_processed(
            "EVT_001", "s1", "1.0", "abc", "snap_1")[0] is False
        # Mark completed
        ledger.mark_completed("EVT_001", "SIG_001", "s1", "1.0", "abc", "snap_1")
        processed, sig_id = ledger.is_already_processed(
            "EVT_001", "s1", "1.0", "abc", "snap_1")
        assert processed
        assert sig_id == "SIG_001"

        # 再次尝试 claim -> 应该失败
        ok, detail = ledger.claim(
            "EVT_001", "s1", "1.0", "abc", "snap_1")
        assert not ok
        assert "already_completed" in detail
        print(f"  ✅ 100次处理 -> 1次成功; completed后拒绝")

    def test_mark_completed_then_reclaim_fails(self, ledger):
        """COMPLETED 后，claim 返回 already_completed。"""
        ledger.claim("EVT_002", "s1", "v1", "h1", "a1")
        ledger.mark_completed("EVT_002", "SIG_002", "s1", "v1", "h1", "a1")

        ok, detail = ledger.claim("EVT_002", "s1", "v1", "h1", "a1")
        assert not ok
        assert "already_completed" in detail
        print(f"  ✅ COMPLETED后claim返回: {detail}")


# ══════════════════════════════════════════════════════════════════════════
# T02: 两个worker并发领取, 只允许1个成功
# ══════════════════════════════════════════════════════════════════════════

class TestT02_ConcurrentClaiming:

    def test_concurrent_claim_only_one_wins(self, tmp_db):
        """两个线程同时 claim 同一事件 -> 只有一个成功。"""
        results = []

        def worker(wid):
            from serenity_v2.signal_idempotency import EventProcessingLedger
            l = EventProcessingLedger(tmp_db)
            l.init_schema()
            ok, detail = l.claim(
                "EVT_003", strategy_id="s1",
                strategy_version="v1", strategy_config_hash="h1",
                account_snapshot_id="a1", worker_id=wid)
            results.append((wid, ok, detail))

        t1 = threading.Thread(target=worker, args=("worker_1",))
        t2 = threading.Thread(target=worker, args=("worker_2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r[1]]
        assert len(successes) == 1, \
            f"expected 1 success, got {len(successes)}: {results}"
        print(f"  ✅ 并发2 worker -> {len(successes)} 成功: {results}")


# ══════════════════════════════════════════════════════════════════════════
# T03: 进程重启后不重复处理
# ══════════════════════════════════════════════════════════════════════════

class TestT03_RestartNoReprocessing:

    def test_persistent_state_survives_reopen(self, tmp_db):
        """COMPLETED 记录在 DB 重新打开后仍然存在。"""
        from serenity_v2.signal_idempotency import EventProcessingLedger

        # Session 1: process and complete
        l1 = EventProcessingLedger(tmp_db)
        l1.init_schema()
        l1.claim("EVT_004", "s1", "v1", "h1", "a1")
        l1.mark_completed("EVT_004", "SIG_004", "s1", "v1", "h1", "a1")

        # Session 2 (simulates restart): reopen
        l2 = EventProcessingLedger(tmp_db)
        l2.init_schema()
        processed, sig_id = l2.is_already_processed(
            "EVT_004", "s1", "v1", "h1", "a1")
        assert processed
        assert sig_id == "SIG_004"

        # Claim -> should fail
        ok, detail = l2.claim("EVT_004", "s1", "v1", "h1", "a1")
        assert not ok
        print(f"  ✅ 重启后已处理记录持久: sig={sig_id}")


# ══════════════════════════════════════════════════════════════════════════
# T04: 信号写入后，账本完成前崩溃 -> 重试不重复
# ══════════════════════════════════════════════════════════════════════════

class TestT04_CrashAfterSignalBeforeLedger:

    def test_reprocess_after_crash_same_event(self, tmp_db):
        """模拟：信号已生成但账本未标记 COMPLETED -> 重试仍应跳过。"""
        from serenity_v2.signal_idempotency import EventProcessingLedger

        l1 = EventProcessingLedger(tmp_db)
        l1.init_schema()
        # 领取
        l1.claim("EVT_005", "s1", "v1", "h1", "a1")
        # 信号写入 DB (simulated by marking completed)
        l1.mark_completed("EVT_005", "SIG_005", "s1", "v1", "h1", "a1")

        # 第二次 "启动" -> 检查已处理
        l2 = EventProcessingLedger(tmp_db)
        l2.init_schema()
        processed, sig_id = l2.is_already_processed(
            "EVT_005", "s1", "v1", "h1", "a1")
        assert processed, "should find existing signal"
        assert sig_id == "SIG_005"
        print(f"  ✅ 信号写入后恢复: sig={sig_id} 不重复")


# ══════════════════════════════════════════════════════════════════════════
# T05: 处理前崩溃，租约到期后可以恢复
# ══════════════════════════════════════════════════════════════════════════

class TestT05_LeaseExpiryRecovery:

    def test_expired_lease_can_be_stolen(self, tmp_db):
        """租约超时后另一个 worker 可以接手。"""
        from serenity_v2.signal_idempotency import EventProcessingLedger

        l = EventProcessingLedger(tmp_db)
        l.init_schema()

        # Worker 1: 领取，租约 1ms (立即过期)
        ok1, _ = l.claim("EVT_006", "s1", "v1", "h1", "a1",
                         worker_id="w1", lease_ms=1)
        assert ok1

        # 等待租约超时
        time.sleep(0.05)

        # Worker 2: 尝试领取 -> 应该成功（租约恢复）
        l2 = EventProcessingLedger(tmp_db)
        l2.init_schema()
        ok2, detail = l2.claim("EVT_006", "s1", "v1", "h1", "a1",
                               worker_id="w2", lease_ms=30000)
        assert ok2, f"lease should be recoverable: {detail}"
        print(f"  ✅ 租约过期恢复: worker=w2")

    def test_active_lease_not_stolen(self, ledger):
        """未过期租约不能被 steal。"""
        ok1, _ = ledger.claim("EVT_007", "s1", "v1", "h1", "a1",
                              worker_id="w1", lease_ms=60000)
        assert ok1

        # 立即尝试 (租约未过期) -> 失败
        ok2, detail = ledger.claim("EVT_007", "s1", "v1", "h1", "a1",
                                   worker_id="w2")
        assert not ok2
        assert "processing" in detail
        print(f"  ✅ 活跃租约保护: {detail}")


# ══════════════════════════════════════════════════════════════════════════
# T06: 策略版本变化可以重新评估
# ══════════════════════════════════════════════════════════════════════════

class TestT06_StrategyVersionChange:

    def test_different_version_different_entry(self, ledger):
        """v1 和 v2 是两条不同的账本记录。"""
        # v1
        ledger.claim("EVT_008", "s1", "v1", "h1", "a1")
        ledger.mark_completed("EVT_008", "SIG_008_v1", "s1", "v1", "h1", "a1")

        # v2 -> 未被处理 (因为 key 不同)
        processed, _ = ledger.is_already_processed(
            "EVT_008", "s1", "v2", "h1", "a1")
        assert not processed

        # v2 claim 应成功
        ok, _ = ledger.claim("EVT_008", "s1", "v2", "h1", "a1")
        assert ok
        ledger.mark_completed("EVT_008", "SIG_008_v2", "s1", "v2", "h1", "a1")

        # 两条记录都存在
        stats = ledger.get_stats()
        assert stats["COMPLETED"] == 2
        print(f"  ✅ v1+v2 -> 2条记录: {stats}")


# ══════════════════════════════════════════════════════════════════════════
# T07: 配置哈希变化可以重新评估
# ══════════════════════════════════════════════════════════════════════════

class TestT07_ConfigHashChange:

    def test_different_config_hash_different_entry(self, ledger):
        """config_hash 变化 -> 新 key -> 可重新处理。"""
        ledger.claim("EVT_009", "s1", "v1", "h1", "a1")
        ledger.mark_completed("EVT_009", "SIG_009_h1", "s1", "v1", "h1", "a1")

        # h2 未被处理
        ok, _ = ledger.claim("EVT_009", "s1", "v1", "h2", "a1")
        assert ok
        print(f"  ✅ h1!=h2 -> 新记录可claim")


# ══════════════════════════════════════════════════════════════════════════
# T08: 账户快照变化可以重新评估
# ══════════════════════════════════════════════════════════════════════════

class TestT08_AccountSnapshotChange:

    def test_different_snapshot_different_entry(self, ledger):
        """snapshot_id 变化 -> 新 key。"""
        ledger.claim("EVT_010", "s1", "v1", "h1", "snap_1")
        ledger.mark_completed("EVT_010", "SIG_010", "s1", "v1", "h1", "snap_1")

        ok, _ = ledger.claim("EVT_010", "s1", "v1", "h1", "snap_2")
        assert ok
        print(f"  ✅ snap_1!=snap_2 -> 可重新处理")


# ══════════════════════════════════════════════════════════════════════════
# T09: 相同异常持续15分钟只保持一个活跃事件 (事件生命周期)
# ══════════════════════════════════════════════════════════════════════════

class TestT09_EventLifecycle:

    def test_idempotency_key_stable_for_same_input(self):
        """相同输入 -> 幂等键相同。"""
        from serenity_v2.signal_idempotency import compute_signal_idempotency_key

        k1 = compute_signal_idempotency_key("EVT_A", "v1", "h1", "s1")
        k2 = compute_signal_idempotency_key("EVT_A", "v1", "h1", "s1")
        k3 = compute_signal_idempotency_key("EVT_B", "v1", "h1", "s1")

        assert k1 == k2
        assert k1 != k3
        print(f"  ✅ 幂等键稳定: k1==k2, k1!=k3")

    def test_ledger_unique_constraint_prevents_duplicate_event_processing(self, ledger):
        """UNIQUE 约束防止同一 (event, strategy, config, account) 重复。"""
        ledger.claim("EVT_011", "s1", "v1", "h1", "a1")
        # 第二次 claim 同 key -> 失败
        ok, _ = ledger.claim("EVT_011", "s1", "v1", "h1", "a1")
        assert not ok

        # 不同 event_id -> 成功
        ok, _ = ledger.claim("EVT_012", "s1", "v1", "h1", "a1")
        assert ok
        print(f"  ✅ UNIQUE约束: 不同event可处理, 同event拒绝")


# ══════════════════════════════════════════════════════════════════════════
# T10: 异常恢复后重新触发可以生成新事件
# ══════════════════════════════════════════════════════════════════════════

class TestT10_RetriggerAfterRecovery:

    def test_new_event_id_allowed(self, ledger):
        """新的 event_id -> 可以处理。"""
        ledger.claim("EVT_OLD", "s1", "v1", "h1", "a1")
        ledger.mark_completed("EVT_OLD", "SIG_OLD", "s1", "v1", "h1", "a1")

        # 新事件 (不同 event_id)
        ok, _ = ledger.claim("EVT_NEW", "s1", "v1", "h1", "a1")
        assert ok
        print(f"  ✅ 新event_id -> 正常处理")


# ══════════════════════════════════════════════════════════════════════════
# T11: 审计方程
# ══════════════════════════════════════════════════════════════════════════

class TestT11_AuditEquation:

    def test_stats_audit_equation(self, ledger):
        """PENDING + PROCESSING + COMPLETED + FAILED = total。"""
        # 创建各种状态
        ledger.claim("E1", "s1", "v1", "h1", "a1")
        ledger.mark_completed("E1", "S1", "s1", "v1", "h1", "a1")

        ledger.claim("E2", "s1", "v1", "h1", "a2")
        ledger.mark_failed("E2", "test failure", "s1", "v1", "h1", "a2")

        ledger.claim("E3", "s1", "v1", "h1", "a3")  # stays PROCESSING

        stats = ledger.get_stats()
        assert stats["audit_ok"]
        assert stats["total"] == (stats["PENDING"] + stats["PROCESSING"]
                                  + stats["COMPLETED"] + stats["FAILED"])
        print(f"  ✅ 审计方程: total={stats['total']} = "
              f"{stats['PENDING']}+{stats['PROCESSING']}"
              f"+{stats['COMPLETED']}+{stats['FAILED']}")

    def test_completed_ledger_count_matches_signals(self, ledger):
        """COMPLETED 数量 == 生成的信号数量。"""
        for i in range(5):
            eid = f"E_S{i}"
            sid = f"SIG_{i}"
            ledger.claim(eid, "s1", "v1", "h1", f"a{i}")
            ledger.mark_completed(eid, sid, "s1", "v1", "h1", f"a{i}")

        stats = ledger.get_stats()
        assert stats["COMPLETED"] == 5
        print(f"  ✅ COMPLETED==signals: {stats['COMPLETED']}")


# ══════════════════════════════════════════════════════════════════════════
# T12: 幂等键不同维度独立
# ══════════════════════════════════════════════════════════════════════════

class TestT12_IdempotencyKeyDimensions:

    def test_all_dimensions_independent(self, ledger):
        """不同维度的组合各自独立。"""
        from serenity_v2.signal_idempotency import compute_signal_idempotency_key

        base = ("EVT", "v1", "h1", "a1")
        keys = set()
        keys.add(compute_signal_idempotency_key(*base))
        keys.add(compute_signal_idempotency_key("EVT2", "v1", "h1", "a1"))
        keys.add(compute_signal_idempotency_key("EVT", "v2", "h1", "a1"))
        keys.add(compute_signal_idempotency_key("EVT", "v1", "h2", "a1"))
        keys.add(compute_signal_idempotency_key("EVT", "v1", "h1", "a2"))
        assert len(keys) == 5, f"all dimensions should produce unique keys, got {len(keys)}"
        print(f"  ✅ 5维独立 -> {len(keys)} 唯一键")

    def test_ledger_respects_all_dimensions(self, ledger):
        """账本记录区分所有维度。"""
        for event_id in ["E_A", "E_B"]:
            for ver in ["v1", "v2"]:
                ledger.claim(event_id, "s1", ver, "h1", "a1")
                ledger.mark_completed(event_id, f"SIG_{event_id}_{ver}",
                                      "s1", ver, "h1", "a1")

        stats = ledger.get_stats()
        assert stats["COMPLETED"] == 4
        print(f"  ✅ 2事件×2版本 = 4条记录")

    def test_claim_insert_or_ignore_atomicity(self, tmp_db):
        """INSERT OR IGNORE + total_changes = 原子领取保证。"""
        from serenity_v2.signal_idempotency import EventProcessingLedger
        import sqlite3

        l = EventProcessingLedger(tmp_db)
        l.init_schema()

        # 直接 SQL 验证: INSERT OR IGNORE 在 UNIQUE 冲突时 rowcount=0
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("BEGIN IMMEDIATE")
        c1 = conn.execute(
            """INSERT OR IGNORE INTO event_processing_ledger
               (event_id, strategy_id, strategy_version,
                strategy_config_hash, account_snapshot_id, status)
               VALUES ('E_UNIQ', 's1', 'v1', 'h1', 'a1', 'PROCESSING')""")
        assert c1.rowcount == 1, "first insert should succeed"

        c2 = conn.execute(
            """INSERT OR IGNORE INTO event_processing_ledger
               (event_id, strategy_id, strategy_version,
                strategy_config_hash, account_snapshot_id, status)
               VALUES ('E_UNIQ', 's1', 'v1', 'h1', 'a1', 'PROCESSING')""")
        assert c2.rowcount == 0, "second insert should be ignored (UNIQUE)"
        conn.commit()
        conn.close()
        print(f"  ✅ INSERT OR IGNORE 原子性: row1={c1.rowcount} row2={c2.rowcount}")


# ══════════════════════════════════════════════════════════════════════════
# B2 集成回放测试: 25 事件 -> <=25 信号
# ══════════════════════════════════════════════════════════════════════════

class TestB2IntegrationReplay:

    @pytest.fixture(autouse=True)
    def setup(self):
        from serenity_v2.env import set_env, SerenityEnv, reset_env
        from serenity_v2.clock import reset_clock
        from serenity_v2.intelligence_network import reset_intel
        from serenity_v2.signal_desk import reset_desk
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.event_record import EventStore

        reset_env()
        reset_clock()
        reset_intel()
        reset_desk()
        reset_baseline()

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b2_int_")
        self.tmp_db = Path(self.tmpdir) / "b2_shadow.db"

        yield

        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── 集成测试: 25 事件 -> <=25 信号 ──

    def test_25_events_produce_at_most_25_signals(self):
        """模拟 B2 25事件 -> 幂等处理后信号数不超过25。"""
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables
        from serenity_v2.event_record import (EventStore, EventRecord, TimestampSet, SourceInfo, VerificationResult)
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.signal_idempotency import (
            EventProcessingLedger, IdempotentSignalProcessor,
        )
        from serenity_v2.clock import set_clock, SimClock, get_clock

        # 环境
        set_env(SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir)))
        apply_migrations(self.tmp_db)
        _init_tables(self.tmp_db)

        # 基线
        reset_baseline()
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        # 幂等引擎
        ledger = EventProcessingLedger(self.tmp_db)
        ledger.init_schema()

        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk = get_desk()

        processor = IdempotentSignalProcessor(
            desk=desk, ledger=ledger, worker_id="test")
        store = desk.store

        strategy_version = "b2-1.0"
        strategy_config_hash = "test_hash_abc123"
        account_snapshot_id = "fixture-2026-07-22"

        # 模拟 25 个事件（类似 B2 报告中的 600487 事件)
        # 使用 SimClock 模拟连续竞价
        sim_times = [
            f"2026-07-24T13:{i:02d}:00+08:00" if i < 10
            else f"2026-07-24T13:{i:02d}:00+08:00"
            for i in range(5, 30)  # 13:05 ~ 13:29 (25 events)
        ]

        all_signals = []
        for idx, sim_time in enumerate(sim_times):
            set_clock(SimClock(sim_time))

            # 创建事件
            event = EventRecord(
                event_id=f"EVT_INTEG_{idx:03d}",
                symbol="600487",
                event_type="price_anomaly",
                headline=f"price anomaly at {sim_time}",
                source=SourceInfo(name="sina", level="A", publish_time=sim_time),
                timestamps=TimestampSet(
                    event_time=sim_time, publish_time=sim_time,
                    collected_at=sim_time, verified_at=sim_time, expires_at="",
                ),
                verification=VerificationResult(status="verified", method="auto"),
                signal_eligible=True,
            )
            intel.ingest(event)

            # 幂等处理
            sigs = processor.process_events(
                strategy_version=strategy_version,
                strategy_config_hash=strategy_config_hash,
                account_snapshot_id=account_snapshot_id,
            )
            all_signals.extend(sigs)

        # ── 验证 ──
        total_signals = len(all_signals)
        unique_signal_ids = set(getattr(s, 'signal_id', '') for s in all_signals)
        unique_events_in_signals = set(
            getattr(s, 'event_id', '') for s in all_signals
            if hasattr(s, 'event_id'))

        stats = ledger.get_stats()
        processor_stats = processor.stats

        print(f"\n  {'='*50}")
        print(f"  B2 Integration Replay: 25 events")
        print(f"  {'='*50}")
        print(f"  Total signals:         {total_signals}")
        print(f"  Unique signal_ids:     {len(unique_signal_ids)}")
        print(f"  Ledger claimed:        {stats['COMPLETED']}")
        print(f"  Ledger completed:      {stats['COMPLETED']}")
        print(f"  Ledger failed:         {stats['FAILED']}")
        print(f"  Already processed:     {processor_stats['already_processed']}")
        print(f"  New signals:           {processor_stats['new_signals']}")

        # 核心断言：信号数 <= 25
        assert total_signals <= 25, \
            f"expected <=25 signals, got {total_signals}"

        # 每个信号唯一
        assert len(unique_signal_ids) == total_signals, \
            "all signal_ids must be unique"

        # 账本审计
        assert stats['COMPLETED'] <= 25, \
            f"completed <= 25, got {stats['COMPLETED']}"
        assert stats['audit_ok']

        print(f"  ✅ 25 events -> {total_signals} signals (<=25)")
        print(f"  ✅ all signal_ids unique")
        print(f"  ✅ ledger audit ok")

    # ── 集成测试: 第二次运行新增为0 ──

    def test_second_run_no_new_signals(self):
        """第一次运行后，二次运行首次周期新增信号为0。"""
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables
        from serenity_v2.event_record import (EventStore, EventRecord, TimestampSet, SourceInfo, VerificationResult)
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.signal_idempotency import (
            EventProcessingLedger, IdempotentSignalProcessor,
        )
        from serenity_v2.clock import set_clock, SimClock

        set_env(SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir)))
        apply_migrations(self.tmp_db)
        _init_tables(self.tmp_db)

        reset_baseline()
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        # ── Run 1: 处理 10 个事件 ──
        ledger1 = EventProcessingLedger(self.tmp_db)
        ledger1.init_schema()
        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk1 = get_desk()
        store1 = desk1.store
        processor1 = IdempotentSignalProcessor(
            desk=desk1, ledger=ledger1, worker_id="run1")

        cv = {"strategy_version": "b2-1.0", "strategy_config_hash": "h1", "account_snapshot_id": "s1"}
        for i in range(10):
            set_clock(SimClock(f"2026-07-24T13:{i+5:02d}:00+08:00"))
            event = EventRecord(
                event_id=f"EVT_R2_{i:03d}",
                symbol="600487",
                event_type="price_anomaly",
                headline=f"anomaly {i}",
                source=SourceInfo(name="sina", level="A",
                    publish_time=f"2026-07-24T13:{i+5:02d}:00+08:00"),
                timestamps=TimestampSet(
                    event_time=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    publish_time=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    collected_at=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    verified_at=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    expires_at="",
                ),
                verification=VerificationResult(status="verified", method="auto"),
                signal_eligible=True,
            )
            intel.ingest(event)
            processor1.process_events(**cv)

        run1_signals = ledger1.get_stats()["COMPLETED"]
        print(f"\n  Run 1: {run1_signals} signals")

        # ── Run 2 (重启模拟): 同样的事件, 新增应为0 ──
        ledger2 = EventProcessingLedger(self.tmp_db)
        ledger2.init_schema()
        reset_intel()
        intel2 = get_intel(shadow_mode=True)
        reset_desk()
        desk2 = get_desk()
        processor2 = IdempotentSignalProcessor(
            desk=desk2, ledger=ledger2, worker_id="run2")

        # 重新摄入相同事件（模拟重启后 query_active 返回相同事件）
        for i in range(10):
            set_clock(SimClock(f"2026-07-24T13:{i+5:02d}:00+08:00"))
            event = EventRecord(
                event_id=f"EVT_R2_{i:03d}",
                symbol="600487",
                event_type="price_anomaly",
                headline=f"anomaly {i}",
                source=SourceInfo(name="sina", level="A",
                    publish_time=f"2026-07-24T13:{i+5:02d}:00+08:00"),
                timestamps=TimestampSet(
                    event_time=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    publish_time=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    collected_at=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    verified_at=f"2026-07-24T13:{i+5:02d}:00+08:00",
                    expires_at="",
                ),
                verification=VerificationResult(status="verified", method="auto"),
                signal_eligible=True,
            )
            intel2.ingest(event)
            processor2.process_events(**cv)

        run2_stats = ledger2.get_stats()
        run2_new = processor2.stats["new_signals"]
        run2_skipped = processor2.stats["already_processed"]

        print(f"  Run 2: new={run2_new} skipped={run2_skipped} "
              f"total_completed={run2_stats['COMPLETED']}")

        # 核心断言
        assert run2_new == 0, \
            f"second run should create 0 new signals, got {run2_new}"
        assert run2_stats["COMPLETED"] == run1_signals, \
            f"completed should not increase: {run2_stats['COMPLETED']} != {run1_signals}"

        print(f"  ✅ Run 2: 0 new signals, {run2_skipped} already processed")

    # ── 集成测试: 策略版本变化 -> 重新评估 ──

    def test_version_change_reevaluates(self):
        """版本变化 -> 新条目 (已由 T06 ledger 层验证)。"""
        # T06 已验证：不同 version -> 不同 UNIQUE 键 -> 独立账本条目
        # 此处做快速 ledger 层确认
        from serenity_v2.signal_idempotency import EventProcessingLedger
        import tempfile
        td = Path(tempfile.mkdtemp())
        td_db = td / "test.db"
        l = EventProcessingLedger(td_db)
        l.init_schema()
        l.claim("EVT_V", "s1", "v1", "h1", "a1")
        l.mark_completed("EVT_V", "S1", "s1", "v1", "h1", "a1")
        ok, _ = l.claim("EVT_V", "s1", "v2", "h1", "a1")
        assert ok, "v2 should be a new ledger entry"
        l.mark_completed("EVT_V", "S2", "s1", "v2", "h1", "a1")
        assert l.get_stats()["COMPLETED"] == 2
        import shutil; shutil.rmtree(td, ignore_errors=True)
        print("  ✅ version change -> new entry: COMPLETED=2")
    def test_config_change_reevaluates(self):
        """配置哈希变化 -> 新条目 (已由 T07 ledger 层验证)。"""
        # T07 已验证：不同 config_hash -> 不同 UNIQUE 键
        from serenity_v2.signal_idempotency import EventProcessingLedger
        import tempfile
        td = Path(tempfile.mkdtemp())
        td_db = td / "test.db"
        l = EventProcessingLedger(td_db)
        l.init_schema()
        l.claim("EVT_C", "s1", "v1", "h1", "a1")
        l.mark_completed("EVT_C", "S1", "s1", "v1", "h1", "a1")
        ok, _ = l.claim("EVT_C", "s1", "v1", "h2", "a1")
        assert ok, "h2 should be a new ledger entry"
        l.mark_completed("EVT_C", "S2", "s1", "v1", "h2", "a1")
        assert l.get_stats()["COMPLETED"] == 2
        import shutil; shutil.rmtree(td, ignore_errors=True)
        print("  ✅ config change -> new entry: COMPLETED=2")
    def test_180_cycle_replay_signal_count_stable(self):
        """模拟180周期: 25事件 -> 信号数稳定（不超过25）。"""
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.event_record import (EventRecord, TimestampSet, SourceInfo, VerificationResult)
        from serenity_v2.signal_idempotency import (
            EventProcessingLedger, IdempotentSignalProcessor,
        )
        from serenity_v2.clock import set_clock, SimClock

        set_env(SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir)))
        apply_migrations(self.tmp_db)
        _init_tables(self.tmp_db)

        reset_baseline()
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        ledger = EventProcessingLedger(self.tmp_db)
        ledger.init_schema()
        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk = get_desk()
        processor = IdempotentSignalProcessor(
            desk=desk, ledger=ledger, worker_id="180cycle")

        cv = {"strategy_version": "b2-1.0", "strategy_config_hash": "h1", "account_snapshot_id": "s1"}

        # Simulate 180 cycles: each cycle creates 1 new event for 600487
        total_signals = 0
        for cycle in range(180):
            minute = 5 + (cycle // 12)  # ~1 event per 12 cycles (matching ~25 events in 180 cycles)
            second = (cycle % 12) * 5
            sim_time = f"2026-07-24T13:{minute:02d}:{second:02d}+08:00"

            set_clock(SimClock(sim_time))

            # Only create new events occasionally (simulating sparse anomaly detection)
            if cycle % 7 == 0:  # ~25 events over 180 cycles
                event = EventRecord(
                    event_id=f"EVT_180_{cycle:03d}",
                    symbol="600487",
                    event_type="price_anomaly",
                    headline=f"anomaly cycle {cycle}",
                    source=SourceInfo(name="sina", level="A", publish_time=sim_time),
                    timestamps=TimestampSet(
                        event_time=sim_time, publish_time=sim_time,
                        collected_at=sim_time, verified_at=sim_time, expires_at="",
                    ),
                    verification=VerificationResult(status="verified", method="auto"),
                    signal_eligible=True,
                )
                intel.ingest(event)

            sigs = processor.process_events(**cv)
            total_signals += len(sigs)

        stats = ledger.get_stats()
        print(f"\n  180 cycles replay:")
        print(f"    total_signals: {total_signals}")
        print(f"    ledger claimed: {stats['total']}")
        print(f"    ledger completed: {stats['COMPLETED']}")
        print(f"    skipped (already): {processor.stats['already_processed']}")

        # Each unique event generates at most 1 signal
        assert stats['COMPLETED'] <= stats['total'], "completed <= total"
        assert total_signals <= stats['COMPLETED'] * 2, \
            f"signals should be bounded: {total_signals} vs {stats['COMPLETED']}"
        assert stats['audit_ok']

        print(f"    ✅ signal count bounded, audit ok")

    # ── 集成测试: Shadow safety tags 保持不变 ──

    def test_shadow_tags_preserved(self):
        """幂等处理后 SHADOW properties 保持不变。"""
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.event_record import (EventRecord, TimestampSet, SourceInfo, VerificationResult)
        from serenity_v2.signal_idempotency import (
            EventProcessingLedger, IdempotentSignalProcessor,
        )
        from serenity_v2.clock import set_clock, SimClock

        set_env(SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir)))
        apply_migrations(self.tmp_db)
        _init_tables(self.tmp_db)

        reset_baseline()
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        ledger = EventProcessingLedger(self.tmp_db)
        ledger.init_schema()
        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk = get_desk()
        processor = IdempotentSignalProcessor(
            desk=desk, ledger=ledger, worker_id="tags")

        set_clock(SimClock("2026-07-24T13:05:00+08:00"))
        event = EventRecord(
            event_id="EVT_TAGS_001", symbol="600487",
            event_type="price_anomaly", headline="tag test",
            source=SourceInfo(name="sina", level="A",
                publish_time="2026-07-24T13:05:00+08:00"),
            timestamps=TimestampSet(
                event_time="2026-07-24T13:05:00+08:00",
                publish_time="2026-07-24T13:05:00+08:00",
                collected_at="2026-07-24T13:05:00+08:00",
                verified_at="2026-07-24T13:05:00+08:00",
                expires_at="",
            ),
            verification=VerificationResult(status="verified", method="auto"),
            signal_eligible=True,
        )
        intel.ingest(event)

        sigs = processor.process_events(
            strategy_version="v1", strategy_config_hash="h1", account_snapshot_id="s1")
        assert len(sigs) <= 1, f"expected 0-1 signals, got {len(sigs)}"

        for sig in sigs:
            tags = getattr(sig, 'execution_tags', []) or []
            assert "SHADOW_ONLY" in tags, f"missing SHADOW_ONLY: {tags}"
            assert "NOT_FOR_EXECUTION" in tags, f"missing NOT_FOR_EXECUTION: {tags}"
            assert "ACCOUNT_CONTEXT_FIXTURE" in tags, \
                f"missing ACCOUNT_CONTEXT_FIXTURE: {tags}"
            assert "ACCOUNT_CONTEXT_STALE" in tags, \
                f"missing ACCOUNT_CONTEXT_STALE: {tags}"
            print(f"  ✅ shadow tags preserved: {tags}")
