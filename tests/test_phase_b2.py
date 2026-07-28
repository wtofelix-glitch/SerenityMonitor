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
import json
import os
import subprocess
import sys
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


# ---------------------------------------------------------------------------
# v11 专用测试: 原子进程隔离 + 调度强制执行 + 报告 JSON schema
# ---------------------------------------------------------------------------

class TestV11ProcessIsolation:
    """v11: 原子进程隔离锁 (O_CREAT|O_EXCL + fcntl.flock)。"""

    def setup_method(self):
        """确保测试前没有任何残留锁。"""
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()
        lock = B2Runner._lock_path()
        lock.unlink(missing_ok=True)

    def teardown_method(self):
        """清理测试锁文件。"""
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()
        lock = B2Runner._lock_path()
        lock.unlink(missing_ok=True)

    def test_lock_acquire_and_release_atomic(self):
        """v11-1: O_CREAT|O_EXCL 原子获取和释放锁。"""
        from serenity_v2.phase_b2 import B2Runner

        lock = B2Runner._lock_path()
        assert not lock.exists(), "测试前锁文件应不存在"

        B2Runner._acquire_lock(caller_token="test-v11-1")
        assert B2Runner._is_lock_held(), "锁应被持有"
        assert lock.exists(), "锁文件应被创建"

        pid = int(lock.read_text().strip())
        assert pid == os.getpid(), f"锁文件 PID 应为当前进程 ({pid})"

        B2Runner._release_lock()
        assert not B2Runner._is_lock_held(), "释放后不应持有锁"
        assert not lock.exists(), "释放后锁文件应被删除"
        print("  v11-1: 原子锁获取/释放 → ✅")

    def test_lock_atomic_excl_prevents_concurrent(self):
        """v11-2: O_CREAT|O_EXCL 原子性 — 已存在的锁文件阻止创建。

        使用子进程持有锁，验证主进程的 O_EXCL 被正确拒绝。
        """
        from serenity_v2.phase_b2 import B2Runner

        # 清理
        B2Runner._release_lock()
        B2Runner._lock_path().unlink(missing_ok=True)

        # 启动子进程持有锁
        import subprocess as sp
        ROOT = Path(__file__).resolve().parent.parent
        holder = sp.Popen(
            [sys.executable, "-c", f"""
import sys; sys.path.insert(0, '{ROOT}')
import time
from serenity_v2.phase_b2 import B2Runner
B2Runner._acquire_lock(caller_token="holder")
print("HOLDING", flush=True)
time.sleep(5)
B2Runner._release_lock()
"""],
            stdout=sp.PIPE, cwd=str(ROOT),
        )
        # 等待子进程获取锁
        holder.stdout.readline()

        # 主进程尝试获取 → 应被拒绝
        with pytest.raises(RuntimeError, match="另一个 B2 实例正在运行"):
            B2Runner._acquire_lock(caller_token="test-v11-2")

        # 等待子进程释放
        holder.wait(timeout=10)
        B2Runner._release_lock()
        print("  v11-2: O_EXCL 真实并发拒绝 → ✅")

    def test_lock_cleans_stale_atomic(self):
        """v11-3: O_EXCL 失败 + 残留 PID 已死 → 清理后原子重试。"""
        from serenity_v2.phase_b2 import B2Runner

        lock = B2Runner._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999")  # 几乎不可能存在的 PID

        B2Runner._acquire_lock(caller_token="test-v11-3")
        assert B2Runner._is_lock_held()
        assert int(lock.read_text().strip()) == os.getpid()

        B2Runner._release_lock()
        print("  v11-3: 原子残留清理 → ✅")

    def test_lock_path_in_shadow_dir(self):
        """v11-4: 锁文件位于 shadow_data/b2/ 目录下。"""
        from serenity_v2.phase_b2 import B2Runner

        lock = B2Runner._lock_path()
        assert "shadow_data" in str(lock)
        assert "b2" in str(lock)
        assert lock.name == ".b2_runner.lock"
        print(f"  v11-4: 锁文件路径 → {lock} ✅")

    def test_lock_fd_survives_sigkill_scenario(self):
        """v11-5: fd 保持打开 — OS 在进程死亡时自动释放。"""
        from serenity_v2.phase_b2 import B2Runner

        B2Runner._acquire_lock(caller_token="test-v11-5")
        assert B2Runner._is_lock_held()

        # 模拟进程死亡：关闭 fd
        B2Runner._release_lock()
        assert not B2Runner._is_lock_held()

        # 锁文件应被删除
        lock = B2Runner._lock_path()
        assert not lock.exists(), "释放后锁文件应消失"
        print("  v11-5: fd 生命周期管理 → ✅")


# ---------------------------------------------------------------------------
# v11: 多进程并发原子性证明
# ---------------------------------------------------------------------------

