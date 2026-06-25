"""
资金面维度模块 — 融资融券 / 大宗交易 / 股东户数 / 分红送转 (v3.3)
基于 a-stock-data V3.2.4 东财数据中心代码, 零第三方依赖, 内嵌限流保护.

用法:
    from capital_flow import MarginAnalyzer, BlockTradeScanner, HolderAnalyzer
"""

from __future__ import annotations
import time, random, json
from datetime import date, timedelta
from typing import Optional

import requests
from serenity_logger import get_logger

log = get_logger(__name__)

# — 东财统一限流 (同 a-stock-data em_get) —
EM_SESSION = None
EM_MIN_INTERVAL = 1.2  # 秒, 默认≥1s+抖动
EM_LAST_CALL = 0.0

def _em_get(url: str, params: dict = None, **kwargs) -> requests.Response:
    """东财数据中心统一查询 (内置串行限流 + 会话复用)"""
    global EM_SESSION, EM_LAST_CALL
    if EM_SESSION is None:
        EM_SESSION = requests.Session()
        EM_SESSION.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://data.eastmoney.com/",
        })
    elapsed = time.time() - EM_LAST_CALL
    if elapsed < EM_MIN_INTERVAL:
        time.sleep(EM_MIN_INTERVAL - elapsed + random.uniform(0, 0.3))
    resp = EM_SESSION.get(url, params=params, timeout=10, **kwargs)
    EM_LAST_CALL = time.time()
    resp.raise_for_status()
    return resp


# ══════════════════════════════════════════════════════════
# 1. 融资融券
# ══════════════════════════════════════════════════════════

class MarginAnalyzer:
    """融资融券分析 — 基于东财 datacenter-web"""

    MARGIN_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    def get_margin_history(self, code: str, days: int = 60) -> list[dict]:
        """获取个股融资融券日级明细

        Returns:
            [{date, rzye(融资余额), rzmre(融资买入额), rzche(融资偿还额),
              rqye(融券余额), rqmcl(融券卖出量), rqchl(融券偿还量)}, ...]
        """
        params = {
            "reportName": "RPTA_WEB_HSGGBASIC",
            "columns": "SECURITY_CODE,TRADE_DATE,RZYE,RZMRE,RZCHE,RQYE,RQMCL,RQCHL",
            "filter": f'(SECURITY_CODE="{code}")',
            "pageNumber": 1, "pageSize": days,
            "sortTypes": -1, "sortColumns": "TRADE_DATE",
            "source": "WEB", "client": "WEB",
        }
        try:
            resp = _em_get(self.MARGIN_URL, params=params)
            data = resp.json()
            if data.get("success"):
                return data["result"]["data"] or []
        except Exception as e:
            log.warning("融资融券查询失败 %s: %s", code, e)
        return []

    def get_margin_summary(self, code: str) -> dict:
        """融资融券概要 — 最近30日趋势

        Returns:
            {code, latest_rzye, avg_rzye_30d, trend_5d(+/-/flat), margin_ratio}
        """
        rows = self.get_margin_history(code, 60)
        if not rows:
            return {"code": code, "data_available": False}

        recent_5 = rows[:5]
        recent_30 = rows[:30]
        if not recent_30:
            return {"code": code, "data_available": False}

        rzye_vals = [r.get("RZYE", 0) or 0 for r in recent_30]
        latest = rzye_vals[0] if rzye_vals else 0
        avg_30 = sum(rzye_vals) / len(rzye_vals)

        # 趋势: 最近5日 vs 前5日
        rzye_5 = [r.get("RZYE", 0) or 0 for r in recent_5]
        prev_5 = [r.get("RZYE", 0) or 0 for r in rows[5:10]] if len(rows) >= 10 else rzye_5
        trend_5 = "up" if sum(rzye_5) > sum(prev_5) else ("down" if sum(rzye_5) < sum(prev_5) else "flat")

        return {
            "code": code,
            "data_available": True,
            "latest_rzye": round(latest / 1e8, 2),  # 转亿
            "avg_rzye_30d": round(avg_30 / 1e8, 2),
            "trend_5d": trend_5,
            "sample_days": len(recent_30),
        }


# ══════════════════════════════════════════════════════════
# 2. 大宗交易
# ══════════════════════════════════════════════════════════

