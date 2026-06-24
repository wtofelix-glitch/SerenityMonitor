"""
Serenity 自主研究引擎 — 全网信息采集 + 深度分析 + 策略融合
────────────────────────────────────────────────────
从 TrendRadar 多平台新闻 / RSS / WebSearch 采集信息,
提取信号 → 映射标的 → 生成早报/周报 → 融入评分系统。
"""
import json, os, re, sqlite3
from datetime import datetime, date, timedelta
from typing import Optional
from collections import Counter

from serenity_logger import get_logger
from db import get_conn

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════
# TRADRADAR 数据路径
# ══════════════════════════════════════════════════════════
TR_DIR = os.path.expanduser("~/workspace/TrendRadar/output/news")


# ══════════════════════════════════════════════════════════
# 信号提取词库
# ══════════════════════════════════════════════════════════
BULLISH_KEYWORDS = [
    "涨停", "突破", "利好", "增持", "回购", "业绩超预期", "政策扶持",
    "资金流入", "主力加仓", "北上资金", "机构调研", "订单饱满",
    "产能扩张", "新产品", "中标", "涨价", "供不应求", "高景气",
    "技术突破", "国产替代", "龙头", "翻倍", "主升浪", "放量",
    "券商看好", "上调评级", "目标价", "买入评级", "强烈推荐",
    "社保加仓", "养老金入市", "外资流入", "降息", "宽松",
]
BEARISH_KEYWORDS = [
    "跌停", "利空", "减持", "业绩暴雷", "监管", "退市", "ST",
    "资金流出", "主力出逃", "北上资金流出", "机构减仓",
    "产能过剩", "价格战", "订单下滑", "需求疲软", "库存高企",
    "技术瓶颈", "被替代", "暴雷", "造假", "被查", "被罚",
    "下调评级", "卖出评级", "看空", "升息", "收紧", "缩表",
    "地缘风险", "贸易战", "制裁", "黑天鹅",
]
TICKER_RE = re.compile(r'\b(00[0-9]{4}|30[0-9]{4}|60[0-9]{4}|68[0-9]{4})\b')

# Stock name → code mapping (for title keyword matching)
def _build_name_map():
    from config import STOCK_MAP
    return {info["name"]: code for code, info in STOCK_MAP.items()}

STOCK_NAME_MAP = {}  # populated lazily

