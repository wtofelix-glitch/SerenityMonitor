"""UZI 产业链卡位分析 — 定性信息面板 (v3.0)

v3.0 重新定位：从评分因子降级为纯信息模块。
- 不再贡献 total_score（评分已 8 维精简）
- 保留 AI 产业链关键词匹配 + 证据等级 + 陷阱检测
- 为监控看板和日报提供定性参考

用途：
  看板: get_uzi_chain_dashboard(code) → 卡位/证据/陷阱三合一面板
  日报: evaluate_uzi_insight(code) → 完整分析（计入 details 供查询）
"""
from __future__ import annotations

from typing import Any

from config import STOCK_DETAILS, STOCK_MAP


AI_CHAIN_KEYWORDS = (
    "光模块", "光芯片", "cpo", "光引擎", "硅光", "光通信", "光器件", "激光器",
    "hbm", "cowos", "先进封装", "封装基板", "abf", "载板",
    "inp", "磷化铟", "砷化镓", "化合物半导体", "衬底", "外延",
    "pcb", "电子布", "玻纤", "基材", "高速铜", "铜连接", "连接器", "液冷", "散热", "电源",
    "交换机", "算力", "ai芯片", "ai 芯片", "asic", "gpu", "存储",
    "ai服务器", "ai 服务器", "数据中心", "光纤", "空芯光纤",
    "光学", "光波导", "衍射光波导", "micro-led", "硅基oled", "车载光学",
    "人形机器人", "具身智能", "谐波减速器", "rv减速器", "行星滚柱丝杠",
    "灵巧手", "空心杯电机", "六维力", "力传感器", "触觉传感器",
)

SUPPLY_CHAIN_TIERS = (
    ("材料耗材", 1.00, ("inp", "磷化铟", "砷化镓", "化合物半导体", "衬底", "外延", "abf", "载板", "空芯光纤", "靶材", "电子特气")),
    ("制程/封装", 0.92, ("cowos", "先进封装", "硅光", "键合")),
    ("设备/测试", 0.85, ("光刻", "刻蚀", "量测", "测试机", "设备")),
    ("芯片/器件", 0.78, ("光芯片", "光器件", "激光器", "hbm", "asic", "gpu", "谐波减速器", "rv减速器", "行星滚柱丝杠", "力传感器")),
    ("基础设施", 0.70, ("数据中心", "算力", "电网", "变压器", "光纤", "光缆")),
    ("模块/子系统", 0.62, ("光模块", "光引擎", "连接器", "电源", "液冷", "散热", "灵巧手", "执行器", "pcb", "电子布", "玻纤", "基材")),
    ("系统集成", 0.50, ("交换机", "服务器", "机械臂", "整机")),
    ("下游需求", 0.40, ("人形机器人", "机器人", "ar眼镜", "ar 眼镜", "头显", "近眼显示")),
)

HARD_EVIDENCE_KEYWORDS = (
    "认证", "定点", "量产", "订单", "中标", "专利", "长协", "在手",
    "通过验证", "合格供应商", "独供", "送样", "小批量", "批量交付",
)
MEDIUM_EVIDENCE_KEYWORDS = (
    "研报", "政策", "协会", "行业数据", "国产替代", "自主可控", "路线图", "roadmap",
)
DEMAND_INFLECTION_KEYWORDS = (
    "放量", "爆发", "受益", "扩产", "产能", "提价", "缺货", "景气", "高增", "ramp",
)
TRAP_KEYWORDS = {
    "template_hype": ("翻倍", "唯一", "最强", "重磅", "爆发", "妖股", "龙头归来", "起飞"),
    "paid_group_style": ("老师", "入群", "vip", "私享", "带单", "内部票", "席位密码"),
    "fake_report_rumor": ("小作文", "网传", "传闻", "截图", "据说", "未经证实"),
    "cyclicality": ("钢铁", "煤炭", "有色冶炼", "化工原料", "航运", "水泥", "养殖", "周期"),
    "alt_design": ("技术路线之争", "被替代", "替代风险", "路线分歧", "新技术冲击", "颠覆性替代"),
    "geopolitics": ("出口管制", "制裁", "实体清单", "断供"),
    "domestic_substitution": ("国产替代", "自主可控", "进口替代"),
    "dilution": ("定增", "增发", "可转债", "再融资", "配股", "解禁", "股权激励摊薄"),
}
EVIDENCE_MULTIPLIER = {"strong": 1.00, "medium": 0.85, "weak": 0.70, "none": 1.00}
EVIDENCE_RANK = {"none": 0, "weak": 1, "medium": 2, "strong": 3}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text_blob(*items: Any) -> str:
    parts: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, dict):
            parts.extend(str(v) for v in item.values())
        elif isinstance(item, (list, tuple, set)):
            parts.extend(str(v) for v in item)
        else:
            parts.append(str(item))
    return " ".join(parts).lower().replace(" ", "")


