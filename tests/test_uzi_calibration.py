"""Tests for UZI weight calibration helpers."""

from uzi_calibration import _rank_values, format_uzi_calibration, rank_ic


def test_rank_values_handles_ties_with_average_rank():
    assert _rank_values([10, 20, 20, 30]) == [0.0, 1.5, 1.5, 3.0]


def test_rank_ic_positive_and_negative():
    assert rank_ic([1, 2, 3, 4], [10, 20, 30, 40]) > 0.99
    assert rank_ic([1, 2, 3, 4], [40, 30, 20, 10]) < -0.99


def test_format_uzi_calibration_warning():
    text = format_uzi_calibration({"ok": False, "warning": "没有数据"})
    assert "UZI 权重校准" in text
    assert "没有数据" in text
