"""测试绩效归因与信号绩效 CLI 辅助格式。"""

import performance_attribution
from signal_performance import _fmt_ratio_pct


def test_run_attribution_summary_uses_top_n_result_sharpe(monkeypatch):
    scores = {
        "2026-06-18": {
            "002281": {
                "total_score": 90,
                "base_score": 70,
                "zone_score": 70,
                "momentum_score": 70,
                "volume_score": 70,
                "serenity_score": 70,
                "factor_score": 70,
                "technical_score": 70,
            },
            "000988": {
                "total_score": 80,
                "base_score": 60,
                "zone_score": 60,
                "momentum_score": 60,
                "volume_score": 60,
                "serenity_score": 60,
                "factor_score": 60,
                "technical_score": 60,
            },
        },
        "2026-06-19": {
            "002281": {
                "total_score": 91,
                "base_score": 71,
                "zone_score": 71,
                "momentum_score": 71,
                "volume_score": 71,
                "serenity_score": 71,
                "factor_score": 71,
                "technical_score": 71,
            },
            "000988": {
                "total_score": 79,
                "base_score": 59,
                "zone_score": 59,
                "momentum_score": 59,
                "volume_score": 59,
                "serenity_score": 59,
                "factor_score": 59,
                "technical_score": 59,
            },
        },
    }
    prices = {
        "002281": {
            "2026-06-18": 100.0,
            "2026-06-19": 110.0,
            "2026-06-22": 115.5,
        },
        "000988": {
            "2026-06-18": 100.0,
            "2026-06-19": 101.0,
            "2026-06-22": 102.0,
        },
        "sh000001": {
            "2026-06-18": 100.0,
            "2026-06-19": 150.0,
            "2026-06-22": 200.0,
        },
    }
    monkeypatch.setattr(
        performance_attribution,
        "load_data",
        lambda: (scores, prices, ["2026-06-18", "2026-06-19", "2026-06-22"]),
    )

    result = performance_attribution.run_attribution(top_n=1)

    assert result["top_n"]["sharpe"] > 1
    assert result["benchmark"]["n_stocks"] == 2
    assert "夏普" in result["summary"]


def test_fmt_ratio_pct_displays_human_percentage():
    assert _fmt_ratio_pct(0.3659) == "36.6%"
    assert _fmt_ratio_pct(None) == "N/A"


def test_factor_contribution_skips_missing_dimension_values():
    records = []
    for idx in range(12):
        score = {
            "total_score": 50 + idx,
            "base_score": 50 + idx,
            "zone_score": 50 + idx,
            "momentum_score": 50 + idx,
            "volume_score": 50 + idx,
            "serenity_score": 50 + idx,
            "factor_score": 50 + idx,
            "technical_score": 50 + idx,
            "sentiment_score": 50 + idx,
            "moat_score": 50 + idx,
            "mr_score": 50 + idx,
        }
        if idx < 3:
            score["volume_score"] = None
        records.append(("2026-06-18", "002281", score, float(idx - 3)))

    result = performance_attribution.factor_contribution(records)

    assert result["volume_score"]["n_samples"] == 9
    assert result["total_score"]["n_samples"] == 12
    assert result["moat_score"]["n_samples"] == 12
    assert result["mr_score"]["label"] == "均值回归"
