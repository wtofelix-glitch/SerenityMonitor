#!/usr/bin/env python3
"""
Serenity 信号实时推送 — 检测可执行信号并推送微信
用法: python3 signal_push.py    # 推送信号简报
      python3 signal_push.py --silent  # 无信号时静默（cron用）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date
from scorer import score_all
from portfolio import PortfolioManager
from signal_engine import generate_signals
from notifier import send_message
from config import STOCK_MAP


def build_signal_brief():
    """构建信号简报"""
    scores = score_all()
    pf = PortfolioManager().get_portfolio_value()
    held_codes = {p["code"] for p in pf.get("position_details", [])}

    buy_candidates = []
    risk_alerts = []

    for s in scores:
        code = s["code"]
        score = s["total_score"]
        action = s.get("signal_action", "HOLD")
        confidence = s.get("signal_confidence", 0)

        if code not in held_codes:
            if score >= 60 and action in ("BUY", "CAUTION_BUY", "STRONG_BUY"):
                buy_candidates.append({
                    "code": code, "name": s["name"],
                    "score": score, "action": action,
                    "confidence": confidence,
                    "price": s.get("price", 0),
                })
        else:
            if action in ("SELL", "STOP_LOSS") or score < 50:
                risk_alerts.append({
                    "code": code, "name": s["name"],
                    "score": score, "action": action,
                    "price": s.get("price", 0),
                    "cost": next((p["buy_price"] for p in pf["position_details"] if p["code"] == code), 0),
                })

    return {
        "buy_candidates": buy_candidates,
        "risk_alerts": risk_alerts,
        "pf_summary": pf,
    }


def format_push_message(brief, silent=False):
    """格式化推送消息"""
    buy = brief["buy_candidates"]
    risk = brief["risk_alerts"]
    pf = brief["pf_summary"]

    if not buy and not risk:
        if silent:
            return None
        return "✅ Serenity 信号监控：无异常信号"

    today = date.today().isoformat()
    lines = [f"📡 **Serenity 实时信号** | {today}", ""]

    # 组合概览
    if pf:
        lines.append(
            f"💰 总权益 ¥{pf.get('total_value', 0):,.0f} | "
            f"浮盈 {pf.get('total_profit_pct', 0):+.1f}%"
        )
        lines.append("")

    if buy:
        lines.append(f"### 🟢 买入候选 ({len(buy)}只)")
        for s in sorted(buy, key=lambda x: x["score"], reverse=True):
            lines.append(
                f"- **{s['name']}** ({s['code']}) "
                f"评分{s['score']:.0f} | {s['action']}"
            )
            if s.get("price"):
                lines.append(f"  💰 现价 ¥{s['price']:.2f} | 信度{s['confidence']:.0f}%")
        lines.append("")

    if risk:
        lines.append(f"### 🔴 风险提醒 ({len(risk)}只)")
        for s in risk:
            extra = ""
            if s.get("cost") and s.get("price"):
                pnl = (s["price"] - s["cost"]) / s["cost"] * 100
                extra = f" | 浮动{pnl:+.1f}%"
            lines.append(
                f"- ⚠️ **{s['name']}** ({s['code']}) "
                f"评分{s['score']:.0f}{extra}"
            )
        lines.append("")

    lines.append("---")
    lines.append(f"⏰ 下次检查: 30分钟后")
    return "\n".join(lines)


if __name__ == "__main__":
    silent = "--silent" in sys.argv
    brief = build_signal_brief()
    msg = format_push_message(brief, silent=silent)

    if msg is None:
        sys.exit(0)  # 静默退出

    print(msg)

    # 推到 stdout（cron no_agent 会通过微信通道送达）— 已由 print 完成
    # notifier 三通道作为备用（静默失败不影响主输出）
    try:
        title = f"📡 Serenity 信号 | {date.today().isoformat()}"
        result = send_message(title, msg, content_type="markdown")
    except Exception:
        pass  # notifier 不通时依赖 cron 微信通道
