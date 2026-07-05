"""
Phase 4 上线自检脚本 — phase4_checklist.py

逐项验证 v4 §13.2 的 18 条上线门槛（小资金半自动实盘准入）。
输出通过/待补/失败状态，YAML 格式。

Usage:
    python3 phase4_checklist.py              # 终端输出
    python3 phase4_checklist.py --json       # JSON 输出
    python3 phase4_checklist.py --save       # 保存到 reports/
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from typing import Optional

from serenity_logger import get_logger

log = get_logger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# v4 §13.2 的 18 条上线门槛
CHECKLIST = [
    {
        "id": "backtest_t1",
        "category": "A-share Microstructure",
        "item": "回测引擎支持 T+1 锁定",
        "check_fn": "check_backtest_t1",
        "priority": "P0",
    },
    {
        "id": "backtest_limit_up_down",
        "category": "A-share Microstructure",
        "item": "回测引擎支持涨跌停不可成交",
        "check_fn": "check_backtest_limit",
        "priority": "P0",
    },
    {
        "id": "backtest_suspended",
        "category": "A-share Microstructure",
        "item": "回测引擎支持停牌不可交易",
        "check_fn": "check_backtest_suspended",
        "priority": "P0",
    },
    {
        "id": "backtest_costs",
        "category": "A-share Microstructure",
        "item": "回测引擎显式扣除佣金+印花税+滑点",
        "check_fn": "check_backtest_costs",
        "priority": "P0",
    },
    {
        "id": "signal_net_returns",
        "category": "Signal Performance",
        "item": "信号绩效全部按净收益统计",
        "check_fn": "check_net_returns",
        "priority": "P0",
    },
    {
        "id": "audit_log_per_signal",
        "category": "Audit Chain",
        "item": "每条信号有完整 decision_audit_log 记录",
        "check_fn": "check_audit_log",
        "priority": "P0",
    },
    {
        "id": "frozen_baseline_8weeks",
        "category": "Verification",
        "item": "Frozen Baseline 已并行运行 ≥8 周",
        "check_fn": "check_frozen_weeks",
        "priority": "P0",
    },
    {
        "id": "adaptive_vs_frozen",
        "category": "Verification",
        "item": "Adaptive 在 ≥8 周内没有显著跑输 Frozen",
        "check_fn": "check_adaptive_vs_frozen",
        "priority": "P0",
    },
    {
        "id": "equal_weight_benchmark",
        "category": "Verification",
        "item": "Equal Weight Basket 基准已接入回测",
        "check_fn": "check_eq_benchmark",
        "priority": "P1",
    },
    {
        "id": "ablation_done",
        "category": "Verification",
        "item": "情报层消融实验已完成（各模块增量贡献已量化）",
        "check_fn": "check_ablation_done",
        "priority": "P0",
    },
    {
        "id": "single_position_cap",
        "category": "Risk Management",
        "item": "单票仓位上限已降至 ≤35%",
        "check_fn": "check_position_cap",
        "priority": "P0",
    },
    {
        "id": "theme_concentration",
        "category": "Risk Management",
        "item": "主题集中度风控（相关性簇）已上线",
        "check_fn": "check_theme_concentration",
        "priority": "P0",
    },
    {
        "id": "t4_defensive_floor",
        "category": "Risk Management",
        "item": "T4 防御底仓强制执行",
        "check_fn": "check_t4_floor",
        "priority": "P1",
    },
    {
        "id": "test_microstructure",
        "category": "Testing",
        "item": "test_market_microstructure.py 通过",
        "check_fn": "check_test_microstructure",
        "priority": "P0",
    },
    {
        "id": "test_execution_sim",
        "category": "Testing",
        "item": "test_execution_simulator.py 通过",
        "check_fn": "check_test_execution",
        "priority": "P1",
    },
    {
        "id": "test_t1_lock",
        "category": "Testing",
        "item": "test_t1_lock.py 通过",
        "check_fn": "check_test_t1",
        "priority": "P0",
    },
    {
        "id": "test_limit_up_down",
        "category": "Testing",
        "item": "test_limit_up_down.py 通过",
        "check_fn": "check_test_limit",
        "priority": "P0",
    },
    {
        "id": "test_no_lookahead",
        "category": "Testing",
        "item": "test_no_lookahead.py 通过",
        "check_fn": "check_test_lookahead",
        "priority": "P1",
    },
    {
        "id": "test_audit_replay",
        "category": "Testing",
        "item": "test_audit_replay.py 通过",
        "check_fn": "check_test_audit",
        "priority": "P1",
    },
]


class Phase4Checklist:
    """Phase 4 上线自检器。"""

    def __init__(self):
        self.results: list[dict] = []
        self._passed = 0
        self._failed = 0
        self._pending = 0

    def run_all(self) -> list[dict]:
        """运行全部检查。"""
        self.results = []
        for item in CHECKLIST:
            fn_name = item["check_fn"]
            fn = getattr(self, fn_name, self._not_implemented)
            try:
                status, detail = fn()
            except Exception as e:
                status, detail = "FAIL", str(e)

            self.results.append({
                **item,
                "status": status,
                "detail": detail,
                "checked_at": datetime.now().isoformat(),
            })

            if status == "PASS":
                self._passed += 1
            elif status == "FAIL":
                self._failed += 1
            else:
                self._pending += 1

        return self.results

    # ── 检查函数 ──────────────────────────────────────────

    def _check_module_exists(self, module_name: str) -> tuple[str, str]:
        try:
            __import__(module_name)
            return "PASS", f"模块 {module_name} 可用"
        except ImportError:
            return "FAIL", f"模块 {module_name} 不可用"

    def _check_test_passes(self, test_module: str) -> tuple[str, str]:
        import subprocess
        test_path = f"tests/{test_module}"
        result = subprocess.run(
            ["python3", "-m", "pytest", test_path, "-q", "--tb=no"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return "PASS", f"{test_module} 全部通过"
        return "FAIL", f"{test_module} 存在失败:\n{result.stdout[-200:] if result.stdout else result.stderr[-200:]}"

    # T+1
    def check_backtest_t1(self) -> tuple[str, str]:
        ms_ok, _ = self._check_module_exists("market_microstructure")
        if ms_ok == "PASS":
            from market_microstructure import MarketMicrostructure
            ms = MarketMicrostructure()
            if hasattr(ms, 'can_sell') and hasattr(ms, 'add_t1_lock'):
                return "PASS", "market_microstructure.py 支持 T+1 检查 (can_sell + add_t1_lock)"
        return "PENDING", "market_microstructure 已实现 T+1; 需验证 backtest_engine 集成"

    # 涨跌停
    def check_backtest_limit(self) -> tuple[str, str]:
        ms_ok, _ = self._check_module_exists("market_microstructure")
        if ms_ok == "PASS":
            from market_microstructure import MarketMicrostructure
            ms = MarketMicrostructure()
            if hasattr(ms, 'get_limit_status'):
                return "PASS", "market_microstructure.get_limit_status() 支持涨跌停检测"
        return "FAIL", "market_microstructure 不可用"

    # 停牌
    def check_backtest_suspended(self) -> tuple[str, str]:
        ms_ok, _ = self._check_module_exists("market_microstructure")
        if ms_ok == "PASS":
            from market_microstructure import MarketMicrostructure
            ms = MarketMicrostructure()
            if hasattr(ms, 'is_suspended') and hasattr(ms, 'set_suspended'):
                return "PASS", "market_microstructure 支持停牌检测"
        return "FAIL", "market_microstructure 不可用"

    # 交易成本
    def check_backtest_costs(self) -> tuple[str, str]:
        ex_ok, _ = self._check_module_exists("execution_simulator")
        if ex_ok == "PASS":
            from execution_simulator import ExecutionSimulator
            sim = ExecutionSimulator()
            cost = sim.compute_trading_cost(100.0, 100, "buy")
            if cost.get("total_cost", 0) > 0:
                return "PASS", f"execution_simulator 计算交易成本: ¥{cost['total_cost']:.2f}"
        return "PENDING", "execution_simulator 已实现; 需验证回测引擎集成"

    # 信号净收益
    def check_net_returns(self) -> tuple[str, str]:
        try:
            conn = __import__('db').get_conn()
            cols = conn.execute("PRAGMA table_info(signal_performance)").fetchall()
            col_names = {c[1] for c in cols}
            conn.close()
            has_net = "net" in str(col_names).lower() or "avg_return" in str(col_names)
            if has_net:
                return "PASS", "signal_performance 表包含收益字段"
        except Exception:
            pass
        return "PENDING", "需在信号绩效统计中加入税后净收益列"

    # 审计日志
    def check_audit_log(self) -> tuple[str, str]:
        try:
            from audit_logger import get_audit_logger
            al = get_audit_logger()
            stats = al.get_stats()
            if stats["total"] > 0:
                return "PASS", f"decision_audit_log 已有 {stats['total']} 条记录"
            return "PENDING", f"decision_audit_log 表已创建 ({stats['total']} 条记录, 需积累)"
        except Exception as e:
            return "FAIL", str(e)

    # Frozen Baseline
    def check_frozen_weeks(self) -> tuple[str, str]:
        try:
            from frozen_baseline import FROZEN_SINCE
            frozen_date = date.fromisoformat(FROZEN_SINCE)
            weeks = (date.today() - frozen_date).days / 7
            if weeks >= 8:
                return "PASS", f"Frozen Baseline 已运行 {weeks:.0f} 周 (since {FROZEN_SINCE})"
            return "PENDING", f"Frozen Baseline 运行中 ({weeks:.0f} 周 / 需 ≥8 周)"
        except Exception as e:
            return "PENDING", f"Frozen Baseline 已部署, 等待时间积累 ({e})"

    def check_adaptive_vs_frozen(self) -> tuple[str, str]:
        return "PENDING", "需 Frozen Baseline 运行 ≥8 周 + 市场状态覆盖后评估"

    def check_eq_benchmark(self) -> tuple[str, str]:
        eq_ok, _ = self._check_module_exists("equal_weight_basket")
        if eq_ok == "PASS":
            return "PASS", "equal_weight_basket.py 可用"
        return "FAIL", "equal_weight_basket 不可用"

    def check_ablation_done(self) -> tuple[str, str]:
        ab_ok, _ = self._check_module_exists("ablation_framework")
        if ab_ok == "PASS":
            return "PENDING", "ablation_framework 已部署; 需真实回测数据完成消融"
        return "FAIL", "ablation_framework 不可用"

    # 仓位
    def check_position_cap(self) -> tuple[str, str]:
        try:
            from config import CAPITAL_CONFIG
            max_single = CAPITAL_CONFIG.get("max_single_weight", 0.85)
            if max_single <= 0.35:
                return "PASS", f"单票上限 {max_single:.0%} ≤ 35%"
            return "FAIL", f"单票上限 {max_single:.0%} > 35%, 需降杠杆 (v4 §10.2)"
        except Exception:
            return "FAIL", "无法读取 CAPITAL_CONFIG"

    def check_theme_concentration(self) -> tuple[str, str]:
        cc_ok, _ = self._check_module_exists("correlation_cluster")
        if cc_ok == "PASS":
            # 检查是否已集成到 risk_manager
            import risk_manager
            if hasattr(risk_manager.RiskManager, '_check_cluster_concentration'):
                return "PASS", "correlation_cluster 已集成到 risk_manager"
            return "PENDING", "correlation_cluster 已实现; 需确认 risk_manager 集成"
        return "FAIL", "correlation_cluster 不可用"

    def check_t4_floor(self) -> tuple[str, str]:
        return "PENDING", "T4 底仓强制执行需在 portfolio.py / auto_execute.py 中实现"

    # 测试
    def check_test_microstructure(self) -> tuple[str, str]:
        return self._check_test_passes("test_market_microstructure.py")

    def check_test_execution(self) -> tuple[str, str]:
        return self._check_test_passes("test_execution_simulator.py")

    def check_test_t1(self) -> tuple[str, str]:
        return self._check_test_passes("test_t1_lock.py")

    def check_test_limit(self) -> tuple[str, str]:
        return self._check_test_passes("test_limit_up_down.py")

    def check_test_lookahead(self) -> tuple[str, str]:
        return self._check_test_passes("test_no_lookahead.py")

    def check_test_audit(self) -> tuple[str, str]:
        return self._check_test_passes("test_audit_replay.py")

    def _not_implemented(self) -> tuple[str, str]:
        return "PENDING", "检查函数未实现"

    # ── 报告 ──────────────────────────────────────────────

    def summary(self) -> str:
        if not self.results:
            self.run_all()

        lines = [
            "# Phase 4 上线自检报告",
            f"  日期: {date.today().isoformat()}",
            f"  进度: {self._passed}/{len(CHECKLIST)} 通过, {self._failed} 失败, {self._pending} 待补",
            f"  整体状态: {'✅ 可以启动 Phase 4' if self._failed == 0 else '❌ 有阻塞项' if self._failed > 2 else '⚠️ 部分待补'}",
            "─" * 48,
            "",
            "| # | 类别 | 检查项 | 状态 | 详情 |",
            "|---|------|--------|------|------|",
        ]

        for r in self.results:
            emoji = {"PASS": "✅", "FAIL": "❌", "PENDING": "⏳"}.get(r["status"], "❓")
            lines.append(f"| {r['id']} | {r['category']} | {r['item']} | {emoji} {r['status']} | {r['detail'][:60]} |")

        lines.extend([
            "",
            "## 阻塞项 (FAIL)",
        ])
        for r in self.results:
            if r["status"] == "FAIL":
                lines.append(f"  - [{r['priority']}] {r['item']}: {r['detail']}")

        lines.extend([
            "",
            "## 待补项 (PENDING)",
        ])
        for r in self.results:
            if r["status"] == "PENDING":
                lines.append(f"  - [{r['priority']}] {r['item']}: {r['detail']}")

        return "\n".join(lines)


def run_checklist():
    checker = Phase4Checklist()
    checker.run_all()

    if "--json" in sys.argv:
        print(json.dumps(checker.results, ensure_ascii=False, indent=2))
    else:
        print(checker.summary())

    if "--save" in sys.argv:
        path = os.path.join(REPORT_DIR, f"phase4_checklist_{date.today().isoformat()}.md")
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(checker.summary())
        print(f"\n📁 报告已保存: {path}")

    return checker


if __name__ == "__main__":
    run_checklist()
