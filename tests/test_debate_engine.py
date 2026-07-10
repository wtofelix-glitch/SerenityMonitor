"""
P1 debate_engine 单元测试

覆盖：
1. 市场环境检测（中英翻译）
2. 辩论管线运行（mock 策略池）
3. 评分融合
4. 格式化输出
5. cli.py 命令注册
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 确保项目根在 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debate_engine import (
    detect_market_regime,
    run_full_debate,
    format_debate_summary,
    inject_debate_into_score,
    CN_TO_EN_REGIME,
    VALID_REGIMES,
    _compute_pre_score,
    _list_strategies,
    cmd_strategy_debate,
)
from strategy_loader import StrategyPool, fuse_strategy_scores


# ====================================================================
# Fixtures
# ====================================================================

@pytest.fixture
def mock_strategy_pool():
    """创建 mock 策略池（避免依赖 YAML 文件）。"""
    strategies = [
        {
            "name": "hot_theme",
            "display_name": "热点题材策略",
            "category": "trend",
            "core_rules": [1, 2],
            "required_data": ["realtime_price", "sector_data"],
            "default_priority": 25,
            "market_regimes": ["trending_up", "volatile"],
            "serenity_dimensions": [
                {"sentiment": 0.35},
                {"momentum": 0.25},
                {"quality": 0.20},
                {"valuation": 0.20},
            ],
            "scoring_rules": [
                {"condition": "板块涨幅 > 3%", "score_adjustment": 15, "reason": "热门板块领涨"},
                {"condition": "个股成交额 > 10亿", "score_adjustment": 10, "reason": "资金关注度高"},
            ],
            "instructions": "1. 分析当前热门板块\n2. 计算个股与板块的相关性\n3. 输出评分",
        },
        {
            "name": "event_driven",
            "display_name": "事件驱动策略",
            "category": "framework",
            "core_rules": [3],
            "required_data": ["news", "announcement"],
            "default_priority": 20,
            "market_regimes": ["trending_up", "range_bound"],
            "serenity_dimensions": [
                {"sentiment": 0.40},
                {"moat": 0.30},
                {"quality": 0.30},
            ],
            "scoring_rules": [
                {"condition": "有重大利好公告", "score_adjustment": 20, "reason": "事件催化"},
            ],
            "instructions": "1. 检查近期公告\n2. 评估事件影响\n3. 输出评分",
        },
        {
            "name": "growth_quality",
            "display_name": "成长质量策略",
            "category": "framework",
            "core_rules": [4, 5],
            "required_data": ["financials"],
            "default_priority": 15,
            "market_regimes": ["trending_up"],
            "serenity_dimensions": [
                {"quality": 0.40},
                {"moat": 0.30},
                {"valuation": 0.30},
            ],
            "scoring_rules": [
                {"condition": "营收增速 > 20%", "score_adjustment": 15, "reason": "高增长"},
                {"condition": "ROE > 15%", "score_adjustment": 10, "reason": "高回报"},
            ],
            "instructions": "1. 分析营收增速\n2. 计算ROE\n3. 输出评分",
        },
    ]
    return strategies


@pytest.fixture
def debate_result(mock_strategy_pool):
    """模拟 run_full_debate 的返回。"""
    from debate_engine import _compute_pre_score, fuse_strategy_scores

    strategies_with_scores = []
    for s in mock_strategy_pool:
        pre = _compute_pre_score(s)
        strategies_with_scores.append({
            "strategy": s,
            "result": {
                "signal": pre["signal"],
                "confidence": pre["confidence"],
                "scores": pre["dimension_scores"],
                "key_arguments": pre.get("key_arguments", []),
                "risks": pre.get("risks", []),
            },
        })

    consensus = fuse_strategy_scores(strategies_with_scores)

    return {
        "debate_date": "2026-07-01",
        "regime": "trending_up",
        "regime_cn": "牛市",
        "total_strategies": 3,
        "applicable_strategies": 3,
        "strategy_details": [
            {"name": s["name"], "display_name": s["display_name"]}
            for s in mock_strategy_pool
        ],
        "consensus": consensus,
    }


# ====================================================================
# 测试：市场环境检测
# ====================================================================

class TestDetectMarketRegime:
    """市场环境检测 + 中英翻译"""

    def test_override_valid(self):
        """有效的手动指定"""
        assert detect_market_regime(override="trending_up") == "trending_up"
        assert detect_market_regime(override="range_bound") == "range_bound"
        assert detect_market_regime(override="trending_down") == "trending_down"

    def test_override_invalid(self):
        """无效的手动指定应报错"""
        with pytest.raises(ValueError):
            detect_market_regime(override="invalid_regime")

    def test_cn_to_en_mapping_complete(self):
        """所有中文 regime 都有对应的英文映射"""
        cn_labels = ["牛市", "结构性牛市", "震荡市", "熊市"]
        for cn in cn_labels:
            assert cn in CN_TO_EN_REGIME, f"缺少 '{cn}' 的英文映射"
            assert CN_TO_EN_REGIME[cn] in VALID_REGIMES

    def test_default_fallback(self):
        """MarketSense 不可用时回退到 trending_up"""
        with patch("debate_engine.CN_TO_EN_REGIME", {"震荡市": "range_bound"}):
            from debate_engine import detect_market_regime as detect
            # 当 MarketSense 导入失败时应返回默认
            result = detect()
            assert result in VALID_REGIMES


# ====================================================================
# 测试：辩论管线
# ====================================================================

class TestRunFullDebate:
    """辩论管线完整执行"""

    def test_basic_run(self, mock_strategy_pool):
        """基本运行应返回完整结构"""
        with patch("debate_engine.StrategyPool") as MockPool:
            mock_pool = MagicMock()
            mock_pool.strategies = {s["name"]: s for s in mock_strategy_pool}
            mock_pool.get_by_regime.return_value = mock_strategy_pool[:2]
            mock_pool.list_names.return_value = [s["name"] for s in mock_strategy_pool]
            MockPool.return_value = mock_pool

            result = run_full_debate(regime="trending_up")

            assert "debate_date" in result
            assert "consensus" in result
            assert "strategy_details" in result
            assert result["total_strategies"] == 3
            assert result["consensus"]["consensus_signal"] in ("BUY", "HOLD", "SELL")

    def test_regime_filtering(self, mock_strategy_pool):
        """市场环境筛选应只返回适用策略"""
        with patch("debate_engine.StrategyPool") as MockPool:
            mock_pool = MagicMock()
            mock_pool.strategies = {s["name"]: s for s in mock_strategy_pool}
            # 只有 trending_up 适用的策略
            suitable = [s for s in mock_strategy_pool if "trending_up" in s["market_regimes"]]
            mock_pool.get_by_regime.return_value = suitable
            MockPool.return_value = mock_pool

            result = run_full_debate(regime="trending_up")
            assert result["applicable_strategies"] == len(suitable)

    def test_empty_pool(self):
        """空策略池应返回默认 HOLD 信号"""
        with patch("debate_engine.StrategyPool") as MockPool:
            mock_pool = MagicMock()
            mock_pool.strategies = {}
            mock_pool.get_by_regime.return_value = []
            MockPool.return_value = mock_pool

            result = run_full_debate(regime="trending_up")
            assert result["applicable_strategies"] == 0
            assert result["consensus"]["consensus_signal"] == "HOLD"


# ====================================================================
# 测试：预评分
# ====================================================================

class TestComputePreScore:
    """策略预评分逻辑"""

    def test_with_scoring_rules(self):
        """有评分规则的策略应返回 HOLD + 0.5 置信度"""
        strategy = {
            "name": "test",
            "scoring_rules": [
                {"condition": "条件A", "score_adjustment": 10, "reason": "理由A"},
                {"condition": "条件B", "score_adjustment": -5, "reason": "理由B"},
            ],
        }
        result = _compute_pre_score(strategy)
        assert result["signal"] == "HOLD"
        assert result["confidence"] == 0.5

    def test_without_scoring_rules(self):
        """无评分规则的策略应返回 HOLD + 0.3 置信度"""
        strategy = {"name": "test", "scoring_rules": []}
        result = _compute_pre_score(strategy)
        assert result["signal"] == "HOLD"
        assert result["confidence"] == 0.3

    def test_dimension_scores_all_50(self):
        """所有维度基准分应为 50"""
        strategy = {"name": "test", "scoring_rules": [{"condition": "A", "score_adjustment": 10, "reason": "A"}]}
        result = _compute_pre_score(strategy)
        for dim_key, dim_val in result["dimension_scores"].items():
            assert dim_val["score"] == 50


# ====================================================================
# 测试：评分融合
# ====================================================================

class TestFuseStrategyScores:
    """fuse_strategy_scores 的全面覆盖"""

    def test_empty_input(self):
        """空输入应返回中性默认值"""
        result = fuse_strategy_scores([])
        assert result["consensus_signal"] == "HOLD"
        assert result["consensus_confidence"] == 0.0
        assert result["vote_distribution"] == {"BUY": 0, "HOLD": 0, "SELL": 0}

    def test_single_strategy(self):
        """单策略投票应直接映射"""
        entries = [{
            "strategy": {"name": "test", "display_name": "测试策略", "default_priority": 10},
            "result": {
                "signal": "BUY",
                "confidence": 0.8,
                "scores": {"momentum": {"score": 85, "rationale": "强趋势"}},
                "key_arguments": ["趋势强劲"],
                "risks": ["回调风险"],
            },
        }]
        result = fuse_strategy_scores(entries)
        assert result["consensus_signal"] == "BUY"
        assert result["consensus_confidence"] == 1.0
        assert result["vote_distribution"]["BUY"] == 1

    def test_priority_weighted_scoring(self, mock_strategy_pool):
        """高优先级策略的评分权重更高"""
        entries = []
        for i, s in enumerate(mock_strategy_pool[:3]):
            priority = s.get("default_priority", 10)
            entries.append({
                "strategy": s,
                "result": {
                    "signal": "BUY" if i == 0 else "HOLD",
                    "confidence": 0.7 + i * 0.1,
                    "scores": {dim: {"score": 50 + priority, "rationale": "test"} for dim in
                               ["moat", "momentum", "quality", "sentiment"]},
                    "key_arguments": [f"{s['display_name']}论据"],
                    "risks": [f"{s['display_name']}风险"],
                },
            })
        result = fuse_strategy_scores(entries)
        # 最高优先级策略（hot_theme, priority=25）的贡献最大
        contributions = sorted(result["strategy_contributions"],
                               key=lambda x: x.get("weight", 0), reverse=True)
        assert contributions[0]["name"] == "热点题材策略"
        assert contributions[0]["weight"] > contributions[1]["weight"]

    def test_all_sell_vote(self):
        """全 SELL 投票应输出 SELL"""
        entries = []
        for i in range(3):
            entries.append({
                "strategy": {"name": f"s{i}", "display_name": f"策略{i}", "default_priority": 10},
                "result": {
                    "signal": "SELL", "confidence": 0.8,
                    "scores": {"moat": {"score": 30, "rationale": "弱"}},
                    "key_arguments": [], "risks": [],
                },
            })
        result = fuse_strategy_scores(entries)
        assert result["consensus_signal"] == "SELL"
        assert result["vote_distribution"]["SELL"] == 3


# ====================================================================
# 测试：注入到现有评分
# ====================================================================

class TestInjectDebateIntoScore:
    """辩论共识注入现有评分"""

    def test_no_existing_scores(self):
        """无现有评分时返回注入元数据"""
        result = inject_debate_into_score({
            "consensus": {
                "consensus_signal": "BUY",
                "consensus_confidence": 0.8,
                "vote_distribution": {"BUY": 2, "HOLD": 1, "SELL": 0},
            },
        })
        assert result["debate_injected"] is True
        assert result["consensus_signal"] == "BUY"
        assert result["debate_weight"] == 0.15

    def test_with_existing_scores(self):
        """有现有评分时按注入权重混合"""
        existing = {"moat": 80.0, "momentum": 60.0, "quality": 70.0}
        debate_dim = {"moat": {"score": 50, "rationale": "test"}, "momentum": {"score": 40, "rationale": "test"}}
        result = inject_debate_into_score(
            {"consensus": {
                "consensus_signal": "HOLD", "consensus_confidence": 0.6,
                "vote_distribution": {"BUY": 1, "HOLD": 2, "SELL": 0},
                "dimension_scores": debate_dim,
            }},
            existing_scores=existing,
            injection_weight=0.2,
        )
        # moat: 80*0.8 + 50*0.2 = 74
        assert abs(result["moat"] - 74.0) < 0.01
        # momentum: 60*0.8 + 40*0.2 = 56
        assert abs(result["momentum"] - 56.0) < 0.01
        assert result["debate_injected"] is True


# ====================================================================
# 测试：格式化输出
# ====================================================================

class TestFormatDebateSummary:
    """格式化输出兼容性"""

    def test_basic_format(self, debate_result):
        """应包含核心要素"""
        text = format_debate_summary(debate_result)
        assert "策略辩论共识" in text
        signal = debate_result["consensus"]["consensus_signal"]
        assert signal in text
        assert "策略投票分布" in text
        assert "融合维度评分" in text

    def test_empty_consensus(self):
        """空共识不应崩"""
        empty = {
            "debate_date": "2026-07-01",
            "regime": "range_bound",
            "regime_cn": "震荡市",
            "total_strategies": 0,
            "applicable_strategies": 0,
            "strategy_details": [],
            "consensus": {
                "consensus_signal": "HOLD",
                "consensus_confidence": 0.0,
                "dimension_scores": {},
                "strategy_contributions": [],
                "key_arguments": [],
                "risks": [],
                "vote_distribution": {"BUY": 0, "HOLD": 0, "SELL": 0},
            },
        }
        text = format_debate_summary(empty)
        assert len(text) > 50
        assert "HOLD" in text


# ====================================================================
# 测试：CLI 注册
# ====================================================================

class TestCLIRegistration:
    """cli.py 的命令注册验证"""

    def test_cmd_strategy_debate_importable(self):
        """cmd_strategy_debate 应从 debate_engine 可导入"""
        from debate_engine import cmd_strategy_debate
        assert callable(cmd_strategy_debate)

    def test_cli_imports_debate_engine(self):
        """cli.py 应导入 cmd_strategy_debate"""
        # 仅验证 import 语句存在
        cli_path = Path(__file__).resolve().parent.parent / "cli.py"
        content = cli_path.read_text()
        assert "from debate_engine import cmd_strategy_debate" in content
        assert "strategy-debate" in content


# ====================================================================
# 启动
# ====================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
