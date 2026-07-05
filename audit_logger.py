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
import uuid
from datetime import date, datetime
from typing import Optional, Any

from db import get_conn
from serenity_logger import get_logger

log = get_logger(__name__)

# 策略版本和配置哈希（在交易内核冻结期内固定）
STRATEGY_VERSION = "v4-phase1-frozen-20260705"
CONFIG_HASH = ""  # 由 bootstrap 时计算


def set_config_hash(hash_val: str) -> None:
    """设置当前配置的哈希值。在系统启动时调用一次。"""
    global CONFIG_HASH
    CONFIG_HASH = hash_val


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
                   risk_checks: dict | None = None,
                   suggested_position: float = 0.0,
                   suggested_position_pct: float = 0.0,
                   data_snapshot_id: int | None = None,
                   correlation_cluster: str = "",
                   ) -> str:
        """记录一条决策信号。

        Returns:
            decision_id (UUID string)
        """
        decision_id = uuid.uuid4().hex[:16]
        now = datetime.now().isoformat()

        signal_divergence = ""
        if baseline_signal and adaptive_signal and baseline_signal != adaptive_signal:
            signal_divergence = f"Frozen={baseline_signal} Adaptive={adaptive_signal}"

        conn = get_conn()
        try:
            conn.execute("""
                INSERT INTO decision_audit_log (
                    decision_id, created_at, stock_code,
                    strategy_version, config_hash, kernel_frozen,
                    data_snapshot_id,
                    score_components_json, total_score, signal_type,
                    baseline_signal, adaptive_signal, signal_divergence,
                    market_regime, theme_exposure, correlation_cluster,
                    t1_locked, limit_status, can_execute, cannot_execute_reason,
                    expected_return_gross, expected_return_net, expected_cost,
                    risk_checks_json, suggested_position, suggested_position_pct,
                    execution_status
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (
                decision_id, now, code,
                STRATEGY_VERSION, CONFIG_HASH,
                data_snapshot_id,
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
                       benchmark_return: float = 0.0) -> None:
        """回填事后结算收益。"""
        excess = 0.0
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

    def settle_all_pending(self) -> int:
        """批量回填所有未结算的审计记录。

        对每条 execution_status='executed' 且 t5_return_net IS NULL 的记录，
        从 price_history 计算实际收益。

        Returns:
            已结算的记录数
        """
        conn = get_conn()
        try:
            pending = conn.execute("""
                SELECT decision_id, stock_code, created_at
                FROM decision_audit_log
                WHERE execution_status = 'executed'
                  AND t5_return_net IS NULL
                  AND created_at <= ?
                ORDER BY created_at
            """, ((date.today().isoformat()),)).fetchall()
        finally:
            conn.close()

        settled = 0
        for row in pending:
            try:
                self._settle_one(row["decision_id"], row["stock_code"],
                                 row["created_at"])
                settled += 1
            except Exception as e:
                log.warning(f"结算 {row['decision_id']} 失败: {e}")

        return settled

    def _settle_one(self, decision_id: str, code: str,
                    signal_date_str: str) -> None:
        """结算单条记录。"""
        from db import get_price_history
        signal_date = date.fromisoformat(signal_date_str[:10])

        prices = get_price_history(code, days=60)
        if not prices or len(prices) < 5:
            return

        # 找到信号日之后的交易日
        closes = [(p[0], p[2]) for p in prices[-30:]]  # (date_str, close)
        signal_idx = None
        for i, (d, _) in enumerate(closes):
            if d >= signal_date_str[:10]:
                signal_idx = i
                break

        if signal_idx is None or signal_idx + 20 >= len(closes):
            return

        signal_close = closes[signal_idx][1] or 0
        if signal_close <= 0:
            return

        def _ret_at(n):
            if signal_idx + n < len(closes):
                c = closes[signal_idx + n][1] or 0
                if c > 0:
                    return (c - signal_close) / signal_close
            return None

        from check_trading_day import is_trading_day
        t1 = _ret_at(1)
        t5 = _ret_at(5)
        t20 = _ret_at(20)

        self.settle_outcome(decision_id, t1, t5, t20)

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
            cutoff = date.today().isoformat()
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
        finally:
            conn.close()

        return {
            "total": total,
            "executed": executed,
            "blocked": blocked,
            "overridden": overridden,
            "settled": settled,
            "execution_rate": executed / max(total, 1),
            "override_rate": overridden / max(total, 1),
        }


# 模块级单例
_audit_logger: Optional[DecisionAuditLogger] = None


def get_audit_logger() -> DecisionAuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = DecisionAuditLogger()
    return _audit_logger
