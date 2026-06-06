"""
交易记录 — 记录你的真实买卖操作，后续与系统信号对比
不修改持仓/仓位，只做记录
"""
import os
import json
from datetime import date, datetime
from typing import Optional

LOG_DIR = os.path.expanduser("~/Documents/trading_logs/user_trades")
os.makedirs(LOG_DIR, exist_ok=True)


def get_log_path() -> str:
    """每天一个文件"""
    return os.path.join(LOG_DIR, f"{date.today().isoformat()}.jsonl")


def record_trade(action: str, code: str, price: float, amount: float = 0,
                 name: str = "", note: str = "") -> dict:
    """
    记录一次手动交易
    action: BUY / SELL
    code: 股票代码
    price: 成交价
    amount: 成交金额
    name: 股票名称（可选）
    note: 备注（可选）
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "action": action.upper(),
        "code": code,
        "name": name or code,
        "price": price,
        "amount": amount,
        "shares": round(amount / price, 0) if price > 0 else 0,
        "note": note,
    }

    path = get_log_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def get_today_trades() -> list:
    """获取今日交易记录"""
    path = get_log_path()
    if not os.path.exists(path):
        return []
    trades = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def get_all_trades(days: int = 30) -> list:
    """获取近N天交易记录"""
    from datetime import timedelta
    start = date.today() - timedelta(days=days)
    trades = []
    root = LOG_DIR
    for fname in sorted(os.listdir(root)):
        fdate = fname.replace(".jsonl", "")
        if fdate >= start.isoformat():
            with open(os.path.join(root, fname), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
    return trades


def compare_with_signals(days: int = 30) -> str:
    """对比用户交易 vs 系统信号"""
    from db import get_recent_signals
    user_trades = get_all_trades(days)

    if not user_trades:
        return "📭 近30天无用户交易记录"

    lines = []
    lines.append(f"📊 **交易记录 vs 系统信号对比（近{days}天）**")
    lines.append("")

    for t in user_trades:
        code = t["code"]
        trade_date = t["date"]
        trade_action = t["action"]

        # 查询系统当天信号
        signals = get_recent_signals(code, days=3, limit=10)
        sys_sigs = [s for s in signals if s["date"] == trade_date]

        lines.append(f"  {t['name']}({code}) | {trade_date} | {'🟢' if trade_action=='BUY' else '🔴'} {trade_action} @ {t['price']:.2f}")

        if sys_sigs:
            for sig in sys_sigs:
                act = sig.get("action", "?")
                sc = sig.get("total_score", 0)
                lines.append(f"    → 系统信号: {act} (总分{sc:.0f})")
        else:
            lines.append(f"    → 当日无系统信号记录")

        if t.get("note"):
            lines.append(f"    📝 {t['note']}")
        lines.append("")

    lines.append("---")
    lines.append(f"共 {len(user_trades)} 笔交易")

    return "\n".join(lines)


def cmd_trade_record(args: list):
    """CLI入口: python3 cli.py trade-record BUY 002281 224 5000 '加仓'"""
    if not args or args[0] in ("-h", "--help", ""):
        print("用法: python3 cli.py trade-record <BUY|SELL> <code> <price> [amount] [note]")
        print("示例: python3 cli.py trade-record BUY 002281 224 5000 '加仓'")
        return

    action = args[0].upper()
    if action not in ("BUY", "SELL"):
        print(f"❌ action 必须是 BUY 或 SELL，收到: {action}")
        return

    code = args[1] if len(args) > 1 else ""
    if not code:
        print("❌ 请提供股票代码")
        return

    try:
        price = float(args[2]) if len(args) > 2 else 0
    except ValueError:
        print("❌ price 必须是数字")
        return

    amount = float(args[3]) if len(args) > 3 and args[3].replace(".", "").isdigit() else 0
    note = " ".join(args[4:]) if len(args) > 4 else ""

    # 尝试获得股票名称
    try:
        from config import STOCK_MAP
        name = STOCK_MAP.get(code, {}).get("name", code)
    except Exception:
        name = code

    entry = record_trade(action, code, price, amount, name, note)
    print(f"✅ 已记录: {action} {name}({code}) @ {price:.2f} × {entry['shares']:.0f}股 = {amount:.0f}元")


def cmd_trade_log(args: list):
    """查看交易记录"""
    if args and args[0] == "--compare":
        print(compare_with_signals())
        return

    trades = get_today_trades()
    if not trades:
        all_trades = get_all_trades(7)
        if not all_trades:
            print("📭 无交易记录")
            return
        print(f"📋 近7天交易记录（共{len(all_trades)}笔）")
        for t in all_trades:
            emoji = "🟢" if t["action"] == "BUY" else "🔴"
            print(f"  {emoji} {t['name']}({t['code']}) {t['action']} @ {t['price']:.2f} | {t['date']}")
        return

    print(f"📋 今日交易记录（{len(trades)}笔）")
    for t in trades:
        emoji = "🟢" if t["action"] == "BUY" else "🔴"
        print(f"  {emoji} {t['name']}({t['code']}) {t['action']} @ {t['price']:.2f} × {t['shares']:.0f}股 = {t['amount']:.0f}元")
        if t.get("note"):
            print(f"    📝 {t['note']}")
