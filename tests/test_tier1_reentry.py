"""测试 tier1_reentry — T1 标的回补提醒"""

import sys
import json
import os
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

from datetime import date, datetime
from tier1_reentry import (
    _load_state, _save_state, _format_reentry_msg,
    check_tier1_reentry, cmd_status, _zone_label_cn,
    STATE_PATH,
)


class TestStateFileIO:
    def test_load_nonexistent(self, tmp_path):
        """不存在的状态文件返回空字典"""
        fake_path = tmp_path / "nonexistent.json"
        assert not fake_path.exists()
        # 用 monkeypatch 替换 STATE_PATH 避免使用真实的持久化文件
        import tier1_reentry
        original_path = tier1_reentry.STATE_PATH
        tier1_reentry.STATE_PATH = str(fake_path)
        assert _load_state() == {}
        tier1_reentry.STATE_PATH = original_path

    def test_load_invalid_json(self, monkeypatch):
        """损坏的 JSON 文件返回空字典"""
        monkeypatch.setattr('tier1_reentry.STATE_PATH', '/tmp/_test_bad.json')
        with open('/tmp/_test_bad.json', 'w') as f:
            f.write('not json')
        result = _load_state()
        assert result == {}
        os.remove('/tmp/_test_bad.json')

    def test_save_and_load_roundtrip(self, monkeypatch):
        """保存后能正确加载"""
        monkeypatch.setattr('tier1_reentry.STATE_PATH', '/tmp/_test_round.json')
        data = {"002281": {"zone_class": "buy_zone", "last_push_date": "2026-06-08"}}
        _save_state(data)
        loaded = _load_state()
        assert loaded["002281"]["zone_class"] == "buy_zone"
        assert loaded["002281"]["last_push_date"] == "2026-06-08"
        os.remove('/tmp/_test_round.json')

    def test_save_empty_dict(self, monkeypatch):
        """保存空字典不崩溃"""
        monkeypatch.setattr('tier1_reentry.STATE_PATH', '/tmp/_test_empty.json')
        _save_state({})
        loaded = _load_state()
        assert loaded == {}
        os.remove('/tmp/_test_empty.json')


class TestFormatReentryMsg:
    def test_below_zone_format(self):
        msg = _format_reentry_msg({
            "code": "000988", "name": "华工科技", "price": 110.0,
            "change_pct": -3.5, "zone_class": "below",
            "zone_label": "低于买入区 📉", "zone_low": 130.0,
            "zone_high": 170.0, "target_sell": 250.0,
            "dist_pct": 15.4, "tier": 1,
        })
        assert "T1 回补机会" in msg
        assert "华工科技" in msg
        assert "低于买入区" in msg
        assert "折扣" in msg

    def test_buy_zone_format(self):
        msg = _format_reentry_msg({
            "code": "002281", "name": "光迅科技", "price": 200.0,
            "change_pct": 0.5, "zone_class": "buy_zone",
            "zone_label": "买入区 ✅", "zone_low": 180.0,
            "zone_high": 230.0, "target_sell": 320.0,
            "dist_pct": None, "tier": 1,
        })
        assert "正处买入区" in msg

    def test_negative_change_format(self):
        msg = _format_reentry_msg({
            "code": "002281", "name": "光迅科技", "price": 180.0,
            "change_pct": -2.1, "zone_class": "buy_zone",
            "zone_label": "买入区 ✅", "zone_low": 180.0,
            "zone_high": 230.0, "target_sell": 320.0,
            "dist_pct": None, "tier": 1,
        })
        assert "-2.10%" in msg

    def test_positive_change_format(self):
        msg = _format_reentry_msg({
            "code": "002281", "name": "光迅科技", "price": 200.0,
            "change_pct": 1.5, "zone_class": "buy_zone",
            "zone_label": "买入区 ✅", "zone_low": 180.0,
            "zone_high": 230.0, "target_sell": 320.0,
            "dist_pct": None, "tier": 1,
        })
        assert "+1.50%" in msg

    def test_target_upside_calculated(self):
        """目标上涨百分比正确计算"""
        msg = _format_reentry_msg({
            "code": "002281", "name": "光迅科技", "price": 200.0,
            "change_pct": 0, "zone_class": "buy_zone",
            "zone_label": "买入区 ✅", "zone_low": 180.0,
            "zone_high": 230.0, "target_sell": 320.0,
            "dist_pct": None, "tier": 1,
        })
        assert "60.0%" in msg  # (320-200)/200 = 60%


