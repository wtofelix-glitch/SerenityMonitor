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

from db import get_conn, get_price_history
from config import ALL_CODES, STOCK_MAP, STOCK_DETAILS
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

# 当前池（ALL_CODES 中的 15 只）
CURRENT_POOL = {**POOL_V2, **POOL_V3, **POOL_V4}
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
                "name": STOCK_MAP.get(code, {}).get("name", code),
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
        """反事实回测：固定池 vs 当前池。

        由于当前系统只有 15 只标的的历史数据,
        这里做一个简化的版本：比较"纯 T1 标的"vs"全池"的收益特征。
        """
        conn = get_conn()
        results = {
            "pre_theme_pool": {"codes": self.pre_theme_pool, "avg_return": 0, "volatility": 0, "sharpe": 0},
            "full_pool": {"codes": self.current_pool, "avg_return": 0, "volatility": 0, "sharpe": 0},
            "theme_stocks": {"codes": [
                c for c in self.current_pool
                if STOCK_MAP.get(c, {}).get("tier", 0) <= 3
            ], "avg_return": 0, "volatility": 0, "sharpe": 0},
        }

        try:
            start = self.theme_start
            for pool_name, pool_info in results.items():
                codes = pool_info["codes"]
                if not codes:
                    continue
                placeholders = ",".join("?" * len(codes))
                rows = conn.execute(
                    f"SELECT code, AVG(change_pct) as avg_ret, "
                    f"  (SELECT AVG(change_pct*change_pct) FROM daily_snapshots ds2 "
                    f"   WHERE ds2.code = ds.code AND ds2.date >= ?) as var "
                    f"FROM daily_snapshots ds "
                    f"WHERE code IN ({placeholders}) AND date >= ? "
                    f"GROUP BY code",
                    (*codes, start, start)
                ).fetchall()

                if rows:
                    returns = [r["avg_ret"] or 0 for r in rows]
                    pool_info["avg_return"] = round(sum(returns) / len(returns), 4)
                    pool_info["volatility"] = round(
                        (sum((r or 0) ** 2 for r in returns) / len(returns)) ** 0.5 * (252 ** 0.5), 4
                    )
                    pool_info["sharpe"] = round(
                        pool_info["avg_return"] * 252 / max(pool_info["volatility"], 0.01), 4
                    )
        except Exception as e:
            log.warning(f"反事实回测数据加载失败: {e}")
        finally:
            conn.close()

        # 归因
        theme_ret = results["theme_stocks"]["avg_return"]
        pre_theme_ret = results["pre_theme_pool"]["avg_return"]
        if abs(theme_ret) > 1e-9:
            selection_effect = (theme_ret - pre_theme_ret) / abs(theme_ret) if theme_ret != 0 else 0
        else:
            selection_effect = 0

        results["attribution"] = {
            "selection_effect_pct": round(selection_effect * 100, 1),
            "interpretation": (
                "选池效应占比较大 → 策略收益中相当一部分来自主题走强后的池扩充"
                if abs(selection_effect) > 0.3 else
                "池内择时能力成立 → 池扩充未引入实质性后见之明偏差"
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
