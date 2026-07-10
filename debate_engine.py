"""
debate_engine — P1 YAML 策略辩论注入引擎

连接 strategy_loader.py（YAML 策略池）与 Serenity 现有评分/信号管线。

核心管线：
    StrategyPool → regime 检测 → 策略筛选 → 辩论 Prompt 生成 → 评分融合 → 信号注入

用法：
    python3 cli.py strategy-debate                    # 运行完整辩论
    python3 cli.py strategy-debate --detail            # 详细输出
    python3 cli.py strategy-debate --regime trending_up  # 指定市场环境
    python3 cli.py strategy-debate --list              # 列出策略池
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 同级模块引入 ────────────────────────────────────────────
from strategy_loader import (
    StrategyPool,
    generate_debate_prompt,
    filter_by_market_regime,
    fuse_strategy_scores,
    export_for_cli,
    VALID_REGIMES,
)
from data_engine import get_all_today_snapshots, fetch_single

# ====================================================================
# 日志
# ====================================================================

logger = logging.getLogger(__name__)

# 从环境变量设置日志级别（DEBATE_LOG_LEVEL）
_log_level_str = os.environ.get("DEBATE_LOG_LEVEL", "INFO").upper()
try:
    logger.setLevel(getattr(logging, _log_level_str))
except (AttributeError, ValueError):
    logger.setLevel(logging.INFO)

# ====================================================================
# 常量
# ====================================================================

# MarketSense(中文) → StrategyPool(英文) 环境映射
CN_TO_EN_REGIME: Dict[str, str] = {
    "牛市":         "trending_up",
    "结构性牛市":   "trending_up",
    "震荡市":       "range_bound",
    "熊市":         "trending_down",
}

# 默认策略目录（相对项目根）
DEFAULT_STRATEGY_DIR = "yaml_strategies"

# 环境变量覆盖（上游 TradingAgents v0.3.0 ENV_OVERRIDES 模式）
# 所有 DEBATE_* 环境变量可在不修改代码的情况下覆盖运行时行为
ENV_OVERRIDES: Dict[str, str] = {
    "DEBATE_INJECTION_WEIGHT":    "0.15",   # 辩论共识注入权重
    "DEBATE_MAX_STRATEGIES":      "20",     # 辩论最大策略数
    "DEBATE_DEFAULT_REGIME":      "",       # 默认市场环境（空=自动检测）
    "DEBATE_LOG_LEVEL":           "INFO",   # 日志级别
}


def _get_env_override(key: str, default: str) -> str:
    """读取环境变量覆盖，不在 ENV_OVERRIDES 中时 key 会自动转为大写 + DEBATE_ 前缀。"""
    value = os.environ.get(key)
    if value is not None:
        return value
    return default


# ====================================================================
# 市场环境检测（中英桥梁）
# ====================================================================

def detect_market_regime(
    override: Optional[str] = None,
) -> str:
    """
    检测当前市场环境，返回英文 regime 标签。

    优先级：
    1. override 参数（手动指定）
    2. MarketSense 模块的中文检测 → 翻译为英文
    3. 默认 'trending_up'

    Args:
        override: 可选，手动指定市场环境（如 "trending_up"）

    Returns:
        英文 regime 标签: trending_up / trending_down / volatile / range_bound
    """
    if override is not None:
        if override not in VALID_REGIMES:
            raise ValueError(
                f"无效市场环境 '{override}'，合法值: {sorted(VALID_REGIMES)}"
            )
        return override

    # 尝试 MarketSense
    try:
        from market_sense import MarketSense
        ms = MarketSense()
        cn_label = ms.get_market_regime().get("regime_label", "震荡市")
        en_label = CN_TO_EN_REGIME.get(cn_label, "range_bound")
        return en_label
    except Exception:
        pass

    # 兜底
    return "trending_up"


# ====================================================================
# 辩论管线核心
# ====================================================================

def run_full_debate(
    strategy_dir: str = DEFAULT_STRATEGY_DIR,
    regime: Optional[str] = None,
    category: Optional[str] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    完整辩论管线：加载策略 → 市场检测 → 筛选 → 生成 → 融合。

    Args:
        strategy_dir: 策略 YAML 目录
        regime: 可选，市场环境覆盖
        category: 可选，策略类别过滤
        verbose: 是否输出详细辩论提示

    Returns:
        {
            "debate_date": "2026-07-01",
            "regime": "trending_up",
            "regime_cn": "牛市",
            "total_strategies": N,
            "applicable_strategies": N,
            "consensus": {...},        # fuse_strategy_scores 输出
            "strategy_details": [...]   # 每个策略的详情（verbose 时含完整 prompt）
        }
    """
    # 1. 获取项目根
    script_dir = Path(__file__).resolve().parent
    abs_strategy_dir = str(script_dir / strategy_dir)

    # 2. 加载策略池
    try:
        pool = StrategyPool(dir=abs_strategy_dir)
    except FileNotFoundError:
        # 回退：直接使用相对路径（cwd 可能在项目根）
        pool = StrategyPool(dir=strategy_dir)

    all_strategies = list(pool.strategies.values())

    # 3. 市场环境检测（支持 DEBATE_DEFAULT_REGIME 环境变量覆盖）
    if regime is None:
        env_regime = _get_env_override("DEBATE_DEFAULT_REGIME", "")
        regime = env_regime if env_regime else None
    actual_regime = detect_market_regime(override=regime)

    # 4. 按市场环境筛选
    filtered = pool.get_by_regime(actual_regime)
    if category:
        filtered = [s for s in filtered if s.get("category") == category]

    # 策略数量上限（支持 DEBATE_MAX_STRATEGIES 环境变量覆盖）
    max_strategies = int(_get_env_override("DEBATE_MAX_STRATEGIES", "20"))
    if len(filtered) > max_strategies:
        filtered = filtered[:max_strategies]

    # 5. 获取今日实时行情（分核心/增强两级，上游 v0.3.0 模式）
    market_context: Dict[str, Any] = {
        "date": date.today().isoformat(),
        "stock_count": 0,
    }
    try:
        today_data = get_all_today_snapshots()
        if today_data:
            market_context["stock_count"] = len(today_data)
            # 增强数据：个股详情（可选，失败不中断辩论）
            try:
                first_stock = list(today_data.values())[0] if isinstance(today_data, dict) else today_data[0]
                if isinstance(first_stock, dict):
                    market_context["sample_stock"] = {
                        "code": first_stock.get("code", ""),
                        "name": first_stock.get("name", ""),
                        "price": first_stock.get("price", 0),
                        "change_pct": first_stock.get("change_pct", 0),
                    }
            except Exception as enrich_err:
                # 增强数据降级：非核心，可跳过
                market_context["enrichment_note"] = f"DATA_UNAVAILABLE: {enrich_err}"
    except Exception as core_err:
        # 核心行情失败：中止辩论，明确告知原因
        logger.error("核心行情数据获取失败: %s", core_err)
        raise RuntimeError(
            f"核心行情数据不可用，无法运行辩论（{core_err}）。"
            f"检查数据引擎 get_all_today_snapshots() 是否正常工作。"
        ) from core_err

    # 6. 为每个适用策略生成辩论 Prompt
    strategy_details: List[Dict[str, Any]] = []
    strategies_with_scores: List[Dict[str, Any]] = []

    for s in filtered:
        # 注：完整辩论需 LLM 评分，此处输出 prompt；
        # debate-engine 第一阶段只生成 prompt + 基于 scoring_rules 的预评分
        prompt = generate_debate_prompt(s, stock_data=market_context)
        pre_score = _compute_pre_score(s)

        detail: Dict[str, Any] = {
            "name": s.get("name"),
            "display_name": s.get("display_name"),
            "category": s.get("category"),
            "priority": s.get("default_priority"),
            "signal": pre_score["signal"],
            "pre_confidence": pre_score["confidence"],
            "regime_applicable": True,
        }
        if verbose:
            detail["prompt"] = prompt
            detail["instructions"] = s.get("instructions", "")[:500]
            detail["scoring_rules"] = s.get("scoring_rules", [])

        strategy_details.append(detail)

        # 组装 fuse_strategy_scores 所需的输入格式
        strategies_with_scores.append({
            "strategy": s,
            "result": {
                "signal": pre_score["signal"],
                "confidence": pre_score["confidence"],
                "scores": pre_score["dimension_scores"],
                "key_arguments": pre_score.get("key_arguments", []),
                "risks": pre_score.get("risks", []),
            },
        })

    # 7. 融合多策略评分
    consensus = fuse_strategy_scores(strategies_with_scores)

    # 8. 计算中文 regime 显示
    rev_map = {v: k for k, v in CN_TO_EN_REGIME.items()}
    regime_cn = rev_map.get(actual_regime, "未知")

    return {
        "debate_date": date.today().isoformat(),
        "regime": actual_regime,
        "regime_cn": regime_cn,
        "total_strategies": len(all_strategies),
        "applicable_strategies": len(filtered),
        "strategy_details": strategy_details,
        "consensus": consensus,
    }


