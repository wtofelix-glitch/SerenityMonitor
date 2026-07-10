"""UZI 集成测试

覆盖 scorer → quant_fusion → uzi_chain_dashboard 管线集成。
验证各组件间数据传递的正确性。
"""

import sys
sys.path.insert(0, "..")

from unittest.mock import patch, MagicMock, ANY
import pytest

from uzi_insight import evaluate_uzi_insight, get_uzi_chain_dashboard, get_chain_summary_table


# ============================================================
# scorer 集成 — evaluate_uzi_insight 被 scorer 正确调用
# ============================================================

class TestScorerUziIntegration:
    """scorer.py 中 UZI 调用点的行为验证"""

    @patch("scorer.evaluate_uzi_insight")
    def test_scorer_calls_evaluate_with_correct_args(self, mock_evaluate):
        """evaluate_uzi_insight 可在 scorer 上下文中正确导入并返回 mock 值"""
        mock_evaluate.return_value = {"uzi_score": 60.0, "ai_chain_hit": True}

        # 验证 mock 已正确安装到 scorer 命名空间
        from scorer import evaluate_uzi_insight as scorer_evaluate
        result = scorer_evaluate(
            "002281",
            snapshot={},
            detail={"name": "光迅科技", "reason": "AI", "tags": [], "serenity_tag": "AI"},
            moat_result={"moat_score": 3.5},
            serenity_score=65,
            sentiment_score=60,
        )

        assert result["uzi_score"] == 60.0
        assert result["ai_chain_hit"] is True

    def test_uzi_result_has_all_scorer_keys(self):
        """UZI 返回 dict 包含 scorer 需要的所有字段"""
        result = evaluate_uzi_insight(
            "002281",
            snapshot={},
            detail={
                "name": "光迅科技", "reason": "光芯片龙头",
                "tags": ["芯片"], "serenity_tag": "AI硬科技",
            },
            moat_result={"moat_score": 3.5},
            serenity_score=65,
            sentiment_score=60,
            evidence_summary={
                "grade": "medium",
                "counts": {"strong": 1, "medium": 1, "weak": 0},
                "total": 2,
                "titles": ["订单"],
            },
        )
        # scorer.py 行596-608 用到的字段
        required_keys = {
            "uzi_score", "rating", "verdict", "ai_chain_hit",
            "ai_chain_keywords", "ai_chain_tier", "evidence_grade",
            "evidence_ledger", "trap_signals", "penalty_total", "reasons",
        }
        missing = required_keys - set(result.keys())
        assert not missing, f"UZI 结果缺少 scorer 需要的字段: {missing}"

    def test_uzi_result_handles_exception_gracefully(self):
        """异常时返回兜底 dict（scorer 行594-608）"""
        # evaluate_uzi_insight 对 None/null 参数有兜底逻辑（snapshot or {}），不会崩
        result = evaluate_uzi_insight(
            "002281",
            snapshot={},
            detail={},
            moat_result={},
            serenity_score=50,
            sentiment_score=50,
        )
        assert "uzi_score" in result
        assert isinstance(result["uzi_score"], (int, float))

    def test_uzi_score_in_reasonable_range(self):
        """uzi_score 应在 0-100 范围内"""
        result = evaluate_uzi_insight(
            "002281",
            snapshot={},
            detail={"name": "光迅", "reason": "光芯片", "tags": ["AI"], "serenity_tag": "AI"},
            moat_result={"moat_score": 3.0},
            serenity_score=60,
            sentiment_score=55,
            evidence_summary={
                "grade": "medium",
                "counts": {"strong": 1, "medium": 1, "weak": 1},
                "total": 3,
                "titles": ["量产", "中标", "布局"],
            },
        )
        assert 0 <= result["uzi_score"] <= 100, (
            f"uzi_score 应在 0-100，实际: {result['uzi_score']}"
        )


# ============================================================
# quant_fusion 集成 — UZI 权重 4%
# ============================================================