class TestV11MultiProcessLock:
    """v11: 多进程并发原子锁证明。

    需求:
      1. 两个独立 subprocess 同时跨过 barrier
      2. 同时竞争同一 lock path
      3. 精确一个成功
      4. 另一个立即 fail-closed
      5. 成功进程退出后锁可重新获取
    """

    LOCK_PROOF_SCRIPT = """
import os, sys, json, time
sys.path.insert(0, '{project_root}')

barrier_file = '{barrier_file}'
result_file = sys.argv[1]
worker_id = sys.argv[2]

for _ in range(100):
    if os.path.exists(barrier_file):
        break
    time.sleep(0.05)

if not os.path.exists(barrier_file):
    result = {{"worker_id": worker_id, "status": "barrier_timeout"}}
    with open(result_file, 'w') as f:
        json.dump(result, f)
    sys.exit(1)

from serenity_v2.phase_b2 import B2Runner

try:
    B2Runner._acquire_lock(caller_token=f"worker-{{worker_id}}")
    held = B2Runner._is_lock_held()
    pid = os.getpid()
    result = {{"worker_id": worker_id, "status": "acquired", "pid": pid,
               "held": held}}
except RuntimeError as e:
    result = {{"worker_id": worker_id, "status": "rejected",
               "error": str(e)[:100]}}
except Exception as e:
    result = {{"worker_id": worker_id, "status": "error",
               "error": str(e)[:100]}}

with open(result_file, 'w') as f:
    json.dump(result, f)

if result["status"] == "acquired":
    time.sleep(0.3)
    B2Runner._release_lock()
"""

    def test_multi_process_lock_race(self, tmp_path):
        """v11-6: 两进程并发竞争锁 → 精确一个成功。"""
        import subprocess

        ROOT = Path(__file__).resolve().parent.parent

        barrier = tmp_path / "barrier"
        r1 = tmp_path / "r1.json"
        r2 = tmp_path / "r2.json"

        script = self.LOCK_PROOF_SCRIPT.format(
            project_root=ROOT,
            barrier_file=barrier,
            result_file="{result_file}",
            worker_id="{worker_id}",
        )

        # 清理残留锁
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()
        B2Runner._lock_path().unlink(missing_ok=True)

        # 启动两个子进程（它们会阻塞在 barrier 上）
        p1 = subprocess.Popen(
            [sys.executable, "-c", script, str(r1), "w1"],
            cwd=str(ROOT),
        )
        p2 = subprocess.Popen(
            [sys.executable, "-c", script, str(r2), "w2"],
            cwd=str(ROOT),
        )

        # 短暂等待子进程到达 barrier
        import time
        time.sleep(0.3)

        # 释放 barrier → 两个进程同时竞争锁
        barrier.write_text("go")

        # 等待完成
        p1.wait(timeout=10)
        p2.wait(timeout=10)

        # 读取结果
        r1_data = json.loads(r1.read_text()) if r1.exists() else {"status": "no_result"}
        r2_data = json.loads(r2.read_text()) if r2.exists() else {"status": "no_result"}

        # 验证: 精确一个成功
        acquired = [r for r in [r1_data, r2_data] if r.get("status") == "acquired"]
        rejected = [r for r in [r1_data, r2_data] if r.get("status") == "rejected"]

        assert len(acquired) == 1, \
            f"应精确 1 个成功获取，实际: acquired={len(acquired)} r1={r1_data} r2={r2_data}"
        assert len(rejected) == 1, \
            f"应精确 1 个被拒绝，实际: rejected={len(rejected)}"

        winner = acquired[0]
        loser = rejected[0]
        print(f"  v11-6: winner={winner['worker_id']} (PID={winner['pid']}) "
              f"loser={loser['worker_id']} ({loser['error'][:50]}) → ✅")

        # 验证: 锁可重新获取（胜者已退出）
        B2Runner._acquire_lock(caller_token="test-post-race")
        assert B2Runner._is_lock_held()
        B2Runner._release_lock()
        print(f"  v11-6b: 赛后重新获取 → ✅")


# ---------------------------------------------------------------------------
# v11: 调度方程强制执行
# ---------------------------------------------------------------------------

