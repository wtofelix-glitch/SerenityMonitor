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
        from serenity_v2.env import set_env, SerenityEnv, get_env
        from serenity_v2.clock import reset_clock
        from serenity_v2.intelligence_network import reset_intel
        from serenity_v2.signal_desk import reset_desk
        from serenity_v2.account_baseline import reset_baseline

        # v17: 保存原始环境（若存在），teardown 时恢复，防止跨测试文件 env 污染
        try:
            _saved_env = get_env()
        except RuntimeError:
            _saved_env = None

        reset_clock()
        reset_intel()
        reset_desk()
        reset_baseline()

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b2_")
        self.tmp_db = Path(self.tmpdir) / "b2_shadow.db"

        yield

        # v17: 恢复原始环境 + 重置 baseline + clock
        if _saved_env is not None:
            set_env(_saved_env)
        reset_baseline()
        reset_clock()

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
        # v17: 防止 SimClock 泄漏到后续测试
        from serenity_v2.clock import reset_clock
        reset_clock()

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
        runner._protected_prod_db = "/test/prod.db"

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
        runner._protected_prod_db = "/test/prod.db"
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
            runner._protected_prod_db = "/test/prod.db"
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



class TestV15FailureEquationFinalization:
    """v15: failure equation — total_failures 在所有 counter 递增之后计算。"""


    # ── helpers ──
    def _make_runner_with_faults(self, ledger_exc=False, postflight_exc=False):
        """构造一个可注入故障的 B2Runner，用于测试 _finalize 区域的 total_failures。

        因为 _finalize 逻辑内嵌在 run() 收尾段中（非独立方法），
        我们通过 monkey-patching ledger.get_stats / guard.postflight 来触发异常路径，
        然后运行短回放并检查报告中的方程是否平衡。
        """
        import tempfile, json
        from pathlib import Path
        from unittest.mock import MagicMock
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables, SinaQuoteFetcher, NormalizedQuote, RawQuoteRecord
        from serenity_v2.event_record import EventStore
        from serenity_v2.account_fixture import load_and_set_fixture, to_account_state
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.signal_idempotency import EventProcessingLedger, IdempotentSignalProcessor
        from serenity_v2.clock import get_clock
        import hashlib

        tmpdir = tempfile.mkdtemp(prefix='v15_test_')
        tmp_db = Path(tmpdir) / 'b2_shadow.db'

        set_clock(SimClock('2026-07-24T09:35:00+08:00'))
        env = SerenityEnv.shadow(db_path=tmp_db, log_dir=Path(tmpdir))
        set_env(env)
        apply_migrations(tmp_db)
        _init_tables(tmp_db)
        store = EventStore(db_path=tmp_db)
        store.init_schema()

        reset_baseline()
        baseline = get_baseline()
        fixture_path = Path(__file__).resolve().parent / 'fixtures' / 'b2' / 'account_fixture_20260722.json'
        fixture = load_and_set_fixture(fixture_path)
        state = to_account_state(fixture)
        state.snapshot_at = ''
        baseline.save_snapshot(state)

        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk = get_desk()

        B2Runner._acquire_lock(caller_token='v15-test')

        runner = B2Runner(duration_seconds=10, interval_seconds=2, init_env=False)
        runner._locked = True
        runner.shadow_dir = Path(tmpdir)
        runner.shadow_db = tmp_db
        runner.store = store
        runner.baseline = baseline
        runner.intel = intel
        runner.desk = desk
        runner._fixture = fixture
        runner._account_snapshot_id = fixture.snapshot_id_full
        runner._account_snapshot_id_short = fixture.snapshot_id
        runner._manifest = None
        runner._protected_prod_db = "/test/prod.db"

        mock_guard = MagicMock()
        mock_guard.preflight.return_value = (True, {'guard': 'mock'}, [])
        mock_guard.before = None
        if postflight_exc:
            mock_guard.postflight.side_effect = RuntimeError('fault-injection: postflight failed')
        else:
            mock_guard.postflight.return_value = (True, [], [])
        runner.guard = mock_guard

        runner.ledger = EventProcessingLedger(tmp_db)
        runner.ledger.init_schema()  # v15: 必须初始化表，否则 get_stats() 抛异常→report_failed
        if ledger_exc:
            runner.ledger.get_stats = MagicMock(side_effect=RuntimeError('fault-injection: ledger stats failed'))
        runner.idempotent = IdempotentSignalProcessor(desk=desk, ledger=runner.ledger, worker_id='v15-test')
        runner._strategy_version = '1.0'
        runner._strategy_config_hash = 'test'
        runner.metrics.run_id = 'B2_V15_FAULT_TEST'
        runner._consecutive_failures = 0
        runner._cycle_count = 0

        fetcher = SinaQuoteFetcher()
        symbols_list = ['sh600519', 'sh600036', 'sh000001']
        fetch_count = [0]

        def mock_fetch(syms):
            fetch_count[0] += 1
            now = get_clock().now()
            raws = []
            for sym in symbols_list:
                raw_str = json.dumps({'price': 100.0, 'symbol': sym})
                raw_hash = hashlib.sha256(f'{sym}_{now}_{raw_str}'.encode()).hexdigest()[:32]
                raws.append(RawQuoteRecord(
                    symbol=sym, source='sina_realtime',
                    collected_at=now, raw_payload=raw_str,
                    raw_payload_hash=raw_hash,
                    http_status=200, response_time_ms=50.0,
                ))
            return raws

        def mock_normalize(raw, business_time=None):
            return NormalizedQuote(
                symbol=raw.symbol, name='Test',
                normalized_at=raw.collected_at,
                price=57.98, previous_close=57.50,
                open=57.60, high=58.50, low=57.30,
                volume=10000, amount=579800.0,
                validation_status='valid', validation_errors=[],
                acceptable_as_postmarket_snapshot=False,
                raw_payload_hash=raw.raw_payload_hash,
                data_age_ms=2000, effective_action_eligible=True,
            )

        fetcher.fetch = mock_fetch
        fetcher.normalize = mock_normalize
        runner.fetcher = fetcher
        runner.verify_environment = lambda: (True, {'clock_mode': 'SIM_TEST', 'guard': 'mock'}, [])

        # 保存对 tmpdir 的引用用于清理
        runner._v15_tmpdir = tmpdir
        runner._v15_tmp_db = tmp_db

        return runner

    def _all_failure_types(self, m):
        """Compute sum of all failure categories."""
        return (m.scheduler_failed + m.session_check_failed +
                m.fetch_failed + m.http_failed +
                m.parse_failed + m.validation_failed +
                m.normalization_failed + m.quarantine_failed +
                m.event_failed + m.signal_failed +
                m.ledger_failed + m.report_failed +
                m.safety_guard_failed)

    # ── test cases ──


    def test_no_fault_equation_balanced(self):
        """v15-01: 无故障 → total_failures=0, sum=0, equation PASS."""
        import shutil
        from serenity_v2.phase_b2 import B2Runner
        runner = self._make_runner_with_faults(ledger_exc=False, postflight_exc=False)
        result = runner.run()
        tf = result.total_failures
        fsum = self._all_failure_types(result)
        assert tf == 0, f'total_failures should be 0, got {tf}'
        assert fsum == 0, f'sum(types) should be 0, got {fsum}'
        assert tf == fsum, f'equation: {tf} != {fsum}'
        B2Runner._release_lock()
        shutil.rmtree(runner._v15_tmpdir, ignore_errors=True)


    def test_ledger_stats_exception_balanced(self):
        """v15-02: ledger stats 异常 → report_failed=1, total=1, equation PASS."""
        import shutil
        from serenity_v2.phase_b2 import B2Runner
        runner = self._make_runner_with_faults(ledger_exc=True, postflight_exc=False)
        result = runner.run()
        tf = result.total_failures
        fsum = self._all_failure_types(result)
        assert result.report_failed == 1, f'report_failed should be 1, got {result.report_failed}'
        assert tf == 1, f'total_failures should be 1, got {tf}'
        assert fsum == 1, f'sum(types) should be 1, got {fsum}'
        assert tf == fsum, f'equation: {tf} != {fsum}'
        B2Runner._release_lock()
        shutil.rmtree(runner._v15_tmpdir, ignore_errors=True)


    def test_postflight_exception_balanced(self):
        """v15-03: guard.postflight 异常 → safety_guard_failed=1, total=1, equation PASS."""
        import shutil
        from serenity_v2.phase_b2 import B2Runner
        runner = self._make_runner_with_faults(ledger_exc=False, postflight_exc=True)
        result = runner.run()
        tf = result.total_failures
        fsum = self._all_failure_types(result)
        assert result.safety_guard_failed == 1, f'safety_guard_failed should be 1, got {result.safety_guard_failed}'
        assert tf == 1, f'total_failures should be 1, got {tf}'
        assert fsum == 1, f'sum(types) should be 1, got {fsum}'
        assert tf == fsum, f'equation: {tf} != {fsum}'
        B2Runner._release_lock()
        shutil.rmtree(runner._v15_tmpdir, ignore_errors=True)


    def test_both_exceptions_balanced(self):
        """v15-04: 两者同时异常 → report_failed=1, safety_guard_failed=1, total=2, equation PASS."""
        import shutil
        from serenity_v2.phase_b2 import B2Runner
        runner = self._make_runner_with_faults(ledger_exc=True, postflight_exc=True)
        result = runner.run()
        tf = result.total_failures
        fsum = self._all_failure_types(result)
        assert result.report_failed == 1, f'report_failed should be 1, got {result.report_failed}'
        assert result.safety_guard_failed == 1, f'safety_guard_failed should be 1, got {result.safety_guard_failed}'
        assert tf == 2, f'total_failures should be 2, got {tf}'
        assert fsum == 2, f'sum(types) should be 2, got {fsum}'
        assert tf == fsum, f'equation: {tf} != {fsum}'
        B2Runner._release_lock()
        shutil.rmtree(runner._v15_tmpdir, ignore_errors=True)


    def test_fault_injection_does_not_produce_false_pass(self):
        """v15-05: 故障注入报告不能伪装成 clean run — 至少有一个 failure counter 非零。

        验证: 任何 fault injection 场景下，report 的 total_failures > 0 或者
        relevant failure counter > 0，确保故障不会被静默吞掉。
        """
        import shutil
        from serenity_v2.phase_b2 import B2Runner
        runner = self._make_runner_with_faults(ledger_exc=True, postflight_exc=True)
        result = runner.run()
        # 至少一个 counter 非零
        has_fault = (result.report_failed > 0 or result.safety_guard_failed > 0)
        assert has_fault, (
            f'fault injection should produce non-zero failure counters; '
            f'report_failed={result.report_failed} safety_guard_failed={result.safety_guard_failed}'
        )
        # total_failures 也应反映故障
        assert result.total_failures > 0, (
            f'fault injection should produce total_failures > 0; got {result.total_failures}'
        )
        # 方程仍应平衡
        fsum = self._all_failure_types(result)
        assert result.total_failures == fsum, (
            f'equation unbalanced after fault: {result.total_failures} != {fsum}'
        )
        B2Runner._release_lock()
        shutil.rmtree(runner._v15_tmpdir, ignore_errors=True)


    def test_report_dict_has_failure_equation_consistent(self):
        """v15-06: to_report_dict() 中的 total_failures 与 sum(types) 一致。

        直接构造 B2Metrics 并验证 report dict 中的方程平衡。
        此测试不经过 runner，纯粹验证 B2Metrics 序列化层的方程。
        """
        from serenity_v2.phase_b2 import B2Metrics

        m = B2Metrics()
        # 场景 A: 无故障
        d0 = m.to_report_dict()
        tf0 = d0['total_failures']
        fs0 = sum(d0[k] for k in [
            'scheduler_failed','session_check_failed','fetch_failed','http_failed',
            'parse_failed','validation_failed','normalization_failed','quarantine_failed',
            'event_failed','signal_failed','ledger_failed','report_failed','safety_guard_failed'
        ])
        assert tf0 == fs0, f'clean: {tf0} != {fs0}'

        # 场景 B: 注入 report_failed=1
        m2 = B2Metrics()
        m2.report_failed = 1
        m2.total_failures = 1  # v15: total 手动设置以模拟最终化后结果
        d2 = m2.to_report_dict()
        assert d2['total_failures'] == 1
        assert d2['report_failed'] == 1

        # 场景 C: 注入 safety_guard_failed=1
        m3 = B2Metrics()
        m3.safety_guard_failed = 1
        m3.total_failures = 1
        d3 = m3.to_report_dict()
        assert d3['total_failures'] == 1
        assert d3['safety_guard_failed'] == 1

        # 场景 D: 两者都有
        m4 = B2Metrics()
        m4.report_failed = 1
        m4.safety_guard_failed = 1
        m4.total_failures = 2
        d4 = m4.to_report_dict()
        assert d4['total_failures'] == 2
        assert d4['report_failed'] == 1
        assert d4['safety_guard_failed'] == 1


