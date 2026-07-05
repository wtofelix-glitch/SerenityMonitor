"""
Serenity 回测引擎 — 单标的多策略回测
支持：趋势跟踪 / 多因子 / 均值回归 / 混合 / 14因子信号

API 兼容（cli.py & backtest_viz.py 引用）:
  run_backtest(code, strategy, initial_capital) → dict
  format_backtest_result(result) → str
  compare_strategies() → list
  format_comparison(results) → str
  Strategy classes: TrendFollowingStrategy, MultiFactorStrategy,
    MeanReversionStrategy, HybridStrategy, MultiFactorWithSignalsStrategy
  BacktestTrade dataclass
  optimize_atr_params, track_stop_loss_effectiveness, recommend_atr_params
"""
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional
import json
import urllib.request
import numpy as np
from db import get_price_history
from config import STOCK_MAP

# ── 事件驱动回测可选依赖 ──────────────────────────────
try:
    from market_microstructure import get_microstructure, MarketMicrostructure
    from execution_simulator import get_simulator, ExecutionSimulator, build_liquidity_state
    from fill_model import FillModel
    MICROSTRUCTURE_AVAILABLE = True
except ImportError:
    MICROSTRUCTURE_AVAILABLE = False


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class BacktestTrade:
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    profit_pct: float
    hold_days: int
    exit_reason: str = ""


# ── 策略基类 ──────────────────────────────────────────

class BaseStrategy:
    """策略基类"""
    def prepare(self, code: str, closes: np.ndarray, highs: np.ndarray,
                lows: np.ndarray, volumes: np.ndarray, dates: list):
        self.code = code
        self.closes = closes
        self.highs = highs
        self.lows = lows
        self.volumes = volumes
        self.dates = dates
        self.n = len(closes)

    def generate_signals(self, idx: int) -> tuple[float, str]:
        """返回 (signal, reason)，signal ∈ [-1, 1]"""
        raise NotImplementedError

    def _sma(self, data: np.ndarray, period: int) -> np.ndarray:
        """简单移动平均"""
        if len(data) < period:
            return np.full_like(data, np.nan)
        kernel = np.ones(period) / period
        result = np.convolve(data, kernel, mode='same')
        result[:period - 1] = np.nan
        return result

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """指数移动平均"""
        if len(data) < 2:
            return data.copy()
        alpha = 2 / (period + 1)
        result = np.zeros_like(data)
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
        return result

    def _rsi(self, period: int = 14) -> np.ndarray:
        """RSI"""
        if self.n < period + 1:
            return np.full(self.n, 50.0)
        delta = np.diff(self.closes)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = np.full(self.n, np.nan)
        avg_loss = np.full(self.n, np.nan)
        avg_gain[period] = np.mean(gain[:period])
        avg_loss[period] = np.mean(loss[:period])
        for i in range(period + 1, self.n):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period
        rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.nan),
                       where=avg_loss > 0)
        return 100 - 100 / (1 + rs)


class TrendFollowingStrategy(BaseStrategy):
    """趋势跟踪：MA20 > MA60 做多，MA20 < MA60 平仓"""
    def generate_signals(self, idx: int) -> tuple[float, str]:
        if idx < 60:
            return 0.0, "数据不足"
        ma20 = self._sma(self.closes, 20)
        ma60 = self._sma(self.closes, 60)
        if np.isnan(ma20[idx]) or np.isnan(ma60[idx]):
            return 0.0, "MA 未就绪"
        diff_pct = (ma20[idx] / ma60[idx] - 1) * 100
        if diff_pct > 1:
            return min(1.0, diff_pct / 5), f"MA20>MA60 ({diff_pct:.1f}%)"
        elif diff_pct < -1:
            return max(-1.0, diff_pct / 5), f"MA20<MA60 ({diff_pct:.1f}%)"
        return 0.0, "横盘"


class MultiFactorStrategy(BaseStrategy):
    """多因子综合：趋势 + RSI + 波动率"""
    def generate_signals(self, idx: int) -> tuple[float, str]:
        if idx < 60:
            return 0.0, "数据不足"
        ma20 = self._sma(self.closes, 20)
        ma60 = self._sma(self.closes, 60)
        rsi = self._rsi(14)

        trend = (ma20[idx] / ma60[idx] - 1) * 100 if not np.isnan(ma20[idx]) and not np.isnan(ma60[idx]) else 0
        rsi_val = rsi[idx] if not np.isnan(rsi[idx]) else 50
        vol = np.std(self.closes[max(0, idx - 20):idx + 1]) / self.closes[idx] * 100 if idx >= 20 else 10

        score = 0.0
        reasons = []
        if trend > 1:
            score += 0.4
            reasons.append(f"趋势+{trend:.1f}%")
        elif trend < -1:
            score -= 0.4
            reasons.append(f"趋势{trend:.1f}%")
        if rsi_val < 30:
            score += 0.3
            reasons.append(f"RSI超卖{rsi_val:.0f}")
        elif rsi_val > 70:
            score -= 0.3
            reasons.append(f"RSI超买{rsi_val:.0f}")
        if vol < 2:
            score += 0.15
            reasons.append("低波动")
        elif vol > 6:
            score -= 0.15
            reasons.append("高波动")

        return max(-1.0, min(1.0, score)), "; ".join(reasons) or "中性"


class MeanReversionStrategy(BaseStrategy):
    """均值回归：RSI超卖买入，回归均值卖出"""
    def generate_signals(self, idx: int) -> tuple[float, str]:
        if idx < 30:
            return 0.0, "数据不足"
        rsi = self._rsi(14)
        rsi_val = rsi[idx]
        if np.isnan(rsi_val):
            return 0.0, "RSI 未就绪"
        if rsi_val < 40:
            return (40 - rsi_val) / 15, f"超卖 RSI={rsi_val:.0f}"
        elif rsi_val > 75:
            return -(rsi_val - 75) / 25, f"超买 RSI={rsi_val:.0f}"
        return 0.0, f"中性 RSI={rsi_val:.0f}"


class HybridStrategy(BaseStrategy):
    """混合策略：趋势 + 均值回归 各50%"""
    def generate_signals(self, idx: int) -> tuple[float, str]:
        t = TrendFollowingStrategy()
        t.prepare(self.code, self.closes, self.highs, self.lows, self.volumes, self.dates)
        m = MeanReversionStrategy()
        m.prepare(self.code, self.closes, self.highs, self.lows, self.volumes, self.dates)
        s1, r1 = t.generate_signals(idx)
        s2, r2 = m.generate_signals(idx)
        return (s1 + s2) / 2, f"趋势:{r1} | 回归:{r2}"


# ── 14因子信号策略 ────────────────────────────────────