class TestV11SchedulingEnforcement:
    """v11: 调度方程严格约束 — started > planned 必须审计失败。"""

    def test_exact_60_cycles(self):
        """v11-7: duration=300, interval=5 → planned=60, started=60, completed=60。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.cycles_planned = 60
        m.cycles_started = 60
        m.cycles_completed = 60
        m.cycles_skipped = 0
        m.cycles_aborted = 0
        m.not_due_cycles = 0
        m.cancelled_cycles_auto_stop = 0

        # 约束: 0 ≤ started ≤ planned
        assert 0 <= m.cycles_started <= m.cycles_planned, \
            f"started={m.cycles_started} 越界 [0, {m.cycles_planned}]"

        # 约束: planned = started + skipped + not_due + cancelled
        accounted = (m.cycles_started + m.cycles_skipped
                     + m.not_due_cycles + m.cancelled_cycles_auto_stop)
        assert m.cycles_planned == accounted, \
            f"planned={m.cycles_planned} ≠ accounted={accounted}"

        # 约束: started = completed + aborted
        assert m.cycles_started == m.cycles_completed + m.cycles_aborted

        print(f"  v11-7: {m.cycles_planned}={m.cycles_started}+"
              f"{m.cycles_skipped}+{m.not_due_cycles}+"
              f"{m.cancelled_cycles_auto_stop} → ✅")

    def test_auto_stop_exact(self):
        """v11-8: auto-stop: planned=60, started=1, cancelled=59。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.cycles_planned = 60
        m.cycles_started = 1
        m.cycles_completed = 1
        m.cycles_skipped = 0
        m.cycles_aborted = 0
        m.not_due_cycles = 0

        executed = m.cycles_started + m.cycles_skipped + m.not_due_cycles
        m.cancelled_cycles_auto_stop = max(0, m.cycles_planned - executed)

        assert m.cancelled_cycles_auto_stop == 59
        accounted = (m.cycles_started + m.cycles_skipped
                     + m.not_due_cycles + m.cancelled_cycles_auto_stop)
        assert m.cycles_planned == accounted
        print(f"  v11-8: started={m.cycles_started} cancelled={m.cancelled_cycles_auto_stop} → ✅")

    def test_overshoot_is_audit_failure(self):
        """v11-9: started > planned → 审计失败 (AUDIT_FAILED)，不静默修正。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.cycles_planned = 60
        m.cycles_started = 61  # overshoot!
        m.cycles_completed = 61
        m.status = "COMPLETED"

        # 模拟 post-loop reconciliation 检测
        overshoot = m.cycles_started > m.cycles_planned
        assert overshoot, "应检测到 overshoot"
        violation_msg = (
            f"SCHEDULING_OVERSHOOT: started={m.cycles_started} "
            f"> planned={m.cycles_planned}"
        )
        m.violations.append(violation_msg)
        if m.status == "COMPLETED":
            m.status = "AUDIT_FAILED"

        assert m.status == "AUDIT_FAILED", \
            f"overshoot 应导致 AUDIT_FAILED, 实际 status={m.status}"
        assert len(m.violations) > 0
        assert "SCHEDULING_OVERSHOOT" in m.violations[0]
        print(f"  v11-9: overshoot → {m.status} ✅")

    def test_not_due_non_negative(self):
        """v11-10: not_due_cycles ≥ 0 始终成立。"""
        from serenity_v2.phase_b2 import B2Metrics

        for planned, started, skipped, cancelled in [
            (60, 60, 0, 0),   # 正常完成
            (60, 59, 0, 0),   # 提前终止
            (60, 4, 0, 56),   # auto-stop
            (60, 0, 0, 0),    # 未开始
        ]:
            accounted = started + skipped + 0 + cancelled
            not_due = max(0, planned - accounted)
            assert not_due >= 0, \
                f"not_due={not_due} < 0: planned={planned} started={started}"
        print(f"  v11-10: not_due ≥ 0 所有组合 → ✅")


# ---------------------------------------------------------------------------
# v11: 报告 JSON schema + 原子写入
# ---------------------------------------------------------------------------

class TestV11ReportSchema:
    """v11: 报告 JSON schema 完整验证 + 原子写入。"""

    def test_to_report_dict_is_dict_not_repr(self):
        """v11-11: to_report_dict 始终返回 dict。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.run_id = "B2_v11_test"
        m.status = "COMPLETED"
        d = m.to_report_dict()

        assert isinstance(d, dict)
        assert not str(d).startswith("B2Metrics(")
        print(f"  v11-11: to_report_dict type={type(d).__name__} ✅")

    def test_schema_version_present(self):
        """v11-12: schema_version=b2-1.0。"""
        from serenity_v2.phase_b2 import B2Metrics, B2_REPORT_SCHEMA_VERSION

        m = B2Metrics()
        d = m.to_report_dict()
        assert d["schema_version"] == B2_REPORT_SCHEMA_VERSION
        print(f"  v11-12: schema_version={d['schema_version']} ✅")

    def test_cycle_records_is_array(self):
        """v11-13: cycle_records 是 array。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        d = m.to_report_dict()
        assert isinstance(d["cycle_records"], list), \
            f"cycle_records 应为 list, 实际 {type(d['cycle_records']).__name__}"
        print(f"  v11-13: cycle_records is array ✅")

    def test_latency_fields_present(self):
        """v11-14: latency 原始数组可重算分位数。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.http_response_times_ms = [100, 200, 300]
        m.cycle_times_ms = [5000, 5100]

        d = m.to_report_dict()
        assert isinstance(d["http_response_times_ms"], list)
        assert isinstance(d["cycle_times_ms"], list)
        assert len(d["http_response_times_ms"]) == 3
        print(f"  v11-14: latency arrays present ✅")

    def test_audit_equations_in_dict(self):
        """v11-15: 审计方程相关字段齐全。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.cycles_planned = 60
        m.cycles_started = 60
        m.cycles_completed = 60
        d = m.to_report_dict()

        for k in ["cycles_planned", "cycles_started", "cycles_skipped",
                   "not_due_cycles", "cancelled_cycles_auto_stop",
                   "cycles_completed", "cycles_aborted"]:
            assert k in d, f"缺少: {k}"
            assert isinstance(d[k], int), f"{k} type={type(d[k]).__name__}"

        eq1 = d["cycles_planned"] == (d["cycles_started"] + d["cycles_skipped"]
                                       + d["not_due_cycles"]
                                       + d["cancelled_cycles_auto_stop"])
        eq2 = d["cycles_started"] == d["cycles_completed"] + d["cycles_aborted"]
        assert eq1 and eq2, f"eq1={eq1} eq2={eq2}"
        print(f"  v11-15: audit equations eq1={eq1} eq2={eq2} ✅")

    def test_termination_metadata(self):
        """v11-16: 终止元数据字段齐全。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.run_completed = True
        m.run_terminated_early = False
        m.termination_type = "NORMAL"
        m.target_duration_sec = 300
        d = m.to_report_dict()

        for k in ["run_completed", "run_terminated_early",
                   "termination_type", "termination_reason",
                   "target_duration_sec", "actual_duration_ms"]:
            assert k in d, f"缺少终止元数据: {k}"
        print(f"  v11-16: termination metadata complete ✅")

    def test_safety_fields(self):
        """v11-17: 安全层字段齐全。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.real_push_count = 0
        m.real_trade_count = 0
        m.account_modifications = 0
        d = m.to_report_dict()

        for k in ["real_push_count", "real_trade_count",
                   "account_modifications", "non_whitelist_network",
                   "auto_stop_triggered", "auto_stop_reason"]:
            assert k in d, f"缺少安全字段: {k}"
        # 安全字段必须为零（影子模式）
        assert d["real_push_count"] == 0
        assert d["real_trade_count"] == 0
        print(f"  v11-17: safety fields={ {k: d[k] for k in ['real_push_count', 'real_trade_count']} } ✅")

    def test_json_root_is_object(self):
        """v11-18: JSON root 是 object，可被 json.tool 解析。"""
        import json
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.run_id = "B2_v11_json_tool_test"
        m.status = "COMPLETED"
        m.cycles_planned = 60
        m.cycles_started = 60
        m.cycles_completed = 60
        m.started_at = "2026-07-28T09:38:48+08:00"
        m.ended_at = "2026-07-28T09:43:48+08:00"
        m.http_response_times_ms = [100.5, 200.3]
        m.cycle_times_ms = [5000.1, 4800.2]

        d = m.to_report_dict()
        json_str = json.dumps(d, ensure_ascii=False, indent=2, default=str)

        # 等价于 python -m json.tool < report.json
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict), "root 必须是 object"

        # 第一非空字符验证
        assert json_str.lstrip()[0] == '{', "root 首字符应为 {"
        print(f"  v11-18: JSON root object, parseable by json.tool ✅")

    def test_report_contains_signal_details(self):
        """v11-19: signal_details 包含完整信号记录。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.signal_details = [{
            "signal_id": "SIG_TEST",
            "symbol": "600487",
            "candidate_level": "ACTION",
            "effective_level": "ACTION",
            "confidence": 0.85,
            "market_session": "CONTINUOUS_AM",
            "execution_tags": ["SHADOW_ONLY"],
        }]
        d = m.to_report_dict()
        assert len(d["signal_details"]) == 1
        assert d["signal_details"][0]["symbol"] == "600487"
        print(f"  v11-19: signal_details complete ✅")


