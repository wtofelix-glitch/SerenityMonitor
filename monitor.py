"""
Serenity Monitor — 盘中实时监控引擎
检测异常行为并在暴雷前发出预警
"""
from datetime import date, datetime
import json

from data_engine import fetch_realtime, fetch_single
from config import STOCK_DETAILS, STOCK_MAP
from db import (load_all_stocks, get_price_history, get_avg_volume,
                add_anomaly, get_today_anomalies, get_stock)
from portfolio import get_portfolio
from notifier import push_alert


# ========== 异常检测阈值 ==========

THRESHOLDS = {
    "A_drop": -5.0,         # 单日跌幅超5% → 紧急卖出
    "B_drop": -3.0,         # 单日跌幅超3% → 重点关注
    "A_volume": 4.0,        # 成交量超均值4倍 → 异常放量
    "B_volume": 2.5,        # 成交量超均值2.5倍 → 关注
    "max_consecutive_decline": 3,  # 连续3次检测下跌 → 预警
    "news_negative_hours": 6,      # 新闻搜索覆盖最近N小时
}


def check_price_drop(code: str, name: str, price: float, change_pct: float) -> list[dict]:
    """价格跌幅检测"""
    alerts = []

    if change_pct <= THRESHOLDS["A_drop"]:
        msg = (f"🚨🔴 **{name}({code}) 紧急卖出预警！**\n"
               f"单日暴跌 {change_pct:.1f}%，当前 {price:.2f} 元\n"
               f"建议立即评估止损！")
        alerts.append({"level": "A", "type": "price_drop", "msg": msg})

    elif change_pct <= THRESHOLDS["B_drop"]:
        # 检查成交量是否配合
        data = fetch_single(code)
        volume = data.get("volume", 0) if data else 0
        avg_vol = get_avg_volume(code, 10)
        vol_ratio = volume / avg_vol if avg_vol > 0 else 1

        if vol_ratio >= THRESHOLDS["B_volume"]:
            msg = (f"⚠️🔴 **{name}({code}) 下跌放量！**\n"
                   f"跌幅 {change_pct:.1f}%，成交量{vol_ratio:.1f}倍均值\n"
                   f"当前 {price:.2f} 元，密切监控")
            alerts.append({"level": "B", "type": "volume_surge", "msg": msg})
        else:
            msg = (f"⚠️ **{name}({code}) 下跌 {change_pct:.1f}%**\n"
                   f"当前 {price:.2f} 元，成交量正常\n"
                   f"建议关注")
            alerts.append({"level": "C", "type": "price_drop", "msg": msg})

    return alerts


def check_consecutive_decline(code: str, name: str, price: float, snapshots: list) -> list[dict]:
    """智能连续下跌检测（含反弹过滤）"""
    alerts = []
    recent_snaps = get_price_history(code, 5)

    if len(recent_snaps) >= 3:
        # 最近N次中的下跌次数
        decline_count = sum(1 for s in recent_snaps if s.get("change_pct", 0) < 0)

        # 最新一次涨跌方向 — 反弹中则不报警
        latest_change = recent_snaps[0].get("change_pct", 0)  # recent_snaps[0] = 最新
        is_bouncing = latest_change >= 2.0  # 今日反弹 > 2% 则不报警

        # 总累积跌幅
        total_decline = sum(s["change_pct"] for s in recent_snaps if s.get("change_pct", 0) < 0)

        if decline_count >= 3 and not is_bouncing and total_decline <= -8:
            abs_decline = abs(total_decline)
            label = f"{name}({code})"
            msg = (f"⚠️ **{label} 连续下跌预警！**\n"
                   f"最近{len(recent_snaps)}个交易日中{decline_count}次收跌\n"
                   f"累计跌幅 {abs_decline:.1f}%\n"
                   f"当前 {price:.2f} 元\n"
                   f"建议检查持仓逻辑是否改变")
            alerts.append({"level": "C", "type": "consecutive_decline", "msg": msg})
        elif decline_count >= 4 and not is_bouncing:
            # 极端连续下跌（4/5）即使跌幅不够也报警
            label2 = f"{name}({code})"
            msg = (f"⚠️ **{label2} 频繁下跌预警！**\n"
                   f"最近5日{decline_count}次收跌, 今日{latest_change:+.2f}%\n"
                   f"当前 {price:.2f} 元\n"
                   f"关注是否破位")
            alerts.append({"level": "C", "type": "consecutive_decline", "msg": msg})

    return alerts


