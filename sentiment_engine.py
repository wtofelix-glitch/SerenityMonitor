"""
Sentiment Engine — A股新闻情绪评分模块
从新浪财经获取个股新闻，分析标题情感 keywords 计算情绪得分
"""

import re
import logging
from datetime import datetime
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from config import STOCK_MAP, ALL_CODES
import json as _json
import os as _os
import hashlib as _hashlib

logger = logging.getLogger(__name__)

# ── LLM 配置 ──────────────────────────────────────────
LLM_API_KEY = _os.environ.get("SERENITY_LLM_API_KEY", "")
LLM_API_BASE = _os.environ.get("SERENITY_LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_MODEL = _os.environ.get("SERENITY_LLM_MODEL", "deepseek-chat")
LLM_AVAILABLE = bool(LLM_API_KEY)
_llm_cache: dict[str, float] = {}  # key: md5(code+date+titles) → score

# ============================================================
# 情绪关键词
# ============================================================

BULLISH_KEYWORDS = [
    "增持", "买入", "涨停", "突破", "中标", "预增", "超预期",
    "利好", "放量", "新高", "订单", "产能", "扩产", "回购",
    "分红", "补贴",
]

BEARISH_KEYWORDS = [
    "减持", "卖出", "跌停", "破位", "预亏", "不及预期", "利空",
    "缩量", "新低", "诉讼", "调查", "ST", "退市", "爆雷", "亏损",
]

REQUEST_TIMEOUT = 10  # seconds
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ============================================================
# 工具函数
# ============================================================

def _get_market_prefix(code: str) -> str:
    """根据股票代码返回新浪市场前缀 (sh/sz)"""
    info = STOCK_MAP.get(code)
    if info:
        return info["market"]
    # fallback: 600xxx → sh, 002xxx/000xxx → sz
    if code.startswith("6"):
        return "sh"
    return "sz"


def _build_news_url(code: str) -> str:
    """构建新浪个股新闻 URL"""
    market = _get_market_prefix(code)
    return f"http://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{market}{code}.phtml"


def _fetch_page(url: str) -> Optional[str]:
    """带超时的 HTTP GET 请求，返回 UTF-8 文本"""
    req = Request(url, headers=FETCH_HEADERS)
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            # 尝试检测编码，回退 GBK（新浪常见）
            content_type = resp.headers.get("Content-Type", "")
            if "gbk" in content_type.lower() or "gb2312" in content_type.lower():
                html = raw.decode("gbk", errors="replace")
            else:
                try:
                    html = raw.decode("utf-8")
                except UnicodeDecodeError:
                    html = raw.decode("gbk", errors="replace")
            return html
    except (URLError, OSError, ValueError) as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        return None


def _parse_news_items(html: str) -> list[dict]:
    """
    从新浪新闻列表页 HTML 中提取新闻条目

    新浪页面结构为 <div class="datelist"><ul>
        &nbsp;&nbsp;&nbsp;&nbsp;2026-06-06 11:54&nbsp;&nbsp;
        <a target='_blank' href='...'>标题</a> <br>
    ...

    Returns
    -------
    list[dict]
        [{"title": ..., "date": ..., "source": ...}, ...]
    """
    items: list[dict] = []

    # 找到 <div class="datelist"> 或包含个股相关资讯的区域
    # Note: HTML may use single or double quotes for the class attribute
    datelist_idx = html.find('class="datelist"')
    if datelist_idx < 0:
        datelist_idx = html.find("class='datelist'")
    if datelist_idx < 0:
        datelist_idx = html.find("datelist")
    if datelist_idx < 0:
        # fallback: 搜索 "个股相关资讯"
        info_idx = html.find("个股相关资讯")
        if info_idx >= 0:
            datelist_idx = info_idx

    if datelist_idx < 0:
        return items

    # 只取 datelist 区域之后的内容（避免匹配侧边栏无关链接）
    search_region = html[datelist_idx:]

    # 查找新闻条目模式:
    #   &nbsp;...&nbsp;2026-06-06&nbsp;11:54&nbsp;&nbsp;
    #   <a ...>标题</a> <br>
    #
    # 每一条: 日期后紧跟 <a> 标签

    # 匹配: 日期部分 + <a>title</a>
    # 日期格式: 2026-06-06 或 2026-06-06&nbsp;11:54 (时间用 &nbsp; 分隔)
    # 中间可能有 &nbsp; + 空格 + 换行
    pattern = re.compile(
        r"(?P<date>\d{4}-\d{2}-\d{2}"
        r"(?:(?:&nbsp;|\s)\d{2}:\d{2})?)"
        r"(?:\s|&nbsp;)*"
        r'<a[^>]*?>(?P<title>[^<]+?)</a>',
        re.IGNORECASE,
    )

    for m in pattern.finditer(search_region):
        title = m.group("title").strip()
        date_str = m.group("date").strip()
        if title and len(title) >= 4:
            # Clean &nbsp; from date string
            clean_date = date_str.replace("&nbsp;", " ")
            items.append({
                "title": title,
                "date": clean_date,
                "source": "新浪财经",
            })

    return items


# ============================================================
# Sentiment 分析
# ============================================================

def _analyze_sentiment(title: str) -> int:
    """
    分析单条新闻标题的情绪 (关键词模式)
    Returns: +1 (bullish), -1 (bearish), 0 (neutral)
    """
    for kw in BULLISH_KEYWORDS:
        if kw in title:
            return 1
    for kw in BEARISH_KEYWORDS:
        if kw in title:
            return -1
    return 0


def _analyze_sentiment_llm(code: str, name: str, titles: list[str]) -> float:
    """
    LLM 情绪分析 — 将全部标题批量送入 LLM，返回 [0, 100] 得分。
    失败或不可用时返回 None，由调用方 fallback 到关键词模式。
    """
    if not LLM_AVAILABLE or not titles:
        return None

    cache_key = _hashlib.md5(
        (code + "|" + "|".join(sorted(titles[:30]))).encode()
    ).hexdigest()
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    prompt = f"""你是一位 A 股市场情绪分析师。请根据以下 {name}({code}) 的新闻标题，给出综合情绪评分。

评分标准: 0=极空, 25=偏空, 50=中性, 75=偏多, 100=极多
请同时判断 利多/利空/中性 信号数量，并给出 1-2 句简要解读。

新闻标题:
"""
    for i, t in enumerate(titles[:30], 1):
        prompt += f"{i}. {t}\n"
    prompt += """
请以 JSON 格式回复，只返回 JSON:
{"score": 65, "bullish_count": 3, "bearish_count": 1, "neutral_count": 2, "summary": "整体偏多，..."}"""

    try:
        import urllib.request as _req
        body = _json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 300,
        }).encode("utf-8")

        rq = _req.Request(
            f"{LLM_API_BASE}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
        )
        with _req.urlopen(rq, timeout=20) as resp:
            data = _json.loads(resp.read().decode())
        
        content = data["choices"][0]["message"]["content"]
        # Extract JSON from response (may contain markdown fences)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        
        result = _json.loads(content)
        score = float(result.get("score", 50))
        score = max(0.0, min(100.0, score))
        
        _llm_cache[cache_key] = score
        logger.info(f"LLM sentiment {code}: {score:.0f} ({result.get('summary', '')[:40]})")
        return score
    except Exception as e:
        logger.debug(f"LLM sentiment failed for {code}: {e}")
        return None


