"""
决策审计链 — audit_logger.py

在每次信号生成时写入 decision_audit_log，实现从"原始行情 → 因子值 →
评分 → 信号 → 可成交性 → 执行 → 收益 → 归因"的完整追溯链。

Usage:
    from audit_logger import DecisionAuditLogger

    dal = DecisionAuditLogger()
    did = dal.log_signal(code, scores, signal_type, market_state, ...)

每条决策记录的字段定义见 v4 报告 §5.2。
"""

from __future__ import annotations

import json
import hashlib
import uuid
from datetime import date, datetime, timedelta
from typing import Optional, Any

from db import get_conn
from serenity_logger import get_logger

log = get_logger(__name__)

# 策略版本和配置哈希（在交易内核冻结期内固定）
STRATEGY_VERSION = "v4-phase1-frozen-20260705"
CONFIG_HASH = ""  # 由 bootstrap 时计算
ROUND_TRIP_COST_RATE = 0.00302


def set_config_hash(hash_val: str) -> None:
    """设置当前配置的哈希值。在系统启动时调用一次。"""
    global CONFIG_HASH
    CONFIG_HASH = hash_val


def get_strategy_identity() -> tuple[str, str]:
    """返回当前重大策略版本及覆盖全部开闸语义的哈希。"""
    if CONFIG_HASH:
        return STRATEGY_VERSION, CONFIG_HASH
    from auto_gate import ensure_current_strategy_version
    current = ensure_current_strategy_version("decision audit identity check")
    return current["version"], current["config_hash"]


def get_config_hash() -> str:
    return get_strategy_identity()[1]