# ══════════════════════════════════════════════════════════
# 话题 → 板块 → 标的 三级映射 (扩展版, 100+ 条目)
# ══════════════════════════════════════════════════════════
TOPIC_SECTOR_TICKER_MAP = {
    # === AI / 算力 ===
    "AI": ("人工智能", ["300308", "300502", "300394", "002281", "600460", "688256"]),
    "人工智能": ("人工智能", ["300308", "300502", "300394", "002281", "600460"]),
    "算力": ("算力基础设施", ["300308", "300502", "300394", "002281"]),
    "光模块": ("光通信", ["300308", "300502", "300394", "002281"]),
    "CPO": ("光通信", ["300308", "300502", "300394"]),
    "硅光子": ("光通信", ["300308", "300502"]),
    "数据中心": ("算力基础设施", ["300308", "300502", "600460"]),
    "液冷": ("算力基础设施", ["600460"]),
    "服务器": ("算力基础设施", ["600460"]),
    "GPU": ("芯片", ["600460"]),
    "英伟达": ("芯片", ["600460"]),
    "NVIDIA": ("芯片", ["600460"]),
    "芯片": ("半导体", ["600460", "688256"]),
    "半导体": ("半导体", ["600460", "688256", "300308"]),
    "先进封装": ("半导体", ["600460"]),
    "CoWoS": ("半导体", ["600460"]),
    "HBM": ("半导体", ["600460"]),
    "光刻": ("半导体设备", []),
    "EDA": ("半导体软件", []),

    # === 新能源 ===
    "光伏": ("光伏", []),
    "储能": ("储能", []),
    "锂电": ("锂电池", []),
    "锂电池": ("锂电池", []),
    "固态电池": ("锂电池", []),
    "新能源汽车": ("新能源汽车", []),
    "充电桩": ("充电桩", []),
    "特高压": ("电力设备", []),
    "电力": ("电力", []),
    "风电": ("风电", []),

    # === 周期 / 化工 / 建材 ===
    "化工": ("化工", ["600141"]),
    "磷化工": ("化工", ["600141"]),
    "有机硅": ("化工", ["600141"]),
    "草甘膦": ("化工", ["600141"]),
    "水泥": ("建材", ["600585"]),
    "建材": ("建材", ["600585"]),
    "基建": ("基建", ["600585"]),
    "地产": ("房地产", ["600585"]),
    "房地产": ("房地产", ["600585"]),
    "钢铁": ("钢铁", []),
    "有色": ("有色金属", []),
    "黄金": ("贵金属", []),
    "稀土": ("稀土", []),
    "煤炭": ("煤炭", []),
    "石油": ("石油化工", []),

    # === 消费 / 医药 ===
    "消费": ("消费", []),
    "白酒": ("白酒", []),
    "食品": ("食品饮料", []),
    "医药": ("医药", []),
    "创新药": ("医药", []),
    "医疗器械": ("医疗器械", []),
    "中药": ("中药", []),
    "医美": ("医美", []),

    # === 金融 ===
    "券商": ("券商", []),
    "银行": ("银行", []),
    "保险": ("保险", []),
    "降息": ("宏观-利好", []),
    "降准": ("宏观-利好", []),
    "加息": ("宏观-利空", []),
    "MLF": ("宏观", []),
    "LPR": ("宏观", []),
    "人民币": ("汇率", []),
    "汇率": ("汇率", []),
    "美联储": ("宏观", []),

    # === 政策 / 产业 ===
    "信创": ("信创", []),
    "数字经济": ("数字经济", []),
    "数据要素": ("数字经济", []),
    "新质生产力": ("新质生产力", []),
    "国企改革": ("国企改革", []),
    "中特估": ("国企改革", []),
    "一带一路": ("一带一路", ["600585"]),
    "出海": ("出海", []),
    "低空经济": ("低空经济", []),
    "商业航天": ("商业航天", []),
    "机器人": ("机器人", []),
    "量子": ("量子计算", []),
    "6G": ("通信", []),
    "卫星": ("卫星互联网", []),

    # === 风险 ===
    "地缘": ("风险-地缘", []),
    "贸易战": ("风险-贸易", []),
    "制裁": ("风险-制裁", []),
    "监管": ("风险-监管", []),
    "退市": ("风险-退市", []),
    "暴雷": ("风险-暴雷", []),
    "减持": ("风险-减持", []),
    "解禁": ("风险-解禁", []),

    # === 博主关键词 ===
    "易中天": ("光通信", ["300502", "300308", "300394"]),
    "首板": ("短线热点", []),
    "龙头首阴": ("短线情绪", []),
    "捡漏": ("短线情绪", []),
    "WLFI": ("加密", []),
    "TRON": ("加密", []),
    "ETH": ("加密", []),
}

# ══════════════════════════════════════════════════════════
# 研究数据库 (serenity.db 中新增表)
# ══════════════════════════════════════════════════════════
RESEARCH_TABLES_SQL = """
    CREATE TABLE IF NOT EXISTS research_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        topic TEXT NOT NULL,
        source TEXT DEFAULT 'trendradar',
        mention_count INTEGER DEFAULT 1,
        sector TEXT,
        mapped_tickers TEXT DEFAULT '[]',
        sentiment TEXT DEFAULT 'neutral',
        is_actionable INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS research_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        topic TEXT,
        ticker TEXT NOT NULL,
        signal_type TEXT DEFAULT 'info',
        confidence REAL DEFAULT 0.5,
        source TEXT DEFAULT 'research_engine',
        reason TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_rt_date ON research_topics(date);
    CREATE INDEX IF NOT EXISTS idx_rs_date ON research_signals(date);
    CREATE INDEX IF NOT EXISTS idx_rs_ticker ON research_signals(ticker);
"""


# ══════════════════════════════════════════════════════════
# 研究引擎
# ══════════════════════════════════════════════════════════

