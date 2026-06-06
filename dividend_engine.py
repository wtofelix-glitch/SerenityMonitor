"""
红利低波主引擎 — DividentEngine.score_all() 综合评分 + 调仓信号
基于 config_dividend.py 的 8 只主板红利标的池
"""
import numpy as np
from datetime import date
from db import get_conn, get_price_history

# 红利标的池（主板，高股息+低波动）
DIVIDEND_POOL = [
    "600585",  # 海螺水泥
    "601088",  # 中国神华
    "600036",  # 招商银行
    "601398",  # 工商银行
    "600900",  # 长江电力
    "601857",  # 中国石油
    "600019",  # 宝钢股份
    "601006",  # 大秦铁路
]

DIVIDEND_NAMES = {
    "600585": "海螺水泥", "601088": "中国神华", "600036": "招商银行",
    "601398": "工商银行", "600900": "长江电力", "601857": "中国石油",
    "600019": "宝钢股份", "601006": "大秦铁路",
}


class DividendEngine:
    """红利低波综合评分引擎"""

    def score_all(self) -> list[dict]:
        """
        对所有红利标的进行四维评分：
        - 股息率评分 (25%)
        - 低波动评分 (35%)
        - 估值评分 (25%)
        - 质量评分 (15%)
        """
        today = date.today().isoformat()
        conn = get_conn()
        cur = conn.cursor()
        results = []

        for code in DIVIDEND_POOL:
            name = DIVIDEND_NAMES.get(code, code)
            prices = self._get_prices(code, 60)

            # 各维度评分
            div_score = self._score_dividend_yield(code)
            vol_score = self._score_lowvol(code, prices)
            val_score = self._score_valuation(code)
            qual_score = self._score_quality(code)

            total = div_score * 0.25 + vol_score * 0.35 + val_score * 0.25 + qual_score * 0.15

            # 保存到 DB
            cur.execute("""
                INSERT OR REPLACE INTO dividend_scores 
                (code, score_date, dividend_yield_score, low_vol_score, 
                 valuation_score, quality_score, total_score, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, today, round(div_score, 1), round(vol_score, 1),
                  round(val_score, 1), round(qual_score, 1), round(total, 1),
                  f"股息{div_score:.1f}|低波{vol_score:.1f}|估值{val_score:.1f}|质量{qual_score:.1f}"))

            results.append({
                "code": code, "name": name, "total_score": round(total, 1),
                "dividend_yield_score": round(div_score, 1),
                "low_vol_score": round(vol_score, 1),
                "valuation_score": round(val_score, 1),
                "quality_score": round(qual_score, 1),
            })

        conn.commit()
        results.sort(key=lambda x: x["total_score"], reverse=True)
        return results

    def _get_prices(self, code: str, days: int) -> list:
        """获取价格序列（最新在前）"""
        rows = get_price_history(code, days)
        return [r["close"] for r in reversed(rows)] if rows else []

    def _score_dividend_yield(self, code: str) -> float:
        """股息率评分 — 基于板块均值估算"""
        # 简化版：按行业给默认股息率
        mapping = {
            "600585": 4.5, "601088": 6.0, "600036": 4.0,
            "601398": 5.5, "600900": 3.8, "601857": 5.0,
            "600019": 4.2, "601006": 5.8,
        }
        dy = mapping.get(code, 3.5)
        # 映射到 0-100：3% → 50, 7%+ → 100
        return min(100, max(0, dy * 15))

    def _score_lowvol(self, code: str, prices: list) -> float:
        """低波动评分 — 年化波动率越低越好"""
        if len(prices) < 10:
            return 50
        returns = [(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
        vol = np.std(returns) * np.sqrt(252) * 100  # 年化波动率%
        # 映射：10%波动→80分，30%波动→30分
        return max(0, min(100, 100 - vol * 2.5))

    def _score_valuation(self, code: str) -> float:
        """估值评分 — 基于板块均值"""
        # 简化版：红利股估值普遍合理
        mapping = {
            "600585": 10, "601088": 8, "600036": 6,
            "601398": 5.5, "600900": 20, "601857": 9,
            "600019": 7, "601006": 9,
        }
        pe = mapping.get(code, 12)
        # 映射：PE<8→80+，PE>20→40-
        return max(0, min(100, 100 - pe * 3))

    def _score_quality(self, code: str) -> float:
        """质量评分 — ROE + 负债率"""
        mapping = {
            "600585": 15, "601088": 14, "600036": 12,
            "601398": 11, "600900": 16, "601857": 10,
            "600019": 8, "601006": 9,
        }
        roe = mapping.get(code, 12)
        return min(100, roe * 6)


if __name__ == "__main__":
    de = DividendEngine()
    results = de.score_all()
    print(f"📊 红利低波评分 | {date.today()}")
    print("=" * 60)
    for r in results:
        print(f"{r['name']:6s} | 总分 {r['total_score']:.1f} | "
              f"股息{r['dividend_yield_score']:.0f} "
              f"低波{r['low_vol_score']:.0f} "
              f"估值{r['valuation_score']:.0f} "
              f"质量{r['quality_score']:.0f}")
