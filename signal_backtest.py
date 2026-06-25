"""
signal_backtest.py -- 60 天全量信号回放
Replay v3.0 scoring logic on historical data to simulate
"what if we had used v3.0 thresholds for the past 60 days?"

Strategy: use signal_log's historical scores and reclassify them
using current v3.0 thresholds (strong_buy=74, buy=66, etc).
Compare the reclassified signals' simulated outcomes vs the
original signals' actual outcomes.
"""

from datetime import date, timedelta
from collections import defaultdict
from typing import Optional

from db import get_conn, get_price_history
from config import SIGNAL_CONFIG, STOCK_MAP
from signal_engine import get_signal_level


# ── Signal category sets (defined locally to avoid coupling) ──
_BULLISH_SIGNALS = {"STRONG_BUY", "BUY", "CAUTION_BUY", "STRONG_HOLD"}
_BEARISH_SIGNALS = {"SELL", "STRONG_SELL", "CAUTION_SELL", "STOP_LOSS", "TAKE_PROFIT"}


# ── v3.0 thresholds (from SIGNAL_CONFIG) ──
V3_STRONG_BUY = SIGNAL_CONFIG.get("strong_buy_threshold", 74.0)
V3_BUY = SIGNAL_CONFIG.get("buy_threshold", 66.0)
V3_HOLD_HIGH = SIGNAL_CONFIG.get("hold_high", 60.0)
V3_HOLD_LOW = SIGNAL_CONFIG.get("hold_low", 50.0)
V3_SELL = SIGNAL_CONFIG.get("sell_threshold", 45.0)

# ── DB table name ──
BACKTEST_TABLE = "signal_backtest_results"