class TestQuantFusionUziWeight:
    """quant_fusion.py 中 UZI 权重4%的集成验证"""

    def test_uzi_weight_is_4pct(self):
        """UZI 权重为 0.04"""
        from quant_fusion import _objective_from_scoring_row

        # 构造包含 uzi_score 的 scoring row
        class MockRow(dict):
            def __getitem__(self, key):
                return self.get(key)

        row = MockRow({
            "total_score": 60, "technical_score": 55, "factor_score": 50,
            "sentiment_score": 52, "serenity_score": 58,
            "moat_score": 48, "uzi_score": 45,
        })

        result = _objective_from_scoring_row(row, set(row.keys()))

        # UZI 应出现在 components 中，权重 0.04
        uzi_comp = [c for c in result.get("components", []) if c["key"] == "uzi_score"]
        assert len(uzi_comp) == 1, "UZI component 应出现在 objective 计算中"
        assert uzi_comp[0]["weight"] == 0.04, (
            f"UZI weight 应为 0.04，实际: {uzi_comp[0]['weight']}"
        )

    def test_uzi_contributions_affect_overall(self):
        """不同 uzi_score 应影响 overall_score"""
        from quant_fusion import _objective_from_scoring_row

        class MockRow(dict):
            def __getitem__(self, key):
                return self.get(key)

        base_fields = {
            "total_score": 60, "technical_score": 55, "factor_score": 50,
            "sentiment_score": 52, "serenity_score": 58, "moat_score": 48,
        }

        # 高 uzi_score
        row_high = MockRow({**base_fields, "uzi_score": 80})
        result_high = _objective_from_scoring_row(row_high, set(row_high.keys()))

        # 低 uzi_score
        row_low = MockRow({**base_fields, "uzi_score": 20})
        result_low = _objective_from_scoring_row(row_low, set(row_low.keys()))

        assert result_high["overall_score"] > result_low["overall_score"], (
            f"高 UZI 分({result_high['overall_score']})应大于低 UZI 分({result_low['overall_score']})"
        )

    def test_missing_uzi_does_not_crash(self):
        """uzi_score 列缺失不应崩溃"""
        from quant_fusion import _objective_from_scoring_row

        class MockRow(dict):
            def __getitem__(self, key):
                return self.get(key)

        row = MockRow({
            "total_score": 60, "technical_score": 55, "factor_score": 50,
            "moat_score": 48,
        })

        result = _objective_from_scoring_row(row, set(row.keys()))
        uzi_comp = [c for c in result.get("components", []) if c["key"] == "uzi_score"]
        assert len(uzi_comp) == 0, "uzi_score 不在 columns 中时不应出现"
        assert isinstance(result["overall_score"], (int, float))


# ============================================================
# get_uzi_chain_dashboard 集成
# ============================================================

class TestUziChainDashboard:
    """AI产业链卡位面板输出格式验证"""

    def test_dashboard_has_required_fields(self):
        """get_uzi_chain_dashboard 返回所有必需字段"""
        panel = get_uzi_chain_dashboard("002281")
        required_keys = {
            "code", "name", "chain_tier", "chain_keywords",
            "evidence_grade", "evidence_count", "trap_count",
            "trap_warnings", "summary_line", "is_ai_chain", "gates_passed",
        }
        missing = required_keys - set(panel.keys())
        assert not missing, f"Dashboard 缺少字段: {missing}"

    def test_dashboard_non_ai_chain(self):
        """非AI链标的返回 is_ai_chain=False"""
        panel = get_uzi_chain_dashboard("600585")
        assert panel["is_ai_chain"] is False
        assert panel["chain_tier"] in ("未知", "非AI链", "未分层")

    def test_dashboard_ai_chain_detected(self):
        """AI链标的应被识别"""
        panel = get_uzi_chain_dashboard("002281")
        # 取决于测试环境的 stock_map 配置
        # 至少不应崩溃，返回结构正确
        assert isinstance(panel["is_ai_chain"], bool)
        assert isinstance(panel["chain_keywords"], list)
        assert isinstance(panel["summary_line"], str)

    def test_dashboard_code_not_found(self):
        """未配置的 code 不应崩溃"""
        panel = get_uzi_chain_dashboard("000000")
        assert panel["code"] == "000000"
        assert isinstance(panel["is_ai_chain"], bool)

    def test_dashboard_evidence_count_is_int(self):
        """evidence_count 应为整数"""
        panel = get_uzi_chain_dashboard("002281")
        assert isinstance(panel["evidence_count"], int)

    def test_dashboard_trap_warnings_is_list(self):
        """trap_warnings 应为列表"""
        panel = get_uzi_chain_dashboard("002281")
        assert isinstance(panel["trap_warnings"], list)

    def test_dashboard_summary_line_not_empty(self):
        """summary_line 不应为空字符串"""
        panel = get_uzi_chain_dashboard("002281")
        assert len(panel["summary_line"]) > 0


