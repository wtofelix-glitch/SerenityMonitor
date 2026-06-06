"""
大盘择时信号 — 基于上证指数和沪深300的MA20/MA60趋势 + RSI14 + 成交量趋势
数据源: 新浪财经日K线 API
"""
import json
import urllib.request
import numpy as np
from typing import Optional

# 新浪K线API (与 fetch_history.py 一致)
SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=80"
)

# A股数据不走代理
proxy_handler = urllib.request.ProxyHandler({})
_opener = urllib.request.build_opener(proxy_handler)

# 监控的指数
INDEX_CODES = {
    "000001": {"name": "上证指数", "symbol": "sh000001"},
    "000300": {"name": "沪深300", "symbol": "sh000300"},
}


def _fetch_index_kline(symbol: str, days: int = 80) -> list[dict]:
    """从新浪获取指数日K线数据"""
    url = SINA_KLINE_URL.format(symbol=symbol)
    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")
    try:
        with _opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        return []

    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]

    try:
        rows = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(rows, list) or len(rows) == 0:
        return []

    cleaned = []
    for r in rows:
        day = (r.get("day") or "").strip()
        if not day:
            continue
        try:
            open_p = float(r["open"]) if r.get("open") not in (None, "") else None
            close_p = float(r["close"]) if r.get("close") not in (None, "") else None
            high_p = float(r["high"]) if r.get("high") not in (None, "") else None
            low_p = float(r["low"]) if r.get("low") not in (None, "") else None
            volume = float(r["volume"]) if r.get("volume") not in (None, "") else None
        except (ValueError, KeyError):
            continue
        if None in (open_p, close_p, high_p, low_p):
            continue
        cleaned.append({
            "day": day, "open": open_p, "close": close_p,
            "high": high_p, "low": low_p, "volume": volume or 0,
        })

    cleaned.sort(key=lambda x: x["day"])
    return cleaned[-days:] if len(cleaned) > days else cleaned


def compute_ma_trend(closes: np.ndarray, ma_short: int = 20, ma_long: int = 60) -> dict:
    """计算均线趋势"""
    n = len(closes)
    if n < ma_long:
        return {"trend": "数据不足", "ma_short": 0, "ma_long": 0, "ma_short_val": 0, "ma_long_val": 0}

    ma_s = closes[-ma_short:].mean()
    ma_l = closes[-ma_long:].mean()
    current = closes[-1]

    if current > ma_s > ma_l:
        trend = "多头"
    elif current < ma_s < ma_l:
        trend = "空头"
    elif ma_s > ma_l:
        trend = "震荡偏多"
    elif ma_s < ma_l:
        trend = "震荡偏空"
    else:
        trend = "震荡"

    return {
        "trend": trend,
        "ma_short_val": round(ma_s, 2),
        "ma_long_val": round(ma_l, 2),
        "price_vs_ma20_pct": round((current - ma_s) / ma_s * 100, 2) if ma_s > 0 else 0,
        "price_vs_ma60_pct": round((current - ma_l) / ma_l * 100, 2) if ma_l > 0 else 0,
    }


def compute_rsi(closes: np.ndarray, period: int = 14) -> dict:
    """计算RSI14"""
    n = len(closes)
    if n < period + 1:
        return {"rsi": 50, "status": "数据不足"}

    deltas = np.diff(closes[-(period + 1):])
    gains = deltas[deltas > 0].sum()
    losses = abs(deltas[deltas < 0].sum())
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    if rsi >= 70:
        status = "超买"
    elif rsi <= 30:
        status = "超卖"
    elif 40 <= rsi <= 60:
        status = "中性"
    elif rsi > 60:
        status = "偏强"
    else:
        status = "偏弱"

    return {"rsi": round(rsi, 1), "status": status}


def compute_volume_trend(volumes: np.ndarray, short_window: int = 5, long_window: int = 20) -> dict:
    """计算成交量趋势"""
    n = len(volumes)
    if n < long_window:
        return {"volume_trend": "数据不足", "vol_ratio": 1.0}

    vol_ma_short = volumes[-short_window:].mean()
    vol_ma_long = volumes[-long_window:].mean()
    vol_ratio = vol_ma_short / vol_ma_long if vol_ma_long > 0 else 1.0

    if vol_ratio >= 1.5:
        trend = "放量"
    elif vol_ratio >= 1.2:
        trend = "温和放量"
    elif vol_ratio <= 0.5:
        trend = "缩量"
    elif vol_ratio <= 0.8:
        trend = "温和缩量"
    else:
        trend = "正常"

    return {"volume_trend": trend, "vol_ratio": round(vol_ratio, 2)}


