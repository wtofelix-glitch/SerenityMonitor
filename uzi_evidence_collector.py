"""UZI 证据自动采集器 — 桥接 sentinel_observations → uzi_evidence

v3.0: 自动从哨兵观察中提取AI产业链相关证据，写入 uzi_evidence 表。
替代之前仅通过 CLI 手动录入的方式，让 UZI 卡位面板有真实数据可展示。
"""
from datetime import date, timedelta
from db import get_conn, add_uzi_evidence, get_uzi_evidence_summary
from config import STOCK_MAP, ALL_CODES
from serenity_logger import get_logger

log = get_logger(__name__)

# 证据关键词 — 来自 uzi_insight.py 的 HARD/MEDIUM 证据检测
EVIDENCE_KEYWORDS = {
    "strong": [
        "量产", "订单", "中标", "独供", "定点", "认证",
        "通过验证", "合格供应商", "批量交付", "长协",
    ],
    "medium": [
        "送样", "小批量", "研报", "券商推荐", "金股",
        "扩产", "产能", "投产", "进展", "突破",
        "专利", "核心技术", "领先",
    ],
    "weak": [
        "关注", "布局", "预期", "规划", "有望",
        "合作", "战略", "研发",
    ],
}


def _match_evidence_strength(text: str) -> str | None:
    """从文本中匹配证据强度，返回 'strong'/'medium'/'weak'/None"""
    for strength in ("strong", "medium"):
        for kw in EVIDENCE_KEYWORDS[strength]:
            if kw in text:
                return strength
    for kw in EVIDENCE_KEYWORDS["weak"]:
        if kw in text:
            return "weak"
    return None


def collect_evidence_for_code(code: str, days_back: int = 7) -> dict:
    """为单只标的从 sentinel_observations 提取证据

    Args:
        code: 股票代码
        days_back: 回溯天数

    Returns:
        {"code": ..., "added": int, "skipped": int}
    """
    conn = get_conn()
    added = 0
    skipped = 0
    name = STOCK_MAP.get(code, {}).get("name", code)

    try:
        # 获取已有证据去重
        existing_summary = get_uzi_evidence_summary(code)
        existing_titles = set(existing_summary.get("titles", []))

        # 查询近期哨兵观察（匹配标的代码或名称）
        since = (date.today() - timedelta(days=days_back)).isoformat()
        rows = conn.execute("""
            SELECT source_id, content_raw, signal_type, tickers, topics,
                   confidence, impact_score, created_at
            FROM sentinel_observations
            WHERE created_at >= ?
              AND (tickers LIKE ? OR tickers LIKE ?
                   OR content_raw LIKE ? OR content_raw LIKE ?
                   OR source_id LIKE ?)
              AND (content_raw LIKE '%AI%' OR content_raw LIKE '%芯片%' OR content_raw LIKE '%光模块%'
                   OR content_raw LIKE '%算力%' OR content_raw LIKE '%订单%' OR content_raw LIKE '%量产%'
                   OR content_raw LIKE '%产能%' OR content_raw LIKE '%扩产%' OR content_raw LIKE '%突破%'
                   OR content_raw LIKE '%专利%' OR content_raw LIKE '%认证%' OR content_raw LIKE '%中标%'
                   OR content_raw LIKE '%研报%' OR content_raw LIKE '%推荐%')
            ORDER BY created_at DESC
            LIMIT 20
        """, (since, f"%{code}%", f"%{name}%", f"%{code}%", f"%{name}%", f"%{code}%")).fetchall()

        for row in rows:
            source_id = row["source_id"] or "sentinel"
            content = row["content_raw"] or ""
            topics = row["topics"] or ""
            confidence = row["confidence"] or 0.5
            combined = f"{source_id} {topics} {content}"

            # 去重用 source_id
            if source_id in existing_titles:
                skipped += 1
                continue

            strength = _match_evidence_strength(combined)
            if not strength:
                continue

            try:
                add_uzi_evidence(
                    code=code,
                    title=source_id[:200],
                    strength=strength,
                    source_type="sentinel",
                    summary=content[:500] if content else topics[:200],
                    impact=round(float(confidence) * 10, 1),
                    event_date=row["created_at"][:10] if row["created_at"] else None,
                )
                existing_titles.add(source_id)
                added += 1
            except Exception as e:
                log.debug("证据写入失败 %s: %s", code, e)
                skipped += 1
    finally:
        conn.close()

    return {"code": code, "name": name, "added": added, "skipped": skipped}


def collect_evidence_for_all(days_back: int = 7) -> dict:
    """为全部标的自动采集证据

    Returns:
        {"checked": int, "added": int, "updated": int, "details": [...]}
    """
    total_added = 0
    total_updated = 0
    details = []

    for code in ALL_CODES:
        try:
            r = collect_evidence_for_code(code, days_back)
            total_added += r["added"]
            total_updated += r["skipped"]
            if r["added"] > 0:
                details.append(r)
                log.info("📎 %s: +%d条证据", r["name"], r["added"])
        except Exception as e:
            log.warning("证据采集失败 %s: %s", code, e)

    return {
        "checked": len(ALL_CODES),
        "added": total_added,
        "updated": total_updated,
        "details": details,
    }


# ── 种子数据：从 STOCK_DETAILS 的 reason 字段提取初始证据 ──

def seed_evidence_from_config():
    """从 STOCK_DETAILS 配置的 reason 字段种子化初始证据。

    只用于 uzi_evidence 表为空时的首次初始化。
    每条 reason 按关键词匹配强度自动分级。
    """
    from config import STOCK_DETAILS

    conn = get_conn()
    existing_count = conn.execute("SELECT COUNT(*) FROM uzi_evidence").fetchone()[0]
    conn.close()

    if existing_count > 0:
        return {"status": "skipped", "reason": f"已有 {existing_count} 条证据，跳过种子化"}

    added = 0
    for code, detail in STOCK_DETAILS.items():
        reason = detail.get("reason", "")
        tag = detail.get("serenity_tag", "")
        combined = f"{reason} {tag}"
        strength = _match_evidence_strength(combined) or "medium"

        try:
            add_uzi_evidence(
                code=code,
                title=f"Serenity框架评级: {tag}" if tag else f"{STOCK_MAP.get(code, {}).get('name', code)} 投资逻辑",
                strength=strength,
                source_type="config/STOCK_DETAILS",
                summary=reason[:500],
                impact=2.0,
            )
            added += 1
        except Exception as e:
            log.warning("种子证据写入失败 %s: %s", code, e)

    return {"status": "seeded", "added": added}


if __name__ == "__main__":
    # 先种子化，再采集
    seed = seed_evidence_from_config()
    print(f"种子化: {seed}")

    result = collect_evidence_for_all(days_back=14)
    print(f"采集完成: 检查{result['checked']}只, 新增{result['added']}条, 去重{result['updated']}条")
    for d in result.get("details", []):
        print(f"  {d['name']}: +{d['added']}条")