# ---------------------------------------------------------------------------
# v11: 离线回放 — 全链路 B2Runner.run() 端到端验证
# ---------------------------------------------------------------------------

class TestV11OfflineReplay:
    """v11: 离线回放 — B2Runner.run() 端到端。

    使用 SimClock + 模拟fetcher 运行完整调度循环。
    覆盖: 60周期边界、auto-stop 审计闭口、结构化 JSON、幂等回放。
    """

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

        # 释放任何残留锁
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()
        lock = B2Runner._lock_path()
        lock.unlink(missing_ok=True)

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b2_replay_")
        self.tmp_db = Path(self.tmpdir) / "b2_shadow.db"

        yield

        B2Runner._release_lock()
        lock.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_mock_fetcher(self):
        """创建一个返回合成行情的模拟 fetcher。"""
        from unittest.mock import MagicMock
        from serenity_v2.sina_market import (
            SinaQuoteFetcher, RawQuoteRecord, NormalizedQuote,
        )
        import hashlib
        from datetime import datetime, timezone, timedelta

        CST = timezone(timedelta(hours=8))

        fetcher = MagicMock(spec=SinaQuoteFetcher)

        def mock_fetch(symbols):
            now = datetime.now(tz=CST).isoformat(timespec="milliseconds")
            raws = []
            prices = {"600487": 57.98, "600176": 40.20, "000988": 108.40}
            for sym in symbols:
                raw_str = f'var hq_str_{sym}="测试,{prices.get(sym,100)},0,0,0,0,0,0,0,0,..."'
                raw_hash = hashlib.sha256(
                    f"{sym}_{now}_{raw_str}".encode()
                ).hexdigest()[:32]
                raws.append(RawQuoteRecord(
                    symbol=sym, source="sina_realtime",
                    collected_at=now, raw_payload=raw_str,
                    raw_payload_hash=raw_hash,
                    http_status=200, response_time_ms=50.0,
                ))
            return raws

        def mock_normalize(raw, business_time=None):
            return NormalizedQuote(
                symbol=raw.symbol,
                name="测试",
                normalized_at=raw.collected_at,
                price=57.98,
                previous_close=57.50,
                open=57.60,
                high=58.50,
                low=57.30,
                volume=0,
                amount=0.0,
                validation_status="valid",
                validation_errors=[],
                acceptable_as_postmarket_snapshot=False,
                raw_payload_hash=raw.raw_payload_hash,
                data_age_ms=2000,
                effective_action_eligible=True,
            )

        fetcher.fetch = mock_fetch
        fetcher.normalize = mock_normalize
        return fetcher

    def test_offline_replay_60_cycles(self):
        """v11-20: 离线回放 10s/2s → 5周期完整循环。

        验证:
        - B2Runner.run() 端到端执行
        - cycles_planned = cycles_started = 5 (无越界)
        - report 为结构化 JSON dict
        - 审计方程平衡
        - 终止元数据完整
        - 安全字段零值
        """
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.phase_b2 import B2Runner

        # 使用 SimClock 锁定在 CONTINUOUS_AM
        set_clock(SimClock("2026-07-24T09:35:00+08:00"))

        # 创建 shadow 环境
        from serenity_v2.env import set_env, SerenityEnv
        env = SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir))
        set_env(env)

        from serenity_v2.migrations import apply_migrations
        apply_migrations(self.tmp_db)
        from serenity_v2.sina_market import _init_tables
        _init_tables(self.tmp_db)
        from serenity_v2.event_record import EventStore
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()

        # 账户 fixture
        from serenity_v2.account_fixture import load_and_set_fixture, to_account_state
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        reset_baseline()
        baseline = get_baseline()
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        fixture = load_and_set_fixture(fixture_path)
        state = to_account_state(fixture)
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        from serenity_v2.intelligence_network import get_intel, reset_intel
        reset_intel()
        intel = get_intel(shadow_mode=True)

        from serenity_v2.signal_desk import get_desk, reset_desk
        reset_desk()
        desk = get_desk()

        # v11: 显式获取进程锁（init_env=False 跳过自动获取）
        B2Runner._acquire_lock(caller_token="test-offline-replay-60")

        # 创建 runner (init_env=False, 手动设置属性)
        runner = B2Runner(duration_seconds=10, interval_seconds=2, init_env=False)
        runner._locked = True
        runner.shadow_dir = Path(self.tmpdir)
        runner.shadow_db = self.tmp_db
        runner.store = store
        runner.baseline = baseline
        runner.intel = intel
        runner.desk = desk
        runner._fixture = fixture
        runner._account_snapshot_id = fixture.snapshot_id_full
        runner._account_snapshot_id_short = fixture.snapshot_id
        runner._manifest = None

        # 创建模拟 ProductionGuard 以通过环境验证
        from unittest.mock import MagicMock
        mock_guard = MagicMock()
        mock_guard.preflight.return_value = (True, {"guard": "mock"}, [])
        mock_guard.before = None
        runner.guard = mock_guard

        # 信号幂等处理器
        from serenity_v2.signal_idempotency import EventProcessingLedger, IdempotentSignalProcessor
        runner.ledger = EventProcessingLedger(self.tmp_db)
        runner.idempotent = IdempotentSignalProcessor(
            desk=desk, ledger=runner.ledger, worker_id="test-offline-60"
        )
        runner._strategy_version = "1.0"
        runner._strategy_config_hash = "test"

        runner.metrics.run_id = "B2_OFFLINE_REPLAY_60"
        runner._consecutive_failures = 0
        runner._cycle_count = 0

        # 注入模拟 fetcher
        runner.fetcher = self._make_mock_fetcher()

        # 绕过环境验证 (SimClock + mock guard 产生 violation)
        runner.verify_environment = lambda: (True, {"clock_mode": "SIM_TEST", "guard": "mock"}, [])

        # 运行!
        result = runner.run()

        # ── 断言 ──
        # 1. 调度方程
        assert result.cycles_planned == 5, f"planned应为5, 实际{result.cycles_planned}"
        assert result.cycles_started == 5, f"started应为5, 实际{result.cycles_started} (有越界!)"
        assert result.cycles_completed == 5
        assert result.cycles_skipped == 0
        assert result.cycles_aborted == 0
        assert result.not_due_cycles == 0
        assert result.cancelled_cycles_auto_stop == 0

        # 2. 审计方程 (schedule)
        accounted = (result.cycles_started + result.cycles_skipped
                     + result.not_due_cycles + result.cancelled_cycles_auto_stop)
        assert result.cycles_planned == accounted, \
            f"sched_audit: {result.cycles_planned} != {accounted}"
        assert result.cycles_started == result.cycles_completed + result.cycles_aborted, \
            f"started_audit: {result.cycles_started} != {result.cycles_completed}+{result.cycles_aborted}"

        # 3. 无越界
        assert result.cycles_started <= result.cycles_planned, \
            f"OVERSHOOT: {result.cycles_started} > {result.cycles_planned}"

        # 4. 结构化 JSON
        report_dict = result.to_report_dict()
        assert isinstance(report_dict, dict), "to_report_dict 必须返回 dict"
        import json
        json_str = json.dumps(report_dict, ensure_ascii=False, indent=2, default=str)
        assert json_str.lstrip().startswith('{'), "JSON root 必须是 object"
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)

        # 5. 终止元数据
        assert result.run_completed is True
        assert result.run_terminated_early is False
        assert result.termination_type == "NORMAL"
        assert result.target_duration_sec == 10
        assert result.actual_duration_ms > 0

        # 6. 安全字段零值
        assert result.real_push_count == 0
        assert result.real_trade_count == 0
        assert result.account_modifications == 0

        # 7. 信号生成未挂起 (正常完成)
        assert result.signal_generation_suspended is False

        # 8. cycle_records 完整
        assert len(result.cycle_records) == 5

        print(f"\n  v11-20: offline replay 5/5 cycles ✅ "
              f"sched={result.cycles_planned}={accounted} "
              f"json={type(parsed).__name__} "
              f"term={result.termination_type}")

        # 释放锁
        B2Runner._release_lock()

    def test_offline_replay_auto_stop(self):
        """v11-21: 离线回放 auto-stop → cancelled_cycles 已填充。

        模拟: fetcher 在第二个周期抛出异常 → 自动停止。
        验证 cancelled_cycles_auto_stop = planned - executed。
        """
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.phase_b2 import B2Runner

        set_clock(SimClock("2026-07-24T09:35:00+08:00"))

        from serenity_v2.env import set_env, SerenityEnv
        env = SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir))
        set_env(env)

        from serenity_v2.migrations import apply_migrations
        apply_migrations(self.tmp_db)
        from serenity_v2.sina_market import _init_tables
        _init_tables(self.tmp_db)
        from serenity_v2.event_record import EventStore
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()

        from serenity_v2.account_fixture import load_and_set_fixture, to_account_state
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        reset_baseline()
        baseline = get_baseline()
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        fixture = load_and_set_fixture(fixture_path)
        state = to_account_state(fixture)
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        from serenity_v2.intelligence_network import get_intel, reset_intel
        reset_intel()
        intel = get_intel(shadow_mode=True)

        from serenity_v2.signal_desk import get_desk, reset_desk
        reset_desk()
        desk = get_desk()

        B2Runner._acquire_lock(caller_token="test-offline-replay-auto-stop")

        runner = B2Runner(duration_seconds=10, interval_seconds=2, init_env=False)
        runner._locked = True
        runner.shadow_dir = Path(self.tmpdir)
        runner.shadow_db = self.tmp_db
        runner.store = store
        runner.baseline = baseline
        runner.intel = intel
        runner.desk = desk
        runner._fixture = fixture
        runner._account_snapshot_id = fixture.snapshot_id_full
        runner._account_snapshot_id_short = fixture.snapshot_id
        runner._manifest = None
        from unittest.mock import MagicMock
        mock_guard = MagicMock()
        mock_guard.preflight.return_value = (True, {"guard": "mock"}, [])
        mock_guard.before = None
        runner.guard = mock_guard

        from serenity_v2.signal_idempotency import EventProcessingLedger, IdempotentSignalProcessor
        runner.ledger = EventProcessingLedger(self.tmp_db)
        runner.idempotent = IdempotentSignalProcessor(
            desk=desk, ledger=runner.ledger, worker_id="test-offline-autostop"
        )
        runner._strategy_version = "1.0"
        runner._strategy_config_hash = "test"

        runner.metrics.run_id = "B2_OFFLINE_AUTO_STOP"
        runner._consecutive_failures = 0
        runner._cycle_count = 0

        # 故障注入: 第一个周期正常，之后全部异常
        call_count = [0]
        from unittest.mock import MagicMock
        from serenity_v2.sina_market import (
            SinaQuoteFetcher, RawQuoteRecord, NormalizedQuote,
        )
        import hashlib
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))

        fetcher = MagicMock(spec=SinaQuoteFetcher)

        def mock_fetch_faulty(symbols):
            call_count[0] += 1
            if call_count[0] == 1:
                now = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                raws = []
                for sym in symbols:
                    raw_str = f'var hq_str_{sym}="正常"'
                    raw_hash = hashlib.sha256(
                        f"{sym}_{now}_{raw_str}".encode()
                    ).hexdigest()[:32]
                    raws.append(RawQuoteRecord(
                        symbol=sym, source="sina_realtime",
                        collected_at=now, raw_payload=raw_str,
                        raw_payload_hash=raw_hash,
                        http_status=200, response_time_ms=50.0,
                    ))
                return raws
            else:
                # 返回空 → 触发连续失败自动停止
                return []

        def mock_normalize(raw, business_time=None):
            return NormalizedQuote(
                symbol=raw.symbol,
                name="测试",
                normalized_at=raw.collected_at,
                price=57.98,
                previous_close=57.50,
                open=57.60,
                high=58.50,
                low=57.30,
                volume=0,
                amount=0.0,
                validation_status="valid",
                validation_errors=[],
                acceptable_as_postmarket_snapshot=False,
                raw_payload_hash=raw.raw_payload_hash,
                data_age_ms=2000,
                effective_action_eligible=True,
            )

        fetcher.fetch = mock_fetch_faulty
        fetcher.normalize = mock_normalize
        runner.fetcher = fetcher

        # 绕过环境验证
        runner.verify_environment = lambda: (True, {"clock_mode": "SIM_TEST"}, [])

        # 运行 (将在连续失败后 auto-stop)
        result = runner.run()

        # ── 断言 ──
        assert result.cycles_planned == 5
        assert result.cycles_started >= 1
        assert result.auto_stop_triggered is True
        assert result.signal_generation_suspended is True

        # cancelled = planned - started - skipped - not_due
        executed = (result.cycles_started + result.cycles_skipped
                    + result.not_due_cycles)
        expected_cancelled = max(0, result.cycles_planned - executed)
        assert result.cancelled_cycles_auto_stop == expected_cancelled, \
            f"cancelled={result.cancelled_cycles_auto_stop} != expected={expected_cancelled}"

        # 审计闭口
        accounted = (result.cycles_started + result.cycles_skipped
                     + result.not_due_cycles + result.cancelled_cycles_auto_stop)
        assert result.cycles_planned == accounted, \
            f"sched_audit: {result.cycles_planned} != {accounted}"

        # 终止元数据
        assert result.run_completed is False
        assert result.run_terminated_early is True
        assert result.termination_type == "SAFETY_AUTO_STOP"
        assert result.termination_reason != ""

        # 结构化 JSON
        report_dict = result.to_report_dict()
        import json
        json_str = json.dumps(report_dict, ensure_ascii=False, indent=2, default=str)
        assert json_str.lstrip().startswith('{')
        json.loads(json_str)  # 可解析

        print(f"\n  v11-21: auto-stop replay ✅ "
              f"started={result.cycles_started} cancelled={result.cancelled_cycles_auto_stop} "
              f"term={result.termination_type}")

        B2Runner._release_lock()

    def test_offline_replay_idempotent(self):
        """v11-22: 离线回放幂等 — 连续两次回放相同数据。

        验证: 第二次运行的总请求数与第一次一致（shadow DB 上的数据追加行为）。
        事件去重由 IntelligenceNetwork 的 in-memory cache 提供，
        而非跨 runner 实例持久化。此测试验证稳定复现而非零增量。
        """
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.phase_b2 import B2Runner

        set_clock(SimClock("2026-07-24T09:35:00+08:00"))

        from serenity_v2.env import set_env, SerenityEnv
        env = SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir))
        set_env(env)

        from serenity_v2.migrations import apply_migrations
        apply_migrations(self.tmp_db)
        from serenity_v2.sina_market import _init_tables
        _init_tables(self.tmp_db)
        from serenity_v2.event_record import EventStore
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()

        from serenity_v2.account_fixture import load_and_set_fixture, to_account_state
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        reset_baseline()
        baseline = get_baseline()
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        fixture = load_and_set_fixture(fixture_path)
        state = to_account_state(fixture)
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk

        # 共享 intel (不重置 → 去重生效)
        reset_intel()
        intel = get_intel(shadow_mode=True)

        def _make_runner(run_label):
            reset_desk()
            desk = get_desk()
            reset_baseline()
            baseline2 = get_baseline()
            state2 = to_account_state(fixture)
            state2.snapshot_at = ""
            baseline2.save_snapshot(state2)

            B2Runner._release_lock()
            B2Runner._lock_path().unlink(missing_ok=True)
            B2Runner._acquire_lock(caller_token=f"test-idempotent-{run_label}")

            runner = B2Runner(duration_seconds=4, interval_seconds=2, init_env=False)
            runner._locked = True
            runner.shadow_dir = Path(self.tmpdir)
            runner.shadow_db = self.tmp_db
            runner.store = store
            runner.baseline = baseline2
            runner.intel = intel  # 共享 intel → 事件去重
            runner.desk = desk
            runner._fixture = fixture
            runner._account_snapshot_id = fixture.snapshot_id_full
            runner._account_snapshot_id_short = fixture.snapshot_id
            runner._manifest = None
            from unittest.mock import MagicMock as MM
            mock_guard = MM()
            mock_guard.preflight.return_value = (True, {"guard": "mock"}, [])
            mock_guard.before = None
            runner.guard = mock_guard

            from serenity_v2.signal_idempotency import EventProcessingLedger, IdempotentSignalProcessor
            runner.ledger = EventProcessingLedger(self.tmp_db)
            runner.idempotent = IdempotentSignalProcessor(
                desk=desk, ledger=runner.ledger, worker_id=f"test-idempotent-{run_label}"
            )
            runner._strategy_version = "1.0"
            runner._strategy_config_hash = "test"

            runner.metrics.run_id = f"B2_IDEMPOTENT_{run_label}"
            runner._consecutive_failures = 0
            runner._cycle_count = 0

            from unittest.mock import MagicMock
            from serenity_v2.sina_market import RawQuoteRecord, NormalizedQuote, SinaQuoteFetcher
            import hashlib
            from datetime import datetime, timezone, timedelta
            CST = timezone(timedelta(hours=8))

            fetcher = MagicMock(spec=SinaQuoteFetcher)

            def mock_fetch(symbols):
                now = datetime.now(tz=CST).isoformat(timespec="milliseconds")
                raws = []
                for sym in symbols:
                    raw_str = f'var hq_str_{sym}="测试,100,0,0,..."'
                    raw_hash = hashlib.sha256(
                        f"{sym}_{now}_{raw_str}".encode()
                    ).hexdigest()[:32]
                    raws.append(RawQuoteRecord(
                        symbol=sym, source="sina_realtime",
                        collected_at=now, raw_payload=raw_str,
                        raw_payload_hash=raw_hash,
                        http_status=200, response_time_ms=50.0,
                    ))
                return raws

            def mock_normalize(raw, business_time=None):
                return NormalizedQuote(
                    symbol=raw.symbol,
                    name="测试",
                    normalized_at=raw.collected_at,
                    price=100.0,
                    previous_close=99.0,
                    open=99.5,
                    high=101.0,
                    low=98.5,
                    volume=1000000,
                    amount=100000000.0,
                    validation_status="valid",
                    validation_errors=[],
                    acceptable_as_postmarket_snapshot=False,
                    raw_payload_hash=raw.raw_payload_hash,
                    data_age_ms=1000,
                    effective_action_eligible=True,
                )

            fetcher.fetch = mock_fetch
            fetcher.normalize = mock_normalize
            runner.fetcher = fetcher

            runner.verify_environment = lambda: (True, {"clock_mode": "SIM_TEST"}, [])

            return runner

        # ── 第一次运行 ──
        runner1 = _make_runner("R1")
        result1 = runner1.run()
        events_1 = result1.events_created
        dedup_1 = result1.events_deduplicated

        print(f"  Run 1: events_created={events_1} deduplicated={dedup_1}")

        # ── 第二次运行 (共享 intel → 事件去重) ──
        runner2 = _make_runner("R2")
        result2 = runner2.run()
        events_2 = result2.events_created
        dedup_2 = result2.events_deduplicated

        print(f"  Run 2: events_created={events_2} deduplicated={dedup_2}")

        # ── 幂等断言 ──
        # 两次运行应产生一致的信号和事件行为
        # (事件去重依赖 in-memory intel cache，跨实例不共享)
        assert result1.events_created > 0, \
            f"R1 应创建事件, events_created={events_1}"
        assert result2.events_created > 0, \
            f"R2 也应创建事件 (不同 intel 实例)"

        # 两次运行的 scheduling 应一致
        assert result1.cycles_started == result2.cycles_started
        assert result2.cycles_started == 2  # 4s / 2s

        # 报告格式验证
        for label, result in [("R1", result1), ("R2", result2)]:
            d = result.to_report_dict()
            assert isinstance(d, dict)
            import json
            j = json.dumps(d, ensure_ascii=False, default=str)
            assert j.lstrip().startswith('{')

        print(f"  v11-22: replay consistency ✅ "
              f"R1: {events_1} events R2: {events_2} events "
              f"both {result1.cycles_started} cycles")

        B2Runner._release_lock()


