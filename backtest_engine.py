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
from datetime import datetime
from typing import Optional
import numpy as np
from db import get_price_history
from config import STOCK_MAP


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


def format_backtest_result(result: dict) -> str:
    """格式化单个回测结果"""
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