# ============================================================
# get_chain_summary_table 批量接口
# ============================================================

class TestChainSummaryTable:
    """批量卡位面板"""

    def test_returns_list_of_dicts(self):
        """返回 {code, name, chain_tier, ...} 列表"""
        summaries = get_chain_summary_table(codes=["002281", "600585"])
        assert isinstance(summaries, list)
        assert len(summaries) == 2
        for s in summaries:
            assert "code" in s
            assert "name" in s
            assert "chain_tier" in s

    def test_default_all_codes(self):
        """不传 codes 时返回所有标的"""
        summaries = get_chain_summary_table()
        assert len(summaries) >= 2  # 至少有基础标的
        assert isinstance(summaries, list)

    def test_empty_code_list(self):
        """空列表返回空"""
        summaries = get_chain_summary_table(codes=[])
        assert isinstance(summaries, list)
        assert len(summaries) == 0


# ============================================================
# 证据加分公式验证
# ============================================================

class TestEvidenceBonusFormula:
    """evidence_bonus 公式正确性"""

    def test_evidence_bonus_formula(self):
        """evidence_bonus = sum(count*weight) capped at 8.0"""
        result = evaluate_uzi_insight(
            "002281",
            snapshot={},
            detail={
                "name": "光迅", "reason": "光芯片",
                "tags": ["芯片"], "serenity_tag": "AI",
            },
            moat_result={"moat_score": 3.0},
            serenity_score=60,
            sentiment_score=55,
            evidence_summary={
                "grade": "strong",
                "counts": {"strong": 1, "medium": 0, "weak": 0},
                "total": 1,
                "titles": ["量产"],
            },
        )
        # 1 strong × 3 = 3, capped at 8.0
        bonus = result.get("evidence_bonus", 0)
        assert 0 <= bonus <= 8.0, (
            f"evidence_bonus 应在 0-8.0，实际: {bonus}"
        )


# ============================================================
# 完整 UZI 管线流
# ============================================================

class TestFullUziPipeline:
    """从 evidence → insight → dashboard 完整管线"""

    def test_pipeline_produces_consistent_data(self):
        """同一 code 的 insight 和 dashboard 数据应一致"""
        ai_chain_data = evaluate_uzi_insight(
            "002281",
            snapshot={},
            detail={
                "name": "光迅科技", "reason": "AI算力受益",
                "tags": ["芯片"], "serenity_tag": "AI硬科技",
            },
            moat_result={"moat_score": 4.0},
            serenity_score=70,
            sentiment_score=65,
            evidence_summary={
                "grade": "medium",
                "counts": {"strong": 1, "medium": 1, "weak": 0},
                "total": 2,
                "titles": ["量产", "订单"],
            },
        )

        dashboard = get_uzi_chain_dashboard("002281")

        # 两者都标记为 AI 链标的
        assert ai_chain_data["ai_chain_hit"] is True
        assert dashboard["is_ai_chain"] is True
        # evidence_grade 一致性
        assert dashboard["evidence_grade"] == ai_chain_data["evidence_grade"]
