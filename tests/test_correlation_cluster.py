"""
相关性簇分析测试 — correlation_cluster.py

覆盖:
  [x] Pearson 相关系数计算正确性
  [x] 正相关 / 负相关 / 不相关
  [x] 样本量不足（<5）返回 0
  [x] identify_clusters 贪心聚类
  [x] threshold 参数控制聚类粒度
  [x] get_cluster_for / get_cluster_members 查询
  [x] compute_theme_exposure 暴露比例
  [x] check_cluster_concentration 5 条风控规则
  [x] 单一标的 = 独立簇
  [x] get_cluster 单例
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

from correlation_cluster import (
    CorrelationCluster, get_cluster,
    CORRELATION_THRESHOLD, DEFAULT_LOOKBACK_DAYS,
    THEME_EXPOSURE_LIMIT,
)

# 缓存绕过 helper：设置 _last_update 避免 identify_clusters 触发 DB 查询
def _seal_cache(cc: CorrelationCluster) -> None:
    cc._last_update = date.today().isoformat()


# ═══════════════════════════════════════════════════════════════
# Pearson 相关系数
# ═══════════════════════════════════════════════════════════════


class TestPearson:
    """Pearson 相关系数计算"""

    def test_perfect_positive(self):
        r = CorrelationCluster._pearson(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [2.0, 4.0, 6.0, 8.0, 10.0],
        )
        assert r == pytest.approx(1.0, abs=0.01)

    def test_perfect_negative(self):
        r = CorrelationCluster._pearson(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [5.0, 4.0, 3.0, 2.0, 1.0],
        )
        assert r == pytest.approx(-1.0, abs=0.01)

    def test_uncorrelated(self):
        """不相关序列 → 相关系数接近 0"""
        r = CorrelationCluster._pearson(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [3.0, 1.0, 5.0, 2.0, 4.0],
        )
        assert abs(r) < 0.5

    def test_insufficient_samples(self):
        """样本量 < 5 → 返回 0.0"""
        r = CorrelationCluster._pearson(
            [1.0, 2.0],
            [2.0, 4.0],
        )
        assert r == 0.0

    def test_zero_variance(self):
        """方差为 0 的序列 → 返回 0.0"""
        r = CorrelationCluster._pearson(
            [2.0, 2.0, 2.0, 2.0, 2.0],
            [1.0, 2.0, 3.0, 4.0, 5.0],
        )
        assert r == 0.0

    def test_different_lengths_aligned(self):
        """不同长度序列 → 取较短者"""
        r = CorrelationCluster._pearson(
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            [2.0, 4.0, 6.0, 8.0, 10.0],
        )
        assert r == pytest.approx(1.0, abs=0.01)

    def test_clamp_to_range(self):
        """结果限幅在 [-1.0, 1.0]"""
        r = CorrelationCluster._pearson(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [2.0, 4.0, 6.0, 8.0, 10.0],
        )
        assert -1.0 <= r <= 1.0


# ═══════════════════════════════════════════════════════════════
# 聚类算法
# ═══════════════════════════════════════════════════════════════


class TestIdentifyClusters:
    """相关性簇识别"""

    def test_each_code_assigned_to_a_cluster(self):
        """每只标的都被分配到某个簇"""
        cc = CorrelationCluster()

        # mock 相关矩阵（避免访问数据库）
        codes = ["002281", "000988", "600487", "600036"]
        cc._corr_matrix = {
            "002281": {"000988": 0.85, "600487": 0.30, "600036": 0.10},
            "000988": {"002281": 0.85, "600487": 0.35, "600036": 0.05},
            "600487": {"002281": 0.30, "000988": 0.35, "600036": 0.15},
            "600036": {"002281": 0.10, "000988": 0.05, "600487": 0.15},
        }
        _seal_cache(cc)

        clusters = cc.identify_clusters(codes=codes)

        all_members = []
        for members in clusters.values():
            all_members.extend(members)
        assert sorted(all_members) == sorted(codes)

    def test_high_correlation_same_cluster(self):
        """|r| > threshold → 同簇"""
        cc = CorrelationCluster()
        codes = ["A", "B", "C"]
        cc._corr_matrix = {
            "A": {"B": 0.85, "C": 0.20},
            "B": {"A": 0.85, "C": 0.25},
            "C": {"A": 0.20, "B": 0.25},
        }
        _seal_cache(cc)

        clusters = cc.identify_clusters(threshold=0.7, codes=codes)

        # A 和 B 应该同簇（r=0.85 > 0.7）
        # C 应该独立簇（与 A/B 的 r < 0.7）
        assert len(clusters) == 2
        cluster_a = cc.get_cluster_for("A")
        assert cc.get_cluster_for("A") == cc.get_cluster_for("B")
        assert cc.get_cluster_for("A") != cc.get_cluster_for("C")

    def test_low_threshold_more_clustering(self):
        """降低 threshold → 更多标的被合并为较少簇"""
        cc = CorrelationCluster()
        codes = ["X", "Y", "Z"]
        cc._corr_matrix = {
            "X": {"Y": 0.60, "Z": 0.10},
            "Y": {"X": 0.60, "Z": 0.15},
            "Z": {"X": 0.10, "Y": 0.15},
        }
        _seal_cache(cc)

        # threshold=0.5 → 3 只可能合并
        clusters_low = cc.identify_clusters(threshold=0.5, codes=list(codes))
        # threshold=0.8 → 所有独立
        clusters_high = cc.identify_clusters(threshold=0.8, codes=list(codes))

        assert len(clusters_low) <= len(clusters_high)

    def test_negative_correlation_same_cluster(self):
        """|r| > threshold 包含负相关也视为同簇（反向波动也是关联）"""
        cc = CorrelationCluster()
        codes = ["A", "B"]
        cc._corr_matrix = {
            "A": {"B": -0.85},
            "B": {"A": -0.85},
        }
        _seal_cache(cc)

        clusters = cc.identify_clusters(threshold=0.7, codes=codes)
        # 负相关也属于同一簇（|r|=0.85 > 0.7）
        assert len(clusters) == 1


class TestClusterQueries:
    """簇查询接口"""

    def test_get_cluster_for(self):
        cc = CorrelationCluster()
        cc._corr_matrix = {
            "002281": {"000988": 0.85},
            "000988": {"002281": 0.85},
        }
        _seal_cache(cc)
        cc.identify_clusters(codes=["002281", "000988"])

        cid = cc.get_cluster_for("002281")
        assert cid >= 0

    def test_get_cluster_for_unknown_code(self):
        cc = CorrelationCluster()
        assert cc.get_cluster_for("UNKNOWN") == -1

    def test_get_cluster_members(self):
        cc = CorrelationCluster()
        codes = ["002281", "000988", "600487"]
        cc._corr_matrix = {
            "002281": {"000988": 0.85, "600487": 0.20},
            "000988": {"002281": 0.85, "600487": 0.25},
            "600487": {"002281": 0.20, "000988": 0.25},
        }
        _seal_cache(cc)
        clusters = cc.identify_clusters(codes=codes)

        for cid in clusters:
            members = cc.get_cluster_members(cid)
            assert len(members) >= 1
            for code in members:
                assert cc.get_cluster_for(code) == cid


# ═══════════════════════════════════════════════════════════════
# 主题暴露
# ═══════════════════════════════════════════════════════════════


class TestThemeExposure:
    """主题暴露计算"""

    def test_exposure_pct_computed(self):
        cc = CorrelationCluster()
        cc._clusters = {
            0: ["002281", "000988"],
            1: ["600487"],
        }
        cc._code_to_cluster = {
            "002281": 0, "000988": 0,
            "600487": 1,
        }
        cc._corr_matrix = {
            "002281": {"000988": 0.85},
            "000988": {"002281": 0.85},
        }

        positions = {
            "002281": 30000.0,
            "000988": 20000.0,
            "600487": 15000.0,
        }
        total_nav = 70000.0

        exposure = cc.compute_theme_exposure(positions, total_nav)

        # 簇 0: 002281+000988 = 50000/70000 ≈ 71.4%
        # 簇 1: 600487 = 15000/70000 ≈ 21.4%
        assert len(exposure) == 2
        assert exposure[0]["exposure_pct"] == pytest.approx(5 / 7, rel=0.05)
        assert exposure[1]["exposure_pct"] == pytest.approx(1.5 / 7, rel=0.05)

    def test_empty_positions(self):
        cc = CorrelationCluster()
        exposure = cc.compute_theme_exposure({}, 100000.0)
        assert exposure == {}

    def test_zero_nav(self):
        cc = CorrelationCluster()
        exposure = cc.compute_theme_exposure({"002281": 50000.0}, 0.0)
        assert exposure == {}

    def test_get_max_cluster_exposure(self):
        cc = CorrelationCluster()
        cc._clusters = {0: ["002281", "000988"], 1: ["600036"]}
        cc._code_to_cluster = {"002281": 0, "000988": 0, "600036": 1}
        cc._corr_matrix = {}

        positions = {"002281": 40000.0, "000988": 30000.0, "600036": 30000.0}
        total_nav = 100000.0

        max_exp = cc.get_max_cluster_exposure(positions, total_nav)
        # 簇 0 = 70%, 簇 1 = 30%
        assert max_exp == pytest.approx(0.7, rel=0.05)

    def test_exposure_includes_avg_correlation(self):
        """同一簇 ≥2 只标的时包含平均相关度"""
        cc = CorrelationCluster()
        cc._clusters = {0: ["002281", "000988"]}
        cc._code_to_cluster = {"002281": 0, "000988": 0}
        cc._corr_matrix = {
            "002281": {"000988": 0.85},
            "000988": {"002281": 0.85},
        }

        exposure = cc.compute_theme_exposure(
            {"002281": 30000.0, "000988": 20000.0}, 50000.0)

        assert "avg_correlation" in exposure[0]
        assert exposure[0]["avg_correlation"] == pytest.approx(0.85, abs=0.05)


# ═══════════════════════════════════════════════════════════════
# 风控规则
# ═══════════════════════════════════════════════════════════════


class TestConcentrationChecks:
    """风控规则 v4 §11.3"""

    def test_no_alerts_for_diversified(self):
        """充分分散 → 无告警"""
        cc = CorrelationCluster()
        cc._clusters = {0: ["A"], 1: ["B"], 2: ["C"]}
        cc._code_to_cluster = {"A": 0, "B": 1, "C": 2}
        cc._corr_matrix = {}

        result = cc.check_cluster_concentration(
            {"A": 20000.0, "B": 20000.0, "C": 20000.0}, 60000.0)

        block_alerts = [a for a in result["alerts"] if a["rule"] == "theme_exposure_limit"]
        assert len(block_alerts) == 0

    def test_overexposed_triggers_block(self):
        """单簇超过 50% → BLOCK 告警"""
        cc = CorrelationCluster()
        cc._clusters = {0: ["002281", "000988"]}
        cc._code_to_cluster = {"002281": 0, "000988": 0}
        cc._corr_matrix = {
            "002281": {"000988": 0.85},
            "000988": {"002281": 0.85},
        }

        result = cc.check_cluster_concentration(
            {"002281": 40000.0, "000988": 25000.0}, 100000.0)

        # 簇 0 暴露 = 65% > 50% → BLOCK
        block_alerts = [a for a in result["alerts"] if a["level"] == "BLOCK"]
        assert len(block_alerts) >= 1

    def test_high_correlation_warning(self):
        """高相关簇 > 2 只 → WARNING"""
        cc = CorrelationCluster()
        cc._clusters = {0: ["002281", "000988", "603083"]}
        cc._code_to_cluster = {"002281": 0, "000988": 0, "603083": 0}
        cc._corr_matrix = {
            "002281": {"000988": 0.75, "603083": 0.72},
            "000988": {"002281": 0.75, "603083": 0.78},
            "603083": {"002281": 0.72, "000988": 0.78},
        }

        result = cc.check_cluster_concentration(
            {"002281": 10000.0, "000988": 10000.0, "603083": 10000.0}, 100000.0)

        corr_alerts = [a for a in result["alerts"] if a["rule"] == "high_correlation_cluster"]
        assert len(corr_alerts) >= 1
        assert corr_alerts[0]["level"] == "WARNING"

    def test_block_new_buys_in_overexposed_clusters(self):
        """超限簇标记为 block_new_buys"""
        cc = CorrelationCluster()
        cc._clusters = {0: ["002281", "000988"]}
        cc._code_to_cluster = {"002281": 0, "000988": 0}
        cc._corr_matrix = {}

        result = cc.check_cluster_concentration(
            {"002281": 40000.0, "000988": 30000.0}, 100000.0)

        assert 0 in result["block_new_buys_in_cluster"]

    def test_clusters_detail_correct(self):
        cc = CorrelationCluster()
        cc._clusters = {0: ["002281"]}
        cc._code_to_cluster = {"002281": 0}
        cc._corr_matrix = {}

        result = cc.check_cluster_concentration(
            {"002281": 30000.0}, 100000.0)

        assert 0 in result["clusters_detail"]
        assert result["clusters_detail"][0]["exposure_pct"] == 30.0
        assert "002281" in result["clusters_detail"][0]["members"]


class TestSingleton:
    """get_cluster 单例"""

    def test_get_cluster_returns_instance(self):
        c = get_cluster()
        assert isinstance(c, CorrelationCluster)

    def test_same_instance(self):
        c1 = get_cluster()
        c2 = get_cluster()
        assert c1 is c2
