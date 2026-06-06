"""
每日调仓建议 — 基于因子信号 + Rank IC 生成调仓建议并推送微信

Usage:
    from daily_rebalance import generate_rebalance_push
    generate_rebalance_push()
"""

import logging
from datetime import date
from typing import Optional

from config import STOCK_MAP

logger = logging.getLogger("Serenity.DailyRebalance")


def generate_rebalance_push() -> Optional[str]:
    """
    生成调仓建议并推送微信。

    流程:
        1. 获取因子信号 (factor_engine.get_current_signals())
        2. 获取 Rank IC 数据
        3. 获取当前持仓
        4. 运行组合优化 (PositionOptimizer.optimize_allocation())
        5. 格式化为微信友好文本
        6. 通过 notifier.push_daily_report() 推送

    Returns
    -------
    str or None — 推送的文本内容（成功时）或 None（失败时）
    """
    from factor_engine import get_current_signals
    from portfolio_optimizer import PositionOptimizer
    from db import load_all_stocks

    # 1. 获取因子信号
    signals = get_current_signals()
    if not signals:
        msg = "⚠️ 无因子信号数据，无法生成调仓建议"
        logger.warning(msg)
        print(msg)
        return None

    # 2. 获取 IC 数据（可选）
    ic_data = None
    try:
        from factor_ic import compute_rank_ic
        ic_data = compute_rank_ic(days=30, window=20)
        if "error" in ic_data:
            logger.warning(f"Rank IC 数据不可用: {ic_data['error']}")
            ic_data = None
    except Exception as e:
        logger.warning(f"Rank IC 计算失败: {e}")

    # 3. 获取当前持仓
    stocks = load_all_stocks()
    positions = [s for s in stocks if s.get("is_active")]

    # 4. 获取可用现金
    from portfolio import get_portfolio
    pm = get_portfolio()
    cash = pm.get_cash()

    # 5. 运行优化
    opt = PositionOptimizer()
    allocation = opt.optimize_allocation(signals, positions, cash, ic_data)

    if not allocation:
        msg = "⚠️ 优化结果为空，无法生成调仓建议"
        logger.warning(msg)
        print(msg)
        return None

    # 6. 格式化输出
    today = date.today().isoformat()
    lines = [f"📊 Serenity 调仓建议 {today}", ""]

    # 按动作分组
    reduce_items = []   # 减仓/卖出
    add_items = []      # 加仓/买入
    hold_items = []     # 持有

    for code, alloc in allocation.items():
        name = STOCK_MAP.get(code, {}).get("name", code)
        cur_pct = alloc["current_weight"] * 100
        tgt_pct = alloc["suggested_weight"] * 100
        action = alloc["action"]
        diff = alloc["diff_amount"]

        if action in ("SELL", "REDUCE"):
            reduce_items.append((name, code, cur_pct, tgt_pct, diff))
        elif action == "BUY":
            add_items.append((name, code, cur_pct, tgt_pct, diff))
        else:
            hold_items.append((name, code, cur_pct, tgt_pct))

    # 减仓
    if reduce_items:
        parts = []
        for name, code, cur, tgt, diff in reduce_items:
            parts.append(f"{name}({cur:.1f}→{tgt:.1f}%)")
        lines.append(f"🟡减仓: {'，'.join(parts)}")
        lines.append("")

    # 加仓
    if add_items:
        parts = []
        for name, code, cur, tgt, diff in add_items:
            parts.append(f"{name}({cur:.1f}→{tgt:.1f}%)")
        lines.append(f"🟢加仓: {'，'.join(parts)}")
        lines.append("")

    # 持有
    if hold_items:
        parts = []
        for name, code, cur, tgt in hold_items:
            parts.append(f"{name}({cur:.1f}%→{tgt:.1f}%)")
        lines.append(f"⚪持有: {'，'.join(parts)}")
        lines.append("")

    # 汇总
    total_buy = sum(a["diff_amount"] for a in allocation.values() if a["diff_amount"] > 0)
    total_sell = sum(abs(a["diff_amount"]) for a in allocation.values() if a["diff_amount"] < 0)
    if total_buy > 0 or total_sell > 0:
        lines.append(f"💸 预计调仓: 买入 {total_buy:.0f}元 / 卖出 {total_sell:.0f}元")

    lines.append("")
    lines.append("---")
    lines.append("> SerenityMonitor 自动推送")

    content = "\n".join(lines)

    # 7. 推送微信
    try:
        from notifier import push_daily_report
        push_daily_report(content)
        print(content)
        print("\n✅ 调仓建议已推送微信")
    except Exception as e:
        logger.error(f"调仓建议推送失败: {e}")
        print(content)
        print(f"\n⚠️ 推送失败: {e}，内容如上")

    return content


if __name__ == "__main__":
    generate_rebalance_push()
