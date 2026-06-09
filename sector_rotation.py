#!/usr/bin/env python3
"""
sector_rotation.py — 行业轮动监控模块

按行业分组计算相对强弱，检测轮动信号，生成适合微信推送的摘要。
"""

import json
import urllib.request
from datetime import datetime
from typing import Optional

from data_engine import sina_fetch_raw, parse_sina_line
from config import STOCK_MAP

# ============================================================
# 行业分类
# ============================================================
SECTOR_MAP = {
    "通信":   ["002281", "000988", "600487", "603083"],  # 光迅/华工/亨通/剑桥
    "半导体": ["603986", "600460"],                      # 兆易/士兰微
    "化工":   ["600141"],                                # 兴发
    "有色金属": ["002428"],                              # 云南锗业
    "建材":   ["600176"],                                # 中国巨石
}

# A 股数据不走代理
_proxy_handler = urllib.request.ProxyHandler({})
_opener = urllib.request.build_opener(_proxy_handler)

SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
)


def _fetch_kline_close(code: str, days: int = 20) -> Optional[float]:
    """获取指定股票 days 个交易日前（最早的一天）的收盘价"""
    info = STOCK_MAP.get(code)
    if not info:
        return None

    symbol = f"{info['market']}{code}"
    url = SINA_KLINE_URL.format(symbol=symbol, datalen=days + 5)

    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")
    try:
        with _opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        return None

    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    try:
        rows = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(rows, list) or len(rows) == 0:
        return None

    # 取最早的有效收盘价（即 days 个交易日前）
    try:
        closes = [
            float(r["close"])
            for r in rows
            if r.get("close") not in (None, "")
        ]
    except (ValueError, KeyError):
        return None

    if not closes:
        return None
    return closes[0]


class SectorRotationEngine:
    """行业轮动监控引擎"""

    def __init__(self, sector_map: Optional[dict] = None):
        self.sector_map = sector_map or SECTOR_MAP

    # ----------------------------------------------------------
    # 获取行业表现
    # ----------------------------------------------------------
    def get_sector_performance(self, days: int = 20) -> dict:
        """
        计算每个行业的平均涨跌幅（vs N 天前）

        返回:
            {"通信": {"change": 3.2, "stocks": {"002281": {"change": 4.1, "price": 188.5, ...}, ...}}, ...}
        """
        # 收集所有标代码
        all_codes = []
        for codes in self.sector_map.values():
            all_codes.extend(codes)

        # 1) 获取实时行情
        raw = sina_fetch_raw(all_codes)
        realtime = {}
        for line in raw.strip().split("\n"):
            parsed = parse_sina_line(line)
            if parsed:
                code = parsed["code"]
                realtime[code] = parsed

        # 2) 对每个行业计算
        result = {}
        for sector_name, codes in self.sector_map.items():
            stock_items = {}
            total_change = 0.0
            valid_count = 0

            for code in codes:
                rt = realtime.get(code)
                if not rt:
                    stock_items[code] = {"change": None, "price": None, "error": "无实时行情"}
                    continue

                price_now = rt.get("price", 0)
                price_before = _fetch_kline_close(code, days)

                if price_now and price_before and price_before > 0:
                    change_pct = round((price_now - price_before) / price_before * 100, 2)
                    total_change += change_pct
                    valid_count += 1
                else:
                    change_pct = None

                stock_items[code] = {
                    "code": code,
                    "name": STOCK_MAP.get(code, {}).get("name", code),
                    "price": price_now,
                    "close_before": price_before,
                    "change": change_pct,
                }

            sector_avg = round(total_change / valid_count, 2) if valid_count > 0 else 0.0
            result[sector_name] = {
                "change": sector_avg,
                "stocks": stock_items,
            }

        return result

    # ----------------------------------------------------------
    # 行业排名
    # ----------------------------------------------------------
    def get_sector_rank(self) -> list[dict]:
        """按行业表现排序，返回排名列表"""
        perf = self.get_sector_performance()
        ranked = []
        for sector_name, info in perf.items():
            change = info["change"]
            if change > 2.0:
                momentum = "strong"
            elif change >= 0:
                momentum = "neutral"
            else:
                momentum = "weak"
            ranked.append({
                "sector": sector_name,
                "change": change,
                "momentum": momentum,
            })

        ranked.sort(key=lambda x: x["change"], reverse=True)
        for i, item in enumerate(ranked, 1):
            item["rank"] = i

        return ranked

    # ----------------------------------------------------------
    # 轮动信号检测
    # ----------------------------------------------------------
    def get_rotation_signal(self) -> str:
        """检测行业轮动信号"""
        ranked = self.get_sector_rank()
        if not ranked:
            return "数据不足"

        all_positive = all(r["change"] >= 0 for r in ranked)
        all_negative = all(r["change"] < 0 for r in ranked)

        top_change = ranked[0]["change"] if ranked else 0
        bottom_change = ranked[-1]["change"] if ranked else 0
        gap = abs(top_change - bottom_change)

        # 强弱差距 > 10%
        if gap > 10.0:
            return "分化"

        # 普涨 / 普跌
        if all_positive:
            return "普涨"
        if all_negative:
            return "普跌"

        # 强者恒强：前2名 momentum 都是 strong
        top2 = ranked[:2]
        if len(top2) == 2 and all(r["momentum"] == "strong" for r in top2):
            return "强者恒强"

        # 弱势行业反超强势（如有行业从 negative 变成 positive）
        # 我们简化为：最后一名 momentum 不是 weak（说明弱势行业在反弹）
        # 更精确：如果 top 1 是 weak 或者 bottom 是 strong，说明在切换
        if ranked[0]["momentum"] == "weak" and len(ranked) >= 2:
            return "风格切换"
        if ranked[-1]["momentum"] == "strong" and len(ranked) >= 2:
            return "风格切换"

        # 默认
        return "分化"

    # ----------------------------------------------------------
    # 行业摘要（微信推送格式）
    # ----------------------------------------------------------
    def get_sector_summary(self) -> str:
        """
        返回格式适合微信推送的行业轮动扫描文本
        """
        ranked = self.get_sector_rank()
        signal = self.get_rotation_signal()

        # 获取上一次排名（简化为用 rank 字段）
        lines = []
        for item in ranked:
            change = item["change"]
            if change > 2:
                emoji = "🔴"
            elif change >= 0:
                emoji = "⚪"
            else:
                emoji = "🟢"

            rank_str = f"rank:{item['rank']}"
            # 相对位置箭头
            arrow = ""
            # 这里没对比上一次，简化显示
            lines.append(f"{emoji}{item['sector']} {change:+.1f}% (↑{item['rank']})")

        # 信号 emoji
        signal_emojis = {
            "强者恒强": "🔥",
            "风格切换": "🔄",
            "普涨": "📈",
            "普跌": "📉",
            "分化": "⚡",
        }
        signal_emoji = signal_emojis.get(signal, "📊")

        # 构建报告
        now_str = datetime.now().strftime("%m/%d %H:%M")
        out = [
            f"📊 行业轮动扫描 [{now_str}]",
        ]
        out.extend(lines)
        out.append(f"信号: {signal_emoji}{signal}")

        return "\n".join(out)


