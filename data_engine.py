"""
数据引擎 — 通过新浪 API 获取 A 股实时行情与历史数据
"""
import urllib.request
import json
import re
from datetime import datetime, date
from typing import Optional

from config import STOCK_MAP, ALL_CODES, SINA_PREFIX


def sina_fetch_raw(code_list: list[str]) -> str:
    """
    通过新浪 API 获取股票实时行情
    返回 CSV 格式原始数据
    """
    codes = []
    for c in code_list:
        mkt = STOCK_MAP[c]["market"]
        codes.append(f"{mkt}{c}")

    url = SINA_PREFIX + ",".join(codes)
    # A 股数据不走代理
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")
    with opener.open(req, timeout=10) as resp:
        raw = resp.read().decode("gbk")
    return raw


def parse_sina_line(line: str) -> Optional[dict]:
    """
    解析新浪单行行情数据
    格式: var hq_str_sh600000="浦东发展,15.20,15.30,..."
    """
    if not line.startswith("var hq_str_"):
        return None
    try:
        # 提取股票代码
        code_match = re.search(r'var hq_str_(\w+)="', line)
        if not code_match:
            return None
        full_code = code_match.group(1)  # sh300782 或 sz300308
        code = full_code[2:]  # 去掉 sh/sz 前缀

        # 提取数据部分
        data_str = line.split('="')[1].rstrip('";\n')
        fields = data_str.split(",")

        return {
            "code": code,
            "name": fields[0],
            "open": float(fields[1]) if fields[1] else 0,
            "close_yesterday": float(fields[2]) if fields[2] else 0,
            "price": float(fields[3]) if fields[3] else 0,  # 当前价
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": int(fields[8]) if fields[8] else 0,  # 手
            "amount": float(fields[9]) if fields[9] else 0,  # 万元
            "buy1": float(fields[10]) if fields[10] else 0,
            "sell1": float(fields[12]) if fields[12] else 0,
            "date": fields[30] if len(fields) > 30 else "",
            "time": fields[31] if len(fields) > 31 else "",
        }
    except (ValueError, IndexError, AttributeError):
        return None


def fetch_realtime(code_list: Optional[list[str]] = None) -> list[dict]:
    """
    获取多只股票的实时行情
    返回解析后的字典列表
    """
    if code_list is None:
        code_list = ALL_CODES

    raw = sina_fetch_raw(code_list)
    results = []
    for line in raw.strip().split("\n"):
        parsed = parse_sina_line(line)
        if parsed:
            results.append(parsed)
    return results


def fetch_single(code: str) -> Optional[dict]:
    """获取单只股票实时行情"""
    results = fetch_realtime([code])
    return results[0] if results else None


def get_today_snapshot(code: str) -> Optional[dict]:
    """
    获取今日收盘快照（适合收盘后调用）
    如果还没收盘，返回当前行情并附备注
    """
    data = fetch_single(code)
    if not data:
        return None

    info = STOCK_MAP.get(code, {})
    close_y = data.get("close_yesterday", 0)
    price = data.get("price", 0)
    change_pct = round((price - close_y) / close_y * 100, 2) if close_y else 0

    return {
        "code": code,
        "name": info.get("name", code),
        "date": data.get("date") or date.today().isoformat(),
        "open": data.get("open"),
        "close": price,
        "high": data.get("high"),
        "low": data.get("low"),
        "volume": data.get("volume"),
        "amount": data.get("amount"),
        "change_pct": change_pct,
        "price": price,
    }


def get_all_today_snapshots() -> list[dict]:
    """获取所有候选标的今日快照"""
    results = fetch_realtime()
    snapshots = []
    for data in results:
        code = data["code"]
        info = STOCK_MAP.get(code, {})
        close_y = data.get("close_yesterday", 0)
        price = data.get("price", 0)
        change_pct = round((price - close_y) / close_y * 100, 2) if close_y else 0
        snapshots.append({
            "code": code,
            "name": info.get("name", data.get("name", code)),
            "tier": info.get("tier", 3),
            "date": data.get("date") or date.today().isoformat(),
            "open": data.get("open"),
            "close": price,
            "high": data.get("high"),
            "low": data.get("low"),
            "volume": data.get("volume"),
            "amount": data.get("amount"),
            "change_pct": change_pct,
            "price": price,
            "buy1": data.get("buy1"),
            "sell1": data.get("sell1"),
        })
    return snapshots