def _compute_pre_score(
    strategy: Dict[str, Any],
) -> Dict[str, Any]:
    """
    基于策略 YAML 的 scoring_rules 做预评分（无 LLM 调用）。

    仅统计规则数量，给出中性基准信号：
    - 有规则 → HOLD + 0.5 基准置信度 + 全维度 50 分
    - 无规则 → HOLD + 0.3 基准置信度
    """
    scoring_rules = strategy.get("scoring_rules", [])
    rules_count = len(scoring_rules)

    dim_scores: Dict[str, Dict[str, Any]] = {}
    for dim in [
        "cpo_alignment", "bottleneck", "AIcapex", "moat",
        "momentum", "valuation", "sentiment", "quality",
    ]:
        dim_scores[dim] = {"score": 50, "rationale": "基准中性分（待LLM辩论）"}

    if rules_count > 0:
        signal = "HOLD"
        confidence = 0.5
        key_args = [f"策略含 {rules_count} 条评分规则", "未调用 LLM 辩论（基准预评分）"]
        risks = ["预评分无实时行情上下文", "需 LLM 完整推理后覆盖"]
    else:
        signal = "HOLD"
        confidence = 0.3
        key_args = ["策略无显式评分规则"]
        risks = ["无法基于规则的预评分"]

    return {
        "signal": signal,
        "confidence": confidence,
        "dimension_scores": dim_scores,
        "key_arguments": key_args,
        "risks": risks,
    }