class DecisionAuditLogger:
    """决策审计日志写入器。

    在每个信号生成点调用，将完整的决策上下文写入 decision_audit_log。
    """

    def __init__(self):
        self._today = date.today().isoformat()

    # ── 写入 ──────────────────────────────────────────────

    def log_signal(self, code: str, signal_type: str,
                   total_score: float,
                   score_components: dict[str, float],
                   market_regime: str = "",
                   theme_exposure: float = 0.0,
                   t1_locked: bool = False,
                   limit_status: str = "normal",
                   can_execute: bool = True,
                   cannot_execute_reason: str = "",
                   expected_return_gross: float = 0.0,
                   expected_return_net: float = 0.0,
                   expected_cost: float = 0.0,
                   baseline_signal: str = "",
                   adaptive_signal: str = "",
                   risk_checks: Optional[dict] = None,
                   suggested_position: float = 0.0,
                   suggested_position_pct: float = 0.0,
                   data_snapshot_id: Optional[int] = None,
                   feature_snapshot: Optional[dict] = None,
                   correlation_cluster: str = "",
                   ) -> str:
        """记录一条决策信号。

        Returns:
            decision_id (UUID string)
        """
        decision_id = uuid.uuid4().hex[:16]
        now = datetime.now().isoformat()
        feature_snapshot_hash = hashlib.sha256(
            json.dumps(
                feature_snapshot or {}, sort_keys=True, ensure_ascii=True, default=str
            ).encode("utf-8")
        ).hexdigest()
        strategy_version, config_hash = get_strategy_identity()

        signal_divergence = ""
        if baseline_signal and adaptive_signal and baseline_signal != adaptive_signal:
            signal_divergence = f"Frozen={baseline_signal} Adaptive={adaptive_signal}"

        conn = get_conn()
        try:
            conn.execute("""
                INSERT INTO decision_audit_log (
                    decision_id, created_at, stock_code,
                    strategy_version, config_hash, kernel_frozen,
                    data_snapshot_id, feature_snapshot_hash,
                    score_components_json, total_score, signal_type,
                    baseline_signal, adaptive_signal, signal_divergence,
                    market_regime, theme_exposure, correlation_cluster,
                    t1_locked, limit_status, can_execute, cannot_execute_reason,
                    expected_return_gross, expected_return_net, expected_cost,
                    risk_checks_json, suggested_position, suggested_position_pct,
                    execution_status
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (
                decision_id, now, code,
                strategy_version, config_hash,
                data_snapshot_id, feature_snapshot_hash,
                json.dumps(score_components, ensure_ascii=False),
                total_score, signal_type,
                baseline_signal, adaptive_signal, signal_divergence,
                market_regime, theme_exposure, correlation_cluster,
                1 if t1_locked else 0, limit_status,
                1 if can_execute else 0, cannot_execute_reason,
                expected_return_gross, expected_return_net, expected_cost,
                json.dumps(risk_checks or {}, ensure_ascii=False),
                suggested_position, suggested_position_pct,
            ))
            conn.commit()
            log.debug(f"审计记录写入: {decision_id} {code} {signal_type} score={total_score:.0f}")
        except Exception as e:
            log.error(f"审计记录写入失败: {e}")
            raise
        finally:
            conn.close()

        return decision_id

    # ── 更新：执行 ────────────────────────────────────────

    def log_execution(self, decision_id: str, trade_id: int,
                      fill_price: float, slippage: float,
                      actual_position: float,
                      execution_status: str = "executed") -> None:
        """记录实际执行结果。"""
        conn = get_conn()
        try:
            conn.execute("""
                UPDATE decision_audit_log
                SET execution_status = ?, actual_trade_id = ?,
                    fill_price = ?, slippage = ?,
                    actual_position = ?
                WHERE decision_id = ?
            """, (execution_status, trade_id, fill_price, slippage,
                  actual_position, decision_id))
            conn.commit()
        except Exception as e:
            log.error(f"执行记录更新失败 {decision_id}: {e}")
        finally:
            conn.close()

    def log_human_override(self, decision_id: str, reason: str,
                           notes: str = "") -> None:
        """记录人工干预。"""
        conn = get_conn()
        try:
            conn.execute("""
                UPDATE decision_audit_log
                SET human_override = 1,
                    human_override_reason = ?,
                    operator_notes = ?,
                    execution_status = 'overridden'
                WHERE decision_id = ?
            """, (reason, notes, decision_id))
            conn.commit()
        except Exception as e:
            log.error(f"人工干预记录失败 {decision_id}: {e}")
        finally:
            conn.close()

    def log_blocked(self, decision_id: str, reason: str) -> None:
        """标记信号被可成交性检查阻止。"""
        conn = get_conn()
        try:
            conn.execute("""
                UPDATE decision_audit_log
                SET can_execute = 0, cannot_execute_reason = ?,
                    execution_status = 'blocked'
                WHERE decision_id = ?
            """, (reason, decision_id))
            conn.commit()
        except Exception as e:
            log.error(f"阻塞记录失败 {decision_id}: {e}")
        finally:
            conn.close()

    # ── 事后结算 ──────────────────────────────────────────

    def settle_outcome(self, decision_id: str,
                       t1_return_net: float, t5_return_net: float,
                       t20_return_net: float,
                       benchmark_return: Optional[float] = None) -> None:
        """回填事后结算收益。"""
        excess = None
        if t5_return_net is not None and benchmark_return is not None:
            excess = t5_return_net - benchmark_return

        conn = get_conn()
        try:
            conn.execute("""
                UPDATE decision_audit_log
                SET t1_return_net = ?, t5_return_net = ?, t20_return_net = ?,
                    benchmark_return = ?, excess_return = ?,
                    settled_at = ?
                WHERE decision_id = ?
            """, (t1_return_net, t5_return_net, t20_return_net,
                  benchmark_return, excess,
                  date.today().isoformat(), decision_id))
            conn.commit()
        except Exception as e:
            log.error(f"结算回填失败 {decision_id}: {e}")
        finally:
            conn.close()

    def settle_all_pending(self, min_age_days: int = 6) -> int:
        """批量回填所有未结算的审计记录。

        对每条 t5_return_net IS NULL 且 created_at 距今 ≥ min_age_days 的记录，
        从 price_history 计算实际收益。不要求 execution_status='executed' ——
        只要时间到了就结算，无论执行状态。

        Args:
            min_age_days: 最少等待天数（默认 6，即 T+6）

        Returns:
            已结算的记录数
        """
        cutoff = (date.today() - timedelta(days=min_age_days)).isoformat()
        conn = get_conn()
        try:
            pending = conn.execute("""
                SELECT decision_id, stock_code, created_at
                FROM decision_audit_log
                WHERE t5_return_net IS NULL
                  AND created_at <= ?
                ORDER BY created_at
            """, (cutoff,)).fetchall()
        finally:
            conn.close()

        settled = 0
        for row in pending:
            try:
                if self._settle_one(row["decision_id"], row["stock_code"],
                                    row["created_at"]):
                    settled += 1
            except Exception as e:
                log.warning(f"结算 {row['decision_id']} 失败: {e}")

        return settled

    def backfill_from_signal_log(self) -> int:
        """从 signal_log 回填 decision_audit_log 的事后收益。

        匹配逻辑：同一只标的 + 同一天（date 对齐 created_at[:10]）。
        优先使用已结算记录，其次使用有 outcome_5d 的记录。

        Returns:
            已回填的记录数
        """
        conn = get_conn()
        try:
            # 优先：已结算的 return_5d
            rows = conn.execute("""
                SELECT sl.code, sl.date, sl.action, sl.return_5d, sl.outcome_5d,
                       sl.benchmark_return_5d, sl.excess_5d, sl.outcome_1d,
                       sl.outcome_3d, sl.outcome_10d
                FROM signal_log sl
                WHERE sl.settlement_status = 'settled'
                  AND sl.return_5d IS NOT NULL
                ORDER BY sl.date DESC
            """).fetchall()

            # 回退：未结算但有 outcome 数据的
            if not rows:
                rows = conn.execute("""
                    SELECT sl.code, sl.date, sl.action, sl.return_5d, sl.outcome_5d,
                           sl.benchmark_return_5d, sl.excess_5d, sl.outcome_1d,
                           sl.outcome_3d, sl.outcome_10d
                    FROM signal_log sl
                    WHERE sl.return_5d IS NOT NULL
                       OR sl.outcome_5d IS NOT NULL
                    ORDER BY sl.date DESC
                    LIMIT 200
                """).fetchall()
        finally:
            conn.close()

        backfilled = 0
        for row in rows:
            code = row["code"]
            sig_date = row["date"]
            return_5d_raw = row["return_5d"] or row["outcome_5d"]

            if return_5d_raw is None:
                continue

            return_5d = float(return_5d_raw)
            # return_5d is a percentage in signal_log (e.g., 3.5 means 3.5%),
            # but decision_audit_log stores as decimal (e.g., 0.035)
            t5_value = return_5d / 100.0 if abs(return_5d) > 0.5 else return_5d

            benchmark_raw = row["benchmark_return_5d"]
            excess_raw = row["excess_5d"]
            benchmark = None
            excess = None
            if benchmark_raw is not None:
                bv = float(benchmark_raw)
                benchmark = bv / 100.0 if abs(bv) > 0.5 else bv
            if excess_raw is not None:
                ev = float(excess_raw)
                excess = ev / 100.0 if abs(ev) > 0.5 else ev

            outcome_1d = row["outcome_1d"]
            t1_value = float(outcome_1d) / 100.0 if outcome_1d is not None and abs(float(outcome_1d)) > 0.5 else (float(outcome_1d) if outcome_1d is not None else None)

            outcome_10d = row["outcome_10d"]
            t20_value = float(outcome_10d) / 100.0 if outcome_10d is not None and abs(float(outcome_10d)) > 0.5 else (float(outcome_10d) if outcome_10d is not None else None)

            # 匹配 decision_audit_log: 同 code + 同日期
            conn2 = get_conn()
            try:
                matched = conn2.execute("""
                    UPDATE decision_audit_log
                    SET t1_return_net = ?,
                        t5_return_net = ?,
                        t20_return_net = ?,
                        benchmark_return = ?,
                        excess_return = ?,
                        settled_at = ?
                    WHERE stock_code = ?
                      AND created_at LIKE ?
                      AND t5_return_net IS NULL
                """, (
                    t1_value, t5_value, t20_value,
                    benchmark, excess, date.today().isoformat(),
                    code, f"{sig_date}%",
                ))
                conn2.commit()
                if matched.rowcount > 0:
                    backfilled += matched.rowcount
            except Exception as e:
                log.warning(f"回填 {code} {sig_date} 失败: {e}")
            finally:
                conn2.close()

        return backfilled

    def _settle_one(self, decision_id: str, code: str,
                    signal_date_str: str) -> bool:
        """按 T+1 开盘入场、T+6 开盘结算单条记录。"""
        from db import get_price_history
        signal_date = date.fromisoformat(signal_date_str[:10])

        prices = sorted(get_price_history(code, days=90), key=lambda p: p["date"])
        future = [p for p in prices if p["date"] > signal_date.isoformat()]
        if len(future) < 6:
            return False

        entry_open = float(future[0].get("open") or 0)
        if entry_open <= 0:
            return False

        def _net_open_return(offset: int) -> Optional[float]:
            if offset >= len(future):
                return None
            exit_open = float(future[offset].get("open") or 0)
            if exit_open <= 0:
                return None
            return (exit_open / entry_open - 1.0) - ROUND_TRIP_COST_RATE

        t1 = _net_open_return(1)
        t5 = _net_open_return(5)
        t20 = _net_open_return(20)

        benchmark_return = self._benchmark_return(code, signal_date, 5)
        self.settle_outcome(decision_id, t1, t5, t20, benchmark_return)
        return True

    @staticmethod
    def _benchmark_return(code: str, signal_date: date, offset: int) -> Optional[float]:
        """使用与个股完全相同的 T+1/T+6 开盘区间。"""
        from auto_gate import BENCHMARK_BY_TIER
        from config import TIER_1_CODES, TIER_2_CODES, TIER_3_CODES
        from db import get_price_history

        tier = 1 if code in TIER_1_CODES else 2 if code in TIER_2_CODES else 3 if code in TIER_3_CODES else 4
        benchmark_code = BENCHMARK_BY_TIER[tier]["code"]
        rows = sorted(get_price_history(benchmark_code, days=90), key=lambda p: p["date"])
        future = [p for p in rows if p["date"] > signal_date.isoformat()]
        if len(future) <= offset:
            return None
        start = float(future[0].get("open") or 0)
        end = float(future[offset].get("open") or 0)
        return end / start - 1.0 if start > 0 and end > 0 else None

    # ── 查询 ──────────────────────────────────────────────

    def get_pending_count(self) -> int:
        """未结算记录数。"""
        conn = get_conn()
        try:
            row = conn.execute("""
                SELECT COUNT(*) FROM decision_audit_log
                WHERE t5_return_net IS NULL AND execution_status = 'executed'
            """).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def get_stats(self, days: int = 30) -> dict:
        """获取审计统计信息。"""
        conn = get_conn()
        try:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            total = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ?",
                (cutoff,)).fetchone()[0]
            executed = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ? AND execution_status='executed'",
                (cutoff,)).fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ? AND execution_status='blocked'",
                (cutoff,)).fetchone()[0]
            overridden = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ? AND human_override=1",
                (cutoff,)).fetchone()[0]
            settled = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ? AND t5_return_net IS NOT NULL",
                (cutoff,)).fetchone()[0]
            pending_settlements = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ? "
                "AND execution_status='executed' AND t5_return_net IS NULL",
                (cutoff,),
            ).fetchone()[0]
            replay_ready = conn.execute(
                "SELECT COUNT(*) FROM decision_audit_log WHERE created_at >= ? "
                "AND config_hash<>'' AND feature_snapshot_hash<>'' "
                "AND baseline_signal<>'' AND risk_checks_json<>'{}'",
                (cutoff,),
            ).fetchone()[0]
        finally:
            conn.close()

        return {
            "total": total,
            "executed": executed,
            "blocked": blocked,
            "overridden": overridden,
            "settled": settled,
            "pending_settlements": pending_settlements,
            "replay_ready": replay_ready,
            "legacy_incomplete": max(0, total - replay_ready),
            "execution_rate": executed / max(total, 1),
            "override_rate": overridden / max(total, 1),
        }

    # ── 微观结构回填 (v4 Phase 2) ──────────────────────────

    def backfill_microstructure(self) -> int:
        """对最近的 pending 决策回填 t1_locked / limit_status / can_execute。

        从 market_microstructure 模块获取当前可成交性状态，
        写入那些还没有这些字段的 decision_audit_log 记录。

        Returns:
            已更新的记录数
        """
        try:
            from market_microstructure import get_microstructure
            ms = get_microstructure()
        except ImportError:
            log.warning("market_microstructure 不可用，跳过回填")
            return 0

        today = date.today()
        conn = get_conn()
        try:
            pending = conn.execute("""
                SELECT decision_id, stock_code
                FROM decision_audit_log
                WHERE (t1_locked IS NULL OR t1_locked = 0)
                  AND (limit_status IS NULL OR limit_status = 'normal')
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 50
            """, (today.isoformat(),)).fetchall()
        finally:
            conn.close()

        updated = 0
        for row in pending:
            code = row["stock_code"]
            try:
                # 获取可成交性状态
                limit = ms.get_limit_status(code)
                suspended = ms.is_suspended(code)
                t1_locked = ms.is_t1_locked(code, today)

                limit_str = limit.value if hasattr(limit, 'value') else str(limit)
                can_exec = not suspended and limit_str not in (
                    "limit_up_hard", "limit_down_hard", "suspended")
                reason = ""
                if suspended:
                    reason = "停牌"
                elif limit_str in ("limit_up_hard", "limit_down_hard"):
                    reason = f"一字板 ({limit_str})"

                conn2 = get_conn()
                try:
                    conn2.execute("""
                        UPDATE decision_audit_log
                        SET t1_locked = ?,
                            limit_status = ?,
                            can_execute = ?,
                            cannot_execute_reason = CASE
                                WHEN cannot_execute_reason = '' THEN ?
                                ELSE cannot_execute_reason
                            END
                        WHERE decision_id = ?
                    """, (1 if t1_locked else 0, limit_str,
                          1 if can_exec else 0, reason,
                          row["decision_id"]))
                    conn2.commit()
                    updated += 1
                finally:
                    conn2.close()
            except Exception as e:
                log.debug(f"微观结构回填 {code} 失败: {e}")

        return updated


    # ── 标的池变更记录 ────────────────────────────────────

    def log_pool_change(self, code: str, action: str, reason: str,
                        benchmark_expanded: bool = False) -> str:
        """记录标的池成员变更。

        每次手工往 STOCK_MAP 添加/移除标的时调用，
        与 decision_audit_log 共享同一审计存储，
        确保标的池历史可完整回溯。

        同时触发 ensure_current_strategy_version()，
        在 strategy_versions 表的 change_source 字段中记录
        本次 config_hash 变化的具体根因。

        Args:
            code: 标的代码
            action: 'add' | 'remove'
            reason: 入池/移除原因
            benchmark_expanded: 是否同步扩大了 BENCHMARK_UNIVERSE_SIZE

        Returns:
            event_id (UUID string)
        """
        event_id = uuid.uuid4().hex[:16]
        now = datetime.now().isoformat()

        # 先触发策略版本快照 — change_source 精确记录根因
        # （必须在 get_strategy_identity() 之前调用，
        #   否则后者会在 hash 变化时静默创建无 change_source 的版本行）
        change_source = f"stock_pool_change: {action} {code}"
        if benchmark_expanded:
            change_source += " + benchmark_universe_size expanded"
        try:
            from auto_gate import ensure_current_strategy_version
            sv = ensure_current_strategy_version(
                reset_reason=f"pool {action}: {code}",
                change_source=change_source,
            )
        except Exception as e:
            log.warning("标的池变更版本快照失败: %s", e)
            sv = {"version": "unknown", "config_hash": ""}

        # 再写决策审计日志（使用刚创建的版本信息）
        strategy_version = sv.get("version", "unknown")
        config_hash_val = sv.get("config_hash", "")

        conn = get_conn()
        try:
            conn.execute("""
                INSERT INTO decision_audit_log (
                    decision_id, created_at, stock_code,
                    strategy_version, config_hash, kernel_frozen,
                    signal_type,
                    score_components_json,
                    total_score,
                    human_override, human_override_reason,
                    execution_status, post_mortem_label
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?, 'settled', ?)
            """, (
                event_id, now, code,
                strategy_version, config_hash_val,
                f"POOL_{action.upper()}",
                json.dumps({
                    "action": action,
                    "reason": reason,
                    "benchmark_expanded": benchmark_expanded,
                }, ensure_ascii=False),
                0,
                f"pool_{action}",
                f"pool_change: {action} {code} benchmark_expanded={benchmark_expanded}",
            ))
            conn.commit()
        except Exception as e:
            log.error("标的池变更记录失败: %s", e)
            raise
        finally:
            conn.close()

        log.info("标的池变更: %s %s reason=%s benchmark_expanded=%s → %s",
                 action, code, reason, benchmark_expanded, strategy_version)

        return event_id


# 模块级单例
_audit_logger: Optional[DecisionAuditLogger] = None


def get_audit_logger() -> DecisionAuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = DecisionAuditLogger()
    return _audit_logger
