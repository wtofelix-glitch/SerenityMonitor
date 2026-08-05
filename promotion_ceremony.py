"""
模块晋级仪式 — promotion_ceremony.py

Phase 5a: 证据门控的模块解冻流程。将研究实验层模块晋级至交易内核层
的唯一入口。

流程（v4 §2.5 四关全部通过）：
  1. 消融实验通过（Bonferroni α/n）
  2. 样本外验证通过
  3. Frozen Baseline 对照通过（≥8 周）
  4. 实盘半自动验证通过

Usage:
    python3 promotion_ceremony.py --check weight_adjuster   # 检查晋级条件
    python3 promotion_ceremony.py --promote moat            # 发起晋级
    python3 promotion_ceremony.py --list                    # 列出所有模块状态
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from kernel_freeze import (
    is_frozen, freeze_reason, frozen_behavior,
    unfreeze_conditions, all_frozen_ids, FROZEN_MANIFEST
)
from serenity_logger import get_logger

log = get_logger(__name__)

PROMOTION_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".promotion_log.json"
)


# ═══════════════════════════════════════════════════════════════
# 晋级证据收集
# ═══════════════════════════════════════════════════════════════


class PromotionCeremony:
    """模块晋级仪式。"""

    def __init__(self):
        self._log = self._load_log()

    def check_module(self, module_id: str) -> dict:
        """检查指定模块的晋级条件满足情况。

        Returns:
            {module_id, frozen, conditions: [{name, met, evidence, verdict}], overall: PASS/FAIL}
        """
        if module_id not in FROZEN_MANIFEST:
            return {
                "module_id": module_id,
                "error": f"模块 '{module_id}' 不在 FROZEN_MANIFEST 中",
                "overall": "UNKNOWN",
            }

        entry = FROZEN_MANIFEST[module_id]
        conditions = entry.get("unfreeze_conditions", [])
        checks = []

        for cond in conditions:
            check = self._evaluate_condition(module_id, cond)
            checks.append(check)

        all_met = all(c["met"] for c in checks)

        return {
            "module_id": module_id,
            "frozen": entry.get("frozen", True),
            "frozen_since": entry.get("frozen_since", ""),
            "description": entry.get("description", ""),
            "conditions": checks,
            "overall": "READY" if all_met else "NOT_READY",
        }

    def try_promote(self, module_id: str, evidence: Optional[dict] = None) -> dict:
        """尝试晋级模块。所有条件满足时自动调用 _unfreeze。

        必须显式传入证据（消融结果、Frozen 对比数据等），
        不可仅凭时间流逝通过。
        """
        check = self.check_module(module_id)
        if check.get("error"):
            return {"success": False, "error": check["error"]}

        if check["overall"] != "READY":
            unmet = [c["condition"][:60] for c in check["conditions"] if not c["met"]]
            return {
                "success": False,
                "module_id": module_id,
                "reason": f"{len(unmet)} 项未满足: {'; '.join(unmet)}",
                "check": check,
            }

        # ── 晋级！ ──
        try:
            from kernel_freeze import _unfreeze as _do_unfreeze
            reason = evidence.get("reason", "") if evidence else ""
            _do_unfreeze(module_id, reason)
        except Exception as e:
            return {
                "success": False,
                "module_id": module_id,
                "error": f"_unfreeze 调用失败: {e}",
            }

        # 记录晋级
        self._log["promotions"].append({
            "module_id": module_id,
            "promoted_at": datetime.now().isoformat(),
            "evidence": evidence or {},
            "conditions_check": check,
        })
        self._save_log()

        log.warning(f"🎉 模块晋级: {module_id} 已从冻结中解除!")
        log.warning(f"   描述: {freeze_reason(module_id)}")
        log.warning(f"   解冻后行为: {frozen_behavior(module_id)}")

        return {
            "success": True,
            "module_id": module_id,
            "new_status": "UNFROZEN",
            "check": check,
        }

    def list_all(self) -> list[dict]:
        """列出所有模块的冻结/晋级状态。"""
        results = []
        for mid in FROZEN_MANIFEST:
            entry = FROZEN_MANIFEST[mid]
            results.append({
                "module_id": mid,
                "frozen": entry.get("frozen", True),
                "type": entry.get("freeze_type", "unknown"),
                "description": entry.get("description", "")[:60],
                "frozen_since": entry.get("frozen_since", ""),
                "unfrozen_at": entry.get("unfrozen_at", ""),
            })
        return results

    # ── 内部条件评估 ──────────────────────────────────────

    def _evaluate_condition(self, module_id: str, condition: str) -> dict:
        """评估单条晋级条件。基于可用数据自动判断。"""
        result = {"condition": condition, "met": False, "evidence": "", "verdict": "NEEDS_EVIDENCE"}

        # ── Frozen Baseline 对照 ──
        if "Frozen Baseline 对照" in condition and "8" in condition:
            met, evidence = self._check_frozen_weeks(min_weeks=8)
            result["met"] = met
            result["evidence"] = evidence
            result["verdict"] = "PASS" if met else f"WAITING ({evidence})"
            return result

        # ── 消融实验 ──
        if "消融实验" in condition and "Bonferroni" in condition:
            met, evidence = self._check_ablation(module_id)
            result["met"] = met
            result["evidence"] = evidence
            result["verdict"] = "PASS" if met else "NOT_RUN"
            return result

        # ── 样本外验证 ──
        if "样本外" in condition or "验证集" in condition:
            met, evidence = self._check_sample_out()
            result["met"] = met
            result["evidence"] = evidence
            result["verdict"] = "PASS" if met else f"WAITING ({evidence})"
            return result

        # ── 实盘半自动验证 ──
        if "实盘半自动" in condition or "信号质量" in condition:
            met, evidence = self._check_semi_auto_signal_quality()
            result["met"] = met
            result["evidence"] = evidence
            result["verdict"] = "PASS" if met else f"WAITING ({evidence})"
            return result

        return result

    def _check_frozen_weeks(self, min_weeks: int = 8) -> tuple[bool, str]:
        """检查 Frozen Baseline 是否已运行 ≥ N 周。"""
        try:
            from frozen_baseline import FROZEN_SINCE
            frozen_date = date.fromisoformat(FROZEN_SINCE)
            weeks = (date.today() - frozen_date).days / 7
            if weeks >= min_weeks:
                # 进一步检查：是否有数据日
                from db import get_conn
                conn = get_conn()
                row = conn.execute(
                    "SELECT COUNT(DISTINCT date) FROM frozen_comparison_history"
                ).fetchone()
                conn.close()
                nd = row[0] if row else 0
                if nd >= min_weeks * 3:  # 至少每周 3 天有数据
                    return True, f"Frozen Baseline 运行 {weeks:.0f} 周, {nd} 个数据日 ≥ {min_weeks} 周"
                return False, f"Frozen Baseline {weeks:.0f} 日历周, 但仅 {nd} 个数据日"
            return False, f"Frozen Baseline 仅 {weeks:.0f} 周 / 需 ≥{min_weeks} 周"
        except Exception as e:
            return False, str(e)

    def _check_ablation(self, module_id: str) -> tuple[bool, str]:
        """检查消融实验是否通过。"""
        try:
            # 检查 ablation_framework 是否有该模块的结果
            from ablation_framework import ABLATION_MODULES, BONFERRONI_ALPHA

            # 找到模块的消融映射
            module_ablation_map = {
                "weight_adjuster": "",
                "signal_thresholds": "",
                "sentinel_weights": "sentinel",
                "conviction": "conviction",
                "llm_sentiment": "llm_sentiment",
                "serenity_council": "council",
                "debate_engine": "",
                "ensemble_voting": "",
                "market_sense_regime_shifts": "market_sense",
            }

            ab_id = module_ablation_map.get(module_id, "")
            if not ab_id:
                return False, f"模块 {module_id} 未映射到消融实验变体"

            # 查找 ablation 结果
            # (真实场景中从 ablation_framework 结果表中读取)
            return False, (
                f"消融实验需要在 Frozen Baseline ≥8 周后，"
                f"用 EventDrivenBacktest 运行 7 个变体。"
                f"当前仅可用 simple_backtest_for_ablation (占位)。"
                f"Bonferroni α={BONFERRONI_ALPHA:.5f}"
            )
        except Exception as e:
            return False, str(e)

    def _check_sample_out(self) -> tuple[bool, str]:
        """检查样本外验证。"""
        return False, "需要独立验证集 + 至少 30 条信号在样本外评估"

    def _check_semi_auto_signal_quality(self) -> tuple[bool, str]:
        """检查实盘半自动信号质量。"""
        try:
            from db import get_conn
            conn = get_conn()
            # 检查 human_override 率
            row = conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN human_override=1 THEN 1 ELSE 0 END) as overridden "
                "FROM decision_audit_log "
                "WHERE created_at >= date('now', '-30 days')"
            ).fetchone()
            conn.close()

            total = row[0] if row else 0
            overrides = row[1] if row else 0

            if total < 30:
                return False, f"仅 {total} 条决策样本 / 需 ≥30 条"

            override_rate = overrides / max(total, 1)
            if override_rate > 0.30:
                return False, f"人工干预率 {override_rate:.0%} > 30%"

            return True, f"{total} 条样本, 人工干预率 {override_rate:.0%}"
        except Exception as e:
            return False, str(e)

    # ── 日志 ──────────────────────────────────────────────

    def _load_log(self) -> dict:
        if os.path.exists(PROMOTION_LOG):
            try:
                with open(PROMOTION_LOG) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"promotions": [], "checks": []}

    def _save_log(self) -> None:
        try:
            with open(PROMOTION_LOG, "w") as f:
                json.dump(self._log, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    ceremony = PromotionCeremony()

    if "--list" in sys.argv:
        modules = ceremony.list_all()
        print(f"{'模块':<30} {'状态':<10} {'类型':<12} {'冻结日期':<12}")
        print("─" * 70)
        for m in modules:
            status = "🔒 冻结" if m["frozen"] else "🔓 解冻"
            unfrozen_at = m.get("unfrozen_at", "") or ""
            label = f"{status} {unfrozen_at}" if unfrozen_at else status
            print(f"{m['module_id']:<30} {label:<16} {m['type']:<12} {m.get('frozen_since', '')}")
        return

    if "--check" in sys.argv:
        idx = sys.argv.index("--check")
        if idx + 1 < len(sys.argv):
            module_id = sys.argv[idx + 1]
        else:
            print("用法: python3 promotion_ceremony.py --check <module_id>")
            return
        check = ceremony.check_module(module_id)
        print(f"\n模块晋级检查: {module_id}")
        print(f"  当前: {'🔒 冻结' if check.get('frozen') else '🔓 解冻'}")
        print(f"  总体: {check.get('overall', '?')}")
        print(f"\n条件检查:")
        for i, cond in enumerate(check.get("conditions", [])):
            icon = "✅" if cond["met"] else "❌"
            print(f"  {icon} {cond['condition']}")
            print(f"     → {cond['verdict']}: {cond['evidence']}")
        return

    if "--promote" in sys.argv:
        idx = sys.argv.index("--promote")
        if idx + 1 < len(sys.argv):
            module_id = sys.argv[idx + 1]
        else:
            print("用法: python3 promotion_ceremony.py --promote <module_id>")
            return
        result = ceremony.try_promote(module_id)
        if result["success"]:
            print(f"\n🎉 {module_id} 晋级成功！已从冻结清单中解除。")
        else:
            print(f"\n❌ {module_id} 晋级失败: {result.get('reason', '')}")
        return

    # 默认
    print("用法:")
    print("  python3 promotion_ceremony.py --list            列出所有模块")
    print("  python3 promotion_ceremony.py --check <module>  检查晋级条件")
    print("  python3 promotion_ceremony.py --promote <module> 发起晋级")


if __name__ == "__main__":
    main()
