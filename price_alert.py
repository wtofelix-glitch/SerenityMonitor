"""价格预警模块 — DB 持仓阈值 + 自定义价格条件告警"""
import json
import os
import time
from datetime import datetime

from config import SUGGESTED_TARGETS
from serenity_logger import get_logger

log = get_logger(__name__)
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_alerts.json")


def _load():
    if not os.path.exists(P):
        return {"alerts": [], "history": []}
    with open(P, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("alerts", [])
    data.setdefault("history", [])
    return data


def _save(d):
    with open(P, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def add(code, name, cond, price, note=""):
    d = _load()
    a = {
        "id": str(int(time.time() * 1000)),
        "code": code,
        "name": name,
        "condition": cond,
        "price": price,
        "note": note,
        "active": True,
        "triggered": False,
        "created_at": datetime.now().isoformat(),
        "triggered_at": None,
        "triggered_price": None,
    }
    d["alerts"].append(a)
    _save(d)
    return a


def remove(aid):
    d = _load()
    d["alerts"] = [a for a in d["alerts"] if a["id"] != aid]
    _save(d)


def active():
    return [a for a in _load()["alerts"] if a.get("active") and not a.get("triggered")]


def history(n=20):
    return [a for a in _load()["alerts"] if a.get("triggered")][:n]


def check():
    from data_engine import fetch_realtime

    d = _load()
    codes = list({a["code"] for a in d["alerts"] if a.get("active") and not a.get("triggered")})
    if not codes:
        return []

    try:
        rt = fetch_realtime(codes)
        rm = {r["code"]: r for r in rt} if rt else {}
    except Exception as exc:
        log.warning("自定义价格告警行情获取失败: %s", exc)
        return []

    trig = []
    changed = False
    for a in d["alerts"]:
        if not a.get("active") or a.get("triggered"):
            continue
        p = rm.get(a["code"], {}).get("price", 0)
        if p <= 0:
            continue
        hit = (
            (a["condition"] == "below" and p <= a["price"]) or
            (a["condition"] == "above" and p >= a["price"])
        )
        if hit:
            a["triggered"] = True
            a["triggered_at"] = datetime.now().isoformat()
            a["triggered_price"] = p
            changed = True
            trig.append(a)
    if changed:
        _save(d)
    if trig:
        for t in trig:
            log.info("告警触发: %s %s ¥%s (现价%s)", t["name"], t["condition"], t["price"], t["triggered_price"])
    return trig


def check_alerts() -> list[dict]:
    """检查 active 持仓的目标价、买入区间下限和止损线。"""
    from data_engine import fetch_single
    from db import add_alert as add_db_alert, load_all_stocks

    triggered = []
    stocks = load_all_stocks()
    for stock in [s for s in stocks if s.get("is_active")]:
        code = stock["code"]
        target_high = stock.get("target_high") or 0
        target_low = stock.get("target_low") or 0
        stop_loss = stock.get("stop_loss") or 0

        if not target_high and not target_low and not stop_loss:
            continue

        data = fetch_single(code)
        if not data:
            continue

        price = data.get("price", 0) or 0
        name = stock.get("name") or data.get("name") or code

        if target_high > 0 and price >= target_high:
            msg = f"🚨 {name}({code}) 达到目标价！当前{price:.2f} ≥ 目标{target_high:.2f}，建议卖出"
            add_db_alert(code, "target_high", price, msg)
            triggered.append({"code": code, "type": "target_high", "price": price, "target": target_high, "msg": msg})

        if target_low > 0 and price <= target_low:
            msg = f"⚠️ {name}({code}) 触及价值区间下限！当前{price:.2f} ≤ 目标{target_low:.2f}，关注是否有新加仓机会"
            add_db_alert(code, "target_low", price, msg)
            triggered.append({"code": code, "type": "target_low", "price": price, "target": target_low, "msg": msg})

        if stop_loss > 0 and price <= stop_loss:
            msg = f"🔴 {name}({code}) 触发止损！当前{price:.2f} ≤ 止损{stop_loss:.2f}"
            add_db_alert(code, "stop_loss", price, msg)
            triggered.append({"code": code, "type": "stop_loss", "price": price, "target": stop_loss, "msg": msg})

    return triggered


def get_pending_alerts() -> list[dict]:
    """获取所有未确认的 DB 预警。"""
    from db import get_unacknowledged_alerts

    return get_unacknowledged_alerts()


def ack(alert_id: int):
    """确认 DB 预警。"""
    from db import acknowledge_alert

    acknowledge_alert(alert_id)


def check_suggested_targets(code: str) -> dict:
    """检查候选标的当前价格是否在预设买入区间。"""
    from data_engine import fetch_single

    data = fetch_single(code)
    if not data:
        return {"status": "error", "msg": "获取行情失败"}

    price = data.get("price", 0)
    name = data.get("name", code)

    if code not in SUGGESTED_TARGETS:
        return {
            "status": "no_target",
            "code": code,
            "name": name,
            "price": price,
            "msg": f"📊 {name} 当前{price:.2f}元，无预设目标区间，请自行判断",
        }

    suggestion = SUGGESTED_TARGETS[code]
    buy_zone = suggestion["buy_zone"]
    parts = buy_zone.replace("元", "").split("-")
    zone_low, zone_high = float(parts[0]), float(parts[1])

    if zone_low <= price <= zone_high:
        return {
            "status": "buy_zone",
            "code": code,
            "name": name,
            "price": price,
            "buy_zone": buy_zone,
            "msg": f"✅ {name} 当前{price:.2f}元，处于建议买入区间 {buy_zone}，可以考虑建仓",
        }
    if price < zone_low:
        return {
            "status": "below_zone",
            "code": code,
            "name": name,
            "price": price,
            "buy_zone": buy_zone,
            "msg": f"📉 {name} 当前{price:.2f}元，低于建议买入区间 {buy_zone}，可等待企稳",
        }
    return {
        "status": "above_zone",
        "code": code,
        "name": name,
        "price": price,
        "buy_zone": buy_zone,
        "msg": f"📈 {name} 当前{price:.2f}元，高于建议买入区间 {buy_zone}，追高风险大",
    }