def check_volume_anomaly(code: str, name: str, price: float, volume: float) -> list[dict]:
    """成交量异常检测"""
    alerts = []
    avg_vol = get_avg_volume(code, 10)
    if avg_vol <= 0:
        return alerts

    ratio = volume / avg_vol if avg_vol > 0 else 1

    if ratio >= THRESHOLDS["A_volume"]:
        msg = (f"🚨 **{name}({code}) 巨量异常！**\n"
               f"成交量{ratio:.1f}倍均值 ({volume:.0f} vs {avg_vol:.0f})\n"
               f"当前 {price:.2f} 元\n"
               f"警惕主力出货/换手！")
        alerts.append({"level": "B", "type": "volume_surge", "msg": msg})

    return alerts


def check_technical_breach(code: str, name: str, price: float, low: float) -> list[dict]:
    """技术面破位检测"""
    alerts = []
    recent = get_price_history(code, 10)
    if len(recent) < 3:
        return alerts

    # 计算近期最低价（不包括今天）
    recent_lows = [s["low"] for s in recent if s.get("low") and s["low"] > 0]
    if not recent_lows:
        return alerts

    support_level = min(recent_lows)

    if price < support_level * 0.97:  # 跌破近期低点3%
        msg = (f"⚠️ **{name}({code}) 跌破技术支撑！**\n"
               f"当前 {price:.2f} 跌破近期低点 {support_level:.2f}\n"
               f"技术面走弱，建议关注")
        alerts.append({"level": "B", "type": "technical_breach", "msg": msg})

    return alerts


def scan_negative_news(code: str, name: str, price: float) -> list[dict]:
    """
    扫描负面新闻（Black Swan检测）
    通过搜索关键词获取最新消息
    """
    alerts = []
    try:
        # 这里预留web_search接口，当前返回空
        # 实际使用时可以通过 TrendRadar 或 web_search 扫描
        # 例如：web_search(f"{name} {code} 利空 暴雷 负面 突发")
        pass
    except Exception:
        pass
    return alerts


def monitor_all() -> dict:
    """
    全面监控所有持仓标的
    返回异常检测结果
    """
    stocks = load_all_stocks()
    active = [s for s in stocks if s["is_active"]]
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M")

    if not active:
        return {"status": "empty", "message": "当前无持仓，无需监控"}

    # 批量获取实时行情
    snapshots = fetch_realtime([s["code"] for s in active])
    snapshot_map = {s["code"]: s for s in snapshots}

    all_alerts = []
    for stock in active:
        code = stock["code"]
        name = stock["name"]
        data = snapshot_map.get(code)
        if not data:
            continue

        price = data.get("price", 0)
        change_pct = 0
        close_y = data.get("close_yesterday", 0)
        if close_y:
            change_pct = round((price - close_y) / close_y * 100, 2)

        volume = data.get("volume", 0) or 0
        low = data.get("low", 0) or 0

        # 执行所有检测
        alerts = []
        alerts += check_price_drop(code, name, price, change_pct)
        alerts += check_volume_anomaly(code, name, price, volume)
        alerts += check_consecutive_decline(code, name, price, snapshots)
        alerts += check_technical_breach(code, name, price, low)
        alerts += scan_negative_news(code, name, price)

        # 存入数据库
        for a in alerts:
            add_anomaly(code, a["level"], a["type"], price, a["msg"])

        all_alerts += alerts

    # 去重：相同code的相同level只保留最新的
    seen = set()
    unique_alerts = []
    for a in all_alerts:
        key = (a.get("level", ""), a.get("type", ""))
        if key not in seen:
            seen.add(key)
            unique_alerts.append(a)

    # 组合止盈止损检查
    pm = get_portfolio()
    stop_actions = pm.check_stop_conditions()
    for sa in stop_actions:
        alert_level = "A" if "STOP" in sa["action"] else "B"
        add_anomaly(sa["code"], alert_level, sa["action"], sa["price"], sa["reason"])
        unique_alerts.append({
            "level": alert_level,
            "type": sa["action"],
            "msg": f"{sa['name']}: {sa['reason']}",
        })

    # 推送预警
    for a in unique_alerts[:3]:  # 最多推3条，避免刷屏
        try:
            push_alert(a)
        except Exception:
            pass

    return {
        "status": "ok",
        "check_time": now,
        "checked_count": len(active),
        "alert_count": len(unique_alerts),
        "alerts": unique_alerts,
    }


