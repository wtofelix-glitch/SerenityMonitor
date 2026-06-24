"""
哨兵引擎 — 多位博主持续监控 + 信号融合 + 自进化
──────────────────────────────────────────────────
从 8 位财经信源自动抓取最新观点，融合到 Serenity 评分系统，
追踪信源准确率，自动调节权重。支持盘前早报推送到微信。
"""
import json
import os
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional

from db import get_conn
from serenity_logger import get_logger

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════
# 信源种子数据 (8 人评估结果, 2026-06-23)
# ══════════════════════════════════════════════════════════

SENTINEL_SOURCES_SEED = [
    {
        "id": "li_yien",
        "name": "李一恩",
        "platform": "douyin",
        "quality_rating": 4.0,
        "weight": 0.15,
        "active": 1,
        "fetch_interval_hours": 12,
        "focus": ["光模块", "算力", "易中天"],
        "core_tickers": ["300502", "300308", "300394"],
        "description": "抖音A股光模块布道者，核心标的'易中天'(新易盛/中际旭创/天孚通信)",
    },
    {
        "id": "serenity_aleabito",
        "name": "Serenity",
        "platform": "x_twitter",
        "quality_rating": 5.0,
        "weight": 0.15,
        "active": 1,
        "fetch_interval_hours": 8,
        "focus": ["AI供应链", "硅光子", "InP衬底", "CPO", "CoWoS"],
        "core_tickers": ["300308", "300502", "AAOI", "SIVE", "XFAB"],
        "description": "AI供应链瓶颈猎人，X平台810K粉丝，#1付费订阅。硅光子/CPO/800G光模块",
    },
    {
        "id": "yuboluo",
        "name": "宇菠萝",
        "platform": "bilibili",
        "quality_rating": 4.0,
        "weight": 0.10,
        "active": 1,
        "fetch_interval_hours": 24,
        "focus": ["龙头首阴", "捡漏战法", "情绪周期"],
        "core_tickers": [],
        "description": "B站职业短线客，龙头首阴捡漏战法集大成者。'放弃鱼头只吃鱼身'",
    },
    {
        "id": "wushuang_yige",
        "name": "无双一哥",
        "platform": "douyin",
        "quality_rating": 4.0,
        "weight": 0.10,
        "active": 1,
        "fetch_interval_hours": 12,
        "focus": ["首板战法", "题材热度", "资金承接"],
        "core_tickers": [],
        "description": "抖音短线实力派，深耕A股近20年。首板战法精准猎杀，仓位择时智慧",
    },
    {
        "id": "justin_sun",
        "name": "孙宇晨",
        "platform": "x_twitter",
        "quality_rating": 3.0,
        "weight": 0.05,
        "active": 1,
        "fetch_interval_hours": 12,
        "focus": ["加密宏观", "流动性", "DeFi", "AI支付"],
        "core_tickers": [],
        "description": "TRON创始人，链上大额操作+加密监管信号。间接影响A股加密/AI概念",
    },
    {
        "id": "wanshi_tiege",
        "name": "万石投资铁哥",
        "platform": "douyin",
        "quality_rating": 2.0,
        "weight": 0.03,
        "active": 1,
        "fetch_interval_hours": 168,  # 每周
        "focus": ["宏观分析", "A股3000点", "金融风险"],
        "core_tickers": [],
        "description": "抖音10万粉财经生活博主。宏观教育+金融风险案例。⚠️最新内容截至2025/03",
    },
    {
        "id": "hongbi_xiaochou",
        "name": "红鼻小丑",
        "platform": "unknown",
        "quality_rating": 1.0,
        "weight": 0.0,
        "active": 0,
        "fetch_interval_hours": 0,
        "focus": [],
        "core_tickers": [],
        "description": "B站92粉非财经博主，已排除",
    },
    {
        "id": "wushuang_tiege",
        "name": "无双铁哥(未找到)",
        "platform": "unknown",
        "quality_rating": 1.0,
        "weight": 0.0,
        "active": 0,
        "fetch_interval_hours": 0,
        "focus": [],
        "core_tickers": [],
        "description": "全网未搜到该博主。最接近为'无双一哥'或'万石投资铁哥'",
    },
    {
        "id": "research_engine",
        "name": "Serenity研究引擎",
        "platform": "trendradar",
        "quality_rating": 4.0,
        "weight": 0.06,
        "active": 1,
        "fetch_interval_hours": 6,
        "focus": ["AI", "算力", "芯片", "半导体", "光模块", "新能源", "宏观"],
        "core_tickers": [],
        "description": "自主研究引擎: TrendRadar全平台新闻→话题提取→标的映射→信号生成",
    },
    # ═══ 大师智库 (13位) ══════════════════════════════════
    {"id": "guru_duanyongping", "name": "段永平", "platform": "xueqiu", "quality_rating": 5.0, "weight": 0.10, "active": 1, "fetch_interval_hours": 24, "focus": ["价值投资", "苹果", "茅台", "腾讯"], "core_tickers": ["600519"], "description": "步步高/OPPO/vivo创始人，雪球'大道无形我有型'"},
    {"id": "guru_buffett", "name": "巴菲特", "platform": "berkshire", "quality_rating": 5.0, "weight": 0.08, "active": 1, "fetch_interval_hours": 24, "focus": ["价值投资", "美股", "宏观"], "core_tickers": [], "description": "伯克希尔·哈撒韦CEO，价值投资之父"},
    {"id": "guru_munger", "name": "芒格", "platform": "berkshire", "quality_rating": 4.5, "weight": 0.07, "active": 1, "fetch_interval_hours": 48, "focus": ["逆向思维", "多元模型", "集中投资"], "core_tickers": [], "description": "伯克希尔副董事长，多学科思维框架"},
    {"id": "guru_howardmarks", "name": "霍华德·马克斯", "platform": "oaktree", "quality_rating": 4.5, "weight": 0.07, "active": 1, "fetch_interval_hours": 48, "focus": ["周期理论", "风险控制", "逆向投资"], "core_tickers": [], "description": "橡树资本联合创始人，备忘录作家"},
    {"id": "guru_ackman", "name": "阿克曼", "platform": "x_twitter", "quality_rating": 3.5, "weight": 0.04, "active": 1, "fetch_interval_hours": 48, "focus": ["激进投资", "集中持仓", "美股"], "core_tickers": [], "description": "Pershing Square CEO"},
    {"id": "guru_dalio", "name": "达利欧", "platform": "bridgewater", "quality_rating": 4.5, "weight": 0.07, "active": 1, "fetch_interval_hours": 48, "focus": ["宏观周期", "债务周期", "全球配置"], "core_tickers": [], "description": "桥水基金创始人,《原则》作者"},
    {"id": "guru_lilu", "name": "李录", "platform": "himalaya", "quality_rating": 4.0, "weight": 0.06, "active": 1, "fetch_interval_hours": 48, "focus": ["价值投资", "中国", "现代化"], "core_tickers": [], "description": "喜马拉雅资本创始人，芒格家族资产管理人"},
    {"id": "guru_danbin", "name": "但斌", "platform": "weibo", "quality_rating": 3.5, "weight": 0.05, "active": 1, "fetch_interval_hours": 24, "focus": ["茅台", "价值投资", "A股"], "core_tickers": ["600519"], "description": "东方港湾董事长"},
    {"id": "guru_linyuan", "name": "林园", "platform": "weibo", "quality_rating": 3.5, "weight": 0.05, "active": 1, "fetch_interval_hours": 24, "focus": ["消费", "医药", "A股核心资产"], "core_tickers": [], "description": "林园投资董事长，民间股神"},
    {"id": "guru_burry", "name": "迈克尔·巴里", "platform": "x_twitter", "quality_rating": 4.0, "weight": 0.06, "active": 1, "fetch_interval_hours": 48, "focus": ["做空泡沫", "逆向投资", "13F"], "core_tickers": [], "description": "Scion创始人,《大空头》原型"},
    {"id": "guru_druckenmiller", "name": "德鲁肯米勒", "platform": "duquesne", "quality_rating": 4.5, "weight": 0.06, "active": 1, "fetch_interval_hours": 48, "focus": ["宏观交易", "索罗斯体系", "全球宏观"], "core_tickers": [], "description": "索罗斯量子基金前操盘手"},
    {"id": "guru_tepper", "name": "泰珀", "platform": "appaloosa", "quality_rating": 3.5, "weight": 0.05, "active": 1, "fetch_interval_hours": 48, "focus": ["困境反转", "价值投资", "13F"], "core_tickers": [], "description": "Appaloosa Management，华尔街抄底王"},
    {"id": "guru_cathiewood", "name": "Cathie Wood", "platform": "x_twitter", "quality_rating": 3.0, "weight": 0.05, "active": 1, "fetch_interval_hours": 24, "focus": ["颠覆创新", "AI", "机器人", "基因"], "core_tickers": ["NVDA", "TSLA"], "description": "ARK Invest CEO，木头姐"},
]

