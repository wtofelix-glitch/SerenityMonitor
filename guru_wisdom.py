#!/usr/bin/env python3
"""
guru_wisdom — 投资大师智慧采集与Serenity集成层

三层架构：
  Layer 1: 大师数据库 + 画像定义
  Layer 2: 多源采集引擎（GoogleNews / X / Oaktree / xueqiu）
  Layer 3: Serenity集成（scorer因子 / 日报 / 看板）

Usage:
    python3 guru_wisdom.py collect          # 全量采集所有大师
    python3 guru_wisdom.py collect --guru 段永平  # 单人大师采集
    python3 guru_wisdom.py seed             # 种子数据（历史名言）
    python3 guru_wisdom.py status           # 当前状态概览
    python3 guru_wisdom.py report           # 大师信号简报
"""
import sqlite3, json, time, re, os, sys
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import urllib.request, urllib.parse, urllib.error
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guru_wisdom")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guru_wisdom.db")

# ─── 大师画像定义 ────────────────────────────────────────────

@dataclass
class GuruProfile:
    id: str                    # 英文ID
    name: str                  # 英文名
    cn_name: str               # 中文名
    short_bio: str             # 一句话简介
    category: str              # value / growth / macro / quant / distressed
    region: str                # china / us
    sources: dict = field(default_factory=dict)  # {"type": "target"}
    # source types: google_news_keyword, x_account, website_rss, xueqiu_uid, weibo_uid
    active: bool = True
    influence: int = 5         # 1-10

GURUS = {
    "duanyongping": GuruProfile(
        id="duanyongping", name="Duan Yongping", cn_name="段永平",
        short_bio="步步高/OPPO/vivo创始人，价值投资信徒，雪球活跃用户'大道无形我有型'",
        category="value", region="china",
        sources={"google_news_keyword": "段永平 投资 观点", "xueqiu_user": "大道无形我有型"},
        influence=9,
    ),
    "buffett": GuruProfile(
        id="buffett", name="Warren Buffett", cn_name="沃伦·巴菲特",
        short_bio="伯克希尔·哈撒韦CEO，价值投资之父，每年致股东信全球关注",
        category="value", region="us",
        sources={"google_news_keyword": "Warren Buffett investment", "website_rss": "https://www.berkshirehathaway.com/"},  # noqa
        influence=10,
    ),
    "munger": GuruProfile(
        id="munger", name="Charlie Munger", cn_name="查理·芒格",
        short_bio="伯克希尔副董事长，巴菲特搭档，以跨学科思维和多模型框架闻名",
        category="value", region="us",
        sources={"google_news_keyword": "Charlie Munger investing"},
        influence=9,
    ),
    "howardmarks": GuruProfile(
        id="howardmarks", name="Howard Marks", cn_name="霍华德·马克斯",
        short_bio="橡树资本联合创始人，备忘录作家，以周期理论和风险控制闻名",
        category="distressed", region="us",
        sources={"google_news_keyword": "Howard Marks memo", "website_memos": "https://www.oaktreecapital.com/insights/howard-marks-memos"},
        influence=8,
    ),
    "ackman": GuruProfile(
        id="ackman", name="Bill Ackman", cn_name="比尔·阿克曼",
        short_bio="Pershing Square CEO，激进投资者，活跃Twitter发言人",
        category="activist", region="us",
        sources={"google_news_keyword": "Bill Ackman Pershing Square", "x_account": "BillAckman"},
        influence=7,
    ),
    "dalio": GuruProfile(
        id="dalio", name="Ray Dalio", cn_name="瑞·达利欧",
        short_bio="桥水基金创始人，《原则》作者，宏观投资大师",
        category="macro", region="us",
        sources={"google_news_keyword": "Ray Dalio principles investing"},
        influence=8,
    ),
    "lilu": GuruProfile(
        id="lilu", name="Li Lu", cn_name="李录",
        short_bio="喜马拉雅资本创始人，查理·芒格家族资产管理人，《文明、现代化、价值投资与中国》作者",
        category="value", region="china",
        sources={"google_news_keyword": "李录 投资"},
        influence=7,
    ),
    "danbin": GuruProfile(
        id="danbin", name="Dan Bin", cn_name="但斌",
        short_bio="东方港湾投资董事长，中国价值投资代表人物，长期重仓茅台",
        category="value", region="china",
        sources={"google_news_keyword": "但斌 东方港湾 茅台"},
        influence=6,
    ),
    "linyuan": GuruProfile(
        id="linyuan", name="Lin Yuan", cn_name="林园",
        short_bio="林园投资董事长，中国民间股神代表，重仓消费+医药",
        category="value", region="china",
        sources={"google_news_keyword": "林园 投资 观点"},
        influence=6,
    ),
    "burry": GuruProfile(
        id="burry", name="Michael Burry", cn_name="迈克尔·巴里",
        short_bio="Scion Asset Management创始人，《大空头》原型，擅长做空泡沫",
        category="distressed", region="us",
        sources={"google_news_keyword": "Michael Burry Scion 13F"},
        influence=7,
    ),
    "druckenmiller": GuruProfile(
        id="druckenmiller", name="Stanley Druckenmiller", cn_name="斯坦利·德鲁肯米勒",
        short_bio="Duquesne Family Office创始人，索罗斯量子基金前操盘手，宏观交易大师",
        category="macro", region="us",
        sources={"google_news_keyword": "Stanley Druckenmiller portfolio"},
        influence=7,
    ),
    "tepper": GuruProfile(
        id="tepper", name="David Tepper", cn_name="大卫·泰珀",
        short_bio="Appaloosa Management创始人，擅长困境反转和价值投资",
        category="distressed", region="us",
        sources={"google_news_keyword": "David Tepper Appaloosa 13F"},
        influence=6,
    ),
    "cathiewood": GuruProfile(
        id="cathiewood", name="Cathie Wood", cn_name="凯西·伍德",
        short_bio="ARK Invest CEO，专注颠覆性创新投资（AI/机器人/基因/区块链）",
        category="growth", region="us",
        sources={"google_news_keyword": "Cathie Wood ARK Invest"},
        influence=6,
    ),
}