def _hits(blob: str, keywords: tuple[str, ...]) -> list[str]:
    return [kw for kw in keywords if kw.replace(" ", "").lower() in blob]


def _tier_for_blob(blob: str) -> tuple[str, float]:
    for name, weight, keywords in SUPPLY_CHAIN_TIERS:
        if _hits(blob, keywords):
            return name, weight
    return "未分层", 0.55


def _elasticity_for_code(code: str) -> float:
    """
    供应链层级弹性因子：
    - tier 1 (核心卡位如 EDA/IP): 0.75 — 高壁垒,弹性略低于 tier2 因已充分预期
    - tier 2 (关键环节如光刻机/高端芯片): 0.90 — 最高弹性,稀缺性+国产替代空间最大
    - tier 3 (基础器件/材料): 0.70 — 标准化程度高,弹性受限
    - 未分层: 0.55 — 保守回退值
    """
    tier = STOCK_MAP.get(code, {}).get("tier", 3)
    if tier == 1:
        return 0.75
    if tier == 2:
        return 0.90
    if tier == 3:
        return 0.70
    return 0.55


def _rating(score: float, chain_hit: bool) -> str:
    if not chain_hit:
        return "none"
    if score >= 75:
        return "strong"
    if score >= 50:
        return "medium"
    if score >= 30:
        return "weak"
    return "none"


def _strongest_grade(*grades: str) -> str:
    return max((g or "none" for g in grades), key=lambda g: EVIDENCE_RANK.get(g, 0))


def _trap_signal(trap_id: str, label: str, severity: str, penalty: float, reason: str) -> dict:
    return {
        "id": trap_id,
        "label": label,
        "severity": severity,
        "penalty": penalty,
        "reason": reason,
    }


