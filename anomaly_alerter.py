#!/usr/bin/env python3
"""
Serenity 异动预警 — 持仓价格急跌/放量监控
由 cron 定时运行，检查持仓异常并通过推送通知

用法: python3 anomaly_alerter.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime
from data_engine import fetch_realtime
from portfolio import PortfolioManager
from config import STOCK_MAP
from db import get_conn


def check_anomalies():
    """检查持仓异动"""
    pm = PortfolioManager()
    pv = pm.get_portfolio_value()
    positions = pv.get("positions", [])
    
    if not positions:
        return []
    
    codes = [p["code"] for p in positions if p["code"] != "CASH"]
    if not codes:
        return []
    try:
        realtime = fetch_realtime(codes)
    except Exception as e:
        print(f"[WARN] fetch_realtime失败(收盘后正常): {e}", file=sys.stderr)
        return []  # 优雅降级：无实时数据 = 无不报警
    rt_map = {r["code"]: r for r in realtime}
    
    alerts = []
    
    for pos in positions:
        code = pos["code"]
        if code == "CASH":
            continue  # CASH 不是股票，不参与异常检测
        name = pos.get("name", STOCK_MAP.get(code, {}).get("name", code))
        rt = rt_map.get(code, {})
        price = rt.get("price", 0)
        change_pct = (price - rt.get("close_yesterday", price)) / rt.get("close_yesterday", price) * 100 if rt.get("close_yesterday", 0) > 0 else 0
        volume = rt.get("volume", 0)
        
        # 1. 急跌预警（单日跌超5%）
        if change_pct <= -5:
            alerts.append({
                "level": "CRITICAL",
                "code": code, "name": name,
                "type": "PRICE_DROP",
                "message": f"🔴 {name} 急跌 {change_pct:.1f}%，现价 {price:.2f}",
                "price": price, "change_pct": round(change_pct, 2),
            })
        
        # 2. 大幅下跌预警（跌超3%）
        elif change_pct <= -3:
            alerts.append({
                "level": "WARNING",
                "code": code, "name": name,
                "type": "PRICE_DROP",
                "message": f"🟠 {name} 下跌 {change_pct:.1f}%，现价 {price:.2f}",
                "price": price, "change_pct": round(change_pct, 2),
            })
        
        # 3. 止损触发
        buy_price = pos.get("buy_price", 0)
        if buy_price > 0:
            loss_pct = (price - buy_price) / buy_price * 100
            if loss_pct <= -8:
                alerts.append({
                    "level": "CRITICAL",
                    "code": code, "name": name,
                    "type": "STOP_LOSS",
                    "message": f"🔴 {name} 触及止损线！亏损 {loss_pct:.1f}%（成本 {buy_price:.2f}）",
                    "price": price, "loss_pct": round(loss_pct, 2),
                })
            elif loss_pct <= -5:
                alerts.append({
                    "level": "WARNING",
                    "code": code, "name": name,
                    "type": "APPROACHING_STOP",
                    "message": f"🟠 {name} 接近止损，亏损 {loss_pct:.1f}%（成本 {buy_price:.2f}）",
                    "price": price, "loss_pct": round(loss_pct, 2),
                })
        
        # 4. 成交量异常（3倍以上）
        avg_vol = _get_avg_volume(code, 10)
        if avg_vol > 0 and volume > avg_vol * 3:
            alerts.append({
                "level": "INFO",
                "code": code, "name": name,
                "type": "VOLUME_SURGE",
                "message": f"📊 {name} 成交量异常放大 {volume/avg_vol:.1f}x",
                "price": price, "vol_ratio": round(volume / avg_vol, 1),
            })
    
    # 保存到数据库
    if alerts:
        conn = get_conn()
        today = date.today().isoformat()
        for a in alerts:
            conn.execute("""
                INSERT INTO anomalies (code, level, alert_type, price, message, data)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (a["code"], a["level"], a["type"], a["price"], a["message"],
                  str({"change_pct": a.get("change_pct"), "loss_pct": a.get("loss_pct"), "vol_ratio": a.get("vol_ratio")})))
        conn.commit()
        conn.close()
    
    return alerts


def _get_avg_volume(code: str, days: int = 10) -> float:
    """获取平均成交量"""
    from db import get_price_history
    rows = get_price_history(code, days)
    if not rows:
        return 0
    volumes = [r.get("volume", 0) for r in rows if r.get("volume")]
    return sum(volumes) / len(volumes) if volumes else 0


def format_alert_message(alerts: list) -> str:
    """格式化预警消息"""
    if not alerts:
        return ""
    
    lines = [f"⚠️ Serenity 异动预警 {datetime.now().strftime('%H:%M')}", ""]
    
    critical = [a for a in alerts if a["level"] == "CRITICAL"]
    warning = [a for a in alerts if a["level"] == "WARNING"]
    info = [a for a in alerts if a["level"] == "INFO"]
    
    if critical:
        lines.append("🚨 紧急:")
        for a in critical:
            lines.append(f"  {a['message']}")
        lines.append("")
    
    if warning:
        lines.append("⚠️ 关注:")
        for a in warning:
            lines.append(f"  {a['message']}")
        lines.append("")
    
    if info:
        lines.append("📊 提示:")
        for a in info:
            lines.append(f"  {a['message']}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    alerts = check_anomalies()
    if alerts:
        msg = format_alert_message(alerts)
        print(msg)
        # Push via Telegram bot (proxy)
        try:
            import asyncio
            from telegram import Bot
            from telegram.request import HTTPXRequest
            request = HTTPXRequest(proxy='http://127.0.0.1:7890', connect_timeout=15, read_timeout=15)
            bot = Bot(token="8668009256:AAHe8wNCeY85pp4t41A-jBs_EAJqjl_cPuw", request=request)
            async def _send():
                await bot.send_message(chat_id=8703799832, text=msg)
            asyncio.run(_send())
        except Exception:
            pass
    else:
        # 无异常不推送 — 空输出让 cron 静默跳过
        pass
