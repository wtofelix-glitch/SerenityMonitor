"""
基本面因子引擎 — 集成AKShare同花顺通道获取财务数据
计算PE/PB/ROE/EPS因子得分，整合到Serenity评分体系
"""
import os
import json
import logging
from datetime import datetime, date
from typing import Optional

# AKShare 在 _get_ak() 中懒加载（避免模块 import 时卡30秒）
# _AKSHARE_OK 在 _get_ak() 首次调用时设置
_AKSHARE_OK = None  # None=未检查, True=可用, False=未安装
import pandas as pd

from config import STOCK_MAP, ALL_CODES
from data_engine import fetch_single

logger = logging.getLogger(__name__)

# ============================================================
# 缓存配置
# ============================================================
CACHE_PATH = os.path.expanduser("~/.serenity_fundamental_cache.json")
CACHE_TTL_HOURS = 24


# ============================================================
# 工具函数
# ============================================================

def _parse_number(val) -> Optional[float]:
    """解析中文数字字符串，如 '12.67亿' → 12.67, '3000.48万' → 0.300048, False → None"""
    if val is None or val is False or val == 'False':
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        if '亿' in s:
            return float(s.replace('亿', ''))
        elif '万' in s:
            return float(s.replace('万', '')) / 10000
        else:
            return float(s)
    except (ValueError, TypeError):
        return None


def _parse_pct(val) -> Optional[float]:
    """解析百分比字符串，如 '24.88%' → 24.88, False → None"""
    if val is None or val is False or val == 'False':
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace('%', '')
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _get_current_price(code: str) -> Optional[float]:
    """通过新浪行情获取当前价格"""
    try:
        data = fetch_single(code)
        if data:
            return data.get("price") or data.get("close", 0)
        return None
    except Exception:
        return None


# ============================================================
# 主类
# ============================================================