def _detect_traps(
    blob: str,
    *,
    chain_hit: bool,
    evidence_grade: str,
    moat_score: float,
    serenity_score: float,
    sentiment_score: float,
    change_pct: float,
) -> list[dict]:
    traps: list[dict] = []
    if chain_hit and sentiment_score >= 75 and evidence_grade == "weak":
        traps.append(_trap_signal("hype_no_orders", "高热度弱证据", "high", 0.30, "情绪偏热但缺少订单/量产/认证证据"))
    if chain_hit and moat_score < 45:
        traps.append(_trap_signal("weak_moat", "护城河不足", "medium", 0.15, "卡位叙事存在，但护城河分偏低"))
    if chain_hit and len(_hits(blob, TRAP_KEYWORDS["template_hype"])) >= 2 and evidence_grade != "strong":
        traps.append(_trap_signal("template_hype", "模板化概念炒作", "medium", 0.10, "出现多个强营销式概念词"))
    if _hits(blob, TRAP_KEYWORDS["paid_group_style"]):
        traps.append(_trap_signal("paid_group_style", "荐股话术风险", "high", 0.20, "文本接近带单/付费群话术"))
    if chain_hit and change_pct >= 8 and (moat_score < 45 or serenity_score < 55):
        # change_pct>=9 且有证据乏力时合并为一个更强的陷阱,避免双重罚分
        if change_pct >= 9 and evidence_grade != "strong":
            traps.append(_trap_signal("overheated_fundamental_mismatch", "过热+基本面错配", "medium", 0.25, "涨幅过大+基础评分偏弱+缺硬证据"))
        else:
            traps.append(_trap_signal("heat_fundamental_mismatch", "热度与基本面错配", "medium", 0.15, "涨幅较大但基础评分偏弱"))
    elif chain_hit and change_pct >= 9 and evidence_grade != "strong":
        traps.append(_trap_signal("overheated_without_hard_evidence", "过热无硬证据", "medium", 0.15, "接近涨停但缺少强证据支撑"))
    if _hits(blob, TRAP_KEYWORDS["fake_report_rumor"]):
        traps.append(_trap_signal("fake_report_rumor", "传闻/小作文", "high", 0.15, "证据来源疑似传闻或截图"))
    if _hits(blob, TRAP_KEYWORDS["cyclicality"]):
        traps.append(_trap_signal("cyclicality", "周期伪装成长", "medium", 0.15, "周期属性可能稀释 AI 卡位质量"))
    if _hits(blob, TRAP_KEYWORDS["alt_design"]):
        traps.append(_trap_signal("alt_design", "技术路线替代", "medium", 0.15, "存在路线分歧或被替代风险"))
    if _hits(blob, TRAP_KEYWORDS["geopolitics"]) and not _hits(blob, TRAP_KEYWORDS["domestic_substitution"]):
        traps.append(_trap_signal("geopolitics", "地缘风险无替代闭环", "medium", 0.15, "受制裁/断供影响但未看到国产替代闭环"))
    if _hits(blob, TRAP_KEYWORDS["dilution"]):
        traps.append(_trap_signal("dilution", "融资摊薄压力", "medium", 0.15, "存在定增/解禁/再融资等摊薄压力"))
    return traps


