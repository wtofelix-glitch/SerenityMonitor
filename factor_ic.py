"""
因子有效性评估 — Rank IC 分析
衡量每个评分维度与次日收益的秩相关性

用法：
    python3 factor_ic.py              # 默认近30天，20天滚动窗口
    python3 factor_ic.py --days 60    # 近60天
    python3 factor_ic.py --window 10  # 10天滚动窗口
"""
import sys
import argparse
import numpy as np
from collections import defaultdict
from db import get_conn

# ── 维度配置 ──────────────────────────────────────────────

IC_DIMENSIONS = [
    "total_score",
    "base_score",
    "zone_score",
    "momentum_score",
    "volume_score",
    "serenity_score",
    "factor_score",
    "technical_score",
    "moat_score",       # v2.0 护城河因子
    "uzi_score",        # UZI AI卡位/证据层
]

DIMENSION_LABELS = {
    "total_score": "综合评分",
    "base_score": "基本面",
    "zone_score": "价格位置",
    "momentum_score": "动量",
    "volume_score": "成交量",
    "serenity_score": "Serenity",
    "factor_score": "因子引擎",
    "technical_score": "技术面",
    "moat_score": "护城河",        # v2.0 护城河因子
    "uzi_score": "UZI卡位",
}

# ── 相关性计算 ────────────────────────────────────────────

try:
    from scipy.stats import spearmanr as _scipy_spearmanr

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