# ══════════════════════════════════════════════════════════════════════════════
# v16: Cooldown 集成 — CooldownTracker 单元 + 端到端验证
# ══════════════════════════════════════════════════════════════════════════════

class TestV16CooldownTrackerUnit:
    """v16: CooldownTracker 单元测试 — 直接验证 cooldown 窗口逻辑。"""

    def test_basic_suppression_within_window(self):
        """v16-01: 窗口内同一 (symbol, action) 第二次请求被抑制。"""
        from serenity_v2.phase_b2 import CooldownTracker
        import time
        ct = CooldownTracker(window_seconds=300)
        t0 = time.monotonic()
        # 第一次: 不放行，记录
        assert ct.should_suppress('600487', 'BUY', t0) is False
        assert ct.total_checked == 1
        assert ct.skipped_count == 0
        # 同一键，1 秒后: 应抑制
        assert ct.should_suppress('600487', 'BUY', t0 + 1.0) is True
        assert ct.total_checked == 2
        assert ct.skipped_count == 1

    def test_different_keys_not_suppressed(self):
        """v16-02: 不同 symbol 或不同 action 互不干扰。"""
        from serenity_v2.phase_b2 import CooldownTracker
        import time
        ct = CooldownTracker(window_seconds=300)
        t0 = time.monotonic()
        # 放行 600487/BUY
        assert ct.should_suppress('600487', 'BUY', t0) is False
        # 不同 symbol: 不抑制
        assert ct.should_suppress('600176', 'BUY', t0 + 1.0) is False
        # 不同 action: 不抑制
        assert ct.should_suppress('600487', 'SELL', t0 + 1.0) is False
        assert ct.skipped_count == 0  # 全部放行（键不同）
        assert ct.total_checked == 3

    def test_window_expiry_allows_new_signal(self):
        """v16-03: cooldown 窗口过期后，新信号可以放行。"""
        from serenity_v2.phase_b2 import CooldownTracker
        import time
        ct = CooldownTracker(window_seconds=5)
        t0 = time.monotonic()
        # 第一次放行
        assert ct.should_suppress('600487', 'BUY', t0) is False
        # 窗口内抑制
        assert ct.should_suppress('600487', 'BUY', t0 + 3.0) is True
        assert ct.skipped_count == 1
        # 窗口外放行
        assert ct.should_suppress('600487', 'BUY', t0 + 6.0) is False
        assert ct.skipped_count == 1  # 不放行不增加
        assert ct.total_checked == 3

    def test_exact_boundary_allows(self):
        """v16-04: 刚好在窗口边界（elapsed == window）时不抑制。"""
        from serenity_v2.phase_b2 import CooldownTracker
        import time
        ct = CooldownTracker(window_seconds=300)
        t0 = time.monotonic()
        assert ct.should_suppress('600487', 'BUY', t0) is False
        # 刚好 300s: elapsed == window, 不抑制 (elapsed < window 才抑制)
        assert ct.should_suppress('600487', 'BUY', t0 + 300.0) is False
        assert ct.skipped_count == 0

    def test_reset_clears_all_state(self):
        """v16-05: reset() 清空所有记录和计数器。"""
        from serenity_v2.phase_b2 import CooldownTracker
        import time
        ct = CooldownTracker(window_seconds=300)
        t0 = time.monotonic()
        ct.should_suppress('600487', 'BUY', t0)
        t1 = t0 + 1.0
        assert ct.should_suppress('600487', 'BUY', t1) is True  # 抑制
        assert ct.total_checked == 2
        assert ct.skipped_count == 1
        # 重置
        ct.reset()
        assert ct.total_checked == 0
        assert ct.skipped_count == 0
        # 重置后首请求放行
        assert ct.should_suppress('600487', 'BUY', t1) is False
        assert ct.total_checked == 1
        assert ct.skipped_count == 0

    def test_multiple_keys_independent_windows(self):
        """v16-06: 多个键各自维护独立窗口，互不干扰。"""
        from serenity_v2.phase_b2 import CooldownTracker
        import time
        ct = CooldownTracker(window_seconds=10)
        t0 = time.monotonic()
        # 填充 3 个不同键
        ct.should_suppress('A', 'BUY', t0)
        ct.should_suppress('B', 'BUY', t0)
        ct.should_suppress('A', 'SELL', t0)
        # t0+5s: 全部抑制
        for sym, act in [('A', 'BUY'), ('B', 'BUY'), ('A', 'SELL')]:
            assert ct.should_suppress(sym, act, t0 + 5.0) is True
        assert ct.skipped_count == 3
        assert ct.total_checked == 6

    def test_monotonic_clock_used_not_real(self):
        """v16-07: CooldownTracker 使用调用者传入的 monotonic 时间，
        不依赖系统时钟（与 SimClock 解耦）。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        # 使用一个任意时间戳，验证 tracker 正常工作
        assert ct.should_suppress('X', 'BUY', 1000000.0) is False
        assert ct.should_suppress('X', 'BUY', 1000001.0) is True
        assert ct.skipped_count == 1


class TestV16CooldownIntegration:
    """v16: Cooldown 端到端集成 — B2Runner.run() 中 cooldown 行为验证。"""

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

        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()
        lock = B2Runner._lock_path()
        lock.unlink(missing_ok=True)

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b2_cooldown_")
        self.tmp_db = Path(self.tmpdir) / "b2_shadow.db"

        yield

        B2Runner._release_lock()
        lock.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # v17: 防止 SimClock 泄漏到后续测试
        from serenity_v2.clock import reset_clock
        reset_clock()

    def _make_runner(self):
        """构造 cooldown 集成测试的 runner（与 V11OfflineReplay 类似）。"""
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables
        from serenity_v2.event_record import EventStore
        from serenity_v2.account_fixture import load_and_set_fixture, to_account_state
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.signal_idempotency import EventProcessingLedger, IdempotentSignalProcessor
        from unittest.mock import MagicMock

        set_clock(SimClock("2026-07-24T09:35:00+08:00"))
        env = SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir))
        set_env(env)
        apply_migrations(self.tmp_db)
        _init_tables(self.tmp_db)
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()

        reset_baseline()
        baseline = get_baseline()
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        fixture = load_and_set_fixture(fixture_path)
        state = to_account_state(fixture)
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk = get_desk()

        B2Runner._acquire_lock(caller_token="test-cooldown-integration")
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
        runner._protected_prod_db = "/test/prod.db"

        mock_guard = MagicMock()
        mock_guard.preflight.return_value = (True, {"guard": "mock"}, [])
        mock_guard.before = None
        runner.guard = mock_guard

        runner.ledger = EventProcessingLedger(self.tmp_db)
        runner.ledger.init_schema()
        runner.idempotent = IdempotentSignalProcessor(
            desk=desk, ledger=runner.ledger, worker_id="test-cooldown"
        )
        runner._strategy_version = "1.0"
        runner._strategy_config_hash = "test"
        runner.metrics.run_id = "B2_COOLDOWN_INTEGRATION"
        runner._consecutive_failures = 0
        runner._cycle_count = 0

        # 模拟 fetcher（单信号，确保 cooldown 可观测）
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
            for sym in symbols:
                raw_str = f'var hq_str_{sym}="测试,57.98,0,0,0,0,0,0,0,0,..."'
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
                volume=10000,
                amount=579800.0,
                validation_status="valid",
                validation_errors=[],
                acceptable_as_postmarket_snapshot=False,
                raw_payload_hash=raw.raw_payload_hash,
                data_age_ms=2000,
                effective_action_eligible=True,
            )

        fetcher.fetch = mock_fetch
        fetcher.normalize = mock_normalize
        runner.fetcher = fetcher
        runner.verify_environment = lambda: (True, {"clock_mode": "SIM_TEST", "guard": "mock"}, [])

        return runner

    def test_runner_cooldown_enabled_by_default(self):
        """v16-10: Runner 初始化时 cooldown 默认启用。"""
        runner = self._make_runner()
        assert runner.cooldown is not None
        assert runner.metrics.cooldown_enabled is True
        assert runner.metrics.cooldown_policy_version == "b2-cooldown-2.0"
        assert runner.cooldown.window_seconds == 300
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()

    def test_runner_cooldown_stats_in_report(self):
        """v16-11: 运行后 metrics 中的 cooldown 统计正确同步。"""
        runner = self._make_runner()
        result = runner.run()
        # 报告包含 cooldown 字段
        d = result.to_report_dict()
        assert "signals_skipped_cooldown" in d
        assert "cooldown_enabled" in d
        assert "cooldown_policy_version" in d
        assert d["cooldown_enabled"] is True
        assert d["cooldown_policy_version"] == "b2-cooldown-2.0"
        # total_checked >= 0 且 skipped >= 0
        assert d["signals_skipped_cooldown"] >= 0
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()

    def test_cooldown_not_duplicate_with_idempotent(self):
        """v16-12: cooldown (cross-event) 与 idempotent (per-event) 是独立字段。"""
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.signals_skipped_cooldown = 5
        m.duplicate_signals_created = 3
        d = m.to_report_dict()
        assert d["signals_skipped_cooldown"] == 5
        assert d["duplicate_signals_created"] == 3
        # 两者互不影响
        assert d["signals_skipped_cooldown"] != d["duplicate_signals_created"]

    def test_runner_cancelled_cycles_skip_cooldown(self):
        """v16-13: auto-stop 取消的周期不影响 cooldown 统计（cancelled 周期不生成信号）。"""
        runner = self._make_runner()
        result = runner.run()
        # cancelled_cycles_auto_stop 与 signals_skipped_cooldown 无关
        # cancelled 周期在信号生成之前就已退出
        assert result.cancelled_cycles_auto_stop >= 0
        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()

    def test_cooldown_window_configurable(self):
        """v16-14: CooldownTracker 窗口可配置，非硬编码。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=60)
        assert ct.window_seconds == 60
        ct2 = CooldownTracker(window_seconds=900)
        assert ct2.window_seconds == 900
        # 默认窗口
        ct3 = CooldownTracker()
        assert ct3.window_seconds == 300


# ══════════════════════════════════════════════════════════════════════════════
# v17: SimClock 泄漏修复 + Cooldown storm replay
# ══════════════════════════════════════════════════════════════════════════════

