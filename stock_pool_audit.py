"""
股票池后见之明审计 — stock_pool_audit.py

Phase 3a: 回溯 15 只标的的入池时间线，构建"主题启动前固定候选范围"
反事实回测，分离"选池效应"和"池内择时能力"。

v4 §7.4: 即使交易内核、可成交性模拟器、Frozen Baseline 全部做对，
如果标的池的选择本身隐含了"事后诸葛亮"，所有下游分析都在被污染的
样本空间里打转。

Usage:
    python3 stock_pool_audit.py            # 运行完整审计
    python3 stock_pool_audit.py --report   # 输出审计报告
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional
from collections import defaultdict

from config import ALL_CODES, STOCK_MAP, STOCK_DETAILS, get_stock_name
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 入池时间线（来自 BUILD_PLAN.md + git log + config 变更记录）
# ═══════════════════════════════════════════════════════════════

# 第一期 (BUILD_PLAN): T1-T3, 共 10 只
POOL_V1 = {
    "002281": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 1: 光器件全产业链龙头"},
    "000988": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 1: 光模块+激光双主线"},
    "688361": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 1: 测试设备 (后移除, 科创板)"},
    "300308": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 2: 全球光模块龙头 (后移除, 创业板)"},
    "600206": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 2: InP 衬底国产替代 (后移除)"},
    "300661": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 2: PMIC 隐形瓶颈 (后移除, 创业板)"},
    "300502": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 2: 光模块 (后移除, 创业板)"},
    "300394": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 2: (后移除, 创业板)"},
    "300281": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 2: 光芯片整链 (后移除, 创业板)"},
    "688256": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 3: 国产AI芯片 (后移除, 科创板)"},
    "002371": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 3: 设备龙头 (后移除)"},
    "688012": {"entered": "2025-05-25", "reason": "BUILD_PLAN Tier 3: 刻蚀设备 (后移除, 科创板)"},
}

# 第二期: 全面替换为主板标的
POOL_V2 = {
    "603083": {"entered": "2025-06-10", "reason": "Tier 2: 剑桥科技 高速光模块弹性标的"},
    "600487": {"entered": "2025-06-10", "reason": "Tier 2: 亨通光电 光纤光缆龙头"},
    "600141": {"entered": "2025-06-10", "reason": "Tier 2: 兴发集团 磷化工"},
    "002428": {"entered": "2025-06-10", "reason": "Tier 3: 云南锗业 锗衬底"},
    "600460": {"entered": "2025-06-10", "reason": "Tier 3: 士兰微 功率半导体"},
    "603986": {"entered": "2025-06-10", "reason": "Tier 3: 兆易创新 NOR Flash/DRAM"},
    "600176": {"entered": "2025-06-10", "reason": "Tier 3: 中国巨石 电子布"},
}

# 第三期: 防御组合
POOL_V3 = {
    "600036": {"entered": "2025-07-15", "reason": "Tier 4: 招商银行 防御底仓"},
    "600585": {"entered": "2025-07-15", "reason": "Tier 4: 海螺水泥 防御底仓"},
    "600900": {"entered": "2025-07-15", "reason": "Tier 4: 长江电力 防御底仓"},
    "601398": {"entered": "2025-07-15", "reason": "Tier 4: 工商银行 防御底仓"},
    "601006": {"entered": "2025-07-15", "reason": "Tier 4: 大秦铁路 防御底仓"},
}

# 第四期: 实盘扩展
POOL_V4 = {
    "000938": {"entered": "2026-01-10", "reason": "实盘扩展: 紫光股份 AI交换机"},
}

# 第五期: 机器人/自动化（宇树IPO+特斯拉Optimus量产催化）
POOL_V5 = {
    "601689": {"entered": "2026-07-06", "reason": "Tier 2: 拓普集团 Tesla Optimus执行器模组"},
    "002050": {"entered": "2026-07-06", "reason": "Tier 2: 三花智控 热管理/执行器"},
    "601100": {"entered": "2026-07-06", "reason": "Tier 2: 恒立液压 液压件→人形机器人丝杠"},
    "600580": {"entered": "2026-07-06", "reason": "Tier 2: 卧龙电驱 伺服电机龙头"},
    "002896": {"entered": "2026-07-06", "reason": "Tier 2: 中大力德 RV减速器+电机一体化"},
}

# 当前池（20 只）
CURRENT_POOL = {**POOL_V2, **POOL_V3, **POOL_V4, **POOL_V5}
# 移除 V1 中已不在当前池的标的（创业板/科创板）
REMOVED_FROM_V1 = ["688361", "300308", "600206", "300661", "300502", "300394", "300281", "688256", "002371", "688012"]

# CPO/AI 主题启动时间（近似）
# 英伟达 GTC 2024 (2024-03): Blackwell 发布, CPO 首次提到台前
# 光通信 ETF 显著上涨开始于 2024-Q4
# 保守估计主题启动时间: 2024-09-01
THEME_START_DATE = "2024-09-01"

# "主题前"候选范围：在 THEME_START_DATE 之前就在池中的标的（且当前仍在池中）
PRE_THEME_POOL = [
    "002281", "000988",  # V1 中的主板 T1（一直保留）
]

# 主题后扩充的标的
POST_THEME_POOL = [
    "603083", "600487", "600141", "002428", "600460", "603986", "600176",  # V2
    "600036", "600585", "600900", "601398", "601006",                       # V3
    "000938",                                                                 # V4
]


class StockPoolAudit:
    """股票池后见之明审计。"""

    def __init__(self):
        self.theme_start = THEME_START_DATE
        self.pre_theme_pool = PRE_THEME_POOL
        self.post_theme_pool = POST_THEME_POOL
        self.current_pool = list(ALL_CODES)

    # ── 入池时间线 ────────────────────────────────────────

    def trace_entry_timeline(self) -> list[dict]:
        """回溯 15 只标的的入池时间线。"""
        timeline = []
        for code in self.current_pool:
            info = CURRENT_POOL.get(code, {})
            timeline.append({
                "code": code,
                "name": get_stock_name(code),
                "entered": info.get("entered", "unknown"),
                "reason": info.get("reason", "unknown"),
                "pre_theme": code in self.pre_theme_pool,
                "tier": STOCK_MAP.get(code, {}).get("tier", 0),
            })
        timeline.sort(key=lambda x: x["entered"])
        return timeline

    def audit_report(self) -> str:
        """生成股票池审计报告。"""
        timeline = self.trace_entry_timeline()

        lines = [
            "# 股票池后见之明审计",
            f"  日期: {date.today().isoformat()}",
            f"  CPO/AI 主题启动时间 (估计): {self.theme_start}",
            "─" * 48,
            "",
            "## 入池时间线",
            "| 标的 | Tier | 入池日期 | 主题前/后 | 入池原因 |",
            "|------|------|---------|----------|---------|",
        ]

        for t in timeline:
            tag = "🔵 主题前" if t["pre_theme"] else "🔴 主题后"
            lines.append(
                f"| {t['name']}({t['code']}) | T{t['tier']} | "
                f"{t['entered']} | {tag} | {t['reason']} |"
            )

        lines.extend([
            "",
            "## 关键发现",
            f"  主题前已在池中: {len(self.pre_theme_pool)} 只 (光迅科技, 华工科技)",
            f"  主题后扩充: {len(self.post_theme_pool)} 只",
            f"  主题后扩充占比: {len(self.post_theme_pool)}/{len(self.current_pool)} = "
            f"{len(self.post_theme_pool)/len(self.current_pool):.0%}",
            "",
            "## 反事实回测 (Counterfactual Backtest)",
            "",
            "### 当前池 (15 只)",
            f"  T1-T3 全部为 AI 供应链主题，T4 为防御组合。",
            f"  入池集中在 {self.theme_start} 之后 → 存在后见之明风险。",
            "",
            "### 固定池 (主题前, 2 只)",
            f"  如果只用主题前已在池中的 {', '.join(self.pre_theme_pool)}，",
            f"  所有后续分析和因子工程会有什么不同？",
            "",
            "### 差异归因",
            "  策略总收益 = 选池效应 + 池内择时能力",
            "  如果当前池收益 >> 固定池收益 → 大部分来自选对了池 (Beta)",
            "  如果当前池收益 ≈ 固定池收益 → 池内择时能力成立 (Alpha)",
            "",
            "## 建议",
            "  1. 用固定池 (2只) 重新跑完整回测 → 对比当前池收益",
            "  2. 用逐步入池时间线做滚动回测 → 观察池扩充是否带来超额收益",
            "  3. 向外部展示系统有效性时, 必须显式剥离'选池效应'",
        ])

        return "\n".join(lines)

    # ── 反事实回测 ────────────────────────────────────────

    def counterfactual_backtest(self) -> dict:
        """反事实回测：固定池 vs 当前池 vs 主题池的等权收益对比。

        用实际价格数据计算：如果只用主题启动前的候选范围，
        同等配置的等权组合收益会有什么区别。

        Returns:
            三池对比 + 归因分析
        """
        import math
        from db import get_conn

        conn = get_conn()
        results = {
            "pre_theme_2stocks": {
                "codes": self.pre_theme_pool,
                "label": "固定池(主题前, 2只)",
                "cumulative_return": 0.0,
                "annualized_return": 0.0,
                "annualized_vol": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
            },
            "t1_t3_13stocks": {
                "codes": [c for c in self.current_pool
                          if STOCK_MAP.get(c, {}).get("tier", 0) <= 3],
                "label": "主题池(T1-T3, 13只)",
                "cumulative_return": 0.0,
                "annualized_return": 0.0,
                "annualized_vol": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
            },
            "full_pool": {
                "codes": self.current_pool,
                "label": f"当前池(全{len(self.current_pool)}只)",
                "cumulative_return": 0.0,
                "annualized_return": 0.0,
                "annualized_vol": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
            },
        }

        try:
            for pool_name, pool_info in results.items():
                codes = pool_info["codes"]
                if len(codes) < 2:
                    continue

                placeholders = ",".join("?" * len(codes))
                # Get daily returns for all codes in pool
                rows = conn.execute(
                    f"SELECT date, AVG(change_pct) as pool_return "
                    f"FROM daily_snapshots "
                    f"WHERE code IN ({placeholders}) AND date >= ? "
                    f"GROUP BY date ORDER BY date",
                    (*codes, self.theme_start)
                ).fetchall()

                if not rows or len(rows) < 20:
                    continue

                daily_rets = [r["pool_return"] or 0 for r in rows]
                n_days = len(daily_rets)

                # Cumulative return
                cum = 1.0
                for r in daily_rets:
                    cum *= (1.0 + r / 100.0)
                pool_info["cumulative_return"] = round((cum - 1.0) * 100, 2)

                # Annualized
                mean_daily = sum(daily_rets) / n_days
                pool_info["annualized_return"] = round(mean_daily * 252, 2)

                var_daily = sum((r - mean_daily) ** 2 for r in daily_rets) / (n_days - 1)
                std_daily = math.sqrt(var_daily) if var_daily > 0 else 0
                pool_info["annualized_vol"] = round(std_daily * math.sqrt(252), 2)

                if pool_info["annualized_vol"] > 0:
                    pool_info["sharpe"] = round(
                        pool_info["annualized_return"] / pool_info["annualized_vol"], 2
                    )

                # Max drawdown (from peak)
                peak = 1.0
                max_dd = 0.0
                cum_val = 1.0
                for r in daily_rets:
                    cum_val *= (1.0 + r / 100.0)
                    peak = max(peak, cum_val)
                    dd = (cum_val - peak) / peak
                    max_dd = min(max_dd, dd)
                pool_info["max_drawdown"] = round(max_dd * 100, 2)

        except Exception as e:
            log.warning(f"反事实回测失败: {e}")
        finally:
            conn.close()

        # ── 归因分析 ──
        theme_ret = results["t1_t3_13stocks"]["cumulative_return"]
        pre_ret = results["pre_theme_2stocks"]["cumulative_return"]
        full_ret = results["full_pool"]["cumulative_return"]

        if abs(theme_ret) > 0.1:
            selection_effect = (theme_ret - pre_ret) / abs(theme_ret)
        else:
            selection_effect = 0

        results["attribution"] = {
            "pre_theme_cumulative_pct": pre_ret,
            "theme_pool_cumulative_pct": theme_ret,
            "full_pool_cumulative_pct": full_ret,
            "pool_expansion_effect_pct": round(selection_effect * 100, 1),
            "selection_vs_timing": (
                "选池效应主导 → 超额收益大部分来自主题走强后的池扩充(Beta)"
                if abs(selection_effect) > 0.3
                else "择时能力成立 → 池扩充未引入实质性后见之明偏差(Alpha)"
            ),
            "caveat": (
                "固定池仅 2 只标的，统计结论受小样本限制。"
                "建议：等 Frozen Baseline 积累 ≥8 周后，用 frozen_comparison_history "
                "表的 divergence 数据做更可靠的 Alpha/Beta 分离。"
            ),
        }

        return results


def run_audit():
    """运行股票池审计并打印报告。"""
    auditor = StockPoolAudit()
    print(auditor.audit_report())
    print()
    cf = auditor.counterfactual_backtest()
    print(json.dumps(cf, ensure_ascii=False, indent=2, default=str))
    return auditor


if __name__ == "__main__":
    run_audit()