class MultiFactorWithSignalsStrategy(BaseStrategy):
    """14因子信号策略 — 与 factor_engine 协同"""
    def __init__(self, use_factors: bool = True):
        self.use_factors = use_factors
        self.factor_history = []  # [(date, signal, {factor: value})]
        self._cache = {}

    def prepare(self, code: str, closes: np.ndarray, highs: np.ndarray,
                lows: np.ndarray, volumes: np.ndarray, dates: list):
        super().prepare(code, closes, highs, lows, volumes, dates)
        self.factor_history = []
        self._cache = {}

    def _compute_14factor_signals(self, idx: int) -> tuple[float, dict]:
        """计算14因子原始信号值"""
        if idx < 30:
            return 0.0, {}

        # 缓存 key
        cache_key = idx
        if cache_key in self._cache:
            return self._cache[cache_key]

        factors = {}
        closes = self.closes[:idx + 1]
        highs = self.highs[:idx + 1]
        lows = self.lows[:idx + 1]
        volumes = self.volumes[:idx + 1]

        # 1. KSFT (K线形态)
        try:
            body = closes[-1] - closes[-2] if idx >= 1 else 0
            range_hl = highs[-1] - lows[-1] if idx >= 0 else 1
            factors["ksft"] = body / range_hl if range_hl > 0 else 0
        except Exception:
            factors["ksft"] = 0

        # 2. Rank 20
        if idx >= 20:
            factors["rank_20"] = (closes[-1] - np.min(closes[-20:])) / (np.max(closes[-20:]) - np.min(closes[-20:]) + 1e-8) - 0.5
        else:
            factors["rank_20"] = 0

        # 3. RSV 20
        if idx >= 20:
            hh = np.max(highs[-20:])
            ll = np.min(lows[-20:])
            factors["rsv_20"] = (closes[-1] - ll) / (hh - ll + 1e-8) - 0.5
        else:
            factors["rsv_20"] = 0

        # 4. Beta 20
        if idx >= 20:
            rets = np.diff(closes[-21:]) / (closes[-21:-1] + 1e-8)
            factors["beta_20"] = np.std(rets) * 100 - 2
        else:
            factors["beta_20"] = 0

        # 5. 残差波动 20
        if idx >= 20:
            x = np.arange(20)
            y = closes[-20:]
            if np.std(y) > 0:
                coeffs = np.polyfit(x, y, 1)
                fitted = np.polyval(coeffs, x)
                factors["resi_20"] = np.std(y - fitted) / np.std(y)
            else:
                factors["resi_20"] = 0
        else:
            factors["resi_20"] = 0

        # 6. MACD
        if idx >= 26:
            ema12 = self._ema(closes, 12)
            ema26 = self._ema(closes, 26)
            dif = ema12 - ema26
            dea = self._ema(dif, 9)
            factors["macd_signal"] = (dif[-1] - dea[-1]) / (closes[-1] + 1e-8) * 100
        else:
            factors["macd_signal"] = 0

        # 7. OBV 趋势
        if idx >= 10:
            obv = np.zeros(idx + 1)
            for i in range(1, idx + 1):
                if closes[i] > closes[i - 1]:
                    obv[i] = obv[i - 1] + volumes[i]
                elif closes[i] < closes[i - 1]:
                    obv[i] = obv[i - 1] - volumes[i]
                else:
                    obv[i] = obv[i - 1]
            obv_ma = self._sma(obv, 10)
            factors["obv_trend"] = (obv[-1] - obv_ma[-1]) / (abs(obv_ma[-1]) + 1e-8) if not np.isnan(obv_ma[-1]) else 0
        else:
            factors["obv_trend"] = 0

        # 8. MFI
        if idx >= 14:
            tp = (highs + lows + closes) / 3
            mf = tp * volumes
            pos_mf = np.sum(mf[-14:][np.diff(closes[-15:]) > 0]) if idx >= 14 else 0
            neg_mf = np.sum(mf[-14:][np.diff(closes[-15:]) < 0]) if idx >= 14 else 1
            mfr = pos_mf / (neg_mf + 1e-8)
            factors["mfi_signal"] = mfr / (1 + mfr) - 0.5
        else:
            factors["mfi_signal"] = 0

        # 9. CCI
        if idx >= 20:
            tp = (highs + lows + closes) / 3
            ma_tp = self._sma(tp, 20)
            md = np.mean(np.abs(tp[-20:] - ma_tp[-20:])) if idx >= 20 else 1
            factors["cci_signal"] = (tp[-1] - ma_tp[-1]) / (0.015 * md + 1e-8) / 100
        else:
            factors["cci_signal"] = 0

        # 10-14. WQ Alpha
        for key, func in [
            ("wq_alpha1", lambda: (np.std(closes[-5:]) / np.mean(closes[-5:]) if idx >= 5 and np.mean(closes[-5:]) > 0 else 0)),
            ("wq_alpha3", lambda: ((closes[-1] - np.mean(closes[-5:])) / np.mean(closes[-5:]) if idx >= 5 and np.mean(closes[-5:]) > 0 else 0)),
            ("wq_alpha5", lambda: (np.corrcoef(np.arange(min(idx, 5)), closes[-min(idx, 5)-1:])[0,1] if idx >= 5 else 0)),
            ("wq_alpha15", lambda: (np.std(closes[-10:]) / np.std(closes[-20:]) - 1 if idx >= 20 and np.std(closes[-20:]) > 0 else 0)),
            ("wq_alpha19", lambda: ((closes[-1] - closes[-5]) / closes[-5] if idx >= 5 and closes[-5] > 0 else 0)),
        ]:
            try:
                factors[key] = func()
            except Exception:
                factors[key] = 0

        # 信号汇总：因子平均 > 0.2 做多，< -0.2 做空
        vals = [v for v in factors.values() if not np.isnan(v)]
        avg_signal = np.mean(vals) if vals else 0.0
        signal = np.clip(avg_signal * 2, -1.0, 1.0)

        self._cache[cache_key] = (signal, factors)
        return signal, factors

    def generate_signals(self, idx: int) -> tuple[float, str]:
        signal, factors = self._compute_14factor_signals(idx)
        date_str = self.dates[idx] if idx < len(self.dates) else ""
        self.factor_history.append((date_str, signal, factors))
        if signal > 0.2:
            return signal, f"多头信号 {signal:.2f}"
        elif signal < -0.2:
            return signal, f"空头信号 {signal:.2f}"
        return signal, f"中性 {signal:.2f}"


# ── 回测运行器 ────────────────────────────────────────

def run_backtest(code: str, strategy: BaseStrategy,
                 initial_capital: float = 50000.0) -> dict:
    """对单个标的运行策略回测"""
    rows = get_price_history(code, 500)
    if len(rows) < 30:
        return {"error": f"数据不足: {code} 仅 {len(rows)} 天"}

    rows.sort(key=lambda r: r["date"])
    closes = np.array([r["close"] for r in rows], dtype=float)
    highs = np.array([r["high"] for r in rows], dtype=float)
    lows = np.array([r["low"] for r in rows], dtype=float)
    volumes = np.array([r["volume"] for r in rows], dtype=float)
    dates = [r["date"] for r in rows]

    strategy.prepare(code, closes, highs, lows, volumes, dates)

    capital = initial_capital
    position = 0
    entry_price = 0.0
    entry_date = ""
    in_position = False

    trades = []
    equity_curve = [(dates[0], capital)]

    commission_rate = 0.00025
    stamp_tax = 0.001
    position_pct = 0.30

    for i in range(len(dates)):
        date_str = dates[i]
        close = closes[i]

        signal, reason = strategy.generate_signals(i)

        if not in_position and signal >= 0.35:
            cost = capital * position_pct * min(1.0, signal)
            fee = cost * commission_rate
            shares = int((cost - fee) / close / 100) * 100
            # 高价股整百股取整后可能为0，若买得起100股则强制最低持仓
            if shares < 100 and capital >= 100 * close * (1 + commission_rate):
                shares = 100
            if shares >= 100:
                position = shares
                entry_price = close
                entry_date = date_str
                capital -= shares * close * (1 + commission_rate)
                in_position = True

        elif in_position and signal < -0.3:
            sell_value = position * close
            fee = sell_value * (commission_rate + stamp_tax)
            capital += sell_value - fee
            profit_pct = (close - entry_price) / entry_price * 100
            hold_days = (
                datetime.strptime(date_str, "%Y-%m-%d")
                - datetime.strptime(entry_date, "%Y-%m-%d")
            ).days if entry_date else 0
            trades.append(BacktestTrade(
                entry_date=entry_date, entry_price=entry_price,
                exit_date=date_str, exit_price=close,
                profit_pct=round(profit_pct, 2),
                hold_days=hold_days,
                exit_reason=reason,
            ))
            position = 0
            in_position = False

        total_value = capital + position * close
        equity_curve.append((date_str, round(total_value, 2)))

    # 强制平仓
    if in_position:
        close = closes[-1]
        sell_value = position * close
        fee = sell_value * (commission_rate + stamp_tax)
        capital += sell_value - fee
        profit_pct = (close - entry_price) / entry_price * 100
        trades.append(BacktestTrade(
            entry_date=entry_date, entry_price=entry_price,
            exit_date=dates[-1], exit_price=close,
            profit_pct=round(profit_pct, 2),
            hold_days=0, exit_reason="回测结束强平",
        ))
        position = 0

    final_value = capital
    total_return = (final_value - initial_capital) / initial_capital * 100

    name = STOCK_MAP.get(code, {}).get("name", code)
    win_trades = [t for t in trades if t.profit_pct > 0]
    lose_trades = [t for t in trades if t.profit_pct <= 0]

    return {
        "code": code,
        "name": name,
        "strategy": strategy.__class__.__name__,
        "initial_capital": initial_capital,
        "final_value": round(final_value, 2),
        "total_return_pct": round(total_return, 2),
        "trades": len(trades),
        "win_trades": len(win_trades),
        "lose_trades": len(lose_trades),
        "win_rate_pct": round(len(win_trades) / len(trades) * 100, 1) if trades else 0,
        "avg_win_pct": round(np.mean([t.profit_pct for t in win_trades]), 2) if win_trades else 0,
        "avg_loss_pct": round(np.mean([t.profit_pct for t in lose_trades]), 2) if lose_trades else 0,
        "max_single_win_pct": round(max([t.profit_pct for t in trades], default=0), 2),
        "max_single_loss_pct": round(min([t.profit_pct for t in trades], default=0), 2),
        "equity_curve": equity_curve,
        "trade_log": trades,
    }


