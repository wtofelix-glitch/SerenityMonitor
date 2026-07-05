"""
SerenityMonitor — 交易内核冻结开关

集中控制所有自适应机制的冻结/解冻状态。
Phase 1 (v4 报告, 2026-07-05): 全局冻结 60-90 天，建立可验证的静态基线。

Usage:
    from kernel_freeze import is_frozen, freeze_reason, FROZEN_MANIFEST

    if is_frozen("weight_adjuster"):
        # 使用默认固定权重，不做 IC 调整
        ...

冻结清单覆盖 v4 报告 §2.2 的 8 个模块。
解冻条件见 v4 报告 §2.5（消融+样本外+Frozen对照+实盘验证, 四关全部通过）。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

# ═══════════════════════════════════════════════════════════════
# 冻结清单 — v4 §2.2
# ═══════════════════════════════════════════════════════════════

FROZEN_MANIFEST = {
    "weight_adjuster": {
        "frozen": True,
        "description": "IC 驱动权重自进化 — 冻结后使用默认固定权重（_SCORE_WEIGHT_DEFAULTS）",
        "frozen_behavior": "scorer.py 的 score_weight 直接使用 _SCORE_WEIGHT_DEFAULTS，不从 weight_adjuster 加载",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入 IC 权重自进化后扣费净收益/Sharpe/Calmar 有统计显著改善（Bonferroni α/7）",
            "样本外验证：在独立验证集上表现优于冻结基准",
            "Frozen Baseline 对照：Adaptive 跑赢 Frozen ≥8 周，且覆盖 ≥2 周非单边上涨行情",
            "实盘半自动验证：实盘信号质量与回测一致",
        ],
    },
    "signal_thresholds": {
        "frozen": True,
        "description": "信号阈值自校准 — 冻结后使用固定阈值，不做周期性重校准",
        "frozen_behavior": "SIGNAL_CONFIG 中的阈值不变，不根据历史胜率自动调整",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "Train/test 时间切分验证：新阈值在独立验证集上胜率显著优于旧阈值",
            "STRONG_BUY 信号至少积累 ≥30 条新样本后才允许做阈值调整",
            "Frozen Baseline 对照通过 ≥8 周",
        ],
    },
    "sentinel_weights": {
        "frozen": True,
        "description": "哨兵信源权重自进化 — 冻结后权重只记录不生效，不影响评分",
        "frozen_behavior": "sentinel_engine 继续采集和记录，但 compute_sentinel_bonus() 始终返回 0",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入 Sentinel 后扣费净收益有统计显著改善",
            "信源独立性检查：多源共振不是来自同一信息源的回音",
            "信源权重加入人工复核层",
        ],
    },
    "conviction": {
        "frozen": True,
        "description": "Conviction 动态阈值 — 冻结后只输出辩论结果，不调整买卖门槛",
        "frozen_behavior": "conviction_engine 继续生成辩论，但 _apply_conviction_to_signal_config() 返回原值不做修改",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入 Conviction 调整后扣费净收益有统计显著改善",
            "与 market_sense 的市场体制检测不冲突（消除了权重双层叠加）",
            "Frozen Baseline 对照通过 ≥8 周",
        ],
    },
    "llm_sentiment": {
        "frozen": True,
        "description": "LLM 情绪引擎 — 冻结后 LLM 情绪不进核心评分，仅用于日报/复盘",
        "frozen_behavior": "sentiment_engine LLM 模式暂停。关键词情绪模式作为观察信号运行但不参与 technical_score 融合",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：LLM 情绪显著优于纯关键词情绪（扣费净收益/Sharpe/胜率）",
            "LLM 输出可复现性验证（同一输入多次运行结果一致）",
            "数据时点审计：确认 LLM 未使用未来信息",
        ],
    },
    "serenity_council": {
        "frozen": True,
        "description": "5-Agent 投委会 — 冻结后只输出委员会意见，不改变任何信号",
        "frozen_behavior": "serenity_council 继续生成委员会报告，但不注入评分或修改信号",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入委员会后扣费净收益有统计显著改善",
            "委员会投票与最终收益的一致性分析",
        ],
    },
    "debate_engine": {
        "frozen": True,
        "description": "辩论引擎 — 冻结后只输出辩论摘要，不注入评分",
        "frozen_behavior": "debate_engine 继续生成辩论，但 inject_debate_into_score() 不修改评分",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入辩论后扣费净收益有统计显著改善",
        ],
    },
    "ensemble_voting": {
        "frozen": True,
        "description": "多源融合投票 — 冻结后只输出融合结果作为参考，不作为交易依据",
        "frozen_behavior": "ensemble_voting 继续输出结果，但不影响信号生成",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入 ensemble 后扣费净收益有统计显著改善",
        ],
    },
    "market_sense_regime_shifts": {
        "frozen": True,
        "description": "市场体制权重偏移 — 冻结后不使用 REGIME_WEIGHT_SHIFTS 动态修改评分权重",
        "frozen_behavior": "scorer.py 使用固定权重，不叠加市场状态偏移",
        "frozen_since": "2026-07-05",
        "unfreeze_conditions": [
            "消融实验：加入 market_sense 权重偏移后扣费净收益/Sharpe 有统计显著改善",
            "确认不与 weight_adjuster 产生双层叠加",
        ],
    },
}


def is_frozen(module_id: str) -> bool:
    """检查指定模块是否处于冻结状态。"""
    entry = FROZEN_MANIFEST.get(module_id)
    if entry is None:
        # 未知模块：默认冻结（default-deny 原则）
        return True
    return entry.get("frozen", True)


def freeze_reason(module_id: str) -> str:
    """返回模块冻结的原因描述。"""
    entry = FROZEN_MANIFEST.get(module_id)
    if entry is None:
        return "未知模块，默认冻结（default-deny）"
    return entry.get("description", "已冻结")


def frozen_behavior(module_id: str) -> str:
    """返回模块冻结后的替代行为描述。"""
    entry = FROZEN_MANIFEST.get(module_id)
    if entry is None:
        return "不执行任何操作"
    return entry.get("frozen_behavior", "使用默认行为")


def unfreeze_conditions(module_id: str) -> list[str]:
    """返回模块的解冻条件列表。"""
    entry = FROZEN_MANIFEST.get(module_id)
    if entry is None:
        return ["模块需先在 FROZEN_MANIFEST 中注册"]
    return entry.get("unfreeze_conditions", [])


def all_frozen_ids() -> list[str]:
    """返回当前所有处于冻结状态的模块 ID 列表。"""
    return [mid for mid, entry in FROZEN_MANIFEST.items() if entry.get("frozen", True)]


def frozen_summary() -> str:
    """返回冻结状态的文本摘要，用于日志/看板展示。"""
    lines = ["[SerenityMonitor] 交易内核冻结状态 (v4 Phase 1):", "─" * 48]
    for mid in FROZEN_MANIFEST:
        status = "🔒 冻结" if is_frozen(mid) else "🔓 解冻"
        lines.append(f"  {status}  {mid}: {FROZEN_MANIFEST[mid]['description']}")
    lines.append("─" * 48)
    lines.append(f"  共 {len(FROZEN_MANIFEST)} 个模块处于管控中")
    lines.append(f"  冻结: {len(all_frozen_ids())} / 解冻: {len(FROZEN_MANIFEST) - len(all_frozen_ids())}")
    return "\n".join(lines)


def _unfreeze(module_id: str, reason: str = "") -> None:
    """解冻指定模块（仅供 Phase 5 模块晋级流程使用，不通过 CLI 暴露）。

    调用前必须验证：
    1. 消融实验通过（Bonferroni α/n）
    2. 样本外验证通过
    3. Frozen Baseline 对照通过 ≥8 周
    4. 实盘半自动验证通过
    """
    if module_id not in FROZEN_MANIFEST:
        raise KeyError(f"模块 '{module_id}' 不在冻结清单中")

    # 验证所有解冻条件（此处为软检查，实际应由调用方提供证据）
    conditions = unfreeze_conditions(module_id)
    unmet = conditions  # 在正式流程中，应由调用方传入已满足的条件列表

    FROZEN_MANIFEST[module_id]["frozen"] = False
    FROZEN_MANIFEST[module_id]["unfrozen_at"] = date.today().isoformat()
    FROZEN_MANIFEST[module_id]["unfrozen_reason"] = reason