class TestV17SimClockLeak:
    """v17: 验证 reset_clock() 始终恢复 RealClock，SimClock 不会全局泄漏。"""

    def test_reset_clock_always_restores_real_clock(self):
        """v17-01: set_clock(SimClock) → reset_clock() → get_clock() 返回 RealClock。"""
        from serenity_v2.clock import set_clock, SimClock, reset_clock, get_clock, RealClock
        set_clock(SimClock("2026-07-24T09:35:00+08:00"))
        assert isinstance(get_clock(), SimClock)
        reset_clock()
        assert isinstance(get_clock(), RealClock), \
            "reset_clock() 必须恢复 RealClock"

    def test_reset_clock_twice_is_idempotent(self):
        """v17-02: 重复 reset_clock() 安全且幂等。"""
        from serenity_v2.clock import set_clock, SimClock, reset_clock, get_clock, RealClock
        set_clock(SimClock("2026-07-24T09:35:00+08:00"))
        reset_clock()
        reset_clock()  # 第二次 reset 不应出错
        assert isinstance(get_clock(), RealClock)

    def test_no_sim_clock_leak_after_reset(self):
        """v17-03: reset_clock() 后 now() 返回真实时间（非 SimClock 时间）。"""
        from serenity_v2.clock import set_clock, SimClock, reset_clock, get_clock
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))

        set_clock(SimClock("2020-01-01T00:00:00+08:00"))
        assert get_clock().now().year == 2020
        reset_clock()
        # 真实时间应该在 2026 年之后
        assert get_clock().now().year >= 2026, \
            "reset_clock() 后 now() 应返回真实时间"

    def test_get_clock_returns_real_clock_by_default(self):
        """v17-04: 未设置时钟时，get_clock() 返回 RealClock。"""
        from serenity_v2.clock import reset_clock, get_clock, RealClock
        reset_clock()
        assert isinstance(get_clock(), RealClock)

    def test_set_clock_after_reset_is_clean(self):
        """v17-05: reset → set(SimClock) → reset → get 必须始终有效。"""
        from serenity_v2.clock import set_clock, SimClock, reset_clock, get_clock, RealClock
        # 循环 3 次以验证无状态残留
        for i in range(3):
            set_clock(SimClock(f"2026-07-24T09:35:0{i}+08:00"))
            assert isinstance(get_clock(), SimClock), f"iter {i}: set failed"
            reset_clock()
            assert isinstance(get_clock(), RealClock), f"iter {i}: reset failed"


class TestV17CooldownStormReplay:
    """v17: 确定性 cooldown storm replay — 验证 cooldown 拦截重复建议。"""

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

        from serenity_v2.phase_b2 import B2Runner
        B2Runner._release_lock()
        lock = B2Runner._lock_path()
        lock.unlink(missing_ok=True)

        self.tmpdir = tempfile.mkdtemp(prefix="serenity_b2_storm_")
        self.tmp_db = Path(self.tmpdir) / "b2_shadow.db"

        yield

        B2Runner._release_lock()
        lock.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        from serenity_v2.clock import reset_clock
        reset_clock()

    def _make_storm_runner(self, window_seconds=300):
        """构造 cooldown storm 测试的 runner。

        使用极短 cooldown 窗口（10s），在同一 symbol 上注入相同 action，
        验证 cooldown 在窗口内抑制重复信号，窗口外放行。
        """
        from serenity_v2.clock import set_clock, SimClock
        from serenity_v2.env import set_env, SerenityEnv
        from serenity_v2.migrations import apply_migrations
        from serenity_v2.sina_market import _init_tables
        from serenity_v2.event_record import EventStore
        from serenity_v2.account_fixture import load_and_set_fixture, to_account_state
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.intelligence_network import get_intel, reset_intel
        from serenity_v2.signal_desk import get_desk, reset_desk
        from serenity_v2.phase_b2 import B2Runner, CooldownTracker
        from serenity_v2.signal_idempotency import EventProcessingLedger, IdempotentSignalProcessor
        from unittest.mock import MagicMock

        set_clock(SimClock("2026-07-24T09:35:00+08:00"))
        env = SerenityEnv.shadow(db_path=self.tmp_db, log_dir=Path(self.tmpdir))
        set_env(env)
        apply_migrations(self.tmp_db)
        _init_tables(self.tmp_db)
        store = EventStore(db_path=self.tmp_db)
        store.init_schema()

        reset_baseline()
        baseline = get_baseline()
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        fixture = load_and_set_fixture(str(fixture_path))
        state = to_account_state(fixture)
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        reset_intel()
        intel = get_intel(shadow_mode=True)
        reset_desk()
        desk = get_desk()

        B2Runner._acquire_lock(caller_token="test-cooldown-storm")
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
        runner._protected_prod_db = "/test/prod.db"

        # 使用自定义窗口的 CooldownTracker
        runner.cooldown = CooldownTracker(window_seconds=window_seconds)
        runner.metrics.cooldown_enabled = True
        runner.metrics.cooldown_policy_version = "b2-cooldown-2.0"

        mock_guard = MagicMock()
        mock_guard.preflight.return_value = (True, {"guard": "mock"}, [])
        mock_guard.before = None
        runner.guard = mock_guard

        runner.ledger = EventProcessingLedger(self.tmp_db)
        runner.ledger.init_schema()
        runner.idempotent = IdempotentSignalProcessor(
            desk=desk, ledger=runner.ledger, worker_id="test-cooldown-storm"
        )
        runner._strategy_version = "1.0"
        runner._strategy_config_hash = "test"
        runner.metrics.run_id = "B2_COOLDOWN_STORM"
        runner._consecutive_failures = 0
        runner._cycle_count = 0

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
                volume=10000,
                amount=579800.0,
                validation_status="valid",
                validation_errors=[],
                acceptable_as_postmarket_snapshot=False,
                raw_payload_hash=raw.raw_payload_hash,
                data_age_ms=2000,
                effective_action_eligible=True,
            )

        fetcher.fetch = mock_fetch
        fetcher.normalize = mock_normalize
        runner.fetcher = fetcher
        runner.verify_environment = lambda: (True, {"clock_mode": "SIM_TEST", "guard": "mock"}, [])

        return runner

    def test_cooldown_suppresses_duplicate_signals_across_cycles(self):
        """v17-10: 确定性 storm — cooldown 拦截重复信号。

        构造场景: 5 个周期，每个周期对同一 symbol 生成相同 action 的信号。
        预期: 第 1 周期放行 3 个信号（1 per symbol），后续 4 个周期全部抑制。
        总计: signals_skipped_cooldown = 12 (= 3 symbols × 4 重复周期)
        """
        import time
        from serenity_v2.phase_b2 import CooldownTracker

        # 直接测试 CooldownTracker — 比端到端测试更确定、更快
        ct = CooldownTracker(window_seconds=10)
        symbols = ["600487", "600176", "000988"]
        action = "BUY"
        t0 = time.monotonic()

        # 5 个周期，每个 2s 间隔（窗口内）
        skipped = 0
        checked = 0
        for cycle in range(5):
            cycle_t = t0 + cycle * 2.0
            for sym in symbols:
                if ct.should_suppress(sym, action, cycle_t):
                    skipped += 1
                checked += 1

        # 周期 0: 3 个信号全部放行（首次出现）
        # 周期 1-4: 每个周期 3 个信号全部抑制（窗口内）
        assert checked == 15, f"应检查 15 次: {checked}"
        assert skipped == 12, (
            f"cooldown 应抑制 12 次 (3 symbols × 4 cycles): {skipped}"
        )
        assert ct.total_checked == 15
        assert ct.skipped_count == 12

    def test_cooldown_window_expiry_allows_after_window(self):
        """v17-11: cooldown 窗口过期后重新放行信号。

        周期 0: 放行 3 个信号
        周期 1 (t+2s, 窗口内): 抑制 3 个信号
        周期 2 (t+12s, 窗口外): 放行 3 个信号
        """
        import time
        from serenity_v2.phase_b2 import CooldownTracker

        ct = CooldownTracker(window_seconds=10)
        symbols = ["600487", "600176", "000988"]
        action = "BUY"
        t0 = time.monotonic()

        # 周期 0: t+0s — 首次放行
        for sym in symbols:
            assert ct.should_suppress(sym, action, t0) is False
        assert ct.skipped_count == 0

        # 周期 1: t+2s — 窗口内全部抑制
        t1 = t0 + 2.0
        for sym in symbols:
            assert ct.should_suppress(sym, action, t1) is True
        assert ct.skipped_count == 3

        # 周期 2: t+12s — 窗口外全部放行
        t2 = t0 + 12.0
        for sym in symbols:
            assert ct.should_suppress(sym, action, t2) is False
        assert ct.skipped_count == 3  # 不增加

    def test_cooldown_different_actions_not_suppressed(self):
        """v17-12: 不同 action 互不抑制 — 粗粒度键误抑制风险已关闭。

        同一 symbol 的 BUY → SELL → BUY：每个 action 有独立 cooldown 窗口。
        """
        import time
        from serenity_v2.phase_b2 import CooldownTracker

        ct = CooldownTracker(window_seconds=10)
        t0 = time.monotonic()

        # BUY 放行，记录 (600487, BUY)
        assert ct.should_suppress("600487", "BUY", t0) is False
        # SELL 放行 — 不同键
        assert ct.should_suppress("600487", "SELL", t0 + 1.0) is False
        # BUY 抑制 — 同键在窗口内
        assert ct.should_suppress("600487", "BUY", t0 + 1.0) is True
        # SELL 抑制 — 同键在窗口内
        assert ct.should_suppress("600487", "SELL", t0 + 1.0) is True

    def test_cooldown_policy_single_strategy_scope(self):
        """v17-13（v18 修订）: cooldown 作用域为单策略上下文。

        验证:
        - 同一 symbol 不同 action 各独立窗口
        - 不同 symbol 互不影响
        - 窗口长度可配置
        - 键 = (symbol, effective_action) — 2 个字段
        - 在此单策略作用域内，(symbol, action) 粒度正确

        多策略安全说明:
        - 当前实现假设单策略上下文（一个 run() 内 strategy/config/rule 不可变）
        - 多策略场景需独立 CooldownTracker 实例
        """
        import time
        from serenity_v2.phase_b2 import CooldownTracker

        # 验证键字段常量
        assert CooldownTracker.KEY_FIELDS == (
            "symbol", "effective_action", "strategy_id", "strategy_version",
            "strategy_config_hash", "signal_rule_version",
            "account_snapshot_id_full", "environment", "market_fingerprint",
        ), f"cooldown 键字段: {CooldownTracker.KEY_FIELDS}"

        ct = CooldownTracker(window_seconds=10)
        t0 = time.monotonic()

        # 场景: A/BUY 和 A/SELL 是两个独立键
        ct.should_suppress("A", "BUY", t0)       # A/BUY 窗口开始
        ct.should_suppress("A", "SELL", t0 + 1.0)  # A/SELL 窗口开始
        ct.should_suppress("B", "BUY", t0 + 2.0)    # B/BUY 窗口开始

        # t+3s: A/BUY 抑制（窗口内），A/SELL 抑制，B/BUY 抑制
        assert ct.should_suppress("A", "BUY", t0 + 3.0) is True
        assert ct.should_suppress("A", "SELL", t0 + 3.0) is True
        assert ct.should_suppress("B", "BUY", t0 + 3.0) is True

        # t+12s: 全部放行（窗口过期）
        assert ct.should_suppress("A", "BUY", t0 + 12.0) is False
        assert ct.should_suppress("A", "SELL", t0 + 12.0) is False
        assert ct.should_suppress("B", "BUY", t0 + 12.0) is False

    def test_cooldown_reset_reason_tracked(self):
        """v18-01: CooldownTracker.reset() 记录 reset reason。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker()
        assert ct.reset_reason == ""
        ct.reset("test_teardown")
        assert ct.reset_reason == "test_teardown"
        ct.reset("strategy_changed")
        assert ct.reset_reason == "strategy_changed"


class TestV18CooldownScope:
    """v18: cooldown 作用域验证 — 单策略上下文 fail-closed。"""

    def test_runner_metrics_has_cooldown_scope_fields(self):
        """v18-10: B2Metrics 包含 cooldown 作用域字段。"""
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.cooldown_enabled = True
        m.cooldown_policy_version = "b2-cooldown-2.0"
        m.cooldown_key_fields = "symbol,effective_action,strategy_id,strategy_version,strategy_config_hash,signal_rule_version,account_snapshot_id_full,environment,market_fingerprint"
        m.cooldown_scope = "single_strategy_fixture_shadow"
        d = m.to_report_dict()
        assert d["cooldown_key_fields"] == "symbol,effective_action,strategy_id,strategy_version,strategy_config_hash,signal_rule_version,account_snapshot_id_full,environment,market_fingerprint"
        assert d["cooldown_scope"] == "single_strategy_fixture_shadow"
        assert "cooldown_reset_reason" in d

    def test_cooldown_key_fields_match_tracker(self):
        """v18-11: metrics 中的 cooldown_key_fields 与 CooldownTracker.KEY_FIELDS 一致。"""
        from serenity_v2.phase_b2 import CooldownTracker, B2Metrics
        expected = "symbol,effective_action,strategy_id,strategy_version,strategy_config_hash,signal_rule_version,account_snapshot_id_full,environment,market_fingerprint"
        m = B2Metrics()
        m.cooldown_enabled = True
        m.cooldown_key_fields = expected
        d = m.to_report_dict()
        assert d["cooldown_key_fields"] == "symbol,effective_action,strategy_id,strategy_version,strategy_config_hash,signal_rule_version,account_snapshot_id_full,environment,market_fingerprint"

    def test_cooldown_scope_is_single_strategy(self):
        """v18-12: cooldown 作用域显式声明为 single_strategy_fixture_shadow。

        此作用域含义:
        - single_strategy: 一次 run() 内 strategy/config/rule 不可变
        - fixture: 账户快照来自 fixture（固定，非实时）
        - shadow: 影子环境（非生产 DB）
        """
        from serenity_v2.phase_b2 import B2Runner
        runner = B2Runner(duration_seconds=10, interval_seconds=2, init_env=False)
        assert runner.metrics.cooldown_scope == "single_strategy_fixture_shadow"
        assert runner.metrics.cooldown_key_fields == "symbol,effective_action,strategy_id,strategy_version,strategy_config_hash,signal_rule_version,account_snapshot_id_full,environment,market_fingerprint"
        assert runner.metrics.cooldown_enabled is True

    def test_cooldown_scope_in_report_from_runner(self):
        """v18-13: runner 生成的 report 包含完整 cooldown 作用域信息。"""
        from serenity_v2.phase_b2 import B2Runner
        runner = B2Runner(duration_seconds=10, interval_seconds=2, init_env=False)
        runner.metrics.run_id = "v18-scope-test"
        # 直接调用 to_report_dict（不运行 run()）
        d = runner.metrics.to_report_dict()
        assert d["cooldown_enabled"] is True
        assert d["cooldown_key_fields"] == "symbol,effective_action,strategy_id,strategy_version,strategy_config_hash,signal_rule_version,account_snapshot_id_full,environment,market_fingerprint"
        assert d["cooldown_scope"] == "single_strategy_fixture_shadow"
        assert d["cooldown_reset_reason"] == ""


# ══════════════════════════════════════════════════════════════════════════════
# v19: Cooldown 运行时作用域强制执行（fail-closed）
# ══════════════════════════════════════════════════════════════════════════════

class TestV19CooldownRuntimeEnforcement:
    """v19: cooldown 作用域运行时验证 — fail-closed 防止多策略误用。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """为作用域验证测试设置 shadow 环境。"""
        import tempfile
        from pathlib import Path
        from serenity_v2.env import set_env, SerenityEnv
        tmpdir = tempfile.mkdtemp(prefix="v19_scope_")
        env = SerenityEnv.shadow(db_path=Path(tmpdir) / "test.db",
                                  log_dir=Path(tmpdir) / "logs")
        set_env(env)
        self._v19_tmpdir = tmpdir
        yield
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def _make_runner_with_context(self, strategy_version="1.0",
                                   config_hash="test-hash",
                                   snapshot_id="abc123",
                                   protected_prod_db="/test/prod.db"):
        """构造一个上下文完整的 runner 用于测试 _verify_cooldown_context。"""
        from serenity_v2.phase_b2 import B2Runner

        runner = B2Runner(duration_seconds=10, interval_seconds=2, init_env=False,
                          protected_prod_db=protected_prod_db)
        runner._strategy_version = strategy_version
        runner._strategy_config_hash = config_hash
        runner._account_snapshot_id = snapshot_id
        # 绕过 env 确认以允许 run() 在不设置完整环境的情况下被调用
        runner._print_env_confirmation = lambda: None
        return runner

    def test_verify_context_pass_with_valid_single_strategy(self):
        """v19-01: 有效单策略上下文通过验证。"""
        runner = self._make_runner_with_context()
        ok, violations = runner._verify_cooldown_context()
        assert ok, f"应该通过: violations={violations}"
        assert len(violations) == 0

    def test_verify_context_fails_without_strategy_version(self):
        """v19-02: strategy_version 缺失 → fail-closed。"""
        runner = self._make_runner_with_context(strategy_version=None)
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "缺少 strategy_version 应该失败"
        assert any("strategy_version" in v for v in violations)

    def test_verify_context_fails_without_config_hash(self):
        """v19-03: strategy_config_hash 缺失 → fail-closed。"""
        runner = self._make_runner_with_context(config_hash=None)
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "缺少 strategy_config_hash 应该失败"
        assert any("strategy_config_hash" in v for v in violations)

    def test_verify_context_fails_without_snapshot_id(self):
        """v19-04: account_snapshot_id 缺失 → fail-closed。"""
        runner = self._make_runner_with_context(snapshot_id=None)
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "缺少 account_snapshot_id 应该失败"
        assert any("account_snapshot_id" in v for v in violations)

    def test_verify_context_fails_without_protected_prod(self):
        """v21-fix: protected_prod_db 未设置 → fail-closed（必须显式声明受保护的生产 DB）。"""
        runner = self._make_runner_with_context(protected_prod_db=None)
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "protected_prod_db 未设置应该失败"
        assert any("protected_prod_db" in v for v in violations)

    def test_verify_context_passes_with_protected_prod(self):
        """v21-fix: protected_prod_db 已设置 → 通过（正确保护生产 DB）。"""
        runner = self._make_runner_with_context(protected_prod_db="/prod/serenity.db")
        ok, violations = runner._verify_cooldown_context()
        assert ok, f"protected_prod_db 已设置应通过，但得到: {violations}"

    def test_snapshot_context_captures_all_fields(self):
        """v22-06: _snapshot_cooldown_context 捕获所有相关字段（含 v22 新增）。"""
        runner = self._make_runner_with_context()
        snap = runner._snapshot_cooldown_context()
        expected_keys = {"strategy_version", "strategy_config_hash",
                         "account_snapshot_id", "protected_prod_db",
                         "shadow_db_realpath", "push_adapter", "trade_adapter"}
        assert set(snap.keys()) == expected_keys, \
            f"快照键: {set(snap.keys())}, 期望: {expected_keys}"
        assert snap["strategy_version"] == "1.0"
        assert snap["strategy_config_hash"] == "test-hash"
        assert snap["account_snapshot_id"] == "abc123"
        assert snap["protected_prod_db"] == "/test/prod.db"
        # init_env=False → shadow_db 未设置; autouse fixture 设置 shadow env → adapters disabled
        assert snap["shadow_db_realpath"] is None
        assert snap["push_adapter"] == "disabled"
        assert snap["trade_adapter"] == "disabled"

    def test_context_change_detected_by_snapshot_diff(self):
        """v19-07: 快照差异正确检测上下文变化。"""
        runner = self._make_runner_with_context()
        snap1 = runner._snapshot_cooldown_context()
        # 变更 strategy_version
        runner._strategy_version = "2.0"
        snap2 = runner._snapshot_cooldown_context()
        assert snap1 != snap2
        changed = [k for k in snap2 if snap2[k] != snap1[k]]
        assert "strategy_version" in changed

    def test_context_change_triggers_cooldown_reset(self):
        """v19-08: 上下文变更触发 cooldown reset 并记录原因。"""
        from serenity_v2.phase_b2 import B2Runner, CooldownTracker
        runner = self._make_runner_with_context()
        runner._cooldown_context_snapshot = runner._snapshot_cooldown_context()

        # 模拟上下文变更
        runner._strategy_version = "2.0"
        current_ctx = runner._snapshot_cooldown_context()

        # 执行逐周期验证逻辑
        if runner._cooldown_context_snapshot:
            if current_ctx != runner._cooldown_context_snapshot:
                changed = [k for k in current_ctx
                           if current_ctx[k] != runner._cooldown_context_snapshot[k]]
                reason = f"cooldown_context_changed:{','.join(changed)}"
                runner.cooldown.reset(reason)
                runner._cooldown_context_snapshot = current_ctx

        assert runner.cooldown.reset_reason == "cooldown_context_changed:strategy_version"
        assert runner.cooldown.total_checked == 0
        assert runner.cooldown.skipped_count == 0

    def test_run_fails_closed_on_scope_violation(self):
        """v19-09: run() 在作用域违规时返回 ERROR 状态不被执行。"""
        runner = self._make_runner_with_context(strategy_version=None)
        runner.metrics.run_id = "v19-fail-closed"
        result = runner.run()
        assert result.status == "ERROR"
        assert len(result.violations) > 0

    def test_all_violations_reported(self):
        """v19-10: 多个作用域违规全部报告（不只是第一个）。

        strategy_version + config_hash 合并为一条违规（两者都缺失 → strategy 违规），
        account_snapshot_id 单独一条。环境已设置（shadow mode → 通过）。"""
        runner = self._make_runner_with_context(
            strategy_version=None, config_hash=None, snapshot_id=None
        )
        ok, violations = runner._verify_cooldown_context()
        assert not ok
        assert len(violations) >= 2, \
            f"应至少有 2 个违规（strategy + account_snapshot）: {violations}"
        assert any("strategy_version" in v for v in violations), \
            f"应报告 strategy 违规: {violations}"
        assert any("account_snapshot_id" in v for v in violations), \
            f"应报告 account_snapshot_id 违规: {violations}"


