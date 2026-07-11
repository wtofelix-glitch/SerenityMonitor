"""Tests for pool_shadow_monitor — shadow delist monitoring."""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pool_shadow_monitor import (
    UniverseShadowMonitor,
    _pearson_correlation,
    UNIVERSE_STATUSES,
    HARD_QUAL_FLAGS,
    ECONOMIC_FLAGS,
    LOGIC_FLAGS,
    PROBATION_MIN_DAYS,
)
from config import ALL_CODES, STOCK_MAP


class TestPoolShadowMonitor(unittest.TestCase):

    def setUp(self):
        self.monitor = UniverseShadowMonitor(oos_frozen=True)

    # ── 状态机 ──────────────────────────────────────────

    def test_all_stocks_active_when_no_hard_qual_evidence(self):
        """OOS 冻结期间，无硬资格证据时所有标的保持 ACTIVE。"""
        results = self.monitor.compute_all()
        for s in results:
            if not s.hard_qual_flags:
                self.assertEqual(s.status, "ACTIVE",
                                f"{s.code} 无硬资格证据时应为 ACTIVE，实际 {s.status}")

    def test_oos_frozen_prevents_archive(self):
        """OOS 冻结期间，任何标的不能进入 ARCHIVE。"""
        results = self.monitor.compute_all()
        for s in results:
            self.assertNotEqual(s.status, "ARCHIVE",
                               f"OOS 冻结期间 {s.code} 不应为 ARCHIVE")

    def test_status_values_are_valid(self):
        """所有状态必须是有效枚举值。"""
        results = self.monitor.compute_all()
        for s in results:
            self.assertIn(s.status, UNIVERSE_STATUSES,
                         f"{s.code} 状态 {s.status} 不是有效值")

    # ── 数据完整性 ─────────────────────────────────────

    def test_all_pool_stocks_are_evaluated(self):
        """固定池中每只标的都必须被评估。"""
        results = self.monitor.compute_all()
        evaluated = {s.code for s in results}
        expected = set(ALL_CODES)
        self.assertEqual(evaluated, expected,
                        f"评估集合不匹配: 缺 {expected - evaluated}, 多 {evaluated - expected}")

    def test_each_stock_has_name_and_tier(self):
        """每只标的必须有名字和层级。"""
        results = self.monitor.compute_all()
        for s in results:
            self.assertTrue(s.name, f"{s.code} 缺少名字")
            self.assertGreaterEqual(s.tier, 1, f"{s.code} tier 应为 1-4")
            self.assertLessEqual(s.tier, 4, f"{s.code} tier 应为 1-4")

    def test_hard_qual_flags_are_valid(self):
        """硬资格标记必须来自预定义集合。"""
        results = self.monitor.compute_all()
        for s in results:
            for flag in s.hard_qual_flags:
                self.assertIn(flag, HARD_QUAL_FLAGS,
                             f"{s.code} 无效的硬资格标记: {flag}")

    def test_economic_flags_are_valid(self):
        """经济贡献标记必须来自预定义集合。"""
        results = self.monitor.compute_all()
        for s in results:
            for flag in s.economic_flags:
                self.assertIn(flag, ECONOMIC_FLAGS,
                             f"{s.code} 无效的经济贡献标记: {flag}")

    def test_logic_flags_are_valid(self):
        """产业逻辑标记必须来自预定义集合。"""
        results = self.monitor.compute_all()
        for s in results:
            for flag in s.logic_flags:
                self.assertIn(flag, LOGIC_FLAGS,
                             f"{s.code} 无效的产业逻辑标记: {flag}")

    # ── 不可变性 ───────────────────────────────────────

    def test_stock_map_is_never_mutated(self):
        """compute_all() 绝不修改 STOCK_MAP。"""
        before = dict(STOCK_MAP)
        self.monitor.compute_all()
        after = dict(STOCK_MAP)
        self.assertEqual(before, after, "STOCK_MAP 被修改了！退池监控绝不能改动正式池。")

    def test_all_codes_stays_constant(self):
        """compute_all() 绝不修改 ALL_CODES。"""
        before = list(ALL_CODES)
        self.monitor.compute_all()
        after = list(ALL_CODES)
        self.assertEqual(before, after, "ALL_CODES 被修改了！")

    # ── 统计工具 ────────────────────────────────────────

    def test_pearson_perfect_correlation(self):
        """完全正相关 → 1.0。"""
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(_pearson_correlation(a, a), 1.0, places=3)

    def test_pearson_perfect_negative(self):
        """完全负相关 → -1.0。"""
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [5.0, 4.0, 3.0, 2.0, 1.0]
        self.assertAlmostEqual(_pearson_correlation(a, b), -1.0, places=3)

    def test_pearson_too_short_returns_zero(self):
        """少于 3 个数据点 → 0.0。"""
        self.assertEqual(_pearson_correlation([1.0], [1.0]), 0.0)

    def test_pearson_constant_series(self):
        """常数序列 → 0.0。"""
        self.assertEqual(_pearson_correlation([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0]), 0.0)

    # ── 持久化 ─────────────────────────────────────────

    def test_record_daily_writes_to_db(self):
        """record_daily() 必须写入 universe_status_log 表。"""
        from db import get_conn
        self.monitor.record_daily()
        conn = get_conn()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM universe_status_log WHERE date = ?",
            (__import__("datetime").date.today().isoformat(),),
        ).fetchone()["cnt"]
        conn.close()
        self.assertGreaterEqual(count, len(ALL_CODES),
                               f"应为每只标的记录一行，写了 {count} 行")

    def test_record_daily_no_exceptions(self):
        """record_daily() 对当前池不应抛异常。"""
        try:
            self.monitor.record_daily()
        except Exception as e:
            self.fail(f"record_daily() 抛出异常: {e}")

    # ── 经济证据样本不足时安全 ──────────────────────────

    def test_zero_signal_samples_no_economic_flags(self):
        """信号样本为 0 时，不产生经济贡献标记（样本不足不能判）。"""
        results = self.monitor.compute_all()
        for s in results:
            if s.signal_samples == 0:
                # 可以有其他标记，但不应该有 negative_marginal_return
                self.assertNotIn("negative_marginal_return", s.economic_flags,
                                f"{s.code} 0 样本但标记了负边际收益")

    # ── 报告生成 ────────────────────────────────────────

    def test_generate_report_returns_string(self):
        """generate_report() 返回非空字符串。"""
        report = self.monitor.generate_report()
        self.assertIsInstance(report, str)
        self.assertGreater(len(report), 100)
        self.assertIn("影子退池监控报告", report)

    def test_generate_report_includes_pool_size(self):
        """报告包含池规模和 OOS 状态。"""
        report = self.monitor.generate_report()
        self.assertIn(f"{len(ALL_CODES)} 只", report)
        self.assertIn("OOS 冻结", report)


if __name__ == "__main__":
    unittest.main()