def _ensure_backtest_table():
    """Create signal_backtest_results table if it does not exist."""
    conn = get_conn()
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {BACKTEST_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            original_action TEXT NOT NULL,
            v3_action TEXT NOT NULL,
            total_score REAL DEFAULT 0,
            price REAL DEFAULT 0,
            v3_outcome_1d REAL DEFAULT NULL,
            v3_outcome_3d REAL DEFAULT NULL,
            v3_outcome_5d REAL DEFAULT NULL,
            original_outcome_1d REAL DEFAULT NULL,
            original_outcome_3d REAL DEFAULT NULL,
            original_outcome_5d REAL DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_backtest_run
        ON {BACKTEST_TABLE}(run_id, date)
    """)
    conn.commit()
    conn.close()


def _reclassify_v3_action(total_score: float) -> str:
    """Reclassify a score using current v3.0 thresholds."""
    if total_score >= V3_STRONG_BUY:
        return "STRONG_BUY"
    elif total_score >= V3_BUY:
        return "BUY"
    elif total_score >= V3_HOLD_HIGH:
        return "CAUTION_BUY"
    elif total_score >= V3_HOLD_LOW:
        return "HOLD"
    elif total_score >= V3_SELL:
        return "WATCH"
    else:
        return "SELL"


def _is_bullish(action: str) -> bool:
    return action in _BULLISH_SIGNALS


def _is_bearish(action: str) -> bool:
    return action in _BEARISH_SIGNALS


# ── Main backtest ──

def run_signal_backtest(days: int = 60) -> dict:
    """
    Run a full backtest replaying the last N days of signals.

    For each day in signal_log within the window:
      1. Take the original total_score and price
      2. Reclassify using v3.0 thresholds
      3. Determine v3 simulated outcome from price_history
      4. Compare v3 outcome vs original outcome
      5. Track aggregate statistics

    Returns a dict with comparison data.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    _ensure_backtest_table()

    conn = get_conn()
    rows = conn.execute("""
        SELECT id, code, date, action, total_score, price,
               outcome_1d, outcome_3d, outcome_5d
        FROM signal_log
        WHERE date >= ?
        ORDER BY date, code
    """, (since,)).fetchall()

    if not rows:
        conn.close()
        return {"error": f"No signal_log entries found in the last {days} days.", "total_signals": 0}

    run_id = f"v3_backtest_{date.today().isoformat()}"

    # ── Per-day score simulation ──
    v3_trades = []   # simulated v3 actions
    v3_pnl = []      # simulated P&L per trade
    original_pnl = []

    v3_wins_1d = 0
    v3_wins_3d = 0
    v3_wins_5d = 0
    v3_total_1d = 0
    v3_total_3d = 0
    v3_total_5d = 0

    original_wins_1d = 0
    original_total_1d = 0

    position_map: dict[str, dict] = {}  # code -> {buy_date, buy_price, v3_action}
    v3_portfolio_value = 100000.0
    v3_cash = 100000.0
    v3_holdings: dict[str, dict] = {}

    # Clear previous run results for this run_id
    conn.execute(f"DELETE FROM {BACKTEST_TABLE} WHERE run_id = ?", (run_id,))

    inserted = 0
    for row in rows:
        sid = row["id"]
        code = row["code"]
        signal_date = row["date"]
        original_action = row["action"]
        total_score = row["total_score"] or 50.0
        price = row["price"] or 0

        if price <= 0:
            continue

        v3_action = _reclassify_v3_action(total_score)
        v3_is_bullish = _is_bullish(v3_action)
        original_is_bullish = _is_bullish(original_action)

        # ── Simulate v3 outcome from price_history ──
        price_rows = get_price_history(code, 10)
        sorted_rows = sorted(price_rows, key=lambda r: r["date"])
        try:
            start_idx = next(i for i, r in enumerate(sorted_rows) if r["date"] >= signal_date)
        except StopIteration:
            start_idx = -1

        v3_o1d = None
        v3_o3d = None
        v3_o5d = None
        if start_idx >= 0:
            for offset, field in [(1, "v3_o1d"), (3, "v3_o3d"), (5, "v3_o5d")]:
                idx = start_idx + offset
                if idx < len(sorted_rows):
                    tc = sorted_rows[idx]["close"]
                    if tc and tc > 0:
                        val = (tc - price) / price * 100
                        if field == "v3_o1d":
                            v3_o1d = round(val, 2)
                        elif field == "v3_o3d":
                            v3_o3d = round(val, 2)
                        elif field == "v3_o5d":
                            v3_o5d = round(val, 2)

        original_o1d = row["outcome_1d"]
        original_o3d = row["outcome_3d"]
        original_o5d = row["outcome_5d"]

        # ── Track v3 outcomes for win-rate stats ──
        if v3_o1d is not None:
            v3_total_1d += 1
            if v3_o1d > 0:
                v3_wins_1d += 1
        if v3_o3d is not None:
            v3_total_3d += 1
            if v3_o3d > 0:
                v3_wins_3d += 1
        if v3_o5d is not None:
            v3_total_5d += 1
            if v3_o5d > 0:
                v3_wins_5d += 1

        if original_o1d is not None:
            original_total_1d += 1
            if original_o1d > 0:
                original_wins_1d += 1

        # ── Insert into backtest_results table ──
        conn.execute(f"""
            INSERT INTO {BACKTEST_TABLE}
                (run_id, code, date, original_action, v3_action,
                 total_score, price,
                 v3_outcome_1d, v3_outcome_3d, v3_outcome_5d,
                 original_outcome_1d, original_outcome_3d, original_outcome_5d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, code, signal_date, original_action, v3_action,
            total_score, price,
            v3_o1d, v3_o3d, v3_o5d,
            original_o1d, original_o3d, original_o5d,
        ))
        inserted += 1

        # ── Track P&L per signal ──
        if v3_o1d is not None:
            v3_pnl.append(v3_o1d)
        if original_o1d is not None:
            original_pnl.append(original_o1d)

        # ── Simulated v3 position tracking (buy at signal, sell when signal turns bearish or at +10% target) ──
        if v3_is_bullish and code not in v3_holdings:
            # Simulated buy: allocate a fixed position size
            position_size = min(v3_cash * 0.25, v3_cash)
            v3_holdings[code] = {
                "buy_price": price,
                "buy_date": signal_date,
                "shares": position_size / price if price > 0 else 0,
                "invested": position_size,
                "action": v3_action,
            }
            v3_cash -= position_size
        elif not v3_is_bullish and code in v3_holdings:
            # Sell
            holding = v3_holdings.pop(code)
            sell_value = holding["shares"] * price
            buy_value = holding["invested"]
            pnl_pct = ((price - holding["buy_price"]) / holding["buy_price"]) * 100
            v3_cash += sell_value
            v3_trades.append({
                "code": code,
                "buy_date": holding["buy_date"],
                "sell_date": signal_date,
                "buy_price": holding["buy_price"],
                "sell_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(sell_value - buy_value, 2),
                "entry_action": holding["action"],
                "exit_action": v3_action,
            })

        # Also sell if score hits >= 90 (take profit) or < 40 (stop loss)
        if total_score >= 90 and code in v3_holdings:
            holding = v3_holdings.pop(code)
            sell_value = holding["shares"] * price
            pnl_pct = ((price - holding["buy_price"]) / holding["buy_price"]) * 100
            v3_cash += sell_value
            v3_trades.append({
                "code": code,
                "buy_date": holding["buy_date"],
                "sell_date": signal_date,
                "buy_price": holding["buy_price"],
                "sell_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(sell_value - holding["invested"], 2),
                "entry_action": holding["action"],
                "exit_action": "TAKE_PROFIT",
            })

    # Close any remaining positions at last price
    for code, holding in list(v3_holdings.items()):
        last_price = None
        ph = get_price_history(code, 5)
        if ph:
            last_price = ph[0]["close"]
        if last_price and last_price > 0:
            sell_value = holding["shares"] * last_price
            pnl_pct = ((last_price - holding["buy_price"]) / holding["buy_price"]) * 100
            v3_cash += sell_value
            v3_trades.append({
                "code": code,
                "buy_date": holding["buy_date"],
                "sell_date": "open_position",
                "buy_price": holding["buy_price"],
                "sell_price": last_price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(sell_value - holding["invested"], 2),
                "entry_action": holding["action"],
                "exit_action": "FORCE_CLOSE",
            })

    v3_portfolio_value = v3_cash

    conn.commit()
    conn.close()

    # ── Aggregate stats ──
    stats = _compute_backtest_stats(run_id)

    # ── Build result ──
    v3_total_pnl = sum(t["pnl_amount"] for t in v3_trades)
    v3_avg_pnl = sum(t["pnl_pct"] for t in v3_trades) / len(v3_trades) if v3_trades else 0
    winning_trades = [t for t in v3_trades if t["pnl_pct"] > 0]
    losing_trades = [t for t in v3_trades if t["pnl_pct"] <= 0]
    win_rate = len(winning_trades) / len(v3_trades) * 100 if v3_trades else 0
    avg_win = sum(t["pnl_pct"] for t in winning_trades) / len(winning_trades) if winning_trades else 0
    avg_loss = sum(t["pnl_pct"] for t in losing_trades) / len(losing_trades) if losing_trades else 0

    # Drawdown
    cum_pnl = []
    running = 100000.0
    for t in v3_trades:
        running += t["pnl_amount"]
        cum_pnl.append(running)
    peak = max(cum_pnl) if cum_pnl else 100000.0
    max_drawdown = min(((c - peak) / peak) * 100 for c in cum_pnl) if cum_pnl else 0

    original_avg_o1d = sum(original_pnl) / len(original_pnl) if original_pnl else 0
    v3_avg_o1d = sum(v3_pnl) / len(v3_pnl) if v3_pnl else 0

    return {
        "run_id": run_id,
        "backtest_days": days,
        "total_signal_entries": len(rows),
        "v3_reclassified_actions": stats.get("v3_action_distribution", {}),
        "original_action_distribution": stats.get("original_action_distribution", {}),
        "v3_trades_executed": len(v3_trades),
        "v3_trade_stats": {
            "win_rate_pct": round(win_rate, 1),
            "total_pnl_amount": round(v3_total_pnl, 2),
            "total_pnl_pct": round((v3_portfolio_value - 100000.0) / 100000.0 * 100, 2),
            "avg_trade_pnl_pct": round(v3_avg_pnl, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "final_portfolio_value": round(v3_portfolio_value, 2),
        },
        "v3_win_rate": {
            "outcome_1d": round(v3_wins_1d / v3_total_1d * 100, 1) if v3_total_1d else 0,
            "outcome_3d": round(v3_wins_3d / v3_total_3d * 100, 1) if v3_total_3d else 0,
            "outcome_5d": round(v3_wins_5d / v3_total_5d * 100, 1) if v3_total_5d else 0,
        },
        "original_vs_v3_comparison": {
            "original_avg_outcome_1d_pct": round(original_avg_o1d, 2),
            "v3_avg_outcome_1d_pct": round(v3_avg_o1d, 2),
            "original_1d_win_rate": round(original_wins_1d / original_total_1d * 100, 1) if original_total_1d else 0,
            "v3_1d_win_rate": round(v3_wins_1d / v3_total_1d * 100, 1) if v3_total_1d else 0,
        },
        "records_inserted": inserted,
    }


def _compute_backtest_stats(run_id: str) -> dict:
    """Compute aggregate stats from the backtest table."""
    conn = get_conn()
    v3_dist = {}
    orig_dist = {}
    rows = conn.execute(f"""
        SELECT v3_action, original_action, COUNT(*) as cnt
        FROM {BACKTEST_TABLE}
        WHERE run_id = ?
        GROUP BY v3_action, original_action
    """, (run_id,)).fetchall()

    for r in rows:
        v3_dist[r["v3_action"]] = v3_dist.get(r["v3_action"], 0) + r["cnt"]
        orig_dist[r["original_action"]] = orig_dist.get(r["original_action"], 0) + r["cnt"]

    conn.close()
    return {
        "v3_action_distribution": dict(sorted(v3_dist.items())),
        "original_action_distribution": dict(sorted(orig_dist.items())),
    }


def format_backtest_report(results: dict) -> str:
    """
    Format backtest results as a clean Markdown report.
    """
    if results.get("error"):
        return f"⚠️ {results['error']}"

    lines = []
    lines.append("## 🔄 信号回放回测报告 — v3.0 门槛重分类")
    lines.append(f"  回测周期: 过去 {results.get('backtest_days', 60)} 天")
    lines.append(f"  信号总数: {results.get('total_signal_entries', 0)}")
    lines.append(f"  Run ID: {results.get('run_id', 'N/A')}")
    lines.append("")

    # ── Action distribution ──
    lines.append("### 信号分布变化")
    lines.append("")
    orig_dist = results.get("original_action_distribution", {})
    v3_dist = results.get("v3_reclassified_actions", {})
    all_actions = sorted(set(list(orig_dist.keys()) + list(v3_dist.keys())))
    lines.append(f"  {'信号类型':<16s} {'原信号':>6s} {'v3重分类':>8s} {'变化':>6s}")
    lines.append(f"  {'─' * 40}")
    for a in all_actions:
        orig_cnt = orig_dist.get(a, 0)
        v3_cnt = v3_dist.get(a, 0)
        delta = v3_cnt - orig_cnt
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        lines.append(f"  {a:<16s} {orig_cnt:>6d} {v3_cnt:>8d} {delta_str:>6s}")
    lines.append("")

    # ── Win rate comparison ──
    lines.append("### 1日胜率对比")
    lines.append("")
    comp = results.get("original_vs_v3_comparison", {})
    lines.append(f"  | 指标 | 原系统 | v3.0重分类 |")
    lines.append(f"  |------|--------|------------|")
    lines.append(f"  | 1日平均收益 | {comp.get('original_avg_outcome_1d_pct', 0):+.2f}% | {comp.get('v3_avg_outcome_1d_pct', 0):+.2f}% |")
    lines.append(f"  | 1日胜率 | {comp.get('original_1d_win_rate', 0):.1f}% | {comp.get('v3_1d_win_rate', 0):.1f}% |")

    wr = results.get("v3_win_rate", {})
    lines.append("")
    lines.append(f"  **v3.0 各周期胜率**:")
    lines.append(f"  - 1日: {wr.get('outcome_1d', 0):.1f}%")
    lines.append(f"  - 3日: {wr.get('outcome_3d', 0):.1f}%")
    lines.append(f"  - 5日: {wr.get('outcome_5d', 0):.1f}%")
    lines.append("")

    # ── Simulated trade stats ──
    ts = results.get("v3_trade_stats", {})
    lines.append("### 模拟交易绩效")
    lines.append("")
    lines.append(f"  - **交易次数**: {results.get('v3_trades_executed', 0)}")
    lines.append(f"  - **胜率**: {ts.get('win_rate_pct', 0):.1f}%")
    lines.append(f"  - **总盈亏**: {ts.get('total_pnl_pct', 0):+.2f}% ({ts.get('total_pnl_amount', 0):+.2f}元)")
    lines.append(f"  - **平均单笔**: {ts.get('avg_trade_pnl_pct', 0):+.2f}%")
    lines.append(f"  - **平均盈利**: {ts.get('avg_win_pct', 0):+.2f}%")
    lines.append(f"  - **平均亏损**: {ts.get('avg_loss_pct', 0):+.2f}%")
    lines.append(f"  - **最大回撤**: {ts.get('max_drawdown_pct', 0):.2f}%")
    lines.append(f"  - **最终组合价值**: {ts.get('final_portfolio_value', 0):.2f}元 (起始100,000)")
    lines.append("")

    # ── Conclusion ──
    win_delta = comp.get("v3_1d_win_rate", 0) - comp.get("original_1d_win_rate", 0)
    pnl_delta = comp.get("v3_avg_outcome_1d_pct", 0) - comp.get("original_avg_outcome_1d_pct", 0)
    lines.append("### 结论")
    lines.append("")
    if win_delta > 0:
        lines.append(f"  ✅ **v3.0 门槛重分类后 1日胜率提升 {win_delta:+.1f}pp**，平均收益 {pnl_delta:+.2f}pp")
    elif win_delta < 0:
        lines.append(f"  ⚠️ **v3.0 门槛重分类后 1日胜率下降 {win_delta:.1f}pp**，平均收益 {pnl_delta:+.2f}pp")
    else:
        lines.append(f"  ➡️ v3.0 门槛重分类后胜率基本持平")

    lines.append("")
    lines.append("---")
    lines.append(f"> ⏰ 生成时间: {date.today().isoformat()} | 数据来源: signal_log + v3.0 SIGNAL_CONFIG")

    return "\n".join(lines)


# ── CLI entry ──

def cmd_signal_backtest(days: int = 60):
    """CLI entry point."""
    print(f"\n🔄  Signal Backtest — {days} 天信号回放 | {date.today()}")
    print("=" * 60)
    print("  加载 signal_log 历史数据 → v3.0 阈值重分类 → 胜负率对比")
    print()

    results = run_signal_backtest(days=days)
    print(format_backtest_report(results))


if __name__ == "__main__":
    import sys
    days = 60
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    cmd_signal_backtest(days=days)