class ResearchEngine:
    """自主研究引擎 — 信息采集 → 信号提取 → 策略融合"""

    def __init__(self):
        self._init_tables()

    def _init_tables(self):
        conn = get_conn()
        conn.executescript(RESEARCH_TABLES_SQL)
        conn.commit()
        conn.close()

    # ── 数据采集 ────────────────────────────────────────

    def load_trendradar_news(self, days: int = 3) -> list[dict]:
        """从 TrendRadar 本地 SQLite 读取最近 N 天的新闻"""
        all_news = []
        for i in range(days):
            d = (date.today() - timedelta(days=i)).isoformat()
            db_path = os.path.join(TR_DIR, f"{d}.db")
            if not os.path.exists(db_path):
                continue
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT title, platform_id, rank, url FROM news_items ORDER BY rank LIMIT 300"
                ).fetchall()
                conn.close()
                for r in rows:
                    all_news.append({"date": d, "title": r["title"], "platform": r["platform_id"], "rank": r["rank"], "url": r["url"] or ""})
            except Exception as e:
                log.warning(f"读取 TrendRadar {d}.db 失败: {e}")

        return all_news

    def filter_finance_news(self, news: list[dict]) -> list[dict]:
        """过滤出财经相关新闻"""
        finance_platforms = {"cls-hot", "wallstreetcn-hot", "xueqiu"}
        finance_keywords = [
            "股", "基金", "涨", "跌", "A股", "大盘", "板块", "涨停", "跌停",
            "券商", "银行", "保险", "地产", "新能源", "芯片", "半导体",
            "AI", "算力", "光模块", "机器人", "医药", "消费", "汽车",
            "央行", "利率", "政策", "GDP", "PMI", "CPI", "人民币", "美元",
            "IPO", "退市", "监管", "财报", "业绩", "分红", "回购",
            "科技", "互联网", "华为", "苹果", "特斯拉", "英伟达",
            "债券", "期货", "黄金", "原油", "比特币", "加密",
        ]
        result = []
        for item in news:
            title = item.get("title", "")
            if item.get("platform") in finance_platforms:
                result.append(item)
                continue
            if any(kw in title for kw in finance_keywords):
                result.append(item)
        return result

    # ── 信号提取 ────────────────────────────────────────

    def extract_signals_from_news(self, news: list[dict]) -> list[dict]:
        """从新闻标题提取: ticker (代码+名称匹配), direction, confidence, topic"""
        global STOCK_NAME_MAP
        if not STOCK_NAME_MAP:
            STOCK_NAME_MAP = _build_name_map()

        signals = []
        for item in news:
            title = item.get("title", "")
            # 提取标的代码
            tickers = set(TICKER_RE.findall(title))
            # 提取标的名称
            for name, code in STOCK_NAME_MAP.items():
                if name in title:
                    tickers.add(code)

            # 提取方向
            bullish = sum(1 for kw in BULLISH_KEYWORDS if kw in title)
            bearish = sum(1 for kw in BEARISH_KEYWORDS if kw in title)
            signal_type = "info"
            if bullish > bearish:
                signal_type = "bullish"
            elif bearish > bullish:
                signal_type = "bearish"

            # 提取话题
            topics = []
            for topic in TOPIC_SECTOR_TICKER_MAP:
                if topic in title:
                    topics.append(topic)

            # 置信度
            confidence = min(0.9, 0.3 + (bullish + bearish) * 0.15)
            if tickers:
                confidence += 0.2

            if tickers or topics or signal_type != "info":
                signals.append({
                    "title": title,
                    "tickers": list(tickers),
                    "signal_type": signal_type,
                    "confidence": min(confidence, 0.9),
                    "topics": topics,
                    "source": f"{item.get('platform','')}:{item.get('date','')}",
                    "url": item.get("url", ""),
                })

        return signals

    # ── 映射引擎 ────────────────────────────────────────

    def map_topic_to_tickers(self, topic: str) -> dict:
        """话题 → (板块, 标的列表) 三级映射"""
        # 直接匹配
        if topic in TOPIC_SECTOR_TICKER_MAP:
            sector, tickers = TOPIC_SECTOR_TICKER_MAP[topic]
            return {"topic": topic, "sector": sector, "tickers": tickers, "match": "exact"}

        # 模糊匹配
        for key, (sector, tickers) in TOPIC_SECTOR_TICKER_MAP.items():
            if key in topic or topic in key:
                return {"topic": topic, "sector": sector, "tickers": tickers, "match": "fuzzy"}

        return {"topic": topic, "sector": "其他", "tickers": [], "match": "none"}

    def get_mapped_topics(self, news: list[dict] = None) -> list[dict]:
        """从新闻中提取话题，映射到标的，返回影响分析"""
        signals = self.extract_signals_from_news(news or [])
        topic_counter = Counter()
        ticker_signals = {}  # code → {bullish: n, bearish: n, topics: set}

        for sig in signals:
            for topic in sig.get("topics", []):
                topic_counter[topic] += 1

            for ticker in sig.get("tickers", []):
                if ticker not in ticker_signals:
                    ticker_signals[ticker] = {"bullish": 0, "bearish": 0, "topics": set()}
                if sig["signal_type"] == "bullish":
                    ticker_signals[ticker]["bullish"] += 1
                elif sig["signal_type"] == "bearish":
                    ticker_signals[ticker]["bearish"] += 1
                ticker_signals[ticker]["topics"].update(sig.get("topics", []))

        # 热门话题 Top 20
        top_topics = [{"topic": t, "count": c, "mapping": self.map_topic_to_tickers(t)}
                      for t, c in topic_counter.most_common(30)]

        # 只保留能映射到具体标的的话题
        actionable = [t for t in top_topics if t["mapping"]["tickers"]]

        return {
            "top_topics": top_topics[:20],
            "actionable_topics": actionable[:15],
            "ticker_signals": {k: {
                "bullish": v["bullish"], "bearish": v["bearish"],
                "net": v["bullish"] - v["bearish"],
                "topics": list(v["topics"])
            } for k, v in ticker_signals.items()},
            "total_news_analyzed": len(news or []),
            "total_signals": len(signals),
        }

    # ── 持久化 ──────────────────────────────────────────

    def persist_research(self, mapped: dict):
        """将研究结果写入 research_topics + research_signals 表"""
        today = date.today().isoformat()
        conn = get_conn()

        # 清空今日旧数据
        conn.execute("DELETE FROM research_topics WHERE date=?", (today,))
        conn.execute("DELETE FROM research_signals WHERE date=?", (today,))

        # 写入话题
        for t in mapped.get("actionable_topics", []):
            tickers = t["mapping"]["tickers"]
            if tickers:
                conn.execute(
                    "INSERT INTO research_topics (date, topic, source, mention_count, sector, mapped_tickers, sentiment, is_actionable) VALUES (?,?,?,?,?,?,?,?)",
                    (today, t["topic"], "trendradar", t["count"], t["mapping"]["sector"],
                     json.dumps(tickers, ensure_ascii=False), "neutral", 1 if tickers else 0)
                )

        # 写入标的信号
        for code, sig in mapped.get("ticker_signals", {}).items():
            signal_type = "bullish" if sig["net"] > 0 else ("bearish" if sig["net"] < 0 else "neutral")
            confidence = min(0.8, abs(sig["net"]) * 0.15 + 0.3)
            conn.execute(
                "INSERT INTO research_signals (date, topic, ticker, signal_type, confidence, source, reason) VALUES (?,?,?,?,?,?,?)",
                (today, ",".join(sig["topics"][:3]), code, signal_type, confidence,
                 "research_engine", f"新闻提及 {sig['bullish']+sig['bearish']} 次")
            )

        conn.commit()
        conn.close()
        log.info(f"研究结果已持久化: {len(mapped.get('actionable_topics',[]))}话题, {len(mapped.get('ticker_signals',{}))}标的")

    def sync_to_sentinel(self):
        """将研究信号同步到 sentinel_observations (source_id='research_engine')"""
        today = date.today().isoformat()
        conn = get_conn()
        signals = conn.execute(
            "SELECT * FROM research_signals WHERE date=? AND confidence >= 0.4",
            (today,)
        ).fetchall()
        conn.close()

        from sentinel_engine import get_sentinel
        engine = get_sentinel()

        synced = 0
        for s in signals:
            # 检查是否已存在
            existing = engine.get_recent_observations(source_id="research_engine", hours=24, limit=50)
            already = any(
                (o.get("content_raw") or "").startswith(s["ticker"])
                for o in existing
            )
            if not already:
                engine.record_observation(
                    "research_engine",
                    f"[{s['signal_type']}] {s['ticker']}: {s['reason']} (置信{s['confidence']:.0%})",
                    signal_type=s["signal_type"],
                    tickers=[s["ticker"]],
                    topics=(s["topic"] or "").split(",") if s["topic"] else [],
                    confidence=s["confidence"],
                    impact_score=(1 if s["signal_type"] == "bullish" else -1) * s["confidence"] * 3
                )
                synced += 1

        if synced > 0:
            log.info(f"研究信号已同步到哨兵: {synced} 条")

    # ── 早报 / 周报 ─────────────────────────────────────

    def generate_daily_brief(self) -> str:
        """从研究数据生成结构化早报"""
        today = date.today().isoformat()
        conn = get_conn()
        topics = conn.execute(
            "SELECT * FROM research_topics WHERE date=? AND is_actionable=1 ORDER BY mention_count DESC LIMIT 15",
            (today,)
        ).fetchall()
        signals = conn.execute(
            "SELECT * FROM research_signals WHERE date=? ORDER BY confidence DESC LIMIT 20",
            (today,)
        ).fetchall()
        conn.close()

        now = datetime.now().strftime("%m/%d %H:%M")
        lines = [f"📡 **Serenity 自主研究早报** {now}"]
        lines.append("")

        # 热门话题
        if topics:
            lines.append("🔥 **今日热门话题**")
            for t in topics[:10]:
                ticker_str = ", ".join(json.loads(t["mapped_tickers"])[:4]) if t["mapped_tickers"] else "—"
                lines.append(f"  • {t['topic']} (提及{t['mention_count']}次) → {t['sector']} [{ticker_str}]")
            lines.append("")

        # 标的信号
        if signals:
            lines.append("📊 **标的信号摘要**")
            bullish_signals = [s for s in signals if s["signal_type"] == "bullish"][:5]
            bearish_signals = [s for s in signals if s["signal_type"] == "bearish"][:5]

            if bullish_signals:
                lines.append("  🟢 看多:")
                for s in bullish_signals:
                    lines.append(f"    {s['ticker']} ({s['confidence']:.0%}) — {s['reason']}")
            if bearish_signals:
                lines.append("  🔴 看空:")
                for s in bearish_signals:
                    lines.append(f"    {s['ticker']} ({s['confidence']:.0%}) — {s['reason']}")
            lines.append("")

        # 对持仓池影响
        from config import ALL_CODES, STOCK_MAP
        impacted = []
        for s in signals:
            if s["ticker"] in ALL_CODES:
                name = STOCK_MAP.get(s["ticker"], {}).get("name", s["ticker"])
                impacted.append(f"  {name}({s['ticker']}): {'🟢' if s['signal_type']=='bullish' else '🔴'} {s['reason']}")

        if impacted:
            lines.append("🎯 **对持仓池影响**")
            lines.extend(impacted[:8])
        else:
            lines.append("🎯 **对持仓池影响**: 暂无明显新闻信号")

        lines.append("")
        lines.append(f"🤖 Serenity 研究引擎 · {now}")
        return "\n".join(lines)

    def generate_weekly_review(self) -> str:
        """生成周报: 本周热点趋势 + 研究信号准确性回顾"""
        conn = get_conn()
        week_ago = (date.today() - timedelta(days=7)).isoformat()

        # 本周热门话题
        topics = conn.execute(
            "SELECT topic, SUM(mention_count) as total, sector FROM research_topics WHERE date >= ? GROUP BY topic ORDER BY total DESC LIMIT 15",
            (week_ago,)
        ).fetchall()

        # 本周信号统计
        sig_stats = conn.execute(
            "SELECT signal_type, COUNT(*) as cnt, AVG(confidence) as avg_conf FROM research_signals WHERE date >= ? GROUP BY signal_type",
            (week_ago,)
        ).fetchall()

        # 信源绩效
        from sentinel_engine import get_sentinel
        perf = get_sentinel().get_source_performance(days=7)

        conn.close()

        lines = ["📡 **Serenity 自主研究周报**"]
        lines.append(f"📅 {(date.today() - timedelta(days=7)).strftime('%m/%d')} → {date.today().strftime('%m/%d')}")
        lines.append("")

        lines.append("🔥 **本周热门话题 TOP10**")
        for t in topics[:10]:
            lines.append(f"  • {t['topic']} ({t['total']}次) — {t['sector']}")
        lines.append("")

        lines.append("📊 **本周信号统计**")
        for s in sig_stats:
            emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(s["signal_type"], "⚪")
            lines.append(f"  {emoji} {s['signal_type']}: {s['cnt']}条, 均置信 {s['avg_conf']:.0%}")
        lines.append("")

        lines.append("🏆 **信源准确率排行**")
        for p in perf[:6]:
            icon = "🟢" if p.get("accuracy", 0) >= 50 else "🔴"
            lines.append(f"  {icon} {p['name']}: {p['accuracy']}% ({p['total']}次)")

        lines.append("")
        lines.append("🤖 Serenity 自主进化系统 · 周报")
        return "\n".join(lines)

    # ── 全流程 ──────────────────────────────────────────

    def run_daily_research(self) -> dict:
        """执行每日全流程: 采集 → 提取 → 映射 → 持久化 → 同步哨兵"""
        log.info("自主研究引擎: 开始每日研究流程")

        # 1. 采集
        news = self.load_trendradar_news(days=2)
        finance_news = self.filter_finance_news(news)
        log.info(f"  采集: {len(news)}条 → 财经过滤: {len(finance_news)}条")

        # 2. 提取 + 映射
        mapped = self.get_mapped_topics(finance_news)
        log.info(f"  提取: {mapped['total_signals']}个信号, {len(mapped['actionable_topics'])}个可操作话题")

        # 3. 持久化
        self.persist_research(mapped)

        # 4. 同步到哨兵
        self.sync_to_sentinel()

        # 5. 生成早报
        brief = self.generate_daily_brief()
        log.info(f"  早报: {len(brief)}字符")

        return {"news_count": len(finance_news), "signals": mapped["total_signals"],
                "actionable_topics": len(mapped["actionable_topics"]), "brief": brief}


