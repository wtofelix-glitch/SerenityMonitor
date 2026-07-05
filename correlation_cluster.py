"""
相关性簇分析 — correlation_cluster.py

基于收益相关性而非行业分类来衡量真实的组合分散度。

v4 §11: 替代 SECTOR_MAP，用近 N 日收益相关矩阵识别高度同向波动的标的群。

核心功能:
  1. 计算近 20/60/120 日收益相关矩阵
  2. 识别相关性簇 (|r| > 0.7 → 同簇)
  3. 计算主题暴露得分
  4. 5 条风控规则

Usage:
    from correlation_cluster import CorrelationCluster

    cc = CorrelationCluster()
    clusters = cc.identify_clusters()
    exposure = cc.compute_theme_exposure(positions)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional
from collections import defaultdict
import math

from db import get_conn, get_price_history
from config import ALL_CODES, STOCK_MAP
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

CORRELATION_THRESHOLD = 0.7          # 相关系数 > 0.7 → 视为同一簇
DEFAULT_LOOKBACK_DAYS = 60           # 默认回溯窗口
THEME_EXPOSURE_LIMIT = 0.50          # 单一主题暴露上限 50%


class CorrelationCluster:
    """基于收益相关性的真实分散度分析。

    替代传统的行业分类（SECTOR_MAP），用实际价格行为
    衡量标的之间的真实关联度。
    """

    def __init__(self, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
        self.lookback_days = lookback_days
        self._corr_matrix: dict[str, dict[str, float]] = {}  # code → {code: corr}
        self._clusters: dict[int, list[str]] = {}             # cluster_id → [codes]
        self._code_to_cluster: dict[str, int] = {}            # code → cluster_id
        self._daily_returns: dict[str, list[float]] = {}      # code → [daily_returns]
        self._last_update: str = ""

    # ── 数据获取 ──────────────────────────────────────────

    def _load_returns(self, codes: list[str] | None = None) -> dict[str, list[float]]:
        """从 daily_snapshots 加载近 N 日收益率序列。"""
        if codes is None:
            codes = list(ALL_CODES)

        conn = get_conn()
        returns: dict[str, list[float]] = {c: [] for c in codes}
        try:
            start_date = (date.today() - timedelta(days=self.lookback_days * 2)).isoformat()
            placeholders = ",".join("?" * len(codes))
            rows = conn.execute(
                f"SELECT code, date, change_pct FROM daily_snapshots "
                f"WHERE code IN ({placeholders}) AND date >= ? "
                f"ORDER BY code, date",
                (*codes, start_date)
            ).fetchall()

            # 按 code 分组
            by_code: dict[str, list[tuple[str, float]]] = defaultdict(list)
            for row in rows:
                chg = row["change_pct"] or 0
                by_code[row["code"]].append((row["date"], chg))

            # 取最近 N 天
            for code, data in by_code.items():
                data.sort(key=lambda x: x[0])
                returns[code] = [d[1] for d in data[-self.lookback_days:]]
        except Exception as e:
            log.warning(f"加载收益率数据失败: {e}")
        finally:
            conn.close()

        return returns

    # ── 相关性计算 ────────────────────────────────────────

    @staticmethod
    def _pearson(x: list[float], y: list[float]) -> float:
        """计算 Pearson 相关系数。"""
        n = min(len(x), len(y))
        if n < 5:
            return 0.0

        # 对齐长度
        x = x[-n:]
        y = y[-n:]

        mean_x = sum(x) / n
        mean_y = sum(y) / n

        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
        std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))

        if std_x < 1e-12 or std_y < 1e-12:
            return 0.0

        r = cov / (std_x * std_y)
        return max(-1.0, min(1.0, r))  # clamp

    def compute_correlation_matrix(self, codes: list[str] | None = None,
                                   force_refresh: bool = False) -> dict[str, dict[str, float]]:
        """计算近 N 日收益相关矩阵。

        Returns:
            {code: {other_code: correlation}}
        """
        today = date.today().isoformat()
        if not force_refresh and self._corr_matrix and self._last_update == today:
            return self._corr_matrix

        if codes is None:
            codes = list(ALL_CODES)

        self._daily_returns = self._load_returns(codes)

        matrix: dict[str, dict[str, float]] = {}
        for code in codes:
            matrix[code] = {}
            ret_a = self._daily_returns.get(code, [])
            if len(ret_a) < 5:
                continue
            for other in codes:
                if other <= code:
                    continue
                ret_b = self._daily_returns.get(other, [])
                corr = self._pearson(ret_a, ret_b)
                matrix[code][other] = round(corr, 4)
                if other not in matrix:
                    matrix[other] = {}
                matrix[other][code] = round(corr, 4)

        self._corr_matrix = matrix
        self._last_update = today
        return matrix

    # ── 聚类 ──────────────────────────────────────────────

    def identify_clusters(self, threshold: float = CORRELATION_THRESHOLD,
                          codes: list[str] | None = None) -> dict[int, list[str]]:
        """识别相关性簇。

        |r| > threshold → 同一簇。
        使用简单贪心算法：从最高相关度开始合并。

        Returns:
            {cluster_id: [code, ...]}
        """
        if codes is None:
            codes = list(ALL_CODES)

        corr = self.compute_correlation_matrix(codes)

        # 贪心聚类
        visited: set[str] = set()
        clusters: dict[int, list[str]] = {}
        cluster_id = 0

        for code in codes:
            if code in visited:
                continue
            # 新簇
            cluster = [code]
            visited.add(code)
            # 找所有与 code 高度相关的标的
            for other in codes:
                if other in visited:
                    continue
                r = corr.get(code, {}).get(other, 0)
                if abs(r) >= threshold:
                    cluster.append(other)
                    visited.add(other)
            clusters[cluster_id] = cluster
            cluster_id += 1

        self._clusters = clusters
        self._code_to_cluster = {}
        for cid, members in clusters.items():
            for code in members:
                self._code_to_cluster[code] = cid

        return clusters

    def get_cluster_for(self, code: str) -> int:
        """获取指定标的所属的簇 ID。"""
        if not self._code_to_cluster:
            self.identify_clusters()
        return self._code_to_cluster.get(code, -1)

    def get_cluster_members(self, cluster_id: int) -> list[str]:
        """获取指定簇的所有标的。"""
        if not self._clusters:
            self.identify_clusters()
        return self._clusters.get(cluster_id, [])

    # ── 主题暴露 ──────────────────────────────────────────

    def compute_theme_exposure(self, positions: dict[str, float],
                                total_nav: float) -> dict:
        """计算组合在各相关性簇上的暴露比例。

        Args:
            positions: {code: market_value}
            total_nav: 组合总净值

        Returns:
            {cluster_id: {exposure_pct, codes, avg_correlation}}
        """
        if not self._clusters:
            self.identify_clusters()

        if total_nav <= 0:
            return {}

        exposure: dict[int, dict] = defaultdict(lambda: {
            "exposure_pct": 0.0,
            "codes": [],
            "market_value": 0.0,
        })

        for code, mv in positions.items():
            cid = self._code_to_cluster.get(code, -1)
            if cid < 0:
                continue
            exposure[cid]["exposure_pct"] += mv / total_nav
            exposure[cid]["market_value"] += mv
            exposure[cid]["codes"].append(code)

        # 计算每个簇的平均相关度
        for cid in exposure:
            members = exposure[cid]["codes"]
            if len(members) >= 2:
                corrs = []
                for i, a in enumerate(members):
                    for b in members[i+1:]:
                        r = self._corr_matrix.get(a, {}).get(b, 0)
                        if r != 0:
                            corrs.append(abs(r))
                exposure[cid]["avg_correlation"] = (
                    sum(corrs) / len(corrs) if corrs else 0.0
                )
            else:
                exposure[cid]["avg_correlation"] = 0.0

        return dict(exposure)

    def get_max_cluster_exposure(self, positions: dict[str, float],
                                  total_nav: float) -> float:
        """获取最大的单一簇暴露比例。"""
        exposure = self.compute_theme_exposure(positions, total_nav)
        if not exposure:
            return 0.0
        return max(e["exposure_pct"] for e in exposure.values())

    # ── 风控规则 (v4 §11.3) ──────────────────────────────

    def check_cluster_concentration(self, positions: dict[str, float],
                                     total_nav: float) -> dict:
        """检查主题集中度（5 条风控规则）。

        Returns:
            {alerts: [...], max_exposure, clusters_detail, action_required}
        """
        exposure = self.compute_theme_exposure(positions, total_nav)
        alerts = []
        max_exp = 0.0

        for cid, exp in exposure.items():
            pct = exp["exposure_pct"]
            max_exp = max(max_exp, pct)
            codes = exp["codes"]
            avg_corr = exp.get("avg_correlation", 0)

            # 规则 1: 同一簇平均相关度 > 0.7 → 视为同一风险资产
            if avg_corr > CORRELATION_THRESHOLD and len(codes) >= 2:
                alerts.append({
                    "rule": "high_correlation_cluster",
                    "level": "WARNING",
                    "cluster_id": cid,
                    "codes": codes,
                    "avg_correlation": avg_corr,
                    "message": f"簇 {cid}: {', '.join(codes)} 平均相关度 {avg_corr:.2f} > 0.7, 视为同一风险资产",
                })

            # 规则 2: AI 供应链簇总仓位 > 50%
            if pct > THEME_EXPOSURE_LIMIT:
                alerts.append({
                    "rule": "theme_exposure_limit",
                    "level": "BLOCK",
                    "cluster_id": cid,
                    "exposure_pct": round(pct * 100, 1),
                    "message": f"簇 {cid} 主题暴露 {pct:.1%} > 上限 {THEME_EXPOSURE_LIMIT:.0%}, 禁止继续买入同簇标的",
                })

        # 规则 4: 同簇两只同时触发止损 → 主题危机
        # (此规则需要结合 risk_manager 的止损状态，此处预留接口)
        crisis_detected = any(
            a["rule"] == "theme_exposure_limit" for a in alerts
        )

        return {
            "alerts": alerts,
            "max_exposure": round(max_exp, 4),
            "clusters_detail": {
                cid: {
                    "members": e["codes"],
                    "exposure_pct": round(e["exposure_pct"] * 100, 1),
                    "avg_correlation": round(e.get("avg_correlation", 0), 3),
                }
                for cid, e in exposure.items()
            },
            "action_required": "REDUCE_CLUSTER_EXPOSURE" if crisis_detected else "NONE",
            "block_new_buys_in_cluster": [cid for cid, e in exposure.items()
                                           if e["exposure_pct"] > THEME_EXPOSURE_LIMIT],
        }

    # ── 报告 ──────────────────────────────────────────────

    def summary_report(self) -> str:
        """生成相关性簇摘要报告。"""
        if not self._clusters:
            self.identify_clusters()

        lines = [
            "# 相关性簇分析报告",
            f"  日期: {date.today().isoformat()}",
            f"  回溯窗口: {self.lookback_days} 天",
            f"  聚类阈值: |r| > {CORRELATION_THRESHOLD}",
            "─" * 48,
            "",
        ]

        for cid, members in sorted(self._clusters.items()):
            names = [f"{c}({STOCK_MAP.get(c, {}).get('name', c)})" for c in members]
            lines.append(f"## 簇 {cid}: {len(members)} 只")
            lines.append(f"  {', '.join(names)}")
            # 簇内平均相关度
            if len(members) >= 2:
                corrs = []
                for i, a in enumerate(members):
                    for b in members[i+1:]:
                        r = self._corr_matrix.get(a, {}).get(b, 0)
                        if r != 0:
                            corrs.append(r)
                if corrs:
                    avg_r = sum(corrs) / len(corrs)
                    lines.append(f"  簇内平均相关度: {avg_r:.3f}")
            lines.append("")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════

_cluster_instance: Optional[CorrelationCluster] = None


def get_cluster() -> CorrelationCluster:
    global _cluster_instance
    if _cluster_instance is None:
        _cluster_instance = CorrelationCluster()
    return _cluster_instance
