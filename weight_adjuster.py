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

# 默认权重（与 scorer.py 保持一致，9维度含护城河）
DEFAULT_WEIGHTS = {
    "base": 0.14,
    "zone": 0.14,
    "momentum": 0.14,
    "volume": 0.04,
    "serenity": 0.14,
    "factor": 0.14,
    "technical": 0.09,
    "sentiment": 0.09,
    "moat": 0.10,       # v2.0 护城河因子（50 评委交叉验证支持上调）
}

# IC 维度 → score_weight 键 映射
IC_TO_WEIGHT = {
    "base_score": "base",
    "zone_score": "zone",
    "momentum_score": "momentum",
    "volume_score": "volume",
    "serenity_score": "serenity",
    "factor_score": "factor",
    "technical_score": "technical",
    "sentiment_score": "sentiment",
    "moat_score": "moat",       # v2.0 护城河因子
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
    else:
        adjust_weights()


if __name__ == "__main__":
    main()