# 全局单例
_engine: Optional[ResearchEngine] = None


def get_research_engine() -> ResearchEngine:
    global _engine
    if _engine is None:
        _engine = ResearchEngine()
    return _engine


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Serenity 自主研究引擎")
    ap.add_argument("--daily", action="store_true", help="执行每日研究流程")
    ap.add_argument("--brief", action="store_true", help="生成今日早报")
    ap.add_argument("--weekly", action="store_true", help="生成周报")
    ap.add_argument("--topics", action="store_true", help="查看热门话题映射")
    ap.add_argument("--sync", action="store_true", help="同步研究信号到哨兵")

    args = ap.parse_args()
    engine = get_research_engine()

    if args.daily:
        result = engine.run_daily_research()
        print(f"✅ 研究完成: {result['news_count']}条新闻, {result['signals']}个信号")
        print(result['brief'])

    elif args.brief:
        engine.run_daily_research()
        print(engine.generate_daily_brief())

    elif args.weekly:
        print(engine.generate_weekly_review())

    elif args.topics:
        news = engine.load_trendradar_news(days=2)
        finance = engine.filter_finance_news(news)
        mapped = engine.get_mapped_topics(finance)
        for t in mapped["actionable_topics"]:
            tickers = t["mapping"]["tickers"]
            print(f"  {t['topic']} ({t['count']}次) → {t['mapping']['sector']} → {tickers}")

    elif args.sync:
        engine.sync_to_sentinel()
        print("✅ 研究信号已同步到哨兵")

    else:
        ap.print_help()
