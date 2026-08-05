"""UZI-Skill 修复测试 — pytest 迁移版本

from typing import Optional
对齐 v3.0 实际 API 签名：
- evaluate_uzi_insight(code, *, snapshot: Optional[dict], ...)
- _detect_traps(blob, *, chain_hit, ...)  第一参数为文本 blob
- _tier_for_blob(blob) → (name, weight)   替代旧的 _calc_elasticity(tier_str)
"""

import json
import sys
sys.path.insert(0, "..")

import pytest

from uzi_insight import (
    evaluate_uzi_insight,
    _as_float,
    _text_blob,
    _tier_for_blob,
    _elasticity_for_code,
    _detect_traps,
    AI_CHAIN_KEYWORDS,
    SUPPLY_CHAIN_TIERS,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def non_ai_code() -> str:
    """非AI链标的（海螺水泥）"""
    return "600585"


@pytest.fixture
def ai_code() -> str:
    """AI链上标的（光迅科技）"""
    return "002281"


@pytest.fixture
def empty_detail() -> dict:
    return {"name": "测试", "reason": "传统行业", "tags": [], "serenity_tag": ""}


@pytest.fixture
def ai_detail() -> dict:
    return {
        "name": "光迅科技",
        "reason": "光芯片龙头，AI算力受益",
        "tags": ["芯片", "光模块"],
        "serenity_tag": "AI硬科技",
        "industry": "通信/光器件",
    }


@pytest.fixture
def empty_snapshot() -> dict:
    return {}


@pytest.fixture
def high_moat_snapshot() -> dict:
    return {
        "change_pct": 3.5,
        "title": "护城河评分",
        "moat_score": 4.5,
        "industry": "通信",
    }


# ============================================================
# P1-1: 非AI链标的合理基线分
# ============================================================

class TestNonAiChainBaseline:
    """非AI链标的基线分应在25-60之间"""

    def test_non_ai_baseline_in_range(self, non_ai_code, empty_detail):
        result = evaluate_uzi_insight(
            non_ai_code,
            snapshot={},
            detail=empty_detail,
            moat_result={"moat_score": 1.0},
            serenity_score=40,
            sentiment_score=45,
            evidence_summary={"grade": "none", "counts": {}, "total": 0, "titles": []},
        )
        assert 25 <= result["uzi_score"] <= 60, (
            f"非AI链基线分应落在25-60之间，实际: {result['uzi_score']}"
        )
        assert result["ai_chain_hit"] is False
        assert result["rating"] in ("weak", "medium", "strong", "none")

    def test_non_ai_no_chain_keywords(self, non_ai_code, empty_detail):
        """非AI链标的不应命中AI关键词"""
        result = evaluate_uzi_insight(
            non_ai_code,
            snapshot={},
            detail=empty_detail,
            moat_result={"moat_score": 1.0},
            serenity_score=40,
            sentiment_score=45,
        )
        assert result["ai_chain_hit"] is False
        assert len(result["ai_chain_keywords"]) == 0


# ============================================================
# P1-2: 证据等级强制none
# ============================================================

class TestEvidenceGradeForcedNone:
    """空证据时 evidence_bonus 应为 0（evidence_grade 由关键词提升，非原始 grade）"""

    def test_grade_none_on_empty_evidence(self, ai_code, ai_detail):
        """AI链+serenity_tag→keyword_grade提升，evidence_bonus应为0"""
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={"change_pct": 2.0, "title": "AI芯片"},
            detail=ai_detail,
            moat_result={"moat_score": 3.0},
            serenity_score=60,
            sentiment_score=55,
            evidence_summary={"grade": "none", "counts": {}, "total": 0, "titles": []},
        )
        # AI chain hit + serenity_tag → keyword_grade 提升，非原始 "none"
        assert result["evidence_grade"] in ("none", "medium"), (
            f"AI链+空证据: evidence_grade 应为 none/medium，实际: {result['evidence_grade']}"
        )
        assert result["evidence_bonus"] == 0.0, (
            f"空证据 bonus 应为 0，实际: {result['evidence_bonus']}"
        )


# ============================================================
# P1-3: Quick pass 跳过 UZI DB 查询
# ============================================================

class TestQuickPassSkipsDb:
    """评分管线中 quick pass 应跳过不必要的 DB 查询"""

    def test_quick_pass_empty_snapshot(self, non_ai_code, empty_detail):
        result = evaluate_uzi_insight(
            non_ai_code,
            snapshot={},
            detail=empty_detail,
            moat_result={},
            serenity_score=30,
            sentiment_score=30,
        )
        assert isinstance(result["uzi_score"], (int, float))
        assert 0 <= result["uzi_score"] <= 100

    def test_quick_pass_no_lookup_error(self, ai_code, empty_detail):
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={},
            detail=empty_detail,
            moat_result={"moat_score": 2.0},
            serenity_score=50,
            sentiment_score=50,
        )
        assert "uzi_score" in result


# ============================================================
# P1-4: 双重罚分合并（过热+基本面错配）
# ============================================================