# ============================================================
# 快捷函数
# ============================================================
def get_sector_rotation_summary() -> str:
    """一行入口：返回行业轮动摘要"""
    engine = SectorRotationEngine()
    return engine.get_sector_summary()


def get_sector_rotation_detail() -> str:
    """详细版：含个股明细"""
    engine = SectorRotationEngine()
    perf = engine.get_sector_performance()
    ranked = engine.get_sector_rank()
    signal = engine.get_rotation_signal()

    now_str = datetime.now().strftime("%m/%d %H:%M")
    out = [f"📊 行业轮动扫描（详细版）[{now_str}]"]
    out.append("=" * 36)

    for item in ranked:
        sector = item["sector"]
        change = item["change"]
        rank = item["rank"]
        momentum = item["momentum"]

        if change > 2:
            emoji = "🔴"
        elif change >= 0:
            emoji = "⚪"
        else:
            emoji = "🟢"

        momentum_map = {"strong": "强势", "neutral": "中性", "weak": "弱势"}
        out.append(f"\n{emoji} #{rank} {sector} | {change:+.2f}% | {momentum_map[momentum]}")

        # 个股明细
        sector_stocks = perf.get(sector, {}).get("stocks", {})
        for code, sinfo in sector_stocks.items():
            if sinfo.get("change") is not None:
                sc = sinfo["change"]
                se = "🔴" if sc >= 0 else "🟢"
                out.append(f"  {se} {sinfo['name']}({code}) {sinfo['price']:.2f} {sc:+.2f}%")
            else:
                out.append(f"  ⚪ {sinfo['name']}({code}) 数据不足")

    # 信号
    signal_emojis = {
        "强者恒强": "🔥",
        "风格切换": "🔄",
        "普涨": "📈",
        "普跌": "📉",
        "分化": "⚡",
    }
    signal_emoji = signal_emojis.get(signal, "📊")
    out.append(f"\n信号: {signal_emoji}{signal}")

    return "\n".join(out)


if __name__ == "__main__":
    engine = SectorRotationEngine()
    print("=== 行业表现 ===")
    perf = engine.get_sector_performance()
    for s, info in perf.items():
        print(f"  {s}: {info['change']:+.2f}%")
    print()
    print("=== 行业排名 ===")
    for r in engine.get_sector_rank():
        print(f"  #{r['rank']} {r['sector']} {r['change']:+.2f}% ({r['momentum']})")
    print()
    print("=== 信号 ===")
    print(f"  {engine.get_rotation_signal()}")
    print()
    print("=== 摘要 ===")
    print(engine.get_sector_summary())
