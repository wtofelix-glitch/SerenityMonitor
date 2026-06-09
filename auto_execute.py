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
    python3 auto_execute.py                       # 生成调仓计划
    python3 auto_execute.py --push                # 生成 + 推送
    python3 auto_execute.py --dry-run             # 仅查看，不写入任何记录
    python3 auto_execute.py --force-execute       # 强制执行 + 自动重试 (3次)
    python3 auto_execute.py --stats               # 查看执行统计
    python3 auto_execute.py --premarket           # 盘前推送今日计划
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date

from serenity_logger import get_logger
from db import get_conn, load_all_stocks
from config import (
    STOCK_MAP, STOCK_DETAILS, ALL_CODES, TIER_1_CODES,
    SIGNAL_CONFIG, CAPITAL_CONFIG, RISK_CONFIG,
)

log = get_logger(__name__)

from risk_manager import get_risk_manager
risk = get_risk_manager()

# ── 熔断保护 ──
CIRCUIT_BREAKER_DD = abs(RISK_CONFIG.get("max_portfolio_drawdown", -0.12))  # 与RISK_CONFIG对齐
INITIAL_CAPITAL = 50000.0


# 大盘择时调整参数
def _get_market_adjustments():
    """获取大盘择时对仓位参数的调整系数
    
    核心逻辑：
    - 牛市（MA20 > MA60）：超卖=抄底机会，重仓集中出击
    - 熊市（MA20 < MA60）：现金为王，仅极端超卖时允许小仓位试探
    """
    try:
        from market_timing import get_market_signal
        sig = get_market_signal()
    except Exception:
        return {'trend': '中性', 'max_pos': 2, 'max_single': 0.50, 'enter_adj': 0,
                'bull': True, 'rsi': 50}

    sh_data = sig.get('sh', {})
    sh_rsi = sh_data.get('rsi', 50)
    sh_ma20 = sh_data.get('ma20', 0)
    sh_ma60 = sh_data.get('ma60', 0)
    is_bull = sh_ma20 > sh_ma60

    if is_bull:
        if sh_rsi < 30:
            return {'trend': '超卖抄底', 'max_pos': 1, 'max_single': 0.80,
                    'enter_adj': -8, 'bull': True, 'rsi': sh_rsi}
        elif sh_rsi < 45:
            return {'trend': '回调加仓', 'max_pos': 2, 'max_single': 0.60,
                    'enter_adj': -4, 'bull': True, 'rsi': sh_rsi}
        elif sh_rsi < 65:
            return {'trend': '正常持仓', 'max_pos': 2, 'max_single': 0.50,
                    'enter_adj': 0, 'bull': True, 'rsi': sh_rsi}
        else:
            return {'trend': '高位减仓', 'max_pos': 1, 'max_single': 0.30,
                    'enter_adj': +5, 'bull': True, 'rsi': sh_rsi}
    else:
        if sh_rsi < 25:
            return {'trend': '熊市超卖', 'max_pos': 1, 'max_single': 0.30,
                    'enter_adj': +3, 'bull': False, 'rsi': sh_rsi}
        else:
            return {'trend': '熊市防守', 'max_pos': 0, 'max_single': 0,
                    'enter_adj': +99, 'bull': False, 'rsi': sh_rsi}

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

    # ── 风险检查（熔断 + 最大回撤 + 日亏损） ──
    risk_check = risk.is_trade_allowed(
        code="", action="SELL",
        holdings=holdings,
        current_total_value=total_value,
        initial_capital=INITIAL_CAPITAL,
    )
    if not risk_check["allowed"]:
        critical_reasons = [r for r in risk_check["reasons"] if "熔断" in r or "回撤" in r]
        if critical_reasons:
            log.warning("风控触发: %s", "; ".join(critical_reasons))

    # ── 熔断保护：总回撤 > 15% → 强制清仓 ──
    portfolio_dd = (INITIAL_CAPITAL - total_value) / INITIAL_CAPITAL
    if portfolio_dd > CIRCUIT_BREAKER_DD:
        sells = []
        for h in holdings:
            code = h["code"]
            name = STOCK_MAP.get(code, {}).get("name", code)
            amt = h.get("trade_amount", 0) or 0
            buy_price = h.get("buy_price", 1)
            shares = int(amt / buy_price / 100) * 100 if buy_price > 0 else 0
            s = scores.get(code, {})
            d = _parse_details(s.get("details", "{}"))
            close = d.get("price", buy_price)
            estimated = shares * close
            profit = ((close - buy_price) / buy_price * 100) if buy_price > 0 else 0
            sells.append({
                "code": code, "name": name, "action": "SELL",
                "score": s.get("total_score", 0),
                "shares": shares,
                "estimated_proceeds": round(estimated, 2),
                "profit_pct": round(profit, 1),
                "reasons": [f"🚨 熔断！总回撤 {portfolio_dd*100:.1f}% > {CIRCUIT_BREAKER_DD*100:.0f}%，强制清仓"],
                "urgency": "high",
            })
        # 生成熔断摘要
        freed = sum(s["estimated_proceeds"] for s in sells)
        summary_lines = [
            f"🚨 熔断触发 | {today}",
            f"总回撤: {portfolio_dd*100:.1f}% > 阈值 {CIRCUIT_BREAKER_DD*100:.0f}%",
            f"清仓 {len(sells)} 只标的，回收 ¥{freed:,.0f}",
            f"现金: ¥{cash + freed:,.0f} — 等待市场企稳",
        ]
        return {
            "date": today, "cash": cash, "total_value": total_value,
            "sells": sells, "buys": [], "swaps": [],
            "summary": "\n".join(summary_lines),
        }

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

        # 最短持有天数：防止买入当日即触发目标价卖出
        buy_date_str = h.get("buy_date", "")
        min_hold_days = 2
        held_days = 999
        if buy_date_str:
            try:
                buy_date = date.fromisoformat(buy_date_str)
                held_days = (date.today() - buy_date).days
            except ValueError:
                pass

        reasons = []
        should_sell = False

        # 条件1: 评分跌破退出阈值
        if score < exit_threshold:
            should_sell = True
            reasons.append(f"评分{score:.0f} < 退出线{exit_threshold}")

        # 条件2: 已达目标价
        if zone_label == "已达目标" and close >= target_sell and target_sell > 0 and held_days >= min_hold_days:
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

    # 将换仓建议转为实际买卖订单
    for sw in swap_candidates:
        swap_out = sw["swap_out_code"]
        h = next((x for x in holdings if x["code"] == swap_out), None)
        if not h:
            continue
        amt = h.get("trade_amount", 0) or 0
        buy_price = h.get("buy_price", 1)
        shares = int(amt / buy_price / 100) * 100 if buy_price > 0 else 0
        s_details = _parse_details(scores.get(sw["code"], {}).get("details", "{}"))
        # 用换出标的的实际收盘价
        out_details = _parse_details(scores.get(swap_out, {}).get("details", "{}"))
        close = out_details.get("price", buy_price)
        estimated = shares * close
        profit = ((close - buy_price) / buy_price * 100) if buy_price > 0 else 0
        sells.append({
            "code": swap_out,
            "name": sw["swap_out_name"],
            "action": "SELL",
            "score": sw["swap_out_score"],
            "shares": shares,
            "estimated_proceeds": round(estimated, 2),
            "profit_pct": round(profit, 1),
            "reasons": [f"换仓→{sw['name']}(评分+{sw['score_gap']:.0f})"],
            "urgency": "medium",
        })
        freed_cash += estimated
        open_slots += 1

        # 换仓买入：用换出资金买入换入标的
        in_price = s_details.get("price", 0)
        if in_price > 0:
            swap_budget = estimated + cash  # 换出资金 + 剩余现金
            in_shares = int(swap_budget * 0.95 / in_price / 100) * 100
            if in_shares >= 100:
                in_amount = in_shares * in_price
                buys.append({
                    "code": sw["code"],
                    "name": sw["name"],
                    "action": "BUY",
                    "score": sw["score"],
                    "signal": s_details.get("signal_action", "BUY"),
                    "price": in_price,
                    "shares": in_shares,
                    "amount": round(in_amount, 2),
                    "tier": STOCK_MAP.get(sw["code"], {}).get("tier", 3),
                    "zone_label": s_details.get("zone_label", ""),
                    "reason": STOCK_DETAILS.get(sw["code"], {}).get("reason", ""),
                })
                freed_cash -= in_amount
                open_slots -= 1
    # 更新可用资金（含换仓卖出释放）
    available_cash = cash + freed_cash
    remaining_holdings = len(holdings) - len(sells)
    open_slots = max(0, max_positions - remaining_holdings)

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

    # 分配仓位（含风控检查）
    for candidate in buy_candidates:
        if open_slots <= 0:
            break

        code_check = candidate["code"]

        # 风控检查：黑名单 + 冷却 + 行业集中度
        risk_check = risk.is_trade_allowed(
            code=code_check, action="BUY",
            holdings=holdings,
            current_total_value=total_value,
            initial_capital=INITIAL_CAPITAL,
            new_amount=available_cash * max_single_weight,
        )
        if not risk_check["allowed"]:
            log.info("风控拦截买入 %s: %s", code_check, "; ".join(risk_check["reasons"]))
            continue

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