class TestDoublePenaltyMerged:
    """过热+基本面错配陷阱应合并罚分不超过0.60"""

    def test_double_penalty_capped(self, ai_code, ai_detail):
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={"change_pct": 9.5, "title": "AI芯片", "moat_score": 3.0},
            detail=ai_detail,
            moat_result={"moat_score": 3.0},
            serenity_score=50,
            sentiment_score=70,
            evidence_summary={
                "grade": "weak",
                "counts": {"strong": 0, "medium": 1, "weak": 1},
                "total": 2,
                "titles": ["研报", "布局"],
            },
        )
        penalty_total = result.get("penalty_total", 99)
        assert penalty_total <= 0.60, (
            f"双重罚分合并后应≤0.60，实际: {penalty_total}"
        )
        # change_pct>=9 + weak evidence → 应触发过热陷阱
        trap_ids = [t["id"] for t in result.get("trap_signals", [])]
        assert any("overheat" in tid or "heat" in tid for tid in trap_ids), (
            f"高涨幅+弱证据应触发过热相关陷阱，实际: {trap_ids}"
        )


# ============================================================
# P1-5: 过热陷阱边界条件
# ============================================================

class TestOverheatTrapBoundary:
    """过热陷阱在 change_pct 边界处正确触发/不触发"""

    def test_no_overheat_at_8pct_normal_scores(self):
        """change_pct=8 但 moat/serenity 正常 → 不触发或触发热度错配"""
        traps = _detect_traps(
            "光迅科技 光芯片 龙头 量产 认证",
            chain_hit=True,
            evidence_grade="strong",
            moat_score=70.0,
            serenity_score=70.0,
            sentiment_score=60,
            change_pct=8,
        )
        overheat_traps = [t for t in traps if "过热" in t.get("label", "")]
        # strong evidence 抑制过热
        assert len(overheat_traps) == 0, (
            f"strong evidence + 正常评分不应触发过热，实际: {overheat_traps}"
        )

    def test_overheat_at_9pct_with_weak_evidence(self):
        """change_pct>=9 + weak evidence + 弱评分 → 触发过热陷阱"""
        traps = _detect_traps(
            "光迅科技 光芯片 概念 关注",
            chain_hit=True,
            evidence_grade="weak",
            moat_score=40.0,
            serenity_score=50.0,
            sentiment_score=60,
            change_pct=9,
        )
        trap_ids = [t["id"] for t in traps]
        assert any("overheat" in tid for tid in trap_ids), (
            f"change_pct>=9+弱证据+弱评分应触发过热陷阱，实际: {trap_ids}"
        )

    def test_no_overheat_with_strong_evidence(self):
        """strong evidence 抑制过热触发"""
        traps = _detect_traps(
            "光迅科技 光芯片 量产 订单 认证",
            chain_hit=True,
            evidence_grade="strong",
            moat_score=40.0,
            serenity_score=50.0,
            sentiment_score=60,
            change_pct=9,
        )
        overheat_traps = [t for t in traps if "过热" in t.get("label", "")]
        assert len(overheat_traps) == 0


# ============================================================
# P1-6: 供应链层级映射测试（替代原 _calc_elasticity 测试）
# ============================================================

class TestSupplyChainTier:
    """供应链层级映射和权重"""

    def test_chip_tier_weight(self):
        """芯片/器件层级权重应为0.78"""
        name, weight = _tier_for_blob("光芯片 突破")
        assert "芯片" in name, f"应匹配芯片/器件层级，实际: {name}"
        assert weight == 0.78, f"芯片/器件权重应为0.78，实际: {weight}"

    def test_material_tier_weight(self):
        """材料耗材层级权重应为1.00"""
        name, weight = _tier_for_blob("磷化铟 衬底 外延")
        assert "材料" in name, f"应匹配材料耗材层级，实际: {name}"
        assert weight == 1.00, f"材料耗材权重应为1.00，实际: {weight}"

    def test_unknown_tier_fallback(self):
        """无匹配层级应返回未分层+0.55"""
        name, weight = _tier_for_blob("完全无关的文本内容")
        assert name == "未分层", f"无匹配应返回未分层，实际: {name}"
        assert weight == 0.55, f"无匹配权重应为0.55，实际: {weight}"

    def test_all_tiers_have_weight(self):
        """每个供应链层级都有正权重"""
        for name, weight, _ in SUPPLY_CHAIN_TIERS:
            assert weight > 0, f"层级 {name} 权重应为正数，实际: {weight}"
            assert weight <= 1.0, f"层级 {name} 权重应≤1.0，实际: {weight}"


# ============================================================
# P1-7: 空证据不崩溃
# ============================================================