class BlockTradeScanner:
    """大宗交易扫描 — 基于东财 datacenter-web"""

    BLOCK_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    def get_block_trades(self, code: str, days: int = 90) -> list[dict]:
        """获取个股近期大宗交易

        Returns:
            [{trade_date, deal_price, deal_amount(成交额万), close_today,
              premium(溢价率%), buyer_dept(买方营业部), seller_dept(卖方营业部)}, ...]
        """
        params = {
            "reportName": "RPTA_BLOCKTRADE",
            "columns": "SECURITY_CODE,TRADE_DATE,DEAL_PRICE,DEAL_AMOUNT,CLOSE_TODAY,PREMIUM,BUYER_DEPT,SELLER_DEPT",
            "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{self._since(days)}\')',
            "pageNumber": 1, "pageSize": 50,
            "sortTypes": -1, "sortColumns": "TRADE_DATE",
            "source": "WEB", "client": "WEB",
        }
        try:
            resp = _em_get(self.BLOCK_URL, params=params)
            data = resp.json()
            if data.get("success"):
                return data["result"]["data"] or []
        except Exception as e:
            log.warning("大宗交易查询失败 %s: %s", code, e)
        return []

    def get_premium_summary(self, code: str) -> dict:
        """大宗交易溢价率摘要"""
        rows = self.get_block_trades(code, 90)
        if not rows:
            return {"code": code, "data_available": False}

        premiums = [r.get("PREMIUM", 0) or 0 for r in rows]
        avg_prem = sum(premiums) / len(premiums)
        discount_count = sum(1 for p in premiums if p < 0)

        return {
            "code": code,
            "data_available": True,
            "total_trades": len(rows),
            "avg_premium_pct": round(avg_prem, 2),
            "discount_ratio": round(discount_count / len(rows) * 100, 1),
            "latest_premium": round(premiums[0], 2) if premiums else 0,
        }

    @staticmethod
    def _since(days: int) -> str:
        return (date.today() - timedelta(days=max(days, 7))).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════
# 3. 股东户数 (筹码集中度)
# ══════════════════════════════════════════════════════════

class HolderAnalyzer:
    """股东户数分析 — 基于东财 datacenter-web"""

    HOLDER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    def get_holder_history(self, code: str, periods: int = 8) -> list[dict]:
        """获取个股股东户数变化 (季度数据)

        Returns:
            [{report_date, holder_num(股东户数), holder_num_change(环比变化%),
              avg_holding(户均持股), avg_holding_mv(户均持股市值)}, ...]
        """
        params = {
            "reportName": "RPTA_HOLDERANALYSIS",
            "columns": "SECURITY_CODE,END_DATE,HOLDER_NUM,HOLDER_NUM_CHANGE,AVG_HOLD_NUM,AVG_HOLD_MV",
            "filter": f'(SECURITY_CODE="{code}")',
            "pageNumber": 1, "pageSize": periods,
            "sortTypes": -1, "sortColumns": "END_DATE",
            "source": "WEB", "client": "WEB",
        }
        try:
            resp = _em_get(self.HOLDER_URL, params=params)
            data = resp.json()
            if data.get("success"):
                return data["result"]["data"] or []
        except Exception as e:
            log.warning("股东户数查询失败 %s: %s", code, e)
        return []

    def get_concentration_signal(self, code: str) -> dict:
        """筹码集中度信号

        - 股东户数连续2期下降且降幅>5% → 筹码集中
        - 股东户数连续2期上升且增幅>10% → 筹码分散
        """
        rows = self.get_holder_history(code, 4)
        if len(rows) < 2:
            return {"code": code, "data_available": False}

        changes = [r.get("HOLDER_NUM_CHANGE", 0) or 0 for r in rows[:3]]
        latest_chg = changes[0] if changes else 0

        if len(changes) >= 2 and all(c < 0 for c in changes[:2]):
            if sum(abs(c) for c in changes[:2]) >= 10:
                signal = "concentrating"   # 筹码集中
            else:
                signal = "mild_concentrating"
        elif len(changes) >= 2 and all(c > 0 for c in changes[:2]):
            if sum(c for c in changes[:2]) >= 15:
                signal = "dispersing"       # 筹码分散
            else:
                signal = "mild_dispersing"
        else:
            signal = "neutral"

        return {
            "code": code,
            "data_available": True,
            "signal": signal,
            "latest_change_pct": round(latest_chg, 2),
            "latest_holders": rows[0].get("HOLDER_NUM", 0) if rows else 0,
            "sample_periods": len(rows),
        }