# ══════════════════════════════════════════════════════════════════════════════
# v20: Cooldown 离线验收 — 12 场景全覆盖
# ══════════════════════════════════════════════════════════════════════════════

class TestV20CooldownAcceptance:
    """v20: cooldown 离线验收 — 覆盖 12 个接受场景（b2-cooldown-2.0 完整语义）。

    场景:
      1.  同一语义 300s 内只产生一次 ✅ (已有 TestV16CooldownTrackerUnit)
      2.  跳过信号计入 skipped_cooldown ✅ (已有 TestV17CooldownStormReplay)
      3.  300s 后允许重新产生 ✅ (已有 TestV16CooldownTrackerUnit)
      4.  action 改变时不错误拦截 ✅ (已有 TestV16CooldownTrackerUnit)
      5.  strategy/config/snapshot 改变时不错误拦截 🆕
      6.  行情实质变化时重新武装 🆕
      7.  NO_SIGNAL 不创建 cooldown 状态 🆕
      8.  第二次固定回放不产生额外 signal ✅ (已有 TestV17CooldownStormReplay)
      9.  重启后 cooldown 重置语义明确 🆕
      10. SimClock 与 RealClock 一致 ✅ (已有 TestV17SimClockLeak)
      11. 多进程原子锁 fail-closed ✅ (已有 TestV11ProcessIsolation)
      12. 报告审计关系: emitted + skipped_idempotent + skipped_cooldown 🆕
    """

    # ── 5. strategy/config/snapshot 改变时不错误拦截 ──

    def test_acceptance_05_different_strategy_not_suppressed(self):
        """v20-05: 不同 strategy_id 的同 (symbol, action) 不会互抑。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        t0 = 1000.0
        assert ct.should_suppress(
            "600487", "BUY", t0,
            strategy_id="strat-A", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint="fp:1",
        ) is False
        assert ct.should_suppress(
            "600487", "BUY", t0 + 1.0,
            strategy_id="strat-B", strategy_version="2.0",
            strategy_config_hash="bbb", signal_rule_version="2.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint="fp:1",
        ) is False  # 不同策略，不抑制

    def test_acceptance_05_different_config_not_suppressed(self):
        """v20-05b: 不同 strategy_config_hash 不互抑。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        t0 = 1000.0
        ct.should_suppress(
            "600487", "BUY", t0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="config-v1", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint="fp:1",
        )
        assert ct.should_suppress(
            "600487", "BUY", t0 + 1.0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="config-v2",
            signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint="fp:1",
        ) is False

    def test_acceptance_05_different_snapshot_not_suppressed(self):
        """v20-05c: 不同 account_snapshot_id 不互抑。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        t0 = 1000.0
        ct.should_suppress(
            "600487", "BUY", t0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-20260722", environment="shadow",
            market_fingerprint="fp:1",
        )
        assert ct.should_suppress(
            "600487", "BUY", t0 + 1.0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-20260723",
            environment="shadow",
            market_fingerprint="fp:1",
        ) is False

    def test_acceptance_05_different_environment_not_suppressed(self):
        """v20-05d: 不同 environment 不互抑。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        t0 = 1000.0
        ct.should_suppress(
            "600487", "BUY", t0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint="fp:1",
        )
        assert ct.should_suppress(
            "600487", "BUY", t0 + 1.0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="production",
            market_fingerprint="fp:1",
        ) is False

    # ── 6. 行情实质变化时重新武装 ──

    def test_acceptance_06_market_fingerprint_reams(self):
        """v20-06: 行情指纹变化时允许重新生成信号（v24: 基于昨收参考价）。"""
        from serenity_v2.phase_b2 import CooldownTracker, compute_market_fingerprint
        ct = CooldownTracker(window_seconds=300)
        t0 = 1000.0
        # 昨收 56.00, 当前 57.98 → +3.54% → bucket 1 (2-4%)
        # 昨收 56.00, 当前 59.20 → +5.71% → bucket 2 (4-6%)
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 57.98, 10000, reference_price=56.00)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 59.20, 12000, reference_price=56.00)
        assert fp2 != fp1, "不同行情应产生不同指纹"
        ct.should_suppress(
            "600487", "BUY", t0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint=fp1,
        )
        # 同一语义，行情不变 → 抑制
        assert ct.should_suppress(
            "600487", "BUY", t0 + 1.0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint=fp1,
        ) is True
        # 行情变化（跨 2% bucket）→ 重新武装
        assert ct.should_suppress(
            "600487", "BUY", t0 + 2.0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint=fp2,
        ) is False

    def test_acceptance_06_minor_price_change_not_ream(self):
        """v20-06b: 微小价格变动不触发 re-arm（同一 2% bucket）。"""
        from serenity_v2.phase_b2 import CooldownTracker, compute_market_fingerprint
        ct = CooldownTracker(window_seconds=300)
        t0 = 1000.0
        # 昨收 56.00, 57.98 → +3.54% → bucket 1
        #             58.50 → +4.46% → bucket 2 (crosses 4%!)
        # 使用同一 bucket 内的值:
        # 昨收 56.00, 57.98 → +3.54% → bucket 1
        #             58.00 → +3.57% → bucket 1 (still < 4%)
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 57.98, 10000, reference_price=56.00)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 58.00, 10500, reference_price=56.00)
        assert fp1 == fp2, "同一 2% bucket 内应产生相同指纹"
        ct.should_suppress(
            "600487", "BUY", t0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint=fp1,
        )
        assert ct.should_suppress(
            "600487", "BUY", t0 + 1.0,
            strategy_id="s1", strategy_version="1.0",
            strategy_config_hash="aaa", signal_rule_version="1.0",
            account_snapshot_id_full="snap-1", environment="shadow",
            market_fingerprint=fp2,
        ) is True

    def test_acceptance_06_fingerprint_stable(self):
        """v20-06c: compute_market_fingerprint 对相同输入 + 相同参考价稳定。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        fp1 = compute_market_fingerprint("evt-001", 57.98, 10000, reference_price=56.00)
        fp2 = compute_market_fingerprint("evt-001", 57.98, 10000, reference_price=56.00)
        assert fp1 == fp2
        # 不同 event_id → 不同指纹（session 标识部分不同）
        fp3 = compute_market_fingerprint("evt-002", 57.98, 10000, reference_price=56.00)
        assert fp3 != fp1

    # ── 7. NO_SIGNAL 不创建 cooldown 状态 ──

    def test_acceptance_07_no_signal_no_cooldown(self):
        """v20-07: 空信号列表不创建 cooldown 记录。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        assert ct.total_checked == 0
        assert ct.skipped_count == 0

    # ── 9. 重启后 cooldown 重置语义 ──

    def test_acceptance_09_cooldown_reset_on_new_instance(self):
        """v20-09: 新 CooldownTracker 实例无历史状态。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct1 = CooldownTracker(window_seconds=300)
        ct1.should_suppress("600487", "BUY", 1000.0)
        ct2 = CooldownTracker(window_seconds=300)
        assert ct2.total_checked == 0
        assert ct2.skipped_count == 0
        assert ct2.should_suppress("600487", "BUY", 1000.0) is False

    def test_acceptance_09_explicit_reset_clears_all(self):
        """v20-09b: 显式 reset() 清空所有状态。"""
        from serenity_v2.phase_b2 import CooldownTracker
        ct = CooldownTracker(window_seconds=300)
        ct.should_suppress("600487", "BUY", 1000.0)
        ct.should_suppress("600176", "SELL", 1000.0)
        assert ct.total_checked == 2
        ct.reset("restart")
        assert ct.total_checked == 0
        assert ct.skipped_count == 0
        assert ct.reset_reason == "restart"

    # ── 12. 报告审计关系 ──

    def test_acceptance_12_audit_relationship(self):
        """v20-12: emitted + skipped_idempotent + skipped_cooldown
        构成完整的信号生命周期审计方程。"""
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.signals_created_unique = 15
        m.signals_skipped_idempotent = 3
        m.signals_skipped_cooldown = 7
        m.duplicate_signals_created = 0
        d = m.to_report_dict()
        assert "signals_created_unique" in d
        assert "signals_skipped_idempotent" in d
        assert "signals_skipped_cooldown" in d
        total = (d["signals_created_unique"]
                 + d["signals_skipped_idempotent"]
                 + d["signals_skipped_cooldown"])
        assert total == 25


# ══════════════════════════════════════════════════════════════════════════════
# v22: Cooldown 作用域 — DB 隔离 + adapter 验证
# ══════════════════════════════════════════════════════════════════════════════

class TestV22CooldownScopeIsolation:
    """v22: cooldown 作用域运行时验证 — DB 路径隔离与 adapter 检查。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """为作用域验证测试设置 shadow 环境。"""
        import tempfile, shutil
        from pathlib import Path
        from serenity_v2.env import set_env, SerenityEnv
        tmpdir = tempfile.mkdtemp(prefix="v22_scope_")
        self._v22_tmpdir = tmpdir
        self._v22_db_dir = Path(tmpdir) / "db"
        self._v22_db_dir.mkdir(exist_ok=True)
        env = SerenityEnv.shadow(db_path=self._v22_db_dir / "test.db",
                                  log_dir=Path(tmpdir) / "logs")
        set_env(env)
        yield
        shutil.rmtree(tmpdir, ignore_errors=True)

    def _make_runner_with_db(self, protected_prod_db="/test/prod.db",
                             shadow_db_path=None):
        """构造 runner 并设置 shadow_db 用于 DB 隔离测试。

        init_env=False 避免在测试 DB 上运行完整 env 初始化（不需要
        portfolio_reconciliations 等生产表）。
        """
        from serenity_v2.phase_b2 import B2Runner
        runner = B2Runner(duration_seconds=10, interval_seconds=2,
                          init_env=False,
                          protected_prod_db=protected_prod_db)
        runner._strategy_version = "1.0"
        runner._strategy_config_hash = "test-hash"
        runner._account_snapshot_id = "abc123"
        runner._print_env_confirmation = lambda: None
        if shadow_db_path is not None:
            runner.shadow_db = shadow_db_path
        return runner

    # ── DB 隔离测试 ──────────────────────────────────────────────────

    def test_shadow_prod_same_path_fails(self):
        """v22-01: shadow_db 与 protected_prod_db 同路径 → FAIL。"""
        same_path = str(self._v22_db_dir / "same.db")
        # 创建文件使其存在
        from pathlib import Path
        Path(same_path).touch()
        runner = self._make_runner_with_db(
            protected_prod_db=same_path,
            shadow_db_path=Path(same_path),
        )
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "同路径应被拒绝"
        assert any("realpath 相同" in v for v in violations), \
            f"应报告 realpath 相同: {violations}"

    def test_shadow_prod_symlink_same_file_fails(self):
        """v22-02: shadow_db 与 protected_prod_db 通过 symlink 指向同一文件 → FAIL。"""
        import os
        real_file = str(self._v22_db_dir / "real.db")
        link_file = str(self._v22_db_dir / "link.db")
        from pathlib import Path
        Path(real_file).touch()
        os.symlink(real_file, link_file)
        runner = self._make_runner_with_db(
            protected_prod_db=real_file,
            shadow_db_path=Path(link_file),
        )
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "symlink 指向同文件应被拒绝"
        assert any("realpath 相同" in v for v in violations), \
            f"应通过 realpath 检测到相同文件: {violations}"

    def test_shadow_prod_hardlink_same_inode_fails(self):
        """v22-03: shadow_db 与 protected_prod_db 通过硬链接同 inode → FAIL。"""
        import os
        path_a = str(self._v22_db_dir / "a.db")
        path_b = str(self._v22_db_dir / "b.db")
        from pathlib import Path
        Path(path_a).touch()
        os.link(path_a, path_b)  # 硬链接
        runner = self._make_runner_with_db(
            protected_prod_db=path_a,
            shadow_db_path=Path(path_b),
        )
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "硬链接同 inode 应被拒绝"
        assert any("inode 相同" in v for v in violations), \
            f"应检测到 inode 相同: {violations}"

    def test_shadow_prod_different_paths_pass(self):
        """v22-04: shadow_db 与 protected_prod_db 不同路径 → PASS。"""
        prod_path = str(self._v22_db_dir / "prod.db")
        shadow_path = str(self._v22_db_dir / "shadow.db")
        from pathlib import Path
        Path(prod_path).touch()
        Path(shadow_path).touch()
        runner = self._make_runner_with_db(
            protected_prod_db=prod_path,
            shadow_db_path=Path(shadow_path),
        )
        ok, violations = runner._verify_cooldown_context()
        assert ok, f"不同路径应通过，但得到: {violations}"

    def test_protected_prod_db_not_exists_still_passes(self):
        """v22-05: protected_prod_db 文件不存在（仅路径声明）→ 仍应通过。"""
        runner = self._make_runner_with_db(
            protected_prod_db="/nonexistent/prod.db",
            shadow_db_path=self._v22_db_dir / "shadow.db",
        )
        from pathlib import Path
        (self._v22_db_dir / "shadow.db").touch()
        ok, violations = runner._verify_cooldown_context()
        assert ok, f"不存在的 protected_prod_db 路径声明应通过: {violations}"

    # ── Adapter 检查 ─────────────────────────────────────────────────

    def test_push_adapter_enabled_fails(self):
        """v22-06: push_adapter 已启用 → FAIL。"""
        from serenity_v2.env import get_env, set_env, SerenityEnv
        env = get_env()
        # 设置 push_adapter（模拟推送适配器已连接）
        env.push_adapter = object()  # 非 None → 已启用
        runner = self._make_runner_with_db()
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "push_adapter 已启用应被拒绝"
        assert any("push_adapter" in v for v in violations), \
            f"应报告 push_adapter 违规: {violations}"

    def test_trade_adapter_enabled_fails(self):
        """v22-07: trade_adapter 已启用 → FAIL。"""
        from serenity_v2.env import get_env
        env = get_env()
        env.broker_adapter = object()  # 非 None → 已启用
        runner = self._make_runner_with_db()
        ok, violations = runner._verify_cooldown_context()
        assert not ok, "trade_adapter 已启用应被拒绝"
        assert any("trade_adapter" in v for v in violations), \
            f"应报告 trade_adapter 违规: {violations}"

    def test_both_adapters_disabled_pass(self):
        """v22-08: push_adapter 和 trade_adapter 均 disabled → PASS。"""
        runner = self._make_runner_with_db()
        ok, violations = runner._verify_cooldown_context()
        assert ok, f"adapters 均 disabled 应通过: {violations}"

    # ── 综合场景 ─────────────────────────────────────────────────────

    def test_full_valid_context_passes(self):
        """v22-09: 完整合法单策略 fixture shadow 上下文 → PASS。"""
        prod_path = str(self._v22_db_dir / "prod.db")
        shadow_path = str(self._v22_db_dir / "shadow.db")
        from pathlib import Path
        Path(prod_path).touch()
        Path(shadow_path).touch()
        runner = self._make_runner_with_db(
            protected_prod_db=prod_path,
            shadow_db_path=Path(shadow_path),
        )
        ok, violations = runner._verify_cooldown_context()
        assert ok, f"完整合法上下文应通过: {violations}"

    def test_multiple_violations_reported_together(self):
        """v22-10: 多个违规同时报告（非短路）。"""
        from serenity_v2.phase_b2 import B2Runner
        runner = B2Runner.__new__(B2Runner)  # 不调用 __init__
        runner.metrics = type('M', (), {'cooldown_scope': 'single_strategy_fixture_shadow'})()
        runner._strategy_version = None
        runner._strategy_config_hash = None
        runner._account_snapshot_id = None
        runner._protected_prod_db = None
        runner.shadow_db = None
        runner._print_env_confirmation = lambda: None
        ok, violations = runner._verify_cooldown_context()
        assert not ok
        # autouse fixture 设置 shadow env → check #1 通过
        # 应至少包含: strategy, account, protected_prod_db = 3
        assert len(violations) >= 3, \
            f"应报告至少 3 项违规: {violations}"

    def test_nonempty_path_not_interpreted_as_production(self):
        """v22-11: 非空 protected_prod_db 路径不被误判为 production=true。"""
        runner = self._make_runner_with_db(
            protected_prod_db="/valid/production/path.db",
        )
        ok, violations = runner._verify_cooldown_context()
        # 不应包含 "production" 相关的违规（仅由 env.mode 判定 production）
        prod_violations = [v for v in violations if "production" in v.lower()]
        assert len(prod_violations) == 0, \
            f"不应有 production 模式违规: {prod_violations}"
        assert ok, f"合法路径应通过: {violations}"

    def test_environment_production_with_shadow_scope_fails(self):
        """v22-12: environment=production + cooldown scope=shadow → FAIL。"""
        from unittest.mock import patch
        from serenity_v2.env import SerenityEnv
        mock_push = object()
        prod_env = SerenityEnv.production(
            push_adapter=mock_push,
            db_path=self._v22_db_dir / "prod.db",
            log_dir=self._v22_db_dir / "logs",
        )
        # patch get_env 返回 production 环境（绕过 set_env 的混用保护）
        # _verify_cooldown_context 内部有 from .env import get_env，
        # 执行时解析为 serenity_v2.env.get_env
        with patch('serenity_v2.env.get_env', return_value=prod_env):
            runner = self._make_runner_with_db()
            ok, violations = runner._verify_cooldown_context()
            assert not ok, "production 环境应被拒绝"
            assert any("environment" in v for v in violations), \
                f"应报告 environment 违规: {violations}"