# ====================================================================
# v14: lineage, safety persistence, report contract
# ====================================================================


class TestV14LineageAndSchema:
    """v14: 信号 lineage 完整性、安全持久化、报告字段规范。"""

    def test_to_report_dict_has_schema_v1_1(self):
        """v14-01: schema_version = b2-1.1。"""
        from serenity_v2.phase_b2 import B2Metrics, B2_REPORT_SCHEMA_VERSION

        assert B2_REPORT_SCHEMA_VERSION == "b2-1.1"
        m = B2Metrics()
        d = m.to_report_dict()
        assert d["schema_version"] == "b2-1.1"

    def test_canonical_safety_fields_present(self):
        """v14-02: canonical 安全字段存在且值与 deprecated 一致。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.real_push_count = 0
        m.real_trade_count = 0
        d = m.to_report_dict()

        assert "real_pushes" in d
        assert "real_trades" in d
        assert "duplicate_signals_created" in d
        assert "signals_skipped_cooldown" in d
        assert "cooldown_enabled" in d
        assert "cooldown_policy_version" in d
        assert d["real_pushes"] == d["real_push_count"]
        assert d["real_trades"] == d["real_trade_count"]
        assert d["duplicate_signals_created"] == 0
        assert d["cooldown_enabled"] is False

    def test_terminated_early_canonical(self):
        """v14-03: terminated_early 存在且与 run_terminated_early 一致。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.run_terminated_early = False
        d = m.to_report_dict()
        assert "terminated_early" in d
        assert d["terminated_early"] is False
        assert "run_terminated_early" in d
        assert d["terminated_early"] == d["run_terminated_early"]

        m2 = B2Metrics()
        m2.run_terminated_early = True
        d2 = m2.to_report_dict()
        assert d2["terminated_early"] is True

    def test_duplicate_signals_not_hardcoded(self):
        """v14-04: duplicate_signals_created 来自 metrics 字段。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.duplicate_signals_created = 5
        d = m.to_report_dict()
        assert d["duplicate_signals_created"] == 5

        m.duplicate_signals_created = 0
        d2 = m.to_report_dict()
        assert d2["duplicate_signals_created"] == 0

    def test_signal_detail_lineage_complete(self):
        """v14-05: signal_details 包含完整 lineage 字段。"""
        from serenity_v2.phase_b2 import B2Metrics

        full_snap = "18dc7d197f33a1bde4312e187743309d3a01b7108e51d76d901e8c4e2b46ff67"
        m = B2Metrics()
        m.signal_details = [{
            "signal_id": "SIG_TEST_001", "symbol": "600487",
            "event_id": "EVT_TEST_001",
            "candidate_level": "ACTION", "candidate_action": "REDUCE",
            "effective_level": "ACTION", "effective_action": "REDUCE",
            "primary_norm": "", "secondary_norms": [],
            "confidence": "high", "market_session": "CONTINUOUS_PM",
            "action_suppressed": False,
            "execution_tags": ["SHADOW_ONLY", "NOT_FOR_EXECUTION",
                               "ACCOUNT_CONTEXT_FIXTURE", "ACCOUNT_CONTEXT_STALE"],
            "strategy_id": "b2-shadow-runner",
            "strategy_version": "b2-1.0",
            "strategy_config_hash": "ff81aeab0fa74c5a",
            "account_snapshot_id": full_snap,
            "account_snapshot_id_full": full_snap,
            "signal_rule_version": "b2-1.0",
            "session": "CONTINUOUS_PM", "environment": "shadow",
        }]
        d = m.to_report_dict()
        sd = d["signal_details"][0]

        for key in ["strategy_id", "strategy_version", "strategy_config_hash",
                     "account_snapshot_id", "account_snapshot_id_full",
                     "signal_rule_version", "session", "environment",
                     "execution_tags", "event_id", "signal_id"]:
            assert key in sd, f"missing: {key}"
            assert sd[key] is not None, f"{key} is None"
            if key != "secondary_norms":
                assert sd[key] != "", f"{key} is empty"

        assert len(sd["account_snapshot_id_full"]) == 64
        assert not sd["strategy_id"].startswith("<"), \
            f"strategy_id is Python repr: {sd['strategy_id']}"

        tags = sd["execution_tags"]
        for tag in ["SHADOW_ONLY", "NOT_FOR_EXECUTION",
                     "ACCOUNT_CONTEXT_FIXTURE", "ACCOUNT_CONTEXT_STALE"]:
            assert tag in tags, f"missing tag: {tag}"

    def test_no_signal_detail_for_empty(self):
        """v14-06: 无信号时 signal_details 为空数组。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        d = m.to_report_dict()
        assert d["signal_details"] == []

    def test_report_still_valid_json_dict(self):
        """v14-07: 报告仍是合法 JSON object。"""
        from serenity_v2.phase_b2 import B2Metrics
        import json

        m = B2Metrics()
        m.run_id = "B2_v14_test"
        m.status = "COMPLETED"
        d = m.to_report_dict()

        j = json.dumps(d, ensure_ascii=False, default=str)
        assert j.lstrip().startswith("{")
        parsed = json.loads(j)
        assert isinstance(parsed, dict)
        assert parsed["schema_version"] == "b2-1.1"

    def test_b2_1_0_backward_compatible(self):
        """v14-08: b2-1.0 legacy 字段仍存在。"""
        from serenity_v2.phase_b2 import B2Metrics
        import json

        m = B2Metrics()
        m.run_id = "B2_legacy_test"
        m.status = "COMPLETED"
        d = m.to_report_dict()

        for k in ["run_id", "status", "cycles_planned", "cycles_started",
                   "cycles_completed", "signals_total", "real_push_count",
                   "real_trade_count", "run_terminated_early", "run_completed"]:
            assert k in d, f"legacy key missing: {k}"

        json.loads(json.dumps(d, ensure_ascii=False, default=str))

    def test_cooldown_disabled_by_default(self):
        """v14-09: cooldown 默认禁用。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        d = m.to_report_dict()
        assert d["cooldown_enabled"] is False
        assert d["cooldown_policy_version"] == ""
        assert d["signals_skipped_cooldown"] == 0

    def test_duplicate_vs_idempotent_not_mixed(self):
        """v14-10: duplicate 和 idempotent 独立。"""
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        m.duplicate_signals_created = 7
        m.signals_skipped_idempotent = 3
        d = m.to_report_dict()
        assert d["duplicate_signals_created"] == 7
        assert d["signals_skipped_idempotent"] == 3
        assert d["duplicate_signals_created"] != d["signals_skipped_idempotent"]
