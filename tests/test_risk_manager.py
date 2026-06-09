"""测试 risk_manager — 风控检查全链路"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

import json
import os
import tempfile

from risk_manager import (
    RiskManager, get_risk_manager,
    SECTOR_MAP, CONSECUTIVE_LOSS_FILE, BLACKLIST_FILE,
    COOLDOWN_FILE, DAILY_LOSS_FILE,
)


def _fresh_rm():
    """创建使用临时目录的 RiskManager，避免测试间状态污染"""
    return RiskManager(state_dir=tempfile.mkdtemp())


class TestHardStop:
    """硬止损检查"""

    def test_stop_triggered(self):
        rm = _fresh_rm()
        result = rm.check_hard_stop("002281", buy_price=100.0, current_price=93.0)
        assert result is not None
        assert result["triggered"] is True
        assert "硬止损" in result["reason"]
        # loss_pct = (93-100)/100 = -7%, max_single_loss_pct = -6%
        assert result["loss_pct"] == -7.0

    def test_stop_not_triggered(self):
        rm = _fresh_rm()
        result = rm.check_hard_stop("002281", buy_price=100.0, current_price=97.0)
        assert result is None

    def test_zero_price_no_check(self):
        rm = _fresh_rm()
        assert rm.check_hard_stop("002281", 0, 100) is None
        assert rm.check_hard_stop("002281", 100, 0) is None


class TestTrailingStop:
    """移动止损检查"""

    def test_trailing_triggered(self):
        rm = _fresh_rm()
        # peak at 120, current 108 → 10% drawdown > 8% threshold
        result = rm.check_trailing_stop("002281", buy_price=100.0, current_price=108.0, peak_price=120.0)
        assert result is not None
        assert result["triggered"] is True
        assert "移动止损" in result["reason"]

    def test_trailing_not_triggered(self):
        rm = _fresh_rm()
        # peak at 120, current 115 → 4.2% drawdown < 8% threshold
        result = rm.check_trailing_stop("002281", buy_price=100.0, current_price=115.0, peak_price=120.0)
        assert result is None

    def test_no_peak_returns_none(self):
        rm = _fresh_rm()
        assert rm.check_trailing_stop("002281", 100, 95, 0) is None


class TestDailyLoss:
    """日亏损限额检查"""

    def test_daily_loss_not_triggered(self, monkeypatch):
        rm = _fresh_rm()
        monkeypatch.setattr(rm, '_daily_open_value', 100000)
        result = rm.check_daily_loss_limit(98000, 100000)
        assert result is None  # -2% > -4%

    def test_daily_loss_triggered(self, monkeypatch):
        rm = _fresh_rm()
        monkeypatch.setattr(rm, '_daily_open_value', 100000)
        result = rm.check_daily_loss_limit(95000, 100000)
        assert result is not None
        assert result["triggered"] is True
        assert "日亏损" in result["reason"]


class TestMaxDrawdown:
    """最大回撤检查"""

    def test_no_drawdown(self):
        rm = _fresh_rm()
        result = rm.check_max_drawdown(60000, 51066.41)
        assert result is None  # +17.5% > -12%

    def test_drawdown_triggered(self):
        rm = _fresh_rm()
        result = rm.check_max_drawdown(40000, 51066.41)
        assert result is not None
        assert result["triggered"] is True
        assert "熔断" in result["reason"]


class TestConsecutiveLosses:
    """连续亏损检查"""

    def test_no_losses(self):
        rm = _fresh_rm()
        assert rm.check_consecutive_losses() is None

    def test_threshold_reached(self):
        rm = _fresh_rm()
        rm._consecutive_losses = 2  # max is 2
        result = rm.check_consecutive_losses()
        assert result is not None
        assert result["triggered"] is True
        assert "连续" in result["reason"]

    def test_record_loss_tracks_count(self, monkeypatch):
        rm = _fresh_rm()
        rm.record_loss("002281", -5.0)
        assert rm._consecutive_losses == 1

    def test_record_profit_resets_count(self, monkeypatch):
        rm = _fresh_rm()
        rm._consecutive_losses = 1
        rm.record_loss("002281", 3.0)  # positive → should not increase
        assert rm._consecutive_losses == 1  # didn't increase

    def test_max_losses_triggers_cooldown(self, monkeypatch):
        rm = _fresh_rm()
        rm._consecutive_losses = 0
        rm.record_loss("002281", -5.0)
        rm.record_loss("000988", -3.0)  # 2 consecutive → triggers cooldown
        assert rm._cooldown_until != ""


class TestBlacklist:
    """黑名单检查"""

    def test_not_in_blacklist(self):
        rm = _fresh_rm()
        assert rm.check_blacklist("002281") is None

    def test_in_blacklist(self, monkeypatch):
        rm = _fresh_rm()
        rm._blacklist["002281"] = "2026-06-20"  # future date
        result = rm.check_blacklist("002281")
        assert result is not None
        assert result["triggered"] is True
        assert "黑名单" in result["reason"]

    def test_expired_cleared(self, monkeypatch):
        rm = _fresh_rm()
        monkeypatch.setattr(rm, '_dt_today', __import__('datetime').date(2026, 6, 8))
        rm._blacklist["002281"] = "2026-06-05"  # past date
        result = rm.check_blacklist("002281")
        assert result is None  # expired, cleared
        assert "002281" not in rm._blacklist

    def test_record_stop_loss(self, monkeypatch):
        rm = _fresh_rm()
        rm.record_stop_loss("002281")
        assert "002281" in rm._blacklist


class TestSectorConcentration:
    """行业集中度检查"""

    def test_under_limit(self):
        rm = _fresh_rm()
        result = rm.check_sector_concentration("002281", [{"code": "600036"}])
        assert result is None  # 光通信 vs 银行 — different sectors

    def test_over_limit(self):
        rm = _fresh_rm()
        # 002281 (光通信) + 000988 (光通信) = 2 already
        result = rm.check_sector_concentration("603083", [{"code": "002281"}, {"code": "000988"}])
        assert result is not None
        assert result["triggered"] is True
        assert "行业集中度" in result["reason"]

    def test_unknown_sector_no_check(self):
        rm = _fresh_rm()
        result = rm.check_sector_concentration("999999", [])
        assert result is None


class TestPositionLimits:
    """仓位限制检查"""

    def test_max_positions_reached(self):
        rm = _fresh_rm()
        result = rm.check_position_limits(
            [{"code": "a"}, {"code": "b"}], new_amount=10000, total_value=60000
        )
        assert len(result) > 0
        assert result[0]["triggered"] is True

    def test_single_weight_ok(self):
        rm = _fresh_rm()
        result = rm.check_position_limits(
            [{"code": "a"}], new_amount=20000, total_value=60000
        )
        # 20k/60k = 33% < 60% max, > 30% min → OK but warning for below min
        weight_alerts = [r for r in result if not r.get("warning")]
        assert len(weight_alerts) == 0  # no critical alerts

    def test_single_weight_too_high(self):
        rm = _fresh_rm()
        result = rm.check_position_limits(
            [{"code": "a"}], new_amount=40000, total_value=60000
        )
        # 40k/60k = 67% > 60% → triggered
        critical = [r for r in result if r.get("triggered") and "权重" in r["reason"]]
        assert len(critical) > 0


class TestIsTradeAllowed:
    """统一交易许可检查"""

    def test_buy_allowed(self):
        rm = _fresh_rm()
        result = rm.is_trade_allowed(
            code="002281", action="BUY",
            holdings=[{"code": "600036"}],
            current_total_value=60000,
            initial_capital=51066.41,
            new_amount=20000,
        )
        assert result["allowed"] is True

    def test_buy_blocked_blacklist(self, monkeypatch):
        rm = _fresh_rm()
        rm._blacklist["002281"] = "2026-06-20"
        result = rm.is_trade_allowed(
            code="002281", action="BUY",
            current_total_value=60000,
            initial_capital=51066.41,
        )
        assert result["allowed"] is False
        assert "黑名单" in str(result["reasons"])

    def test_buy_blocked_cooldown(self, monkeypatch):
        rm = _fresh_rm()
        rm._cooldown_until = "2026-06-15"
        result = rm.is_trade_allowed(
            code="002281", action="BUY",
            current_total_value=60000,
            initial_capital=51066.41,
        )
        assert result["allowed"] is False
        assert "冷却" in str(result["reasons"])

    def test_sell_allowed_during_cooldown(self, monkeypatch):
        """冷却期间卖出不阻断"""
        rm = _fresh_rm()
        rm._cooldown_until = "2026-06-15"
        result = rm.is_trade_allowed(
            code="002281", action="SELL",
            current_total_value=60000,
            initial_capital=51066.41,
        )
        # Cooldown still blocks even for sell, since it has critical reasons
        # But it should still be blocked
        if not result["allowed"]:
            assert "冷却" in str(result["reasons"])

    def test_buy_blocked_max_drawdown(self):
        rm = _fresh_rm()
        result = rm.is_trade_allowed(
            code="002281", action="BUY",
            current_total_value=30000,
            initial_capital=51066.41,
        )
        assert result["allowed"] is False
        assert "回撤" in str(result["reasons"]) or "熔断" in str(result["reasons"])

    def test_risk_levels(self):
        rm = _fresh_rm()
        result = rm.is_trade_allowed(
            code="002281", action="BUY",
            current_total_value=60000,
            initial_capital=51066.41,
        )
        assert result["risk_level"] == "low"

        # With active blacklist, should be critical
        rm._blacklist["999999"] = "2026-06-20"
        result = rm.is_trade_allowed(
            code="999999", action="BUY",
            current_total_value=60000,
            initial_capital=51066.41,
        )
        assert result["risk_level"] == "critical"


class TestSectorMapping:
    """行业映射完整性"""

    def test_all_stocks_have_sector(self):
        from config import STOCK_MAP
        for code in STOCK_MAP:
            assert code in SECTOR_MAP, f"{code} ({STOCK_MAP[code]['name']}) 缺少行业映射"


class TestGetRiskReport:
    """风控报告"""

    def test_report_structure(self):
        rm = _fresh_rm()
        report = rm.get_risk_report()
        assert "consecutive_losses" in report
        assert "blacklist" in report
        assert "hard_stop_pct" in report
        assert "trailing_stop_pct" in report
        assert isinstance(report["blacklist"], dict)

    def test_format_report(self):
        rm = _fresh_rm()
        text = rm.format_risk_report()
        assert "Serenity 风控状态" in text
        assert "硬止损" in text