def monitor_single(code: str) -> dict:
    """单只股票快速检测"""
    from db import get_stock
    stock = get_stock(code)
    if not stock or not stock["is_active"]:
        return {"status": "skip", "message": "不在持仓中，跳过监控"}

    data = fetch_single(code)
    if not data:
        return {"status": "error", "message": "获取数据失败"}

    price = data.get("price", 0)
    change_pct = 0
    close_y = data.get("close_yesterday", 0)
    if close_y:
        change_pct = round((price - close_y) / close_y * 100, 2)

    alerts = []
    alerts += check_price_drop(code, stock["name"], price, change_pct)
    alerts += check_volume_anomaly(code, stock["name"], price, data.get("volume", 0) or 0)

    for a in alerts:
        add_anomaly(code, a["level"], a["type"], price, a["msg"])

    return {
        "status": "ok",
        "code": code,
        "price": price,
        "change_pct": change_pct,
        "alert_count": len(alerts),
        "alerts": alerts,
    }


def monitor_candidates() -> list[dict]:
    """监控候选标的（非持仓）是否有极端下跌

    仅检查非活跃标的当日跌幅是否触发 A 级阈值。
    返回触发预警的候选标的列表，每条包含评分和推荐理由。
    """
    stocks = load_all_stocks()
    candidates = [s for s in stocks if not s["is_active"]]

    if not candidates:
        return []

    snapshots = fetch_realtime([s["code"] for s in candidates])
    snapshot_map = {s["code"]: s for s in snapshots}

    alerts = []
    for stock in candidates:
        code = stock["code"]
        name = stock["name"]
        data = snapshot_map.get(code)
        if not data:
            continue

        price = data.get("price", 0)
        change_pct = 0
        close_y = data.get("close_yesterday", 0)
        if close_y:
            change_pct = round((price - close_y) / close_y * 100, 2)

        if change_pct <= THRESHOLDS["A_drop"]:
            detail = STOCK_DETAILS.get(code, {})
            score = detail.get("score", 0)
            reason = detail.get("reason", "")
            msg = (
                f"💡 **{name}({code}) 候选标的极端下跌！**\n"
                f"跌幅 {change_pct:.1f}%，当前 {price:.2f} 元\n"
                f"Serenity评分: {score}/100 | {reason}\n"
                f"建议评估是否为买入机会"
            )
            alerts.append({
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "score": score,
                "reason": reason,
                "msg": msg,
            })

    return alerts


def get_monitor_summary() -> str:
    """生成监控摘要（用于定时推送）"""
    stocks = load_all_stocks()
    active = [s for s in stocks if s["is_active"]]

    if not active:
        return ""  # 无持仓时返回空字符串，cron 不推送

    today_anomalies = get_today_anomalies()
    unacknowledged = [a for a in today_anomalies if not a["acknowledged"]]

    lines = [f"⏱️ **盘中监控 | {datetime.now().strftime('%m-%d %H:%M')}**"]
    lines.append("")

    if unacknowledged:
        lines.append("🚨 **今日异常事件**")
        for a in unacknowledged[:5]:
            level_icon = {"A": "🔴🚨", "B": "⚠️", "C": "💡"}.get(a["level"], "📌")
            lines.append(f"{level_icon} [{a.get('name', a['code'])}] {a['message'].split(chr(10))[0]}")
        lines.append(f"\n共 {len(unacknowledged)} 条未确认")
    else:
        lines.append("✅ 今日无异常，一切正常")
        for s in active:
            lines.append(f"  - {s['name']}({s['code']}) 状态平稳")

        # 检查候选标的是否有极端波动
        candidate_alerts = monitor_candidates()
        if candidate_alerts:
            lines.append("")
            lines.append("💡 **候选标的极端波动**")
            for ca in candidate_alerts[:3]:
                lines.append(f"  🔻 {ca['name']}({ca['code']}) 跌 {ca['change_pct']:.1f}% → 评分{ca['score']}")

    lines.append("")
    lines.append(f"> 监控 {len(active)} 只持仓标的")
    return "\n".join(lines)


if __name__ == "__main__":
    result = monitor_all()
    print(json.dumps(result, ensure_ascii=False, indent=2))