# ============================================================
# 公开 API
# ============================================================

def fetch_sentiment_data(code: str) -> list[dict]:
    """
    获取指定股票的最新新闻数据

    Parameters
    ----------
    code : str
        6 位股票代码，如 "002281"

    Returns
    -------
    list[dict]
        [{"title": ..., "date": ..., "source": ...}, ...]
        网络异常时返回空列表
    """
    try:
        url = _build_news_url(code)
        html = _fetch_page(url)
        if html is None:
            return []
        items = _parse_news_items(html)
        # 去重（新浪页面有时会重复）
        seen = set()
        unique: list[dict] = []
        for item in items:
            key = (item["title"], item["date"])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique
    except Exception:
        logger.debug("fetch_sentiment_data failed for %s", code, exc_info=True)
        return []


def compute_sentiment_score(code: str) -> float:
    """
    计算指定股票的情绪得分 [0, 100]
    
    优先使用 LLM 分析，不可用时回退到关键词模式。
    """
    try:
        news = fetch_sentiment_data(code)
        if not news:
            return 50.0

        name = STOCK_MAP.get(code, {}).get("name", code)
        titles = [item["title"] for item in news]

        # 尝试 LLM
        if LLM_AVAILABLE:
            llm_score = _analyze_sentiment_llm(code, name, titles)
            if llm_score is not None:
                return llm_score

        # 回退关键词
        score = 50.0
        for item in news:
            sentiment = _analyze_sentiment(item["title"])
            if sentiment == 1:
                score += 5.0
            elif sentiment == -1:
                score -= 5.0
        return max(0.0, min(100.0, score))
    except Exception:
        logger.debug("compute_sentiment_score failed for %s", code, exc_info=True)
        return 50.0


def get_sentiment_report(code: str) -> dict:
    """获取完整情绪分析报告（优先 LLM，回退关键词）"""
    name = STOCK_MAP.get(code, {}).get("name", code)
    news = fetch_sentiment_data(code)

    # 关键词计数（始终计算，用于报告统计）
    bullish = bearish = neutral = 0
    for item in news:
        s = _analyze_sentiment(item["title"])
        if s == 1:
            bullish += 1
        elif s == -1:
            bearish += 1
        else:
            neutral += 1

    # 使用 compute_sentiment_score（内部优先 LLM）
    score = compute_sentiment_score(code)

    return {
        "code": code,
        "name": name,
        "score": round(score, 1),
        "news_count": len(news),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "news": news,
    }


# ============================================================
# CLI
# ============================================================

def _sentiment_label(score: float) -> str:
    """将情绪得分映射为可读标签"""
    if score >= 70:
        return "🟢 积极"
    elif score >= 55:
        return "🔵 偏多"
    elif score >= 45:
        return "⚪ 中性"
    elif score >= 30:
        return "🟡 偏空"
    else:
        return "🔴 消极"


if __name__ == "__main__":
    print(f"\n📰  A股新闻情绪分析  |  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 68)

    results: list[dict] = []
    for code in ALL_CODES:
        report = get_sentiment_report(code)
        results.append(report)
        label = _sentiment_label(report["score"])
        print(
            f"{report['name']:8s} ({code})  "
            f"情绪 {report['score']:5.1f}  {label}  "
            f"📄 {report['news_count']:2d}条  "
            f"🟢{report['bullish_count']} 🔴{report['bearish_count']} ⚪{report['neutral_count']}"
        )

    if results:
        avg = sum(r["score"] for r in results) / len(results)
        print("=" * 68)
        market_label = _sentiment_label(avg)
        print(f"组合平均情绪: {avg:.1f}  {market_label}")
    print()
