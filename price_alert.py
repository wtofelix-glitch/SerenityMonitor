"""
价格预警模块 — 检查持有标的是否进入目标区间或触发止损
"""
from datetime import date
from typing import Optional
from config import SUGGESTED_TARGETS
from db import (
    load_all_stocks, add_alert, get_unacknowledged_alerts,
    acknowledge_alert, get_stock
)
from data_engine import fetch_single


def check_alerts() -> list[dict]:
    """
    检查所有 active 标的的价格状态
    返回触发预警的列表
    """
    stocks = load_all_stocks()
    active = [s for s in stocks if s["is_active"]]
    triggered = []

    for s in active:
        code = s["code"]
        target_high = s["target_high"]
        target_low = s["target_low"]
        stop_loss = s["stop_loss"]

        if not target_high and not target_low and not stop_loss:
            continue  # 未设值区间，跳过

        data = fetch_single(code)
        if not data:
            continue

        price = data.get("price", 0)
        name = s["name"]

        # 检查触及目标卖出价上限
        if target_high > 0 and price >= target_high:
            msg = f"🚨 {name}({code}) 达到目标价！当前{price:.2f} ≥ 目标{target_high:.2f}，建议卖出"
            add_alert(code, "target_high", price, msg)
            triggered.append({"code": code, "type": "target_high", "price": price, "target": target_high, "msg": msg})

        # 检查触及目标卖出价下限
        if target_low > 0 and price <= target_low:
            msg = f"⚠️ {name}({code}) 触及价值区间下限！当前{price:.2f} ≤ 目标{target_low:.2f}，关注是否有新加仓机会"
            add_alert(code, "target_low", price, msg)
            triggered.append({"code": code, "type": "target_low", "price": price, "target": target_low, "msg": msg})

        # 检查止损
        if stop_loss > 0 and price <= stop_loss:
            msg = f"🔴 {name}({code}) 触发止损！当前{price:.2f} ≤ 止损{stop_loss:.2f}"
            add_alert(code, "stop_loss", price, msg)
            triggered.append({"code": code, "type": "stop_loss", "price": price, "target": stop_loss, "msg": msg})

        # 多周期趋势预警：评分持续3期下降且距卖出阈值<10分
        # 止盈标的例外（已盈利≥10%，趋势下行是正常调整，不触发预警）
        buy_price = s.get("buy_price", 0)
        profit_pct = ((price - buy_price) / buy_price * 100) if buy_price > 0 else 0
        if profit_pct < 10:  # 未达止盈区域时才检查趋势预警
            try:
                from scorer import score_all
                all_scores = score_all()
                my_score = next((r for r in all_scores if r.get("code") == code), None)
                if my_score:
                    from conviction_engine import multi_cycle_consensus
                    consensus = multi_cycle_consensus(code, my_score["total_score"], "short")
                    if consensus["trend"] == "down" and consensus["consensus_score"] < 58:
                        score_dist = consensus["consensus_score"] - 48
                        if score_dist < 10:
                            msg = (f"📉 {name}({code}) 趋势预警！多周期共识{consensus['consensus_score']:.0f}分 "
                                   f"(距卖出阈值{score_dist:.0f}分)，连续下行中，建议密切关注")
                            add_alert(code, "trend_warning", price, msg)
                            triggered.append({"code": code, "type": "trend_warning", "price": price, "msg": msg})
            except Exception:
                pass

    return triggered


def get_pending_alerts() -> list[dict]:
    """获取所有未确认的预警"""
    return get_unacknowledged_alerts()


def ack(alert_id: int):
    """确认预警"""
    acknowledge_alert(alert_id)


def check_suggested_targets(code: str) -> dict:
    """
    检查某个（尚未买入的）候选标的当前价格是否在建议买入区间
    """
    data = fetch_single(code)
    if not data:
        return {"status": "error", "msg": "获取行情失败"}

    price = data.get("price", 0)
    name = data.get("name", code)

    if code in SUGGESTED_TARGETS:
        suggestion = SUGGESTED_TARGETS[code]
        buy_zone = suggestion["buy_zone"]

        # 解析买入区间
        parts = buy_zone.replace("元", "").split("-")
        zone_low, zone_high = float(parts[0]), float(parts[1])

        if zone_low <= price <= zone_high:
            return {
                "status": "buy_zone",
                "code": code,
                "name": name,
                "price": price,
                "buy_zone": buy_zone,
                "msg": f"✅ {name} 当前{price:.2f}元，处于建议买入区间 {buy_zone}，可以考虑建仓"
            }
        elif price < zone_low:
            return {
                "status": "below_zone",
                "code": code,
                "name": name,
                "price": price,
                "buy_zone": buy_zone,
                "msg": f"📉 {name} 当前{price:.2f}元，低于建议买入区间 {buy_zone}，可等待企稳",
            }
        else:
            return {
                "status": "above_zone",
                "code": code,
                "name": name,
                "price": price,
                "buy_zone": buy_zone,
                "msg": f"📈 {name} 当前{price:.2f}元，高于建议买入区间 {buy_zone}，追高风险大",
            }
    else:
        return {
            "status": "no_target",
            "code": code,
            "name": name,
            "price": price,
            "msg": f"📊 {name} 当前{price:.2f}元，无预设目标区间，请自行判断",
        }
