#!/usr/bin/env python3
"""
组合再平衡引擎 — 当前权重 vs 目标权重 → 买卖量 → T+1 执行
维持最优配置, 自动应对价格漂移
"""
from datetime import date
from serenity_logger import get_logger

log = get_logger(__name__)


def get_current_portfolio() -> dict:
    """获取当前持仓: {code: {shares, current_price, current_value, weight}}"""
    from db import get_conn
    from data_engine import fetch_realtime

    conn = get_conn()
    stocks = conn.execute(
        "SELECT code, name, buy_price, trade_amount FROM stocks WHERE is_active=1 AND code!='CASH'"
    ).fetchall()
    conn.close()

    codes = [s["code"] for s in stocks]
    quotes = fetch_realtime(codes, source="auto")
    quote_map = {q["code"]: q for q in quotes} if quotes else {}

    positions = {}
    total_value = 0
    for s in stocks:
        sd = dict(s)
        code = sd["code"]
        q = quote_map.get(code, {})
        price = q.get("price", float(sd["buy_price"] or 0))
        shares = int(float(sd["trade_amount"] or 0) / float(sd["buy_price"] or 1)) if float(sd["buy_price"] or 0) > 0 else 0
        current_value = shares * price
        total_value += current_value
        positions[code] = {
            "code": code, "name": sd.get("name", sd["code"]),
            "shares": shares, "current_price": price,
            "current_value": round(current_value, 2),
            "weight": 0,  # computed below
        }

    if total_value > 0:
        for p in positions.values():
            p["weight"] = round(p["current_value"] / total_value * 100, 1)

    return {
        "positions": list(positions.values()),
        "total_value": round(total_value, 2),
        "date": date.today().isoformat(),
    }


def get_target_weights(portfolio: dict) -> dict:
    """从优化器获取目标权重"""
    from portfolio_optimizer import run as run_optimizer
    opt = run_optimizer()

    targets = {}
    for a in opt.get("allocations", []):
        if a.get("action") in ("buy", "hold"):
            targets[a["code"]] = a["target_weight"]

    return targets


def compute_rebalance_orders(current: dict, targets: dict) -> list[dict]:
    """计算再平衡订单"""
    positions = current["positions"]
    total_value = current["total_value"]

    orders = []
    for p in positions:
        code = p["code"]
        current_weight = p["weight"]
        target_weight = targets.get(code, 0)

        diff = target_weight - current_weight
        if abs(diff) < 2:  # 差异 < 2% 不调整
            continue

        target_value = total_value * target_weight / 100
        diff_value = target_value - p["current_value"]

        if diff_value > 0:
            # 需要加仓
            shares = int(diff_value / p["current_price"] / 100) * 100
            if shares >= 100:
                orders.append({
                    "code": code, "name": p["name"],
                    "action": "buy",
                    "shares": shares,
                    "price": p["current_price"],
                    "amount": round(shares * p["current_price"], 2),
                    "reason": f"再平衡: {current_weight:.1f}%→{target_weight:.1f}%",
                })
        else:
            # 需要减仓
            shares = int(abs(diff_value) / p["current_price"] / 100) * 100
            shares = min(shares, p["shares"])  # 不能卖超过持有量
            if shares >= 100:
                orders.append({
                    "code": code, "name": p["name"],
                    "action": "sell",
                    "shares": shares,
                    "price": p["current_price"],
                    "amount": round(shares * p["current_price"], 2),
                    "reason": f"再平衡: {current_weight:.1f}%→{target_weight:.1f}%",
                })

    orders.sort(key=lambda x: -x["amount"])
    return orders


def execute_rebalance(dry_run: bool = True) -> dict:
    """执行再平衡"""
    current = get_current_portfolio()
    targets = get_target_weights(current)
    orders = compute_rebalance_orders(current, targets)

    if not orders:
        return {"status": "balanced", "message": "当前配置已达最优", "orders": []}

    if dry_run:
        return {
            "status": "dry_run",
            "total_value": current["total_value"],
            "targets": {k: f"{v:.1f}%" for k, v in targets.items()},
            "orders": orders,
            "note": "🧪 试运行模式, 未实际下单",
        }

    # 实盘执行 — 通过 THS 桥接
    try:
        from ths_bridge import auto_execute_to_ths
        plan = {"buys": [o for o in orders if o["action"] == "buy"],
                "sells": [o for o in orders if o["action"] == "sell"]}
        result = auto_execute_to_ths(plan, dry_run=False)
        return {
            "status": "executed",
            "total_value": current["total_value"],
            "orders": orders,
            "ths_result": result,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "orders": orders}


def generate_rebalance_report(orders: list[dict]) -> str:
    """生成再平衡报告"""
    if not orders:
        return "✅ 当前配置已达最优, 无需调整。"

    lines = ["# ⚖️ 组合再平衡", f"**{date.today().isoformat()}**", ""]
    buys = [o for o in orders if o["action"] == "buy"]
    sells = [o for o in orders if o["action"] == "sell"]

    if buys:
        lines.append(f"## 🟢 加仓 ({len(buys)} 笔)")
        for o in buys:
            lines.append(f"- {o['name']}({o['code']}): +{o['shares']}股 @¥{o['price']:.2f} ≈¥{o['amount']:,.0f}")
        lines.append("")

    if sells:
        lines.append(f"## 🔴 减仓 ({len(sells)} 笔)")
        for o in sells:
            lines.append(f"- {o['name']}({o['code']}): -{o['shares']}股 @¥{o['price']:.2f} ≈¥{o['amount']:,.0f}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    dry = "--execute" not in sys.argv
    result = execute_rebalance(dry_run=dry)

    if result["orders"]:
        print(generate_rebalance_report(result["orders"]))
        print(f"\n状态: {result['status']}")
        if result["status"] == "dry_run":
            print("💡 使用 --execute 执行实盘交易")
    else:
        print("✅ 无需再平衡")