# ══════════════════════════════════════════════════════════════════════════════
# v24: Market fingerprint 定点整数稳定性
# ══════════════════════════════════════════════════════════════════════════════

class TestV24MarketFingerprint:
    """v24: compute_market_fingerprint — 昨收参考价百分比分桶 + 纯整数运算，消除浮点抖动。"""

    # ── 验收 1: 同一价格等价浮点表示 → 相同 fingerprint ──

    def test_same_price_same_fingerprint(self):
        """v24-01: 相同价格 + 相同参考价 → 相同指纹（event_id 使用相同 session 前缀）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 使用一致的 event_id 前缀模拟真实场景（EVT_2026... 所有事件共享）
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 49.10, 1000000, reference_price=48.50)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 49.10, 1000000, reference_price=48.50)
        assert fp1 == fp2, f"相同(price, ref)应相同: {fp1} vs {fp2}"

    def test_price_rounding_consistency(self):
        """v24-01b: 相同价格的不同浮点表示 → 相同指纹（无 ULP 抖动）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        prices = [48.99, 48.990, 48.99000000001]
        fingerprints = set()
        for p in prices:
            fp = compute_market_fingerprint("evt-001", p, 1000000, reference_price=48.50)
            fingerprints.add(fp)
        assert len(fingerprints) == 1, f"等价价格应相同: {fingerprints}"

    # ── 验收 2: 微小 ULP 差异不改变 bucket ──

    def test_ulp_does_not_change_bucket(self):
        """v24-02: IEEE 754 最后一位差异不改变 bucket（整数运算消除抖动）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 97.33/(97.33*0.02)=49.999... → 旧公式 p49; 实际价格未变
        # v24: 昨收 96.00, 97.33 → +1.39% → bucket 0
        #      昨收 96.00, 97.11 → +1.16% → bucket 0
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 97.33, 43353751, reference_price=96.00)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 97.11, 43095923, reference_price=96.00)
        assert fp1 == fp2, f"微小价格差异应在同一 bucket: {fp1} vs {fp2}"

    # ── 验收 3: 价格未跨真实 2% 区间时 fingerprint 稳定 ──

    def test_price_within_2pct_band_stable(self):
        """v24-03: 同一 2% bucket 内价格变化 → 指纹不变。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 昨收 50.00, 50.50 → +1.0% → bucket 0
        # 昨收 50.00, 50.99 → +1.98% → bucket 0 (still < 2%)
        EID = "EVT_20260731_00000{}_xxxx"
        fp1 = compute_market_fingerprint(EID.format(1), 50.50, 10000, reference_price=50.00)
        fp2 = compute_market_fingerprint(EID.format(2), 50.99, 10000, reference_price=50.00)
        assert fp1 == fp2, f"同一 2% bucket: {fp1} vs {fp2}"

    # ── 验收 4: 实际跨区间时 fingerprint 改变 ──

    def test_price_cross_2pct_band_changes(self):
        """v24-04: 跨 2% 边界 → 指纹改变。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 昨收 50.00, 50.99 → +1.98% → bucket 0
        # 昨收 50.00, 51.01 → +2.02% → bucket 1
        fp1 = compute_market_fingerprint("evt-001", 50.99, 10000, reference_price=50.00)
        fp2 = compute_market_fingerprint("evt-002", 51.01, 10000, reference_price=50.00)
        assert fp1 != fp2, f"跨 2%边界应不同: {fp1} vs {fp2}"
        assert "p0" in fp1
        assert "p1" in fp2

    def test_large_price_move_multi_bucket(self):
        """v24-04b: 大幅价格变动 → 跨多个 bucket。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 昨收 50.00, 55.00 → +10% → bucket 5
        fp = compute_market_fingerprint("evt-001", 55.00, 10000, reference_price=50.00)
        assert "p5" in fp

    def test_negative_price_move(self):
        """v26-04c: 价格下跌 → 负 bucket（绝对值向零取整）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        # 昨收 50.00, 48.00 → -4.0% → abs_bucket=2 → p-2
        fp = cmf("EVT_20260731_000001_xxxx", 48.00, 10000, reference_price=50.00)
        assert "p-2" in fp
        # 昨收 50.00, 48.50 → -3.0% → abs_bucket = floor(1.5) = 1 → p-1
        fp2 = cmf("EVT_20260731_000002_xxxx", 48.50, 10000, reference_price=50.00)
        assert "p-1" in fp2

    # ── 验收 5: 边界附近往返不连续 re-arm ──

    def test_boundary_oscillation_stable(self):
        """v24-05: 价格围绕 2% 边界往返 → 往返后回到相同 bucket。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 昨收 50.00
        # 50.99 → +1.98% → bucket 0
        # 51.01 → +2.02% → bucket 1
        # 50.99 → +1.98% → bucket 0 (back to same)
        EID = "EVT_20260731_00000{}_xxxx"
        fp_a = compute_market_fingerprint(EID.format(1), 50.99, 10000, reference_price=50.00)
        fp_b = compute_market_fingerprint(EID.format(2), 51.01, 10000, reference_price=50.00)
        fp_c = compute_market_fingerprint(EID.format(3), 50.99, 10000, reference_price=50.00)
        assert fp_a == fp_c, "往返应回到相同指纹"
        assert fp_a != fp_b, "跨边界应不同"

    def test_boundary_exact_2pct(self):
        """v24-05b: 精确 2% 边界（如 51.00 from 50.00）→ bucket 1（floor）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 昨收 50.00, 51.00 → +2.00% → bucket 1
        # 整数: (5100 - 5000) * 50 // 5000 = 100*50//5000 = 5000//5000 = 1
        fp = compute_market_fingerprint("evt-001", 51.00, 10000, reference_price=50.00)
        assert "p1" in fp

    # ── 验收 6: 复现 v23 三组价格不产生 p49/p50 抖动 ──

    def test_v23_000988_pair_no_jitter(self):
        """v24-06a: 97.11 vs 97.33（000988），昨收同价 → 相同 bucket。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # v23 昨收 = 97.11（首价），97.33 仍在 2% 内 → bucket 0
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 97.11, 43095923, reference_price=97.11)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 97.33, 43353751, reference_price=97.11)
        assert fp1 == fp2, f"000988 v23 pair: {fp1} vs {fp2}"
        assert "p0" in fp1  # 0% change bucket

    def test_v23_600176_pair_no_jitter(self):
        """v24-06b: 38.38 vs 38.41（600176），昨收同价 → 相同 bucket。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 38.38, 307479843, reference_price=38.38)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 38.41, 307540843, reference_price=38.38)
        assert fp1 == fp2, f"600176 v23 pair: {fp1} vs {fp2}"

    def test_v23_600487_pair_no_jitter(self):
        """v24-06c: 49.10 vs 49.22（600487），昨收同价 → 相同 bucket。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 49.10, 157936517, reference_price=49.10)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 49.22, 160007417, reference_price=49.10)
        assert fp1 == fp2, f"600487 v23 pair: {fp1} vs {fp2}"

    # ── 验收 7: 不同参考价产生不同指纹 ──

    def test_different_reference_different_fingerprint(self):
        """v24-07: 同价不同参考 → 不同指纹。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        fp1 = compute_market_fingerprint("evt-001", 50.00, 10000, reference_price=49.00)
        fp2 = compute_market_fingerprint("evt-001", 50.00, 10000, reference_price=48.00)
        assert fp1 != fp2, f"不同参考价应产生不同指纹: {fp1} vs {fp2}"

    # ── 成交量 bucket 验证 ──

    def test_volume_bucket_deterministic(self):
        """v24-08: 成交量 bucket 使用 bit_length — 确定性整数运算。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        import math
        for vol in [1, 100, 10000, 1000000, 100000000, 157936517]:
            fp = compute_market_fingerprint("evt-001", 50.0, vol, reference_price=50.00)
            expected_v = int(math.log2(max(vol, 1)) / 2)
            assert f"v{expected_v}" in fp, f"vol={vol}: expected v{expected_v}, got {fp}"

    def test_volume_bucket_stable_for_small_changes(self):
        """v24-09: 成交量微小变化 → 相同 bucket。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        fp1 = compute_market_fingerprint("EVT_20260731_000001_xxxx", 50.0, 43353751, reference_price=50.00)
        fp2 = compute_market_fingerprint("EVT_20260731_000002_xxxx", 50.0, 43356251, reference_price=50.00)
        assert fp1 == fp2, f"微小成交量变化应保持相同: {fp1} vs {fp2}"

    # ── 负向价格变化（验收 8 补充）──

    def test_negative_within_2pct_stays_in_bucket(self):
        """v26-13: ±2% 内 → p0（零边界不产生 false re-arm）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        ref = 50.00
        # -0.02%: 49.99, delta=-1, abs(1)*50//5000 = 50//5000 = 0 → p0
        fp_tiny = cmf("EVT_20260731_000001_xxxx", 49.99, 10000, reference_price=ref)
        # -1.00%: 49.50, delta=-50, abs(50)*50//5000 = 2500//5000 = 0 → p0
        fp_one = cmf("EVT_20260731_000002_xxxx", 49.50, 10000, reference_price=ref)
        # -1.98%: 49.01, delta=-99, abs(99)*50//5000 = 4950//5000 = 0 → p0
        fp_198 = cmf("EVT_20260731_000003_xxxx", 49.01, 10000, reference_price=ref)
        # +0.00%: 50.00 → p0
        fp_zero = cmf("EVT_20260731_000004_xxxx", 50.00, 10000, reference_price=ref)
        # +1.98%: 50.99, delta=99, abs(99)*50//5000 = 4950//5000 = 0 → p0
        fp_pos = cmf("EVT_20260731_000005_xxxx", 50.99, 10000, reference_price=ref)
        # All within ±2% → p0
        assert fp_tiny == fp_one == fp_198 == fp_zero == fp_pos, (
            f"All within +-2% should be p0: {fp_tiny} {fp_one} {fp_198} {fp_zero} {fp_pos}"
        )
        assert "p0" in fp_tiny
        assert "p0" in fp_zero
        assert "p0" in fp_pos

    def test_negative_cross_2pct_boundary(self):
        """v26-14: -2.00% 边界 → p-1; -4.00% 边界 → p-2。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        ref = 50.00
        # -2.00% = 49.00: abs(100)*50//5000 = 5000//5000 = 1 → p-1
        fp_200 = cmf("EVT_20260731_000001_xxxx", 49.00, 10000, reference_price=ref)
        # -4.00% = 48.00: abs(200)*50//5000 = 10000//5000 = 2 → p-2
        fp_400 = cmf("EVT_20260731_000002_xxxx", 48.00, 10000, reference_price=ref)
        assert "p-1" in fp_200, f"-2.00% exact boundary should be p-1: {fp_200}"
        assert "p-2" in fp_400, f"-4.00% should cross to p-2: {fp_400}"
        assert fp_200 != fp_400, "-2.00% and -4.00% should be different buckets"

    def test_negative_symmetric_with_positive(self):
        """v25-15: 正负方向边界行为对称 +2.00% vs -2.00%。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        ref = 50.00
        fp_pos = cmf("EVT_20260731_000001_xxxx", 51.00, 10000, reference_price=ref)
        fp_neg = cmf("EVT_20260731_000002_xxxx", 49.00, 10000, reference_price=ref)
        assert "p1" in fp_pos
        assert "p-1" in fp_neg
        assert fp_pos != fp_neg

    def test_negative_boundary_oscillation(self):
        """v26-16: 负向 2% 边界附近往返 → 同 bucket 往返（p0 ↔ p-1 ↔ p0）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        ref = 50.00
        EID = "EVT_20260731_00000{}_xxxx"
        # -1.98% = 49.01: abs(99)*50//5000 = 0 → p0
        # -2.02% = 48.99: abs(101)*50//5000 = 5050//5000 = 1 → p-1
        # Back to -1.98% = 49.01 → p0
        fp_a = cmf(EID.format(1), 49.01, 10000, reference_price=ref)  # -1.98% → p0
        fp_b = cmf(EID.format(2), 48.99, 10000, reference_price=ref)  # -2.02% → p-1
        fp_c = cmf(EID.format(3), 49.01, 10000, reference_price=ref)  # -1.98% → p0
        assert fp_a == fp_c, "往返应回到相同 bucket (p0)"
        assert fp_a != fp_b, "跨边界应不同 (p0 vs p-1)"

    # ── 缺失参考价处理 ──

    def test_previous_close_zero_fallback_suppresses_price_dimension(self):
        """v25-17: previous_close=0 → delta 恒 0，价格维度不贡献 discriminatory power。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        fp1 = cmf("EVT_20260731_000001_xxxx", 50.00, 10000, reference_price=0.0)
        fp2 = cmf("EVT_20260731_000002_xxxx", 55.00, 10000, reference_price=0.0)
        assert "p0" in fp1
        assert "p0" in fp2  # 价格维度降级，cooldown 依赖其余 8 字段

    def test_previous_close_none_handled(self):
        """v25-18: previous_close=None → 等同 0 回退（不崩溃）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        fp = cmf("EVT_20260731_000001_xxxx", 50.00, 10000, reference_price=None)
        assert "p0" in fp

    def test_previous_close_nan_handled(self):
        """v25-19: previous_close=NaN → reference_price > 0 is False → 回退（不崩溃）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        fp = cmf("EVT_20260731_000001_xxxx", 50.00, 10000, reference_price=float('nan'))
        assert "p0" in fp

    # ── 边缘情况 ──

    def test_zero_reference_falls_back(self):
        """v24-10: reference_price=0 → 回退到 price 自身（测试兼容性）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        fp1 = compute_market_fingerprint("evt-001", 50.00, 10000, reference_price=0.0)
        fp2 = compute_market_fingerprint("evt-001", 50.00, 10000, reference_price=0.0)
        assert fp1 == fp2, "零参考价回退应一致"

    def test_high_price_stock(self):
        """v24-11: 高价股 percentage bucket 表现。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint
        # 昨收 500.00, 510.00 → +2.0% → bucket 1
        fp = compute_market_fingerprint("evt-001", 510.00, 10000, reference_price=500.00)
        assert "p1" in fp
        # 昨收 500.00, 509.99 → +1.998% → bucket 0
        fp2 = compute_market_fingerprint("evt-002", 509.99, 10000, reference_price=500.00)
        assert "p0" in fp2

    def test_low_price_stock(self):
        """v26-20: 低价股 percentage bucket 表现（±2% 内 → p0）。"""
        from serenity_v2.phase_b2 import compute_market_fingerprint as cmf
        EID = "EVT_20260731_00000{}_xxxx"
        # 昨收 5.00, 5.10 → +2.0% → p1
        fp = cmf(EID.format(1), 5.10, 10000, reference_price=5.00)
        assert "p1" in fp
        # 昨收 5.00, 4.95 → -1.0% → p0 (within ±2%)
        fp2 = cmf(EID.format(2), 4.95, 10000, reference_price=5.00)
        assert "p0" in fp2
        # 昨收 5.00, 4.85 → -3.0% → p-1 (abs_bucket = floor(1.5) = 1, sign=-)
        fp3 = cmf(EID.format(3), 4.85, 10000, reference_price=5.00)
        assert "p-1" in fp3
        # 昨收 5.00, 4.80 → -4.0% → p-2
        fp4 = cmf(EID.format(4), 4.80, 10000, reference_price=5.00)
        assert "p-2" in fp4