def _spearmanr(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman 秩相关系数（scipy优先，纯numpy回退）"""
    n = len(x)
    if n < 3:
        return 0.0
    if HAS_SCIPY:
        # 处理全常数列（如 factor_score 全为零）：返回 0
        if np.std(x) < 1e-12 or np.std(y) < 1e-12:
            return 0.0
        with np.errstate(all="ignore"):
            r, _ = _scipy_spearmanr(x, y)
        return r if (not np.isnan(r) and not np.isinf(r)) else 0.0
    # 纯 numpy：对秩向量算 Pearson
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx_m, ry_m = np.mean(rx), np.mean(ry)
    num = np.sum((rx - rx_m) * (ry - ry_m))
    den = np.sqrt(np.sum((rx - rx_m) ** 2) * np.sum((ry - ry_m) ** 2))
    return float(num / den) if den != 0 else 0.0


# ── 数据库辅助 ────────────────────────────────────────────

def _get_score_columns(conn) -> list[str]:
    """检测 scoring_history 表中实际存在的评分列"""
    cur = conn.execute("PRAGMA table_info(scoring_history)")
    existing = {row[1] for row in cur.fetchall()}
    return [d for d in IC_DIMENSIONS if d in existing]


# ── 核心计算 ──────────────────────────────────────────────

def compute_rank_ic(days: int = 30, window: int = 20) -> dict:
    """
    计算最近 *days* 天各维度的 Rank IC。

    返回:
        {
            "latest":   {dim: ic_value, ...},   # 最新一天 IC
            "mean_ic":  {dim: mean_ic, ...},    # 窗口均值
            "ic_ir":    {dim: ic_ir, ...},      # IC 稳定性（均值/标准差）
            "win_rate": {dim: pct, ...},        # IC > 0 的天数占比（%）
            "n_days":   {dim: int, ...},        # 实际可用天数
            "rankings": {
                "best":  [(dim, latest_ic), ...],
                "worst": [(dim, latest_ic), ...],
            },
            "all_ics":  {dim: [ic_values], ...},   # 每日 IC 序列（调试用）
        }
    """
    conn = get_conn()
    dimensions = _get_score_columns(conn)
    if not dimensions:
        conn.close()
        return {"error": "scoring_history 表中未找到评分列"}

    # ── 1. 读取评分数据 ──────────────────────────────────
    score_cols = ", ".join(dimensions)
    rows = conn.execute(
        f"""
        SELECT code, date, {score_cols}
        FROM scoring_history
        ORDER BY date DESC
        LIMIT ?
        """,
        (days * 200,),
    ).fetchall()

    score_map: dict[tuple[str, str], dict[str, float]] = {}
    date_set: set[str] = set()
    for r in rows:
        d = dict(r)
        key = (d["code"], d["date"])
        score_map[key] = {dim: (d.get(dim) or 0.0) for dim in dimensions}
        date_set.add(d["date"])
    all_dates = sorted(date_set)

    # ── 2. 读取行情数据 → 构建次日收益率 ──────────────────
    price_rows = conn.execute(
        "SELECT code, date, close FROM price_history ORDER BY code, date"
    ).fetchall()
    conn.close()

    # prices_by_code:  {code: [(date, close), ...]}
    prices_by_code: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in price_rows:
        prices_by_code[r["code"]].append((r["date"], r["close"]))

    # return_map: {(code, date): return_t1}
    return_map: dict[tuple[str, str], float] = {}
    for code, entries in prices_by_code.items():
        for i in range(len(entries) - 1):
            dt, close_t = entries[i]
            _, close_t1 = entries[i + 1]
            if close_t > 1e-8:
                return_map[(code, dt)] = float(close_t1 / close_t - 1.0)

    # ── 3. 每日截面 IC 计算 ───────────────────────────────
    date_ics: dict[str, list[float]] = {dim: [] for dim in dimensions}

    for date in all_dates:
        valid: list[tuple[dict[str, float], float]] = []
        for code in prices_by_code:
            score_key = (code, date)
            ret_key = (code, date)
            if score_key not in score_map or ret_key not in return_map:
                continue
            ret_val = return_map[ret_key]
            if abs(ret_val) > 0.20:  # 过滤异常收益率
                continue
            valid.append((score_map[score_key], ret_val))
        if len(valid) < 5:  # 至少5只标的才有意义
            continue

        scores_by_dim: dict[str, list[float]] = {dim: [] for dim in dimensions}
        rets_list: list[float] = []
        for s_map, r_val in valid:
            rets_list.append(r_val)
            for dim in dimensions:
                scores_by_dim[dim].append(s_map[dim])

        rets_arr = np.array(rets_list)
        for dim in dimensions:
            s_arr = np.array(scores_by_dim[dim])
            ic = _spearmanr(s_arr, rets_arr)
            date_ics[dim].append(ic)

    # ── 4. 聚合指标 ───────────────────────────────────────
    result: dict = {
        "latest": {},
        "mean_ic": {},
        "ic_ir": {},
        "win_rate": {},
        "n_days": {},
        "all_ics": {},
        "rankings": {"best": [], "worst": []},
    }

    for dim in dimensions:
        ics = date_ics[dim]
        if len(ics) < 2:  # 数据不足时返回 0
            result["latest"][dim] = 0.0
            result["mean_ic"][dim] = 0.0
            result["ic_ir"][dim] = 0.0
            result["win_rate"][dim] = 0.0
            result["n_days"][dim] = len(ics)
            result["all_ics"][dim] = ics
            continue

        arr = np.array(ics)
        latest = float(arr[-1])
        wdw = arr[-window:] if len(arr) > window else arr
        mean_ic = float(np.mean(wdw))
        std_ic = float(np.std(wdw, ddof=1)) if len(wdw) > 1 else 0.0
        ic_ir_val = mean_ic / std_ic if std_ic > 1e-12 else 0.0
        win = float(np.sum(wdw > 0)) / len(wdw) * 100.0

        result["latest"][dim] = round(latest, 4)
        result["mean_ic"][dim] = round(mean_ic, 4)
        result["ic_ir"][dim] = round(ic_ir_val, 4)
        result["win_rate"][dim] = round(win, 1)
        result["n_days"][dim] = len(wdw)
        result["all_ics"][dim] = [round(v, 4) for v in ics]

    # 按最新 IC 绝对值排序
    dims_with_data = [
        (dim, result["latest"][dim])
        for dim in dimensions
        if result["n_days"].get(dim, 0) > 0
    ]
    dims_sorted = sorted(dims_with_data, key=lambda x: abs(x[1]), reverse=True)
    result["rankings"]["best"] = dims_sorted[:5]
    result["rankings"]["worst"] = dims_sorted[-5:] if len(dims_sorted) > 5 else []

    return result


# ── 输出格式化 ────────────────────────────────────────────

def _fmt_ic(val: float, width: int = 8) -> str:
    """格式化 IC 值，带 +/- 前缀"""
    if abs(val) < 0.00005:
        return f"{'0.00':>{width}}"
    return f"{val:>+{width - 1}.2f} "


def print_report(result: dict):
    """打印 Rank IC 报告表格"""
    if "error" in result:
        print(f"\n❌ {result['error']}")
        return

    dimensions = [d for d in IC_DIMENSIONS if d in result.get("latest", {})]
    n_days_sample = max(result["n_days"].values()) if result["n_days"] else 0

    print()
    print(f"📊 Rank IC 报告（近{n_days_sample}天）")
    print("═" * 60)
    print(f"{'维度':<12} {'最新IC':>8} {'均值IC':>8} {'IC IR':>8} {'胜率':>7}")
    print("─" * 60)

    for dim in dimensions:
        label = DIMENSION_LABELS.get(dim, dim)
        latest = result["latest"].get(dim, 0)
        mean_ic = result["mean_ic"].get(dim, 0)
        ic_ir = result["ic_ir"].get(dim, 0)
        win_rate = result["win_rate"].get(dim, 0)

        if result["n_days"].get(dim, 0) == 0:
            print(f"{label:<12} {'N/A':>8} {'N/A':>8} {'N/A':>8} {'N/A':>7}")
            continue

        print(
            f"{label:<12} {_fmt_ic(latest, 8)} {_fmt_ic(mean_ic, 8)}"
            f"{ic_ir:>+7.2f}  {win_rate:>5.1f}%"
        )

    print("─" * 60)

    # 最佳 / 最差
    best = result["rankings"]["best"]
    worst = result["rankings"]["worst"]
    if best:
        top = best[0]
        top_label = DIMENSION_LABELS.get(top[0], top[0])
        print(f"\n🏆 最有效: {top_label} ({top[1]:+.2f})")
    if worst:
        bot = worst[-1]
        bot_label = DIMENSION_LABELS.get(bot[0], bot[0])
        print(f"⚠️  最无效: {bot_label} ({bot[1]:+.2f})")

    # 数据质量提示
    min_days = min(result["n_days"].values()) if result["n_days"] else 0
    if min_days < 5:
        print(f"\n💡 提示: 部分维度数据天数不足（最少{min_days}天），IC IR 和胜率参考价值有限。")
    if not HAS_SCIPY:
        print("💡 提示: 使用纯 numpy 回退模式（scipy 未安装），数值与 scipy 一致。")
    print()


# ── CLI ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Rank IC 因子有效性评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 factor_ic.py               # 近30天，20天窗口
  python3 factor_ic.py --days 60     # 近60天
  python3 factor_ic.py --window 10   # 10天滚动窗口
  python3 factor_ic.py --json        # JSON 输出（供程序消费）
        """,
    )
    parser.add_argument(
        "--days", type=int, default=30, help="回看天数（默认30）"
    )
    parser.add_argument(
        "--window", type=int, default=20, help="滚动窗口天数（默认20）"
    )
    parser.add_argument(
        "--json", action="store_true", help="输出 JSON 格式"
    )
    args = parser.parse_args()

    result = compute_rank_ic(days=args.days, window=args.window)

    if args.json:
        import json

        # 精简 JSON 输出，不包含原始序列
        slim = {
            "latest": result.get("latest", {}),
            "mean_ic": result.get("mean_ic", {}),
            "ic_ir": result.get("ic_ir", {}),
            "win_rate": result.get("win_rate", {}),
            "n_days": result.get("n_days", {}),
            "rankings": result.get("rankings", {}),
        }
        print(json.dumps(slim, ensure_ascii=False, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()


# ============================================================
# 🆕 v3.0 维度自动建议 — 基于 IC 趋势的淘汰/降权推荐
# ============================================================

# 维度 → 权重映射（与 scorer.py score_weight 对齐）
_WEIGHT_MAP = {
    "base_score": "base",
    "zone_score": "zone",
    "momentum_score": "momentum",
    "volume_score": "volume",
    "serenity_score": "serenity",
    "factor_score": "factor",
    "technical_score": "technical",
    "moat_score": "moat",
    "uzi_score": "uzi",
    "sentiment_score": "sentiment",
    "guru_wisdom_score": "guru_wisdom",
}


def recommend_dimension_changes(days: int = 30, window: int = 14,
                                nag_threshold: float = -0.03,
                                promote_threshold: float = 0.05,
                                nag_days: int = 10) -> dict:
    """基于 IC 数据推荐维度调整

    淘汰信号：
    - mean_IC < nag_threshold 且有效天数 >= nag_days → 建议降权或移除
    - IC 连续 N 天为负且 N >= nag_days → 警告

    提权信号：
    - mean_IC > promote_threshold 且 IC_IR > 0.5 → 建议提权

    Returns:
        {
            "warnings": [{dim, mean_ic, nag_days, action, reason}, ...],
            "promotions": [{dim, mean_ic, ic_ir, action}, ...],
            "summary": str,
        }
    """
    result = compute_rank_ic(days=days, window=window)
    mean_ic = result.get("mean_ic", {})
    n_days = result.get("n_days", {})
    ic_ir = result.get("ic_ir", {})

    warnings = []
    promotions = []

    for dim in mean_ic:
        mic = mean_ic[dim]
        nd = n_days.get(dim, 0)
        ir = ic_ir.get(dim, 0)

        weight_key = _WEIGHT_MAP.get(dim, dim)

        # 淘汰检查
        if mic < nag_threshold and nd >= nag_days:
            warnings.append({
                "dim": dim,
                "weight_key": weight_key,
                "mean_ic": round(mic, 4),
                "n_days": nd,
                "ic_ir": round(ir, 3),
                "action": "DEGRADE" if mic > -0.06 else "ELIMINATE",
                "reason": (
                    f"mean_IC={mic:.3f} < {nag_threshold:.2f} 持续{nd}天 → "
                    + ("建议降权" if mic > -0.06 else "建议淘汰")
                ),
            })
        elif mic < 0 and nd >= nag_days:
            warnings.append({
                "dim": dim,
                "weight_key": weight_key,
                "mean_ic": round(mic, 4),
                "n_days": nd,
                "ic_ir": round(ir, 3),
                "action": "MONITOR",
                "reason": f"mean_IC={mic:.3f} 持续为负{nd}天 → 关注，暂不调整",
            })

        # 提权检查
        if mic > promote_threshold and ir > 0.5:
            promotions.append({
                "dim": dim,
                "weight_key": weight_key,
                "mean_ic": round(mic, 4),
                "ic_ir": round(ir, 3),
                "action": "PROMOTE",
                "reason": f"mean_IC={mic:+.3f} IR={ir:.2f} → 建议提权",
            })

    # 按严重性排序
    warnings.sort(key=lambda w: w["mean_ic"])
    promotions.sort(key=lambda p: -p["mean_ic"])

    # 生成摘要
    summary_parts = []
    elim = [w for w in warnings if w["action"] == "ELIMINATE"]
    deg = [w for w in warnings if w["action"] == "DEGRADE"]
    mon = [w for w in warnings if w["action"] == "MONITOR"]

    if elim:
        summary_parts.append(f"❌ 建议淘汰: {', '.join(w['weight_key'] for w in elim)}")
    if deg:
        summary_parts.append(f"🔻 建议降权: {', '.join(w['weight_key'] for w in deg)}")
    if mon:
        summary_parts.append(f"👀 需关注: {', '.join(w['weight_key'] for w in mon)}")
    if promotions:
        summary_parts.append(f"🔺 建议提权: {', '.join(p['weight_key'] for p in promotions)}")
    if not summary_parts:
        summary_parts.append("✅ 所有维度 IC 正常，无需调整")

    return {
        "warnings": warnings,
        "promotions": promotions,
        "summary": " | ".join(summary_parts),
        "analyzed_dims": len(mean_ic),
        "analysis_window": f"{days}d",
    }


def format_recommendation_report(rec: dict) -> str:
    """格式化维度建议为可读报告"""
    lines = ["=" * 60]
    lines.append(f"  🧬 维度 IC 自动分析 ({rec['analysis_window']})")
    lines.append("=" * 60)
    lines.append(f"\n📊 分析维度: {rec['analyzed_dims']} 个")
    lines.append(f"\n{rec['summary']}\n")

    if rec["warnings"]:
        lines.append("─" * 60)
        lines.append("  ⚠️ 负 IC 维度详情")
        lines.append("─" * 60)
        for w in rec["warnings"]:
            icon = {"ELIMINATE": "❌", "DEGRADE": "🔻", "MONITOR": "👀"}.get(w["action"], "⚡")
            lines.append(f"  {icon} {w['weight_key']:15s} IC={w['mean_ic']:+7.4f}  "
                        f"N={w['n_days']:3d}  IR={w['ic_ir']:+6.3f}")
            lines.append(f"     {w['reason']}")

    if rec["promotions"]:
        lines.append("\n─" * 60)
        lines.append("  🔺 正 IC 维度详情")
        lines.append("─" * 60)
        for p in rec["promotions"]:
            lines.append(f"  🔺 {p['weight_key']:15s} IC={p['mean_ic']:+7.4f}  "
                        f"IR={p['ic_ir']:+.3f}")
            lines.append(f"     {p['reason']}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


# ============================================================
# 🆕 v4.0 IC 衰减曲线 — 多水平 Rank IC
# ============================================================


def compute_ic_decay(days_back: int = 60) -> dict:
    """
    计算每个评分维度在多个持有期水平上的 Rank IC。

    水平: 1, 3, 5, 10, 20 天（持有期）

    返回:
        {
            "decay_curves": {
                "total_score": {
                    "horizons": [1, 3, 5, 10, 20],
                    "mean_ic":   [0.05, 0.04, ...],
                    "std_ic":    [...],
                    "win_rate":  [...],
                    "n_samples": [...],
                },
                ...
            },
            "staying_power": [
                {"dim": "total_score", "ic_20d": 0.04, "verdict": "staying_power", ...},
                ...
            ],
            "summary": str,
        }
    """
    conn = get_conn()
    dimensions = _get_score_columns(conn)
    if not dimensions:
        conn.close()
        return {"error": "scoring_history 表中未找到评分列"}

    # ── 1. 读取评分数据 ──────────────────────────────
    score_cols = ", ".join(dimensions)
    rows = conn.execute(
        f"""
        SELECT code, date, {score_cols}
        FROM scoring_history
        ORDER BY date DESC
        LIMIT ?
        """,
        (days_back * 200,),
    ).fetchall()

    score_map: dict[tuple[str, str], dict[str, float]] = {}
    date_set: set[str] = set()
    for r in rows:
        d = dict(r)
        key = (d["code"], d["date"])
        score_map[key] = {dim: (d.get(dim) or 0.0) for dim in dimensions}
        date_set.add(d["date"])
    all_dates = sorted(date_set)

    # ── 2. 读取行情数据 → 构建多水平远期收益率 ─────────
    price_rows = conn.execute(
        "SELECT code, date, close FROM price_history ORDER BY code, date"
    ).fetchall()
    conn.close()

    # 只取有评分数据的代码，避免 sh 指数类标的稀释截面
    scored_codes = {k[0] for k in score_map}
    prices_by_code: dict[str, list[tuple[str, float]]] = {}
    for r in price_rows:
        if r["code"] in scored_codes:
            prices_by_code.setdefault(r["code"], []).append((r["date"], r["close"]))

    # return_map: {(code, date, horizon_days): forward_return}
    # horizon forward return = price_{t+horizon} / price_t - 1
    return_map: dict[tuple[str, str, int], float] = {}
    horizons = [1, 3, 5, 10, 20]
    for code, entries in prices_by_code.items():
        for i, (dt, close_t) in enumerate(entries):
            for h in horizons:
                j = min(i + h, len(entries) - 1)
                _, close_fwd = entries[j]
                if close_t > 1e-8 and j > i:
                    fwd_ret = close_fwd / close_t - 1.0
                    if abs(fwd_ret) < 0.50:  # 过滤极端值
                        return_map[(code, dt, h)] = float(fwd_ret)

    # ── 3. 每日截面 IC（每个水平）─────────────────────
    decay: dict[str, dict] = {}
    for dim in dimensions:
        decay[dim] = {h: [] for h in horizons}

    for date in all_dates:
        for h in horizons:
            valid: list[tuple[float, float]] = []
            for code in prices_by_code:
                score_key = (code, date)
                ret_key = (code, date, h)
                if score_key not in score_map or ret_key not in return_map:
                    continue
                s = score_map[score_key][dim]
                r = return_map[ret_key]
                if abs(r) > 0.50:
                    continue
                valid.append((s, r))
            if len(valid) < 5:
                continue

            s_arr = np.array([v[0] for v in valid])
            r_arr = np.array([v[1] for v in valid])
            ic = _spearmanr(s_arr, r_arr)
            decay[dim][h].append(ic)

    # ── 4. 聚合 ─────────────────────────────────────
    result: dict = {"decay_curves": {}, "staying_power": []}

    for dim in dimensions:
        curve: dict = {"horizons": horizons, "mean_ic": [], "std_ic": [],
                       "win_rate": [], "n_samples": []}
        for h in horizons:
            ics = decay[dim][h]
            curve["n_samples"].append(len(ics))
            if len(ics) < 3:
                curve["mean_ic"].append(0.0)
                curve["std_ic"].append(0.0)
                curve["win_rate"].append(0.0)
                continue
            arr = np.array(ics)
            curve["mean_ic"].append(round(float(np.mean(arr)), 4))
            curve["std_ic"].append(round(float(np.std(arr, ddof=1)), 4))
            curve["win_rate"].append(round(float(np.sum(arr > 0)) / len(arr), 4))

        result["decay_curves"][dim] = curve

        # 判断"持久力"
        ic_1d = curve["mean_ic"][0]
        ic_20d = curve["mean_ic"][-1]
        ic_3d = curve["mean_ic"][1] if len(curve["mean_ic"]) > 1 else 0.0

        if ic_20d > 0.03:
            verdict = "staying_power"
        elif ic_3d < 0.02 and abs(ic_1d - ic_20d) > 0.02:
            verdict = "fast_decay"
        else:
            verdict = "neutral"

        result["staying_power"].append({
            "dim": dim,
            "label": DIMENSION_LABELS.get(dim, dim),
            "ic_1d": round(ic_1d, 4),
            "ic_3d": round(ic_3d, 4),
            "ic_20d": round(ic_20d, 4),
            "decay_rate": round(ic_1d - ic_20d, 4),
            "verdict": verdict,
        })

    # 排序：持久力维度在前
    result["staying_power"].sort(
        key=lambda x: (x["verdict"] != "staying_power", -x["ic_20d"])
    )

    # 摘要
    sp = [s for s in result["staying_power"] if s["verdict"] == "staying_power"]
    fd = [s for s in result["staying_power"] if s["verdict"] == "fast_decay"]
    parts = []
    if sp:
        parts.append(f"持久力: {', '.join(s['label'] for s in sp)}")
    if fd:
        parts.append(f"快速衰减: {', '.join(s['label'] for s in fd)}")
    parts.append(f"分析周期: {days_back}天, 水平: {', '.join(str(h) + 'd' for h in horizons)}")
    result["summary"] = " | ".join(parts)

    return result


def format_ic_decay_report(result: dict) -> str:
    """格式化 IC 衰减报告"""
    if "error" in result:
        return f"\n{result['error']}\n"

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("  IC 衰减曲线报告（Rank IC vs 持有期）")
    lines.append("=" * 72)
    lines.append(f"\n{result['summary']}\n")

    # 表头
    lines.append(f"{'维度':<16} {'1d IC':>7} {'3d IC':>7} {'5d IC':>7} "
                 f"{'10d IC':>8} {'20d IC':>8} {'衰减':>7} {'判定':<10}")
    lines.append("-" * 72)

    for sp in result["staying_power"]:
        dim = sp["dim"]
        curve = result["decay_curves"].get(dim, {})
        mean_ic = curve.get("mean_ic", [])
        decay_rate = sp["decay_rate"]
        verdict = sp["verdict"]

        ic_strs = []
        for val in mean_ic:
            if abs(val) < 0.00005:
                ic_strs.append(f"{'0.00':>7}")
            else:
                ic_strs.append(f"{val:>+7.2f}")

        verdict_label = {"staying_power": "持久力", "fast_decay": "快速衰减",
                         "neutral": "中性"}.get(verdict, verdict)

        line = (f"{DIMENSION_LABELS.get(dim, dim):<16} "
                + " ".join(f"{s}" for s in ic_strs)
                + f" {decay_rate:>+7.2f} {verdict_label:<10}")
        lines.append(line)

    lines.append("-" * 72)

    # 持久力维度详情
    staying = [s for s in result["staying_power"] if s["verdict"] == "staying_power"]
    if staying:
        lines.append("\n  [持久力维度 — IC > 0.03 at 20d]")
        for s in staying:
            lines.append(f"    {s['label']:<12} 1d={s['ic_1d']:+.2f}  "
                         f"20d={s['ic_20d']:+.2f}  衰减={s['decay_rate']:+.2f}")

    # 快速衰减维度详情
    fast = [s for s in result["staying_power"] if s["verdict"] == "fast_decay"]
    if fast:
        lines.append("\n  [快速衰减维度 — IC < 0.02 at 3d]")
        for s in fast:
            lines.append(f"    {s['label']:<12} 1d={s['ic_1d']:+.2f}  "
                         f"3d={s['ic_3d']:+.2f}  20d={s['ic_20d']:+.2f}")

    lines.append("\n" + "=" * 72)
    return "\n".join(lines)