# ── 强制信号执行 ──────────────────────────────────────

def _record_execution_orders(plan: dict):
    """将调仓计划中的订单写入执行日志"""
    from db import get_conn, init_db
    init_db()  # 确保 execution_log 表存在
    today = plan["date"]
    conn = get_conn()
    # 先清理当日旧记录，避免重复
    for s in plan["sells"]:
        conn.execute("""
            DELETE FROM execution_log
            WHERE date = ? AND code = ? AND action = ? AND status IN ('pending', 'failed')
        """, (today, s["code"], "SELL"))
        conn.execute("""
            INSERT INTO execution_log
                (date, code, action, status, price, shares, amount, reason, attempt)
            VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, 1)
        """, (today, s["code"], "SELL", s["shares"], s["estimated_proceeds"],
              "; ".join(s.get("reasons", []))[:200]))
    for b in plan["buys"]:
        conn.execute("""
            DELETE FROM execution_log
            WHERE date = ? AND code = ? AND action = ? AND status IN ('pending', 'failed')
        """, (today, b["code"], "BUY"))
        conn.execute("""
            INSERT INTO execution_log
                (date, code, action, status, price, shares, amount, reason, attempt)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 1)
        """, (today, b["code"], "BUY", b["price"], b["shares"], b["amount"],
              b.get("reason", "")[:200]))
    conn.commit()
    conn.close()