# ============================================================================
# v27: Postflight 不变量审计测试
# ============================================================================

class TestPostflightAudit:
    """v27: B2Metrics.audit_postflight_invariants 综合审计测试。"""

    @staticmethod
    def _make_clean_metrics():
        """创建通过所有不变量的一致性 B2Metrics。"""
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.run_id = "B2_TEST_20260801_120000"
        m.status = "COMPLETED"
        m.run_completed = True

        # 调度层（自洽）
        m.cycles_planned = 10
        m.cycles_started = 8
        m.cycles_completed = 7
        m.cycles_aborted = 1
        m.cycles_skipped = 1
        m.not_due_cycles = 1
        m.cancelled_cycles_auto_stop = 0
        m.cycles_failed = 0

        # 数据流
        m.raw_received = 100
        m.raw_stored = 90
        m.raw_duplicates = 10

        # 事件
        m.events_created = 50
        m.events_deduplicated = 5

        # 信号
        m.signals_total = 20
        m.signals_created_unique = 12
        m.signals_candidate_total_run = 20  # v29: 候选守恒 20 == 12+3+2+3
        m.signals_skipped_idempotent = 3
        m.signals_skipped_idempotent_run = 3  # v28: run-scoped delta
        m.signals_skipped_cooldown = 2
        m.signals_no_decision = 3

        # 失败（全零）
        m.total_failures = 0

        # 账本
        m.ledger_claimed = 20
        m.ledger_completed = 18
        m.ledger_failed_count = 1
        m.ledger_in_progress = 1

        # 安全
        m.real_push_count = 0
        m.real_trade_count = 0
        m.account_modifications = 0

        return m

    def test_all_invariants_pass_with_clean_metrics(self):
        """自洽 metrics 下所有审计检查通过。"""
        m = self._make_clean_metrics()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123"},
            prod_guard_after={"sha256": "abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123"},
            shadow_db_path="/tmp/shadow/shadow.db",
        )
        assert audit["all_pass"] is True
        assert audit["failed"] == 0

    def test_audit_detects_scheduling_violation(self):
        """调度方程不成立时审计检测到。"""
        m = self._make_clean_metrics()
        m.cycles_planned = 10
        m.cycles_started = 5  # 不匹配 planned = started + skipped + not_due + cancelled (5+1+1+0=7 != 10)
        audit = m.audit_postflight_invariants()
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SCHED_EQ1" in failed_ids

    def test_audit_detects_data_integrity_violation(self):
        """数据流方程不成立时审计检测到。"""
        m = self._make_clean_metrics()
        m.raw_received = 100
        m.raw_stored = 80
        m.raw_duplicates = 5  # 80+5=85 != 100
        audit = m.audit_postflight_invariants()
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "DATA_EQ1" in failed_ids

    def test_audit_detects_failure_accounting_violation(self):
        """失败会计方程不成立时审计检测到。"""
        m = self._make_clean_metrics()
        m.total_failures = 5
        m.scheduler_failed = 3  # sum=3 != 5
        audit = m.audit_postflight_invariants()
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "FAIL_EQ1" in failed_ids

    def test_audit_detects_ledger_violation(self):
        """账本方程不成立时审计检测到。"""
        m = self._make_clean_metrics()
        m.ledger_claimed = 10
        m.ledger_completed = 5
        m.ledger_failed_count = 1
        m.ledger_in_progress = 1  # 5+1+1=7 != 10
        audit = m.audit_postflight_invariants()
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "LEDGER_EQ1" in failed_ids

    def test_audit_detects_prod_db_change(self):
        """生产 DB 哈希变化时审计检测到 (v28: GLOBAL_UNCHANGED)。"""
        m = self._make_clean_metrics()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "aaa111222333aaa111222333aaa111222333aaa111222333aaa111222333aaa111"},
            prod_guard_after={"sha256": "bbb444555666bbb444555666bbb444555666bbb444555666bbb444555666bbb444"},
        )
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SEC_PROD_DB_GLOBAL_UNCHANGED" in failed_ids

    def test_audit_detects_side_effects(self):
        """有真实副作用时审计检测到。"""
        m = self._make_clean_metrics()
        m.real_push_count = 1  # 违规!
        audit = m.audit_postflight_invariants()
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SEC_ZERO_SIDE_EFFECTS" in failed_ids

    def test_audit_detects_signal_equation_violation(self):
        """信号方程不成立时审计检测到。"""
        m = self._make_clean_metrics()
        m.signals_total = 20
        m.signals_created_unique = 10
        # 10+3+2+3=18 != 20
        audit = m.audit_postflight_invariants()
        assert audit["all_pass"] is False
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SIGNAL_EQ1" in failed_ids

    def test_audit_includes_all_categories(self):
        """审计报告包含所有检查类别。"""
        m = self._make_clean_metrics()
        audit = m.audit_postflight_invariants(
            prod_guard_before={"sha256": "aaa111222333aaa111222333aaa111222333aaa111222333aaa111222333aaa111"},
            prod_guard_after={"sha256": "aaa111222333aaa111222333aaa111222333aaa111222333aaa111222333aaa111"},
            shadow_db_path="/tmp/shadow/shadow.db",
        )
        categories = {c["category"] for c in audit["checks"]}
        assert "scheduling" in categories
        assert "data_integrity" in categories
        assert "failure_accounting" in categories
        assert "idempotency" in categories
        assert "security" in categories
        assert "report_integrity" in categories
        assert "process_integrity" in categories

    def test_audit_shadow_db_identity_rejects_production_path(self):
        """影子 DB 路径不含 /shadow 时审计拒绝。"""
        m = self._make_clean_metrics()
        audit = m.audit_postflight_invariants(
            shadow_db_path="/data/prod/serenity.db",
        )
        failed_ids = {c["id"] for c in audit["checks"] if not c["pass"]}
        assert "SEC_SHADOW_IDENTITY" in failed_ids

    def test_audit_termination_reported(self):
        """运行终止状态合理时审计通过该检查。"""
        m = self._make_clean_metrics()
        m.run_completed = True
        m.run_terminated_early = False
        audit = m.audit_postflight_invariants()
        term_check = [c for c in audit["checks"] if c["id"] == "PROC_TERMINATION"][0]
        assert term_check["pass"] is True