def evaluate_uzi_insight(
    code: str,
    *,
    snapshot: dict | None = None,
    detail: dict | None = None,
    moat_result: dict | None = None,
    serenity_score: float | None = None,
    sentiment_score: float | None = None,
    evidence_summary: dict | None = None,
) -> dict:
    """Return UZI-style bottleneck/evidence/penalty insight for one stock."""
    detail = detail if detail is not None else STOCK_DETAILS.get(code, {})
    snapshot = snapshot or {}
    moat_result = moat_result or {}
    if evidence_summary is None:
        try:
            from db import get_uzi_evidence_summary
            evidence_summary = get_uzi_evidence_summary(code)
        except Exception:
            evidence_summary = {"grade": "none", "counts": {}, "total": 0, "titles": [], "records": []}

    name = STOCK_MAP.get(code, {}).get("name", code)
    evidence_text = _text_blob(
        evidence_summary.get("titles", []),
        [r.get("summary", "") for r in evidence_summary.get("records", [])],
    )
    blob = _text_blob(
        name,
        STOCK_MAP.get(code, {}),
        detail.get("reason"),
        detail.get("serenity_tag"),
        detail.get("notes"),
        evidence_text,
    )

    chain_hits = _hits(blob, AI_CHAIN_KEYWORDS)
    chain_hit = bool(chain_hits)
    tier_name, tier_weight = _tier_for_blob(blob)

    moat_score = _as_float(moat_result.get("moat_score"), 50.0)
    serenity_score = _as_float(serenity_score, _as_float(detail.get("score"), 50.0))
    sentiment_score = _as_float(sentiment_score, 50.0)
    change_pct = _as_float(snapshot.get("change_pct"), 0.0)

    hard_hits = _hits(blob, HARD_EVIDENCE_KEYWORDS)
    medium_hits = _hits(blob, MEDIUM_EVIDENCE_KEYWORDS)
    if hard_hits:
        keyword_evidence_grade = "strong"
    elif medium_hits or detail.get("serenity_tag") or serenity_score >= 70:
        keyword_evidence_grade = "medium"
    else:
        keyword_evidence_grade = "weak"
    if not chain_hit:
        evidence_grade = "none"
    else:
        evidence_grade = _strongest_grade(keyword_evidence_grade, evidence_summary.get("grade", "none"))
    evidence_multiplier = EVIDENCE_MULTIPLIER[evidence_grade]

    irreplaceable = moat_score >= 65 or tier_weight >= 0.85
    irreplaceable_norm = max(0.0, min(1.0, moat_score / 85.0))
    elasticity = _elasticity_for_code(code)

    inflection = 0.0
    demand_hits = _hits(blob, DEMAND_INFLECTION_KEYWORDS)
    if demand_hits:
        inflection += 0.40
    if serenity_score >= 75:
        inflection += 0.25
    if sentiment_score >= 65:
        inflection += 0.20
    if change_pct >= 2:
        inflection += 0.15
    inflection = min(inflection, 1.0)

    trap_signals = _detect_traps(
        blob,
        chain_hit=chain_hit,
        evidence_grade=evidence_grade,
        moat_score=moat_score,
        serenity_score=serenity_score,
        sentiment_score=sentiment_score,
        change_pct=change_pct,
    )
    penalties = {trap["id"]: trap["penalty"] for trap in trap_signals}

    penalty_total = min(sum(penalties.values()), 0.60)
    evidence_counts = evidence_summary.get("counts", {})
    evidence_bonus = min(
        8.0,
        evidence_counts.get("strong", 0) * 3.0 +
        evidence_counts.get("medium", 0) * 1.5 +
        evidence_counts.get("weak", 0) * 0.5 +
        max(0.0, _as_float(evidence_summary.get("total_impact"), 0.0)),
    )

    if chain_hit:
        keyword_strength = min(len(chain_hits), 3) / 3.0
        base = (
            0.35 * keyword_strength +
            0.30 * irreplaceable_norm +
            0.20 * elasticity +
            0.15 * inflection
        )
        before_penalty = base * (0.70 + 0.30 * tier_weight) * evidence_multiplier * 100
        before_penalty = min(100.0, before_penalty + evidence_bonus)
        score = before_penalty * (1.0 - penalty_total)
    else:
        before_penalty = 50.0 * elasticity
        score = before_penalty

    score = round(max(0.0, min(100.0, score)), 1)
    rating = _rating(score, chain_hit)

    gates = {
        "ai_chain": chain_hit,
        "evidence": evidence_grade in ("strong", "medium"),
        "irreplaceable": bool(chain_hit and irreplaceable),
        "inflection": bool(chain_hit and inflection >= 0.4),
    }
    gates_passed = sum(1 for ok in gates.values() if ok)

    if rating == "strong":
        verdict = "Go"
    elif rating == "medium":
        verdict = "Watch"
    elif rating == "weak":
        verdict = "Wait"
    else:
        verdict = "Skip"

    reasons = []
    if chain_hit:
        reasons.append(f"命中AI链: {', '.join(chain_hits[:4])}")
        reasons.append(f"供应链层级: {tier_name}")
        reasons.append(f"证据等级: {evidence_grade}")
        if evidence_summary.get("total", 0):
            reasons.append(f"账本证据: {evidence_summary.get('total')}条")
    else:
        reasons.append("未命中AI卡位链")
    if penalties:
        reasons.append("罚分: " + ", ".join(penalties))

    return {
        "uzi_score": score,
        "rating": rating,
        "verdict": verdict,
        "ai_chain_hit": chain_hit,
        "ai_chain_keywords": chain_hits[:8],
        "ai_chain_tier": tier_name,
        "ai_chain_tier_weight": tier_weight,
        "evidence_grade": evidence_grade,
        "evidence_multiplier": evidence_multiplier,
        "evidence_hits": (hard_hits or medium_hits or evidence_summary.get("titles", []))[:6],
        "evidence_ledger": {
            "grade": evidence_summary.get("grade", "none"),
            "counts": evidence_summary.get("counts", {}),
            "total": evidence_summary.get("total", 0),
            "latest_date": evidence_summary.get("latest_date", ""),
            "titles": evidence_summary.get("titles", [])[:4],
        },
        "evidence_bonus": round(evidence_bonus, 1),
        "irreplaceable": bool(irreplaceable),
        "elasticity": round(elasticity, 2),
        "inflection": round(inflection, 2),
        "gates": gates,
        "gates_passed": gates_passed,
        "trap_signals": trap_signals,
        "penalties": penalties,
        "penalty_total": round(penalty_total, 2),
        "score_before_penalty": round(before_penalty, 1),
        "reasons": reasons,
    }