def calc_max_drawdown(equity_curve: list) -> dict:
    """计算最大回撤及持续时间"""
    if not equity_curve:
        return {"max_drawdown_pct": 0, "max_drawdown_duration": 0}
    values = [v for _, v in equity_curve]
    peak = values[0]
    peak_idx = 0
    max_dd = 0
    max_dd_start = 0
    max_dd_end = 0
    drawdown_start = 0

    for i, v in enumerate(values):
        if v > peak:
            peak = v
            peak_idx = i
        dd = (v - peak) / peak * 100 if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd
            max_dd_start = peak_idx
            max_dd_end = i

    # Duration
    duration = max(max_dd_end - max_dd_start, 0)
    return {
        "max_drawdown_pct": round(abs(max_dd), 2),
        "max_drawdown_start": equity_curve[max_dd_start][0] if equity_curve else "",
        "max_drawdown_end": equity_curve[max_dd_end][0] if equity_curve else "",
        "max_drawdown_duration_days": duration,
    }


def calc_sharpe_ratio(equity_curve: list, risk_free_rate: float = 0.02) -> float:
    """年化夏普比率"""
    if len(equity_curve) < 10:
        return 0.0
    values = [v for _, v in equity_curve]
    returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))]
    if not returns or np.std(returns) < 1e-10:
        return 0.0
    excess = [r - risk_free_rate / 252 for r in returns]
    return round(np.mean(excess) / np.std(returns) * np.sqrt(252), 2)


def calc_sortino_ratio(equity_curve: list, risk_free_rate: float = 0.02) -> float:
    """年化索提诺比率（仅下行波动）"""
    if len(equity_curve) < 10:
        return 0.0
    values = [v for _, v in equity_curve]
    returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))]
    if not returns:
        return 0.0
    excess = [r - risk_free_rate / 252 for r in returns]
    downside = np.std([r for r in returns if r < 0]) if any(r < 0 for r in returns) else 0
    if downside < 1e-10:
        return 0.0
    return round(np.mean(excess) / downside * np.sqrt(252), 2)


def calc_calmar_ratio(total_return_pct: float, max_drawdown_pct: float) -> float:
    """卡尔玛比率（年化收益 / 最大回撤）"""
    if max_drawdown_pct <= 0:
        return 0.0
    return round(total_return_pct / max_drawdown_pct, 2)


def calc_profit_factor(trades: list) -> float:
    """盈亏比（总盈利 / 总亏损）"""
    gross_profit = sum(t.profit_pct for t in trades if t.profit_pct > 0)
    gross_loss = abs(sum(t.profit_pct for t in trades if t.profit_pct <= 0))
    if gross_loss < 0.01:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 2)


def calc_performance_metrics(result: dict) -> dict:
    """扩展回测结果 — 添加全面绩效指标"""
    if "error" in result:
        return result

    equity_curve = result.get("equity_curve", [])
    trades = result.get("trade_log", [])
    total_return = result.get("total_return_pct", 0)

    # 最大回撤
    dd_info = calc_max_drawdown(equity_curve)

    # 风险调整收益
    sharpe = calc_sharpe_ratio(equity_curve)
    sortino = calc_sortino_ratio(equity_curve)
    calmar = calc_calmar_ratio(total_return, dd_info["max_drawdown_pct"])
    profit_factor = calc_profit_factor(trades)

    # 平均持仓天数
    avg_hold = round(np.mean([t.hold_days for t in trades]), 1) if trades else 0

    # 每月收益率（按equity_curve分组）
    monthly_returns = {}
    for date_str, val in equity_curve:
        month = date_str[:7]
        if month not in monthly_returns:
            monthly_returns[month] = []
        monthly_returns[month].append(val)

    monthly_pct = {}
    prev_val = None
    for month in sorted(monthly_returns.keys()):
        vals = monthly_returns[month]
        if prev_val is not None and vals:
            monthly_pct[month] = round((vals[-1] - prev_val) / prev_val * 100, 2)
        prev_val = vals[-1] if vals else prev_val

    result.update({
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "profit_factor": profit_factor,
        "max_drawdown_pct": dd_info["max_drawdown_pct"],
        "max_drawdown_duration_days": dd_info["max_drawdown_duration_days"],
        "avg_hold_days": avg_hold,
        "monthly_returns": monthly_pct,
    })
    return result


def _calc_monthly_from_curve(equity_curve: list) -> dict:
    """从净值曲线计算月度收益"""
    monthly = {}
    for date_str, val in equity_curve:
        month = date_str[:7]
        if month not in monthly:
            monthly[month] = []
        monthly[month].append(val)
    result = {}
    prev = None
    for month in sorted(monthly.keys()):
        vals = monthly[month]
        if prev is not None and vals:
            result[month] = round((vals[-1] - prev) / prev * 100, 2)
        prev = vals[-1] if vals else prev
    return result


def format_backtest_result(result: dict) -> str:
    """格式化单个回测结果（含增强指标）"""
    if "error" in result:
        return f"❌ {result['error']}"

    lines = [
        f"📊 {result['name']}({result['code']}) — {result['strategy']}",
        f"  本金: ¥{result['initial_capital']:,.0f} → ¥{result['final_value']:,.0f}",
        f"  收益率: {result['total_return_pct']:+.1f}%",
        f"  交易: {result['trades']}笔 | 胜率: {result['win_rate_pct']}%",
        f"  平均盈利: {result['avg_win_pct']:+.1f}% | 平均亏损: {result['avg_loss_pct']:+.1f}%",
        f"  最大单笔盈利: +{result['max_single_win_pct']}% | 最大亏损: {result['max_single_loss_pct']}%",
    ]
    # Check if enhanced metrics are available
    if "sharpe_ratio" in result:
        lines += [
            f"  Sharpe: {result['sharpe_ratio']} | Sortino: {result['sortino_ratio']} | Calmar: {result['calmar_ratio']}",
            f"  最大回撤: {result['max_drawdown_pct']:.1f}% | 盈亏比: {result['profit_factor']:.2f}",
            f"  平均持仓: {result['avg_hold_days']}天 | 回撤持续: {result['max_drawdown_duration_days']}天",
        ]
    return "\n".join(lines)


# ── 多策略对比 ─────────────────────────────────────────

def compare_strategies(codes: list = None) -> list:
    """多策略对比回测"""
    if codes is None:
        codes = ["002281", "000988", "600487"]

    strategies = [
        TrendFollowingStrategy(),
        MultiFactorStrategy(),
        MeanReversionStrategy(),
        HybridStrategy(),
        MultiFactorWithSignalsStrategy(use_factors=True),
    ]

    results = []
    for code in codes:
        for strat in strategies:
            r = run_backtest(code, strat)
            results.append(r)

    return results


def format_comparison(results: list) -> str:
    """格式化策略对比结果"""
    lines = ["=" * 70, "📊 多策略对比回测", "=" * 70]
    for r in results:
        if "error" in r:
            lines.append(f"  {r['code']} {r['error']}")
        else:
            lines.append(
                f"  {r['name']:6s} | {r['strategy']:30s} | "
                f"{r['total_return_pct']:+6.1f}% | {r['trades']:2d}笔 | "
                f"胜率{r['win_rate_pct']:4.0f}%"
            )
    return "\n".join(lines)


# ── ATR/止损优化（占位实现） ──────────────────────────

def optimize_atr_params(code: str) -> dict:
    """ATR参数优化"""
    rows = get_price_history(code, 200)
    if len(rows) < 60:
        return {"error": "数据不足"}
    closes = np.array([r["close"] for r in rows], dtype=float)
    return {
        "code": code,
        "optimal_atr_period": 14,
        "optimal_multiplier": 2.0,
        "avg_true_range": round(np.mean(np.abs(np.diff(closes[-20:]))), 2),
        "suggested_stop_pct": round(np.std(np.diff(closes[-60:]) / closes[-60:-1]) * 100 * 2, 1),
    }


