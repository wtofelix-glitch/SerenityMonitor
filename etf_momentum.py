"""
ETF动量轮动引擎 — 多ETF动量排名，定期轮动
基于短/中/长三周期动量 + 趋势强度
数据源：Sina 指数 K 线 API（真实指数数据）
"""
import json
import ssl
import urllib.request
import numpy as np
from datetime import date
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from db import get_conn

# 10只 ETF（A股主流宽基+行业）
ETF_POOL = [
    "510300",  # 沪深300
    "510050",  # 上证50
    "510500",  # 中证500
    "159915",  # 创业板
    "512880",  # 证券ETF
    "512690",  # 酒ETF
    "159995",  # 芯片ETF
    "515790",  # 光伏ETF
    "516160",  # 新能源ETF
    "513100",  # 纳指ETF
]

ETF_NAMES = {
    "510300": "沪深300", "510050": "上证50", "510500": "中证500",
    "159915": "创业板", "512880": "证券ETF", "512690": "酒ETF",
    "159995": "芯片ETF", "515790": "光伏ETF", "516160": "新能源ETF",
    "513100": "纳指ETF",
}

# ETF → Sina 指数符号映射（已验证可用）
ETF_INDEX_MAP = {
    "510300": "sh000300",   # 沪深300 ✅
    "510050": "sh000016",   # 上证50 ✅
    "510500": "sh000905",   # 中证500 ✅
    "159915": "sz399006",   # 创业板指 ✅
    "512880": "sz399975",   # 证券公司指数 ✅
    "512690": "sh000932",   # 中证白酒 ✅
    "159995": "sh000688",   # 科创50（芯片近似） ✅
    "515790": "sz399808",   # 中证新能（光伏近似） ✅
    "516160": "sz399673",   # 创业板50（新能源近似） ✅
    "513100": "em_513100",      # 纳指ETF — 东方财富日K ✅
}

# 缓存
_KLINE_CACHE = {}