XUEQIU_UID_MAP = {
    "大道无形我有型": "871508679",
}

STOCK_CODE_MAP = {
    "茅台": "600519", "贵州茅台": "600519",
    "五粮液": "000858",
    "腾讯": "600519",  # placeholder - 腾讯是港股
    "比亚迪": "002594",
    "宁德时代": "300750",
    "长江电力": "600900",
    "中国平安": "601318",
    "招商银行": "600036",
    "美的集团": "000333",
    "格力电器": "000651",
    "伊利股份": "600887",
    "海康威视": "002415",
    "万华化学": "600309",
    "药明康德": "603259",
    "恒瑞医药": "600276",
    "迈瑞医疗": "300760",
    "苹果": "AAPL", "Apple": "AAPL",
    "微软": "MSFT", "Microsoft": "MSFT",
    "谷歌": "GOOGL", "Google": "GOOGL",
    "亚马逊": "AMZN", "Amazon": "AMZN",
    "英伟达": "NVDA", "NVIDIA": "NVDA", "Nvidia": "NVDA",
    "特斯拉": "TSLA", "Tesla": "TSLA",
    "Meta": "META", "meta": "META",
    "伯克希尔": "BRK.B",
    "高盛": "GS",
    "摩根大通": "JPM",
}


# ─── 数据库 ────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    """创建/迁移数据库"""
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gurus (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            cn_name TEXT NOT NULL,
            category TEXT,
            region TEXT,
            active INTEGER DEFAULT 1,
            last_collected_at TEXT
        );

        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guru_id TEXT NOT NULL REFERENCES gurus(id),
            content TEXT NOT NULL,
            topic TEXT,
            sentiment TEXT CHECK(sentiment IN ('bullish','bearish','neutral','contrarian')),
            source_url TEXT,
            source_date TEXT,
            relevant_stocks TEXT,  -- JSON array of codes
            context TEXT,
            collected_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(guru_id, content)
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guru_id TEXT NOT NULL REFERENCES gurus(id),
            stock_code TEXT,
            stock_name TEXT,
            direction TEXT CHECK(direction IN ('long','short','core','trade')),
            entry_date TEXT,
            exit_date TEXT,
            notes TEXT,
            source_url TEXT,
            reported_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS sentiment_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guru_id TEXT NOT NULL REFERENCES gurus(id),
            stock_code TEXT,
            score REAL CHECK(score BETWEEN -1 AND 1),
            date TEXT NOT NULL,
            source TEXT DEFAULT 'auto',
            notes TEXT,
            UNIQUE(guru_id, stock_code, date)
        );

        CREATE TABLE IF NOT EXISTS collection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guru_id TEXT REFERENCES gurus(id),
            source_type TEXT NOT NULL,
            status TEXT CHECK(status IN ('success','partial','failed')),
            items_collected INTEGER DEFAULT 0,
            notes TEXT,
            collected_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_quotes_guru ON quotes(guru_id);
        CREATE INDEX IF NOT EXISTS idx_quotes_topic ON quotes(topic);
        CREATE INDEX IF NOT EXISTS idx_sentiment_stock ON sentiment_scores(stock_code);
        CREATE INDEX IF NOT EXISTS idx_sentiment_guru ON sentiment_scores(guru_id);
    """)
    db.commit()

    # 种子大师数据
    for gid, gp in GURUS.items():
        db.execute(
            "INSERT OR IGNORE INTO gurus (id, name, cn_name, category, region, active) VALUES (?, ?, ?, ?, ?, ?)",
            (gid, gp.name, gp.cn_name, gp.category, gp.region, 1 if gp.active else 0),
        )
    db.commit()
    db.close()


# ─── 大师观点种子数据（历史名言） ──────────────────────────

SEED_QUOTES = [
    # 段永平
    ("duanyongping", "做对的事情，然后把事情做对。", "投资哲学", "neutral", None),
    ("duanyongping", "好的生意模式是：长长的坡，湿湿的雪。", "选股标准", "neutral", None),
    ("duanyongping", "买股票就是买公司。看不懂的公司不要碰。", "价值投资", "neutral", None),
    ("duanyongping", "本分、平常心。做企业是这样，做投资也是这样。", "投资哲学", "neutral", None),
    ("duanyongping", "我一般不投我不懂的东西。苹果我懂，茅台我懂。", "选股理念", "bullish"),
    ("duanyongping", "真正好的生意，就像苹果、茅台这样的，不需要太多管理。", "持仓观点", "bullish"),
    ("duanyongping", "市场恐慌的时候，往往是买入优质公司的最好时机。", "择时", "bullish"),
    ("duanyongping", "茅台这个价格不贵，拿着就好了。", "茅台", "bullish"),
    ("duanyongping", "苹果是最好理解的商业模式，我一直在买。", "苹果", "bullish"),
    ("duanyongping", "腾讯是我看得懂的生意，游戏+社交的护城河很深。", "腾讯", "bullish"),

    # 巴菲特
    ("buffett", "Be fearful when others are greedy, and greedy when others are fearful.", "投资哲学", "contrarian", None),
    ("buffett", "Rule No.1: Never lose money. Rule No.2: Never forget rule No.1.", "投资哲学", "neutral", None),
    ("buffett", "The best investment you can make is in yourself.", "人生哲学", "neutral", None),
    ("buffett", "Price is what you pay. Value is what you get.", "价值投资", "neutral", None),
    ("buffett", "It's far better to buy a wonderful company at a fair price than a fair company at a wonderful price.", "选股理念", "neutral", None),
    ("buffett", "The stock market is a device for transferring money from the impatient to the patient.", "投资哲学", "neutral", None),
    ("buffett", "只有当潮水退去时，你才知道谁在裸泳。", "风险警示", "bearish", None),
    ("buffett", "永远不要做空美国。", "宏观观点", "bullish", None),
    ("buffett", "Our favourite holding period is forever.", "投资哲学", "neutral", None),

    # 芒格
    ("munger", "Invert, always invert.", "思维模型", "neutral", None),
    ("munger", "The best way to get what you want is to deserve what you want.", "人生哲学", "neutral", None),
    ("munger", "Show me the incentive and I will show you the outcome.", "行为金融", "neutral", None),
    ("munger", "I have never known a wise person who didn't read all the time.", "学习", "neutral", None),
    ("munger", "The big money is not in the buying and selling, but in the waiting.", "投资哲学", "neutral", None),
    ("munger", "If you can't handle the volatility, you don't deserve the returns.", "风险", "neutral", None),
    ("munger", "A handful of opportunities is all you need in a lifetime.", "集中投资", "neutral", None),
    ("munger", "The most important rule of compounding is to never interrupt it unnecessarily.", "复利", "neutral", None),
    ("munger", "Forget what you know about buying fair businesses at wonderful prices; instead, buy wonderful businesses at fair prices.", "选股理念", "neutral", None),

    # 霍华德·马克斯
    ("howardmarks", "The biggest investing errors come not from factors that are informational or analytical, but from those that are psychological.", "行为金融", "neutral", None),
    ("howardmarks", "We may never know where we're going, but we'd better know where we are.", "周期理论", "neutral", None),
    ("howardmarks", "The key to superior performance is not being right more often than others, but being more right when you are right.", "投资哲学", "neutral", None),
    ("howardmarks", "When everyone thinks something is a sure thing, it probably isn't.", "反向思维", "contrarian", None),
    ("howardmarks", "Risk is not inherent in an investment; it's always relative to the price paid.", "风险", "neutral", None),
    ("howardmarks", "The most dangerous thing is the belief that risk has been eliminated.", "风险警示", "bearish", None),
    ("howardmarks", "We are in the eighth inning of a long ballgame, and the bull market has been going on for a long time.", "周期判断", "bearish", None),

    # 比尔·阿克曼
    ("ackman", "The best investments come from complex situations that are misunderstood by the market.", "投资哲学", "neutral", None),
    ("ackman", "When you find a wonderful business at a discount, bet big.", "集中投资", "bullish", None),

    # 瑞·达利欧
    ("dalio", "Principles are fundamental truths that serve as the foundations for behavior.", "人生哲学", "neutral", None),
    ("dalio", "The biggest mistake most people make is to believe that what worked well in the recent past will work well in the future.", "宏观", "neutral", None),
    ("dalio", "如果你不觉得一年前的自己是个蠢货，那说明你这年没学到什么东西。", "学习", "neutral", None),
    ("dalio", "Debt cycles drive the economy. Understanding where we are in the cycle is the key.", "宏观周期", "neutral", None),

    # 李录
    ("lilu", "价值投资的核心：股票是公司的所有权，市场是为你服务的而不是指导你的。", "价值投资", "neutral", None),
    ("lilu", "现代化就是自由市场经济+现代科技。", "宏观", "neutral", None),
    ("lilu", "中国未来20年的核心是科技创新和内需消费。", "中国观点", "bullish", None),

    # 但斌
    ("danbin", "投资就是投确定性。茅台是中国最好的确定性。", "选股理念", "bullish", None),
    ("danbin", "长期持有优质企业，与伟大企业共成长。", "投资哲学", "neutral", None),
    ("danbin", "时间是最好的朋友，复利是第八大奇迹。", "复利", "neutral", None),

    # 林园
    ("linyuan", "投资要投'垄断'，没有垄断就没有超额利润。", "选股理念", "neutral", None),
    ("linyuan", "我只买嘴巴有关的——吃喝拉撒、医药健康。", "消费投资", "bullish", None),
    ("linyuan", "A股未来20年，核心资产会跑赢绝大多数股票。", "A股观点", "bullish", None),

    # 迈克尔·巴里
    ("burry", "The market can remain irrational longer than you can remain solvent.", "风险警示", "bearish", None),
    ("burry", "The biggest risks are the ones nobody is talking about.", "风险", "contrarian", None),
    ("burry", "Index funds have created a bubble in passive investing.", "宏观", "bearish", None),
]


def seed_quotes(db=None):
    """插入种子名言"""
    close = False
    if db is None:
        db = get_db()
        close = True
    count = 0
    for gid, content, topic, sentiment, stock in SEED_QUOTES:
        try:
            db.execute(
                "INSERT OR IGNORE INTO quotes (guru_id, content, topic, sentiment, source_date) VALUES (?, ?, ?, ?, ?)",
                (gid, content, topic, sentiment, "2000-01-01"),
            )
            count += 1
        except Exception:
            pass
    db.commit()
    if close:
        db.close()
    return count


# ─── 采集引擎 ──────────────────────────────────────────────

def fetch_google_news(guru: GuruProfile, max_results: int = 5) -> list[dict]:
    """通过Google新闻搜索大师最新观点"""
    keyword = guru.sources.get("google_news_keyword")
    if not keyword:
        return []

    query = urllib.parse.quote(keyword)
    url = f"https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN"
    results = []

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        # 简易RSS解析
        items = re.findall(r"<item>(.*?)</item>", html, re.DOTALL)
        for item in items[:max_results]:
            title = re.search(r"<title>(.*?)</title>", item)
            link = re.search(r"<link>(.*?)</link>", item)
            pubdate = re.search(r"<pubDate>(.*?)</pubDate>", item)
            if title and link:
                results.append({
                    "title": title.group(1).strip(),
                    "url": link.group(1).strip(),
                    "date": pubdate.group(1).strip() if pubdate else "",
                })
    except Exception as e:
        log.warning(f"Google News fetch failed for {guru.cn_name}: {e}")

    return results


def fetch_oaktree_memos(max_results: int = 3) -> list[dict]:
    """抓取Howard Marks最新备忘录（橡树资本官网）"""
    url = "https://www.oaktreecapital.com/insights/howard-marks-memos"
    results = []

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        # 找备忘录条目
        items = re.findall(
            r'<a[^>]*href="([^"]*memo[^"]*)"[^>]*>(.*?)</a>',
            html, re.IGNORECASE | re.DOTALL,
        )
        for href, title_html in items[:max_results]:
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            if title and len(title) > 5:
                full_url = href if href.startswith("http") else f"https://www.oaktreecapital.com{href}"
                results.append({"title": title, "url": full_url})
    except Exception as e:
        log.warning(f"Oaktree memo fetch failed: {e}")

    return results


def fetch_xueqiu_user(user_id: str, max_results: int = 10) -> list[dict]:
    """抓取雪球用户最新帖子的API方式

    使用雪球API: https://xueqiu.com/statuses/original/timeline.json?user_id={UID}&page=1
    需要设置Referer和User-Agent。如果API返回403/401，fallback到Google缓存抓取。
    """
    results = []
    cookies = os.environ.get("XUEQIU_COOKIES", "")

    # 方案一：API请求
    api_url = f"https://xueqiu.com/statuses/original/timeline.json?user_id={user_id}&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://xueqiu.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookies:
        headers["Cookie"] = cookies

    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        statuses = data.get("statuses", []) or data.get("list", [])
        for s in statuses[:max_results]:
            text = s.get("text", "") or s.get("content", "")
            # 去除HTML标签
            text = re.sub(r"<[^>]+>", "", text).strip()
            if not text:
                continue
            created_at = s.get("created_at", "")
            status_id = s.get("id") or s.get("status_id", "")
            results.append({
                "text": text,
                "date": created_at,
                "url": f"https://xueqiu.com/{user_id}/{status_id}" if status_id else "",
            })
        if results:
            return results
    except urllib.error.HTTPError as e:
        log.warning(f"Xueqiu API failed ({e.code}), falling back to cache fetch")
    except Exception as e:
        log.warning(f"Xueqiu API error: {e}, falling back to cache fetch")

    # 方案二：Google缓存抓取
    cache_url = f"http://webcache.googleusercontent.com/search?q=cache:xueqiu.com/{user_id}"
    try:
        req = urllib.request.Request(cache_url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        # 从缓存页面提取帖子文本
        # 雪球帖子的常见模式
        patterns = [
            r'<div class="status-item[^"]*"[^>]*>.*?<div[^>]*class="content[^"]*"[^>]*>(.*?)</div>',
            r'class="status-box[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>',
            r'<div[^>]*class="text[^"]*"[^>]*>(.*?)</div>',
        ]
        texts = []
        for pat in patterns:
            texts = re.findall(pat, html, re.DOTALL | re.IGNORECASE)
            if texts:
                break

        # 如果没匹配到，尝试提取所有可见文本
        if not texts:
            texts = re.findall(r">([^<]{20,})<", html)

        for t in texts[:max_results]:
            clean = re.sub(r"<[^>]+>", "", t).strip()
            if clean and len(clean) > 10:
                results.append({
                    "text": clean,
                    "date": "",
                    "url": f"https://xueqiu.com/{user_id}",
                })
    except Exception as e:
        log.warning(f"Xueqiu cache fetch failed: {e}")

    return results


def extract_sentiment(text: str) -> str:
    """简易情绪判断"""
    bullish_words = ["买入", "看好", "加仓", "低估", "机会", "bullish", "buy", "cheap",
                     "undervalued", "great opportunity", "strong buy"]
    bearish_words = ["卖出", "减仓", "高估", "泡沫", "风险", "bearish", "sell", "overvalued",
                     "bubble", "danger", "crash", "caution"]

    text_lower = text.lower()
    bull_score = sum(1 for w in bullish_words if w.lower() in text_lower)
    bear_score = sum(1 for w in bearish_words if w.lower() in text_lower)

    if bull_score > bear_score:
        return "bullish"
    elif bear_score > bull_score:
        return "bearish"
    return "neutral"


def extract_stock_codes(text: str) -> list[str]:
    """从文本中提取可能的股票代码/名称"""
    found = []
    for name, code in STOCK_CODE_MAP.items():
        if name in text:
            found.append(code)
    # 匹配A股代码格式 (6位数字)
    codes = re.findall(r"\b(?:600\d{3}|000\d{3}|002\d{3}|603\d{3}|605\d{3}|300\d{3}|688\d{3})\b", text)
    found.extend(codes)
    return list(set(found))


def collect_guru(guru_id: str, db=None) -> dict:
    """执行单个大师的采集"""
    if guru_id not in GURUS:
        return {"status": "failed", "error": f"Unknown guru: {guru_id}"}

    guru = GURUS[guru_id]
    close_db = False
    if db is None:
        db = get_db()
        close_db = True

    total_items = 0
    errors = []

    # 1. Google News采集
    news = fetch_google_news(guru)
    for item in news:
        title = item["title"]
        sentiment = extract_sentiment(title)
        stocks = extract_stock_codes(title)
        try:
            db.execute(
                "INSERT OR IGNORE INTO quotes (guru_id, content, topic, sentiment, source_url, source_date, relevant_stocks) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guru_id, title, "news", sentiment, item["url"], item.get("date", ""),
                 json.dumps(stocks, ensure_ascii=False) if stocks else None),
            )
            total_items += 1
        except Exception:
            pass

    # 2. Howard Marks特有：橡树备忘录
    if guru_id == "howardmarks":
        memos = fetch_oaktree_memos()
        for memo in memos:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO quotes (guru_id, content, topic, sentiment, source_url, source_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (guru_id, memo["title"], "memo", "neutral", memo["url"], ""),
                )
                total_items += 1
            except Exception:
                pass

    # 3. 雪球采集
    xueqiu_target = guru.sources.get("xueqiu_user")
    if xueqiu_target:
        xueqiu_uid = XUEQIU_UID_MAP.get(xueqiu_target, xueqiu_target) or xueqiu_target
        posts = fetch_xueqiu_user(xueqiu_uid)
        for post in posts:
            try:
                # 去重检查
                exists = db.execute("SELECT id FROM quotes WHERE guru_id=? AND content=? AND source='xueqiu'",
                                    (guru_id, post["text"])).fetchone()
                if exists:
                    continue
                sentiment = extract_sentiment(post["text"])
                stocks = extract_stock_codes(post["text"])
                db.execute(
                    "INSERT OR IGNORE INTO quotes (guru_id, content, topic, sentiment, source_url, source_date, relevant_stocks) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (guru_id, post["text"], "xueqiu", sentiment, post["url"], post.get("date", ""),
                     json.dumps(stocks, ensure_ascii=False) if stocks else None),
                )
                total_items += 1
            except Exception:
                pass

    # 更新采集时间
    db.execute("UPDATE gurus SET last_collected_at = datetime('now','localtime') WHERE id = ?", (guru_id,))
    db.commit()

    status = "success" if total_items > 0 else "partial"
    db.execute(
        "INSERT INTO collection_log (guru_id, source_type, status, items_collected, notes) VALUES (?, ?, ?, ?, ?)",
        (guru_id, "auto", status, total_items, "; ".join(errors[:3]) if errors else ""),
    )
    db.commit()

    if close_db:
        db.close()

    return {"status": status, "guru": guru.cn_name, "items": total_items}


def collect_all(db=None):
    """全量采集"""
    results = []
    for gid in GURUS:
        log.info(f"采集 {GURUS[gid].cn_name}...")
        result = collect_guru(gid, db)
        results.append(result)
        time.sleep(0.5)  # 礼貌间隔
    return results


# ─── Serenity 集成 API ────────────────────────────────────

def get_guru_sentiment(stock_code: str, days: int = 30) -> dict:
    """获取指定股票的大师综合情绪"""
    db = get_db()
    rows = db.execute("""
        SELECT q.guru_id, q.sentiment, q.content, g.cn_name as guru_name
        FROM quotes q JOIN gurus g ON q.guru_id = g.id
        WHERE q.relevant_stocks LIKE ? OR q.content LIKE ?
        ORDER BY q.collected_at DESC LIMIT 20
    """, (f'%{stock_code}%', f'%{stock_code}%')).fetchall()

    if not rows:
        db.close()
        return {"stock_code": stock_code, "gurus_count": 0,
                "bullish": 0, "bearish": 0, "neutral": 0, "net_score": 0.0, "quotes": []}

    sentiments = [r["sentiment"] for r in rows]
    bullish = sum(1 for s in sentiments if s == "bullish")
    bearish = sum(1 for s in sentiments if s == "bearish")
    neutral = sum(1 for s in sentiments if s == "neutral")

    db.close()
    return {
        "stock_code": stock_code,
        "gurus_count": len(set(r["guru_id"] for r in rows)),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "net_score": (bullish - bearish) / max(len(sentiments), 1),
        "quotes": [{
            "guru": r["guru_name"],
            "content": r["content"][:80],
            "sentiment": r["sentiment"]
        } for r in rows[:5]],
    }


def get_guru_factor(stock_code: str) -> float:
    """返回大师因子分值 [-0.05, +0.05]，供scorer.py集成"""
    sentiment = get_guru_sentiment(stock_code)
    if sentiment["gurus_count"] == 0:
        return 0.0
    return sentiment["net_score"] * 0.05  # 最高+-0.05分


def get_recent_quotes(limit: int = 10) -> list[dict]:
    """获取最新大师语录"""
    db = get_db()
    rows = db.execute("""
        SELECT g.cn_name, q.content, q.topic, q.sentiment, q.source_date, q.collected_at
        FROM quotes q JOIN gurus g ON q.guru_id = g.id
        ORDER BY q.collected_at DESC LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_guru_stock_mentions(stock_code: str) -> list[dict]:
    """查询某只股票被哪些大师提及过"""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT g.cn_name, q.content, q.sentiment, q.source_date
        FROM quotes q JOIN gurus g ON q.guru_id = g.id
        WHERE q.relevant_stocks LIKE ? OR q.content LIKE ?
        ORDER BY q.source_date DESC LIMIT 20
    """, (f'%{stock_code}%', f'%{stock_code}%')).fetchall()
    db.close()
    return [dict(r) for r in rows]


def generate_report() -> str:
    """生成大师信号简报（用于daily_report集成）"""
    db = get_db()
    rows = db.execute("""
        SELECT g.cn_name, q.content, q.sentiment, q.topic
        FROM quotes q JOIN gurus g ON q.guru_id = g.id
        WHERE q.collected_at >= datetime('now', '-7 days', 'localtime')
        ORDER BY q.collected_at DESC LIMIT 15
    """).fetchall()

    if not rows:
        # fallback到最新随机语录
        rows = db.execute("""
            SELECT g.cn_name, q.content, q.sentiment, q.topic
            FROM quotes q JOIN gurus g ON q.guru_id = g.id
            ORDER BY RANDOM() LIMIT 10
        """).fetchall()

    db.close()

    bullish = [r for r in rows if r["sentiment"] == "bullish"]
    bearish = [r for r in rows if r["sentiment"] == "bearish"]
    neutral = [r for r in rows if r["sentiment"] == "neutral"]

    parts = ["📜 大师智慧信号"]
    parts.append("=" * 30)

    if bullish:
        parts.append(f"\n🟢 看多观点 ({len(bullish)}条):")
        for r in bullish[:5]:
            parts.append(f"  · {r['cn_name']}: {r['content'][:60]}")

    if bearish:
        parts.append(f"\n🔴 看空/谨慎 ({len(bearish)}条):")
        for r in bearish[:3]:
            parts.append(f"  · {r['cn_name']}: {r['content'][:60]}")

    if neutral:
        parts.append(f"\n⚪ 智慧箴言 ({len(neutral)}条):")
        for r in neutral[:3]:
            parts.append(f"  · {r['cn_name']}: {r['content'][:60]}")

    parts.append(f"\n📊 共收录 {get_stats()['total_quotes']} 条大师语录")
    return "\n".join(parts)


def status() -> dict:
    """系统状态概览"""
    db = get_db()
    stats = {
        "gurus": db.execute("SELECT count(*) FROM gurus WHERE active=1").fetchone()[0],
        "total_quotes": db.execute("SELECT count(*) FROM quotes").fetchone()[0],
        "recent_quotes_7d": db.execute(
            "SELECT count(*) FROM quotes WHERE collected_at >= datetime('now', '-7 days', 'localtime')"
        ).fetchone()[0],
        "oaktree_memos": db.execute(
            "SELECT count(*) FROM quotes WHERE guru_id='howardmarks' AND topic='memo'"
        ).fetchone()[0],
        "last_collection": db.execute(
            "SELECT MAX(collected_at) FROM collection_log WHERE status='success'"
        ).fetchone()[0] or "never",
        "sentiment_distribution": {
            "bullish": db.execute("SELECT count(*) FROM quotes WHERE sentiment='bullish'").fetchone()[0],
            "bearish": db.execute("SELECT count(*) FROM quotes WHERE sentiment='bearish'").fetchone()[0],
            "neutral": db.execute("SELECT count(*) FROM quotes WHERE sentiment='neutral'").fetchone()[0],
        }
    }
    db.close()
    return stats


def get_stats():
    return status()


# ─── 可视化 ─────────────────────────────────────────────────

def dashboard_html() -> str:
    """生成大师智慧看板HTML片段"""
    stats = status()
    recent = get_recent_quotes(8)

    guru_rows = ""
    s = stats["sentiment_distribution"]
    total = s["bullish"] + s["bearish"] + s["neutral"]
    bull_pct = round(s["bullish"] / max(total, 1) * 100)
    bear_pct = round(s["bearish"] / max(total, 1) * 100)

    # 语录HTML
    quotes_html = ""
    for q in recent:
        emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪", "contrarian": "🟣"}.get(q.get("sentiment", "neutral"), "⚪")
        quotes_html += f"""
        <div class="guru-quote" style="margin:8px 0;padding:10px;background:rgba(255,255,255,0.03);border-left:3px solid #C0392B;border-radius:0 6px 6px 0;">
            <div style="font-size:13px;color:#CCC;">{emoji} <strong>{q['cn_name']}</strong> · {q.get('topic','')}</div>
            <div style="font-size:14px;margin:4px 0;color:#EEE;">{q['content'][:80]}</div>
        </div>"""

    return f"""
    <div class="guru-dashboard" style="margin:16px 0;">
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;">
            <div style="flex:1;min-width:120px;background:rgba(255,255,255,0.05);border-radius:10px;padding:12px;text-align:center;">
                <div style="font-size:24px;color:#E74C3C;">{stats['total_quotes']}</div>
                <div style="font-size:11px;color:#999;">大师语录</div>
            </div>
            <div style="flex:1;min-width:120px;background:rgba(255,255,255,0.05);border-radius:10px;padding:12px;text-align:center;">
                <div style="font-size:24px;color:#2ECC71;">{bull_pct}%</div>
                <div style="font-size:11px;color:#999;">看多比例</div>
            </div>
            <div style="flex:1;min-width:120px;background:rgba(255,255,255,0.05);border-radius:10px;padding:12px;text-align:center;">
                <div style="font-size:24px;color:#E74C3C;">{bear_pct}%</div>
                <div style="font-size:11px;color:#999;">看空比例</div>
            </div>
            <div style="flex:1;min-width:120px;background:rgba(255,255,255,0.05);border-radius:10px;padding:12px;text-align:center;">
                <div style="font-size:24px;color:#F39C12;">{stats['gurus']}</div>
                <div style="font-size:11px;color:#999;">监控大师</div>
            </div>
        </div>
        <h4 style="color:#E74C3C;margin:8px 0;">📜 最新大师语录</h4>
        {quotes_html}
    </div>
    """


# ─── CLI ──────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 guru_wisdom.py init           # 初始化数据库")
        print("  python3 guru_wisdom.py seed            # 插入种子语录")
        print("  python3 guru_wisdom.py collect         # 全量采集")
        print("  python3 guru_wisdom.py collect --guru ID  # 单人采集")
        print("  python3 guru_wisdom.py status          # 状态概览")
        print("  python3 guru_wisdom.py report          # 大师信号简报")
        print("  python3 guru_wisdom.py sentiment 600519 # 查询股票大师情绪")
        print("  python3 guru_wisdom.py dashboard       # 看板HTML")
        return

    cmd = sys.argv[1]

    if cmd == "init":
        init_db()
        print(f"✅ 数据库初始化完成: {DB_PATH}")

    elif cmd == "seed":
        init_db()
        count = seed_quotes()
        print(f"✅ 插入 {count} 条种子语录")

    elif cmd == "collect":
        init_db()
        db = get_db()
        if "--guru" in sys.argv:
            idx = sys.argv.index("--guru") + 1
            gid = sys.argv[idx] if idx < len(sys.argv) else ""
            r = collect_guru(gid, db)
            print(f"✅ {r['guru']}: 采集 {r['items']} 条 | 状态: {r['status']}")
        else:
            results = collect_all(db)
            total = sum(r["items"] for r in results)
            success = sum(1 for r in results if r["status"] == "success")
            print(f"✅ 全量采集完成: {success}/{len(results)} 成功, 共 {total} 条")
        db.close()

    elif cmd == "status":
        init_db()
        s = status()
        print(f"📊 Guru Wisdom 状态")
        print(f"  · 监控大师: {s['gurus']} 位")
        print(f"  · 总语录: {s['total_quotes']} 条")
        print(f"  · 近7天: {s['recent_quotes_7d']} 条")
        print(f"  · 橡树备忘录: {s['oaktree_memos']} 篇")
        print(f"  · 上次采集: {s['last_collection']}")
        print(f"  · 情绪分布: 🟢{s['sentiment_distribution']['bullish']} / 🔴{s['sentiment_distribution']['bearish']} / ⚪{s['sentiment_distribution']['neutral']}")

    elif cmd == "report":
        init_db()
        print(generate_report())

    elif cmd == "sentiment" and len(sys.argv) >= 3:
        init_db()
        stock = sys.argv[2]
        ss = get_guru_sentiment(stock)
        print(f"📊 大师对 {stock} 的情绪:")
        print(f"  · {ss['gurus_count']} 位大师提及")
        print(f"  · 🟢 看多: {ss['bullish']}  🔴 看空: {ss['bearish']}  ⚪ 中性: {ss['neutral']}")
        print(f"  · 净分值: {ss['net_score']:.2f}")
        for q in ss["quotes"]:
            print(f"  {q['guru']}: {q['content']}")

    elif cmd == "dashboard":
        init_db()
        print(dashboard_html())

    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
