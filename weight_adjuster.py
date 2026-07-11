"""
动态权重调整 — 基于 Rank IC 数据智能调整 score_weight。

工作原理：
1. 运行 factor_ic.py --json 获取各维度最近 Rank IC
2. 将 IC 维度映射到 score_weight 的 7 个维度键
3. IC 为正 → 权重上调（最高上浮 50%）
   IC 为负 → 权重下调（最低下调 50%）
4. 归一化使权重之和 = 1.0
5. 数据不足（<5天）→ 退回默认权重

用法：
    python3 weight_adjuster.py              # 计算并保存调整后权重
    python3 weight_adjuster.py --show       # 显示当前权重
    python3 weight_adjuster.py --reset      # 重置为默认权重
"""
import json
import sys
import os
from pathlib import Path

# 默认权重（与 scorer.py _SCORE_WEIGHT_DEFAULTS 保持一致，7维度，内核冻结用）
DEFAULT_WEIGHTS = {
    "zone": 0.20,
    "momentum": 0.18,
    "volume": 0.04,
    "serenity": 0.18,
    "factor": 0.17,
    "technical": 0.10,
    "moat": 0.13,
}

# IC 维度 → score_weight 键 映射
IC_TO_WEIGHT = {
    "zone_score": "zone",
    "momentum_score": "momentum",
    "volume_score": "volume",
    "serenity_score": "serenity",
    "factor_score": "factor",
    "technical_score": "technical",
    "moat_score": "moat",
}

# 保存路径
ADJUSTED_WEIGHTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".adjusted_weights.json"
)


def load_adjusted_weights() -> dict:
    """加载已保存的调整后权重，不存在则返回默认"""
    if os.path.exists(ADJUSTED_WEIGHTS_PATH):
        try:
            with open(ADJUSTED_WEIGHTS_PATH) as f:
                data = json.load(f)
            return data.get("weights", dict(DEFAULT_WEIGHTS))
        except (json.JSONDecodeError, KeyError):
            return dict(DEFAULT_WEIGHTS)
    return dict(DEFAULT_WEIGHTS)


def save_adjusted_weights(weights: dict, ic_report: dict = None):
    """保存调整后权重到文件"""
    data = {"weights": weights}
    if ic_report:
        # 保留 IC 元数据用于调试
        data["source_ic"] = {
            "latest": ic_report.get("latest", {}),
            "mean_ic": ic_report.get("mean_ic", {}),
            "n_days": ic_report.get("n_days", {}),
        }
    with open(ADJUSTED_WEIGHTS_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def adjust_weights(min_days: int = 5) -> dict:
    """
    计算调整后权重。

    Args:
        min_days: 最小数据天数要求，不足则退回默认

    Returns:
        调整后的权重 dict
    """
    # 0. 检查手动覆盖锁
    if os.path.exists(ADJUSTED_WEIGHTS_PATH):
        try:
            with open(ADJUSTED_WEIGHTS_PATH) as f:
                existing = json.load(f)
            if existing.get("_manual_override"):
                print("🔒 权重手动覆盖已锁定，跳过自动调整")
                return existing.get("weights", dict(DEFAULT_WEIGHTS))
        except Exception:
            pass
    
    # 1. 运行 factor_ic.py 获取最新 IC 数据
    import subprocess
    script_dir = os.path.dirname(os.path.abspath(__file__))
    result = subprocess.run(
        [sys.executable, os.path.join(script_dir, "factor_ic.py"),
         "--days", "30", "--json"],
        capture_output=True, text=True, cwd=script_dir
    )

    if result.returncode != 0:
        print(f"⚠️ factor_ic.py 执行失败: {result.stderr.strip()}")
        return dict(DEFAULT_WEIGHTS)

    try:
        ic_report = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"⚠️ 解析 IC 数据失败: {e}")
        return dict(DEFAULT_WEIGHTS)

    # 2. 检查数据质量
    n_days = ic_report.get("n_days", {})
    valid_dims = [d for d, n in n_days.items() if n >= min_days]

    if len(valid_dims) < 3:  # 至少 3 个维度有有效数据
        print(f"⚠️ 有效数据维度不足 ({len(valid_dims)} < 3)，退回默认权重")
        save_adjusted_weights(dict(DEFAULT_WEIGHTS), ic_report)
        return dict(DEFAULT_WEIGHTS)

    # 3. 使用均值 IC（mean_ic）调整权重
    mean_ic = ic_report.get("mean_ic", {})

    # 计算每个维度的调整系数
    # 系数范围: [0.5, 1.5] 对应 [-50%, +50%]
    # IC → 系数的映射：IC=0 → 1.0 (不变), IC=+0.3 → 1.5 (最多+50%), IC=-0.3 → 0.5 (最多-50%)
    adjustment = {}
    for ic_dim, weight_key in IC_TO_WEIGHT.items():
        ic_val = mean_ic.get(ic_dim, 0.0)
        # 缺乏数据的维度不加权调整
        if n_days.get(ic_dim, 0) < min_days:
            adjustment[weight_key] = DEFAULT_WEIGHTS[weight_key]
            continue
        # 线性映射 IC ∈ [-0.3, +0.3] → 系数 ∈ [0.5, 1.5]
        factor = 1.0 + ic_val * 1.667  # slope = 0.5/0.3
        factor = max(0.5, min(1.5, factor))
        adjustment[weight_key] = round(DEFAULT_WEIGHTS[weight_key] * factor, 4)

    # 4. 归一化：使权重之和 = 1.0
    total = sum(adjustment.values())
    if total > 0:
        normalized = {k: round(v / total, 4) for k, v in adjustment.items()}
        # 确保循环精度：最后一维修正
        diff = round(1.0 - sum(normalized.values()), 4)
        if abs(diff) > 0:
            # 加到最大权重的维度
            max_key = max(normalized, key=normalized.get)
            normalized[max_key] = round(normalized[max_key] + diff, 4)
    else:
        normalized = dict(DEFAULT_WEIGHTS)

    # 5. 保存
    save_adjusted_weights(normalized, ic_report)

    # 打印调整报告
    print(f"📊 动态权重调整完成")
    print(f"  {'维度':<12} {'默认':>8} {'调整':>8} {'变化':>10}")
    print(f"  {'─'*40}")
    for k in DEFAULT_WEIGHTS:
        delta = normalized[k] - DEFAULT_WEIGHTS[k]
        arrow = "🟢+" if delta > 0.005 else ("🔴" if delta < -0.005 else "⚪ ")
        print(f"  {k:<12} {DEFAULT_WEIGHTS[k]:>7.1%} {normalized[k]:>7.1%} {arrow}{delta:>+.1%}")

    return normalized


