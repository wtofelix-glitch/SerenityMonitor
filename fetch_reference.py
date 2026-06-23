#!/usr/bin/env python3
"""
拉取参考标的历史数据 (指数 + ETF)
这些数据仅用于基准对比和图表展示，不参与评分/交易逻辑

用法:
    python3 fetch_reference.py              # 拉取全部参考标的
"""
from check_trading_day import require_trading_day
require_trading_day()

import sys, os, json, urllib.request, ssl
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from config import REFERENCE_SYMBOLS
from db import save_price_history

ssl._create_default_https_context = ssl._create_unverified_context
proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(proxy_handler)

SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=200"
)


def fetch_reference(symbol: str, name: str) -> int:
    """拉取单只参考标的，返回写入条数"""
    url = SINA_KLINE_URL.format(symbol=symbol)
    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")

    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except OSError as e:
        print(f"  ❌ {name}({symbol}) 网络请求失败: {e}")
        return 0

    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ❌ {name}({symbol}) JSON 解析失败: {e}")
        return 0

    if not isinstance(rows, list) or len(rows) == 0:
        print(f"  ⚠️ {name}({symbol}) 返回空数据")
        return 0

    saved = 0
    prev_close = None
    for r in rows:
        day = (r.get("day") or "").strip()
        if not day:
            continue
        try:
            open_p = float(r["open"])
            close_p = float(r["close"])
            high_p = float(r["high"])
            low_p = float(r["low"])
            volume = float(r.get("volume", 0) or 0)
        except (ValueError, KeyError, TypeError):
            continue

        change_pct = 0.0
        if prev_close and prev_close > 0:
            change_pct = round((close_p - prev_close) / prev_close * 100, 4)
        prev_close = close_p

        record = {
            "code": symbol, "date": day,
            "open": open_p, "close": close_p,
            "high": high_p, "low": low_p,
            "volume": volume, "change_pct": change_pct,
        }
        try:
            save_price_history(symbol, record)
            saved += 1
        except Exception as e:
            print(f"    ⚠️ 写入失败 {symbol} {day}: {e}")

    return saved


def main():
    print("=" * 60)
    print("  📊 SerenityMonitor — 参考标的数据拉取 (指数/ETF)")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    total = 0
    for symbol, info in REFERENCE_SYMBOLS.items():
        name = info["name"]
        print(f"\n  🔄 {name}({symbol}) 正在拉取...")
        saved = fetch_reference(symbol, name)
        total += saved
        print(f"  ✅ {name}({symbol}) 完成 | {saved} 条")

    print(f"\n{'=' * 60}")
    print(f"  📊 总记录数: {total}")
    print(f"  ✅ 完成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")


if __name__ == "__main__":
    main()