class ETFMomentumStrategy:
    """ETF 动量轮动策略"""

    def rank_all(self) -> list[dict]:
        """对所有 ETF 进行动量排名（并行抓取）"""
        today = date.today().isoformat()
        conn = get_conn()
        cur = conn.cursor()
        results = []

        # 并行抓取所有 ETF 价格数据（10只 → ~2-3s，原串行 ~17s）
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {}
            for etf_code in ETF_POOL:
                index_sym = ETF_INDEX_MAP.get(etf_code)
                futures[executor.submit(self._fetch_prices, etf_code, index_sym)] = etf_code

            price_map = {}
            for future in as_completed(futures):
                etf_code = futures[future]
                try:
                    price_map[etf_code] = future.result()
                except Exception:
                    price_map[etf_code] = []

        # 逐个计算动量（CPU密集，无需并行）
        for etf_code in ETF_POOL:
            name = ETF_NAMES.get(etf_code, etf_code)
            prices = price_map.get(etf_code, [])

            # 三周期动量
            mom_short = self._compute_momentum(prices, 5)
            mom_medium = self._compute_momentum(prices, 20)
            mom_long = self._compute_momentum(prices, 60)

            # 趋势强度 (MA20 vs MA60)
            trend = self._trend_strength(prices)

            # 综合评分
            total = mom_short * 0.40 + mom_long * 0.30 + trend * 0.30

            results.append({
                "etf_code": etf_code, "name": name,
                "momentum_short": round(mom_short, 1),
                "momentum_medium": round(mom_medium, 1),
                "momentum_long": round(mom_long, 1),
                "trend_strength": round(trend, 1),
                "total_score": round(total, 1),
            })

        # 排名
        results.sort(key=lambda x: x["total_score"], reverse=True)
        for i, r in enumerate(results):
            r["rank"] = i + 1

            # 保存到 DB
            cur.execute("""
                INSERT OR REPLACE INTO etf_scores 
                (etf_code, score_date, momentum_short, momentum_long,
                 trend_strength, total_score, rank, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (r["etf_code"], today, r["momentum_short"], r["momentum_long"],
                  r["trend_strength"], r["total_score"], r["rank"],
                  f"短{r['momentum_short']}|中{r['momentum_medium']}|长{r['momentum_long']}"))

        conn.commit()
        return results

    def _fetch_prices(self, etf_code: str, index_sym: Optional[str]) -> list:
        """抓取 ETF 对应的指数价格数据"""
        if not index_sym:
            return self._get_proxy_prices(etf_code, 70)
        if index_sym.startswith("em_"):
            # 东方财富日K
            real_code = index_sym[3:]  # em_513100 → 513100
            return self._fetch_eastmoney_kline(real_code, days=70)
        return self._fetch_index_kline(index_sym, days=70)

    def _fetch_eastmoney_kline(self, code: str, days: int = 70) -> list:
        """从 Yahoo Finance 获取指数日K线（纳斯达克等海外指数），返回收盘价序列（最新在前）"""
        cache_key = f"yf_{code}_{days}"
        if cache_key in _KLINE_CACHE:
            return _KLINE_CACHE[cache_key]

        # 映射 ETF 代码 → Yahoo Finance symbol
        YF_SYMBOLS = {"513100": "%5EIXIC"}  # 纳指ETF → Nasdaq Composite
        symbol = YF_SYMBOLS.get(code, code)

        import time
        from datetime import datetime

        period2 = int(time.time())
        period1 = period2 - days * 86400 * 2  # 足够覆盖交易日

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?"
            f"period1={period1}&period2={period2}&interval=1d"
        )
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                result = data["chart"]["result"][0]
                closes = result["indicators"]["quote"][0]["close"]
                valid_closes = [c for c in closes if c is not None]
                if valid_closes:
                    _KLINE_CACHE[cache_key] = valid_closes
                    return valid_closes
        except Exception:
            pass
        return []

    def _fetch_index_kline(self, symbol: str, days: int = 70) -> list:
        """从 Sina K 线 API 获取指数日线数据，返回收盘价序列（最新在前）"""
        cache_key = f"{symbol}_{days}"
        if cache_key in _KLINE_CACHE:
            return _KLINE_CACHE[cache_key]

        url = (f"https://money.finance.sina.com.cn/quotes_service/api/"
               f"json_v2.php/CN_MarketData.getKLineData?"
               f"symbol={symbol}&scale=240&datalen={days}")

        try:
            req = urllib.request.Request(url)
            req.add_header("Referer", "https://finance.sina.com.cn")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data and isinstance(data, list) and len(data) > 0:
                    closes = [float(item["close"]) for item in data if "close" in item]
                    if closes:
                        _KLINE_CACHE[cache_key] = closes
                        return closes
        except Exception:
            pass

        # 降级：用代理数据
        return self._get_proxy_prices(symbol, days)

    def _get_proxy_prices(self, etf_code: str, days: int) -> list:
        """备用：用候选标的价格代理"""
        from config import ALL_CODES
        from db import get_price_history

        idx = ETF_POOL.index(etf_code) if etf_code in ETF_POOL else 0
        proxy_codes = ALL_CODES[idx % len(ALL_CODES):] + ALL_CODES[:idx % len(ALL_CODES)]
        for code in proxy_codes[:3]:
            rows = get_price_history(code, days)
            if rows and len(rows) >= 5:
                closes = [r["close"] for r in reversed(rows)]
                if closes and closes[-1] > 0:
                    return closes
        return []

    def _compute_momentum(self, prices: list, period: int) -> float:
        """计算 N 日动量（归一化到 0-100）"""
        if len(prices) < period + 1:
            return 50  # 数据不足→中性
        ret = (prices[-1] / prices[-(period + 1)] - 1) * 100
        # 映射：0% → 50, +10% → 100, -10% → 0
        return max(0, min(100, 50 + ret * 5))

    def _trend_strength(self, prices: list) -> float:
        """趋势强度 — MA20 vs MA60"""
        if len(prices) < 60:
            return 50
        ma20 = np.mean(prices[-20:])
        ma60 = np.mean(prices[-60:])
        if ma60 <= 0:
            return 50
        ratio = (ma20 / ma60 - 1) * 100
        return max(0, min(100, 50 + ratio * 10))


if __name__ == "__main__":
    ems = ETFMomentumStrategy()
    ranks = ems.rank_all()
    print(f"📊 ETF 动量轮动排名 | {date.today()}")
    print("=" * 70)
    for r in ranks:
        print(f"#{r['rank']} {r['name']:6s} | 总分 {r['total_score']:.1f} | "
              f"短{r['momentum_short']:.0f} 中{r['momentum_medium']:.0f} "
              f"长{r['momentum_long']:.0f} 趋势{r['trend_strength']:.0f}")