# Serenity 持仓池 → 信源关注映射
# (哨兵关注的标的若不在持仓中，也记录为"观察池")
WATCHLIST_MAP = {
    "300502": {"name": "新易盛", "sources": ["li_yien", "serenity_aleabito"]},
    "300308": {"name": "中际旭创", "sources": ["li_yien", "serenity_aleabito"]},
    "300394": {"name": "天孚通信", "sources": ["li_yien", "serenity_aleabito"]},
    "002281": {"name": "光迅科技", "sources": ["serenity_aleabito"]},
    "600460": {"name": "士兰微", "sources": ["serenity_aleabito"]},
}

# 融合规则: 信号强度映射
SIGNAL_STRENGTH = {
    "STRONG_BUY": 2.0,
    "BUY": 1.0,
    "CAUTION_BUY": 0.5,
    "HOLD": 0.0,
    "STRONG_HOLD": 0.0,
    "WATCH": -0.3,
    "WEAK_HOLD": -0.5,
    "SELL": -1.0,
    "STOP_LOSS": -2.0,
    "REDUCE": -1.0,
    "TAKE_PROFIT": 0.5,
}

# 话题 → 板块映射 (用于推断间接影响)
TOPIC_SECTOR_MAP = {
    "光模块": ["通信", "光器件"],
    "算力": ["AI服务器", "数据中心"],
    "AI供应链": ["半导体", "光器件", "先进封装"],
    "硅光子": ["光器件", "通信"],
    "CPO": ["光器件", "先进封装"],
    "CoWoS": ["先进封装", "半导体"],
    "首板战法": ["题材热点"],
    "龙头首阴": ["题材热点", "情绪周期"],
    "加密宏观": ["区块链", "金融科技"],
    "流动性": ["宏观", "大金融"],
    "小盘股": ["中小市值"],
}