# ====================================================================
# 结构化输出 fallback（上游 TradingAgents v0.3.0 模式）
# ====================================================================


def invoke_structured_or_freetext(
    raw_response: str,
    fallback_label: str = "辩论输出",
) -> Dict[str, Any]:
    """
    先尝试以结构化 JSON 解析 LLM 输出，失败时降级为自由文本提取。

    上游 TradingAgents v0.3.0 模式（#1051/#1057）：
    thinking 模型可能以纯文本回答而非调用 tool，此时结构化解析返回 None。
    本函数兜底：尝试 JSON 解析 → 失败则返回自由文本内容 + 标记。

    Args:
        raw_response: LLM 原始输出字符串
        fallback_label: 降级标签（用于日志/调试）

    Returns:
        {
            "parsed": True | False,       # 是否成功解析为结构化 JSON
            "data": Optional[dict],          # 解析后的数据（成功时）
            "raw_text": str,              # 原始文本（降级时传入）
            "fallback_reason": Optional[str] # 降级原因（降级时）
        }
    """
    if not raw_response or not raw_response.strip():
        return {
            "parsed": False,
            "data": None,
            "raw_text": raw_response or "",
            "fallback_reason": f"{fallback_label}: 空响应",
        }

    # 1. 尝试直接 JSON 解析（常见输出格式）
    try:
        data = json.loads(raw_response.strip())
        if isinstance(data, dict):
            logger.info("%s: 结构化解析成功", fallback_label)
            return {
                "parsed": True,
                "data": data,
                "raw_text": raw_response,
                "fallback_reason": None,
            }
    except json.JSONDecodeError:
        pass

    # 2. 尝试从代码块提取 JSON（```）
    import re
    json_block = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        raw_response,
        re.DOTALL,
    )
    if json_block:
        try:
            data = json.loads(json_block.group(1).strip())
            if isinstance(data, dict):
                logger.info("%s: 从代码块解析成功", fallback_label)
                return {
                    "parsed": True,
                    "data": data,
                    "raw_text": raw_response,
                    "fallback_reason": None,
                }
        except json.JSONDecodeError:
            pass

    # 3. 尝试从文本中提取 JSON 对象（首个 {…}）
    brace_match = re.search(r"\{[^{}]*\}", raw_response, re.DOTALL)
    if brace_match:
        try:
            data = json.loads(brace_match.group(0))
            if isinstance(data, dict):
                logger.warning("%s: 从嵌入 JSON 对象解析（部分降级）", fallback_label)
                return {
                    "parsed": True,
                    "data": data,
                    "raw_text": raw_response,
                    "fallback_reason": "partial_embed",
                }
        except (json.JSONDecodeError, ValueError):
            pass

    # 4. 全部失败 → 降级为自由文本
    logger.warning(
        "%s: 结构化解析失败，降级为自由文本（模型可能使用 thinking 模式）",
        fallback_label,
    )
    return {
        "parsed": False,
        "data": None,
        "raw_text": raw_response.strip(),
        "fallback_reason": "structured_parse_failed_thinking_mode",
    }