# ============================================================
# 🆕 v3.0 看板/日报用轻量接口 — 不参与评分，仅定性展示
# ============================================================

def get_uzi_chain_dashboard(code: str) -> dict:
    """返回单只标的的 AI 产业链卡位面板数据（轻量，不依赖完整 scorer）

    用于监控看板 (/api/monitor-data) 的 "AI卡位" 列和日报简报。

    Returns:
        {
            "code": "002281",
            "name": "光迅科技",
            "chain_tier": "芯片/器件",        # AI 产业链层级
            "chain_keywords": ["光芯片","光器件"],  # 命中的关键词
            "evidence_grade": "medium",       # 证据等级: strong/medium/weak/none
            "evidence_count": 3,              # 证据条目数
            "trap_count": 1,                  # 陷阱信号数
            "trap_warnings": [...],           # 陷阱详情(摘要)
            "summary_line": "🔗 芯片/器件 · 证据:3条 · ⚠️1陷阱",  # 单行摘要
            "is_ai_chain": true,              # 是否在AI产业链上
        }
    """
    try:
        result = evaluate_uzi_insight(code)
    except Exception:
        return _empty_dashboard(code)

    traps = result.get("trap_signals", [])
    trap_warnings = [
        {"id": t["id"], "label": t["label"], "severity": t["severity"]}
        for t in traps[:3]
    ]

    # 构建摘要行
    parts = []
    if result["ai_chain_hit"]:
        parts.append(f"🔗 {result['ai_chain_tier']}")
    parts.append(f"证据:{result['evidence_ledger']['total']}条")
    if traps:
        sev_icons = {"high": "🔴", "medium": "🟡", "low": "⚪"}
        for t in traps[:1]:
            parts.append(f"{sev_icons.get(t['severity'], '⚪')}{t['label']}")
    if not result["ai_chain_hit"]:
        parts.insert(0, "⚪非AI链")

    return {
        "code": code,
        "name": STOCK_MAP.get(code, {}).get("name", code),
        "chain_tier": result["ai_chain_tier"],
        "chain_keywords": result["ai_chain_keywords"][:5],
        "evidence_grade": result["evidence_grade"],
        "evidence_count": result["evidence_ledger"]["total"],
        "trap_count": len(traps),
        "trap_warnings": trap_warnings,
        "summary_line": " · ".join(parts),
        "is_ai_chain": result["ai_chain_hit"],
        "gates_passed": result["gates_passed"],
    }


def _empty_dashboard(code: str) -> dict:
    return {
        "code": code,
        "name": STOCK_MAP.get(code, {}).get("name", code),
        "chain_tier": "未知",
        "chain_keywords": [],
        "evidence_grade": "none",
        "evidence_count": 0,
        "trap_count": 0,
        "trap_warnings": [],
        "summary_line": "⚪非AI链 · 无数据",
        "is_ai_chain": False,
        "gates_passed": 0,
    }


def get_chain_summary_table(codes: list[str] = None) -> list[dict]:
    """批量获取多只标的的卡位面板（供看板批量渲染）

    Args:
        codes: 标的列表，默认全部 14 只

    Returns:
        [{code, name, chain_tier, evidence_grade, trap_count, summary_line}, ...]
    """
    if codes is None:
        from config import ALL_CODES as _c
        codes = list(_c)

    results = []
    for code in codes:
        try:
            panel = get_uzi_chain_dashboard(code)
            results.append(panel)
        except Exception:
            results.append(_empty_dashboard(code))

    # 按 AI 链层级排序（链上的在前）
    TIER_ORDER = {name: i for i, (name, _, _) in enumerate(SUPPLY_CHAIN_TIERS)}
    results.sort(key=lambda r: (
        0 if r["is_ai_chain"] else 1,
        TIER_ORDER.get(r["chain_tier"], 99),
    ))
    return results