def _retry_pending_executions(dry_run: bool = False) -> int:
    """重试当天待执行的订单（最多 3 次）"""
    from db import get_conn, clear_active, set_active, add_trade, init_db
    init_db()
    from data_engine import fetch_single
    from config import STOCK_DETAILS

    today = date.today().isoformat()
    conn = get_conn()
    pending = conn.execute("""
        SELECT * FROM execution_log
        WHERE date = ? AND status = 'pending' AND attempt < max_attempts
        ORDER BY attempt DESC
    """, (today,)).fetchall()
    conn.close()

    executed = 0
    for row in pending:
        r = dict(row)
        code = r["code"]
        action = r["action"]
        attempt = r["attempt"] + 1

        # 获取实时价格
        try:
            data = fetch_single(code)
            price = data.get("price", 0)
            if price <= 0:
                raise ValueError(f"价格无效: {price}")
        except Exception as e:
            _update_execution(code, today, "failed", attempt=attempt,
                              error_msg=f"获取价格失败: {e}", action=action)
            continue

        shares = r["shares"]
        if shares <= 0:
            # 自动计算可买股数
            if action == "BUY" and price > 0:
                shares = int(r["amount"] * 0.95 / price / 100) * 100
            if shares < 100:
                _update_execution(code, today, "failed", attempt=attempt,
                                  error_msg="股数不足100", action=action)
                continue

        if dry_run:
            log.info("[DRY-RUN] %s %s %s股 @ %.2f", action, code, shares, price)
            _update_execution(code, today, "executed", attempt=attempt, price=price,
                              shares=shares, error_msg="dry-run", action=action)
            executed += 1
            continue

        try:
            if action == "SELL":
                clear_active(code)
                add_trade(code, "sell", price, shares, today,
                          f"force-execute (重试#{attempt-1})", trade_amount=shares*price)
            elif action == "BUY":
                target = STOCK_DETAILS.get(code, {})
                stop_price = round(price * 0.92, 2)  # 8% 止损
                set_active(code, price, today,
                          target.get("target_sell", 0), target.get("buy_zone_low", 0))
                conn2 = get_conn()
                conn2.execute('UPDATE stocks SET stop_loss = ?, trade_amount = ?, notes = ? WHERE code = ?',
                           (stop_price, shares * price, f'force-execute', code))
                conn2.commit()
                conn2.close()
                add_trade(code, "buy", price, shares, today,
                          f"force-execute (重试#{attempt-1})", trade_amount=shares*price)

            _update_execution(code, today, "executed", attempt=attempt, price=price, shares=shares, action=action)
            executed += 1
            print(f"  ✅ {action} {code} {shares}股 @ {price:.2f}")

        except Exception as e:
            _update_execution(code, today, "failed", attempt=attempt, error_msg=str(e), action=action)
            log.error("强制执行 %s %s 失败: %s", action, code, e, exc_info=True)
            print(f"  ❌ {action} {code} 失败: {e}")

    return executed


