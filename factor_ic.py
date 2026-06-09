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
