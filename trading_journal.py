"""
交易日志模块 — 记录每笔交易的决策原因和复盘反思

与 trades 表的区别:
  - trades: 系统自动记录（信号触发的买卖）
  - journal: 用户手动记录（为什么买/卖、当时的想法、事后复盘）

用法:
    python3 trading_journal.py                           # 最近日志
    python3 trading_journal.py --code 002281             # 某只标的
    python3 trading_journal.py --stats                   # 统计
"""
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn
from config import STOCK_MAP


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trading_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    date TEXT NOT NULL,
    price REAL,
    shares INTEGER,
    amount REAL,
    reason TEXT,
    reflection TEXT,
    score_at_entry REAL,
    score_at_exit REAL,
    profit_pct REAL,
    tags TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


def init_journal_table():
    conn = get_conn()
    conn.execute(TABLE_SQL)
    conn.commit()
    conn.close()


def log_trade(code: str, action: str, date_str: str = None, price: float = 0,
              shares: int = 0, reason: str = "", score: float = None,
              tags: str = "") -> int:
    """记录一笔交易及其原因

    Returns:
        journal entry id
    """
    init_journal_table()
    date_str = date_str or date.today().isoformat()
    name = STOCK_MAP.get(code, {}).get("name", code)

    conn = get_conn()
    conn.execute("""
        INSERT INTO trading_journal
            (code, action, date, price, shares, amount, reason, score_at_entry, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (code, action, date_str, price, shares, price * shares,
          reason, score, tags))
    conn.commit()
    entry_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()
    print(f"  📝 [{entry_id}] {action.upper()} {name}({code}) {shares}股 @{price:.2f} — {reason[:40]}")
    return entry_id


def add_reflection(entry_id: int, reflection: str, profit_pct: float = None,
                   score_at_exit: float = None):
    """为已有交易添加事后反思"""
    init_journal_table()
    conn = get_conn()
    updates = ["reflection = ?", "updated_at = datetime('now', 'localtime')"]
    params = [reflection]
    if profit_pct is not None:
        updates.append("profit_pct = ?")
        params.append(profit_pct)
    if score_at_exit is not None:
        updates.append("score_at_exit = ?")
        params.append(score_at_exit)
    params.append(entry_id)
    conn.execute(f"UPDATE trading_journal SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    print(f"  💭 反思已添加到条目 #{entry_id}")


def get_journal(code: str = None, limit: int = 20) -> list[dict]:
    """获取交易日志"""
    init_journal_table()
    conn = get_conn()
    if code:
        rows = conn.execute("""
            SELECT * FROM trading_journal
            WHERE code = ? ORDER BY date DESC, id DESC LIMIT ?
        """, (code, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM trading_journal
            ORDER BY date DESC, id DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """交易日志统计"""
    init_journal_table()
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) as c FROM trading_journal").fetchone()["c"]

    by_action = conn.execute("""
        SELECT action, COUNT(*) as c, ROUND(AVG(profit_pct), 2) as avg_profit
        FROM trading_journal WHERE profit_pct IS NOT NULL
        GROUP BY action
    """).fetchall()

    by_code = conn.execute("""
        SELECT code, COUNT(*) as c, ROUND(AVG(profit_pct), 2) as avg_profit
        FROM trading_journal WHERE profit_pct IS NOT NULL
        GROUP BY code ORDER BY c DESC
    """).fetchall()

    no_reflection = conn.execute(
        "SELECT COUNT(*) as c FROM trading_journal WHERE reflection IS NULL OR reflection = ''"
    ).fetchone()["c"]

    conn.close()
    return {
        "total": total,
        "by_action": [dict(r) for r in by_action],
        "by_code": [dict(r) for r in by_code],
        "no_reflection": no_reflection,
    }


def format_journal(entries: list[dict]) -> str:
    if not entries:
        return "📭 交易日志为空"

    lines = [f"{'':=^60}", "  📝 交易日志", f"{'':=^60}", ""]
    for e in entries:
        name = STOCK_MAP.get(e["code"], {}).get("name", e["code"])
        action_icon = "🟢" if e["action"] == "buy" else "🔴"
        date_str = e["date"][:10]
        lines.append(f"  [{e['id']}] {action_icon} {name}({e['code']}) | {date_str}")
        lines.append(f"      {e['action'].upper()} {e['shares']}股 @{e['price']:.2f} ≈{e['amount']:.0f}元")
        if e["reason"]:
            lines.append(f"      原因: {e['reason']}")
        if e["score_at_entry"]:
            lines.append(f"      买入评分: {e['score_at_entry']:.1f}")
        if e["profit_pct"] is not None:
            emoji = "🟢" if e["profit_pct"] >= 0 else "🔴"
            lines.append(f"      盈亏: {emoji} {e['profit_pct']:+.2f}%")
        if e["reflection"]:
            lines.append(f"      反思: {e['reflection']}")
        if e["tags"]:
            lines.append(f"      标签: {e['tags']}")
        lines.append("")
    return "\n".join(lines)


def format_stats(stats: dict) -> str:
    lines = [f"{'':=^60}", "  📊 交易日志统计", f"{'':=^60}", ""]
    lines.append(f"  总条目: {stats['total']}")
    lines.append(f"  未反思: {stats['no_reflection']}")

    if stats["by_action"]:
        lines.append(f"\n  按操作:")
        for r in stats["by_action"]:
            lines.append(f"    {r['action']:<6} {r['c']}次  "
                         f"平均盈亏{r['avg_profit']:+.2f}%")

    if stats["by_code"]:
        lines.append(f"\n  按标的:")
        for r in stats["by_code"]:
            name = STOCK_MAP.get(r["code"], {}).get("name", r["code"])
            lines.append(f"    {name:<8}({r['code']}) {r['c']}次  "
                         f"平均盈亏{r['avg_profit']:+.2f}%")

    return "\n".join(lines)


# ── CLI ──

def cmd_journal():
    if "--stats" in sys.argv:
        print(format_stats(get_stats()))
        return

    code = None
    for i, a in enumerate(sys.argv):
        if a == "--code" and i + 1 < len(sys.argv):
            code = sys.argv[i + 1]

    entries = get_journal(code=code, limit=30)
    print(format_journal(entries))


def cmd_journal_add():
    """添加交易日志条目 (cli.py trade 完成后自动调用)"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code")
    parser.add_argument("action", choices=["buy", "sell"])
    parser.add_argument("--price", type=float, required=True)
    parser.add_argument("--shares", type=int, required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--score", type=float)
    parser.add_argument("--tags", default="")
    args = parser.parse_args()
    log_trade(args.code, args.action, price=args.price, shares=args.shares,
              reason=args.reason, score=args.score, tags=args.tags)


def cmd_reflect():
    """为交易添加反思"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("id", type=int)
    parser.add_argument("reflection")
    parser.add_argument("--profit", type=float)
    parser.add_argument("--score", type=float)
    args = parser.parse_args()
    add_reflection(args.id, args.reflection, profit_pct=args.profit,
                   score_at_exit=args.score)


if __name__ == "__main__":
    if "--stats" in sys.argv or "--code" in sys.argv:
        cmd_journal()
    elif sys.argv[1:2] == ["add"]:
        sys.argv = sys.argv[1:]
        cmd_journal_add()
    elif sys.argv[1:2] == ["reflect"]:
        sys.argv = sys.argv[1:]
        cmd_reflect()
    else:
        cmd_journal()
