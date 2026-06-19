"""
市场风格感知模块 — 判断当前市场处于牛市/熊市/震荡市
用于动态调整评分权重和策略分配
"""
from datetime import date, timedelta
import numpy as np

from db import get_price_history


class MarketSense:
    """市场风格感知器"""

    def __init__(self, index_code: str = "sh000001"):
        self.index_code = index_code

    def get_market_regime(self) -> dict:
        """
        判断当前市场风格
        基于上证指数近 N 日涨跌幅 + 波动率 + 成交量变化

        Returns
        -------
        dict with: regime_label, score, details
        """
        # 用候选标的的均价变化来近似判断
        from config import ALL_CODES

        all_returns = []
        all_volumes = []

        for code in ALL_CODES[:3]:  # 取前几只做代表
            rows = get_price_history(code, 60)
            if len(rows) < 20:
                continue
            closes = [r["close"] for r in reversed(rows)]
            volumes = [r["volume"] for r in reversed(rows)]

            if len(closes) >= 20:
                ret_20 = (closes[-1] / closes[-21] - 1) * 100 if closes[-21] > 0 else 0
                all_returns.append(ret_20)

            if len(volumes) >= 20:
                vol_ratio = np.mean(volumes[-5:]) / max(1, np.mean(volumes[-20:-5]))
                all_volumes.append(vol_ratio)

        if not all_returns:
            return {"regime_label": "震荡市", "score": 50, "details": {}}

        avg_return = np.mean(all_returns)
        avg_vol_ratio = np.mean(all_volumes) if all_volumes else 1.0

        # 波动率估算
        daily_returns = []
        for code in ALL_CODES[:2]:
            rows = get_price_history(code, 20)
            if len(rows) >= 10:
                closes = [r["close"] for r in reversed(rows)]
                dr = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
                daily_returns.extend(dr)
        volatility = np.std(daily_returns) * 100 if daily_returns else 2.0

        # 判定
        if avg_return > 15 and avg_vol_ratio > 1.2:
            regime = "牛市"
        elif avg_return > 8 and avg_vol_ratio > 1.0:
            regime = "结构性牛市"
        elif avg_return < -10 and avg_vol_ratio > 1.3:
            regime = "熊市"
        elif volatility > 3:
            regime = "震荡市"
        else:
            regime = "震荡市"

        return {
            "regime_label": regime,
            "score": round(50 + avg_return * 2, 1),
            "details": {
                "avg_20d_return": round(avg_return, 2),
                "avg_vol_ratio": round(avg_vol_ratio, 2),
                "volatility": round(volatility, 2),
            }
        }

    def generate_summary(self) -> str:
        """生成市场摘要"""
        regime = self.get_market_regime()
        d = regime.get("details", {})
        return (f"市场风格: {regime['regime_label']} | "
                f"20日收益: {d.get('avg_20d_return', 'N/A')}% | "
                f"波动率: {d.get('volatility', 'N/A')}%")

    def get_operational_mode(self) -> dict:
        """
        返回当前操作模式: trend(趋势跟踪) 或 mean_revert(均值回归)
        
        判断依据:
        - 20日收益 < -3% 且不是熊市放量 → mean_revert (超跌反弹机会)
        - 20日收益 > +5% 且放量 → trend (趋势跟踪)
        - 其余 → neutral (混合模式)
        
        Returns dict: {mode, factor_invert, sell_trigger_weight, regime_label}
        """
        regime = self.get_market_regime()
        d = regime.get("details", {})
        avg_ret = d.get("avg_20d_return", 0)
        vol_ratio = d.get("avg_vol_ratio", 1.0)
        
        # 均值回归模式：跌幅>3% 且非恐慌放量
        if avg_ret < -3 and vol_ratio < 1.5:
            return {
                "mode": "mean_revert",
                "factor_invert": True,           # 翻转负IC因子
                "sell_trigger_weight": 0.3,       # 卖出触发器降至30%力度
                "buy_threshold_shift": -6,         # 买入门槛降低6分
                "regime_label": regime["regime_label"],
                "avg_20d_return": avg_ret,
                # 均值回归止盈参数：见利就跑，快进快出
                "profit_take_tiers": [0.05, 0.10, 0.18],  # +5%/+10%/+18%
                "exit_levels": [0.5, 0.5],                # 每档出50%
            }
        
        # 趋势跟踪模式：涨幅>5% 或放量上涨
        if avg_ret > 5 or (avg_ret > 2 and vol_ratio > 1.2):
            return {
                "mode": "trend",
                "factor_invert": False,
                "sell_trigger_weight": 1.0,        # 卖出触发器正常力度
                "buy_threshold_shift": 0,
                "regime_label": regime["regime_label"],
                "avg_20d_return": avg_ret,
                "profit_take_tiers": [0.12, 0.22, 0.38],  # 趋势市让利润奔跑
                "exit_levels": [0.3, 0.5],
            }
        
        # 弱均值回归模式：跌幅1-3% 或 动量IC为负 → 部分翻转（择股重于择时）
        # 触发条件：avg_ret < -1% 且 动量/技术面 IC 均值为负
        _weak_mr = False
        try:
            from factor_ic import compute_rank_ic
            _ic = compute_rank_ic(days=20, window=20)
            _mom_ic = _ic.get("mean_ic", {}).get("momentum_score", 0)
            _tech_ic = _ic.get("mean_ic", {}).get("technical_score", 0)
            if _mom_ic < -0.05 or (_mom_ic < 0 and _tech_ic < 0):
                _weak_mr = True
        except Exception:
            pass
        if _weak_mr:
            return {
                "mode": "mean_revert",
                "factor_invert": True,
                "sell_trigger_weight": 0.5,       # 卖出触发器降至50%力度（弱MR不降太多）
                "buy_threshold_shift": -5,         # 买入门槛降低5分（弱MR更积极择股）
                "regime_label": regime["regime_label"],
                "avg_20d_return": avg_ret,
                "_weak_mr": True,                 # 标记弱MR，供下游区分
                "profit_take_tiers": [0.06, 0.12, 0.22],  # +6%/+12%/+22%
                "exit_levels": [0.5, 0.5],
            }
        
        # 中性/震荡
        return {
            "mode": "neutral",
            "factor_invert": False,
            "sell_trigger_weight": 0.7,            # 适度降权
            "buy_threshold_shift": -3,
            "regime_label": regime["regime_label"],
            "avg_20d_return": avg_ret,
            "profit_take_tiers": [0.08, 0.16, 0.28],  # +8%/+16%/+28%
            "exit_levels": [0.5, 0.5],
        }
