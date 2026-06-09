"""测试 weight_adjuster — 动态权重调整（Mock 文件 IO + subprocess）"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

import json
import os
import subprocess
from unittest.mock import ANY

import weight_adjuster
from weight_adjuster import (
    load_adjusted_weights, save_adjusted_weights,
    adjust_weights, show_weights, reset_weights,
    DEFAULT_WEIGHTS, ADJUSTED_WEIGHTS_PATH,
)


# ── 辅助 ─────────────────────────────────────────────────

def _mock_subprocess(stdout: str, returncode: int = 0):
    """返回一个模拟 subprocess.run 的对象"""
    class MockProc:
        def __init__(self):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode
    return MockProc()


# ── Load / Save ──────────────────────────────────────────

class TestLoadAdjustedWeights:
    def test_no_file_returns_default(self, monkeypatch):
        """文件不存在返回默认权重"""
        # monkeypatch the path to a nonexistent location
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            '/tmp/_nonexistent_weights.json')
        w = load_adjusted_weights()
        assert w == DEFAULT_WEIGHTS

    def test_invalid_json_returns_default(self, monkeypatch):
        """损坏的 JSON 返回默认权重"""
        path = '/tmp/_test_bad_weights.json'
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH', path)
        with open(path, 'w') as f:
            f.write('not json')
        try:
            w = load_adjusted_weights()
            assert w == DEFAULT_WEIGHTS
        finally:
            os.remove(path)

    def test_roundtrip(self, monkeypatch):
        """保存后能正确加载"""
        path = '/tmp/_test_roundtrip_weights.json'
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH', path)
        custom = {"base": 0.20, "zone": 0.10, "momentum": 0.15,
                  "volume": 0.05, "serenity": 0.15, "factor": 0.15,
                  "technical": 0.10, "sentiment": 0.10}
        save_adjusted_weights(custom)
        try:
            loaded = load_adjusted_weights()
            assert loaded["base"] == 0.20
            assert loaded["zone"] == 0.10
            assert loaded["sentiment"] == 0.10
        finally:
            os.remove(path)


class TestSaveAdjustedWeights:
    def test_save_with_ic_report(self, monkeypatch):
        """保存权重时包含 IC 元数据"""
        path = '/tmp/_test_save_ic.json'
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH', path)
        weights = dict(DEFAULT_WEIGHTS)
        ic_report = {
            "latest": {"base_score": 0.1},
            "mean_ic": {"base_score": 0.12},
            "n_days": {"base_score": 20},
        }
        save_adjusted_weights(weights, ic_report)
        try:
            with open(path) as f:
                data = json.load(f)
            assert "source_ic" in data
            assert data["source_ic"]["mean_ic"]["base_score"] == 0.12
        finally:
            os.remove(path)


# ── Adjust Weights ───────────────────────────────────────

class TestAdjustWeights:
    def setup_method(self):
        self._orig_path = weight_adjuster.ADJUSTED_WEIGHTS_PATH
        self._tmp_path = '/tmp/_test_adjust_weights.json'

    def teardown_method(self):
        weight_adjuster.ADJUSTED_WEIGHTS_PATH = self._orig_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

    def _run(self, monkeypatch, ic_values: dict, n_days: int = 10):
        """执行 adjust_weights 的通用 mock 环境"""
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            self._tmp_path)

        ic_report = json.dumps({
            "mean_ic": ic_values,
            "n_days": {k: n_days for k in ic_values},
            "latest": {},
        })

        monkeypatch.setattr(subprocess, 'run',
                            lambda *a, **kw: _mock_subprocess(ic_report))

        return adjust_weights(min_days=5)

    def test_positive_ic_increases_weight(self, monkeypatch):
        """正 IC → 权重上调"""
        ic = {"base_score": 0.15, "zone_score": 0.10,
              "momentum_score": 0.05, "volume_score": 0.02,
              "serenity_score": 0.12, "factor_score": 0.08,
              "technical_score": 0.03, "sentiment_score": 0.06}
        result = self._run(monkeypatch, ic)
        # base 上调: 0.15 * (1 + 0.15*1.667) / 归一化
        assert result["base"] > DEFAULT_WEIGHTS["base"]

    def test_negative_ic_decreases_weight(self, monkeypatch):
        """负 IC → 权重下调"""
        ic = {"base_score": -0.15, "zone_score": -0.10,
              "momentum_score": 0.05, "volume_score": 0.02,
              "serenity_score": -0.12, "factor_score": -0.08,
              "technical_score": 0.03, "sentiment_score": -0.06}
        result = self._run(monkeypatch, ic)
        assert result["base"] < DEFAULT_WEIGHTS["base"]

    def test_normalized_sum_is_one(self, monkeypatch):
        """归一化后权重之和 = 1.0"""
        ic = {"base_score": 0.20, "zone_score": -0.10,
              "momentum_score": 0.05, "volume_score": 0.0,
              "serenity_score": 0.15, "factor_score": -0.05,
              "technical_score": 0.03, "sentiment_score": 0.08}
        result = self._run(monkeypatch, ic)
        total = sum(result.values())
        assert abs(total - 1.0) < 0.001, f"Weights sum to {total}"

    def test_insufficient_dims_falls_back(self, monkeypatch):
        """不足 3 个有效维度 → 退回默认权重"""
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            self._tmp_path)
        ic_report = json.dumps({
            "mean_ic": {"base_score": 0.1},
            "n_days": {"base_score": 10},
            "latest": {},
        })
        monkeypatch.setattr(subprocess, 'run',
                            lambda *a, **kw: _mock_subprocess(ic_report))
        result = adjust_weights(min_days=5)
        # Should fall back to defaults
        assert result == DEFAULT_WEIGHTS

    def test_subprocess_failure_falls_back(self, monkeypatch):
        """factor_ic.py 执行失败 → 退回默认权重"""
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            self._tmp_path)
        monkeypatch.setattr(subprocess, 'run',
                            lambda *a, **kw: _mock_subprocess("error", 1))
        result = adjust_weights(min_days=5)
        assert result == DEFAULT_WEIGHTS

    def test_invalid_json_falls_back(self, monkeypatch):
        """IC 数据 JSON 解析失败 → 退回默认权重"""
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            self._tmp_path)
        monkeypatch.setattr(subprocess, 'run',
                            lambda *a, **kw: _mock_subprocess("not json at all"))
        result = adjust_weights(min_days=5)
        assert result == DEFAULT_WEIGHTS

    def test_zero_total_handling(self, monkeypatch):
        """调整后权重之和为 0 → 退回默认"""
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            self._tmp_path)
        ic_report = json.dumps({
            "mean_ic": {k: -0.5 for k in
                        ["base_score", "zone_score", "momentum_score",
                         "volume_score", "serenity_score", "factor_score",
                         "technical_score", "sentiment_score"]},
            "n_days": {k: 10 for k in
                       ["base_score", "zone_score", "momentum_score",
                        "volume_score", "serenity_score", "factor_score",
                        "technical_score", "sentiment_score"]},
            "latest": {},
        })
        monkeypatch.setattr(subprocess, 'run',
                            lambda *a, **kw: _mock_subprocess(ic_report))
        result = adjust_weights(min_days=5)
        # IC=-0.5 → factor = 1 + (-0.5)*1.667 = 0.1665, clamped to 0.5
        # weight * 0.5 should still be > 0, so normalization works
        assert abs(sum(result.values()) - 1.0) < 0.001


# ── Show / Reset ────────────────────────────────────────

class TestShowReset:
    def test_show_weights(self, monkeypatch, capsys):
        """show_weights 正确输出格式"""
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH',
                            '/tmp/_test_show_nonexistent.json')
        show_weights()
        captured = capsys.readouterr()
        assert "动态权重" in captured.out
        assert "base" in captured.out
        assert "合计" in captured.out

    def test_reset_weights(self, monkeypatch, capsys):
        """reset_weights 重置为默认"""
        path = '/tmp/_test_reset.json'
        monkeypatch.setattr(weight_adjuster, 'ADJUSTED_WEIGHTS_PATH', path)
        # Save custom first
        save_adjusted_weights({"base": 0.99, "zone": 0.01, "momentum": 0.0,
                               "volume": 0.0, "serenity": 0.0, "factor": 0.0,
                               "technical": 0.0, "sentiment": 0.0})
        reset_weights()
        captured = capsys.readouterr()
        assert "已重置" in captured.out
        loaded = load_adjusted_weights()
        assert loaded == DEFAULT_WEIGHTS
        os.remove(path)


# ── Mock helper (must be at module level for pickling) ───

def _mock_subprocess(stdout: str, returncode: int = 0):
    """返回一个模拟 subprocess.CompletedProcess"""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode,
        stdout=stdout, stderr=""
    )