# ====================================================================
# 评分注入（辩论共识 → 现有评分管线）
# ====================================================================


def inject_debate_into_score(
    debate_result: Dict[str, Any],
    existing_scores: Optional[Dict[str, Any]] = None,
    injection_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """
    将辩论共识注入到现有评分结果中。

    Args:
        debate_result: run_full_debate() 的返回
        existing_scores: scorer.py 的输出（可选）
        injection_weight: 辩论共识的注入权重（0-1），默认 0.15，
                          可通过 DEBATE_INJECTION_WEIGHT 环境变量覆盖

    Returns:
        注入后的评分字典
    """
    if injection_weight is None:
        injection_weight = float(_get_env_override("DEBATE_INJECTION_WEIGHT", "0.15"))
    consensus = debate_result.get("consensus", {})
    consensus_confidence = consensus.get("consensus_confidence", 0.0)

    if existing_scores is None:
        return {
            "debate_injected": True,
            "debate_weight": injection_weight,
            "consensus_signal": consensus.get("consensus_signal", "HOLD"),
            "consensus_confidence": consensus_confidence,
            "vote_distribution": consensus.get("vote_distribution", {}),
        }

    # 融合到现有评分
    injected = dict(existing_scores)
    debate_dim_scores = consensus.get("dimension_scores", {})

    # 按注入权重混合（兼容 dim_scores 为 dict 或 float 格式）
    for dim, debate_score in debate_dim_scores.items():
        if isinstance(debate_score, dict):
            debate_score = float(debate_score.get("score", 50))
        if dim in injected and isinstance(injected[dim], (int, float)):
            injected[dim] = round(
                injected[dim] * (1 - injection_weight)
                + debate_score * injection_weight,
                2,
            )

    injected["debate_injected"] = True
    injected["debate_signal"] = consensus.get("consensus_signal", "HOLD")
    injected["debate_confidence"] = consensus_confidence
    injected["vote_distribution"] = consensus.get("vote_distribution", {})

    return injected


# ====================================================================
# 格式化输出
# ====================================================================

def format_debate_summary(result: Dict[str, Any]) -> str:
    """
    生成可读的辩论结果摘要（适合微信/CLI 输出）。
    """
    lines: List[str] = []
    consensus = result.get("consensus", {})

    # 标题
    regime = result.get("regime_cn", "未知")
    lines.append(f"🧬 **策略辩论共识 | {result['debate_date']}**")
    lines.append(f"市场环境: {regime} ({result.get('regime', '')})")
    lines.append(f"策略池: {result['total_strategies']} 个总策略 → {result['applicable_strategies']} 个适用")
    lines.append("")

    # 共识信号
    signal = consensus.get("consensus_signal", "HOLD")
    confidence = consensus.get("consensus_confidence", 0.0)
    signal_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(signal, "⚪")
    lines.append(f"{signal_emoji} **共识信号: {signal}** (置信度 {confidence:.0%})")
    lines.append("")

    # 投票分布
    votes = consensus.get("vote_distribution", {})
    total_votes = sum(votes.values()) or 1
    lines.append("📊 **策略投票分布**")
    for s in ["BUY", "HOLD", "SELL"]:
        count = votes.get(s, 0)
        bar = "█" * count + "░" * max(0, total_votes - count)
        lines.append(f"  {s}: {count} 票 {bar}")
    lines.append("")

    # 策略贡献
    contributions = consensus.get("strategy_contributions", [])
    if contributions:
        lines.append("🧩 **各策略贡献**")
        lines.append(f"{'策略':<16} {'信号':<8} {'置信度':<8} {'权重':<8}")
        lines.append("─" * 44)
        for c in sorted(contributions, key=lambda x: x.get("weight", 0), reverse=True):
            name = c.get("name", "")[:14]
            sig = c.get("signal", "?")
            conf = f"{c.get('confidence', 0):.0%}"
            wt = f"{c.get('weight', 0):.1%}"
            emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig, "⚪")
            lines.append(f"{emoji} {name:<14} {sig:<8} {conf:<8} {wt:<8}")
        lines.append("")

    # 维度得分
    dim_scores = consensus.get("dimension_scores", {})
    if dim_scores:
        lines.append("🎯 **融合维度评分**")
        for dim, score in sorted(dim_scores.items()):
            bar_len = int(score / 10) if isinstance(score, (int, float)) else 0
            bar = "█" * bar_len + "░" * max(0, 10 - bar_len)
            lines.append(f"  {dim:<16}: {score:>5.1f} {bar}")
        lines.append("")

    # 关键论据
    args = consensus.get("key_arguments", [])
    if args:
        lines.append("💡 **关键论据（汇总）**")
        for i, a in enumerate(args[:5], 1):
            lines.append(f"  {i}. {a}")
        if len(args) > 5:
            lines.append(f"  ... 及 {len(args)-5} 条更多")
        lines.append("")

    # 风险提示
    risks = consensus.get("risks", [])
    if risks:
        lines.append("⚠️ **风险提示（汇总）**")
        for r in risks[:3]:
            lines.append(f"  • {r}")
        if len(risks) > 3:
            lines.append(f"  • ... 及 {len(risks)-3} 条更多")

    return "\n".join(lines)


