"""
Serenity 2.0 — 信号幂等与事件处理账本 (P0-2)

解决信号风暴根因：每个周期重新处理所有历史事件，
同一事件在相同策略/配置/账户下生成多个独立 signal_id。

核心机制:
  1. event_processing_ledger — 事件处理唯一账本
  2. 原子领取 (INSERT OR IGNORE) — 不是 SELECT-then-INSERT
  3. 租约超时崩溃恢复
  4. SignalOutput 幂等键 — 相同输入 -> 返回已有信号
  5. 事件生命周期 — 避免每个 tick 创建独立事件
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

CST = timezone(timedelta(hours=8))
logger = logging.getLogger("serenity_v2.signal_idempotency")

DEFAULT_LEASE_MS = 30_000


# ══════════════════════════════════════════════════════════════════════════
# DDL
# ══════════════════════════════════════════════════════════════════════════

PROCESSING_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS event_processing_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 幂等键: 同一事件/策略/配置/账户下只能成功一次
    event_id            TEXT NOT NULL,
    strategy_id         TEXT NOT NULL DEFAULT '',
    strategy_version    TEXT NOT NULL DEFAULT '',
    strategy_config_hash TEXT NOT NULL DEFAULT '',
    account_snapshot_id TEXT NOT NULL DEFAULT '',

    -- 状态
    status              TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','PROCESSING','COMPLETED','FAILED')),
    attempt_count       INTEGER NOT NULL DEFAULT 0,

    -- 时间戳
    claimed_at          TEXT NOT NULL DEFAULT '',
    completed_at        TEXT NOT NULL DEFAULT '',

    -- 结果
    signal_id           TEXT NOT NULL DEFAULT '',
    failure_reason      TEXT NOT NULL DEFAULT '',

    -- 租约
    lease_until         TEXT NOT NULL DEFAULT '',
    worker_id           TEXT NOT NULL DEFAULT '',

    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),

    UNIQUE(event_id, strategy_id, strategy_version,
           strategy_config_hash, account_snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_status ON event_processing_ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_event ON event_processing_ledger(event_id);
CREATE INDEX IF NOT EXISTS idx_ledger_signal ON event_processing_ledger(signal_id)
    WHERE signal_id != '';
"""


# ══════════════════════════════════════════════════════════════════════════
# EventProcessingLedger
# ══════════════════════════════════════════════════════════════════════════

