#!/usr/bin/env python3
"""
SerenityMonitor — 历史K线数据拉取
从新浪财经 API 拉取 6 只主板标的的历史日K线，灌入 price_history 表

API: https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData?symbol={mkt}{code}&scale=240&ma=no&datalen=200

用法:
    python3 fetch_history.py              # 拉取全部 6 只标的
    python3 fetch_history.py 002281        # 拉取单只标的
"""

import json
import urllib.request
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import STOCK_MAP
from db import save_price_history

# A 股数据不走代理
proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(proxy_handler)

SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=200"
)


def fetch_kline(code: str) -> list[dict]:
    info = STOCK_MAP.get(code)
    if not info:
        print(f"  ⚠ 未知代码 {code}，跳过")
        return []

    symbol = f"{info['market']}{code}"
    url = SINA_KLINE_URL.format(symbol=symbol)

    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")

    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  ❌ {info['name']}({code}) 网络请求失败: {e}")
        return []

    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ❌ {info['name']}({code}) JSON 解析失败: {e}")
        print(f"     原始数据前100字符: {raw[:100]}")
        return []

    if not isinstance(rows, list) or len(rows) == 0:
        print(f"  ⚠ {info['name']}({code}) 返回空数据")
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

    if not cleaned:
        print(f"  ⚠ {info['name']}({code}) 清洗后无有效数据")
        return []

    cleaned.sort(key=lambda x: x["day"])

    for i, rec in enumerate(cleaned):
        if i == 0:
            rec["change_pct"] = 0.0
        else:
            prev_close = cleaned[i - 1]["close"]
            if prev_close != 0:
                rec["change_pct"] = round((rec["close"] - prev_close) / prev_close * 100, 4)
            else:
                rec["change_pct"] = 0.0

    return cleaned


def save_to_db(code: str, data_rows: list[dict]) -> int:
    count = 0
    for r in data_rows:
        record = {
            "code": code, "date": r["day"],
            "open": r["open"], "close": r["close"],
            "high": r["high"], "low": r["low"],
            "volume": r["volume"], "change_pct": r["change_pct"],
        }
        try:
            save_price_history(code, record)
            count += 1
        except Exception as e:
            print(f"    ⚠ 写入失败 {code} {r['day']}: {e}")
    return count


def fetch_and_save(code: str) -> dict:
    info = STOCK_MAP.get(code)
    if not info:
        return {"code": code, "status": "skip", "reason": "未知代码"}

    name = info["name"]
    print(f"\n  \U0001f504 {name}({code}) 正在拉取...")

    rows = fetch_kline(code)
    if not rows:
        return {"code": code, "status": "fail", "reason": "无有效数据"}

    saved = save_to_db(code, rows)
    print(f"  ✅ {name}({code}) 完成 | {saved} 条 | {rows[0]['day']} ~ {rows[-1]['day']}")
    return {
        "code": code, "name": name, "status": "ok", "count": saved,
        "first_date": rows[0]["day"], "last_date": rows[-1]["day"],
    }


def main():
    codes = sys.argv[1:] if len(sys.argv) > 1 else list(STOCK_MAP.keys())
    print("=" * 60)
    print("  \U0001f4e5 SerenityMonitor — 历史数据拉取")
    print(f"  标的数量: {len(codes)}")
    print(f"  数据源:   新浪财经日K线 API")
    print(f"  时间:     {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 并行拉取（I/O 密集，ThreadPoolExecutor 显著加速）
    with ThreadPoolExecutor(max_workers=min(8, len(codes))) as executor:
        futures = {executor.submit(fetch_and_save, c): c for c in codes}
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                code = futures[future]
                results.append({"code": code, "status": "fail", "reason": str(e)})

    ok = [r for r in results if r.get("status") == "ok"]
    fail = [r for r in results if r.get("status") != "ok"]

    print(f"\n{'=' * 60}")
    print(f"  \U0001f4ca 拉取汇总")
    print(f"{'=' * 60}")
    print(f"  成功: {len(ok)} / {len(results)}")
    total_records = sum(r.get("count", 0) for r in ok)
    print(f"  总记录数: {total_records}")
    if ok:
        print(f"  数据区间: {min(r['first_date'] for r in ok)} ~ {max(r['last_date'] for r in ok)}")
    if fail:
        for r in fail:
            print(f"  ❌ {r.get('code','?')}: {r.get('reason','')}")
    print(f"\n  ✅ 完成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")


if __name__ == "__main__":
    main()
