"""
P1 策略加载器 — YAML 化策略池与 Serenity TradingAgents 辩论体系适配层

读取 yaml_strategies/ 目录下所有策略 YAML 文件，提供策略加载、市场环境筛选、
辩论 Prompt 生成、多策略评分融合、CLI 导出等核心功能。

用法：
    python3 strategy_loader.py --list                # 列出所有已加载策略
    python3 strategy_loader.py --regime trending_up  # 按市场环境筛选
    python3 strategy_loader.py --inspect hot_theme   # 查看单个策略详情
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ============================================================================
# 常量
# ============================================================================

# Serenity 8 维评分体系（评分值域 0-100）
SERENITY_8_DIMENSIONS = [
    "cpo_alignment",      # CPO 光模块产业链对齐度
    "bottleneck",         # 产业链瓶颈地位
    "AIcapex",            # AI 资本开支暴露度
    "moat",               # 护城河防御力
    "momentum",           # 动量/趋势强度
    "valuation",          # 估值合理性
    "sentiment",          # 市场情绪/资金面
    "quality",            # 基本面质量
]

# 有效市场环境枚举
VALID_REGIMES = frozenset([
    "trending_up",
    "trending_down",
    "volatile",
    "range_bound",
])

# 需要从 YAML 完整读取的字段列表
_REQUIRED_YAML_FIELDS = [
    "name",
    "display_name",
    "category",
    "core_rules",
    "required_data",
    "default_priority",
    "market_regimes",
    "serenity_dimensions",
    "scoring_rules",
    "instructions",
]


# ============================================================================
# 核心函数
# ============================================================================

def load_strategies(strategy_dir: str = "yaml_strategies") -> List[Dict[str, Any]]:
    """
    遍历 yaml_strategies/*.yaml 读取所有策略。

    Args:
        strategy_dir: 策略 YAML 文件目录路径

    Returns:
        解析后的策略字典列表，每个字典包含所有 YAML 字段

    Raises:
        FileNotFoundError: 目录不存在
        yaml.YAMLError: YAML 解析错误
    """
    path = Path(strategy_dir)
    if not path.is_dir():
        raise FileNotFoundError(
            f"策略目录不存在: {strategy_dir} (cwd={os.getcwd()})"
        )

    strategies: List[Dict[str, Any]] = []

    for yaml_file in sorted(path.glob("*.yaml")):
        strategy = _load_single_strategy(yaml_file)
        _validate_strategy(strategy, yaml_file)
        strategies.append(strategy)

    return strategies


def _load_single_strategy(file_path: Path) -> Dict[str, Any]:
    """加载单个 YAML 文件并返回解析后的字典。"""
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise yaml.YAMLError(f"{file_path}: 期望 YAML 顶层为字典，实际为 {type(data).__name__}")
    return data


def _validate_strategy(strategy: Dict[str, Any], source: Path) -> None:
    """校验策略字典包含所有必需字段。"""
    missing = [k for k in _REQUIRED_YAML_FIELDS if k not in strategy]
    if missing:
        raise ValueError(
            f"{source}: 缺少必需字段 {missing}"
        )
    # 额外校验 market_regimes 值合法性
    regimes = strategy.get("market_regimes", [])
    for r in regimes:
        if r not in VALID_REGIMES:
            raise ValueError(
                f"{source}: 无效 market_regime '{r}'，合法值: {sorted(VALID_REGIMES)}"
            )


# ---------------------------------------------------------------------------
# filter_by_market_regime
# ---------------------------------------------------------------------------

def filter_by_market_regime(
    strategies: List[Dict[str, Any]],
    regime: str,
) -> List[Dict[str, Any]]:
    """
    按市场环境筛选适用的策略。

    一个策略的 market_regimes 列表包含当前 regime 即通过。

    Args:
        strategies: load_strategies() 返回的策略列表
        regime: 当前市场环境（trending_up / trending_down / volatile / range_bound）

    Returns:
        适用当前市场环境的策略列表
    """
    if regime not in VALID_REGIMES:
        raise ValueError(
            f"无效市场环境 '{regime}'，合法值: {sorted(VALID_REGIMES)}"
        )
    return [s for s in strategies if regime in s.get("market_regimes", [])]


# ---------------------------------------------------------------------------
# generate_debate_prompt
# ---------------------------------------------------------------------------

def generate_debate_prompt(
    strategy: Dict[str, Any],
    stock_data: Optional[Dict[str, Any]] = None,
    trade_date: Optional[str] = None,
) -> str:
    """
    根据策略 YAML 的 instructions + scoring_rules 生成 Serenity 辩论方 Prompt。

    输出的结构化辩论提示包含：
    1. 策略身份声明 + 当前日期锚定
    2. Serenity 维度权重配置
    3. 当前市场上下文
    4. 分析步骤（来自 instructions）
    5. 评分规则（来自 scoring_rules）
    6. 预期的输出格式（JSON schema）

    Args:
        strategy: 单个策略字典
        stock_data: 可选，当前标的的股票数据上下文
        trade_date: 可选，交易日日期（默认当天），用于锚定 LLM 时间感知

    Returns:
        结构化的辩论 Prompt 字符串
    """
    name = strategy.get("display_name") or strategy.get("name", "未知策略")
    instructions = strategy.get("instructions", "")
    scoring_rules = strategy.get("scoring_rules", [])
    dimensions = strategy.get("serenity_dimensions", [])
    market_regimes = strategy.get("market_regimes", [])
    priority = strategy.get("default_priority", 0)

    # 日期锚定：默认当天
    today = trade_date or date.today().isoformat()

    # 将 serenity_dimensions 从 YAML 键值对列表转为标准字典
    dim_weights = _normalize_dimensions(dimensions)

    parts: List[str] = []

    # 1. 策略身份声明
    parts.append("=" * 64)
    parts.append(f"【辩论方身份】{name}")
    parts.append(f"策略名称: {strategy.get('name', '')}")
    parts.append(f"策略类别: {strategy.get('category', '')}")
    parts.append(f"默认优先级: {priority}")
    parts.append(f"适用市场环境: {', '.join(market_regimes) if market_regimes else '全部'}")
    parts.append("=" * 64)
    # 日期置顶：放在身份声明之后，所有分析之前，防止 LLM 锚定训练截止日期
    parts.append("")
    parts.append(f"📅 **当前日期**: {today} — 将此日期视为'现在'用于所有分析和评分。")
    parts.append("")

    # 2. Serenity 维度权重配置
    parts.append("【Serenity 8 维评分权重配置】")
    parts.append(f"  {json.dumps(dim_weights, ensure_ascii=False, indent=2)}")
    parts.append("")

    # 3. 市场环境适配说明
    if stock_data:
        parts.append("【当前市场上下文】")
        parts.append(f"  {json.dumps(stock_data, ensure_ascii=False, indent=2)}")
        parts.append("")

    # 4. 分析步骤
    parts.append("【分析步骤（来自策略 instructions）】")
    for line in instructions.strip().split("\n"):
        parts.append(f"  {line}")
    parts.append("")

    # 5. 评分规则
    parts.append("【评分规则（来自策略 scoring_rules）】")
    if scoring_rules:
        parts.append("  | # | 条件 | 评分调整 | 理由 |")
        parts.append("  |---|------|----------|------|")
        for i, rule in enumerate(scoring_rules, 1):
            cond = rule.get("condition", "")
            adj = rule.get("score_adjustment", 0)
            reason = rule.get("reason", "")
            parts.append(f"  | {i} | {cond} | {adj:+d} | {reason} |")
    else:
        parts.append("  (无显式评分规则)")
    parts.append("")

    # 6. 评分规则汇总（供下游解析）
    parts.append("【评分规则 JSON（供程序解析）】")
    parts.append(json.dumps(scoring_rules, ensure_ascii=False, indent=2))
    parts.append("")

    # 7. 预期的输出格式（JSON schema）
    parts.append("【预期输出格式 — JSON Schema】")
    parts.append(_DEBATE_OUTPUT_SCHEMA)
    parts.append("")

    # 8. 输出指令
    parts.append("【辩论输出指令】")
    parts.append(
        "请基于上述策略逻辑、评分规则和当前市场数据，输出以下 JSON 格式的辩论结果。"
    )
    parts.append("评分区间 0-100，信号: BUY / HOLD / SELL。")
    parts.append("=" * 64)

    return "\n".join(parts)


def _normalize_dimensions(
    dims: Any,
) -> Dict[str, float]:
    """
    将 serenity_dimensions 规范化为字典。

    支持两种输入格式：
    1. YAML 列表格式: [{cpo_alignment: 0.35}, {bottleneck: 0.25}, ...]
    2. 纯字典格式: {cpo_alignment: 0.35, bottleneck: 0.25, ...}
    """
    if isinstance(dims, dict):
        return dict(dims)
    if isinstance(dims, list):
        result: Dict[str, float] = {}
        for item in dims:
            if isinstance(item, dict):
                result.update(item)
        return result
    return {}


_DEBATE_OUTPUT_SCHEMA = json.dumps(
    {
        "$schema": "SerenityDebateOutput/1.0",
        "type": "object",
        "required": [
            "strategy_name",
            "signal",
            "confidence",
            "scores",
            "key_arguments",
            "risks",
        ],
        "properties": {
            "strategy_name": {
                "type": "string",
                "description": "策略显示名称",
            },
            "signal": {
                "type": "string",
                "enum": ["BUY", "HOLD", "SELL"],
                "description": "最终交易信号",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "信号置信度 0-1",
            },
            "scores": {
                "type": "object",
                "description": "Serenity 8 维评分 (0-100)，以策略权重为引导",
                "properties": {
                    dim: {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "minimum": 0, "maximum": 100},
                            "rationale": {"type": "string"},
                        },
                    }
                    for dim in SERENITY_8_DIMENSIONS
                },
            },
            "score_adjustments": {
                "type": "array",
                "description": "触发的评分规则调整列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_condition": {"type": "string"},
                        "adjustment": {"type": "number"},
                        "triggered": {"type": "boolean"},
                    },
                },
            },
            "key_arguments": {
                "type": "array",
                "description": "关键论据（3-5 条）",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
            },
            "risks": {
                "type": "array",
                "description": "风险提示",
                "items": {"type": "string"},
            },
        },
    },
    ensure_ascii=False,
    indent=2,
)


# ---------------------------------------------------------------------------
# fuse_strategy_scores
# ---------------------------------------------------------------------------

def fuse_strategy_scores(
    strategies_with_scores: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    融合多个策略的评分结果，按 default_priority 加权平均，生成多策略共识信号。

    输入格式（每个元素）:
        {
            "strategy": {...},        # 原始策略字典
            "result": {
                "signal": "BUY",
                "confidence": 0.85,
                "scores": {"cpo_alignment": {"score": 90, ...}, ...},
                "key_arguments": [...],
                "risks": [...],
            }
        }

    输出格式:
        {
            "consensus_signal": "BUY" | "HOLD" | "SELL",
            "consensus_confidence": 0.82,
            "dimension_scores": {dim: weighted_avg_score, ...},
            "strategy_contributions": [{name, signal, confidence, priority, weight}],
            "key_arguments": [...],  # 汇总所有策略的关键论据
            "risks": [...],          # 汇总所有策略的风险提示
            "vote_distribution": {"BUY": N, "HOLD": N, "SELL": N},
        }
    """
    if not strategies_with_scores:
        return {
            "consensus_signal": "HOLD",
            "consensus_confidence": 0.0,
            "dimension_scores": {},
            "strategy_contributions": [],
            "key_arguments": [],
            "risks": [],
            "vote_distribution": {"BUY": 0, "HOLD": 0, "SELL": 0},
        }

    # 收集所有有效贡献
    contributions: List[Dict[str, Any]] = []
    total_priority: float = 0.0
    # 各维度加权分数累加
    dim_accum: Dict[str, float] = {d: 0.0 for d in SERENITY_8_DIMENSIONS}
    # 信号投票
    votes: Dict[str, int] = {"BUY": 0, "HOLD": 0, "SELL": 0}
    # 汇总论据与风险
    all_arguments: List[str] = []
    all_risks: List[str] = []

    for entry in strategies_with_scores:
        strategy = entry.get("strategy", {})
        result = entry.get("result", {})
        if not result:
            continue

        priority = float(strategy.get("default_priority", 1))
        signal = result.get("signal", "HOLD")
        confidence = float(result.get("confidence", 0.0))
        scores = result.get("scores", {})

        contributions.append({
            "name": strategy.get("display_name") or strategy.get("name", "未知"),
            "signal": signal,
            "confidence": confidence,
            "priority": priority,
        })

        # 投票计数
        votes[signal] = votes.get(signal, 0) + 1

        # 累加各维度加权分数
        for dim in SERENITY_8_DIMENSIONS:
            dim_data = scores.get(dim, {})
            if isinstance(dim_data, dict):
                dim_score = float(dim_data.get("score", 0))
            elif isinstance(dim_data, (int, float)):
                dim_score = float(dim_data)
            else:
                dim_score = 0.0
            dim_accum[dim] += dim_score * priority

        total_priority += priority

        # 汇总论据和风险
        all_arguments.extend(result.get("key_arguments", []))
        all_risks.extend(result.get("risks", []))

    if total_priority == 0:
        total_priority = 1.0

    # 计算加权平均维度分数
    dim_weighted: Dict[str, float] = {}
    for dim in SERENITY_8_DIMENSIONS:
        dim_weighted[dim] = round(dim_accum[dim] / total_priority, 2)

    # 按权重归一化各策略贡献
    for c in contributions:
        c["weight"] = round(c["priority"] / total_priority, 4)

    # 推导共识信号：多数投票
    consensus_signal = max(votes, key=votes.get)  # type: ignore[arg-type]
    # 共识置信度 = (投票一致性 / 总策略数)
    winning_votes = votes.get(consensus_signal, 0)
    consensus_confidence = round(winning_votes / len(strategies_with_scores), 4)

    # 去重论据和风险（保持顺序）
    seen_args: set = set()
    unique_args: List[str] = []
    for a in all_arguments:
        if a not in seen_args:
            seen_args.add(a)
            unique_args.append(a)

    seen_risks: set = set()
    unique_risks: List[str] = []
    for r in all_risks:
        if r not in seen_risks:
            seen_risks.add(r)
            unique_risks.append(r)

    return {
        "consensus_signal": consensus_signal,
        "consensus_confidence": consensus_confidence,
        "dimension_scores": dim_weighted,
        "strategy_contributions": contributions,
        "key_arguments": unique_args,
        "risks": unique_risks,
        "vote_distribution": votes,
    }


# ---------------------------------------------------------------------------
# export_for_cli
# ---------------------------------------------------------------------------

def export_for_cli(
    strategies: List[Dict[str, Any]],
    regime: Optional[str] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    生成 CLI 可读的输出格式。

    输出包含：策略名、评分权重、信号相关元数据、关键论据（如有）。

    Args:
        strategies: load_strategies() 返回的策略列表
        regime: 可选，当前市场环境，用于标记策略适用性
        verbose: 是否输出完整 instructions

    Returns:
        CLI 友好的结构化字典
    """
    items: List[Dict[str, Any]] = []

    for s in strategies:
        dim_weights = _normalize_dimensions(s.get("serenity_dimensions", []))
        item: Dict[str, Any] = {
            "name": s.get("name", ""),
            "display_name": s.get("display_name", ""),
            "category": s.get("category", ""),
            "priority": s.get("default_priority", 0),
            "market_regimes": s.get("market_regimes", []),
            "dimension_weights": dim_weights,
            "num_scoring_rules": len(s.get("scoring_rules", [])),
            "required_data": s.get("required_data", []),
        }
        if regime is not None:
            item["regime_applicable"] = regime in s.get("market_regimes", [])
        if verbose:
            item["instructions"] = s.get("instructions", "")
            item["scoring_rules"] = s.get("scoring_rules", [])
            item["core_rules"] = s.get("core_rules", [])
        items.append(item)

    return {
        "total_strategies": len(strategies),
        "regime": regime,
        "strategies": items,
    }


# ============================================================================
# StrategyPool 类
# ============================================================================

class StrategyPool:
    """
    YAML 化策略池，管理策略生命周期。

    提供按名称、类别、市场环境的策略检索能力。

    Usage:
        pool = StrategyPool(dir="yaml_strategies")
        hot = pool.get_by_name("hot_theme")
        frameworks = pool.get_by_category("framework")
        active = pool.get_by_regime("trending_up")
    """

    def __init__(self, dir: str = "yaml_strategies"):
        """
        初始化策略池，加载所有 YAML 策略。

        Args:
            dir: 策略 YAML 文件目录路径
        """
        self._dir: str = dir
        self.strategies: Dict[str, Dict[str, Any]] = {}  # name → strategy dict
        self.load_all()

    def load_all(self) -> None:
        """（重新）加载目录下所有策略 YAML 文件。"""
        loaded = load_strategies(self._dir)
        self.strategies = {}
        for s in loaded:
            name = s.get("name", "")
            if not name:
                import warnings
                warnings.warn(f"策略缺少 name 字段，跳过: {s}")
                continue
            self.strategies[name] = s

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """根据策略名检索单个策略。"""
        return self.strategies.get(name)

    def get_by_category(self, category: str) -> List[Dict[str, Any]]:
        """根据策略类别检索策略列表。"""
        return [s for s in self.strategies.values() if s.get("category") == category]

    def get_by_regime(self, regime: str) -> List[Dict[str, Any]]:
        """根据市场环境检索适用策略列表。"""
        return filter_by_market_regime(list(self.strategies.values()), regime)

    def list_names(self) -> List[str]:
        """列出所有已加载策略的名称。"""
        return sorted(self.strategies.keys())

    def list_categories(self) -> List[str]:
        """列出所有策略类别（去重）。"""
        return sorted({s.get("category", "") for s in self.strategies.values() if s.get("category")})

    def get_by_priority_desc(self) -> List[Dict[str, Any]]:
        """按 default_priority 降序排列所有策略。"""
        return sorted(
            self.strategies.values(),
            key=lambda s: s.get("default_priority", 0),
            reverse=True,
        )

    def size(self) -> int:
        """返回已加载策略数量。"""
        return len(self.strategies)

    def __len__(self) -> int:
        return self.size()

    def __repr__(self) -> str:
        return f"<StrategyPool: {self.size()} strategies from {self._dir!r}>"

    # -------------------------------------------------------------------
    # 高级方法：加载策略并生成辩论提示 + 融合
    # -------------------------------------------------------------------

    def load_and_filter(
        self,
        regime: str,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        一站式：加载并筛选策略（按市场环境，可选按类别）。

        Args:
            regime: 市场环境
            category: 可选，策略类别过滤

        Returns:
            筛选后的策略列表
        """
        strategies = self.get_by_regime(regime)
        if category:
            strategies = [s for s in strategies if s.get("category") == category]
        return strategies

    def generate_all_debate_prompts(
        self,
        regime: str,
        stock_data: Optional[Dict[str, Any]] = None,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        为所有适用策略生成辩论 Prompt。

        Returns:
            [{strategy: ..., prompt: ...}, ...]
        """
        applicable = self.load_and_filter(regime, category)
        results: List[Dict[str, Any]] = []
        for s in applicable:
            results.append({
                "strategy": s,
                "prompt": generate_debate_prompt(s, stock_data),
            })
        return results


# ============================================================================
# CLI 入口
# ============================================================================

def _cli_print_strategies(
    strategies: List[Dict[str, Any]],
    regime: Optional[str] = None,
    verbose: bool = False,
) -> None:
    """CLI 输出策略列表。"""
    export = export_for_cli(strategies, regime=regime, verbose=verbose)
    print(json.dumps(export, ensure_ascii=False, indent=2))


def _cli_print_strategy_detail(strategy: Dict[str, Any]) -> None:
    """CLI 输出单个策略详情。"""
    detail = {
        "name": strategy.get("name"),
        "display_name": strategy.get("display_name"),
        "category": strategy.get("category"),
        "description": strategy.get("description", ""),
        "core_rules": strategy.get("core_rules", []),
        "required_data": strategy.get("required_data", []),
        "default_priority": strategy.get("default_priority", 0),
        "market_regimes": strategy.get("market_regimes", []),
        "serenity_dimensions": _normalize_dimensions(strategy.get("serenity_dimensions", [])),
        "scoring_rules": strategy.get("scoring_rules", []),
        "instructions": strategy.get("instructions", ""),
    }
    print(json.dumps(detail, ensure_ascii=False, indent=2))


def _cli_generate_prompt(strategy: Dict[str, Any]) -> None:
    """CLI 输出策略的辩论 Prompt。"""
    print(generate_debate_prompt(strategy))


def _cli_main() -> None:
    """CLI 入口：python3 strategy_loader.py <option>"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Serenity 策略加载器 — 管理 YAML 化策略池",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--list", action="store_true",
        help="列出所有已加载策略",
    )
    group.add_argument(
        "--regime", type=str, metavar="REGIME",
        help="按市场环境筛选并显示策略",
    )
    group.add_argument(
        "--inspect", type=str, metavar="NAME",
        help="查看单个策略详情",
    )
    group.add_argument(
        "--prompt", type=str, metavar="NAME",
        help="生成单个策略的辩论 Prompt",
    )
    parser.add_argument(
        "--dir", type=str, default="yaml_strategies",
        help="策略 YAML 目录路径 (默认: yaml_strategies)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="输出详细信息（含 instructions）",
    )

    args = parser.parse_args()

    # 切换到脚本所在目录的相对路径
    script_dir = Path(__file__).resolve().parent
    strategy_dir = str(script_dir / args.dir)

    try:
        pool = StrategyPool(dir=strategy_dir)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except (yaml.YAMLError, ValueError) as e:
        print(f"错误: 策略文件解析失败: {e}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        strategies = list(pool.strategies.values())
        _cli_print_strategies(strategies, verbose=args.verbose)

    elif args.regime is not None:
        regime = args.regime
        if regime not in VALID_REGIMES:
            print(
                f"错误: 无效市场环境 '{regime}'，合法值: {sorted(VALID_REGIMES)}",
                file=sys.stderr,
            )
            sys.exit(1)
        strategies = pool.get_by_regime(regime)
        _cli_print_strategies(strategies, regime=regime)

    elif args.inspect:
        strategy = pool.get_by_name(args.inspect)
        if strategy is None:
            print(
                f"错误: 未找到策略 '{args.inspect}'。"
                f"可用: {pool.list_names()}",
                file=sys.stderr,
            )
            sys.exit(1)
        _cli_print_strategy_detail(strategy)

    elif args.prompt:
        strategy = pool.get_by_name(args.prompt)
        if strategy is None:
            print(
                f"错误: 未找到策略 '{args.prompt}'。"
                f"可用: {pool.list_names()}",
                file=sys.stderr,
            )
            sys.exit(1)
        _cli_generate_prompt(strategy)

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli_main()