class TestZoneLabel:
    def test_buy_zone_labels(self):
        assert "买入区" in _zone_label_cn("")
        assert "买入区" in _zone_label_cn("buy_zone")

    def test_below_label(self):
        assert "低于" in _zone_label_cn("below")

    def test_above_label(self):
        assert "高于" in _zone_label_cn("above")

    def test_done_label(self):
        assert "已达目标" in _zone_label_cn("done")

    def test_fallback(self):
        assert _zone_label_cn("unknown") == "unknown"


class TestCheckTier1Reentry:
    """check_tier1_reentry 核心逻辑"""

    def _make_realtime(self, code: str, price: float, change: float = 0) -> list[dict]:
        return [{"code": code, "price": price, "change_pct": change}]

    def test_stock_in_buy_zone_triggers_alert(self, monkeypatch, tmp_path):
        """标的正处买入区 → 触发提醒"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        realtime = self._make_realtime("002281", 200.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        # 模拟第一次检查（prev_class 为空 → was_outside=True）
        results = check_tier1_reentry()
        assert len(results) == 1
        assert results[0]["code"] == "002281"
        assert results[0]["zone_class"] == "buy_zone" or results[0]["zone_class"] == ""

    def test_stock_above_zone_no_alert(self, monkeypatch, tmp_path):
        """标的高于买入区 → 不触发"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        realtime = self._make_realtime("002281", 250.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry()
        assert len(results) == 0

    def test_stock_at_target_no_alert(self, monkeypatch, tmp_path):
        """标的已达目标价 → 不触发"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        realtime = self._make_realtime("002281", 320.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry()
        assert len(results) == 0

    def test_duplicate_push_suppressed_within_24h(self, monkeypatch, tmp_path):
        """同一天不重复推送"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        # 第一次：触发推送
        realtime = self._make_realtime("002281", 200.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results1 = check_tier1_reentry()
        assert len(results1) == 1

        # 第二次（同一天）：不应触发
        results2 = check_tier1_reentry()
        assert len(results2) == 0

    def test_price_below_zone_triggers_alert(self, monkeypatch, tmp_path):
        """价格低于买入区 → 触发折扣提醒"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        realtime = self._make_realtime("002281", 150.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry()
        assert len(results) >= 1

    def test_from_above_to_buy_zone_triggers_alert(self, monkeypatch, tmp_path):
        """从 above 回落到 buy_zone → 触发"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        # 先设状态为 above
        _save_state({"002281": {"zone_class": "above"}})
        # 改为 buy_zone 的价格
        realtime = self._make_realtime("002281", 200.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry()
        assert len(results) == 1

    def test_from_belown_to_below_no_alert(self, monkeypatch, tmp_path):
        """之前 below 现在依然 below → 不触发（已推过）"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        # 先设置状态为 below
        _save_state({"002281": {"zone_class": "below", "last_push_date": "2026-06-08"}})
        # 仍然 below
        realtime = self._make_realtime("002281", 150.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry()
        assert len(results) == 0

    def test_push_flag_sends_wechat(self, monkeypatch, tmp_path):
        """--push 时触发推送"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        pushed = []
        monkeypatch.setattr('tier1_reentry.push_alert',
                           lambda t, c, m: pushed.append((c, m)))

        realtime = self._make_realtime("002281", 200.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry(push=True)
        assert len(results) == 1
        assert len(pushed) == 1
        assert pushed[0][0] == "002281"

    def test_both_tier1_stocks_checked(self, monkeypatch, tmp_path):
        """两只 Tier 1 标的都被检查"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        def mock_fetch(codes):
            return [
                {"code": "002281", "price": 200.0, "change_pct": 0},
                {"code": "000988", "price": 150.0, "change_pct": 0},
            ]
        monkeypatch.setattr('tier1_reentry.fetch_realtime', mock_fetch)
        results = check_tier1_reentry()
        codes = {r["code"] for r in results}
        # 两只都在 buy_zone，都会触发
        assert "002281" in codes
        assert "000988" in codes

    def test_zero_price_skipped(self, monkeypatch, tmp_path):
        """价格为 0 的标的不触发"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        realtime = self._make_realtime("002281", 0.0)
        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: realtime)
        results = check_tier1_reentry()
        assert len(results) == 0

    def test_cmd_status_output(self, monkeypatch, tmp_path, capsys):
        """cmd_status 正常输出"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))
        _save_state({"002281": {"zone_class": "buy_zone", "last_push_date": "2026-06-08"}})

        cmd_status()
        captured = capsys.readouterr()
        assert "Tier 1" in captured.out
        assert "002281" in captured.out

    def test_no_realtime_data_skipped(self, monkeypatch, tmp_path):
        """无实时数据时不触发"""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr('tier1_reentry.STATE_PATH', str(state_file))

        monkeypatch.setattr('tier1_reentry.fetch_realtime', lambda c: [])
        results = check_tier1_reentry()
        assert len(results) == 0
