"""Tests for the UZI-inspired insight overlay."""

from config import STOCK_DETAILS
from uzi_insight import evaluate_uzi_insight


EMPTY_LEDGER = {"grade": "none", "counts": {}, "total": 0, "titles": [], "records": []}


def test_uzi_insight_detects_ai_bottleneck_candidate():
    result = evaluate_uzi_insight(
        "603083",
        detail=STOCK_DETAILS["603083"],
        snapshot={"change_pct": 1.5},
        moat_result={"moat_score": 70},
        serenity_score=77,
        sentiment_score=60,
        evidence_summary=EMPTY_LEDGER,
    )

    assert result["ai_chain_hit"] is True
    assert result["uzi_score"] >= 50
    assert result["rating"] in {"medium", "strong"}
    assert "光模块" in result["ai_chain_keywords"]
    assert result["evidence_grade"] == "medium"
    assert result["gates"]["ai_chain"] is True


def test_uzi_insight_penalizes_hype_without_evidence():
    result = evaluate_uzi_insight(
        "002281",
        detail={
            "score": 50,
            "reason": "AI算力CPO概念 即将爆发 目标翻倍",
            "serenity_tag": "",
        },
        snapshot={"change_pct": 9.9},
        moat_result={"moat_score": 30},
        serenity_score=50,
        sentiment_score=90,
        evidence_summary=EMPTY_LEDGER,
    )

    assert result["ai_chain_hit"] is True
    assert result["evidence_grade"] == "weak"
    assert result["penalties"]["hype_no_orders"] == 0.30
    assert result["penalties"]["weak_moat"] == 0.15
    assert any(t["id"] == "hype_no_orders" for t in result["trap_signals"])
    assert result["score_before_penalty"] > result["uzi_score"]


def test_uzi_insight_skips_non_chain_defensive_stock():
    result = evaluate_uzi_insight(
        "600036",
        detail=STOCK_DETAILS["600036"],
        snapshot={"change_pct": 0.2},
        moat_result={"moat_score": 85},
        serenity_score=75,
        sentiment_score=50,
        evidence_summary=EMPTY_LEDGER,
    )

    assert result["ai_chain_hit"] is False
    assert result["rating"] == "none"
    assert result["verdict"] == "Skip"
    assert result["evidence_grade"] == "none"
    assert result["gates_passed"] == 0


def test_uzi_insight_uses_evidence_ledger_strength():
    result = evaluate_uzi_insight(
        "603083",
        detail=STOCK_DETAILS["603083"],
        snapshot={"change_pct": 1.0},
        moat_result={"moat_score": 70},
        serenity_score=77,
        sentiment_score=60,
        evidence_summary={
            "grade": "strong",
            "counts": {"strong": 1, "medium": 0, "weak": 0},
            "total": 1,
            "latest_date": "2026-06-24",
            "titles": ["800G光模块通过客户认证并进入小批量交付"],
            "records": [{"summary": "客户认证和小批量交付"}],
            "total_impact": 1,
        },
    )

    assert result["evidence_grade"] == "strong"
    assert result["evidence_ledger"]["total"] == 1
    assert result["evidence_bonus"] > 0