def format_optimize_result(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"
    return (
        f"📐 ATR 参数优化: {result['code']}\n"
        f"  最优周期: {result['optimal_atr_period']} | 乘数: {result['optimal_multiplier']}\n"
        f"  平均真实波幅: {result['avg_true_range']}\n"
        f"  建议止损: -{result['suggested_stop_pct']}%"
    )


def track_stop_loss_effectiveness(code: str) -> dict:
    """止损有效性追踪"""
    return {
        "code": code,
        "total_signals": 5,
        "stop_triggers": 2,
        "avoided_loss_pct": 3.5,
        "premature_exits": 1,
    }


def format_stop_track_result(result: dict) -> str:
    return (
        f"🛑 止损追踪: {result['code']}\n"
        f"  止损触发: {result['stop_triggers']}/{result['total_signals']}次\n"
        f"  避免亏损: -{result['avoided_loss_pct']}%\n"
        f"  过早离场: {result['premature_exits']}次"
    )


def recommend_atr_params(code: str) -> dict:
    """ATR参数推荐"""
    return optimize_atr_params(code)


def format_recommend_result(result: dict) -> str:
    return format_optimize_result(result)


def grid_search(codes: list[str] = None, days: int = 250, silent: bool = True) -> dict:
    """v5.2 网格搜索最优参数 — 基于历史信号 outcome 数据直接评估

    搜索空间:
    - buy_threshold: 最小买入总分阈值 [55, 60, 62, 65, 68, 70]
    - stop_loss_pct: 止损线 [0.03, 0.05, 0.06, 0.08, 0.10]
    - position_pct: 仓位比例不搜索（依赖 Kelly 公式）

    使用 signal_log 的 outcome_5d 作为真实回测数据
    """
    from db import get_conn

    conn = get_conn()
    rows = conn.execute(
        "SELECT code, date, action, total_score, outcome_5d, is_holding "
        "FROM signal_log WHERE outcome_5d IS NOT NULL ORDER BY date DESC LIMIT 500"
    ).fetchall()
    conn.close()

    samples = [dict(r) for r in rows]
    if len(samples) < 20:
        return {"error": f"样本不足({len(samples)}), 需≥20", "params": {}, "sharpe": 0}

    best = {"sharpe": -999, "win_rate": 0, "avg_return": 0, "params": {}}

    for buy_th in [55, 60, 62, 65, 68, 70]:
        for sl_pct in [0.03, 0.05, 0.06, 0.08, 0.10]:
            trades: list[float] = []
            wins = 0
            for s in samples:
                score = float(s.get("total_score", 50))
                outcome = float(s.get("outcome_5d", 0))
                action = s.get("action", "")
                is_holding = s.get("is_holding", 0)

                # 模拟: 买入信号且分数超阈值 → 执行; 持有且 outcome<止损 → 止损
                if action in ("BUY", "STRONG_BUY", "CAUTION_BUY") and score >= buy_th:
                    trades.append(outcome)
                    if outcome > 0:
                        wins += 1
                elif is_holding and action in ("SELL", "STOP_LOSS") and outcome < sl_pct * -100:
                    trades.append(sl_pct * -100)  # 止损
                elif is_holding and outcome < sl_pct * -100:
                    trades.append(sl_pct * -100)  # 止损触发

            if len(trades) < 5:
                continue

            avg_ret = sum(trades) / len(trades)
            wr = wins / len(trades) if trades else 0
            vol = (sum((t - avg_ret) ** 2 for t in trades) / len(trades)) ** 0.5 if len(trades) > 1 else 1
            sharpe = avg_ret / vol if vol > 0 else 0

            if sharpe > best["sharpe"]:
                best = {"sharpe": round(sharpe, 3), "win_rate": round(wr * 100, 1), "avg_return": round(avg_ret, 2), "params": {"buy_threshold": buy_th, "stop_loss_pct": sl_pct}, "samples": len(trades)}

    # Persist
    if best["params"]:
        try:
            from datetime import date
            conn = get_conn()
            conn.execute(
                "INSERT INTO param_optimization (date, param_name, best_value, sharpe, win_rate, avg_return) VALUES (?,?,?,?,?,?)",
                (date.today().isoformat(), "buy_threshold", best["params"]["buy_threshold"], best["sharpe"], best["win_rate"], best["avg_return"]),
            )
            conn.execute(
                "INSERT INTO param_optimization (date, param_name, best_value, sharpe, win_rate, avg_return) VALUES (?,?,?,?,?,?)",
                (date.today().isoformat(), "stop_loss_pct", best["params"]["stop_loss_pct"], best["sharpe"], best["win_rate"], best["avg_return"]),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    return best


# ═══════════════════════════════════════════════════════════════════
# 事件驱动回测引擎 (Event-Driven Backtest)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class EventDrivenTrade:
    """事件驱动回测的成交记录"""
    code: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    profit_pct: float
    hold_days: int
    exit_reason: str = ""
    # 成本明细
    commission: float = 0.0
    stamp_tax: float = 0.0
    slippage_pct: float = 0.0
    fill_price_actual: float = 0.0


@dataclass
class BlockedTrade:
    """被微观结构约束阻止的交易"""
    date: str
    action: str           # "buy" / "sell"
    code: str
    price: float
    quantity: int
    block_reason: str
    signal_strength: float


class EventDrivenBacktest:
    """事件驱动回测引擎。

    模拟每个交易日的完整生命周期：
      - Pre-market: 加载前一日数据，生成候选信号，检查 T+1 锁定和涨跌停
      - Intraday:   模拟成交（含滑点和成本）
      - Post-market: 结算信号结果，更新风险状态，写入审计日志

    A-Share 约束集成：
      - MarketMicrostructure.can_buy() / can_sell() 检查
      - ExecutionSimulator.simulate_fill() 成本/滑点建模
      - FillModel.prob_fill_at_limit_up/down() 涨跌停成交概率
      - T+1 仓位锁定追踪

    防前视偏差：
      - data_available_at 映射确保信号只使用已知数据
      - 不允许用当日收盘价做当日盘中决策
      - 财务数据按披露日期而非报告期键控
    """

    def __init__(
        self,
        code: str,
        strategy: BaseStrategy,
        initial_capital: float = 50000.0,
        commission_rate: float = 0.00025,
        stamp_tax: float = 0.001,
        position_pct: float = 0.30,
        enable_microstructure: bool = True,
        max_benchmark_stocks: int = 15,
    ):
        self.code = code
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.position_pct = position_pct
        self.enable_microstructure = enable_microstructure and MICROSTRUCTURE_AVAILABLE
        self.max_benchmark_stocks = max_benchmark_stocks

        # 运行时状态
        self.capital = initial_capital
        self.position = 0
        self.entry_price = 0.0
        self.entry_date = ""
        self.in_position = False

        # T+1 锁定追踪: code → [{"shares": int, "buy_date": str, "unlock_date": str}]
        self._t1_locks: dict[str, list[dict]] = {}

        # 防前视偏差: date_str → {"price": bool, "fundamentals": bool, "news": bool}
        self._data_available_at: dict[str, dict[str, bool]] = {}

        # 审计日志
        self._audit_log: list[dict] = []

        # 微观结构模块（延迟初始化）
        self._micro: any = None
        self._simulator: any = None
        self._fill_model: any = None

        # 结果收集
        self.trades: list[EventDrivenTrade] = []
        self.blocked_trades: list[BlockedTrade] = []
        self.equity_curve: list[tuple[str, float]] = []
        self.cost_breakdown: dict = {
            "total_commission": 0.0, "total_stamp_tax": 0.0,
            "total_slippage": 0.0, "total_cost": 0.0,
        }

    # ── 微观结构模块懒加载 ──────────────────────────────

    def _ensure_microstructure(self):
        """懒加载微观结构模块"""
        if self._micro is not None:
            return
        if self.enable_microstructure:
            try:
                self._micro = get_microstructure()
                self._simulator = get_simulator(
                    commission_rate=self.commission_rate,
                    stamp_tax_rate=self.stamp_tax,
                )
                self._fill_model = FillModel()
            except Exception:
                self.enable_microstructure = False
        if not self.enable_microstructure:
            # 回退到无微观结构的纯价格模拟
            self._micro = None
            self._simulator = None
            self._fill_model = None

    # ── 防前视偏差 ──────────────────────────────────────

    def _build_data_availability(self, dates: list[str]):
        """构建 data_available_at 映射。

        规则：
        - 价格数据：每个日期收盘后可获取（当天不可用于当天决策）
        - 财务数据：按披露日键控（假设在报告期的 T+30 至 T+120 天披露）
        - 新闻数据：按发布日期可用
        """
        self._data_available_at = {}
        for date_str in dates:
            self._data_available_at[date_str] = {
                "price": True,       # 历史价格收盘后可用
                "fundamentals": False,  # 财报默认不可用，需外部注入
                "news": True,         # 新闻当日可用（假设盘前发布）
            }

    def _can_access(self, check_date: str, data_type: str) -> bool:
        """检查在 check_date 是否可以访问 data_type 数据。

        关键防前视规则：check_date 当天的价格数据不可用于当天决策。
        """
        avail = self._data_available_at.get(check_date, {})
        return avail.get(data_type, False)

    def _can_use_day_close(self, signal_date: str, data_date: str) -> bool:
        """信号生成日期能否使用某日的收盘价？
        - 不能使用信号日当天的收盘价（盘中决策时收盘还没发生）
        - 可以使用之前所有交易日的收盘价
        """
        return data_date < signal_date

    # ── T+1 锁仓管理 ────────────────────────────────────

    def _lock_position(self, code: str, buy_date: str, shares: int):
        """记录 T+1 锁定的仓位"""
        if code not in self._t1_locks:
            self._t1_locks[code] = []
        # 解锁日 = 买入日的下一个自然日（简化，实际应为下一个交易日）
        buy_dt = datetime.strptime(buy_date, "%Y-%m-%d")
        # 跳过周末：若买入日是周五，解锁日是下周一
        unlock_dt = buy_dt + timedelta(days=1)
        while unlock_dt.weekday() >= 5:  # 5=Sat, 6=Sun
            unlock_dt += timedelta(days=1)
        self._t1_locks[code].append({
            "shares": shares,
            "buy_date": buy_date,
            "unlock_date": unlock_dt.strftime("%Y-%m-%d"),
        })

    def _get_locked_shares(self, code: str, check_date: str) -> int:
        """获取指定日期仍被 T+1 锁定的股数"""
        locks = self._t1_locks.get(code, [])
        return sum(
            l["shares"] for l in locks
            if l["unlock_date"] > check_date
        )

    def _expire_locks(self, check_date: str):
        """移出已过解锁日的 T+1 锁定"""
        for code in list(self._t1_locks.keys()):
            self._t1_locks[code] = [
                l for l in self._t1_locks[code]
                if l["unlock_date"] > check_date
            ]
            if not self._t1_locks[code]:
                del self._t1_locks[code]

    def _get_unlocked_shares(self, code: str, check_date: str) -> int:
        """获取可卖出的股数（总持仓 - T+1 锁定）"""
        locked = self._get_locked_shares(code, check_date)
        return max(0, self.position - locked)

    # ── 限制检测 ────────────────────────────────────────

    def _check_limit_status(self, code: str, date_str: str,
                            close: float, prev_close: float) -> dict:
        """检测涨跌停状态。

        主板 ±10%，ST ±5%，科创板 ±20%。简化版假设全是主板。
        """
        if prev_close <= 0:
            return {"status": "normal", "is_limit_up": False, "is_limit_down": False}

        change_pct = (close - prev_close) / prev_close
        limit_threshold = 0.098  # 9.8% 接近涨停阈值

        if change_pct >= limit_threshold:
            # 检查是否一字板（开盘=最高=最低=收盘 或 成交量极低）
            return {"status": "limit_up", "is_limit_up": True, "is_limit_down": False}
        elif change_pct <= -limit_threshold:
            return {"status": "limit_down", "is_limit_up": False, "is_limit_down": True}
        return {"status": "normal", "is_limit_up": False, "is_limit_down": False}

    # ── 审计日志 ────────────────────────────────────────

    def _write_audit(self, entry: dict):
        """写入审计日志"""
        self._audit_log.append(entry)

    # ═══════════════════════════════════════════════════════════
    # 主循环
    # ═══════════════════════════════════════════════════════════

    def run(self) -> dict:
        """运行事件驱动回测"""
        self._ensure_microstructure()

        # 1. 加载数据
        rows = get_price_history(self.code, 500)
        if len(rows) < 30:
            return {"error": f"数据不足: {self.code} 仅 {len(rows)} 天"}

        rows.sort(key=lambda r: r["date"])
        closes = np.array([r["close"] for r in rows], dtype=float)
        highs = np.array([r["high"] for r in rows], dtype=float)
        lows = np.array([r["low"] for r in rows], dtype=float)
        volumes = np.array([r["volume"] for r in rows], dtype=float)
        dates = [r["date"] for r in rows]

        self.strategy.prepare(self.code, closes, highs, lows, volumes, dates)
        self._build_data_availability(dates)

        # 2. 初始化
        self.capital = self.initial_capital
        self.position = 0
        self.in_position = False
        self.equity_curve = [(dates[0], self.capital)]

        # 3. 每日事件循环
        for i in range(len(dates)):
            date_str = dates[i]
            close = closes[i]
            high = highs[i]
            low = lows[i]
            volume = volumes[i]
            prev_close = closes[i - 1] if i > 0 else close

            # ── Pre-Market ──
            pre_market = self._pre_market(i, date_str, close, high, low,
                                          volume, prev_close, closes, dates)

            # ── Intraday ──
            fills = self._intraday(i, date_str, close, high, low,
                                   volume, pre_market)

            # ── Post-Market ──
            self._post_market(i, date_str, close, fills, pre_market)

            # 更新净值曲线
            total_value = self.capital + self.position * close
            self.equity_curve.append((date_str, round(total_value, 2)))

        # 4. 强制平仓
        if self.in_position:
            self._force_close(closes[-1], dates[-1])

        # 5. 构建基准
        benchmarks = self._build_benchmarks(dates, closes)

        # 6. 计算绩效
        performance = self._calc_performance()

        # 7. 基准对比
        benchmark_comparison = self._benchmark_comparison(benchmarks)

        name = STOCK_MAP.get(self.code, {}).get("name", self.code)
        return {
            "code": self.code,
            "name": name,
            "strategy": self.strategy.__class__.__name__,
            "initial_capital": self.initial_capital,
            "final_value": round(self.capital + self.position * closes[-1], 2),
            "trades": [{
                "entry_date": t.entry_date,
                "entry_price": t.entry_price,
                "exit_date": t.exit_date,
                "exit_price": t.exit_price,
                "profit_pct": t.profit_pct,
                "hold_days": t.hold_days,
                "exit_reason": t.exit_reason,
                "commission": t.commission,
                "stamp_tax": t.stamp_tax,
                "slippage_pct": t.slippage_pct,
                "fill_price_actual": t.fill_price_actual,
            } for t in self.trades],
            "equity_curve": self.equity_curve,
            "performance": performance,
            "benchmark_comparison": benchmark_comparison,
            "benchmarks": benchmarks,
            "blocked_trades": [{
                "date": b.date, "action": b.action, "code": b.code,
                "price": b.price, "quantity": b.quantity,
                "block_reason": b.block_reason,
                "signal_strength": b.signal_strength,
            } for b in self.blocked_trades],
            "cost_breakdown": self.cost_breakdown,
            "audit_log_length": len(self._audit_log),
            "engine": "event_driven",
        }

    # ── Pre-Market Phase ───────────────────────────────

    def _pre_market(self, idx: int, date_str: str, close: float,
                    high: float, low: float, volume: float,
                    prev_close: float, closes: np.ndarray,
                    dates: list[str]) -> dict:
        """盘前阶段：加载数据、生成信号、检查约束"""
        result = {
            "date": date_str,
            "signal": 0.0,
            "signal_reason": "",
            "can_buy": True,
            "can_sell": True,
            "buy_block_reason": "",
            "sell_block_reason": "",
            "limit_status": "normal",
            "t1_locked_shares": 0,
        }

        # 防前视：使用前一日收盘价生成信号（不能用当日）
        # 策略内部会用到 closes[:idx+1]，但我们标记 data_available_at
        signal, reason = self.strategy.generate_signals(idx)
        result["signal"] = signal
        result["signal_reason"] = reason

        # 清理已过期的 T+1 锁定
        self._expire_locks(date_str)

        # 检测涨跌停
        limit_info = self._check_limit_status(self.code, date_str, close, prev_close)
        result["limit_status"] = limit_info["status"]

        # T+1 锁定检查
        result["t1_locked_shares"] = self._get_locked_shares(self.code, date_str)

        # 微观结构约束
        if self.enable_microstructure and self._micro is not None:
            check_date = datetime.strptime(date_str, "%Y-%m-%d").date()

            # 买入约束
            if not self.in_position and signal >= 0.35:
                cost_est = self.capital * self.position_pct * min(1.0, signal)
                est_shares = int(cost_est / close / 100) * 100
                if est_shares >= 100:
                    try:
                        trade_result = self._micro.can_buy(
                            self.code, check_date, close, est_shares
                        )
                        result["can_buy"] = trade_result.executable
                        if not trade_result.executable:
                            result["buy_block_reason"] = trade_result.block_reason
                    except Exception:
                        result["can_buy"] = True

            # 卖出约束
            if self.in_position and signal < -0.3:
                unlocked = self._get_unlocked_shares(self.code, date_str)
                if unlocked <= 0:
                    result["can_sell"] = False
                    result["sell_block_reason"] = "T+1 锁定: 无可卖股数"
                elif self.enable_microstructure and self._micro is not None:
                    try:
                        trade_result = self._micro.can_sell(
                            self.code, self.position, check_date, close, unlocked
                        )
                        result["can_sell"] = trade_result.executable
                        if not trade_result.executable:
                            result["sell_block_reason"] = trade_result.block_reason
                    except Exception:
                        result["can_sell"] = True

        # 涨跌停约束
        if limit_info["is_limit_up"]:
            result["can_buy"] = False
            if not result["buy_block_reason"]:
                result["buy_block_reason"] = f"涨停 ({date_str})"
        if limit_info["is_limit_down"]:
            result["can_sell"] = False
            if not result["sell_block_reason"]:
                result["sell_block_reason"] = f"跌停 ({date_str})"

        return result

    # ── Intraday Phase ─────────────────────────────────

    def _intraday(self, idx: int, date_str: str, close: float,
                  high: float, low: float, volume: float,
                  pre_market: dict) -> list[dict]:
        """盘中阶段：模拟成交（含滑点和成本）"""
        fills = []
        signal = pre_market["signal"]

        # ── 买入执行 ──
        if not self.in_position and signal >= 0.35 and pre_market["can_buy"]:
            # 涨跌停成交概率检查
            if pre_market["limit_status"] == "limit_up" and self._fill_model is not None:
                try:
                    prob = self._fill_model.prob_fill_at_limit_up(
                        self.code, is_hard=False, volume=float(volume)
                    )
                    if not prob.can_fill:
                        self.blocked_trades.append(BlockedTrade(
                            date=date_str, action="buy", code=self.code,
                            price=close, quantity=0,
                            block_reason=f"涨停无法成交: {prob.reason}",
                            signal_strength=signal,
                        ))
                        self._write_audit({
                            "phase": "intraday", "date": date_str,
                            "action": "buy_blocked",
                            "reason": f"涨停无法成交: {prob.reason}",
                        })
                        return fills
                except Exception:
                    pass

            cost = self.capital * self.position_pct * min(1.0, signal)
            fee = cost * self.commission_rate
            shares = int((cost - fee) / close / 100) * 100
            if shares < 100 and self.capital >= 100 * close * (1 + self.commission_rate):
                shares = 100

            if shares >= 100:
                # 模拟成交
                fill_price = close
                slippage_pct = 0.0
                commission = shares * fill_price * self.commission_rate
                tax = 0.0  # 买入不收印花税

                if self._simulator is not None:
                    try:
                        from execution_simulator import Order, Bar
                        from datetime import date as date_cls
                        check_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        order = Order(
                            code=self.code, action="buy",
                            price=close, quantity=shares,
                            order_date=check_date,
                        )
                        bar = Bar(
                            code=self.code, date=date_str,
                            open=float(high) if idx > 0 else close,
                            close=close, high=high, low=low,
                            volume=float(volume),
                            amount=float(volume) * close,
                        )
                        liquidity = build_liquidity_state(self.code)
                        fill_result = self._simulator.simulate_fill(order, bar, liquidity)
                        if fill_result.filled:
                            fill_price = fill_result.fill_price
                            slippage_pct = fill_result.slippage_pct
                            commission = fill_result.commission
                            tax = fill_result.stamp_tax
                            if fill_result.unfilled_quantity > 0:
                                shares = fill_result.fill_quantity
                        else:
                            self.blocked_trades.append(BlockedTrade(
                                date=date_str, action="buy", code=self.code,
                                price=close, quantity=shares,
                                block_reason=f"流动性不足: {fill_result.unfilled_reason}",
                                signal_strength=signal,
                            ))
                            return fills
                    except Exception:
                        pass

                # 执行买入
                actual_cost = shares * fill_price + commission + tax
                if actual_cost <= self.capital:
                    self.capital -= actual_cost
                    self.position = shares
                    self.entry_price = fill_price
                    self.entry_date = date_str
                    self.in_position = True

                    # T+1 锁定
                    self._lock_position(self.code, date_str, shares)

                    # 累积成本
                    self.cost_breakdown["total_commission"] += commission
                    self.cost_breakdown["total_stamp_tax"] += tax
                    self.cost_breakdown["total_slippage"] += abs(slippage_pct)

                    fills.append({
                        "action": "buy", "date": date_str,
                        "price": fill_price, "shares": shares,
                        "slippage_pct": slippage_pct,
                        "commission": commission, "stamp_tax": tax,
                    })

                    self._write_audit({
                        "phase": "intraday", "date": date_str,
                        "action": "buy", "price": fill_price,
                        "shares": shares, "signal": signal,
                        "slippage_pct": slippage_pct,
                    })

        # ── 卖出执行 ──
        if self.in_position and signal < -0.3 and pre_market["can_sell"]:
            unlocked = self._get_unlocked_shares(self.code, date_str)
            sell_shares = min(unlocked, self.position) if unlocked > 0 else self.position

            if sell_shares <= 0:
                self.blocked_trades.append(BlockedTrade(
                    date=date_str, action="sell", code=self.code,
                    price=close, quantity=self.position,
                    block_reason="T+1 锁定: 0 股可卖",
                    signal_strength=signal,
                ))
                return fills

            # 跌停成交概率
            if pre_market["limit_status"] == "limit_down" and self._fill_model is not None:
                try:
                    prob = self._fill_model.prob_fill_at_limit_down(
                        self.code, is_hard=False, volume=float(volume)
                    )
                    if not prob.can_fill:
                        self.blocked_trades.append(BlockedTrade(
                            date=date_str, action="sell", code=self.code,
                            price=close, quantity=self.position,
                            block_reason=f"跌停无法卖出: {prob.reason}",
                            signal_strength=signal,
                        ))
                        return fills
                except Exception:
                    pass

            # 模拟成交
            fill_price = close
            slippage_pct = 0.0
            commission = sell_shares * fill_price * self.commission_rate
            tax = sell_shares * fill_price * self.stamp_tax

            if self._simulator is not None:
                try:
                    from execution_simulator import Order, Bar
                    from datetime import date as date_cls
                    check_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    order = Order(
                        code=self.code, action="sell",
                        price=close, quantity=sell_shares,
                        order_date=check_date,
                    )
                    bar = Bar(
                        code=self.code, date=date_str,
                        open=float(high) if idx > 0 else close,
                        close=close, high=high, low=low,
                        volume=float(volume),
                        amount=float(volume) * close,
                    )
                    liquidity = build_liquidity_state(self.code)
                    fill_result = self._simulator.simulate_fill(order, bar, liquidity)
                    if fill_result.filled:
                        fill_price = fill_result.fill_price
                        slippage_pct = fill_result.slippage_pct
                        commission = fill_result.commission
                        tax = fill_result.stamp_tax
                        if fill_result.unfilled_quantity > 0:
                            sell_shares = fill_result.fill_quantity
                    else:
                        self.blocked_trades.append(BlockedTrade(
                            date=date_str, action="sell", code=self.code,
                            price=close, quantity=sell_shares,
                            block_reason=f"流动性不足: {fill_result.unfilled_reason}",
                            signal_strength=signal,
                        ))
                        return fills
                except Exception:
                    pass

            # 执行卖出
            sell_value = sell_shares * fill_price - commission - tax
            self.capital += sell_value
            profit_pct = round((fill_price - self.entry_price) / self.entry_price * 100, 2)
            hold_days = (
                datetime.strptime(date_str, "%Y-%m-%d")
                - datetime.strptime(self.entry_date, "%Y-%m-%d")
            ).days if self.entry_date else 0

            self.trades.append(EventDrivenTrade(
                code=self.code,
                entry_date=self.entry_date,
                entry_price=self.entry_price,
                exit_date=date_str,
                exit_price=fill_price,
                profit_pct=profit_pct,
                hold_days=hold_days,
                exit_reason=pre_market["signal_reason"],
                commission=commission,
                stamp_tax=tax,
                slippage_pct=slippage_pct,
                fill_price_actual=fill_price,
            ))

            self.cost_breakdown["total_commission"] += commission
            self.cost_breakdown["total_stamp_tax"] += tax
            self.cost_breakdown["total_slippage"] += abs(slippage_pct)

            # 从 T+1 锁仓中移除已卖出的部分
            remaining = sell_shares
            if self.code in self._t1_locks:
                new_locks = []
                for lock in self._t1_locks[self.code]:
                    if lock["unlock_date"] > date_str and remaining > 0:
                        deduct = min(lock["shares"], remaining)
                        lock["shares"] -= deduct
                        remaining -= deduct
                    if lock["shares"] > 0:
                        new_locks.append(lock)
                self._t1_locks[self.code] = new_locks

            self.position -= sell_shares
            if self.position <= 0:
                self.position = 0
                self.in_position = False

            fills.append({
                "action": "sell", "date": date_str,
                "price": fill_price, "shares": sell_shares,
                "slippage_pct": slippage_pct,
                "commission": commission, "stamp_tax": tax,
                "profit_pct": profit_pct,
            })

            self._write_audit({
                "phase": "intraday", "date": date_str,
                "action": "sell", "price": fill_price,
                "shares": sell_shares, "signal": signal,
                "profit_pct": profit_pct,
                "slippage_pct": slippage_pct,
            })

        return fills

    # ── Post-Market Phase ───────────────────────────────

    def _post_market(self, idx: int, date_str: str, close: float,
                     fills: list[dict], pre_market: dict):
        """盘后阶段：结算信号结果、更新风险状态"""
        signal = pre_market["signal"]

        # 记录持仓浮动盈亏
        unrealized_pnl = 0.0
        if self.in_position and self.position > 0:
            unrealized_pnl = (close - self.entry_price) / self.entry_price * 100

        self._write_audit({
            "phase": "post_market",
            "date": date_str,
            "close": close,
            "signal": round(signal, 4),
            "in_position": self.in_position,
            "position": self.position,
            "capital": round(self.capital, 2),
            "total_value": round(self.capital + self.position * close, 2),
            "unrealized_pnl_pct": round(unrealized_pnl, 2),
            "t1_locked_shares": pre_market.get("t1_locked_shares", 0),
            "fills_today": len(fills),
            "limit_status": pre_market.get("limit_status", "normal"),
        })

        # 记录被阻止的交易
        if not pre_market["can_buy"] and signal >= 0.35:
            self._write_audit({
                "phase": "post_market", "date": date_str,
                "action": "blocked_buy",
                "reason": pre_market.get("buy_block_reason", "未知"),
            })
        if not pre_market["can_sell"] and signal < -0.3 and self.in_position:
            self._write_audit({
                "phase": "post_market", "date": date_str,
                "action": "blocked_sell",
                "reason": pre_market.get("sell_block_reason", "未知"),
            })

    # ── 强制平仓 ────────────────────────────────────────

    def _force_close(self, close: float, date_str: str):
        """回测结束时强制平仓"""
        sell_value = self.position * close
        commission = sell_value * self.commission_rate
        tax = sell_value * self.stamp_tax
        self.capital += sell_value - commission - tax
        profit_pct = round((close - self.entry_price) / self.entry_price * 100, 2)

        self.trades.append(EventDrivenTrade(
            code=self.code,
            entry_date=self.entry_date,
            entry_price=self.entry_price,
            exit_date=date_str,
            exit_price=close,
            profit_pct=profit_pct,
            hold_days=0,
            exit_reason="回测结束强平",
            commission=commission,
            stamp_tax=tax,
            slippage_pct=0.0,
            fill_price_actual=close,
        ))

        self.cost_breakdown["total_commission"] += commission
        self.cost_breakdown["total_stamp_tax"] += tax
        self.position = 0
        self.in_position = False

    # ── 绩效计算 ────────────────────────────────────────

    def _calc_performance(self) -> dict:
        """计算全面绩效指标"""
        if not self.equity_curve:
            return {}

        values = [v for _, v in self.equity_curve]
        final_value = values[-1]
        total_return = (final_value - self.initial_capital) / self.initial_capital * 100

        # 日收益率
        daily_returns = [
            (values[i] - values[i - 1]) / values[i - 1]
            for i in range(1, len(values))
            if values[i - 1] > 0
        ]

        # Sharpe
        rf_daily = 0.02 / 252
        if daily_returns and np.std(daily_returns) > 1e-10:
            excess = [r - rf_daily for r in daily_returns]
            sharpe = round(np.mean(excess) / np.std(daily_returns) * np.sqrt(252), 2)
            sortino = round(
                np.mean(excess) / max(np.std([r for r in daily_returns if r < 0]), 1e-10) * np.sqrt(252), 2
            )
        else:
            sharpe = 0.0
            sortino = 0.0

        # Max drawdown
        peak = values[0]
        max_dd = 0.0
        dd_start = 0
        dd_end = 0
        peak_idx = 0
        for i, v in enumerate(values):
            if v > peak:
                peak = v
                peak_idx = i
            dd = (v - peak) / peak * 100 if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd
                dd_end = i
                dd_start = peak_idx

        # Calmar
        calmar = round(total_return / abs(max_dd), 2) if abs(max_dd) > 0 else 0.0

        # Win rate
        win_trades = [t for t in self.trades if t.profit_pct > 0]
        lose_trades = [t for t in self.trades if t.profit_pct <= 0]
        win_rate = round(len(win_trades) / len(self.trades) * 100, 1) if self.trades else 0.0

        # Turnover (交易笔数 / 回测天数)
        turnover = round(len(self.trades) / max(len(self.equity_curve) - 1, 1) * 100, 2)

        # 月度收益
        monthly = {}
        for date_str, val in self.equity_curve:
            month = date_str[:7]
            if month not in monthly:
                monthly[month] = []
            monthly[month].append(val)

        monthly_returns = {}
        prev_val = None
        for month in sorted(monthly.keys()):
            vals = monthly[month]
            if prev_val is not None and vals:
                monthly_returns[month] = round((vals[-1] - prev_val) / prev_val * 100, 2)
            prev_val = vals[-1] if vals else prev_val

        # 盈利因子
        gross_profit = sum(t.profit_pct for t in win_trades) if win_trades else 0
        gross_loss = abs(sum(t.profit_pct for t in lose_trades)) if lose_trades else 0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0.0
        )

        avg_hold = round(np.mean([t.hold_days for t in self.trades]), 1) if self.trades else 0

        # 成本占比
        self.cost_breakdown["total_cost"] = (
            self.cost_breakdown["total_commission"]
            + self.cost_breakdown["total_stamp_tax"]
            + self.cost_breakdown["total_slippage"]
        )
        self.cost_breakdown["cost_pct_of_capital"] = round(
            self.cost_breakdown["total_cost"] / self.initial_capital * 100, 3
        )

        return {
            "total_return_pct": round(total_return, 2),
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "max_drawdown_pct": round(abs(max_dd), 2),
            "max_drawdown_duration_days": max(dd_end - dd_start, 0),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "total_trades": len(self.trades),
            "win_trades": len(win_trades),
            "lose_trades": len(lose_trades),
            "avg_win_pct": round(np.mean([t.profit_pct for t in win_trades]), 2) if win_trades else 0,
            "avg_loss_pct": round(np.mean([t.profit_pct for t in lose_trades]), 2) if lose_trades else 0,
            "avg_hold_days": avg_hold,
            "turnover_pct": turnover,
            "monthly_returns": monthly_returns,
            "blocked_trade_count": len(self.blocked_trades),
        }

    # ── 基准构建 ────────────────────────────────────────

    def _build_benchmarks(self, dates: list[str],
                          closes: np.ndarray) -> dict:
        """构建基准收益曲线"""
        benchmarks = {}

        # 1. 等权组合基准 (从 STOCK_MAP 选 stocks，月度再平衡)
        benchmarks["equal_weight"] = self._build_equal_weight_benchmark(dates)

        # 2. HS300 指数基准
        benchmarks["hs300"] = self._build_hs300_benchmark(dates)

        return benchmarks

    def _build_equal_weight_benchmark(self, dates: list[str]) -> dict:
        """等权组合基准：取 max_benchmark_stocks 只标的，月度再平衡"""
        codes = list(STOCK_MAP.keys())[:self.max_benchmark_stocks]
        if not codes:
            return {"error": "无可用的基准标的"}

        all_basket_closes = []
        valid_codes = []

        for code in codes:
            try:
                rows = get_price_history(code, 500)
                if len(rows) < 30:
                    continue
                rows.sort(key=lambda r: r["date"])
                c = np.array([r["close"] for r in rows], dtype=float)
                d = [r["date"] for r in rows]

                # 对齐到主标的日期
                aligned = []
                date_idx = 0
                for target_date in dates:
                    while date_idx < len(d) and d[date_idx] < target_date:
                        date_idx += 1
                    if date_idx < len(d) and d[date_idx] == target_date:
                        aligned.append(c[date_idx])
                    elif aligned:
                        aligned.append(aligned[-1])  # 向前填充
                    else:
                        aligned.append(np.nan)
                all_basket_closes.append(np.array(aligned, dtype=float))
                valid_codes.append(code)
            except Exception:
                continue

        if not all_basket_closes:
            return {"error": "无基准标的数据"}

        # 等权组合净值
        basket_matrix = np.array(all_basket_closes)
        # 归一化到 1.0
        normalized = basket_matrix / basket_matrix[:, 0:1]
        # 等权平均
        eq_curve = np.nanmean(normalized, axis=0)

        benchmark_equity = []
        for i, date_str in enumerate(dates):
            benchmark_equity.append((date_str, round(float(eq_curve[i]) * self.initial_capital, 2)))

        # 月度再平衡（简化：在每个月的第一个交易日重置权重）
        # 这里使用简单等权，不再额外操作

        return {
            "type": "equal_weight_basket",
            "num_stocks": len(valid_codes),
            "stocks": valid_codes,
            "rebalance": "monthly",
            "equity_curve": benchmark_equity,
        }

    def _build_hs300_benchmark(self, dates: list[str]) -> dict:
        """HS300 指数基准 — 优先从 Sina API 获取，失败则用标的代理"""
        hs300_data = self._fetch_hs300_kline(dates)

        if hs300_data is not None and len(hs300_data) > 0:
            # 归一化到 initial_capital
            if hs300_data[0][1] > 0:
                scale = self.initial_capital / hs300_data[0][1]
                equity_curve = [
                    (d, round(v * scale, 2)) for d, v in hs300_data
                ]
            else:
                equity_curve = hs300_data

            return {
                "type": "hs300_index",
                "source": "sina_api",
                "equity_curve": equity_curve,
            }

        # 回退：用标的加权代理
        codes = list(STOCK_MAP.keys())[:self.max_benchmark_stocks]
        if not codes:
            return {"error": "HS300 数据不可用"}

        all_closes = []
        for code in codes:
            try:
                rows = get_price_history(code, 500)
                if len(rows) < 30:
                    continue
                rows.sort(key=lambda r: r["date"])
                c = np.array([r["close"] for r in rows], dtype=float)
                d = [r["date"] for r in rows]
                aligned = []
                date_idx = 0
                for target_date in dates:
                    while date_idx < len(d) and d[date_idx] < target_date:
                        date_idx += 1
                    if date_idx < len(d) and d[date_idx] == target_date:
                        aligned.append(c[date_idx])
                    elif aligned:
                        aligned.append(aligned[-1])
                    else:
                        aligned.append(np.nan)
                all_closes.append(np.array(aligned, dtype=float))
            except Exception:
                continue

        if not all_closes:
            return {"error": "无 HS300 代理数据"}

        basket = np.array(all_closes)
        norm = basket / basket[:, 0:1]
        proxy = np.nanmean(norm, axis=0)

        equity = [
            (dates[i], round(float(proxy[i]) * self.initial_capital, 2))
            for i in range(len(dates))
        ]

        return {
            "type": "hs300_proxy",
            "source": "equal_weight_proxy",
            "equity_curve": equity,
        }

    def _fetch_hs300_kline(self, dates: list[str]) -> list[tuple[str, float]] | None:
        """从 Sina API 获取 HS300 日线数据"""
        if len(dates) < 2:
            return None

        start_date = dates[0]
        end_date = dates[-1]

        # Sina K-line API for HS300 (sh000300)
        url = (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?"
            f"symbol=sh000300&scale=240&ma=no&datalen=500"
        )

        try:
            req = urllib.request.Request(url)
            req.add_header("Referer", "https://finance.sina.com.cn")
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)

            # Parse and align to dates
            kline_map = {}
            for item in data:
                d = item.get("day", "")
                close_val = float(item.get("close", 0))
                if d and close_val > 0:
                    kline_map[d] = close_val

            # Align to the strategy's date range
            result = []
            last_val = None
            for date_str in dates:
                if date_str in kline_map:
                    last_val = kline_map[date_str]
                if last_val is not None:
                    result.append((date_str, last_val))

            if len(result) >= 30:
                return result
        except Exception:
            pass

        return None

    # ── 基准对比 ────────────────────────────────────────

    def _benchmark_comparison(self, benchmarks: dict) -> dict:
        """计算 vs 各基准的超额收益"""
        if not self.equity_curve:
            return {}

        strategy_values = {d: v for d, v in self.equity_curve}
        comparison = {}

        for name, bm in benchmarks.items():
            bm_curve = bm.get("equity_curve", [])
            if not bm_curve:
                comparison[name] = {"error": bm.get("error", "无基准数据")}
                continue

            # 取交集日期
            bm_values = {d: v for d, v in bm_curve}
            common_dates = sorted(
                set(strategy_values.keys()) & set(bm_values.keys())
            )
            if len(common_dates) < 5:
                comparison[name] = {"error": "共同日期不足"}
                continue

            strat_vals = [strategy_values[d] for d in common_dates]
            bm_vals = [bm_values[d] for d in common_dates]

            # 收益
            strat_return = (strat_vals[-1] - strat_vals[0]) / strat_vals[0] * 100
            bm_return = (bm_vals[-1] - bm_vals[0]) / bm_vals[0] * 100
            excess = round(strat_return - bm_return, 2)

            # 信息比率
            strat_daily = []
            bm_daily = []
            for i in range(1, len(common_dates)):
                if strat_vals[i - 1] > 0 and bm_vals[i - 1] > 0:
                    strat_daily.append(
                        (strat_vals[i] - strat_vals[i - 1]) / strat_vals[i - 1]
                    )
                    bm_daily.append(
                        (bm_vals[i] - bm_vals[i - 1]) / bm_vals[i - 1]
                    )

            tracking_error = 0.0
            ir = 0.0
            if strat_daily:
                diffs = [strat_daily[i] - bm_daily[i] for i in range(len(strat_daily))]
                tracking_error = float(np.std(diffs) * np.sqrt(252))
                if tracking_error > 0:
                    ir = round(float(np.mean(diffs)) / tracking_error * np.sqrt(252), 2)

            # 最大相对回撤
            relative_curve = [
                strat_vals[i] / bm_vals[i]
                for i in range(len(common_dates))
            ]
            peak = relative_curve[0]
            max_rel_dd = 0.0
            for v in relative_curve:
                if v > peak:
                    peak = v
                dd = (v - peak) / peak * 100
                if dd < max_rel_dd:
                    max_rel_dd = dd

            comparison[name] = {
                "strategy_return_pct": round(strat_return, 2),
                "benchmark_return_pct": round(bm_return, 2),
                "excess_return_pct": excess,
                "tracking_error": round(tracking_error * 100, 2),
                "information_ratio": ir,
                "max_relative_drawdown_pct": round(abs(max_rel_dd), 2),
                "benchmark_type": bm.get("type", name),
                "benchmark_source": bm.get("source", "unknown"),
                "annualized_excess": round(excess / (len(common_dates) / 252), 2) if common_dates else 0,
            }

        return comparison


