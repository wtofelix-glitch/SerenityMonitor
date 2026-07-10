"""
因子审计 — factor_audit.py

Phase 3b: 因子去冗余 + ICIR 替代简单 IC + bootstrap 置信区间

功能:
  1. 计算 14 因子之间的相关矩阵，识别和合并 |r|>0.7 的因子
  2. 用 ICIR (IC / IC_std) 替代简单 IC 均值
  3. Bootstrap 置信区间
  4. 输出"去冗余后的独立因子集"（6-8 个），
     用于替换 Frozen Baseline v1 → v2

Usage:
    python3 factor_audit.py             # 运行完整因子审计
    python3 factor_audit.py --report    # 输出审计报告
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from db import get_conn
from config import ALL_CODES, STOCK_MAP
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

MIN_OBSERVATIONS = 30          # 最少观测数
CORRELATION_MERGE_THRESHOLD = 0.70  # |r|>0.7 → 合并
ICIR_MIN_THRESHOLD = 0.30      # ICIR < 0.3 → 噪声因子
BOOTSTRAP_SAMPLES = 1000       # bootstrap 采样次数
CI_LEVEL = 0.95                # 置信水平

# 14 个 Alpha 因子（来自 factor_metadata.py）
SIGNAL_FACTORS = [
    "ksft", "kmid", "kmid_2", "kup", "klow",    # K 线形态
    "rank_20", "rsv_20", "beta_20", "resi_20",  # 时序统计
    "macd_signal", "obv_trend", "mfi_signal", "cci_signal",  # 技术指标
    "wq_alpha1", "wq_alpha3", "wq_alpha5", "wq_alpha15", "wq_alpha19",  # WQ Alpha
]
# 实际在用的信号因子（从 factor_metadata 动态获取，此处为 fallback）
try:
    from factor_metadata import SIGNAL_FACTORS as _SF
    SIGNAL_FACTORS = _SF
except ImportError:
    pass


class FactorAudit:
    """因子审计 — 去冗余 + 稳定性评估。"""

    def __init__(self, lookback_days: int = 120):
        self.lookback_days = lookback_days
        self._factor_history: dict[str, dict[str, list[float]]] = {}  # factor → {code: [values]}
        self._returns: dict[str, list[float]] = {}                    # code → [daily returns]
        self._dates: list[str] = []
        self._audit_results: dict = {}

    # ── 数据加载 ──────────────────────────────────────────

    def load_data(self, codes: Optional[list[str]] = None) -> None:
        """从 scoring_history 和 daily_snapshots 加载因子值和收益率数据。"""
        if codes is None:
            codes = list(ALL_CODES)

        conn = get_conn()
        try:
            start = (date.today() - timedelta(days=self.lookback_days)).isoformat()

            # 加载评分历史（含因子分）
            placeholders = ",".join("?" * len(codes))
            score_rows = conn.execute(
                f"SELECT code, date, factor_score, zone_score, momentum_score, "
                f"volume_score, serenity_score, technical_score, moat_score, "
                f"total_score "
                f"FROM scoring_history "
                f"WHERE code IN ({placeholders}) AND date >= ? "
                f"ORDER BY code, date",
                (*codes, start)
            ).fetchall()

            # 组织数据
            by_date: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
            for row in score_rows:
                d = row["date"]
                c = row["code"]
                by_date[d][c] = {
                    "factor_score": row["factor_score"] or 0,
                    "zone_score": row["zone_score"] or 0,
                    "momentum_score": row["momentum_score"] or 0,
                    "volume_score": row["volume_score"] or 0,
                    "serenity_score": row["serenity_score"] or 0,
                    "technical_score": row["technical_score"] or 0,
                    "moat_score": row["moat_score"] or 0,
                    "total_score": row["total_score"] or 0,
                }

            # 加载收益率
            ret_rows = conn.execute(
                f"SELECT code, date, change_pct FROM daily_snapshots "
                f"WHERE code IN ({placeholders}) AND date >= ? "
                f"ORDER BY code, date",
                (*codes, start)
            ).fetchall()

            returns_by_code: dict[str, dict[str, float]] = defaultdict(dict)
            for row in ret_rows:
                returns_by_code[row["code"]][row["date"]] = row["change_pct"] or 0

        finally:
            conn.close()

        # 构建面板数据
        dates = sorted(by_date.keys())
        self._factor_history = defaultdict(lambda: defaultdict(list))
        self._returns = defaultdict(list)
        self._dates = dates

        for d in dates:
            for code in codes:
                scores = by_date[d].get(code, {})
                ret = returns_by_code.get(code, {}).get(d, 0)
                self._returns[code].append(ret)
                for dim in ["factor_score", "zone_score", "momentum_score",
                            "volume_score", "serenity_score", "technical_score", "moat_score"]:
                    self._factor_history[dim][code].append(scores.get(dim, 0))

        log.info(f"因子审计数据加载完成: {len(dates)} 天 × {len(codes)} 标的")

    # ── 相关性矩阵 ────────────────────────────────────────

    @staticmethod
    def _corr(x: list[float], y: list[float]) -> float:
        n = min(len(x), len(y))
        if n < 5:
            return 0.0
        x, y = x[-n:], y[-n:]
        mx, my = sum(x)/n, sum(y)/n
        num = sum((xi-mx)*(yi-my) for xi, yi in zip(x, y))
        dx = math.sqrt(sum((xi-mx)**2 for xi in x))
        dy = math.sqrt(sum((yi-my)**2 for yi in y))
        if dx < 1e-12 or dy < 1e-12:
            return 0.0
        return max(-1.0, min(1.0, num/(dx*dy)))

    def compute_factor_correlation_matrix(self) -> dict[str, dict[str, float]]:
        """计算因子之间的相关性矩阵（跨标的池化）。"""
        dims = list(self._factor_history.keys())
        # 池化：将所有标的的因子值拼接
        pooled: dict[str, list[float]] = {}
        for dim in dims:
            pooled[dim] = []
            for code in self._factor_history[dim]:
                pooled[dim].extend(self._factor_history[dim][code])

        matrix = {}
        for dim in dims:
            matrix[dim] = {}
            for other in dims:
                if other <= dim:
                    continue
                r = self._corr(pooled[dim], pooled[other])
                matrix[dim][other] = round(r, 4)
                if other not in matrix:
                    matrix[other] = {}
                matrix[other][dim] = round(r, 4)

        return matrix

    # ── ICIR ──────────────────────────────────────────────

    @staticmethod
    def _spearman_ic(x: list[float], y: list[float]) -> float:
        """计算 Spearman Rank IC。"""
        n = min(len(x), len(y))
        if n < 5:
            return 0.0
        x, y = x[-n:], y[-n:]
        # rank
        rx = sorted(range(n), key=lambda i: x[i])
        ry = sorted(range(n), key=lambda i: y[i])
        rank_x = [0] * n
        rank_y = [0] * n
        for i, idx in enumerate(rx):
            rank_x[idx] = i + 1
        for i, idx in enumerate(ry):
            rank_y[idx] = i + 1
        # Pearson on ranks
        return FactorAudit._corr(rank_x, rank_y)

    def compute_icir(self, dimension: str, forward_days: int = 1) -> dict:
        """计算指定维度的 ICIR (IC / IC_std) 和 bootstrap CI。

        Returns:
            {mean_ic, std_ic, icir, t_stat, n_obs, ci_lower, ci_upper, significant}
        """
        ic_values = []
        dates = self._dates
        all_codes = list(self._factor_history[dimension].keys())

        for t in range(len(dates) - forward_days):
            scores = []
            fwd_returns = []
            for code in all_codes:
                vals = self._factor_history[dimension].get(code, [])
                rets = self._returns.get(code, [])
                if t < len(vals) and t + forward_days < len(rets):
                    scores.append(vals[t])
                    fwd_returns.append(rets[t + forward_days])
            if len(scores) >= MIN_OBSERVATIONS:
                ic = self._spearman_ic(scores, fwd_returns)
                if not math.isnan(ic):
                    ic_values.append(ic)

        n = len(ic_values)
        if n < 5:
            return {"mean_ic": 0, "std_ic": 0, "icir": 0, "t_stat": 0,
                    "n_obs": n, "ci_lower": 0, "ci_upper": 0, "significant": False}

        mean_ic = sum(ic_values) / n
        var_ic = sum((ic - mean_ic) ** 2 for ic in ic_values) / (n - 1) if n > 1 else 1e-12
        std_ic = math.sqrt(var_ic)
        icir = mean_ic / std_ic if std_ic > 1e-12 else 0.0
        t_stat = mean_ic / (std_ic / math.sqrt(n)) if std_ic > 1e-12 else 0.0

        # Bootstrap CI
        ci_lower, ci_upper = self._bootstrap_ci(ic_values)

        return {
            "mean_ic": round(mean_ic, 4),
            "std_ic": round(std_ic, 4),
            "icir": round(icir, 4),
            "t_stat": round(t_stat, 4),
            "n_obs": n,
            "ci_lower": round(ci_lower, 4),
            "ci_upper": round(ci_upper, 4),
            "significant": abs(icir) >= ICIR_MIN_THRESHOLD,
        }

    def _bootstrap_ci(self, values: list[float]) -> tuple[float, float]:
        """Bootstrap 95% 置信区间。"""
        n = len(values)
        if n < 10:
            return values[0] if values else 0, values[-1] if values else 0

        means = []
        for _ in range(BOOTSTRAP_SAMPLES):
            sample = [values[random.randint(0, n-1)] for _ in range(n)]
            means.append(sum(sample) / n)
        means.sort()
        lower_idx = int(BOOTSTRAP_SAMPLES * (1 - CI_LEVEL) / 2)
        upper_idx = int(BOOTSTRAP_SAMPLES * (1 - (1 - CI_LEVEL) / 2))
        return means[lower_idx], means[upper_idx - 1]

    # ── 去冗余 ────────────────────────────────────────────

    def de_redundancy(self) -> dict:
        """识别高度相关的因子组，合并或择一保留。

        Returns:
            {independent_factors, merged_groups, dropped, recommendations}
        """
        corr_matrix = self.compute_factor_correlation_matrix()
        dims = sorted(corr_matrix.keys())

        # 贪心去冗余
        merged_groups: list[list[str]] = []
        remaining = set(dims)

        for dim in sorted(dims, key=lambda d: len(dims), reverse=True):
            if dim not in remaining:
                continue
            group = [dim]
            remaining.discard(dim)
            for other in list(remaining):
                r = corr_matrix.get(dim, {}).get(other, 0)
                if abs(r) >= CORRELATION_MERGE_THRESHOLD:
                    group.append(other)
                    remaining.discard(other)
            merged_groups.append(group)

        # 每组保留一个代表（选 ICIR 最高的）
        independent = []
        dropped = []
        for group in merged_groups:
            if len(group) == 1:
                independent.append(group[0])
            else:
                # 选 ICIR 最高的作为代表
                best = max(group, key=lambda d: abs(self.compute_icir(d).get("icir", 0)))
                independent.append(best)
                dropped.extend([d for d in group if d != best])

        return {
            "independent_factors": independent,
            "n_independent": len(independent),
            "n_original": len(dims),
            "merged_groups": {f"group_{i}": g for i, g in enumerate(merged_groups) if len(g) > 1},
            "dropped": dropped,
            "correlation_matrix_summary": {
                d: {o: r for o, r in corr_matrix[d].items() if o > d and abs(r) >= CORRELATION_MERGE_THRESHOLD}
                for d in dims
            },
        }

    # ── 完整审计 ──────────────────────────────────────────

    def run_full_audit(self, codes: Optional[list[str]] = None) -> dict:
        """运行完整因子审计。"""
        self.load_data(codes)

        # 1. 去冗余
        redundancy = self.de_redundancy()

        # 2. ICIR 分析（对独立因子）
        icir_results = {}
        for dim in redundancy["independent_factors"]:
            icir_results[dim] = self.compute_icir(dim)

        # 3. 排名
        ranked = sorted(icir_results.items(),
                       key=lambda x: abs(x[1]["icir"]), reverse=True)

        self._audit_results = {
            "date": date.today().isoformat(),
            "redundancy": redundancy,
            "icir": icir_results,
            "ranked_factors": [
                {"dimension": d, **r} for d, r in ranked
            ],
            "noise_factors": [
                d for d, r in icir_results.items()
                if abs(r["icir"]) < ICIR_MIN_THRESHOLD
            ],
            "recommendations": self._generate_recommendations(redundancy, icir_results),
        }

        return self._audit_results

    def _generate_recommendations(self, redundancy: dict,
                                   icir: dict) -> list[str]:
        """生成可操作建议。"""
        recs = []

        # 去冗余建议
        for group_name, members in redundancy.get("merged_groups", {}).items():
            recs.append(f"[去冗余] {group_name}: {', '.join(members)} → "
                       f"合并为一个独立因子")

        # 噪声因子建议
        for dim, r in icir.items():
            if abs(r["icir"]) < ICIR_MIN_THRESHOLD:
                recs.append(f"[降噪] {dim}: ICIR={r['icir']:.3f} < {ICIR_MIN_THRESHOLD} "
                           f"→ 标记为噪声因子，从评分中移除")

        # 保留建议
        independent = redundancy.get("independent_factors", [])
        recs.append(f"[保留] 独立因子集 ({len(independent)} 个): {', '.join(independent)}")

        return recs

    def audit_report(self) -> str:
        """生成 Markdown 格式的完整因子审计报告。"""
        if not self._audit_results:
            self.run_full_audit()

        r = self._audit_results
        lines = [
            "# 因子审计报告",
            f"  日期: {r['date']}",
            f"  回溯窗口: {self.lookback_days} 天",
            f"  标的数: {len(ALL_CODES)}",
            "─" * 48,
            "",
            "## 去冗余结果",
            f"  原始因子: {r['redundancy']['n_original']} 个",
            f"  独立因子: {r['redundancy']['n_independent']} 个",
            f"  合并组: {len(r['redundancy']['merged_groups'])} 组",
            f"  移除: {', '.join(r['redundancy']['dropped']) if r['redundancy']['dropped'] else '无'}",
            "",
            "## ICIR 排名",
            "| 因子 | IC均值 | IC_std | ICIR | t-stat | n | 95% CI | 显著 |",
            "|------|--------|--------|------|--------|---|--------|------|",
        ]

        for item in r["ranked_factors"]:
            sig = "✅" if item["significant"] else "❌"
            lines.append(
                f"| {item['dimension']} | {item['mean_ic']:.4f} | "
                f"{item['std_ic']:.4f} | {item['icir']:.4f} | "
                f"{item['t_stat']:.2f} | {item['n_obs']} | "
                f"[{item['ci_lower']:.4f}, {item['ci_upper']:.4f}] | {sig} |"
            )

        lines.append("")
        lines.append("## 噪声因子 (ICIR < 0.3)")
        for dim in r["noise_factors"]:
            lines.append(f"  - {dim}: ICIR={r['icir'][dim]['icir']:.3f}")
        if not r["noise_factors"]:
            lines.append("  无")

        lines.append("")
        lines.append("## 建议")
        for rec in r["recommendations"]:
            lines.append(f"  {rec}")

        return "\n".join(lines)

    def save_de_redundancy_config(self, filepath: str = "") -> str:
        """保存去冗余后的独立因子集到配置文件，供 Frozen Baseline v2 使用。

        Returns:
            保存的文件路径
        """
        import os
        if not filepath:
            filepath = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                ".de_redundancy_config.json"
            )

        if not self._audit_results:
            self.run_full_audit()

        r = self._audit_results
        config = {
            "generated_at": date.today().isoformat(),
            "lookback_days": self.lookback_days,
            "n_observations": len(self._dates),
            "n_stocks": len(self._factor_history.get("factor_score", {})),
            "warning": (
                "ICIR estimates unstable with < 60 observations. "
                "Re-run when data accumulates."
            ) if len(self._dates) < 60 else "",
            "original_factors": r["redundancy"]["n_original"],
            "independent_factors": r["redundancy"]["independent_factors"],
            "n_independent": r["redundancy"]["n_independent"],
            "merged_groups": r["redundancy"]["merged_groups"],
            "dropped_factors": r["redundancy"]["dropped"],
            "icir_ranking": r["ranked_factors"],
            "noise_factors": r["noise_factors"],
            "recommendations": r["recommendations"],
        }

        with open(filepath, "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        log.info(f"去冗余配置已保存: {filepath}")
        return filepath


# ═══════════════════════════════════════════════════════════════
# 模块级工具
# ═══════════════════════════════════════════════════════════════

def run_audit_and_print():
    """运行因子审计并打印报告。"""
    auditor = FactorAudit()
    auditor.run_full_audit()
    print(auditor.audit_report())
    return auditor._audit_results


if __name__ == "__main__":
    run_audit_and_print()