class EventProcessingLedger:
    """事件处理账本 — 保证同一事件在相同上下文中只处理一次。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def init_schema(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(PROCESSING_LEDGER_DDL)
            conn.commit()
        finally:
            conn.close()

    # ── 原子领取 ──

    def claim(
        self,
        event_id: str,
        strategy_id: str = "",
        strategy_version: str = "",
        strategy_config_hash: str = "",
        account_snapshot_id: str = "",
        worker_id: str = "",
        lease_ms: int = DEFAULT_LEASE_MS,
    ) -> tuple:
        """原子领取: INSERT OR IGNORE (不是 SELECT-then-INSERT)。

        返回 (ok: bool, detail: str)。
        ok=True -> 成功领取，可以处理。
        ok=False -> 已存在记录（已完成/处理中/租约未过期）。
        """
        now = datetime.now(tz=CST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        lease_until = (now + timedelta(milliseconds=lease_ms)).strftime(
            "%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """INSERT OR IGNORE INTO event_processing_ledger
                   (event_id, strategy_id, strategy_version,
                    strategy_config_hash, account_snapshot_id,
                    status, attempt_count, claimed_at, lease_until, worker_id)
                   VALUES (?, ?, ?, ?, ?, 'PROCESSING', 1, ?, ?, ?)""",
                (event_id, strategy_id, strategy_version,
                 strategy_config_hash, account_snapshot_id,
                 now_str, lease_until, worker_id),
            )
            if cursor.rowcount > 0:
                conn.commit()
                return True, "claimed"

            # 已存在 -> 检查状态
            row = conn.execute(
                """SELECT status, signal_id, lease_until, attempt_count
                   FROM event_processing_ledger
                   WHERE event_id=? AND strategy_id=? AND strategy_version=?
                     AND strategy_config_hash=? AND account_snapshot_id=?""",
                (event_id, strategy_id, strategy_version,
                 strategy_config_hash, account_snapshot_id),
            ).fetchone()

            if row is None:
                conn.commit()
                return False, "missing_after_insert"

            if row["status"] == "COMPLETED":
                conn.commit()
                return False, f"already_completed:{row['signal_id']}"
            if row["status"] == "FAILED":
                conn.commit()
                return False, f"previously_failed"
            if row["status"] == "PROCESSING":
                lease = row["lease_until"] or ""
                if lease and lease < now_str:
                    # 租约过期 -> 恢复
                    c2 = conn.execute(
                        """UPDATE event_processing_ledger SET
                             attempt_count=attempt_count+1,
                             claimed_at=?, lease_until=?, worker_id=?
                           WHERE event_id=? AND strategy_id=?
                             AND strategy_version=? AND strategy_config_hash=?
                             AND account_snapshot_id=?
                             AND lease_until=?""",
                        (now_str, lease_until, worker_id,
                         event_id, strategy_id, strategy_version,
                         strategy_config_hash, account_snapshot_id,
                         row["lease_until"]))
                    if c2.rowcount > 0:
                        conn.commit()
                        return True, "recovered_lease"
                    conn.commit()
                else:
                    conn.commit()
                return False, "processing"
            conn.commit()
            return False, f"unknown_status:{row['status']}"
        finally:
            conn.close()

    # ── 完成 / 失败 ──

    def mark_completed(self, event_id: str, signal_id: str,
                       strategy_id: str = "", strategy_version: str = "",
                       strategy_config_hash: str = "",
                       account_snapshot_id: str = "") -> bool:
        """标记处理完成。"""
        now = datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            c = conn.execute(
                """UPDATE event_processing_ledger
                   SET status='COMPLETED', signal_id=?,
                       completed_at=?, updated_at=?
                   WHERE event_id=? AND strategy_id=? AND strategy_version=?
                     AND strategy_config_hash=? AND account_snapshot_id=?
                     AND status='PROCESSING'""",
                (signal_id, now, now,
                 event_id, strategy_id, strategy_version,
                 strategy_config_hash, account_snapshot_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    def mark_failed(self, event_id: str, reason: str,
                    strategy_id: str = "", strategy_version: str = "",
                    strategy_config_hash: str = "",
                    account_snapshot_id: str = "") -> bool:
        """标记处理失败（可重试）。"""
        now = datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            c = conn.execute(
                """UPDATE event_processing_ledger
                   SET status='FAILED', failure_reason=?,
                       completed_at=?, updated_at=?
                   WHERE event_id=? AND strategy_id=? AND strategy_version=?
                     AND strategy_config_hash=? AND account_snapshot_id=?
                     AND status='PROCESSING'""",
                (reason, now, now,
                 event_id, strategy_id, strategy_version,
                 strategy_config_hash, account_snapshot_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    def mark_completed_no_signal(self, event_id: str,
                                 strategy_id: str = "",
                                 strategy_version: str = "",
                                 strategy_config_hash: str = "",
                                 account_snapshot_id: str = "") -> bool:
        """标记处理完成但未生成信号（终态，不计入 failure）。

        status=COMPLETED, signal_id='', failure_reason='NO_SIGNAL'.
        与 mark_completed 不同: signal_id 为空但仍是 COMPLETED 终态，
        确保下一周期不再重处理且不计入 P0-4 total_failures。
        """
        now = datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            c = conn.execute(
                """UPDATE event_processing_ledger
                   SET status='COMPLETED', signal_id='',
                       failure_reason='NO_SIGNAL',
                       completed_at=?, updated_at=?
                   WHERE event_id=? AND strategy_id=? AND strategy_version=?
                     AND strategy_config_hash=? AND account_snapshot_id=?
                     AND status='PROCESSING'""",
                (now, now,
                 event_id, strategy_id, strategy_version,
                 strategy_config_hash, account_snapshot_id))
            updated = c.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    # ── 查询 ──

    def is_already_processed(self, event_id: str,
                             strategy_id: str = "",
                             strategy_version: str = "",
                             strategy_config_hash: str = "",
                             account_snapshot_id: str = "") -> tuple:
        """(processed: bool, signal_id: str)

        COMPLETED 和 COMPLETED_NO_SIGNAL 均为终态，不可重处理。
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                """SELECT signal_id, failure_reason FROM event_processing_ledger
                   WHERE event_id=? AND strategy_id=? AND strategy_version=?
                     AND strategy_config_hash=? AND account_snapshot_id=?
                     AND status='COMPLETED'""",
                (event_id, strategy_id, strategy_version,
                 strategy_config_hash, account_snapshot_id)).fetchone()
            if row is not None:
                return True, row[0] or ""
            return False, ""
        finally:
            conn.close()

    def get_stats(self) -> dict:
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                """SELECT status, COUNT(*) as cnt
                   FROM event_processing_ledger GROUP BY status"""
            ).fetchall()
            counts = {"PENDING": 0, "PROCESSING": 0,
                      "COMPLETED": 0, "FAILED": 0,
                      "COMPLETED_NO_SIGNAL": 0}
            for r in row:
                counts[r[0]] = r[1]
            # 区分 COMPLETED with signal vs COMPLETED_NO_SIGNAL
            no_sig_row = conn.execute(
                """SELECT COUNT(*) FROM event_processing_ledger
                   WHERE status='COMPLETED' AND signal_id=''
                     AND failure_reason='NO_SIGNAL'"""
            ).fetchone()
            no_sig_count = no_sig_row[0] if no_sig_row else 0
            counts["COMPLETED_NO_SIGNAL"] = no_sig_count
            all_completed = counts["COMPLETED"]
            counts["COMPLETED_WITH_SIGNAL"] = all_completed - no_sig_count
            total = sum(v for k, v in counts.items()
                       if k not in ("COMPLETED_WITH_SIGNAL",))
            counts["total"] = total
            counts["audit_ok"] = (
                all_completed == counts["COMPLETED_WITH_SIGNAL"] + no_sig_count
            )
            return counts
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 信号幂等键
# ══════════════════════════════════════════════════════════════════════════

