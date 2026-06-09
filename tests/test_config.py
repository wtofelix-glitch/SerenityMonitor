"""测试 config 模块 — 配置完整性和一致性"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

from config import (
    STOCK_MAP, STOCK_DETAILS, SERENITY_DIMENSIONS, ALL_CODES,
    CAPITAL_CONFIG, RISK_CONFIG, SIGNAL_CONFIG, SERENITY_WEIGHTS_LEGACY,
    compute_serenity_score, TIER_1_CODES, TIER_2_CODES, TIER_3_CODES, TIER_4_CODES,
)
from collections import Counter


class TestStockMap:
    """STOCK_MAP 配置完整性"""

    def test_all_stocks_have_name_market_tier(self):
        for code, info in STOCK_MAP.items():
            assert "name" in info, f"{code} missing name"
            assert "market" in info, f"{code} missing market"
            assert "tier" in info, f"{code} missing tier"
            assert info["market"] in ("sh", "sz"), f"{code} invalid market"

    def test_no_duplicate_codes(self):
        codes = list(STOCK_MAP.keys())
        assert len(codes) == len(set(codes)), "Duplicate codes in STOCK_MAP!"

    def test_all_codes_in_tier_lists(self):
        tier_codes = set(TIER_1_CODES + TIER_2_CODES + TIER_3_CODES + TIER_4_CODES)
        for code in STOCK_MAP:
            assert code in tier_codes, f"{code} not in any tier list"

    def test_tier_lists_no_overlap(self):
        t1, t2, t3, t4 = TIER_1_CODES, TIER_2_CODES, TIER_3_CODES, TIER_4_CODES
        assert len(set(t1) & set(t2)) == 0
        assert len(set(t1) & set(t3)) == 0
        assert len(set(t1) & set(t4)) == 0
        assert len(set(t2) & set(t3)) == 0
        assert len(set(t2) & set(t4)) == 0
        assert len(set(t3) & set(t4)) == 0

    def test_all_codes_only_market_stocks(self):
        for code in STOCK_MAP:
            # 仅限主板: 600/601/603/605/000/002
            assert code.startswith(("600", "601", "603", "605", "000", "002")), \
                f"{code} is not a mainboard stock"


class TestStockDetails:
    """STOCK_DETAILS 与 STOCK_MAP 一致性"""

    def test_stock_details_covers_all(self):
        for code in STOCK_MAP:
            assert code in STOCK_DETAILS, f"{code} missing from STOCK_DETAILS"

    def test_stock_details_not_extra(self):
        for code in STOCK_DETAILS:
            assert code in STOCK_MAP, f"{code} in STOCK_DETAILS but not STOCK_MAP"

    def test_all_details_have_required_fields(self):
        for code, d in STOCK_DETAILS.items():
            assert "score" in d, f"{code} missing score"
            assert "buy_zone_low" in d, f"{code} missing buy_zone_low"
            assert "buy_zone_high" in d, f"{code} missing buy_zone_high"
            assert "target_sell" in d, f"{code} missing target_sell"
            # 基本检查：买入区合理
            assert d["buy_zone_low"] > 0, f"{code} buy_zone_low must be > 0"
            assert d["buy_zone_low"] < d["buy_zone_high"], \
                f"{code} buy_zone_low({d['buy_zone_low']}) >= buy_zone_high({d['buy_zone_high']})"
            assert d["target_sell"] > d["buy_zone_high"], \
                f"{code} target_sell({d['target_sell']}) <= buy_zone_high({d['buy_zone_high']})"

    def test_tier1_details_high_score(self):
        for code in TIER_1_CODES:
            assert STOCK_DETAILS[code]["score"] >= 85, f"{code} T1 score too low"

    def test_serenity_dimensions_consistent(self):
        for code in STOCK_MAP:
            assert code in SERENITY_DIMENSIONS, f"{code} missing from SERENITY_DIMENSIONS"


class TestSerenityScores:
    """compute_serenity_score 函数"""

    def test_serenity_score_range(self):
        for code in STOCK_MAP:
            score = compute_serenity_score(code)
            assert 0 <= score <= 100, f"{code} score {score} out of range"

    def test_tier1_serenity_higher_than_tier4(self):
        t1_scores = [compute_serenity_score(c) for c in TIER_1_CODES]
        t4_scores = [compute_serenity_score(c) for c in TIER_4_CODES]
        assert min(t1_scores) > max(t4_scores), \
            f"T1 serenity scores {t1_scores} should be > T4 {t4_scores}"

    def test_serenity_weights_sum_to_one(self):
        total = sum(SERENITY_WEIGHTS_LEGACY.values())
        assert abs(total - 1.0) < 0.001, f"SERENITY_WEIGHTS_LEGACY sum to {total}, expected 1.0"


class TestCapitalAndRiskConfig:
    """资金与风控配置合理性"""

    def test_capital_config_positive(self):
        assert CAPITAL_CONFIG["initial_capital"] > 0
        assert CAPITAL_CONFIG["target_capital"] > CAPITAL_CONFIG["initial_capital"]
        assert 0 < CAPITAL_CONFIG["max_positions"] <= 5
        assert 0 < CAPITAL_CONFIG["max_single_weight"] <= 1.0

    def test_risk_config_reasonable(self):
        assert RISK_CONFIG["stop_loss_pct"] < 0  # should be negative
        assert -0.20 < RISK_CONFIG["stop_loss_pct"] < -0.01
        assert RISK_CONFIG["max_portfolio_drawdown"] < 0
        assert 0 < RISK_CONFIG["trailing_stop_pct"] < 0.2

    def test_signal_thresholds_ordered(self):
        c = SIGNAL_CONFIG
        assert c["strong_buy_threshold"] > c["buy_threshold"], \
            f"strong_buy({c['strong_buy_threshold']}) <= buy({c['buy_threshold']})"
        assert c["buy_threshold"] > c["hold_high"], \
            f"buy({c['buy_threshold']}) <= hold_high({c['hold_high']})"
        assert c["hold_high"] > c["hold_low"], \
            f"hold_high({c['hold_high']}) <= hold_low({c['hold_low']})"
        assert c["hold_low"] > c["sell_threshold"], \
            f"hold_low({c['hold_low']}) <= sell({c['sell_threshold']})"