# ====================================================================
# CLI 入口
# ====================================================================

def cmd_strategy_debate(argv: Optional[List[str]] = None) -> None:
    """
    CLI 入口: python3 cli.py strategy-debate [子参数]

    子参数:
        --regime <R>    指定市场环境（覆盖自动检测）
        --category <C>  按类别筛选
        --detail        输出详细（含策略 Prompt）
        --list          仅列出策略池
        --inject        注入到现有评分
    """
    if argv is None:
        argv = sys.argv[2:]  # 跳过 'cli.py' 和 'strategy-debate'

    # 快速参数解析（不依赖 argparse，兼容 cli.py 的 sys.argv 风格）
    regime: Optional[str] = None
    category: Optional[str] = None
    verbose = "--detail" in argv
    list_only = "--list" in argv
    do_inject = "--inject" in argv

    # 解析 --regime 和 --category
    for i, arg in enumerate(argv):
        if arg == "--regime" and i + 1 < len(argv):
            regime = argv[i + 1]
        elif arg == "--category" and i + 1 < len(argv):
            category = argv[i + 1]

    if list_only:
        _list_strategies()
        return

    # 运行辩论管线
    try:
        result = run_full_debate(
            regime=regime,
            category=category,
            verbose=verbose,
        )
    except FileNotFoundError as e:
        print(f"❌ 策略目录未找到: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ 辩论执行失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 输出
    print(format_debate_summary(result))

    if verbose:
        print("\n" + "=" * 64)
        print("📜 完整策略详情")
        print("=" * 64)
        print(json.dumps(result.get("strategy_details", []), ensure_ascii=False, indent=2))

    if do_inject:
        _do_inject(result)


def _list_strategies() -> None:
    """列出策略池并打印。"""
    script_dir = Path(__file__).resolve().parent
    try:
        pool = StrategyPool(dir=str(script_dir / DEFAULT_STRATEGY_DIR))
    except FileNotFoundError:
        pool = StrategyPool(dir=DEFAULT_STRATEGY_DIR)

    export = export_for_cli(list(pool.strategies.values()))
    print(f"🧬 **策略池状态**")
    print(f"已加载: {export['total_strategies']} 个策略")
    print(f"可用类别: {pool.list_categories()}")
    print("")
    for s in export["strategies"]:
        regimes = ", ".join(s.get("market_regimes", []))
        print(
            f"  • {s['display_name']} ({s['name']})\n"
            f"    类别: {s['category']} | 优先级: {s['priority']} | 适用: {regimes}"
        )


def _do_inject(result: Dict[str, Any]) -> None:
    """将辩论结果注入到 Serenity 评分。"""
    try:
        from scorer import score_all
        existing = score_all()
        injected = inject_debate_into_score(result, existing)
        actual_weight = _get_env_override("DEBATE_INJECTION_WEIGHT", "0.15")
        print(f"\n📥 辩论共识已注入评分系统 (权重 {actual_weight})")
        print(f"   注入后信号: {injected.get('debate_signal', '?')}")
        print(f"   注入后置信度: {injected.get('debate_confidence', 0):.1%}")
    except Exception as e:
        print(f"\n⚠️ 注入失败: {e}")
        print("   评分注入需要 scorer.py 正常运行且有行情数据。")


if __name__ == "__main__":
    cmd_strategy_debate(sys.argv[1:])
