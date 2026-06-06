#!/usr/bin/env python3
"""每日信号 outcome 补填 - 收盘后计算1d/3d/5d/10d收益表现"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_unfilled_outcomes, get_price_history, update_signal_outcome


def calculate_outcomes():
    signs = get_unfilled_outcomes(since_days=60)
    if not signs:
        print("✅ 所有信号 outcome 已填充")
        return

    filled = 0
    for s in signs:
        code = s["code"]
        sig_date = s["date"]
        sig_price = s["price"] or 0
        if sig_price <= 0:
            continue

        # 获取信号日期之后的行情
        rows = get_price_history(code, 30)
        rows_sorted = sorted(rows, key=lambda r: r["date"])
        # 找到信号日期的索引
        idx = None
        for i, r in enumerate(rows_sorted):
            if r["date"] >= sig_date:
                idx = i
                break
        if idx is None:
            continue

        periods = {
            "outcome_1d": 1,
            "outcome_3d": 3,
            "outcome_5d": 5,
            "outcome_10d": 10,
        }
        for field, offset in periods.items():
            target_idx = idx + offset
            if target_idx < len(rows_sorted):
                close_price = rows_sorted[target_idx]["close"]
                if close_price and close_price > 0:
                    ret = (close_price - sig_price) / sig_price * 100
                    update_signal_outcome(s["id"], field, round(ret, 2))
                    filled += 1

    print(f"✅ 已填充 {filled} 个 outcome 字段 ({len(signs)} 条信号)")


if __name__ == "__main__":
    calculate_outcomes()
