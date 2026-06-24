"""
风险矩阵引擎 — 持仓相关性 + VaR + 压力测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from db import get_conn
from config import STOCK_MAP, CAPITAL_CONFIG

def _get_weekly_returns(code, weeks=52):
    """从 daily_snapshots 计算周收益率序列"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, close FROM daily_snapshots
        WHERE code=? ORDER BY date ASC
    """, (code,)).fetchall()
    conn.close()
    if len(rows) < weeks * 5 + 10:
        return None
    closes = [r["close"] for r in rows]
    # 转换为周频: 每5个交易日取一个
    weekly = [closes[i] for i in range(len(closes) - 1, 0, -5) if i >= 5]
    weekly = weekly[:weeks][::-1]  # oldest first
    if len(weekly) < 10:
        return None
    returns = [(weekly[i] - weekly[i - 1]) / weekly[i - 1] for i in range(1, len(weekly))]
    return np.array(returns)

def compute_risk_matrix(codes=None):
    """计算持仓协方差矩阵 + VaR + 最大回撤"""
    from portfolio import PortfolioManager
    pm = PortfolioManager()
    pv = pm.get_portfolio_value()

    if codes is None:
        from config import ALL_CODES as _ALL
        codes = [p["code"] for p in pv.get("positions", [])
                 if p["code"] in _ALL] if pv.get("positions") else []

    if len(codes) < 2:
        return {"error": "需>=2只有效持仓(排除无行情数据的标的)", "positions": len(codes)}

    # 获取周收益率
    returns = {}
    for code in codes:
        r = _get_weekly_returns(code)
        if r is not None:
            returns[code] = r

    if len(returns) < 2:
        return {"error": "insufficient price history", "available": list(returns.keys())}

    # 对齐到共同窗口
    common_codes = list(returns.keys())
    min_len = min(len(returns[c]) for c in common_codes)
    aligned = np.column_stack([returns[c][-min_len:] for c in common_codes])

    # 协方差矩阵 (年化, 52周)
    cov = np.cov(aligned.T) * 52
    corr = np.corrcoef(aligned.T)

    # 计算组合 VaR (95% 置信, parametric)
    weights = []
    for code in common_codes:
        pos = next((p for p in pv.get("positions", []) if p["code"] == code), None)
        w = pos["current_value"] / pv["total_value"] if pos else 0
        weights.append(w)
    weights = np.array(weights)
    weights = weights / weights.sum()

    port_return = np.sum(np.mean(aligned, axis=0) * weights) * 52
    port_vol = np.sqrt(weights @ cov @ weights)
    var_95 = port_vol * 1.645  # parametric VaR 95%
    var_99 = port_vol * 2.326  # parametric VaR 99%

    # 历史最大回撤
    equity = np.cumprod(1 + aligned @ weights)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = float(np.min(dd))

    # 构建矩阵输出
    names = [STOCK_MAP.get(c, {}).get("name", c) for c in common_codes]
    matrix = {
        "codes": common_codes,
        "names": names,
        "correlation": [[round(float(corr[i, j]), 3) for j in range(len(common_codes))]
                        for i in range(len(common_codes))],
        "covariance": [[round(float(cov[i, j]), 4) for j in range(len(common_codes))]
                       for i in range(len(common_codes))],
        "volatilities": [round(float(np.std(aligned[:, i]) * np.sqrt(52)) * 100, 1)
                        for i in range(len(common_codes))],
        "weights": [round(float(w) * 100, 1) for w in weights],
    }

    risk = {
        "portfolio_return_annual": round(float(port_return) * 100, 1),
        "portfolio_vol_annual": round(float(port_vol) * 100, 1),
        "var_95_pct": round(float(var_95) * 100, 1),
        "var_99_pct": round(float(var_99) * 100, 1),
        "max_drawdown_pct": round(float(max_dd) * 100, 1),
        "sharpe": round(float(port_return / port_vol), 2) if port_vol > 0 else 0,
        "diversification_ratio": round(float(np.sum(np.sqrt(np.diag(cov)) * weights) / port_vol), 2) if port_vol > 0 else 0,
    }

    # 压力测试
    stress = {
        "2008_crisis": round(float(port_vol * 2.5 * 100), 1),
        "2015_crash": round(float(port_vol * 2.0 * 100), 1),
        "covid_crash": round(float(port_vol * 1.8 * 100), 1),
    }

    return {"matrix": matrix, "risk": risk, "stress": stress, "total_value": pv["total_value"]}
