"""
标的扩展筛选模块 — 评分排名、池内筛选、今日重点关注
不新增标的到 STOCK_MAP，仅输出建议供用户手动决策。
"""
from datetime import date
from typing import Optional

from config import STOCK_MAP, ALL_CODES, STOCK_DETAILS
from scorer import score_all
from data_engine import get_all_today_snapshots
from db import get_latest_scores
from signal_engine import _factor_engine, _fund_engine

# ─────────────────────────────────────────────────────────────
# 投资资格约束
# ─────────────────────────────────────────────────────────────
# 无科创板(688)和创业板(300/301)投资资格，仅限主板
# 主板代码前缀: 000, 002, 600, 601, 603, 605
MAINBOARD_PREFIXES = ("000", "002", "600", "601", "603", "605")


def _is_mainboard(code: str) -> bool:
    """检查是否为可交易的主板标的"""
    return any(code.startswith(p) for p in MAINBOARD_PREFIXES)


def _validate_codes(codes: list[str]) -> list[str]:
    """仅保留主板标的"""
    return [c for c in codes if c in STOCK_MAP and _is_mainboard(c)]


# ─────────────────────────────────────────────────────────────
# 1. scan_candidates — 评分排名（含明细）
# ─────────────────────────────────────────────────────────────

def scan_candidates() -> list[dict]:
    """
    对 config.STOCK_MAP 中所有标的执行评分排名。

    Returns
    -------
    list[dict]
        每个元素含:
          code, name, rank, total_score,
          base_score, zone_score, momentum_score, volume_score,
          serenity_score, factor_score, technical_score,
          close, change_pct, zone_label, signal_action,
          tier
    """
    results = score_all()
    # 过滤只保留主板标的（score_all 已经只处理 STOCK_MAP 中的标的）
    validated = [r for r in results if _is_mainboard(r["code"])]
    # 重排序（score_all 已排序，但过滤后重新编号）
    for i, r in enumerate(validated):
        r["rank"] = i + 1
    return validated


# ─────────────────────────────────────────────────────────────
# 2. expand_pool — 扩展监控池（从现有 9 只中筛选）
# ─────────────────────────────────────────────────────────────

def expand_pool(
    min_score: float = 0,
    max_count: int = 9,
    tiers: Optional[list[int]] = None,
    min_signal_strength: Optional[float] = None,
) -> list[dict]:
    """
    从 config 现有 9 只标的池中按条件筛选推荐关注列表。
    不新增外部标的。

    Parameters
    ----------
    min_score : float
        最低总分门槛 (默认 0 = 不限制)
    max_count : int
        最多返回数量 (默认 9 = 全部)
    tiers : list[int] | None
        仅保留指定 tier 层级，如 [1, 2]
    min_signal_strength : float | None
        最低 Alpha 因子综合信号强度

    Returns
    -------
    list[dict]
        筛选后的评分列表，按 total_score 降序
    """
    candidates = scan_candidates()

    # tier 过滤
    if tiers:
        candidates = [
            r for r in candidates
            if STOCK_MAP.get(r["code"], {}).get("tier") in tiers
        ]

    # 最低总分
    if min_score > 0:
        candidates = [r for r in candidates if r["total_score"] >= min_score]

    # 信号强度过滤 (使用因子引擎的信号)
    if min_signal_strength is not None:
        filtered = []
        for r in candidates:
            try:
                factors = _factor_engine.compute_all_factors(r["code"])
                signals = factors.get("signals", {})
                vals = [float(v) for v in signals.values() if v is not None]
                avg_signal = sum(vals) / len(vals) if vals else 0.0
                if avg_signal >= min_signal_strength:
                    filtered.append(r)
            except Exception:
                continue
        candidates = filtered

    # 限制数量
    candidates = candidates[:max_count]

    # 重排 rank
    for i, r in enumerate(candidates):
        r["rank"] = i + 1

    return candidates


# ─────────────────────────────────────────────────────────────
# 3. suggest_stock_to_watch — 今日重点关注推荐
# ─────────────────────────────────────────────────────────────

