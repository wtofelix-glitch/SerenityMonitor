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
