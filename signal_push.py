#!/usr/bin/env python3
"""
Serenity 信号推送 — 评分触发 Telegram 通知
由 cron 定时运行，检查买卖信号变化并推送到 Telegram

用法:
    python3 signal_push.py              # 推送所有信号变化
    python3 signal_push.py --force      # 强制推送（忽略变化检测）
    python3 signal_push.py --test       # 测试 Telegram 连通性
"""
import sys
import os
import json
import subprocess
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import STOCK_MAP, ALL_CODES
from db import get_conn
from portfolio import PortfolioManager

from serenity_logger import get_logger
log = get_logger(__name__)

TELEGRAM_TARGET = "telegram:8703799832"
HERMES_BIN = "/Users/mac/.local/bin/hermes"
STATE_FILE = os.path.join(os.path.dirname(__file__), ".signal_state.json")


import asyncio

TELEGRAM_TOKEN = "8668009256:AAHe8wNCeY85pp4t41A-jBs_EAJqjl_cPuw"
TELEGRAM_CHAT_ID = 8703799832

def send_telegram(message: str) -> bool:
    """通过 python-telegram-bot 发送消息（走代理）"""
    try:
        from telegram import Bot
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.request import HTTPXRequest
        request = HTTPXRequest(proxy='http://127.0.0.1:7890', connect_timeout=15, read_timeout=15)
        bot = Bot(token=TELEGRAM_TOKEN, request=request)
        async def _send():
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        asyncio.run(_send())
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False


def get_latest_scores():
    """获取最新评分及信号"""
    from scorer import score_all
    scores = score_all()
    return scores


def get_portfolio_status():
    """获取组合状态摘要"""
    pm = PortfolioManager()
    pv = pm.get_portfolio_value()
    tt = pm.get_target_tracker()
    return pv, tt


def load_signal_state() -> dict:
    """加载上次推送的信号状态"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_signal_state(scores: list[dict], portfolio: dict):
    """保存当前信号状态"""
    state = {
        "date": date.today().isoformat(),
        "portfolio_total": portfolio["total_value"],
        "signals": {}
    }
    for s in scores:
        state["signals"][s["code"]] = s.get("signal_action", "HOLD")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


def emoji_for_action(action: str) -> str:
    return {
        "STRONG_BUY": "🟢🟢🟢",
        "BUY": "🟢🟢",
        "CAUTION_BUY": "🟢",
        "HOLD": "⚪",
        "WATCH": "🟡",
        "WEAK_HOLD": "🟠",
        "SELL": "🔴🔴",
        "STOP_LOSS": "🔴🔴🔴",
    }.get(action, "⚪")


def build_signal_message(scores, pv, tt, changed_only=True) -> str:
    """构建推送消息"""
    now = datetime.now().strftime("%H:%M")
    lines = [f"📡 Serenity 信号 {date.today()} {now}", ""]

    # 组合概览
    lines.append(f"💰 总资产: {pv['total_value']:,.0f} | 现金: {pv['cash']:,.0f}")
    lines.append(f"📈 总盈亏: {pv['total_profit_pct']:+.2f}%")
    lines.append(f"🎯 目标进度: {tt['progress_pct']:.1f}% (需月{tt['required_monthly_return']:+.1f}%)")
    lines.append("")

    # 持仓信号
    positions = pv.get("positions", [])
    held_codes = {p["code"] for p in positions}
    if positions:
        lines.append("📊 持仓:")
        for p in positions:
            sig = next((s for s in scores if s["code"] == p["code"]), {})
            action = sig.get("signal_action", "HOLD")
            emoji = emoji_for_action(action)
            lines.append(f"  {emoji} {p['name']} {p['profit_pct']:+.1f}% → {action}")

    # 信号变化
    old_state = load_signal_state()
    lines.append("")
    lines.append("🔔 信号:")
    buy_signals = [s for s in scores if s.get("signal_action") in ("BUY", "STRONG_BUY", "CAUTION_BUY")]
    sell_signals = [s for s in scores if s.get("signal_action") in ("SELL", "STOP_LOSS")]

    for s in buy_signals:
        code = s["code"]
        old = old_state.get("signals", {}).get(code, "HOLD")
        if not changed_only or old != s["signal_action"]:
            lines.append(f"  🟢 {s['name']} {s['total_score']:.0f}分 {s['signal_action']}")

    for s in sell_signals:
        code = s["code"]
        old = old_state.get("signals", {}).get(code, "HOLD")
        if not changed_only or old != s["signal_action"]:
            lines.append(f"  🔴 {s['name']} {s['total_score']:.0f}分 {s['signal_action']}")

    if not buy_signals and not sell_signals:
        lines.append("  无新增买卖信号")

    return "\n".join(lines)


def send_hermes(platform_target: str, message: str) -> bool:
    """通过 Hermes CLI 发送消息到指定平台"""
    import subprocess
    try:
        result = subprocess.run(
            [HERMES_BIN, "send", "--to", platform_target, message],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return True
        log.warning("Hermes %s 失败: %s", platform_target, result.stderr.strip() or result.stdout.strip())
        return False
    except Exception as e:
        log.error("Hermes %s 异常: %s", platform_target, e, exc_info=True)
        return False


def push_execution_plan(plan: dict = None) -> bool:
    """推送执行计划到 Telegram，附带 Dashboard 确认链接"""
    try:
        from auto_execute import generate_execution_plan
        if plan is None:
            plan = generate_execution_plan(dry_run=True)

        # 读取 Dashboard 公网 URL
        import os
        url_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.serenity_public_url')
        dashboard_url = 'http://localhost:8401/monitor'
        if os.path.exists(url_file):
            with open(url_file) as f:
                dashboard_url = f.read().strip() + '/monitor'

        sells = plan.get('sells', [])
        buys = plan.get('buys', [])
        if not sells and not buys:
            return send_telegram(f"📊 Serenity {plan['date']}\n无操作建议，继续持有。")

        lines = [f"📊 <b>Serenity 执行计划 | {plan['date']}</b>", ""]

        if sells:
            lines.append("🔴 <b>卖出:</b>")
            for s in sells:
                lines.append(f"  {s['name']} {s['shares']}股 ~¥{s['estimated_proceeds']:,.0f}")
                lines.append(f"  └ {(s.get('reasons',[''])[0])[:60]}")

        if buys:
            lines.append("🟢 <b>买入:</b>")
            for b in buys:
                lines.append(f"  {b['name']} {b['shares']}股 @{b['price']:.2f} ≈¥{b['amount']:,.0f}")
                lines.append(f"  └ 评分{b.get('score',0):.0f} {b.get('signal','')}")

        lines.append(f"💰 现金: ¥{plan['cash']:,.0f}")
        lines.append("")
        lines.append(f'👉 <a href="{dashboard_url}">打开 Dashboard 确认执行</a>')

        return send_telegram('\n'.join(lines))
    except Exception as e:
        log.error("push_execution_plan failed: %s", e, exc_info=True)
        return False

def main():

    force = "--force" in sys.argv
    test_mode = "--test" in sys.argv

    if test_mode:
        tg_ok = send_telegram("🧪 Serenity 信号推送测试 — Telegram ✓")
        log.info("Test %s", "OK" if tg_ok else "FAILED")
        return

    scores = get_latest_scores()
    pv, tt = get_portfolio_status()
    message = build_signal_message(scores, pv, tt, changed_only=not force)
    save_signal_state(scores, pv)

    # Telegram 直推（微信已禁用 Serenity 推送）
    tg_ok = send_telegram(message)
    if tg_ok:
        log.info("Telegram 推送成功")
    else:
        log.error("Telegram 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