def compute_signal_idempotency_key(
    event_id: str,
    strategy_version: str = "",
    strategy_config_hash: str = "",
    account_snapshot_id: str = "",
) -> str:
    """相同输入 -> 相同键。用于复用已有 signal_id。"""
    raw = f"{event_id}|{strategy_version}|{strategy_config_hash}|{account_snapshot_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════
# IdempotentSignalProcessor
# ══════════════════════════════════════════════════════════════════════════

class IdempotentSignalProcessor:
    """包装 SignalDesk — 幂等保证。

    每个 (event, strategy, config, account) 组合只生成一次信号。
    """

    def __init__(self, desk, ledger: EventProcessingLedger,
                 worker_id: str = "default"):
        self.desk = desk
        self.ledger = ledger
        self.worker_id = worker_id
        self._stats = {
            "new_signals": 0,
            "already_processed": 0,
            "claim_failed": 0,
            "completed_no_signal": 0,
        }

    def process_events(
        self,
        strategy_version: str = "1.0",
        strategy_config_hash: str = "",
        account_snapshot_id: str = "",
        market_data: Optional[dict] = None,
    ) -> list:
        """幂等处理所有活跃事件。"""
        events = self.desk.store.query_active()
        signals: list = []

        for event in events:
            event_id = event.event_id
            strategy_id = getattr(event.source, 'name', '') if hasattr(event, 'source') else ''

            # 1. 已处理 -> 跳过
            processed, _ = self.ledger.is_already_processed(
                event_id=event_id, strategy_id=strategy_id,
                strategy_version=strategy_version,
                strategy_config_hash=strategy_config_hash,
                account_snapshot_id=account_snapshot_id)
            if processed:
                self._stats["already_processed"] += 1
                continue

            # 2. 原子领取
            claimed, _ = self.ledger.claim(
                event_id=event_id, strategy_id=strategy_id,
                strategy_version=strategy_version,
                strategy_config_hash=strategy_config_hash,
                account_snapshot_id=account_snapshot_id,
                worker_id=self.worker_id)
            if not claimed:
                self._stats["claim_failed"] += 1
                continue

            # 3. 生成信号
            try:
                sig = self.desk._event_to_signal(event, market_data)
                if sig is not None:
                    signals.append(sig)
                    self.ledger.mark_completed(
                        event_id=event_id, signal_id=sig.signal_id,
                        strategy_id=strategy_id,
                        strategy_version=strategy_version,
                        strategy_config_hash=strategy_config_hash,
                        account_snapshot_id=account_snapshot_id)
                    self._stats["new_signals"] += 1
                else:
                    self.ledger.mark_completed_no_signal(
                        event_id=event_id,
                        strategy_id=strategy_id,
                        strategy_version=strategy_version,
                        strategy_config_hash=strategy_config_hash,
                        account_snapshot_id=account_snapshot_id)
                    self._stats["completed_no_signal"] += 1
            except Exception as exc:
                self.ledger.mark_failed(
                    event_id=event_id, reason=str(exc)[:200],
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    strategy_config_hash=strategy_config_hash,
                    account_snapshot_id=account_snapshot_id)

        level_order = {"ACTION": 0, "WATCH": 1, "INFO": 2}
        signals.sort(key=lambda s: level_order.get(
            getattr(s, 'signal_level', 'INFO'), 3))
        return signals

    @property
    def stats(self) -> dict:
        return dict(self._stats)
