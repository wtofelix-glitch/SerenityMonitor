#!/usr/bin/env python3
"""
自动执行引擎 — 收盘后对比评分 vs 持仓，生成可执行调仓指令。
优先推荐 Tier 1 标的（光迅科技/华工科技）。

设计原则:
  - 只输出确定可执行的买入/卖出指令，不留"可考虑"的模糊空间
  - 买入仅在 STRONG_BUY(≥78) 或 BUY(≥72) 时触发
  - 卖出在 评分<48 或 zone=done 时强制触发
  - Tier 1 标的优先于 Tier 2/3（同等评分下优先推荐）

用法:
    python3 auto_execute.py              # 生成调仓计划
    python3 auto_execute.py --push       # 生成 + 推送
    python3 auto_execute.py --dry-run    # 仅查看，不写入任何记录
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date
from db import get_conn, load_all_stocks
from config import (
    STOCK_MAP, STOCK_DETAILS, ALL_CODES, TIER_1_CODES,
    SIGNAL_CONFIG, CAPITAL_CONFIG, RISK_CONFIG,
)

# 大盘择时调整参数
def _get_market_adjustments():
    """获取大盘择时对仓位参数的调整系数"""
    try:
        from market_timing import get_market_signal
        sig = get_market_signal()
        trend = sig.get('overall_signal', '中性')
    except Exception:
        trend = '中性'
    
    # 返回 (max_positions, max_single_pct, enter_threshold_adj)
    adjustments = {
        '危险': (1, 0.40, +5),   # 单只40%, 买入线+5
        '谨慎': (2, 0.50, +3),
        '中性': (2, 0.60, 0),
        '积极': (2, 0.60, 0),
        '机会': (2, 0.60, -2),   # 超卖时可放宽买入
    }
    adj = adjustments.get(trend, (2, 0.60, 0))
    return {'trend': trend, 'max_pos': adj[0], 'max_single': adj[1], 'enter_adj': adj[2]}

# ── 原导入 ──



def _parse_details(details_raw) -> dict:
    """Parse details field from DB — handles both JSON and old repr format"""
    import json, ast, re
    if isinstance(details_raw, dict):
        return details_raw
    if not details_raw or not isinstance(details_raw, str):
        return {}
    try:
        return json.loads(details_raw)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        cleaned = re.sub(r"np\.\w+\(([^)]+)\)", r"\1", details_raw)
        d = ast.literal_eval(cleaned)
        if isinstance(d, dict):
            return d
    except (ValueError, SyntaxError):
        pass
    return {}
def get_latest_scores() -> dict[str, dict]:
    """获取最新评分的所有标的数据"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.* FROM scoring_history s
        INNER JOIN (
            SELECT code, MAX(date) as max_date FROM scoring_history
            GROUP BY code
        ) latest ON s.code = latest.code AND s.date = latest.max_date
        ORDER BY s.total_score DESC
    """).fetchall()
    conn.close()
    return {r["code"]: dict(r) for r in rows}


def get_current_holdings() -> list[dict]:
    """获取当前持仓"""
    return [s for s in load_all_stocks() if s["is_active"]]


def compute_total_portfolio_value(holdings: list[dict], cash: float) -> float:
    """估算总资产（用最新评分里的收盘价）"""
    scores = get_latest_scores()
    holdings_value = 0.0
    for h in holdings:
        code = h["code"]
        s = scores.get(code, {})
        price = s.get("details", {})
        if isinstance(price, str):
                price = _parse_details(price)
        close = price.get("price", h.get("buy_price", 0)) if isinstance(price, dict) else h.get("buy_price", 0)
        amt = h.get("trade_amount", 0) or 0
        buy_price = h.get("buy_price", 1)
        shares = int(amt / buy_price / 100) * 100 if buy_price > 0 else 0
        holdings_value += shares * close
    return cash + holdings_value


def compute_available_cash() -> float:
    """计算可用现金"""
    conn = get_conn()
    rows = conn.execute("SELECT action, price, quantity, trade_amount FROM trades").fetchall()
    conn.close()
    bought = 0.0
    sold = 0.0
    for r in rows:
        amt = r["trade_amount"] or 0
        if amt == 0 and r["price"] and r["quantity"]:
            amt = r["price"] * r["quantity"]
        if r["action"] == "buy":
            bought += amt
        elif r["action"] == "sell":
            sold += amt
    return CAPITAL_CONFIG["initial_capital"] - bought + sold


def generate_execution_plan(dry_run: bool = False) -> dict:
    """
    生成可执行调仓计划。

    Returns:
        {
            "date": str,
            "cash": float,
            "total_value": float,
            "sells": [order],
            "buys": [order],
            "summary": str,
        }
    """
    today = date.today().isoformat()
    scores = get_latest_scores()
    holdings = get_current_holdings()
    holding_codes = {h["code"] for h in holdings}
    cash = compute_available_cash()

    total_value = compute_total_portfolio_value(holdings, cash)

    # 大盘择时调整
    market = _get_market_adjustments()
    max_positions = market['max_pos']
    max_single_weight = market['max_single']
    enter_threshold = CAPITAL_CONFIG.get("enter_threshold", SIGNAL_CONFIG["buy_threshold"]) + market['enter_adj']
    exit_threshold = CAPITAL_CONFIG.get("exit_threshold", SIGNAL_CONFIG.get("pos_exit_threshold", 48))
    reserve_cash_ratio = CAPITAL_CONFIG["reserve_cash_ratio"]

    sells = []
    buys = []

    # ── Phase 1: 检查持仓是否需要卖出 ──────────────────────
    for h in holdings:
        code = h["code"]
        name = STOCK_MAP.get(code, {}).get("name", code)
        s = scores.get(code, {})
        score = s.get("total_score", 50)

        details = _parse_details(s.get("details", "{}"))
        signal = details.get("signal_action", "HOLD")
        zone_label = details.get("zone_label", "")
        close = details.get("price", h.get("buy_price", 0))
        target_sell = details.get("target_sell", 0)

        reasons = []
        should_sell = False

        # 条件1: 评分跌破退出阈值
        if score < exit_threshold:
            should_sell = True
            reasons.append(f"评分{score:.0f} < 退出线{exit_threshold}")

        # 条件2: 已达目标价
        if zone_label == "已达目标" and close >= target_sell and target_sell > 0:
            should_sell = True
            reasons.append(f"已达目标价 {target_sell:.0f}（现价{close:.2f}）")

        # 条件3: 信号本身就是 SELL/STOP_LOSS
        if signal in ("SELL", "STOP_LOSS"):
            should_sell = True
            reasons.append(f"系统信号: {signal}")

        if should_sell:
            amt = h.get("trade_amount", 0) or 0
            buy_price = h.get("buy_price", 1)
            shares = int(amt / buy_price / 100) * 100 if buy_price > 0 else 0
            estimated_proceeds = shares * close
            profit_pct = ((close - buy_price) / buy_price * 100) if buy_price > 0 else 0

            sells.append({
                "code": code,
                "name": name,
                "action": "SELL",
                "score": score,
                "shares": shares,
                "estimated_proceeds": round(estimated_proceeds, 2),
                "profit_pct": round(profit_pct, 1),
                "reasons": reasons,
                "urgency": "high" if signal in ("SELL", "STOP_LOSS") else "medium",
            })

    # ── Phase 2: 释放资金后，检查可买入标的 ──────────────────
    freed_cash = sum(s["estimated_proceeds"] for s in sells)
    available_cash = cash + freed_cash

    # 卖出后剩余持仓数
    remaining_holdings = len(holdings) - len(sells)
    open_slots = max(0, max_positions - remaining_holdings)

    swap_candidates = []
    if open_slots <= 0 and holdings:
        # 无空位 — 检查是否应换仓：非持仓评分 > 最低持仓评分 + 8
        held_scores = {
            h["code"]: scores.get(h["code"], {}).get("total_score", 0)
            for h in holdings if h["code"] not in {s["code"] for s in sells}
        }
        if held_scores:
            worst_held = min(held_scores, key=held_scores.get)
            worst_held_score = held_scores[worst_held]
            for code in ALL_CODES:
                if code in holding_codes:
                    continue
                if code in {s["code"] for s in sells}:
                    continue
                s = scores.get(code, {})
                score = s.get("total_score", 0)
                details = _parse_details(s.get("details", "{}"))
                signal = details.get("signal_action", "")
                if score >= enter_threshold and signal in ("STRONG_BUY", "BUY"):
                    if score >= worst_held_score + 8:
                        swap_candidates.append({
                            "code": code,
                            "name": STOCK_MAP.get(code, {}).get("name", code),
                            "score": score,
                            "swap_out_code": worst_held,
                            "swap_out_name": STOCK_MAP.get(worst_held, {}).get("name", worst_held),
                            "swap_out_score": worst_held_score,
                            "score_gap": round(score - worst_held_score, 1),
                        })
                        break  # 只推荐最优的一个换仓

    # 候选买入池
    buy_candidates = []
    for code in ALL_CODES:
        if code in holding_codes:
            continue
        # 跳过已在卖出列表中的
        if code in {s["code"] for s in sells}:
            continue

        s = scores.get(code, {})
        score = s.get("total_score", 0)

        details = _parse_details(s.get("details", "{}"))
        signal = details.get("signal_action", "HOLD")
        close = details.get("price", 0)
        zone_label = details.get("zone_label", "")

        # 买入条件
        if signal not in ("STRONG_BUY", "BUY"):
            continue
        if score < enter_threshold:
            continue

        # Tier 优先级加分
        tier = STOCK_MAP.get(code, {}).get("tier", 3)
        tier_bonus = 0
        if tier == 1:
            tier_bonus = 10  # Tier 1 优先
        elif tier == 2:
            tier_bonus = 5

        buy_candidates.append({
            "code": code,
            "name": STOCK_MAP.get(code, {}).get("name", code),
            "score": score,
            "signal": signal,
            "price": close,
            "tier": tier,
            "tier_bonus": tier_bonus,
            "zone_label": zone_label,
            "effective_score": score + tier_bonus,
            "reason": STOCK_DETAILS.get(code, {}).get("reason", ""),
        })

    # 按有效评分排序
    buy_candidates.sort(key=lambda x: x["effective_score"], reverse=True)

    # 分配仓位
    for candidate in buy_candidates:
        if open_slots <= 0:
            break

        price = candidate["price"]
        if price <= 0:
            continue

        # 计算仓位: 可用资金 * 单只最大权重，但不超过现金
        position_cash = min(
            available_cash * max_single_weight,
            available_cash * (1 - reserve_cash_ratio) / max(open_slots, 1)
        )
        shares = int(position_cash / price / 100) * 100
        if shares < 100:
            continue

        amount = shares * price
        if amount < CAPITAL_CONFIG.get("min_single_weight", 0.25) * total_value:
            continue

        buys.append({
            "code": candidate["code"],
            "name": candidate["name"],
            "action": "BUY",
            "score": candidate["score"],
            "signal": candidate["signal"],
            "price": price,
            "shares": shares,
            "amount": round(amount, 2),
            "tier": candidate["tier"],
            "zone_label": candidate["zone_label"],
            "reason": candidate["reason"],
        })

        available_cash -= amount
        open_slots -= 1

    # ── Phase 3: 生成摘要 ──────────────────────────────────
    summary_lines = []
    summary_lines.append(f"📊 Serenity 自动执行计划 | {today}  大盘: {market['trend']}")
    summary_lines.append(f"{'='*60}")

    if sells:
        summary_lines.append(f"\n🔴 卖出 ({len(sells)} 笔):")
        for s in sells:
            icon = "🚨" if s["urgency"] == "high" else "⚠️"
            summary_lines.append(
                f"  {icon} {s['name']}({s['code']}) "
                f"卖出{s['shares']}股 @ ~{s['estimated_proceeds']/max(s['shares'],1):.2f} "
                f"评分{s['score']:.0f} 盈亏{s['profit_pct']:+.1f}%"
            )
            for r in s["reasons"]:
                summary_lines.append(f"     └ {r}")

    if buys:
        summary_lines.append(f"\n🟢 买入 ({len(buys)} 笔):")
        for b in buys:
            tier_star = "⭐" if b["tier"] == 1 else ""
            summary_lines.append(
                f"  🎯{tier_star} {b['name']}({b['code']}) "
                f"买入{b['shares']}股 @ {b['price']:.2f} "
                f"≈{b['amount']:.0f}元 评分{b['score']:.0f} {b['signal']}"
            )
            summary_lines.append(f"     └ {b['reason'][:60]}")

    if not sells and not buys and not swap_candidates:
        summary_lines.append("\n✅ 无需操作：持仓评分均在持有区间，无可买入信号")
    if swap_candidates:
        for sw in swap_candidates:
            summary_lines.append(f"\n🔄 建议换仓: {sw['swap_out_name']}({sw['swap_out_code']}) "
                                 f"评分{sw['swap_out_score']:.0f} → {sw['name']}({sw['code']}) "
                                 f"评分{sw['score']:.0f} (差距+{sw['score_gap']:.0f})")
            summary_lines.append(f"   先卖出 {sw['swap_out_name']}，然后买入 {sw['name']}")

    summary_lines.append(f"\n{'─'*60}")
    summary_lines.append(f"现金: {cash:.0f} | 卖出释放: {freed_cash:.0f} | 买入计划: {sum(b['amount'] for b in buys):.0f}")
    
    # 三策略分配建议（基于大盘择时）
    try:
        from config import MARKET_ADJUSTMENTS, STRATEGY_ALLOCATION
        regime_map = {'积极': '牛市', '机会': '牛市', '危险': '熊市', '谨慎': '熊市', '中性': '震荡市'}
        regime = regime_map.get(market['trend'], '震荡市')
        adj = MARKET_ADJUSTMENTS.get(regime, {})
        base_div = STRATEGY_ALLOCATION['dividend_lowvol']['weight']
        base_quant = STRATEGY_ALLOCATION['multi_factor_quant']['weight']
        div_w = base_div + adj.get('dividend_weight', 0)
        quant_w = base_quant + adj.get('quant_weight', 0)
        summary_lines.append(f"策略分配: 红利{div_w*100:.0f}% 多因子{quant_w*100:.0f}% ({regime}模式)")
    except Exception:
        pass
    


    # Executable CLI commands
    if sells or buys or swap_candidates:
        summary_lines.append("")
        summary_lines.append("📋 可执行命令:")
        for s in sells:
            summary_lines.append(f"  python3 cli.py trade {s['code']} sell {s['estimated_proceeds']:.0f}")
        for sw in swap_candidates:
            summary_lines.append(f"  # 换仓: 卖出 {sw['swap_out_code']}")
            summary_lines.append(f"  python3 cli.py trade {sw['swap_out_code']} sell")
            summary_lines.append(f"  python3 cli.py trade {sw['code']} buy")
        for b in buys:
            summary_lines.append(f"  python3 cli.py trade {b['code']} buy {b['amount']:.0f}")
    
    summary_lines.append(f"{'='*60}")
    
    return {
        "date": today,
        "cash": cash,
        "total_value": total_value,
        "available_after_sells": cash + freed_cash,
        "sells": sells,
        "buys": buys,
        "swaps": swap_candidates,
        "summary": "\n".join(summary_lines),
    }


def main():
    dry_run = "--dry-run" in sys.argv
    do_push = "--push" in sys.argv

    plan = generate_execution_plan(dry_run=dry_run)
    print(plan["summary"])

    if do_push and (plan["sells"] or plan["buys"]):
        try:
            from notifier import send_message
            send_message(
                f"📊 Serenity 自动调仓 {plan['date']}",
                plan["summary"],
                content_type="markdown",
            )
            print("\n📡 已推送调仓计划")
        except Exception as e:
            print(f"\n⚠️ 推送失败: {e}")

    # 输出结构化数据供其他脚本使用
    if "--json" in sys.argv:
        import json
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
