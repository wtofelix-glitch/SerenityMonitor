#!/usr/bin/env python3
"""
完整交易日志 — 每笔实盘交易关联: 触发信号、入场/出场评分、市场制度、AI 反思
闭合学习回路: 交易 → 反思 → 参数优化
"""
import json
from datetime import date, datetime
from serenity_logger import get_logger

log = get_logger(__name__)


def complete_trade_journal(trade_id: int = None) -> dict:
    """自动补全所有未完整记录的交易日志"""
    from db import get_conn

    conn = get_conn()

    # 查找 reflection 为空的交易
    if trade_id:
        rows = conn.execute("SELECT * FROM trading_journal WHERE id=?", (trade_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trading_journal WHERE reflection IS NULL OR score_at_entry IS NULL ORDER BY date DESC LIMIT 20"
        ).fetchall()

    filled = 0
    for r in rows:
        d = dict(r)
        code = d["code"]
        trade_date = d["date"]
        action = d.get("action", "")

        # 1. 补入场评分 — 从 scoring_history 取交易当日或前一日评分
        entry_score = d.get("score_at_entry")
        if entry_score is None:
            score_row = conn.execute(
                "SELECT total_score FROM scoring_history WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
                (code, trade_date),
            ).fetchone()
            entry_score = score_row["total_score"] if score_row else None

        # 2. 补出场评分 — 卖出时补
        exit_score = d.get("score_at_exit")
        if exit_score is None and action == "sell":
            score_row = conn.execute(
                "SELECT total_score FROM scoring_history WHERE code=? AND date>=? ORDER BY date ASC LIMIT 1",
                (code, trade_date),
            ).fetchone()
            exit_score = score_row["total_score"] if score_row else None

        # 3. 补信号触发信息
        signal_info = {}
        sig_row = conn.execute(
            "SELECT action, total_score, details FROM signal_log WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (code, trade_date),
        ).fetchone()
        if sig_row:
            signal_info = dict(sig_row)
            try:
                details = json.loads(sig_row["details"]) if isinstance(sig_row["details"], str) else (sig_row["details"] or {})
                signal_info["ai_explanation"] = details.get("ai_explanation", "")
            except (json.JSONDecodeError, TypeError):
                signal_info["ai_explanation"] = ""

        # 4. 补市场制度
        regime = "未知"
        regime_row = conn.execute(
            "SELECT regime FROM conviction_log WHERE date<=? ORDER BY date DESC LIMIT 1",
            (trade_date,),
        ).fetchone()
        if regime_row:
            regime = regime_row["regime"]

        # 5. 补损益
        profit = d.get("profit_pct")
        if profit is None:
            # 从 trades 表计算
            buy_row = conn.execute(
                "SELECT price FROM trades WHERE code=? AND action='buy' AND date<=? ORDER BY date DESC LIMIT 1",
                (code, trade_date),
            ).fetchone()
            sell_row = conn.execute(
                "SELECT price FROM trades WHERE code=? AND action='sell' AND date>=? ORDER BY date ASC LIMIT 1",
                (code, trade_date),
            ).fetchone()
            if buy_row and sell_row:
                buy_p = float(buy_row["price"])
                sell_p = float(sell_row["price"])
                profit = round((sell_p - buy_p) / buy_p * 100, 2) if buy_p > 0 else 0

        # 6. 生成反思文本
        ai_text = signal_info.get("ai_explanation", "")
        score_note = f"入场评分: {entry_score}" if entry_score else ""
        exit_note = f"出场评分: {exit_score}" if exit_score else ""
        profit_note = f"损益: {profit:+.2f}%" if profit is not None else ""

        reflection = f"{profit_note}. {score_note}. {exit_note}. 制度: {regime}. {ai_text}".strip(". ").strip()
        if len(reflection) < 10:
            reflection = f"自动记录. 损益: {profit_note}. {score_note}."

        # 7. 生成 Tags
        tags = []
        if profit and profit > 5:
            tags.append("高收益")
        elif profit and profit < -5:
            tags.append("高亏损")
        if regime:
            tags.append(regime)
        if signal_info.get("action") == "STRONG_BUY":
            tags.append("高确信信号")
        tags.append("v5.9自动")

        # 更新数据库
        conn.execute(
            """UPDATE trading_journal SET
                score_at_entry=?, score_at_exit=?, profit_pct=?,
                reflection=?, tags=?, updated_at=datetime('now','localtime')
            WHERE id=?""",
            (entry_score, exit_score, profit, reflection,
             json.dumps(tags, ensure_ascii=False), d["id"]),
        )
        filled += 1

    conn.commit()
    conn.close()
    log.info("已补全 %d 条交易日志", filled)
    return {"filled": filled}


def get_journal_summary(days: int = 30) -> dict:
    """获取最近 N 天交易日志摘要"""
    from db import get_conn
    conn = get_conn()

    rows = conn.execute(
        "SELECT * FROM trading_journal WHERE date >= date('now', ?) ORDER BY date DESC",
        (f"-{days} days",),
    ).fetchall()
    conn.close()

    trades = [dict(r) for r in rows]
    wins = [t for t in trades if (t.get("profit_pct") or 0) > 0]
    total = len(trades)
    total_pnl = sum(t.get("profit_pct") or 0 for t in trades)

    return {
        "total_trades": total,
        "win_count": len(wins),
        "win_rate": round(len(wins) / total * 100, 1) if total > 0 else 0,
        "total_pnl_pct": round(total_pnl, 2),
        "avg_pnl_pct": round(total_pnl / total, 2) if total > 0 else 0,
        "days": days,
        "recent": trades[:5],
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    result = complete_trade_journal()
    print(f"✅ 已补全 {result['filled']} 条交易日志")

    summary = get_journal_summary(days=30)
    print(f"\n📊 近30日交易:")
    print(f"  交易: {summary['total_trades']} 笔")
    print(f"  胜率: {summary['win_rate']}%")
    print(f"  累计: {summary['total_pnl_pct']:+.2f}%")
    print(f"  平均: {summary['avg_pnl_pct']:+.2f}%")
