"""策略参数网格搜索 — 自动寻优止损/止盈/仓位"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import run_backtest, TrendFollowingStrategy, MeanReversionStrategy
from config import ALL_CODES, STOCK_MAP
from itertools import product
import json

def grid_search(code, strategy_name='trend', capital=50000):
    """网格搜索最优参数组合"""
    if strategy_name == 'trend':
        param_grid = {
            'ma_short': [5,10,15],
            'ma_long': [20,30,40],
        }
        StrategyClass = TrendFollowingStrategy
    elif strategy_name == 'mean_revert':
        param_grid = {
            'lookback': [10,15,20],
            'entry_threshold': [-2.0, -2.5, -3.0],
            'exit_threshold': [0.5, 1.0, 1.5],
        }
        StrategyClass = MeanReversionStrategy
    else:
        return {"error": f"unknown strategy: {strategy_name}"}

    results = []
    best = None
    for combo in product(*param_grid.values()):
        params = dict(zip(param_grid.keys(), combo))
        try:
            strat = StrategyClass()
            for k, v in params.items():
                setattr(strat, k, v)
            r = run_backtest(code, strat, initial_capital=capital)
            results.append({
                "params": params,
                "return": r.get("total_return_pct", 0),
                "sharpe": r.get("sharpe_ratio", 0),
                "max_dd": r.get("max_drawdown_pct", 0),
                "win_rate": r.get("win_rate_pct", 0),
            })
            if best is None or r.get("total_return_pct", 0) > best["return"]:
                best = {"params": params, "return": r.get("total_return_pct", 0),
                        "sharpe": r.get("sharpe_ratio", 0), "max_dd": r.get("max_drawdown_pct", 0)}
        except Exception:
            continue

    if not results:
        return {"error": "no valid results"}

    name = STOCK_MAP.get(code, {}).get("name", code)
    return {"code": code, "name": name, "strategy": strategy_name,
            "best": best, "results": sorted(results, key=lambda x: x["return"], reverse=True)[:10],
            "total_combos": len(results)}

def optimize_all(strategy='trend'):
    """对所有池内标的执行网格搜索"""
    results = []
    for code in ALL_CODES:
        r = grid_search(code, strategy)
        if "error" not in r:
            results.append(r)
    results.sort(key=lambda x: x["best"]["return"], reverse=True)
    return results

if __name__ == "__main__":
    for code in ALL_CODES[:3]:
        r = grid_search(code, 'trend')
        if "error" not in r:
            print(f"{r['name']}({code}): best={r['best']['params']} ret={r['best']['return']:+.1f}%")
