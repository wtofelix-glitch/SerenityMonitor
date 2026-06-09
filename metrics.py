"""
SerenityMonitor Prometheus 指标模块
暴露可观测性指标，供 Flask /metrics 端点使用。
"""
import time
from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY

# ── 评分引擎指标 ─────────────────────────────────────
SCORE_COUNT = Counter(
    "serenity_score_total",
    "总评分数（所有标的）",
)
SCORE_ERRORS = Counter(
    "serenity_score_errors_total",
    "评分过程中发生的异常数",
    labelnames=["module"],
)

SIGNAL_ACTIONS = Counter(
    "serenity_signal_actions_total",
    "信号分布（BUY/SELL/HOLD 等）",
    labelnames=["action"],
)

SCORE_DURATION = Histogram(
    "serenity_score_duration_seconds",
    "单次 score_all() 耗时分布",
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

# ── 数据引擎指标 ────────────────────────────────────
API_CALLS = Counter(
    "serenity_api_calls_total",
    "外部数据源 API 调用次数",
    labelnames=["source"],  # sina / kline / sentiment
)
API_ERRORS = Counter(
    "serenity_api_errors_total",
    "外部 API 调用失败次数",
    labelnames=["source"],
)
CACHE_HITS = Counter(
    "serenity_cache_hits_total",
    "缓存命中次数",
    labelnames=["cache"],  # snapshot / sentiment
)
CACHE_MISSES = Counter(
    "serenity_cache_misses_total",
    "缓存未命中次数",
    labelnames=["cache"],
)

FETCH_DURATION = Histogram(
    "serenity_fetch_duration_seconds",
    "单次数据抓取耗时分布",
    buckets=(0.05, 0.1, 0.3, 0.5, 1.0, 2.0, 5.0),
)

# ── 情绪引擎指标 ────────────────────────────────────
SENTIMENT_SCORE = Gauge(
    "serenity_sentiment_score",
    "各标的情绪得分",
    labelnames=["code"],
)
SENTIMENT_LLM_USED = Counter(
    "serenity_sentiment_llm_calls_total",
    "情绪分析 LLM 调用次数",
)

# ── 反射学习环指标 ───────────────────────────────────
REFLECTION_COUNT = Counter(
    "serenity_reflections_total",
    "生成的反思记录数",
)
WEIGHT_ADJUSTMENTS = Counter(
    "serenity_weight_adjustments_total",
    "权重调整次数",
)

# ── 执行引擎指标 ────────────────────────────────────
EXECUTION_ORDERS = Counter(
    "serenity_execution_orders_total",
    "执行订单数",
    labelnames=["action", "status"],  # BUY/SELL × pending/executed/failed
)

# ── 组合状态指标 ────────────────────────────────────
PORTFOLIO_VALUE = Gauge(
    "serenity_portfolio_value",
    "组合总权益",
)
PORTFOLIO_CASH = Gauge(
    "serenity_portfolio_cash",
    "可用现金",
)
PORTFOLIO_PROFIT_PCT = Gauge(
    "serenity_portfolio_profit_pct",
    "组合总盈亏百分比",
)
PORTFOLIO_POSITIONS = Gauge(
    "serenity_portfolio_positions",
    "持仓数量",
)

# ── 大盘指标 ────────────────────────────────────────
MARKET_RSI = Gauge(
    "serenity_market_rsi",
    "大盘 RSI",
    labelnames=["index_name"],
)
MARKET_TREND = Gauge(
    "serenity_market_trend_score",
    "大盘趋势得分",
    labelnames=["type"],  # sh / hs300
)


def metrics_endpoint():
    """返回 Prometheus 格式的指标文本"""
    return generate_latest(REGISTRY)


def observe_score_duration(func):
    """装饰器：监控 score_all() 耗时"""
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            return result
        finally:
            elapsed = time.monotonic() - start
            SCORE_DURATION.observe(elapsed)
    return wrapper


def observe_fetch_duration(func):
    """装饰器：监控数据抓取耗时"""
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            return result
        finally:
            elapsed = time.monotonic() - start
            FETCH_DURATION.observe(elapsed)
    return wrapper
