#!/usr/bin/env python3
"""
盘前简报生成器 — 每天 08:30 自动运行
内容: 隔夜外盘 · 持仓关键价位 · 今日预期信号 · 风险清单
推送: 微信/Telegram
"""

from datetime import date, datetime
import json, os
from serenity_logger import get_logger

log = get_logger(__name__)


def generate_premarket_brief() -> str:
    """生成盘前简报 Markdown"""
    today = date.today()
    lines = []
    lines.append(f"☀️ *盘前简报* — {today.strftime('%m月%d日')} {datetime.now().strftime('%H:%M')}")
    lines.append("")

    # ═══ 1. 隔夜外盘 ═══
    try:
        import urllib.request, re
        # 新浪美股指数(标普500/DJI/纳指)
        url = "https://hq.sinajs.cn/list=gb_$dji,gb_$ixic,gb_$inx"
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("gbk")
        lines.append("*🌍 隔夜外盘*")
        names = ["道琼斯", "纳斯达克", "标普500"]
        for i, name in enumerate(names):
            parts = raw.split("\n")[i]
            vars_list = parts.split("=", 1)
            if len(vars_list) == 2:
                vals = vars_list[1].strip('"').split(",")
                if len(vals) >= 4:
                    price = float(vals[1])
                    prev_close = float(vals[2])
                    change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
                    icon = "📈" if change_pct > 0 else "📉"
                    lines.append(f"  {icon} {name}: {price:.0f} ({change_pct:+.2f}%)")
        lines.append("")
    except Exception:
        lines.append("*🌍 隔夜外盘*: 数据获取失败")
        lines.append("")

    # ═══ 2. 持仓关键价位 ═══
    lines.append("*💼 持仓关键价位*")
    try:
        from db import get_conn, load_all_stocks
        from data_engine import fetch_realtime

        stocks = [s for s in load_all_stocks() if s.get("is_active") and s["code"] != "CASH"]
        if stocks:
            codes = [s["code"] for s in stocks]
            quotes = fetch_realtime(codes, source="auto")
            quote_map = {q["code"]: q for q in quotes} if quotes else {}

            for s in stocks:
                code = s["code"]
                name = s.get("name", code)
                q = quote_map.get(code, {})
                price = q.get("price", 0) if q else 0
                buy_price = float(s.get("buy_price", 0))
                change = ((price - buy_price) / buy_price * 100) if buy_price and price else 0
                shares = int(float(s.get("trade_amount", 0)) / buy_price) if buy_price else 0
                value = price * shares

                # 目标价/止损线
                target_high = float(s.get("target_high", 0))
                stop_loss = float(s.get("stop_loss", 0))

                icon = "🟢" if change > 0 else "🔴"
                lines.append(f"  {icon} {name}({code}): ¥{price:.2f} ({change:+.1f}%) | 市值 ¥{value:,.0f}")
                if target_high > 0:
                    dist_target = (target_high - price) / price * 100 if price else 0
                    lines.append(f"    🎯 目标 ¥{target_high:.2f} (距 {dist_target:+.1f}%)")
                if stop_loss > 0 and price < stop_loss * 1.05:
                    lines.append(f"    ⚠️ 止损警戒! 止损线 ¥{stop_loss:.2f} (距 {(price-stop_loss)/price*100:.0f}%)")
        else:
            lines.append("  暂无持仓")
        lines.append("")
    except Exception as e:
        lines.append(f"  ⚠️ 持仓数据获取失败")
        lines.append("")

    # ═══ 3. 今日预期信号 ═══
    lines.append("*📡 今日预期信号*")
    try:
        from db import get_conn
        conn = get_conn()
        # 最近交易日的评分
        latest_scores = conn.execute(
            "SELECT sh.code, s.name, sh.total_score, sh.uzi_score FROM scoring_history sh "
            "JOIN stocks s ON s.code=sh.code WHERE sh.date=(SELECT MAX(date) FROM scoring_history) "
            "ORDER BY sh.total_score DESC LIMIT 8"
        ).fetchall()

        for r in latest_scores:
            score = r["total_score"]
            icon = "🔥" if score >= 65 else "💡" if score >= 55 else "👀"
            lines.append(f"  {icon} {r['name']}({r['code']}): {score:.0f}分 UZI{r['uzi_score']:.0f}")
        conn.close()
    except Exception:
        lines.append("  ⚠️ 信号数据获取失败")
    lines.append("")

    # ═══ 4. 风险清单 ═══
    lines.append("*⚠️ 风险提示*")
    try:
        from auto_gate import get_latest_gate_result
        gate = get_latest_gate_result()
        if gate:
            state = gate.get("state", "LOCKED")
            reasons = gate.get("reasons", [])[:3]
            lines.append(f"  🔒 闸门: {state}")
            for r in reasons:
                lines.append(f"    · {r}")

        # 检查连续亏损
        conn = __import__('db').get_conn()
        risk_row = conn.execute(
            "SELECT COUNT(*) as n FROM signal_log WHERE date >= date('now','-5 days') AND action IN ('SELL','STOP_LOSS') AND outcome_5d IS NOT NULL AND outcome_5d < 0"
        ).fetchone()
        conn.close()
        if risk_row and risk_row["n"] >= 2:
            lines.append(f"  🚨 连续亏损: 近5日 {risk_row['n']} 笔卖出信号亏损")
    except Exception:
        pass

    lines.append("")
    lines.append(f"*⏰ 简报生成于 {datetime.now().strftime('%H:%M')} · 数据源: 新浪财经/腾讯*")

    return "\n".join(lines)


def push_premarket_brief(brief: str = None) -> dict:
    """推送盘前简报"""
    if brief is None:
        brief = generate_premarket_brief()

    results = {"telegram": False, "wechat": False, "file": ""}

    # 保存
    today_str = date.today().isoformat()
    brief_dir = "/Users/mac/workspace/SerenityMonitor/reports"
    os.makedirs(brief_dir, exist_ok=True)
    filename = f"{brief_dir}/premarket_{today_str}.md"
    with open(filename, "w") as f:
        f.write(brief)
    results["file"] = filename

    # Telegram 推送
    try:
        import subprocess
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not tg_token or not tg_chat:
            return {"telegram": False, "wechat": False, "file": ""}
        safe = brief.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        cmd = f'curl -s -X POST "https://api.telegram.org/bot{tg_token}/sendMessage" -d "chat_id={tg_chat}" -d "text={safe}" -d "parse_mode=Markdown" --max-time 10'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        results["telegram"] = "ok" in result.stdout.lower()
    except Exception:
        pass

    # 微信投递队列
    try:
        delivery_path = os.path.expanduser("~/.hermes/pending_deliveries/premarket_brief.md")
        os.makedirs(os.path.dirname(delivery_path), exist_ok=True)
        with open(delivery_path, "w") as f:
            f.write(brief)
        results["wechat"] = True
    except Exception:
        pass

    return results


if __name__ == "__main__":
    brief = generate_premarket_brief()
    print(brief)
    import sys
    if "--push" in sys.argv:
        push_premarket_brief(brief)