# ══════════════════════════════════════════════════════════
# 4. 分红送转
# ══════════════════════════════════════════════════════════

class DividendAnalyzer:
    """分红分析"""

    DIV_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    def get_dividend_history(self, code: str, years: int = 10) -> list[dict]:
        """获取个股历史分红送转"""
        params = {
            "reportName": "RPTA_DIVIDEND",
            "columns": "SECURITY_CODE,REPORT_DATE,PLAN_DATE,BONUS_PLAN,TRANSFER_SHARES,GIVE_SHARES,PROGRESS",
            "filter": f'(SECURITY_CODE="{code}")',
            "pageNumber": 1, "pageSize": 30,
            "sortTypes": -1, "sortColumns": "REPORT_DATE",
            "source": "WEB", "client": "WEB",
        }
        try:
            resp = _em_get(self.DIV_URL, params=params)
            data = resp.json()
            if data.get("success"):
                return data["result"]["data"] or []
        except Exception as e:
            log.warning("分红查询失败 %s: %s", code, e)
        return []

    def get_dividend_score(self, code: str) -> dict:
        """分红评分: 连续分红年数 + 平均股息率"""
        rows = self.get_dividend_history(code, 10)
        if not rows:
            return {"code": code, "data_available": False}

        # 连续分红年份
        years = sorted(set(r.get("REPORT_DATE", "")[:4] for r in rows if r.get("REPORT_DATE")), reverse=True)
        consecutive = 1
        for i in range(1, len(years)):
            if int(years[i-1]) - int(years[i]) == 1:
                consecutive += 1
            else:
                break

        # 平均每股派息(最近5次)
        bonuses = [r.get("BONUS_PLAN", 0) or 0 for r in rows[:5]]
        avg_bonus = sum(bonuses) / len(bonuses) if bonuses else 0

        return {
            "code": code,
            "data_available": True,
            "consecutive_years": consecutive,
            "avg_bonus_per_share": round(avg_bonus, 4),
            "total_payouts": len(rows),
        }


# ══════════════════════════════════════════════════════════
# 5. 综合资金面评分 (供 scorer 调用)
# ══════════════════════════════════════════════════════════

def compute_capital_score(code: str) -> dict:
    """综合资金面信号 (0-100, 基于融资/大宗/筹码/分红)

    评分逻辑:
    - 融资余额上升 + 大宗溢价 + 筹码集中 + 连续分红 → 高分
    - 融资余额下降 + 大宗折价 + 筹码分散 → 低分
    """
    score = 50.0
    signals = []
    data_points = 0

    # 融资趋势 (±15)
    try:
        ma = MarginAnalyzer()
        margin = ma.get_margin_summary(code)
        if margin.get("data_available"):
            data_points += 1
            if margin.get("trend_5d") == "up":
                score += 12
                signals.append("融资余额↑")
            elif margin.get("trend_5d") == "down":
                score -= 8
                signals.append("融资余额↓")
    except Exception:
        pass

    # 大宗溢价 (±10)
    try:
        bt = BlockTradeScanner()
        prem = bt.get_premium_summary(code)
        if prem.get("data_available"):
            data_points += 1
            avg_prem = prem.get("avg_premium_pct", 0)
            if avg_prem > 2:
                score += 10; signals.append("大宗溢价+")
            elif avg_prem < -3:
                score -= 8; signals.append("大宗折价-")
    except Exception:
        pass

    # 筹码集中度 (±12)
    try:
        ha = HolderAnalyzer()
        conc = ha.get_concentration_signal(code)
        if conc.get("data_available"):
            data_points += 1
            sig = conc.get("signal", "neutral")
            if sig == "concentrating":
                score += 12; signals.append("筹码集中")
            elif sig == "dispersing":
                score -= 10; signals.append("筹码分散")
    except Exception:
        pass

    # 分红连续性 (±8)
    try:
        da = DividendAnalyzer()
        div = da.get_dividend_score(code)
        if div.get("data_available"):
            data_points += 1
            if div.get("consecutive_years", 0) >= 5:
                score += 8; signals.append(f"连续{div['consecutive_years']}年分红")
            elif div.get("consecutive_years", 0) >= 3:
                score += 4
    except Exception:
        pass

    if data_points == 0:
        score = 50.0  # 无数据→中性

    return {
        "code": code,
        "score": round(max(0, min(100, score)), 1),
        "signals": signals,
        "data_points": data_points,
    }