def show_weights():
    """显示当前权重"""
    weights = load_adjusted_weights()
    print(f"📊 当前动态权重（{ADJUSTED_WEIGHTS_PATH}）")
    print(f"  {'维度':<12} {'权重':>8} {'默认':>8} {'变化':>10}")
    print(f"  {'─'*40}")
    for k in DEFAULT_WEIGHTS:
        delta = weights[k] - DEFAULT_WEIGHTS[k]
        arrow = "🟢+" if delta > 0.005 else ("🔴" if delta < -0.005 else "⚪ ")
        print(f"  {k:<12} {weights[k]:>7.1%} {DEFAULT_WEIGHTS[k]:>7.1%} {arrow}{delta:>+.1%}")
    total = sum(weights.values())
    print(f"  {'─'*40}")
    print(f"  {'合计':<12} {total:>7.1%}")


def reset_weights():
    """重置为默认权重"""
    save_adjusted_weights(dict(DEFAULT_WEIGHTS))
    print("✅ 已重置为默认权重")
    show_weights()


def main():
    if "--show" in sys.argv:
        show_weights()
    elif "--reset" in sys.argv:
        reset_weights()
    elif "--evolve-weekly" in sys.argv:
        _evolve_weekly_candidate()
    else:
        adjust_weights()


def _evolve_weekly_candidate():
    """🆕 v6.0 周度进化候选生成 — 对接 serenity_evolution 内核。

    从 factor_ic.py 收集 ≥50 样本的 IC 证据，生成候选权重，
    通过 Frozen vs Adaptive 回测对比，闸门评估，写入 evolution 存储。
    """
    from evolution_bridge import (
        collect_ic_evidence,
        generate_weekly_candidate,
        backtest_strategy_series,
        backtest_frozen_baseline,
        FROZEN_DEFAULT_WEIGHTS,
    )
    from serenity_evolution.engine import EvolutionEngine
    from serenity_evolution.store import EvolutionStore
    import factor_ic
    from db import get_conn

    print("🧬 周度进化候选生成")
    print("─" * 50)

    # 1. 收集 IC 证据
    ic_result = factor_ic.compute_rank_ic(days=60, window=20)
    evidence = collect_ic_evidence(ic_result)
    if len(evidence) < 3:
        print(f"⚠️ 有效证据维度不足 ({len(evidence)} < 3)，跳过本周进化")
        return
    print(f"📊 IC 证据: {len(evidence)} 个维度")

    # 2. 获取回测数据
    conn = get_conn()
    price_rows = [
        dict(r) for r in conn.execute(
            "SELECT code, date, open, high, low, close, volume FROM price_history "
            "ORDER BY code, date"
        ).fetchall()
    ]
    score_rows = [
        dict(r) for r in conn.execute(
            "SELECT code, date, zone_score, momentum_score, volume_score, "
            "serenity_score, factor_score, technical_score, moat_score "
            "FROM scoring_history ORDER BY code, date"
        ).fetchall()
    ]
    conn.close()

    if len(price_rows) < 120:
        print(f"⚠️ 价格数据不足 ({len(price_rows)} < 120 天)，跳过")
        return

    # 3. 生成候选
    candidate = generate_weekly_candidate(
        "frozen-v1", FROZEN_DEFAULT_WEIGHTS, evidence
    )
    print(f"🎯 候选: {candidate.candidate_id}")
    for k, v in candidate.weights.items():
        delta = v - FROZEN_DEFAULT_WEIGHTS.get(k, 0)
        print(f"  {k}: {FROZEN_DEFAULT_WEIGHTS.get(k, 0):.3f} → {v:.3f} ({delta:+.3f})")

    # 4. 回测对比
    print("⏳ 回测中...")
    cand_series = backtest_strategy_series(candidate.weights, price_rows, score_rows)
    base_series = backtest_frozen_baseline(price_rows, score_rows)

    # 5. 闸门评估
    store = EvolutionStore("serenity.db")
    engine = EvolutionEngine(store)
    comparison, gate_result = engine.evaluate(
        candidate.candidate_id,
        cand_series,
        base_series,
        data_quality_ok=True,
        costs_included=True,
        market_rules_included=True,
    )

    print(f"\n📋 结果: {'✅ PASS' if gate_result.passed else '❌ FAIL'}")
    print(f"  Stage: {gate_result.stage.value}")
    print(f"  Excess Return: {comparison.excess_return:+.4f}")
    print(f"  Sharpe Delta:  {comparison.sharpe_delta:+.4f}")
    print(f"  Bootstrap P:    {comparison.bootstrap_probability:.4f}")
    if gate_result.failures:
        print(f"  Failures: {', '.join(gate_result.failures)}")


if __name__ == "__main__":
    main()
