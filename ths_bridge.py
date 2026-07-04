#!/usr/bin/env python3
"""
同花顺券商桥接 — 行情获取 + 交易执行
使用 easyquotation 获取实时行情，通过同花顺客户端下单
"""

from __future__ import annotations
import json
import time
from datetime import date, datetime
from typing import Optional

from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════
# 行情获取
# ═══════════════════════════════════════════════

_ths_client = None


def _get_ths():
    global _ths_client
    if _ths_client is None:
        try:
            import easyquotation
            _ths_client = easyquotation.use("ths")
            log.info("同花顺行情客户端已连接")
        except Exception as e:
            log.warning("同花顺行情连接失败: %s", e)
            _ths_client = None
    return _ths_client


def get_realtime_quote(codes: list[str]) -> list[dict]:
    """通过同花顺获取实时行情（需客户端登录）"""
    client = _get_ths()
    if not client:
        return []

    results = []
    try:
        # easyquotation THS 返回格式: {code: {name, now, open, high, low, ...}}
        all_quotes = client.stocks(codes)
        for code in codes:
            raw = all_quotes.get(code, {})
            if raw:
                results.append({
                    "code": code,
                    "name": raw.get("name", ""),
                    "price": float(raw.get("now", 0)),
                    "open": float(raw.get("open", 0)),
                    "high": float(raw.get("high", 0)),
                    "low": float(raw.get("low", 0)),
                    "volume": float(raw.get("volume", 0)),
                    "change_pct": float(raw.get("涨跌幅", 0)),
                    "source": "ths",
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        log.warning("同花顺行情获取失败: %s", e)

    return results


# ═══════════════════════════════════════════════
# 交易执行 (通过同花顺客户端下单)
# ═══════════════════════════════════════════════

class THSBroker:
    """
    同花顺券商交易桥接

    模式: SEMI_AUTO — 生成下单指令，用户在同花顺客户端确认执行
    同花顺不提供公开API，交易通过文件队列 + 手动确认完成
    """

    def __init__(self):
        self.order_dir = "/Users/mac/.hermes/ths_orders"
        import os
        os.makedirs(self.order_dir, exist_ok=True)

    def place_order(self, code: str, action: str, price: float, shares: int,
                    reason: str = "") -> dict:
        """下单: 生成订单文件 + 返回操作指令"""

        # 格式化代码为同花顺格式 (如 000938)
        code_clean = code.replace("sh", "").replace("sz", "")

        order = {
            "code": code_clean,
            "action": action,
            "price": round(price, 2),
            "shares": shares,
            "amount": round(price * shares, 2),
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
        }

        # 写入订单文件
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.order_dir}/{ts}_{action}_{code_clean}_{shares}股.json"
        with open(filename, "w") as f:
            json.dump(order, f, ensure_ascii=False, indent=2)

        # 返回操作指令（用户在同花顺手动操作）
        action_cn = "买入" if action == "buy" else "卖出"
        instruction = (
            f"📋 同花顺{action_cn}指令\n"
            f"  代码: {code_clean}\n"
            f"  价格: ¥{price:.2f}\n"
            f"  数量: {shares}股 (¥{price * shares:,.0f})\n"
            f"  原因: {reason}\n"
            f"  ⚠️ 请在 同花顺 客户端手动确认"
        )

        return {
            "status": "pending_confirm",
            "order_id": ts,
            "instruction": instruction,
            "order_file": filename,
        }

    def get_orders_today(self) -> list[dict]:
        """获取今日所有订单"""
        import os, glob
        today_str = date.today().strftime("%Y%m%d")
        orders = []
        for f in sorted(glob.glob(f"{self.order_dir}/{today_str}_*.json")):
            with open(f) as fp:
                orders.append(json.load(fp))
        return orders

    def get_account_summary(self) -> dict:
        """获取账户摘要（通过同花顺行情估算）"""
        from db import get_conn, load_all_stocks

        conn = get_conn()
        stocks = [s for s in load_all_stocks() if s.get("is_active") and s["code"] != "CASH"]
        conn.close()

        total_value = 0
        positions = []
        for s in stocks:
            quotes = get_realtime_quote([s["code"]])
            price = quotes[0]["price"] if quotes else float(s.get("buy_price", 0))
            shares = int(float(s.get("trade_amount", 0)) / float(s.get("buy_price", 1))) if float(s.get("buy_price", 0)) > 0 else 0
            current_value = price * shares
            total_value += current_value
            positions.append({
                "code": s["code"],
                "name": s.get("name", ""),
                "shares": shares,
                "price": price,
                "value": round(current_value, 2),
            })

        return {
            "total_value": round(total_value, 2),
            "positions": len(positions),
            "positions_detail": positions,
            "timestamp": datetime.now().isoformat(),
        }


# ═══════════════════════════════════════════════
# Auto-Execution Integration
# ═══════════════════════════════════════════════

def auto_execute_to_ths(plan: dict, dry_run: bool = False) -> dict:
    """
    v5.2 自动执行管道: Serenity信号 → 同花顺下单
    返回执行摘要
    """
    broker = THSBroker()
    results = {"buys": [], "sells": [], "errors": []}

    for entry in plan.get("buys", [])[:3]:  # 每日最多3笔买入
        if dry_run:
            results["buys"].append({"code": entry["code"], "action": "buy", "status": "dry_run"})
            continue
        try:
            result = broker.place_order(
                code=entry["code"],
                action="buy",
                price=entry.get("price", 0),
                shares=entry.get("shares", 100),
                reason=entry.get("reasons", ["自动信号"])[0] if entry.get("reasons") else "Serenity信号",
            )
            results["buys"].append(result)
        except Exception as e:
            results["errors"].append({"code": entry.get("code"), "error": str(e)})

    for entry in plan.get("sells", []):
        if dry_run:
            results["sells"].append({"code": entry["code"], "action": "sell", "status": "dry_run"})
            continue
        try:
            result = broker.place_order(
                code=entry["code"],
                action="sell",
                price=0,  # 市价卖出
                shares=entry.get("shares", 0),
                reason=entry.get("reasons", ["自动信号"])[0] if entry.get("reasons") else "止盈/止损",
            )
            results["sells"].append(result)
        except Exception as e:
            results["errors"].append({"code": entry.get("code"), "error": str(e)})

    # 写执行日志
    from db import get_conn
    conn = get_conn()
    for r in results["buys"] + results["sells"]:
        conn.execute(
            "INSERT INTO execution_log (code, action, status, price, shares, amount, reason, created_at) VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))",
            (r.get("code", ""), r.get("action", ""), r.get("status", "pending"),
             r.get("price", 0), r.get("shares", 0), r.get("amount", 0),
             r.get("reason", r.get("instruction", "")[:200])),
        )
    conn.commit()
    conn.close()

    return results
