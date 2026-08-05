#!/usr/bin/env python3
"""
Stale execution order reminder.

Checks ``execution_log`` for pending orders older than a threshold and
generates a push notification so the operator doesn't forget to execute them.
"""

import sys
import os
from datetime import datetime, timedelta

PROJECT = os.path.dirname(os.path.abspath(__file__))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from db import get_conn
from notifier import send_message


def _since(label: str, ts: str) -> str:
    """Human-friendly 'N days / hours ago' from ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return "unknown"
    delta = datetime.now() - dt
    if delta.days > 0:
        return f"{delta.days}d ago"
    hours = delta.total_seconds() / 3600
    if hours >= 1:
        return f"{int(hours)}h ago"
    return f"{int(delta.total_seconds() // 60)}m ago"


def check_pending(max_age_hours: int = 4) -> list[dict]:
    """Return pending orders older than *max_age_hours*."""
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    rows = conn.execute(
        """SELECT id, date, code, action, status, price, shares, amount,
                  reason, created_at
           FROM execution_log
           WHERE status = 'pending'
             AND created_at < ?
           ORDER BY created_at""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def expire_stale(days: int = 7, dry_run: bool = True) -> int:
    """Mark pending orders older than *days* as 'expired'."""
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    if dry_run:
        row = conn.execute(
            "SELECT COUNT(*) FROM execution_log WHERE status='pending' AND created_at < ?",
            (cutoff,),
        ).fetchone()
        count = row[0] if row else 0
        conn.close()
        return count
    conn.execute(
        "UPDATE execution_log SET status='expired' WHERE status='pending' AND created_at < ?",
        (cutoff,),
    )
    affected = conn.total_changes
    conn.commit()
    conn.close()
    return affected


def main():
    # Auto-expire orders older than 14 days (positions likely changed)
    stale_count = expire_stale(days=14, dry_run=True)
    expire = "--expire" in sys.argv
    if expire and stale_count > 0:
        expired = expire_stale(days=14, dry_run=False)
        print(f"过期 {expired} 笔超14天订单。")

    pending = check_pending(max_age_hours=4)
    if not pending:
        print("No recent pending orders.")
        if stale_count > 0 and not expire:
            print(f"(另有 {stale_count} 笔超14天待处理 → 加 --expire 自动过期)")
        return

    # Separate recent vs very old
    recent = [p for p in pending if (datetime.now() - datetime.fromisoformat(p["created_at"])).days <= 14]
    ancient = [p for p in pending if (datetime.now() - datetime.fromisoformat(p["created_at"])).days > 14]

    if ancient:
        print(f"⚠️  {len(ancient)} 笔超14天待执行（建议 --expire）:")
        for p in ancient[:3]:
            print(f"  {p['code']} {p['action']} {int(p['shares'])}股 ({_since('', p['created_at'])})")
        if len(ancient) > 3:
            print(f"  ... 及其他 {len(ancient)-3} 笔")
        print()

    if not recent:
        print("无近期待执行订单。")
        return

    # Build reminder message
    lines = ["📋 **待执行调仓提醒**", ""]
    for p in recent[:5]:  # Cap at 5 for readability
        code = p["code"]
        try:
            from config import get_stock_name
            name = get_stock_name(code)
        except Exception:
            name = code
        lines.append(
            f"- **{name}** ({code}): {p['action']} {int(p['shares'])}股 "
            f"→ ¥{p['amount']:.0f}"
        )
        lines.append(f"  {p['reason'][:60]}")
        lines.append("")

    lines.append(f"共 {len(recent)} 笔近期待执行。")
    if stale_count > 0 and not expire:
        lines.append(f"(另有 {stale_count} 笔超14天 → 加 --expire 清理)")
    message = "\n".join(lines)

    push = "--push" in sys.argv
    if push:
        send_message(title="Serenity 待执行提醒", content=message)
        print(f"Pushed reminder for {len(recent)} pending orders.")
    else:
        print(message)
        print("\n附 --push 以推送提醒。")

    # Build reminder message
    lines = ["📋 **待执行调仓提醒**", ""]
    for p in pending:
        code = p["code"]
        name = "?"
        try:
            from config import get_stock_name
            name = get_stock_name(code)
        except Exception:
            pass
        lines.append(
            f"- **{name}** ({code}): {p['action']} {int(p['shares'])}股 "
            f"@{p['price']:.2f} → ¥{p['amount']:.0f}"
        )
        lines.append(f"  原因: {p['reason'][:60]}")
        lines.append(f"  创建: {p['created_at']} ({_since('created', p['created_at'])})")
        lines.append("")

    lines.append(f"共 {len(pending)} 笔待执行，请尽快处理。")
    message = "\n".join(lines)

    push = "--push" in sys.argv
    if push:
        send_message(title="Serenity 待执行提醒", content=message)
        print(f"Pushed reminder for {len(pending)} pending orders.")
    else:
        print(message)
        print("\n附 --push 以推送提醒。")


if __name__ == "__main__":
    main()
