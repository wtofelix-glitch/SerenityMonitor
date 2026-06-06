"""基本面数据每日更新 — no_agent cron 脚本
在 16:18 fetch_history→rescore 之后运行，更新所有标的 get_fundamental_signal()
输出非空时推送到微信

用法: python3 serenity_fundamental_update.py
"""
import sys
import os

# Serenity 项目根目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fundamental_engine import FundamentalEngine
    from config import STOCK_MAP

    fe = FundamentalEngine()
    codes = list(STOCK_MAP.keys())

    results = []
    for code in codes:
        signal = fe.get_fundamental_signal(code)
        name = STOCK_MAP[code].get("name", code)
        if signal is not None:
            results.append(f"{name}({code}): {signal:+.4f}")
        else:
            results.append(f"{name}({code}): ⚠️ 无数据")

    print(f"📊 Serenity 基本面因子更新 {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}")
    for r in results:
        print(r)

    # 输出统计
    scores = [float(r.split(": ")[1]) for r in results if "⚠️" not in r]
    if scores:
        print(f"\n均值: {sum(scores)/len(scores):+.4f}  |  范围: [{min(scores):+.4f}, {max(scores):+.4f}]")

except Exception as e:
    print(f"❌ 基本面更新失败: {e}")
    sys.exit(1)