def suggest_stock_to_watch(
    top_n_score: int = 3,
    top_n_signal: int = 3,
    top_n_fundamental: int = 3,
) -> dict:
    """
    从 STOCK_MAP 现有池中生成"今日重点关注"推荐列表。

    三个维度各选出 TOP N（可能有重叠）:
      1. 综合评分 TOP N
      2. Alpha 因子信号强度 TOP N
      3. 基本面信号 TOP N

    Returns
    -------
    dict
        {
            "date": "2026-06-03",
            "score_top":  [{code, name, total_score, ...}],
            "signal_top":  [{code, name, signal_strength, ...}],
            "fundamental_top": [{code, name, fundamental_signal, ...}],
            "unified_watch": [code1, code2, ...],  # 去重合并
        }
    """
    today = date.today().isoformat()
    codes = _validate_codes(ALL_CODES)

    # ── 维度1: 综合评分 TOP ──
    scores = scan_candidates()
    score_top = scores[:top_n_score]

    # ── 维度2: Alpha 因子信号强度 TOP ──
    signal_list = []
    for code in codes:
        try:
            factors = _factor_engine.compute_all_factors(code)
            signals = factors.get("signals", {})
            vals = [float(v) for v in signals.values() if v is not None]
            strength = sum(vals) / len(vals) if vals else 0.0
        except Exception:
            strength = 0.0
        signal_list.append({
            "code": code,
            "name": STOCK_MAP.get(code, {}).get("name", code),
            "signal_strength": round(strength, 4),
        })
    signal_list.sort(key=lambda x: x["signal_strength"], reverse=True)
    signal_top = signal_list[:top_n_signal]

    # ── 维度3: 基本面信号 TOP ──
    fund_list = []
    for code in codes:
        try:
            fund_sig = _fund_engine.get_fundamental_signal(code)
        except Exception:
            fund_sig = None
        fund_list.append({
            "code": code,
            "name": STOCK_MAP.get(code, {}).get("name", code),
            "fundamental_signal": fund_sig,
        })
    fund_list = [f for f in fund_list if f["fundamental_signal"] is not None]
    fund_list.sort(key=lambda x: x["fundamental_signal"], reverse=True)
    fundamental_top = fund_list[:top_n_fundamental]

    # ── 合并去重 ──
    unified = []
    seen = set()
    for lst in (score_top, signal_top, fundamental_top):
        for item in lst:
            if item["code"] not in seen:
                seen.add(item["code"])
                unified.append(item["code"])

    return {
        "date": today,
        "score_top": score_top,
        "signal_top": signal_top,
        "fundamental_top": fundamental_top,
        "unified_watch": unified,
    }


# ─────────────────────────────────────────────────────────────
# 格式化输出
# ─────────────────────────────────────────────────────────────

def format_candidate_list(candidates: list[dict]) -> str:
    """将候选列表格式化为终端可读文本"""
    if not candidates:
        return "📭 无符合条件的候选标的"

    lines = [
        f"📊 候选标的评分排名 | {date.today()}",
        "=" * 70,
        f"{'#':>3} {'名称':<8} {'总分':>6} {'基':>4} {'位':>4} {'动':>4} {'量':>4} {'信':>6} {'信号':<10} {'Tier':<5}",
        "─" * 70,
    ]
    for r in candidates:
        tier = STOCK_MAP.get(r["code"], {}).get("tier", "-")
        signal_icon = {
            "STRONG_BUY": "🟢🟢🟢", "BUY": "🟢🟢", "CAUTION_BUY": "🟢",
            "HOLD": "⚪", "WATCH": "🟡", "SELL": "🔴🔴", "STOP_LOSS": "🔴🔴🔴",
        }.get(r.get("signal_action", ""), "⚪")
        lines.append(
            f"{r['rank']:>3} {r['name']:<8} "
            f"{r['total_score']:>6.1f} "
            f"{r.get('base_score', 0):>4.0f} "
            f"{r.get('zone_score', 0):>4.0f} "
            f"{r.get('momentum_score', 0):>4.0f} "
            f"{r.get('volume_score', 0):>4.0f} "
            f"{signal_icon:<6} "
            f"{r.get('signal_action', '-'):<10} "
            f"{tier:<5}"
        )
    lines.append("=" * 70)
    return "\n".join(lines)


def format_watch_suggestion(suggestion: dict) -> str:
    """将重点关注建议格式化为终端可读文本"""
    lines = [
        f"🔍 今日重点关注推荐 | {suggestion['date']}",
        "=" * 70,
    ]

    # 评分 TOP
    lines.append("")
    lines.append("📈 综合评分 TOP3:")
    for i, s in enumerate(suggestion["score_top"], 1):
        lines.append(f"  {i}. {s['name']}({s['code']}) — 总分 {s['total_score']:.1f}")

    # 信号强度 TOP
    lines.append("")
    lines.append("⚡ Alpha因子信号强度 TOP3:")
    for i, s in enumerate(suggestion["signal_top"], 1):
        lines.append(f"  {i}. {s['name']}({s['code']}) — 强度 {s['signal_strength']:+.4f}")

    # 基本面 TOP
    lines.append("")
    lines.append("📊 基本面信号 TOP3:")
    for i, s in enumerate(suggestion["fundamental_top"], 1):
        sig = s["fundamental_signal"]
        sig_str = f"{sig:+.4f}" if sig is not None else "N/A"
        lines.append(f"  {i}. {s['name']}({s['code']}) — 信号 {sig_str}")

    # 合并推荐
    lines.append("")
    lines.append("🎯 今日重点关注（去重合并）:")
    for i, code in enumerate(suggestion["unified_watch"], 1):
        name = STOCK_MAP.get(code, {}).get("name", code)
        lines.append(f"  {i}. {name}({code})")

    lines.append("")
    lines.append("=" * 70)
    lines.append("💡 以上为算法建议，最终由用户手动决定是否加入监控。")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────

def cmd_scan_candidates():
    """CLI 命令: python3 cli.py scan-candidates"""
    print(format_candidate_list(scan_candidates()))


def cmd_suggest_stock():
    """CLI 命令: python3 cli.py suggest-stock"""
    suggestion = suggest_stock_to_watch()
    print(format_watch_suggestion(suggestion))


if __name__ == "__main__":
    cmd_scan_candidates()
    print()
    cmd_suggest_stock()