def analyze_index(symbol: str, name: str) -> dict:
    """分析单个指数的技术指标"""
    data = _fetch_index_kline(symbol)

    if len(data) < 30:
        return {
            "name": name,
            "symbol": symbol,
            "status": "数据不足",
            "last_close": 0,
        }

    closes = np.array([r["close"] for r in data], dtype=float)
    volumes = np.array([r["volume"] for r in data], dtype=float)
    last_close = closes[-1]

    ma = compute_ma_trend(closes)
    rsi = compute_rsi(closes)
    vol = compute_volume_trend(volumes)

    # 综合判断
    signals = []
    if rsi["rsi"] >= 70:
        signals.append("超买⚠️")
    elif rsi["rsi"] <= 30:
        signals.append("超卖💡")
    if "多头" in ma["trend"]:
        signals.append("趋势偏多")
    elif "空头" in ma["trend"]:
        signals.append("趋势偏空")
    if vol["volume_trend"] == "放量":
        signals.append("放量")
    elif vol["volume_trend"] == "缩量":
        signals.append("缩量")

    return {
        "name": name,
        "symbol": symbol,
        "last_close": round(last_close, 2),
        "trend": ma["trend"],
        "ma20": ma["ma_short_val"],
        "ma60": ma["ma_long_val"],
        "price_vs_ma20": ma["price_vs_ma20_pct"],
        "rsi": rsi["rsi"],
        "rsi_status": rsi["status"],
        "volume_trend": vol["volume_trend"],
        "vol_ratio": vol["vol_ratio"],
        "signals": ", ".join(signals) if signals else "中性",
    }


def get_market_signal() -> dict:
    """
    获取大盘综合择时信号

    Returns
    -------
    dict: {
        "overall_trend": str,       # 多头/空头/震荡/震荡偏多/震荡偏空
        "overall_signal": str,      # 积极/中性/谨慎/危险
        "overall_advice": str,      # 建议文本
        "sh": dict,                 # 上证指数分析
        "hs300": dict,              # 沪深300分析
    }
    """
    sh = analyze_index("sh000001", "上证指数")
    hs300 = analyze_index("sh000300", "沪深300")

    # 综合判断
    trends = []
    if sh.get("status") != "数据不足":
        trends.append(sh["trend"])
    if hs300.get("status") != "数据不足":
        trends.append(hs300["trend"])

    if not trends:
        return {"overall_trend": "未知", "overall_signal": "中性", "overall_advice": "数据不足"}

    # 提取趋势方向
    bullish_count = sum(1 for t in trends if "多头" in t)
    bearish_count = sum(1 for t in trends if "空头" in t)
    neutral_count = sum(1 for t in trends if "震荡" in t or t == "中性")

    # RSI 综合
    rsi_vals = []
    for idx in [sh, hs300]:
        if idx.get("status") != "数据不足":
            rsi_vals.append(idx.get("rsi", 50))
    avg_rsi = sum(rsi_vals) / len(rsi_vals) if rsi_vals else 50

    if bullish_count >= 1 and avg_rsi < 65:
        overall_trend = "多头"
        overall_signal = "积极"
        overall_advice = "正常仓位"
    elif bearish_count >= 1 and avg_rsi < 40:
        overall_trend = "空头"
        overall_signal = "危险"
        overall_advice = "减仓防守"
    elif bearish_count >= 1:
        overall_trend = "空头"
        overall_signal = "谨慎"
        overall_advice = "轻仓观望"
    elif avg_rsi >= 70:
        overall_trend = "超买"
        overall_signal = "谨慎"
        overall_advice = "注意回调风险,控制仓位"
    elif avg_rsi <= 35:
        overall_trend = "超卖"
        overall_signal = "机会"
        overall_advice = "可逢低布局"
    else:
        overall_trend = "震荡"
        overall_signal = "中性"
        overall_advice = "正常仓位,精选个股"

    return {
        "overall_trend": overall_trend,
        "overall_signal": overall_signal,
        "overall_advice": overall_advice,
        "avg_rsi": round(avg_rsi, 1),
        "sh": sh,
        "hs300": hs300,
    }


def get_market_advice() -> str:
    """
    返回简洁的大盘建议文本，适合微信推送
    """
    signal = get_market_signal()
    sh = signal["sh"]
    hs300 = signal["hs300"]

    lines = []
    lines.append(f"📊 大盘择时 | {signal['overall_signal']}")
    lines.append(f"趋势:{signal['overall_trend']} | RSI:{signal['avg_rsi']}")

    if sh.get("status") != "数据不足":
        lines.append(
            f"上证{sh['last_close']} MA20/{sh['ma20']:.0f} "
            f"MA60/{sh['ma60']:.0f} RSI{sh['rsi']} {sh['trend']}"
        )
    if hs300.get("status") != "数据不足":
        lines.append(
            f"沪深300{hs300['last_close']} MA20/{hs300['ma20']:.0f} "
            f"MA60/{hs300['ma60']:.0f} RSI{hs300['rsi']} {hs300['trend']}"
        )

    lines.append(f"建议: {signal['overall_advice']}")
    return "\n".join(lines)
