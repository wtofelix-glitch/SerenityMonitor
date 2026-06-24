"""UZI overlay weight calibration using historical scores and price history."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Iterable

from db import get_conn


DEFAULT_WEIGHTS = (0.00, 0.03, 0.05, 0.08, 0.10, 0.15)
DEFAULT_HORIZONS = (1, 3, 5, 10)


def _rank_values(values: Iterable[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(indexed)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    if len(x) != len(y) or len(x) < 3:
        return 0.0
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    if dx < 1e-12 or dy < 1e-12:
        return 0.0
    return num / (dx * dy)


def rank_ic(scores: list[float], returns: list[float]) -> float:
    """Spearman rank correlation without scipy/numpy."""
    if len(scores) < 3 or len(scores) != len(returns):
        return 0.0
    return _pearson(_rank_values(scores), _rank_values(returns))


def _extract_uzi_score(row: dict) -> float | None:
    raw = row.get("uzi_score")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        details = json.loads(row.get("details") or "{}")
    except (TypeError, json.JSONDecodeError):
        details = {}
    uzi = details.get("uzi_insight", {}).get("uzi_score")
    try:
        return float(uzi)
    except (TypeError, ValueError):
        return None


def _load_scoring_rows(days: int) -> list[dict]:
    conn = get_conn()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(scoring_history)").fetchall()}
    uzi_expr = "uzi_score" if "uzi_score" in cols else "NULL AS uzi_score"
    rows = conn.execute(f"""
        SELECT code, date, total_score, details, {uzi_expr}
        FROM scoring_history
        ORDER BY date DESC
        LIMIT ?
    """, (days * 200,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_prices() -> dict[str, list[tuple[str, float]]]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT code, date, close
        FROM price_history
        WHERE close IS NOT NULL AND close > 0
        ORDER BY code, date
    """).fetchall()
    conn.close()
    prices: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in rows:
        prices[row["code"]].append((row["date"], float(row["close"])))
    return prices


def _forward_return(prices: list[tuple[str, float]], date: str, horizon: int) -> float | None:
    index = {dt: i for i, (dt, _) in enumerate(prices)}
    i = index.get(date)
    if i is None or i + horizon >= len(prices):
        return None
    close_now = prices[i][1]
    close_future = prices[i + horizon][1]
    if close_now <= 0:
        return None
    ret = close_future / close_now - 1.0
    if abs(ret) > 0.35:
        return None
    return ret


def compute_uzi_weight_calibration(
    *,
    days: int = 120,
    weights: tuple[float, ...] = DEFAULT_WEIGHTS,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    existing_weight: float = 0.05,
    min_cross_section: int = 5,
) -> dict:
    """Scan candidate UZI weights and estimate forward-return Rank IC."""
    rows = _load_scoring_rows(days)
    prices_by_code = _load_prices()

    samples: list[dict] = []
    for row in rows:
        uzi_score = _extract_uzi_score(row)
        if uzi_score is None:
            continue
        total_score = float(row.get("total_score") or 0)
        if existing_weight >= 1:
            base_score = total_score
        else:
            base_score = (total_score - uzi_score * existing_weight) / (1.0 - existing_weight)
        samples.append({
            "code": row["code"],
            "date": row["date"],
            "base_score": base_score,
            "uzi_score": uzi_score,
        })

    result = {
        "ok": True,
        "days": days,
        "sample_rows": len(samples),
        "weights": list(weights),
        "horizons": {},
        "best_by_horizon": {},
        "recommendation": None,
        "warning": "",
    }
    if not samples:
        result["ok"] = False
        result["warning"] = "没有找到带 uzi_score 的历史评分记录；先运行几天 rescore 后再校准。"
        return result

    recommendation_pool: dict[float, list[float]] = defaultdict(list)
    for horizon in horizons:
        rows_for_horizon = []
        for weight in weights:
            by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for sample in samples:
                prices = prices_by_code.get(sample["code"], [])
                fwd = _forward_return(prices, sample["date"], horizon)
                if fwd is None:
                    continue
                candidate_score = (
                    sample["base_score"] * (1.0 - weight) +
                    sample["uzi_score"] * weight
                )
                by_date[sample["date"]].append((candidate_score, fwd))

            daily_ics = []
            sample_count = 0
            for pairs in by_date.values():
                if len(pairs) < min_cross_section:
                    continue
                sc = [p[0] for p in pairs]
                rets = [p[1] for p in pairs]
                daily_ics.append(rank_ic(sc, rets))
                sample_count += len(pairs)

            mean_ic = sum(daily_ics) / len(daily_ics) if daily_ics else 0.0
            row = {
                "weight": weight,
                "mean_ic": round(mean_ic, 4),
                "n_days": len(daily_ics),
                "samples": sample_count,
            }
            rows_for_horizon.append(row)
            if daily_ics:
                recommendation_pool[weight].append(mean_ic)

        valid_rows = [row for row in rows_for_horizon if row["n_days"] > 0]
        best = max(valid_rows, key=lambda x: (x["mean_ic"], x["samples"])) if valid_rows else None
        result["horizons"][horizon] = rows_for_horizon
        result["best_by_horizon"][horizon] = best

    if recommendation_pool:
        best_weight, best_mean = max(
            ((w, sum(vals) / len(vals)) for w, vals in recommendation_pool.items()),
            key=lambda item: item[1],
        )
        result["recommendation"] = {
            "weight": best_weight,
            "mean_ic": round(best_mean, 4),
            "basis": f"{len(recommendation_pool[best_weight])}个周期均值",
        }
    else:
        result["warning"] = "价格历史不足，无法形成有效前瞻收益样本。"

    return result


def format_uzi_calibration(result: dict) -> str:
    lines = ["📐 UZI 权重校准", "=" * 64]
    if not result.get("ok"):
        lines.append(result.get("warning", "校准数据不足"))
        return "\n".join(lines)
    lines.append(f"样本评分行: {result.get('sample_rows', 0)} | 窗口: {result.get('days', 0)}天")
    for horizon, rows in result.get("horizons", {}).items():
        lines.append("")
        lines.append(f"{horizon}日前瞻 Rank IC")
        for row in rows:
            lines.append(
                f"  权重 {row['weight']:.0%}: IC {row['mean_ic']:+.4f} "
                f"| 截面天数 {row['n_days']} | 样本 {row['samples']}"
            )
        best = result.get("best_by_horizon", {}).get(horizon)
        if best:
            lines.append(f"  → 最优: {best['weight']:.0%}")
    rec = result.get("recommendation")
    if rec:
        lines.append("")
        lines.append(f"建议观察权重: {rec['weight']:.0%} | 平均IC {rec['mean_ic']:+.4f} | {rec['basis']}")
        lines.append("注: 这里只给校准建议，不自动修改 scorer.py 的实盘权重。")
    elif result.get("warning"):
        lines.append("")
        lines.append(result["warning"])
    return "\n".join(lines)
