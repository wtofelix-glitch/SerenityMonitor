from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import ComparisonMetrics, EvolutionCandidate, GateResult, Stage


SCHEMA_VERSION = 1


class EvolutionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS evolution_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_candidates_v2 (
                    candidate_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    baseline_version TEXT NOT NULL,
                    weights_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_evaluations_v2 (
                    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    failures_json TEXT NOT NULL,
                    checks_json TEXT NOT NULL,
                    comparison_json TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES evolution_candidates_v2(candidate_id)
                );
                CREATE TABLE IF NOT EXISTS evolution_transitions_v2 (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    from_stage TEXT NOT NULL,
                    to_stage TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    approval_ref TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES evolution_candidates_v2(candidate_id)
                );
                CREATE TABLE IF NOT EXISTS evolution_active_v2 (
                    environment TEXT PRIMARY KEY CHECK(environment IN ('paper', 'live')),
                    candidate_id TEXT,
                    previous_candidate_id TEXT,
                    activated_at TEXT,
                    FOREIGN KEY(candidate_id) REFERENCES evolution_candidates_v2(candidate_id)
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO evolution_schema(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, self._now()),
            )
            db.execute(
                "INSERT OR IGNORE INTO evolution_active_v2(environment) VALUES ('paper')"
            )
            db.execute(
                "INSERT OR IGNORE INTO evolution_active_v2(environment) VALUES ('live')"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(value: object) -> str:
        def default(item: object) -> object:
            if hasattr(item, "__dict__"):
                return item.__dict__
            if hasattr(item, "value"):
                return getattr(item, "value")
            raise TypeError(f"not JSON serializable: {type(item)!r}")

        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=default)

    def save_candidate(self, candidate: EvolutionCandidate) -> None:
        now = self._now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO evolution_candidates_v2
                  (candidate_id, created_at, baseline_version, weights_json,
                   evidence_json, stage, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.created_at.isoformat(),
                    candidate.baseline_version,
                    self._json(candidate.weights),
                    self._json(candidate.evidence),
                    Stage.PAPER_CANDIDATE.value,
                    now,
                ),
            )

    def record_evaluation(
        self,
        candidate_id: str,
        result: GateResult,
        comparison: ComparisonMetrics,
    ) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT stage FROM evolution_candidates_v2 WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            old_stage = row["stage"]
            db.execute(
                """
                INSERT INTO evolution_evaluations_v2
                  (candidate_id, evaluated_at, passed, failures_json, checks_json, comparison_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    self._now(),
                    int(result.passed),
                    self._json(result.failures),
                    self._json(result.checks),
                    self._json(comparison),
                ),
            )
            db.execute(
                "UPDATE evolution_candidates_v2 SET stage = ?, updated_at = ? WHERE candidate_id = ?",
                (result.stage.value, self._now(), candidate_id),
            )
            db.execute(
                """
                INSERT INTO evolution_transitions_v2
                  (candidate_id, from_stage, to_stage, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    old_stage,
                    result.stage.value,
                    "gate_passed" if result.passed else "gate_failed:" + ",".join(result.failures),
                    self._now(),
                ),
            )
            if result.passed:
                self._activate(db, "paper", candidate_id)

    def _activate(self, db: sqlite3.Connection, environment: str, candidate_id: str) -> None:
        row = db.execute(
            "SELECT candidate_id FROM evolution_active_v2 WHERE environment = ?", (environment,)
        ).fetchone()
        previous = row["candidate_id"] if row else None
        db.execute(
            """
            UPDATE evolution_active_v2
               SET previous_candidate_id = ?, candidate_id = ?, activated_at = ?
             WHERE environment = ?
            """,
            (previous, candidate_id, self._now(), environment),
        )

    def promote_live(
        self,
        candidate_id: str,
        *,
        approval_ref: str,
        canary_days: int,
        live_apply_enabled: bool,
    ) -> None:
        if not live_apply_enabled:
            raise PermissionError("live apply is disabled by configuration")
        if not approval_ref.strip():
            raise PermissionError("an explicit human approval reference is required")
        if canary_days < 20:
            raise PermissionError("at least 20 paper-canary trading days are required")
        with self.connect() as db:
            row = db.execute(
                "SELECT stage FROM evolution_candidates_v2 WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            if row["stage"] != Stage.PAPER_CANARY.value:
                raise ValueError("candidate has not passed the paper promotion gate")
            self._activate(db, "live", candidate_id)
            db.execute(
                "UPDATE evolution_candidates_v2 SET stage = ?, updated_at = ? WHERE candidate_id = ?",
                (Stage.LIVE.value, self._now(), candidate_id),
            )
            db.execute(
                """
                INSERT INTO evolution_transitions_v2
                  (candidate_id, from_stage, to_stage, reason, approval_ref, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    Stage.PAPER_CANARY.value,
                    Stage.LIVE.value,
                    f"paper_canary_completed:{canary_days}",
                    approval_ref,
                    self._now(),
                ),
            )

    def rollback_live(self, *, reason: str, approval_ref: str) -> str | None:
        if not reason.strip() or not approval_ref.strip():
            raise ValueError("rollback reason and approval reference are required")
        with self.connect() as db:
            active = db.execute(
                "SELECT candidate_id, previous_candidate_id FROM evolution_active_v2 WHERE environment='live'"
            ).fetchone()
            if active is None or active["candidate_id"] is None:
                return None
            current = active["candidate_id"]
            previous = active["previous_candidate_id"]
            db.execute(
                """
                UPDATE evolution_active_v2
                   SET candidate_id = ?, previous_candidate_id = NULL, activated_at = ?
                 WHERE environment = 'live'
                """,
                (previous, self._now()),
            )
            db.execute(
                "UPDATE evolution_candidates_v2 SET stage = ?, updated_at = ? WHERE candidate_id = ?",
                (Stage.ROLLED_BACK.value, self._now(), current),
            )
            db.execute(
                """
                INSERT INTO evolution_transitions_v2
                  (candidate_id, from_stage, to_stage, reason, approval_ref, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (current, Stage.LIVE.value, Stage.ROLLED_BACK.value, reason, approval_ref, self._now()),
            )
            return previous

    def status(self) -> dict[str, object]:
        with self.connect() as db:
            active = [dict(row) for row in db.execute("SELECT * FROM evolution_active_v2")]
            recent = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT candidate_id, baseline_version, stage, created_at, updated_at
                      FROM evolution_candidates_v2 ORDER BY created_at DESC LIMIT 10
                    """
                )
            ]
        return {"active": active, "recent_candidates": recent}
