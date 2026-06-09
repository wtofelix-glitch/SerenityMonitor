#!/usr/bin/env python3
"""
Tier 1 标的回补提醒 — 检测 T1 标的回调至买入区并推送微信

工作原理：
1. 每日收盘后检查 Tier 1 标的（光迅/华工）当前价格
2. 如果价格从"高于买入区"回落至"买入区内"或"低于买入区"
3. 且上次推送不在 24 小时内 → 推送微信提醒

用法:
    python3 tier1_reentry.py              # 检测并推送
    python3 tier1_reentry.py --push       # 检测 + 微信推送
    python3 tier1_reentry.py --status     # 查看当前状态

集成:
    在 daily_workflow.py 第 6 步（调仓后）调用 check_tier1_reentry()
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import STOCK_MAP, STOCK_DETAILS, TIER_1_CODES
from data_engine import fetch_realtime
from scorer import compute_zone_score
from notifier import push_alert

# ── 状态文件 ──
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tier1_reentry_state.json")

# 重复推送间隔（小时）
REPEAT_HOURS = 24


def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_tier1_reentry(push: bool = False) -> list[dict]:
    """
    检查 Tier 1 标的是否出现回补机会。

    Returns:
        list[dict]: 符合条件的回补提醒列表
    """
    state = _load_state()
    today = date.today().isoformat()
    now = datetime.now()
    results = []

    # 获取实时行情
    realtime = fetch_realtime(TIER_1_CODES)
    rt_map = {r["code"]: r for r in realtime}

    for code in TIER_1_CODES:
        detail = STOCK_DETAILS.get(code, {})
        info = STOCK_MAP.get(code, {})
        name = info.get("name", code)
        rt = rt_map.get(code)

        if not rt:
            continue

        price = rt.get("price", 0)
        change_pct = rt.get("change_pct", 0)
        if price <= 0:
            continue

        # 计算 zone 评分
        zone_score, zone_label, zone_class = compute_zone_score(price, detail)
        zone_low = detail.get("buy_zone_low", 0)
        zone_high = detail.get("buy_zone_high", 0)
        target = detail.get("target_sell", 0)

        # 获取上次状态
        prev = state.get(code, {})
        prev_class = prev.get("zone_class", "")
        last_pushed = prev.get("last_push_date", "")

        # 计算距买入区距离百分比
        dist_pct = None
        if zone_class == "below" and zone_low > 0:
            dist_pct = round((zone_low - price) / zone_low * 100, 1)
        elif zone_class == "above" and zone_high > 0:
            dist_pct = round((price - zone_high) / price * 100, 1)

        # 判定是否触发提醒：
        # 条件：当前在买入区或低于买入区（折扣区）
        #    且之前不在买入区（即从"above"或"done"回落）
        #    且距上次推送超过 REPEAT_HOURS
        in_buy_opportunity = zone_class in ("", "below", "buy_zone")
        was_outside = prev_class in ("above", "done", "")
        not_pushed_recently = (not last_pushed or
                               (today > last_pushed) or
                               (datetime.strptime(today, "%Y-%m-%d") -
                                datetime.strptime(last_pushed, "%Y-%m-%d") >=
                                timedelta(hours=REPEAT_HOURS)))

        alert_data = {
            "code": code,
            "name": name,
            "price": price,
            "change_pct": change_pct,
            "zone_class": zone_class,
            "zone_label": zone_label,
            "zone_low": zone_low,
            "zone_high": zone_high,
            "target_sell": target,
            "dist_pct": dist_pct,
            "tier": info.get("tier", 1),
        }

        if in_buy_opportunity and was_outside and not_pushed_recently:
            results.append(alert_data)

            # 生成推送文本
            msg = _format_reentry_msg(alert_data)
            print(msg)
            print()

            if push:
                push_alert("tier1_reentry", code, msg)
                print(f"  ✅ 微信推送已发送")

            # 更新状态
            state[code] = {
                "zone_class": zone_class,
                "last_push_date": today,
                "last_push_time": now.strftime("%H:%M"),
                "price_at_push": price,
            }
        else:
            # 记录状态（但不推送）
            state[code] = {
                "zone_class": zone_class,
                "last_push_date": prev.get("last_push_date", ""),
                "last_push_time": prev.get("last_push_time", ""),
                "price_at_push": prev.get("price_at_push", 0),
            }

    _save_state(state)

    if not results:
        total = len(TIER_1_CODES)
        print(f"📊 Tier 1 回补检查: {total} 只，暂无触发")
        for code in TIER_1_CODES:
            info = STOCK_MAP.get(code, {})
            s = state.get(code, {})
            print(f"  {info.get('name', code)} ({code}) → zone={s.get('zone_class', '?')}")

    return results


def _format_reentry_msg(data: dict) -> str:
    """格式化为微信推送文本"""
    name = data["name"]
    code = data["code"]
    price = data["price"]
    change = data["change_pct"]
    zone_low = data["zone_low"]
    zone_high = data["zone_high"]
    target = data["target_sell"]
    cls = data["zone_class"]
    dist = data["dist_pct"]

    if change >= 0:
        change_str = f"+{change:.2f}%"
    else:
        change_str = f"{change:.2f}%"

    lines = [
        f"🔄 【T1 回补机会】{name} ({code})",
        f"━━━━━━━━━━━━━━━━━━",
        f"当前价格: {price:.2f} ({change_str})",
        f"状态: {_zone_label_cn(cls)}",
    ]

    if cls == "below":
        lines.append(f"📉 低于买入区 {dist}% — 折扣机会")
    elif cls in ("", "buy_zone"):
        lines.append(f"🎯 正处买入区 {zone_low:.1f}~{zone_high:.1f}")

    lines += [
        f"",
        f"买入区: {zone_low:.1f} ~ {zone_high:.1f}",
        f"目标卖出: {target:.1f} (+{round((target-price)/price*100,1)}%)",
        f"━━━━━━━━━━━━━━━━━━",
        f"💡 建议关注，择机建仓",
    ]
    return "\n".join(lines)


def _zone_label_cn(cls: str) -> str:
    return {
        "": "买入区 ✅",
        "below": "低于买入区 📉",
        "above": "高于买入区 📈",
        "done": "已达目标 🎯",
        "buy_zone": "买入区 ✅",
    }.get(cls, cls)


def cmd_status():
    """查看当前 T1 标的回补状态"""
    state = _load_state()
    print(f"📊 Tier 1 回补状态 | {date.today()}")
    print("=" * 50)
    for code in TIER_1_CODES:
        s = state.get(code, {})
        info = STOCK_MAP.get(code, {})
        name = info.get("name", code)
        pcls = s.get("zone_class", "—")
        last = s.get("last_push_date", "从未推送")
        print(f"  {name:6s} ({code}) zone={pcls:8s} 上次推送={last}")


if __name__ == "__main__":
    do_push = "--push" in sys.argv
    do_status = "--status" in sys.argv

    if do_status:
        cmd_status()
    else:
        results = check_tier1_reentry(push=do_push)
        if results:
            print(f"\n✅ {len(results)} 个回补提醒已生成")
        else:
            print("\n✅ 暂无回补机会")