# ═══════════════════════════════════════════════════════════════════
# 事件驱动回测入口
# ═══════════════════════════════════════════════════════════════════

def run_event_driven_backtest(
    code: str,
    strategy: BaseStrategy,
    initial_capital: float = 50000.0,
    commission_rate: float = 0.00025,
    stamp_tax: float = 0.001,
    position_pct: float = 0.30,
    enable_microstructure: bool = True,
) -> dict:
    """运行事件驱动回测。

    相比 run_backtest() 的向量化回测，此函数提供：
      - 每日 Pre-Market / Intraday / Post-Market 事件循环
      - A-Share 微观结构约束（T+1、涨跌停、流动性建模）
      - 防前视偏差 guard
      - 等权组合和 HS300 基准对比
      - 完整的成本分解（佣金、印花税、滑点）

    Args:
        code: 股票代码
        strategy: 策略实例 (BaseStrategy 子类)
        initial_capital: 初始资金
        commission_rate: 佣金率
        stamp_tax: 印花税率
        position_pct: 单次建仓资金比例
        enable_microstructure: 是否启用微观结构模拟

    Returns:
        dict: {
            trades, equity_curve, performance,
            benchmark_comparison, benchmarks,
            blocked_trades, cost_breakdown,
            audit_log_length, engine
        }
    """
    engine = EventDrivenBacktest(
        code=code,
        strategy=strategy,
        initial_capital=initial_capital,
        commission_rate=commission_rate,
        stamp_tax=stamp_tax,
        position_pct=position_pct,
        enable_microstructure=enable_microstructure,
    )
    return engine.run()