class FundamentalEngine:
    _ak = None
    
    @staticmethod
    def _get_ak():
        """懒加载 akshare（仅在首次调用时触发缓慢的导入）"""
        global _AKSHARE_OK
        if FundamentalEngine._ak is None:
            if _AKSHARE_OK is None:
                # 首次调用：尝试导入（这步会卡30秒，只有第一次调用时触发）
                try:
                    import akshare as ak
                    FundamentalEngine._ak = ak
                    _AKSHARE_OK = True
                except ImportError:
                    _AKSHARE_OK = False
                    print("⚠️  akshare 未安装，基本面功能不可用")
            if not _AKSHARE_OK:
                raise ImportError("akshare 未安装")
        return FundamentalEngine._ak
    """A股基本面因子引擎 — 集成AKShare同花顺通道"""

    def __init__(self):
        self._cache = {}  # {code: {data, timestamp}} — signal cache (TTL=24h)
        self._df_cache = {}  # {code: DataFrame} — session cache for bulk fetch
        self._load_cache()

    # ---- 数据获取 ----

    def _fetch_cached(self, code: str) -> Optional['pd.DataFrame']:
        """带 session 缓存的财务数据获取 — 同一次评分中只请求一次"""
        if code in self._df_cache:
            return self._df_cache[code]
        df = self._fetch_cached(code)
        if df is not None:
            self._df_cache[code] = df
        return df

    def clear_df_cache(self):
        """清除 session 级缓存（下次评分前调用）"""
        self._df_cache = {}

    @staticmethod
    def _fetch_financial_data(code: str) -> Optional[pd.DataFrame]:
        """
        调用AKShare获取同花顺财务摘要数据
        临时清理代理环境变量以避免代理干扰
        """
        old_http = os.environ.pop('HTTP_PROXY', None)
        old_https = os.environ.pop('HTTPS_PROXY', None)
        old_http_lower = os.environ.pop('http_proxy', None)
        old_https_lower = os.environ.pop('https_proxy', None)
        try:
            df = self._get_ak().stock_financial_abstract_ths(symbol=code)
            if df is not None and not df.empty:
                return df
            return None
        except Exception as e:
            logger.debug("AKShare fetch failed for %s: %s", code, e)
            return None
        finally:
            # 恢复代理环境变量
            if old_http:
                os.environ['HTTP_PROXY'] = old_http
            if old_https:
                os.environ['HTTPS_PROXY'] = old_https
            if old_http_lower:
                os.environ['http_proxy'] = old_http_lower
            if old_https_lower:
                os.environ['https_proxy'] = old_https_lower

    # ---- 获取单只股票财务数据 ----

    def get_financials(self, code: str) -> Optional[dict]:
        """
        获取指定股票的最新财务数据

        Returns
        -------
        dict or None
            {
                "code": "600176",
                "name": "中国巨石",
                "report_date": "2026-03-31",
                "eps": 0.3193,
                "eps_growth": 73.48,         # 净利润同比增长率(%)
                "bps": 8.07,
                "roe": 4.00,
                "net_profit": 12.67,
                "revenue": 52.82,
                "net_margin": 24.88,
            }
        """
        try:
            df = self._fetch_cached(code)
            if df is None or df.empty:
                return None

            # 最新行（最后一行 = 最近报告期）
            latest = df.iloc[-1]
            report_date = str(latest['报告期'])

            eps = _parse_number(latest.get('基本每股收益'))
            bps = _parse_number(latest.get('每股净资产'))
            roe = _parse_pct(latest.get('净资产收益率'))
            net_profit = _parse_number(latest.get('净利润'))
            revenue = _parse_number(latest.get('营业总收入'))
            net_margin = _parse_pct(latest.get('销售净利率'))
            eps_growth = _parse_pct(latest.get('净利润同比增长率'))

            # 如果最新行没有ROE（可能为False），尝试用 净资产收益率-摊薄
            if roe is None:
                roe = _parse_pct(latest.get('净资产收益率-摊薄'))

            name = STOCK_MAP.get(code, {}).get("name", code)

            return {
                "code": code,
                "name": name,
                "report_date": report_date,
                "eps": eps,
                "eps_growth": eps_growth,
                "bps": bps,
                "roe": roe,
                "net_profit": net_profit,
                "revenue": revenue,
                "net_margin": net_margin,
            }

        except Exception:
            logger.debug("get_financials failed for %s", code, exc_info=True)
            return None

    # ---- 批量获取 ----

    def get_all_financials(self, codes: list = None) -> dict:
        """
        批量获取所有标的财务数据
        Returns {code: financial_dict}
        """
        if codes is None:
            codes = ALL_CODES
        result = {}
        for code in codes:
            fin = self.get_financials(code)
            if fin:
                result[code] = fin
        return result

    # ---- PE / PB 计算 ----

    def compute_pe_pb(self, code: str) -> tuple:
        """
        根据当前价格和财务数据计算PE(TTM)和PB

        Returns
        -------
        (pe, pb) or (None, None)
        """
        try:
            price = _get_current_price(code)
            if not price or price <= 0:
                return None, None

            df = self._fetch_cached(code)
            if df is None or df.empty:
                return None, None

            # --- 计算EPS(TTM) ---
            # 方法: 最新完整年度EPS + 最新季度EPS - 去年同期季度EPS
            # 数据为累计YTD口径: 2025-12-31是全年累计, 2026-03-31是Q1
            # 定位最新报告期
            latest_row = df.iloc[-1]
            latest_date = str(latest_row['报告期'])
            latest_eps = _parse_number(latest_row.get('基本每股收益'))

            if latest_eps is None or latest_eps <= 0:
                return None, None

            # 找最新完整年度 (12-31)
            annual_df = df[df['报告期'].str.contains('-12-31', na=False)]
            if annual_df.empty:
                return None, None
            latest_annual_row = annual_df.iloc[-1]
            full_year_eps = _parse_number(latest_annual_row.get('基本每股收益'))

            if full_year_eps is None or full_year_eps <= 0:
                eps_ttm = latest_eps  # fallback
            else:
                latest_year = str(latest_annual_row['报告期'])[:4]  # e.g. "2025"

                # 判断最新报告期是否在同一年的年报之后
                if latest_date.endswith('-12-31'):
                    # 最新报告就是年报
                    eps_ttm = latest_eps
                elif latest_date.endswith(('03-31', '06-30', '09-30')):
                    # 非年报: 需要找去年同期的EPS
                    this_month_day = latest_date[5:]  # e.g., "03-31"
                    # 查找去年同期的累计EPS
                    last_year = str(int(latest_date[:4]) - 1)
                    last_year_date = f"{last_year}-{this_month_day}"

                    last_year_mask = df['报告期'] == last_year_date
                    if last_year_mask.any():
                        same_q_last_year = df.loc[last_year_mask].iloc[0]
                        same_q_eps = _parse_number(same_q_last_year.get('基本每股收益'))
                        if same_q_eps is not None:
                            eps_ttm = full_year_eps + latest_eps - same_q_eps
                        else:
                            eps_ttm = full_year_eps
                    else:
                        eps_ttm = full_year_eps
                else:
                    eps_ttm = latest_eps

            if eps_ttm is None or eps_ttm <= 0:
                return None, None

            # --- 每股净资产 ---
            bps = _parse_number(latest_row.get('每股净资产'))
            if bps is None or bps <= 0:
                return None, None

            pe = round(price / eps_ttm, 2)
            pb = round(price / bps, 2)

            return pe, pb

        except Exception:
            logger.debug("compute_pe_pb failed for %s", code, exc_info=True)
            return None, None

    # ---- 基本面得分 ----

    def compute_fundamental_score(self, code: str) -> Optional[float]:
        """
        计算综合基本面得分 [-1, 1]

        收益 = 0.2 * (-PE_score) + 0.2 * (-PB_score) + 0.3 * (ROE_norm) + 0.3 * (EPS_growth_norm)

        采用全市场截面百分位方法:
        - PE/PB: 百分位越高=越贵=得分越低 (负相关)
        - ROE: 百分位越高=盈利能力越强=得分越高
        - EPS增长: 百分位越高=成长性越好=得分越高
        """
        try:
            fin = self.get_financials(code)
            if not fin:
                return None

            # 先获取全市场数据用于截面比较
            all_financials = self.get_all_financials()

            # 计算标的的PE/PB
            pe, pb = self.compute_pe_pb(code)
            if pe is None or pb is None or pe <= 0 or pb <= 0:
                return None

            # ---- 收集全市场截面数据 ----
            all_pe = []
            all_pb = []
            all_roe = []
            all_eps_growth = []

            for c in all_financials:
                c_pe, c_pb = self.compute_pe_pb(c)
                if c_pe and c_pe > 0:
                    all_pe.append(c_pe)
                if c_pb and c_pb > 0:
                    all_pb.append(c_pb)
                c_fin = all_financials[c]
                if c_fin.get('roe') is not None:
                    all_roe.append(c_fin['roe'])
                if c_fin.get('eps_growth') is not None:
                    all_eps_growth.append(c_fin['eps_growth'])

            # ---- 计算百分位得分 ----
            def percentile_pos(val, collection):
                """val在collection中的百分位 [0, 1]"""
                if not collection:
                    return 0.5
                return sum(1 for v in collection if v <= val) / len(collection)

            # PE百分位: 越高=越贵=负贡献 (用1-pct让高PE=高score, 公式中用 -PE_score)
            pe_pct = percentile_pos(pe, all_pe) if all_pe else 0.5

            # PB百分位
            pb_pct = percentile_pos(pb, all_pb) if all_pb else 0.5

            # ROE百分位: 越高越好 (直接正贡献)
            roe_val = fin.get('roe') or 0
            roe_pct = percentile_pos(roe_val, all_roe) if all_roe else 0.5

            # EPS增长百分位: 越高越好
            eps_growth_val = fin.get('eps_growth') or 0
            eps_growth_pct = percentile_pos(eps_growth_val, all_eps_growth) if all_eps_growth else 0.5

            # ---- 加权综合 ----
            # PE/PB是负向因子: PE越高(贵)得分越低
            raw = (
                0.2 * (-pe_pct) +
                0.2 * (-pb_pct) +
                0.3 * roe_pct +
                0.3 * eps_growth_pct
            )

            # 映射到 [-1, 1]
            # 理论范围 [-0.4, 0.6]，缩放到 [-1, 1]
            if -0.4 <= raw <= 0.6:
                scaled = (raw + 0.4) / 1.0 * 2 - 1  # [-0.4, 0.6] → [-1, 1]
            else:
                scaled = max(-1, min(1, raw * 2))

            return round(max(-1.0, min(1.0, scaled)), 4)

        except Exception:
            logger.debug("compute_fundamental_score failed for %s", code, exc_info=True)
            return None

    # ---- 信号接口 ----

    def get_fundamental_signal(self, code: str) -> Optional[float]:
        """
        获取基本面信号 [-1, 1]
        带缓存 (TTL = CACHE_TTL_HOURS)
        """
        # 检查缓存
        cached = self._cache.get(code)
        if cached:
            ts = cached.get("timestamp", 0)
            signal = cached.get("signal")
            if signal is not None and self._is_cache_fresh(ts):
                return signal

        # 重新计算
        signal = self.compute_fundamental_score(code)
        if signal is not None:
            self._cache[code] = {
                "signal": signal,
                "timestamp": datetime.now().timestamp(),
            }
            self._save_cache()

        return signal

    # ---- 缓存管理 ----

    def _is_cache_fresh(self, timestamp: float) -> bool:
        """检查缓存是否在TTL内"""
        if not timestamp:
            return False
        elapsed = datetime.now().timestamp() - timestamp
        return elapsed < CACHE_TTL_HOURS * 3600

    def _load_cache(self):
        """从磁盘加载缓存"""
        try:
            if os.path.exists(CACHE_PATH):
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 只加载仍在TTL内的缓存
                now = datetime.now().timestamp()
                for code, entry in data.items():
                    if now - entry.get("timestamp", 0) < CACHE_TTL_HOURS * 3600:
                        self._cache[code] = entry
        except (json.JSONDecodeError, IOError):
            pass

    def _save_cache(self):
        """保存缓存到磁盘"""
        try:
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except IOError:
            pass

    def clear_cache(self):
        """清除缓存"""
        self._cache = {}
        try:
            if os.path.exists(CACHE_PATH):
                os.remove(CACHE_PATH)
        except IOError:
            pass
