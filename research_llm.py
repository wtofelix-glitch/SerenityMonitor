"""
LLM 深度研究 — DeepSeek 语义分析 + 话题→标的智能映射
"""
import sys, os, json, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serenity_logger import get_logger
log = get_logger(__name__)

LLM_KEY = os.environ.get("SERENITY_LLM_API_KEY", "")
LLM_BASE = os.environ.get("SERENITY_LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("SERENITY_LLM_MODEL", "deepseek-chat")
AVAILABLE = bool(LLM_KEY)
_cache = {}

def _call_llm(prompt, max_tokens=200):
    if not AVAILABLE:
        return None
    key = hashlib.md5(prompt.encode()).hexdigest()
    if key in _cache:
        return _cache[key]
    import urllib.request as req
    try:
        body = json.dumps({"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}],
                           "max_tokens": max_tokens, "temperature": 0.3}).encode()
        r = req.Request(f"{LLM_BASE}/chat/completions",
                        data=body, headers={"Content-Type":"application/json",
                                            "Authorization": f"Bearer {LLM_KEY}"})
        resp = json.loads(req.urlopen(r, timeout=20).read())
        result = resp["choices"][0]["message"]["content"].strip()
        _cache[key] = result
        return result
    except Exception as e:
        log.warning(f"LLM call failed: {e}")
        return None

def analyze_news_batch(titles, max_titles=20):
    """批量分析新闻标题 → 提取信号 + 标的 + 话题"""
    if not AVAILABLE or not titles:
        return []

    batch = titles[:max_titles]
    prompt = f"""你是一个A股研究助手。分析以下新闻标题, 提取每条的关键信息。严格返回JSON数组:
[{{"title_idx": 数字, "signal": "bullish/bearish/neutral", "tickers": ["代码列表"], "topics": ["话题"], "impact": "高/中/低", "reason": "一句话原因"}}]

新闻标题:
{chr(10).join(f"{i}. {t}" for i, t in enumerate(batch))}

只返回JSON数组, 不要其他文字。"""

    result = _call_llm(prompt, max_tokens=800)
    if not result:
        return []

    try:
        # Extract JSON from response (handle markdown wrapping)
        if "```" in result:
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        parsed = json.loads(result.strip())
        log.info(f"LLM analyzed {len(batch)} titles → {len(parsed)} signals")
        return parsed
    except Exception as e:
        log.warning(f"LLM parse failed: {e}, raw: {result[:100]}")
        return []

def extract_market_sentiment(news_titles):
    """从新闻标题提取整体市场情绪 [-10, +10]"""
    if not AVAILABLE or len(news_titles) < 5:
        return None

    sample = news_titles[:15]
    prompt = f"""分析以下财经新闻标题,判断A股市场整体情绪。返回JSON:
{{"score": 数字(-10极空到+10极多), "key_themes": ["3-5个关键词"], "risk_factors": ["风险因素"], "one_line": "一句话总结"}}

标题:
{chr(10).join(sample)}

只返回JSON。"""

    result = _call_llm(prompt, max_tokens=200)
    if not result:
        return None

    try:
        if "```" in result:
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        return json.loads(result.strip())
    except:
        return None

def map_news_to_market(news_titles):
    """DeepSeek 智能话题→板块→标的映射"""
    if not AVAILABLE or not news_titles:
        return None

    from config import ALL_CODES, STOCK_MAP
    stock_list = "\n".join(f"{c} {STOCK_MAP.get(c,{}).get('name',c)}" for c in ALL_CODES)

    prompt = f"""你是A股量化研究员。根据当前新闻判断哪些持仓标的会受影响。可用标的:
{stock_list}

最新新闻:
{chr(10).join(news_titles[:10])}

返回JSON: {{"affected": [{{"code":"代码","name":"名称","direction":"bullish/bearish/neutral","confidence":0到1,"reason":"原因"}}],"overall_market":"一句话判断"}}

只返回JSON。"""

    result = _call_llm(prompt, max_tokens=500)
    if not result:
        return None

    try:
        if "```" in result:
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        return json.loads(result.strip())
    except:
        return None