# ══════════════════════════════════════════════════════════
# 哨兵引擎
# ══════════════════════════════════════════════════════════

class SentinelEngine:
    """哨兵信源管理 + 信号融合 + 自进化引擎"""

    def __init__(self):
        self._init_db()

    def _init_db(self):
        """创建哨兵相关表 (幂等)"""
        conn = get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sentinel_sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                platform TEXT,
                quality_rating REAL DEFAULT 3.0,
                weight REAL DEFAULT 0.10,
                active INTEGER DEFAULT 1,
                fetch_interval_hours INTEGER DEFAULT 12,
                last_fetch_at TEXT,
                focus TEXT DEFAULT '[]',
                core_tickers TEXT DEFAULT '[]',
                description TEXT DEFAULT '',
                base_weight REAL,
                accuracy_30d REAL,
                observation_count_30d INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS sentinel_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                content_raw TEXT,
                content_url TEXT,
                signal_type TEXT DEFAULT 'info',
                tickers TEXT DEFAULT '[]',
                topics TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                impact_score REAL DEFAULT 0,
                processed INTEGER DEFAULT 0,
                is_morning_brief INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS sentinel_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                observation_id INTEGER,
                ticker TEXT,
                direction TEXT,
                outcome_1d REAL,
                outcome_3d REAL,
                outcome_5d REAL,
                correct INTEGER DEFAULT 0,
                settled_at TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_obs_source ON sentinel_observations(source_id, fetched_at);
            CREATE INDEX IF NOT EXISTS idx_obs_processed ON sentinel_observations(processed);
            CREATE INDEX IF NOT EXISTS idx_perf_source ON sentinel_performance(source_id);
        """)
        conn.commit()
        conn.close()

    def seed_sources(self):
        """首次运行时导入信源种子数据"""
        conn = get_conn()
        for src in SENTINEL_SOURCES_SEED:
            conn.execute("""
                INSERT OR IGNORE INTO sentinel_sources
                (id, name, platform, quality_rating, weight, active,
                 fetch_interval_hours, focus, core_tickers, description, base_weight)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                src["id"], src["name"], src["platform"],
                src["quality_rating"], src["weight"], src["active"],
                src["fetch_interval_hours"],
                json.dumps(src.get("focus", []), ensure_ascii=False),
                json.dumps(src.get("core_tickers", []), ensure_ascii=False),
                src.get("description", ""),
                src["weight"],  # base_weight = initial weight
            ))
        conn.commit()
        conn.close()

    # ── 信源管理 ────────────────────────────────────────

    def get_active_sources(self) -> list[dict]:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM sentinel_sources WHERE active=1 ORDER BY weight DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_source(self, source_id: str) -> Optional[dict]:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM sentinel_sources WHERE id=?", (source_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def update_source_activity(self, source_id: str):
        conn = get_conn()
        conn.execute(
            "UPDATE sentinel_sources SET last_fetch_at=?, updated_at=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source_id)
        )
        conn.commit()
        conn.close()

    # ── 观测记录 ────────────────────────────────────────

    def record_observation(self, source_id: str, content: str,
                           signal_type: str = "info",
                           tickers: list = None, topics: list = None,
                           confidence: float = 0.5, content_url: str = "",
                           impact_score: float = 0.0) -> int:
        """记录一条哨兵观测，返回 observation_id"""
        conn = get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            "INSERT INTO sentinel_observations (source_id, fetched_at, content_raw, content_url, signal_type, tickers, topics, confidence, impact_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, now, content[:500], content_url, signal_type,
             json.dumps(tickers or [], ensure_ascii=False),
             json.dumps(topics or [], ensure_ascii=False),
             confidence, impact_score)
        )
        # Update source activity in same connection to avoid DB lock
        conn.execute(
            "UPDATE sentinel_sources SET last_fetch_at=?, updated_at=? WHERE id=?",
            (now, now, source_id)
        )
        conn.commit()
        obs_id = cursor.lastrowid
        conn.close()
        return obs_id

    def get_recent_observations(self, source_id: str = None,
                                 hours: int = 72, limit: int = 50) -> list[dict]:
        conn = get_conn()
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        if source_id:
            rows = conn.execute(
                "SELECT * FROM sentinel_observations WHERE source_id=? AND fetched_at>=? ORDER BY fetched_at DESC LIMIT ?",
                (source_id, since, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sentinel_observations WHERE fetched_at>=? ORDER BY fetched_at DESC LIMIT ?",
                (since, limit)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_unprocessed_observations(self) -> list[dict]:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM sentinel_observations WHERE processed=0 ORDER BY fetched_at ASC LIMIT 100"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def mark_processed(self, obs_id: int):
        conn = get_conn()
        conn.execute(
            "UPDATE sentinel_observations SET processed=1 WHERE id=?", (obs_id,)
        )
        conn.commit()
        conn.close()

    # ── 融合引擎 ────────────────────────────────────────

    def compute_sentinel_bonus(self, code: str) -> dict:
        """计算所有活跃信源对某标的的评分加成"""
        sources = self.get_active_sources()
        bonus = 0.0
        signals = []
        source_count = 0

        for src in sources:
            recent = self.get_recent_observations(src["id"], hours=72)
            for obs in recent:
                tickers = json.loads(obs["tickers"]) if isinstance(obs["tickers"], str) else (obs["tickers"] or [])
                relevant = code in tickers

                # 也检查: 该信源的核心关注列表是否包含此代码
                core = json.loads(src["core_tickers"]) if isinstance(src["core_tickers"], str) else (src["core_tickers"] or [])
                if not relevant and code in core:
                    relevant = True

                if relevant:
                    direction = 1.0 if obs["signal_type"] in ("bullish", "STRONG_BUY", "BUY") else (
                        -1.0 if obs["signal_type"] in ("bearish", "SELL", "STOP_LOSS") else 0.0
                    )
                    weighted = direction * src["weight"] * (obs["confidence"] or 0.5)
                    bonus += weighted
                    signals.append({
                        "source": src["name"],
                        "source_id": src["id"],
                        "content": (obs["content_raw"] or "")[:80],
                        "signal_type": obs["signal_type"],
                        "direction": "bullish" if direction > 0 else ("bearish" if direction < 0 else "neutral"),
                        "weighted_impact": round(weighted, 3),
                        "fetched_at": obs["fetched_at"],
                    })
                    source_count += 1

        # 共振加成：多源确认 → 额外加成
        if source_count >= 3:
            resonance_bonus = 0.5  # 三源共振额外 +0.5
        elif source_count >= 2:
            resonance_bonus = 0.3  # 双源确认额外 +0.3
        else:
            resonance_bonus = 0.0

        total_bonus = round(bonus * 10 + resonance_bonus, 2)

        return {
            "code": code,
            "bonus": total_bonus,
            "source_count": source_count,
            "resonance_bonus": round(resonance_bonus, 2),
            "signals": signals,
        }

    def get_portfolio_fusion(self) -> list[dict]:
        """对 Serenity 当前持仓池 + 观察池逐一计算哨兵影响"""
        from config import ALL_CODES, STOCK_MAP

        results = []
        for code in ALL_CODES:
            fusion = self.compute_sentinel_bonus(code)
            if fusion["source_count"] > 0 or code in WATCHLIST_MAP:
                name = STOCK_MAP.get(code, {}).get("name", code)
                results.append({
                    "code": code,
                    "name": name,
                    **fusion,
                })

        # 添加观察池中不在 ALL_CODES 的标的
        for code, info in WATCHLIST_MAP.items():
            if code not in ALL_CODES and code not in {r["code"] for r in results}:
                fusion = self.compute_sentinel_bonus(code)
                results.append({
                    "code": code, "name": info["name"], **fusion,
                })

        results.sort(key=lambda x: abs(x["bonus"]), reverse=True)
        return results

    # ── 自进化 ──────────────────────────────────────────

    def update_source_weights(self):
        """根据历史准确率 + 活跃度自动调节信源权重"""
        sources = self.get_active_sources()
        conn = get_conn()

        for src in sources:
            sid = src["id"]
            base_weight = src.get("base_weight") or src["weight"]

            # 计算 30 天准确率
            perf = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) as correct_cnt
                FROM sentinel_performance
                WHERE source_id=? AND settled_at >= date('now','-30 days')
            """, (sid,)).fetchone()

            total = perf["total"] or 0
            correct = perf["correct_cnt"] or 0
            accuracy = correct / total if total > 0 else 0.5

            # 计算 30 天活跃度
            obs_count = conn.execute("""
                SELECT COUNT(*) as cnt FROM sentinel_observations
                WHERE source_id=? AND created_at >= datetime('now','-30 days')
            """, (sid,)).fetchone()["cnt"]

            activity_factor = min(1.0, obs_count / 10) if obs_count > 0 else 0.1

            # 权重计算公式
            new_weight = base_weight * (1 + (accuracy - 0.5) * 0.5) * activity_factor
            new_weight = max(base_weight * 0.3, min(base_weight * 1.5, new_weight))

            # 降温: <40% 准确率 → 降权
            if accuracy < 0.4 and total >= 5:
                new_weight = base_weight * 0.3
            # 停更降权: 连续30天无观测
            if obs_count == 0:
                new_weight = base_weight * 0.3

            conn.execute("""
                UPDATE sentinel_sources
                SET weight=?, accuracy_30d=?, observation_count_30d=?, updated_at=?
                WHERE id=?
            """, (round(new_weight, 4), round(accuracy, 3), obs_count,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sid))

        conn.commit()
        conn.close()
        log.info("哨兵权重已更新 (基于30天准确率+活跃度)")

    def settle_outcomes(self, days_back: int = 5):
        """结算最近N天观测的实际结果"""
        conn = get_conn()
        since = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        unsettled = conn.execute("""
            SELECT o.id, o.source_id, o.tickers, o.signal_type, o.fetched_at
            FROM sentinel_observations o
            LEFT JOIN sentinel_performance p ON p.observation_id = o.id
            WHERE p.id IS NULL AND o.fetched_at >= ? AND o.tickers != '[]'
        """, (since,)).fetchall()

        settled = 0
        for obs in unsettled:
            tickers = json.loads(obs["tickers"]) if isinstance(obs["tickers"], str) else (obs["tickers"] or [])
            direction = "bullish" if obs["signal_type"] in ("bullish", "STRONG_BUY", "BUY") else (
                "bearish" if obs["signal_type"] in ("bearish", "SELL", "STOP_LOSS") else "neutral"
            )
            if direction == "neutral":
                continue

            for ticker in tickers:
                # 查该标的在观测日期后的实际涨跌幅
                price_data = conn.execute("""
                    SELECT date, close, change_pct FROM daily_snapshots
                    WHERE code=? AND date > date(?)
                    ORDER BY date ASC LIMIT 5
                """, (ticker, obs["fetched_at"][:10])).fetchall()

                if len(price_data) >= 1:
                    outcome_1d = price_data[0]["change_pct"] if len(price_data) >= 1 else None
                    outcome_3d = sum(p["change_pct"] or 0 for p in price_data[:3]) if len(price_data) >= 3 else None
                    outcome_5d = sum(p["change_pct"] or 0 for p in price_data[:5]) if len(price_data) >= 5 else None

                    # 方向是否正确？
                    correct = 0
                    if outcome_1d is not None:
                        if (direction == "bullish" and outcome_1d > 0) or (direction == "bearish" and outcome_1d < 0):
                            correct = 1

                    conn.execute("""
                        INSERT INTO sentinel_performance
                        (source_id, observation_id, ticker, direction, outcome_1d, outcome_3d, outcome_5d, correct, settled_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, (obs["source_id"], obs["id"], ticker, direction,
                          round(outcome_1d, 2) if outcome_1d else None,
                          round(outcome_3d, 2) if outcome_3d else None,
                          round(outcome_5d, 2) if outcome_5d else None,
                          correct, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    settled += 1

        conn.commit()
        conn.close()
        if settled > 0:
            log.info(f"哨兵结算: {settled} 条观测结果已记录")
            self.update_source_weights()
        return settled

    # ── 绩效查询 ────────────────────────────────────────

    def get_source_performance(self, source_id: str = None, days: int = 30) -> list[dict]:
        conn = get_conn()
        if source_id:
            rows = conn.execute("""
                SELECT source_id, COUNT(*) as total,
                       SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) as correct_cnt,
                       AVG(outcome_1d) as avg_1d_return
                FROM sentinel_performance
                WHERE source_id=? AND settled_at >= date('now',?)
                GROUP BY source_id
            """, (source_id, f'-{days} days')).fetchall()
        else:
            rows = conn.execute("""
                SELECT source_id, COUNT(*) as total,
                       SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) as correct_cnt,
                       AVG(outcome_1d) as avg_1d_return
                FROM sentinel_performance
                WHERE settled_at >= date('now',?)
                GROUP BY source_id
            """, (f'-{days} days',)).fetchall()
        conn.close()

        results = []
        for r in rows:
            src = self.get_source(r["source_id"])
            results.append({
                "source_id": r["source_id"],
                "name": src["name"] if src else r["source_id"],
                "total": r["total"],
                "correct": r["correct_cnt"] or 0,
                "accuracy": round((r["correct_cnt"] or 0) / r["total"] * 100, 1) if r["total"] > 0 else 0,
                "avg_1d_return": round(r["avg_1d_return"] or 0, 2),
            })

        results.sort(key=lambda x: x["accuracy"], reverse=True)
        return results

    # ── 早报 ────────────────────────────────────────────

    def generate_morning_brief(self) -> str:
        """生成盘前哨兵早报"""
        sources = self.get_active_sources()
        obs = self.get_recent_observations(hours=24, limit=30)
        fusion = self.get_portfolio_fusion()
        perf = self.get_source_performance()

        now = datetime.now().strftime("%m/%d %H:%M")
        lines = [f"📡 Serenity 哨兵早报 {now}"]
        lines.append("━" * 20)

        # 各信源最新动态
        for src in sources:
            src_obs = [o for o in obs if o["source_id"] == src["id"]]
            acc = next((p for p in perf if p["source_id"] == src["id"]), None)
            acc_str = f"准确率{acc['accuracy']}%" if acc and acc["total"] >= 3 else "数据不足"
            status = "🟢" if src_obs else ("🟡" if src["weight"] > 0 else "🔴")
            if src_obs:
                latest = src_obs[0]
                content = (latest["content_raw"] or "")[:60]
                lines.append(f"{status} {src['name']}({acc_str}): {content}")
            else:
                lines.append(f"{status} {src['name']}({acc_str}): 暂无新内容")

        # 共振信号
        lines.append("")
        lines.append("⚠️ 共振信号:")
        high_impact = [f for f in fusion if abs(f["bonus"]) >= 0.5]
        if high_impact:
            for f in high_impact[:5]:
                direction = "🟢" if f["bonus"] > 0 else "🔴"
                sources_list = ", ".join(s["source"] for s in f["signals"][:3])
                lines.append(f"  {direction} {f['name']}({f['code']}): {f['bonus']:+.1f}分 ({sources_list})")
        else:
            lines.append("  无显著共振信号")

        # 对池影响
        lines.append("")
        lines.append("📊 对 Serenity 池影响:")
        pool_impact = [f for f in fusion if f["source_count"] > 0]
        if pool_impact:
            for f in pool_impact[:5]:
                lines.append(f"  {f['name']}: {f['bonus']:+.1f}分 ({f['source_count']}源)")
        else:
            lines.append("  持仓池暂无直接哨兵信号")

        lines.append("━" * 20)
        lines.append("🤖 Serenity 哨兵引擎 · 自动生成")

        return "\n".join(lines)

    def push_morning_brief(self):
        """静默生成早报并写入日志 (不再推送到微信, 通过看板查看)"""
        brief = self.generate_morning_brief()
        log.info(f"哨兵早报已生成 ({len(brief)} 字符)")
        # 写入日志文件供看板/调试查阅
        log.debug(f"哨兵早报:\n{brief}")
        return brief

    # ── 大师智库同步 ────────────────────────────────────

    def sync_guru_quotes(self) -> int:
        """从 guru_wisdom.db 同步大师语录到哨兵观测表"""
        import sqlite3 as _sqlite
        gdb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guru_wisdom.db")
        if not os.path.exists(gdb_path):
            return 0

        gdb = _sqlite.connect(gdb_path)
        gdb.row_factory = _sqlite.Row

        # 取最近7天采集的语录
        quotes = gdb.execute("""
            SELECT q.*, g.cn_name, g.id as gid
            FROM quotes q JOIN gurus g ON q.guru_id = g.id
            WHERE q.collected_at >= datetime('now', '-7 days', 'localtime')
            ORDER BY q.collected_at DESC LIMIT 100
        """).fetchall()

        synced = 0
        for q in quotes:
            guru_source_id = f"guru_{q['gid']}"
            # 跳过未注册的大师
            src = self.get_source(guru_source_id)
            if not src or not src.get("active"):
                continue

            # 解析股票代码
            tickers = []
            if q["relevant_stocks"]:
                try:
                    tickers = json.loads(q["relevant_stocks"]) if isinstance(q["relevant_stocks"], str) else q["relevant_stocks"]
                except Exception:
                    tickers = []

            # 判断信号类型
            signal_type = q["sentiment"] if q["sentiment"] in ("bullish", "bearish") else "info"
            confidence = 0.5 if signal_type == "info" else 0.6

            # 跳过已存在
            existing = self.get_recent_observations(source_id=guru_source_id, hours=168, limit=200)
            content_key = (q["content"] or "")[:80]
            if any((o.get("content_raw") or "").startswith(content_key) for o in existing):
                continue

            self.record_observation(
                guru_source_id,
                q["content"][:500],
                signal_type=signal_type,
                tickers=tickers,
                topics=[q["topic"]] if q["topic"] else [],
                confidence=confidence,
                content_url=q["source_url"] or "",
            )
            synced += 1

        gdb.close()
        if synced > 0:
            log.info(f"大师智库同步: {synced} 条新语录→哨兵")
        return synced

    # ── 手动录入 (用于无法自动抓取的平台) ──────────────

    def manual_log(self, source_id: str, content: str,
                   signal_type: str = "info",
                   tickers: str = "", topics: str = "",
                   confidence: float = 0.5) -> int:
        """手动录入观测 (适合抖音/B站等需人工判断的平台)"""
        ticker_list = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else []
        topic_list = [t.strip() for t in topics.split(",") if t.strip()] if topics else []

        # 自动推断 impact_score
        direction = 1.0 if signal_type in ("bullish", "STRONG_BUY", "BUY") else (
            -1.0 if signal_type in ("bearish", "SELL", "STOP_LOSS") else 0.0
        )
        impact = direction * confidence * 5  # -5 到 +5

        return self.record_observation(
            source_id, content, signal_type=signal_type,
            tickers=ticker_list, topics=topic_list,
            confidence=confidence, impact_score=round(impact, 2)
        )


# 全局单例
_engine: Optional[SentinelEngine] = None


def get_sentinel() -> SentinelEngine:
    global _engine
    if _engine is None:
        _engine = SentinelEngine()
        _engine.seed_sources()
    return _engine
