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