def _update_execution(code: str, date_str: str, status: str,
                      attempt: int = 1, price: float = 0, shares: int = 0,
                      error_msg: str = "", action: str = ""):
    """更新执行日志条目状态"""
    from db import get_conn, init_db
    init_db()
    conn = get_conn()
    where_extra = "AND action = ?" if action else ""
    params = (status, attempt, price, shares, error_msg, code, date_str)
    if action:
        params = params + (action,)
    conn.execute(f"""
        UPDATE execution_log
        SET status = ?, attempt = ?, price = ?, shares = ?,
            error_msg = ?, updated_at = datetime('now', 'localtime')
        WHERE code = ? AND date = ? AND status = 'pending' {where_extra}
    """, params)
    conn.commit()
    conn.close()


def cmd_force_execute():
    """强制信号执行模式 — 记录 + 执行 + 重试"""
    dry_run = "--dry-run" in sys.argv
    do_push = "--push" in sys.argv

    today = date.today().isoformat()
    print(f"🚀 强制信号执行 | {today}")
    print("=" * 50)

    # 1. 生成最新调仓计划
    plan = generate_execution_plan(dry_run=dry_run)
    print(plan["summary"])
    print()

    # 2. 记录待执行订单
    _record_execution_orders(plan)
    total_orders = len(plan["sells"]) + len(plan["buys"])
    if total_orders > 0:
        print(f"📝 记录 {total_orders} 笔待执行订单")
    else:
        print("✅ 无待执行订单")

    # 3. 执行（含重试）
    if total_orders > 0 or _pending_count() > 0:
        print("\n🔄 执行中...")
        executed = _retry_pending_executions(dry_run=dry_run)
        print(f"\n✅ 本次执行 {executed} 笔")

    # 4. 推送
    if do_push and (plan["sells"] or plan["buys"]):
        try:
            from notifier import send_message
            send_message(
                f"🚀 Serenity 强制执行 {today}",
                plan["summary"],
                content_type="markdown",
            )
            print("\n📡 已推送")
        except Exception as e:
            print(f"\n⚠️ 推送失败: {e}")

    return plan


def cmd_execution_stats():
    """显示当日信号执行统计"""
    from db import get_conn, init_db
    init_db()
    today = date.today().isoformat()
    conn = get_conn()

    # 今日总信号
    total = conn.execute(
        "SELECT COUNT(*) as c FROM execution_log WHERE date = ?", (today,)
    ).fetchone()["c"]

    if total == 0:
        print("📊 今日尚无执行记录（使用 --force-execute 生成）")
        conn.close()
        return

    # 按状态分组
    statuses = conn.execute("""
        SELECT status, COUNT(*) as c FROM execution_log
        WHERE date = ? GROUP BY status
    """, (today,)).fetchall()

    executed = next((s["c"] for s in statuses if s["status"] == "executed"), 0)
    failed = next((s["c"] for s in statuses if s["status"] == "failed"), 0)
    pending = next((s["c"] for s in statuses if s["status"] == "pending"), 0)

    # 按 action 分组
    by_action = conn.execute("""
        SELECT action, status, COUNT(*) as c FROM execution_log
        WHERE date = ? GROUP BY action, status
    """, (today,)).fetchall()

    conn.close()

    rate = executed / total * 100 if total > 0 else 0

    print(f"📊 信号执行统计 | {today}")
    print("=" * 50)
    print(f"总信号:  {total}")
    print(f"已执行:  {executed} ({rate:.1f}%)")
    print(f"待执行:  {pending}")
    print(f"失败:    {failed}")
    print()
    print("明细:")
    for row in by_action:
        r = dict(row)
        emoji = {"executed": "✅", "failed": "❌", "pending": "⏳"}.get(r["status"], "❓")
        print(f"  {emoji} {r['action']:<6} {r['status']:<10} {r['c']} 笔")
    print("=" * 50)