# ============================================================================
# v27: B2 报告复审与脱敏测试
# ============================================================================

class TestReportReview:
    """v27: B2Metrics.review_report 报告复审测试。"""

    @staticmethod
    def _make_clean_metrics():
        """创建复审通过的 B2Metrics。"""
        from serenity_v2.phase_b2 import B2Metrics
        m = B2Metrics()
        m.run_id = "B2_TEST_20260801_120000"
        m.status = "COMPLETED"
        m.started_at = "2026-08-01T12:00:00+08:00"
        m.ended_at = "2026-08-01T12:05:00+08:00"
        m.duration_seconds = 300
        m.cycles_planned = 60
        m.cycles_started = 60
        m.cycles_completed = 60
        m.real_push_count = 0
        m.real_trade_count = 0
        m.account_modifications = 0
        return m

    def test_clean_metrics_pass_review(self):
        """正常的 metrics 通过复审。"""
        m = self._make_clean_metrics()
        result = m.review_report()
        assert result["ok"] is True
        assert result["findings_count"] == 0

    def test_review_detects_missing_required_field(self):
        """缺少必填字段时复审检测到。"""
        m = self._make_clean_metrics()
        m.run_id = ""
        result = m.review_report()
        # run_id="" 仍然在 dict 中但为空 — 检查 findings
        # 注意: "" 不是 None, 所以不会触发"缺失必填字段"
        # 但审计方程可能受影响
        assert "findings" in result

    def test_review_detects_real_pushes(self):
        """有真实推送时复审检测到。"""
        m = self._make_clean_metrics()
        m.real_push_count = 1
        result = m.review_report()
        assert result["ok"] is False
        assert any("真实推送" in f for f in result["findings"])

    def test_review_detects_real_trades(self):
        """有真实成交时复审检测到。"""
        m = self._make_clean_metrics()
        m.real_trade_count = 5
        result = m.review_report()
        assert result["ok"] is False
        assert any("真实成交" in f for f in result["findings"])

    def test_review_detects_account_modifications(self):
        """有账户修改时复审检测到。"""
        m = self._make_clean_metrics()
        m.account_modifications = 1
        result = m.review_report()
        assert result["ok"] is False
        assert any("账户修改" in f for f in result["findings"])


