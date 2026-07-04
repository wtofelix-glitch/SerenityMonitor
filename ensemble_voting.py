#!/usr/bin/env python3
"""
策略集成投票系统 — 多信号源投票委员会
源: 5个回测策略 + QuantDinger共识 + Serenity信号
3/5策略 + QD都同意BUY → 高确信标记
"""
import json
from datetime import date
from collections import Counter
from serenity_logger import get_logger

log = get_logger(__name__)

# 5回测策略 → 各给一个BUY/SELL/WATCH信号
def vote_backtest_strategies(code: str, price: float, days: int = 60) -> dict:
    """对单只股票运行5个回测策略, 返回投票结果"""
    from db import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT close FROM price_history WHERE code=? ORDER BY date DESC LIMIT ?",
        (code, days + 1),
    ).fetchall()
    conn.close()

    if len(rows) < 20:
        return {"votes": {}, "consensus": "NO_DATA", "confidence": 0}

    closes = [r["close"] for r in reversed(rows)]
    # 从原始 runs 中取最新信号
    conn = get_conn()
    sig_row = conn.execute(
        "SELECT action, total_score FROM signal_log WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    conn.close()

    votes = {}
    base_score = sig_row["total_score"] if sig_row else 50
    action = sig_row["action"] if sig_row else "HOLD"

    # 策略1: 趋势跟随 (MA20 > MA60 → BUY)
    if len(closes) >= 60:
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        votes["趋势跟随"] = "BUY" if ma20 > ma60 * 1.01 else ("SELL" if ma20 < ma60 * 0.99 else "WATCH")
    else:
        votes["趋势跟随"] = "WATCH"

    # 策略2: 多因子 (综合评分代理)
    votes["多因子"] = "BUY" if base_score >= 62 else ("SELL" if base_score < 40 else "WATCH")

    # 策略3: 均值回归 (RSI代理)
    if len(closes) >= 14:
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = sum(d for d in deltas[-14:] if d > 0)
        losses = abs(sum(d for d in deltas[-14:] if d < 0)) or 0.001
        rsi = 100 - (100 / (1 + gains / losses))
        votes["均值回归"] = "BUY" if rsi < 40 else ("SELL" if rsi > 75 else "WATCH")
    else:
        votes["均值回归"] = "WATCH"

    # 策略4: 混合策略 (趋势+回归 50-50)
    trend_vote = 1 if votes.get("趋势跟随") == "BUY" else (-1 if votes.get("趋势跟随") == "SELL" else 0)
    mr_vote = 1 if votes.get("均值回归") == "BUY" else (-1 if votes.get("均值回归") == "SELL" else 0)
    hybrid = trend_vote * 0.5 + mr_vote * 0.5
    votes["混合策略"] = "BUY" if hybrid > 0.2 else ("SELL" if hybrid < -0.2 else "WATCH")

    # 策略5: 14因子信号 (用 signals 表中的 action 代理)
    votes["14因子"] = action if action in ("BUY", "SELL", "WATCH") else ("BUY" if base_score >= 58 else "WATCH")

    # 计票
    buy_count = sum(1 for v in votes.values() if v == "BUY")
    sell_count = sum(1 for v in votes.values() if v == "SELL")
    total = len(votes)

    consensus = "HOLD"
    confidence = 0
    if buy_count >= 4:
        consensus = "STRONG_BUY"
        confidence = min(95, 60 + buy_count * 8)
    elif buy_count >= 3:
        consensus = "BUY"
        confidence = 50 + buy_count * 10
    elif sell_count >= 3:
        consensus = "SELL"
        confidence = 50 + sell_count * 10
    elif buy_count >= 2:
        consensus = "WATCH_BIAS_BUY"
        confidence = 40
    else:
        consensus = "WATCH"
        confidence = 30

    return {"votes": votes, "consensus": consensus, "confidence": confidence, "buy_count": buy_count, "sell_count": sell_count, "total_strategies": total}


def get_ensemble_signal(code: str, name: str = "", serenity_signal: str = "", serenity_score: float = 50) -> dict:
    """集成投票: 5策略 + QuantDinger + Serenity 三源融合"""
    vote = vote_backtest_strategies(code, 0)

    # QuantDinger 信号(从 scoring_history 推断)
    qd_decision = "WATCH"
    try:
        from db import get_conn
        conn = get_conn()
        qd_row = conn.execute(
            "SELECT total_score FROM scoring_history WHERE code=? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
        conn.close()
        if qd_row:
            qs = qd_row["total_score"]
            qd_decision = "BUY" if qs >= 62 else ("SELL" if qs < 38 else "WATCH")
    except Exception:
        pass

    # 三源投票
    sources = {
        "backtest": vote["consensus"],
        "quantdinger": qd_decision,
        "serenity": serenity_signal if serenity_signal in ("BUY", "STRONG_BUY", "SELL") else "WATCH",
    }

    buy_votes = sum(1 for v in sources.values() if v in ("BUY", "STRONG_BUY"))
    total_votes = 3

    # 高确信: 3/3 都同意BUY
    high_confidence = buy_votes >= 3 or (buy_votes >= 2 and vote["buy_count"] >= 4)
    ensemble_action = "STRONG_BUY" if high_confidence else ("BUY" if buy_votes >= 2 else vote["consensus"])
    ensemble_confidence = min(98, vote["confidence"] + buy_votes * 10)

    return {
        "code": code, "name": name,
        "ensemble_action": ensemble_action,
        "ensemble_confidence": ensemble_confidence,
        "high_confidence": high_confidence,
        "backtest_votes": vote,
        "quantdinger_vote": qd_decision,
        "serenity_vote": serenity_signal,
        "buy_sources": buy_votes,
    }


def run_ensemble_for_all() -> list[dict]:
    """对所有 ALL_CODES 运行集成投票, 返回高确信列表"""
    from config import ALL_CODES, STOCK_MAP
    from db import get_conn

    conn = get_conn()
    signals = conn.execute(
        "SELECT code, action, total_score FROM signal_log WHERE date=(SELECT MAX(date) FROM signal_log)"
    ).fetchall()
    conn.close()

    sig_map = {s["code"]: s for s in signals}

    results = []
    for code in ALL_CODES:
        sig = sig_map.get(code, {})
        name = STOCK_MAP.get(code, {}).get("name", code)
        result = get_ensemble_signal(
            code, name=name,
            serenity_signal=dict(sig).get("action", "WATCH") if sig else "WATCH",
            serenity_score=float(dict(sig).get("total_score", 50) if sig else 50) if sig else 50,
        )
        results.append(result)

    results.sort(key=lambda x: -x["ensemble_confidence"])
    return results


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    results = run_ensemble_for_all()
    print(f"\n{'='*60}")
    print(f"  策略集成投票 — {date.today()}")
    print(f"{'='*60}")
    high = [r for r in results if r["high_confidence"]]
    if high:
        print(f"\n🌟 高确信信号 ({len(high)} 个):")
        for r in high:
            print(f"  {r['name']}({r['code']}): {r['ensemble_action']} 确信度{r['ensemble_confidence']}% | {r['buy_sources']}/3源同意")
    else:
        print("\n⚠️ 今日无高确信信号")

    print(f"\n📊 全部排名:")
    for r in results[:8]:
        print(f"  {r['ensemble_action']:14s} {r['name']:6s}({r['code']}): {r['ensemble_confidence']}%确信 | {r['buy_sources']}/3源 | BT:{r['backtest_votes']['consensus']} QD:{r['quantdinger_vote']} SR:{r['serenity_vote']}")
