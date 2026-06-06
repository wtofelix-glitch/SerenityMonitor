#!/usr/bin/env python3
"""
14因子独立信号推送
使用 factor_engine 的14因子计算，独立于 scorer 评分体系
并行于现有 signal_push.py 的哨兵信号

用法:
    python3 fourteen_factor_push.py          # 推送信号简报
    python3 fourteen_factor_push.py --silent  # 无信号静默（cron用）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date
from factor_engine import AlphaFactorEngine
from config import ALL_CODES, STOCK_MAP
import numpy as np


def compute_14factor_signal(code):
    """
    计算14因子信号值 [0-100]，映射到和回测一样的逻辑。

    factor_engine.compute_all_factors() 返回结构:
        {
            "candle": {...},
            "ts": {...},
            "signals": {  # 14个信号因子，每个归一化到 [-1, 1]
                "ksft": 0.123,
                "rank_20": 0.456,
                ...,
                "wq_alpha19": -0.234,
            },
            "descriptive": {...}
        }
    """
    engine = AlphaFactorEngine()
    factors = engine.compute_all_factors(code)
    if not factors or 'signals' not in factors:
        return None

    sigs = factors['signals']
    vals = [v for v in sigs.values() if v is not None and not np.isnan(v)]
    if not vals:
        return None

    avg = np.mean(vals)
    # 映射逻辑（和回测保持一致）：
    # 14因子平均信号 avg ∈ [-1, 1]
    # avg_signal * 2 映射到 [-2, 2]，然后 clip 到 [-1, 1]
    # 再映射到 [0, 100]
    signal = np.clip(avg * 2, -1.0, 1.0)
    score = round(signal * 50 + 50, 1)

    name = STOCK_MAP.get(code, {}).get('name', code) if isinstance(STOCK_MAP.get(code), dict) else STOCK_MAP.get(code, code)

    return {
        'code': code,
        'name': name,
        'avg_factor': round(avg, 3),
        'signal': round(signal, 3),
        'score': score,
        'action': 'BUY' if signal > 0.2 else ('SELL' if signal < -0.2 else 'HOLD'),
    }


def compute_14factor_all():
    """对所有标的计算14因子信号"""
    results = []
    errors = []
    for code in ALL_CODES:
        try:
            r = compute_14factor_signal(code)
            if r:
                results.append(r)
            else:
                errors.append(code)
        except Exception as e:
            errors.append(f"{code}({e})")
    return results, errors


def build_report(silent=False):
    """构建推送报告"""
    results, errors = compute_14factor_all()
    if not results:
        return None

    results.sort(key=lambda x: x['score'], reverse=True)
    buys = [r for r in results if r['action'] == 'BUY']

    # 静默模式：无买入信号时不推送
    if not buys and silent:
        return None

    today = date.today().isoformat()
    lines = [f"🧬 **14因子独立信号** | {today}", ""]

    # 买入信号
    if buys:
        lines.append(f"🟢 **买入信号 ({len(buys)}只)**")
        for r in buys:
            lines.append(
                f"- **{r['name']}** ({r['code']}) "
                f"因子评分 {r['score']:.0f} | "
                f"信号 {r['signal']:.2f}"
            )
    else:
        lines.append("⚪ **无买入信号** — 全部持币观望")

    lines.append("")
    lines.append("---")
    lines.append(f"📊 **14因子排名 Top 5**")
    for i, r in enumerate(results[:5]):
        emoji = '🟢' if r['action'] == 'BUY' else ('🔴' if r['action'] == 'SELL' else '⚪')
        lines.append(f"  {i+1}. {emoji} **{r['name']}** {r['score']:.0f}分 "
                      f"(信号{r['signal']:.2f}, 均值{r['avg_factor']:.3f})")

    # 错误信息
    if errors:
        lines.append("")
        lines.append(f"⚠️ 计算失败: {len(errors)}只 — {' '.join(errors[:3])}")

    return '\n'.join(lines)


if __name__ == '__main__':
    silent = '--silent' in sys.argv
    msg = build_report(silent=silent)
    if msg is None:
        sys.exit(0)
    print(msg)