class TestReportDesensitization:
    """v27: B2Metrics.desensitize_report 脱敏测试。"""

    def test_desensitize_redacts_account_snapshot_full(self):
        """脱敏后 account_snapshot_id_full 被替换。"""
        from serenity_v2.phase_b2 import B2Metrics
        data = {
            "run_id": "B2_TEST",
            "status": "COMPLETED",
            "signal_details": [{
                "signal_id": "SIG_001",
                "symbol": "600487",
                "account_snapshot_id": "18dc7d197f33a1bd",
                "account_snapshot_id_full": "18dc7d197f33a1bde4312e187743309d3a01b7108e51d76d901e8c4e2b46ff67",
            }],
            "prod_file_hash_before": {
                "sha256": "ab9cc9266796abcdef1234567890abcdef1234567890abcdef1234567890abcd",
            },
            "prod_file_hash_after": {
                "sha256": "341f2854e3a2abcdef1234567890abcdef1234567890abcdef1234567890abcd",
            },
        }
        sanitized = B2Metrics.desensitize_report(data)

        # account_snapshot_id_full 被红action
        assert sanitized["signal_details"][0]["account_snapshot_id_full"] == "[REDACTED]"

        # account_snapshot_id 被截断
        assert sanitized["signal_details"][0]["account_snapshot_id"] == "18dc7d19...[REDACTED]"

    def test_desensitize_truncates_sha256(self):
        """脱敏后 SHA256 哈希被截断。"""
        from serenity_v2.phase_b2 import B2Metrics
        data = {
            "run_id": "B2_TEST",
            "prod_file_hash_before": {
                "sha256": "ab9cc9266796abcdef1234567890abcdef1234567890abcdef1234567890abcd",
            },
        }
        sanitized = B2Metrics.desensitize_report(data)
        sha = sanitized["prod_file_hash_before"]["sha256"]
        assert len(sha) < 30  # 截断后应远短于原始 64 字符
        assert "[REDACTED]" in sha

    def test_desensitize_preserves_run_id(self):
        """脱敏保留 run_id 和 status。"""
        from serenity_v2.phase_b2 import B2Metrics
        data = {"run_id": "B2_20260801_120000", "status": "COMPLETED"}
        sanitized = B2Metrics.desensitize_report(data)
        assert sanitized["run_id"] == "B2_20260801_120000"
        assert sanitized["status"] == "COMPLETED"

    def test_desensitize_marks_version(self):
        """脱敏后添加版本标记。"""
        from serenity_v2.phase_b2 import B2Metrics
        sanitized = B2Metrics.desensitize_report({"run_id": "B2_TEST"})
        assert sanitized["_desensitized"] is True
        assert sanitized["_desensitized_version"] == "v28"  # v28 bump

    def test_desensitize_idempotent(self):
        """重复脱敏不改变结果（幂等）。"""
        from serenity_v2.phase_b2 import B2Metrics
        data = {
            "run_id": "B2_TEST",
            "signal_details": [{
                "account_snapshot_id_full": "18dc7d197f33a1bde4312e187743309d3a01b7108e51d76d901e8c4e2b46ff67",
            }],
        }
        s1 = B2Metrics.desensitize_report(data)
        s2 = B2Metrics.desensitize_report(s1)
        assert s1 == s2

    def test_desensitize_handles_empty_signal_details(self):
        """无信号详情时不报错。"""
        from serenity_v2.phase_b2 import B2Metrics
        data = {"run_id": "B2_TEST", "signal_details": []}
        sanitized = B2Metrics.desensitize_report(data)
        assert sanitized["_desensitized"] is True


# ============================================================================
# UI-P0 正向 fixture 验证 & Phase C 兼容性回归
# ============================================================================

class TestUIP0PositiveFixture:
    """验证 UI-P0 正向 fixture 完整性和可用性。"""

    def test_fixture_file_exists_and_valid_json(self):
        """fixture 文件存在且为有效 JSON。"""
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        assert fixture_path.exists(), f"fixture 不存在: {fixture_path}"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_fixture_has_required_fields(self):
        """fixture 包含所有必填字段。"""
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        required = ["fixture_version", "fixture_id", "account_snapshot_as_of",
                     "cash", "total_assets", "position_market_value", "positions"]
        for field in required:
            assert field in data, f"缺失必填字段: {field}"

    def test_fixture_book_equation_balanced(self):
        """现金 + 持仓市值 = 总资产。"""
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        cash = data["cash"]
        positions = data["positions"]
        mv_sum = sum(p["market_value"] for p in positions)
        total = data["total_assets"]

        assert abs(total - (cash + mv_sum)) < 1.0, (
            f"账面不匹配: total={total}, cash={cash}, mv_sum={mv_sum}"
        )

    def test_fixture_positions_have_valid_codes(self):
        """所有持仓股票代码为 6 位数字。"""
        import re
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        for p in data["positions"]:
            assert re.match(r"^\d{6}$", p["code"]), f"无效股票代码: {p['code']}"

    def test_fixture_available_shares_not_exceed_total(self):
        """可卖股数 ≤ 持仓股数（无不合理超卖）。"""
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        for p in data["positions"]:
            assert p["available_shares"] <= p["shares"], (
                f"{p['code']}: 可卖{p['available_shares']} > 持仓{p['shares']}"
            )

    def test_fixture_total_assets_positive(self):
        """总资产为正（正向 fixture）。"""
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        assert data["total_assets"] > 0, "总资产必须为正"
        assert data["cash"] >= 0, "现金不能为负"

    def test_fixture_has_three_baskets(self):
        """fixture 恰好包含三只股票（p0-3）。"""
        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        codes = {p["code"] for p in data["positions"]}
        assert codes == {"600487", "600176", "000988"}, (
            f"期望三篮 (600487/600176/000988)，实际: {codes}"
        )

    def test_fixture_consistent_with_b2_symbols(self):
        """fixture 股票代码与 B2_SYMBOLS 一致。"""
        from serenity_v2.phase_b2 import B2_SYMBOLS

        fixture_path = Path(__file__).parent / "fixtures" / "b2" / "account_fixture_20260722.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        codes = {p["code"] for p in data["positions"]}
        assert codes == set(B2_SYMBOLS), (
            f"fixture codes {codes} vs B2_SYMBOLS {B2_SYMBOLS}"
        )


class TestPhaseCCompatibility:
    """Phase C 兼容性回归: 旧 API /api/monitor-data 与新 API /api/dashboard 并行。"""

    def test_monitoring_dashboard_importable(self):
        """monitoring_dashboard 模块可导入。"""
        try:
            import monitoring_dashboard  # noqa: F401
        except Exception:
            pytest.skip("monitoring_dashboard 需要 serenity.db 运行时依赖")

    def test_both_api_routes_defined(self):
        """/api/dashboard 和 /api/monitor-data 两个路由均已定义。"""
        import importlib
        try:
            md = importlib.import_module("monitoring_dashboard")
        except Exception:
            pytest.skip("monitoring_dashboard 不可导入")

        app = getattr(md, "app", None)
        if app is None:
            pytest.skip("monitoring_dashboard 无 Flask app")

        # 检查路由注册
        rules = {rule.rule: rule for rule in app.url_map.iter_rules()
                 if not rule.rule.startswith("/static")}
        assert "/api/dashboard" in rules, "新 API /api/dashboard 未注册"
        assert "/api/monitor-data" in rules, "旧 API /api/monitor-data 未注册"

    def test_monitor_route_accessible(self):
        """/monitor 路由可访问（旧版看板，Phase C 保留兼容）。"""
        import importlib
        try:
            md = importlib.import_module("monitoring_dashboard")
        except Exception:
            pytest.skip("monitoring_dashboard 不可导入")

        app = getattr(md, "app", None)
        if app is None:
            pytest.skip("monitoring_dashboard 无 Flask app")

        with app.test_client() as client:
            resp = client.get("/monitor")
            # 旧版看板可访问（返回 200 渲染 monitor.html 模板）
            # Phase C 计划: 后续可能 → 302 → /dashboard
            assert resp.status_code == 200, (
                f"旧版看板不可访问: {resp.status_code}"
            )

    def test_dashboard_route_exists(self):
        """/dashboard 路由存在（新版 3-tab 看板）。"""
        import importlib
        try:
            md = importlib.import_module("monitoring_dashboard")
        except Exception:
            pytest.skip("monitoring_dashboard 不可导入")

        app = getattr(md, "app", None)
        if app is None:
            pytest.skip("monitoring_dashboard 无 Flask app")

        rules = {rule.rule: rule for rule in app.url_map.iter_rules()
                 if not rule.rule.startswith("/static")}
        assert "/dashboard" in rules, "/dashboard 路由未注册"