def cmd_premarket_push():
    """盘前推送今日计划"""
    from db import get_conn, init_db
    init_db()
    from notifier import send_message

    today = date.today().isoformat()
    conn = get_conn()
    pending_orders = conn.execute("""
        SELECT * FROM execution_log
        WHERE date = ? AND status IN ('pending', 'failed') AND attempt < max_attempts
    """, (today,)).fetchall()
    conn.close()

    if not pending_orders:
        # 尝试从昨日的执行计划生成盘前简报
        plan = generate_execution_plan(dry_run=True)
        if not plan["sells"] and not plan["buys"]:
            print("📭 今日无待执行信号，无需盘前推送")
            return
        _record_execution_orders(plan)
        msg = plan["summary"]
    else:
        lines = [f"⏰ Serenity 盘前简报 | {today}"]
        lines.append("=" * 50)
        for row in pending_orders:
            r = dict(row)
            emoji = "🟢" if r["action"] == "BUY" else "🔴"
            status_emoji = {"pending": "⏳", "failed": "❌"}.get(r["status"], "❓")
            lines.append(f"  {status_emoji}{emoji} {r['action']} {r['code']} "
                        f"{r['shares']}股 @ ~{r.get('price', 0):.2f} "
                        f"(第{r['attempt']}/{r['max_attempts']}次)")
        remaining = sum(1 for r in pending_orders if r["status"] == "pending")
        if remaining > 0:
            lines.append(f"\n⏳ {remaining} 笔待执行（开盘后将自动重试）")
        msg = "\n".join(lines)

    print(msg)
    try:
        send_message(f"⏰ Serenity 盘前 {today}", msg, content_type="markdown")
        print("\n📡 盘前简报已推送")
    except Exception as e:
        print(f"\n⚠️ 推送失败: {e}")


def _pending_count() -> int:
    """待执行订单数"""
    from db import get_conn, init_db
    init_db()
    today = date.today().isoformat()
    conn = get_conn()
    c = conn.execute("""
        SELECT COUNT(*) as c FROM execution_log
        WHERE date = ? AND status = 'pending' AND attempt < max_attempts
    """, (today,)).fetchone()["c"]
    conn.close()
    return c


def main():
    dry_run = "--dry-run" in sys.argv
    do_push = "--push" in sys.argv
    do_execute = "--execute" in sys.argv
    do_force_execute = "--force-execute" in sys.argv
    do_stats = "--stats" in sys.argv
    do_premarket = "--premarket" in sys.argv

    # ── 独立模式 ──
    if do_stats:
        cmd_execution_stats()
        return
    if do_premarket:
        cmd_premarket_push()
        return
    if do_force_execute:
        cmd_force_execute()
        return

    plan = generate_execution_plan(dry_run=dry_run)
    print(plan["summary"])

    # ── 自动执行 ──
    if do_execute and (plan["sells"] or plan["buys"]):
        from db import set_active, clear_active, add_trade
        from config import STOCK_DETAILS
        from backtest_engine import recommend_atr_params
        today = plan["date"]
        executed = []

        for s in plan["sells"]:
            price = s["estimated_proceeds"] / max(s["shares"], 1)
            clear_active(s["code"])
            add_trade(s["code"], "sell", price, s["shares"], today,
                      f'auto: {" ".join(s.get("reasons",[]))}'[:200])
            executed.append(f'  ✅ 卖出 {s["name"]}({s["code"]}) {s["shares"]}股 @{price:.2f}')

        for b in plan["buys"]:
            price = b["price"]
            # 计算并设置止损
            try:
                atr_rec = recommend_atr_params(b["code"])
                stop_pct = atr_rec.get("suggested_stop_pct", 8.0)
            except Exception:
                stop_pct = 8.0
            stop_price = round(price * (1 - stop_pct / 100), 2)
            
            target = STOCK_DETAILS.get(b["code"], {})
            target_high = target.get("target_sell", 0)
            target_low = target.get("buy_zone_low", 0)

            set_active(b["code"], price, today, target_high, target_low)
            # 手动更新止损（set_active 不设止损）
            try:
                conn = get_conn()
                conn.execute('UPDATE stocks SET stop_loss = ?, trade_amount = ?, notes = ? WHERE code = ?',
                           (stop_price, b["amount"], f'auto买入{b["shares"]}股', b["code"]))
                conn.commit()
                conn.close()
            except Exception:
                pass

            add_trade(b["code"], "buy", price, b["shares"], today,
                      f'auto: score={b.get("score",0):.0f} {b.get("signal","")}', trade_amount=b["amount"])
            executed.append(f'  ✅ 买入 {b["name"]}({b["code"]}) {b["shares"]}股 @{price:.2f} 止损{stop_price}')

        if executed:
            print("\\n🚀 自动执行完成:")
            for line in executed:
                print(line)

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

        # 同时推送到 Telegram
        try:
            from signal_push import push_execution_plan
            push_execution_plan(plan)
            print("📡 Telegram 已推送")
        except Exception:
            pass

    # 输出结构化数据供其他脚本使用
    if "--json" in sys.argv:
        import json
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
