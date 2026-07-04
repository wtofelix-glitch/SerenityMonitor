#!/usr/bin/env python3
"""
持仓优化器 — 均值-方差 + Sharpe最大化
给定当前持仓 + 候选信号, 算最优调仓方案
"""
from datetime import date
from serenity_logger import get_logger
log = get_logger(__name__)


def get_returns_matrix(codes, days=60):
    from db import get_conn
    conn = get_conn()
    rets = {c: [] for c in codes}
    for code in codes:
        rows = conn.execute("SELECT close FROM price_history WHERE code=? ORDER BY date DESC LIMIT ?", (code, days + 1)).fetchall()
        closes = [r["close"] for r in rows if r["close"]]
        for i in range(len(closes) - 1):
            if closes[i + 1] > 0:
                rets[code].append((closes[i] - closes[i + 1]) / closes[i + 1])
        rets[code].reverse()
    conn.close()
    return rets


def optimize(holdings, candidates, total_value, max_positions=5, max_single=0.40):
    """均值-方差简化优化: Sharpe排序, 按比例分配权重"""
    all_codes = [h["code"] for h in holdings] + [c["code"] for c in candidates]
    rets = get_returns_matrix(all_codes, 60)
    risk_free = 0.02

    assets = []
    for h in holdings:
        r = rets.get(h["code"], [0.01])
        vol = max((sum(x**2 for x in r) / max(len(r), 1)) ** 0.5, 0.01)
        assets.append({"code": h["code"], "name": h.get("name", h["code"]), "expected_return": h.get("expected_return", 0.05), "volatility": vol, "current_weight": h.get("current_weight", 0), "is_held": True})

    for c in candidates:
        r = rets.get(c["code"], [0.01])
        vol = max((sum(x**2 for x in r) / max(len(r), 1)) ** 0.5, 0.01)
        assets.append({"code": c["code"], "name": c.get("name", c["code"]), "expected_return": c.get("expected_return", 0.03), "volatility": vol, "current_weight": 0, "is_held": False})

    for a in assets:
        a["sharpe_proxy"] = (a["expected_return"] - risk_free / 252) / a["volatility"]

    ranked = sorted(assets, key=lambda x: -x["sharpe_proxy"])
    allocations, remaining_weight, position_count = [], 1.0, 0

    for a in ranked[:max_positions * 2]:
        if position_count >= max_positions or a["sharpe_proxy"] <= 0:
            break
        total_s = sum(x["sharpe_proxy"] for x in ranked[:max_positions] if x["sharpe_proxy"] > 0)
        if total_s <= 0: break
        tw = min(a["sharpe_proxy"] / total_s * remaining_weight, max_single)
        if tw < 0.05: continue
        allocations.append({"code": a["code"], "name": a["name"], "action": "buy" if not a["is_held"] else "hold", "current_weight": round(a["current_weight"] * 100, 1), "target_weight": round(tw * 100, 1), "target_amount": round(total_value * tw, 0), "sharpe": round(a["sharpe_proxy"], 2), "vol": round(a["volatility"] * 100, 1)})
        remaining_weight -= tw
        position_count += 1

    held_in = {a["code"] for a in allocations}
    for h in holdings:
        if h["code"] not in held_in and h.get("current_weight", 0) > 0:
            allocations.append({"code": h["code"], "name": h.get("name", h["code"]), "action": "reduce", "current_weight": round(h.get("current_weight", 0) * 100, 1), "target_weight": 0, "target_amount": 0, "sharpe": 0, "vol": 0})

    return {"date": date.today().isoformat(), "total_value": total_value, "allocations": allocations, "cash_weight": round(remaining_weight * 100, 1), "position_count": position_count}


def run():
    from db import get_latest_scores
    from portfolio import PortfolioManager
    scores = get_latest_scores()
    score_map = {s["code"]: s["total_score"] for s in scores}
    pm = PortfolioManager()
    pv = pm.get_portfolio_value()
    holdings, held_codes = [], {p.get("code") for p in pv.get("positions", [])}
    for p in (pv.get("positions") or []):
        w = p.get("current_value", 0) / pv["total_value"] if pv["total_value"] else 0
        holdings.append({"code": p["code"], "name": p.get("name", ""), "current_weight": w, "expected_return": (score_map.get(p["code"], 50) - 45) / 100})
    candidates = [{"code": s["code"], "name": s.get("name", s["code"]), "expected_return": (s["total_score"] - 45) / 100} for s in scores if s["code"] not in held_codes and s["total_score"] >= 55][:5]
    return optimize(holdings, candidates, pv["total_value"])


if __name__ == "__main__":
    r = run()
    if "allocations" in r:
        print(f"最优配置 ({r['date']}): 总资产 ¥{r['total_value']:,.0f} | {r['position_count']}持仓 | 现金{r['cash_weight']}%")
        for a in r["allocations"]:
            print(f"  {'🟢' if a['action']=='buy' else '🟡' if a['action']=='hold' else '🔴'} {a['name']}({a['code']}): {a['action']} {a['current_weight']}%→{a['target_weight']}% ¥{a['target_amount']:,.0f} (Sharpe{a['sharpe']})")