class TestEmptyEvidenceNoCrash:
    """各种空证据场景不应崩溃"""

    def test_no_evidence_summary(self, ai_code, ai_detail):
        """不传evidence_summary不应崩溃（内部会查DB）"""
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={},
            detail=ai_detail,
            moat_result={"moat_score": 2.0},
            serenity_score=50,
            sentiment_score=50,
        )
        assert isinstance(result, dict)
        assert "uzi_score" in result

    def test_empty_evidence_dict(self, ai_code, ai_detail):
        """传空dict不应崩溃"""
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={},
            detail=ai_detail,
            moat_result={"moat_score": 2.0},
            serenity_score=50,
            sentiment_score=50,
            evidence_summary={},
        )
        assert isinstance(result, dict)

    def test_evidence_with_missing_keys(self, ai_code, ai_detail):
        """evidence_summary缺少关键字段不应崩溃（AI链+serenity_tag→keyword_grade提升）"""
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={},
            detail=ai_detail,
            moat_result={"moat_score": 2.0},
            serenity_score=50,
            sentiment_score=50,
            evidence_summary={"grade": "none"},
        )
        assert isinstance(result, dict)
        assert result["evidence_grade"] in ("none", "medium"), (
            f"evidence_grade 应为 none/medium，实际: {result['evidence_grade']}"
        )

    def test_evidence_compact_no_records(self, ai_code, empty_detail):
        """只有 grade 无 titles/records 的压紧版 evidence 不应崩溃"""
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={},
            detail=empty_detail,
            moat_result={"moat_score": 2.0},
            serenity_score=50,
            sentiment_score=50,
            evidence_summary={"grade": "weak"},
        )
        assert isinstance(result, dict)
        assert "uzi_score" in result


# ============================================================
# P2: AI链评分构成验证
# ============================================================

class TestAiChainScoring:
    """AI链评分各维度权重验证"""

    def test_chain_hit_produces_keywords(self, ai_code, ai_detail):
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={"change_pct": 2.0, "title": "光芯片"},
            detail=ai_detail,
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
        assert result["ai_chain_hit"] is True
        assert len(result["ai_chain_keywords"]) > 0
        assert result["evidence_grade"] in ("medium", "strong")

    def test_tier_weight_reflects_position(self, ai_code, ai_detail):
        result = evaluate_uzi_insight(
            ai_code,
            snapshot={"change_pct": 2.0, "title": "光器件"},
            detail=ai_detail,
            moat_result={"moat_score": 4.0},
            serenity_score=65,
            sentiment_score=60,
            evidence_summary={
                "grade": "medium",
                "counts": {"strong": 1, "medium": 1, "weak": 0},
                "total": 2,
                "titles": ["订单"],
            },
        )
        assert result["ai_chain_tier_weight"] > 0, (
            f"供应链层级权重应>0，实际: {result['ai_chain_tier_weight']}"
        )

    def test_evidence_bonus_scales_with_strength(self, ai_code, ai_detail):
        result_weak = evaluate_uzi_insight(
            ai_code,
            snapshot={"change_pct": 1.0, "title": "AI"},
            detail=ai_detail,
            moat_result={"moat_score": 3.0},
            serenity_score=60,
            sentiment_score=55,
            evidence_summary={
                "grade": "weak",
                "counts": {"strong": 0, "medium": 0, "weak": 2},
                "total": 2,
                "titles": ["关注", "布局"],
            },
        )
        result_strong = evaluate_uzi_insight(
            ai_code,
            snapshot={"change_pct": 2.0, "title": "量产"},
            detail=ai_detail,
            moat_result={"moat_score": 3.0},
            serenity_score=60,
            sentiment_score=55,
            evidence_summary={
                "grade": "strong",
                "counts": {"strong": 2, "medium": 1, "weak": 0},
                "total": 3,
                "titles": ["量产", "订单", "中标"],
            },
        )
        assert result_strong["evidence_bonus"] > result_weak["evidence_bonus"], (
            f"强证据加分({result_strong['evidence_bonus']})应大于弱证据加分({result_weak['evidence_bonus']})"
        )


# ============================================================
# P2: _as_float 辅助函数
# ============================================================

class TestAsFloat:
    """_as_float 类型安全转换"""

    def test_float_input(self):
        assert _as_float(3.14, 0.0) == 3.14

    def test_int_input(self):
        assert _as_float(42, 0.0) == 42.0

    def test_string_number(self):
        assert _as_float("3.14", 0.0) == 3.14

    def test_none_input(self):
        assert _as_float(None, 5.0) == 5.0

    def test_invalid_string(self):
        assert _as_float("abc", 1.0) == 1.0

    def test_empty_string(self):
        assert _as_float("", -1.0) == -1.0


# ============================================================
# P2: _text_blob 辅助函数
# ============================================================

class TestTextBlob:
    """_text_blob 文本拼合"""

    def test_basic_concat(self):
        result = _text_blob("hello", "world")
        assert "hello" in result
        assert "world" in result

    def test_list_input(self):
        result = _text_blob(["a", "b", "c"])
        assert "a" in result
        assert "c" in result

    def test_dict_values(self):
        result = _text_blob({"name": "foo", "tag": "bar"})
        assert "foo" in result
        assert "bar" in result

    def test_none_skipped(self):
        result = _text_blob("keep", None, "keep2")
        assert "keep" in result
        assert "keep2" in result
