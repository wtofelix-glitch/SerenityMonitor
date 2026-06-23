"""
因子归因日报 — 评估 14 个信号因子对次日收益的贡献与衰减

Functions:
    compute_factor_contribution(code, days=60)  → 贡献分析
    compute_ic_trend(code=None, window=20)      → IC 趋势追踪
    detect_factor_decay(code=None, ...)         → 衰减检测
    generate_factor_report(code=None)           → 微信推送文本

Usage:
    from factor_attribution import compute_factor_contribution, ...
    python3 factor_attribution.py                    # 全市场报告
    python3 factor_attribution.py --code 002281      # 单只标的
"""
import numpy as np
from collections import defaultdict
from db import get_conn, get_price_history

from factor_metadata import SIGNAL_FACTORS, FACTOR_LABELS, FACTOR_EMOJIS
from factor_engine import _to_arrays, _ema, _linear_regression

# 需要回溯的天数（各因子中最大窗口）
MAX_FACTOR_WINDOW = 65  # obv/macd 需要~35d，留余量


# ═══════════════════════════════════════════════════════════
# 内部：将 price_history rows 转为正序 numpy 数组
# ═══════════════════════════════════════════════════════════

def _compute_daily_factors(opens, closes, highs, lows, volumes):
    """
    在给定价格数组上计算 14 个信号因子。
    数组最后一个元素为"当前日"。
    返回 {name: value}，value 归一化到 [-1, 1]。
    """
    n = len(closes)
    if n < 2:
        return {}
    result = {}

    o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
    denom = o if abs(o) > 1e-12 else 1e-12
    hl_range = h - l + 1e-12

    # ── K线形态因子 ──
    # ksft = (close*2 - high - low) / open
    ksft_raw = (c * 2 - h - l) / denom
    result["ksft"] = float(np.tanh(ksft_raw * 2.0))

    # ── 时序因子（20日窗口） ──
    if n >= 21:
        wc = closes[-21:]  # 最近 21 个 close
        wh = highs[-21:]
        wl = lows[-21:]
        wc_20 = wc[1:]  # 最近 20 个 close（不含最远端的）
        wh_20 = wh[1:]
        wl_20 = wl[1:]

        # rank_20: (close - min(close,20)) / (max(close,20) - min(close,20))
        rank_num = (c - wc_20.min()) / (wc_20.max() - wc_20.min() + 1e-12)
        result["rank_20"] = float(np.clip(rank_num, 0, 1))

        # rsv_20: (close - min(low,20)) / (max(high,20) - min(low,20))
        rsv_num = (c - wl_20.min()) / (wh_20.max() - wl_20.min() + 1e-12)
        result["rsv_20"] = float(np.clip(rsv_num, 0, 1))

        # beta_20, resi_20: 线性回归
        slope, _, r_sq, residuals = _linear_regression(wc_20)
        result["beta_20"] = float(np.tanh(slope * 10.0))
        resi_val = residuals[-1] / c if abs(c) > 1e-12 else 0.0
        result["resi_20"] = float(np.tanh(resi_val * 20.0))

    # ── MACD ──
    if n >= 27:
        macd_closes = closes[-35:] if n >= 35 else closes
        ema12 = _ema(macd_closes, 12)
        ema26 = _ema(macd_closes, 26)
        dif = ema12 - ema26
        dea = _ema(dif, 9)
        hist = (dif - dea) * 2.0
        result["macd_signal"] = float(np.tanh(hist[-1] / 0.5))

    # ── OBV ──
    if n >= 25:
        obv_closes = closes[-30:] if n >= 30 else closes
        obv_volumes = volumes[-30:] if n >= 30 else volumes
        obv_raw = np.zeros(len(obv_closes), dtype=float)
        for i in range(1, len(obv_closes)):
            diff = obv_closes[i] - obv_closes[i - 1]
            if diff > 1e-12:
                obv_raw[i] = obv_raw[i - 1] + obv_volumes[i]
            elif diff < -1e-12:
                obv_raw[i] = obv_raw[i - 1] - obv_volumes[i]
            else:
                obv_raw[i] = obv_raw[i - 1]
        window = 20
        obv_win = obv_raw[-window:]
        slope_obv, *_ = _linear_regression(obv_win)
        obv_std = np.std(obv_win)
        result["obv_trend"] = float(np.tanh(slope_obv / (obv_std + 1e-12) * 2.0))

    # ── MFI ──
    if n >= 16:
        period = 14
        mfi_closes = closes
        mfi_highs = highs
        mfi_lows = lows
        mfi_volumes = volumes
        tp = (mfi_highs + mfi_lows + mfi_closes) / 3.0
        rmf = tp * mfi_volumes
        pos_mf = np.zeros(len(tp), dtype=float)
        neg_mf = np.zeros(len(tp), dtype=float)
        for i in range(1, len(tp)):
            if tp[i] > tp[i - 1]:
                pos_mf[i] = rmf[i]
            elif tp[i] < tp[i - 1]:
                neg_mf[i] = rmf[i]
        pos_sum = pos_mf[-period:].sum()
        neg_sum = neg_mf[-period:].sum()
        if neg_sum < 1e-12:
            mfi_val = 100.0 if pos_sum > 1e-12 else 50.0
        else:
            mfi_val = 100.0 - 100.0 / (1.0 + pos_sum / neg_sum)
        result["mfi_signal"] = float(np.clip((mfi_val - 50.0) / 50.0, -1.0, 1.0))

    # ── CCI ──
    if n >= 24:
        period = 20
        cci_tp = (highs + lows + closes) / 3.0
        tp_win = cci_tp[-period:]
        sma_tp = tp_win.mean()
        mean_dev = np.abs(tp_win - sma_tp).mean()
        if mean_dev > 1e-12:
            cci_val = (tp_win[-1] - sma_tp) / (0.015 * mean_dev)
        else:
            cci_val = 0.0
        result["cci_signal"] = float(np.tanh(cci_val / 100.0))

    # ── WQ Alpha#1: (close-open)/(high-low) ──
    a1_raw = (c - o) / hl_range
    result["wq_alpha1"] = float(np.tanh(a1_raw * 2.0))

    # ── WQ Alpha#15: (high/low)-1 ──
    if abs(l) > 1e-12:
        a15_raw = (h / l) - 1.0
        result["wq_alpha15"] = float(np.tanh(a15_raw * 20.0))

    # ── WQ Alpha#19: (close/close.shift(5))-1 ──
    if n >= 6:
        close_5d = closes[-6]
        if abs(close_5d) > 1e-12:
            a19_raw = (c / close_5d) - 1.0
            result["wq_alpha19"] = float(np.tanh(a19_raw * 10.0))

    # 缺省值补零
    for f in SIGNAL_FACTORS:
        if f not in result:
            result[f] = 0.0

    return result


# ═══════════════════════════════════════════════════════════
# 1. compute_factor_contribution
# ═══════════════════════════════════════════════════════════

def compute_factor_contribution(code, days=60):
    """
    计算单只标的历史上每个信号因子对次日收益率的贡献。

    公式:
        contribution = sign(因子值) × 次日收益率

    返回:
        {因子名: {
            avg_contribution: float,    # 平均贡献
            positive_ratio: float,      # 正向贡献占比 (%)
            total_contribution: float,  # 总贡献
            n_samples: int,              # 有效样本数
        }}
    """
    need = days + MAX_FACTOR_WINDOW + 5
    rows = get_price_history(code, days=need)
    if len(rows) < 30:
        return {}

    arr = _to_arrays(rows)
    opens   = arr["open"]
    closes  = arr["close"]
    highs   = arr["high"]
    lows    = arr["low"]
    volumes = arr["volume"]
    dates   = arr["dates"]
    n = len(closes)

    # 次日收益率
    returns = np.zeros(n)
    for i in range(n - 1):
        if closes[i] > 1e-8:
            returns[i] = closes[i + 1] / closes[i] - 1.0
    # 过滤异常
    returns = np.where(np.abs(returns) > 0.20, 0.0, returns)

    # 逐日计算因子贡献
    contributions = {f: [] for f in SIGNAL_FACTORS}
    samples = 0

    # 从第 MAX_FACTOR_WINDOW 天开始（确保有足够历史数据算因子）
    start = min(MAX_FACTOR_WINDOW, n - 2)
    for i in range(start, n - 1):
        # 用 data[:i+1] 算当前因子值
        factors = _compute_daily_factors(
            opens[:i + 1], closes[:i + 1],
            highs[:i + 1], lows[:i + 1],
            volumes[:i + 1],
        )
        if not factors:
            continue
        ret = returns[i]
        if abs(ret) < 1e-12:
            continue
        samples += 1
        for fname in SIGNAL_FACTORS:
            fv = factors.get(fname, 0.0)
            contrib = (1.0 if fv >= 0 else -1.0) * ret
            contributions[fname].append(contrib)

    result = {}
    for fname in SIGNAL_FACTORS:
        vals = contributions[fname]
        if not vals:
            continue
        arr_vals = np.array(vals)
        result[fname] = {
            "avg_contribution": round(float(np.mean(arr_vals)), 6),
            "positive_ratio": round(float(np.sum(arr_vals > 0) / len(arr_vals) * 100), 1),
            "total_contribution": round(float(np.sum(arr_vals)), 6),
            "n_samples": len(arr_vals),
        }

    result["_meta"] = {"code": code, "days": days, "samples": samples}
    return result


# ═══════════════════════════════════════════════════════════
# 2. compute_ic_trend
# ═══════════════════════════════════════════════════════════

try:
    from scipy.stats import spearmanr as _spearmanr
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


def _spearmanr_wrapper(x, y):
    """安全的 Spearman 秩相关，处理边角情况"""
    xa, ya = np.array(x, dtype=float), np.array(y, dtype=float)
    if len(xa) < 5:
        return 0.0
    if np.std(xa) < 1e-12 or np.std(ya) < 1e-12:
        return 0.0
    if HAS_SCIPY:
        with np.errstate(all="ignore"):
            r, _ = _spearmanr(xa, ya)
        return r if (not np.isnan(r) and not np.isinf(r)) else 0.0
    # numpy fallback
    rx = np.argsort(np.argsort(xa)).astype(float)
    ry = np.argsort(np.argsort(ya)).astype(float)
    rx_m, ry_m = np.mean(rx), np.mean(ry)
    num = np.sum((rx - rx_m) * (ry - ry_m))
    den = np.sqrt(np.sum((rx - rx_m) ** 2) * np.sum((ry - ry_m) ** 2))
    return float(num / den) if den != 0 else 0.0


def _get_all_codes():
    """获取所有监控标的代码"""
    conn = get_conn()
    rows = conn.execute("SELECT code FROM stocks").fetchall()
    conn.close()
    return [r["code"] for r in rows]


def _get_price_arrays_for_date(all_arrays, date_idx, lookback=MAX_FACTOR_WINDOW):
    """
    从所有标的的价格数据中提取到 date_idx 为止的最新数据
    返回 {code: {opens, closes, highs, lows, volumes}}
    """
    result = {}
    for code, arr in all_arrays.items():
        if date_idx >= len(arr["close"]):
            continue
        start = max(0, date_idx - lookback)
        result[code] = {
            "open": arr["open"][start:date_idx + 1],
            "close": arr["close"][start:date_idx + 1],
            "high": arr["high"][start:date_idx + 1],
            "low": arr["low"][start:date_idx + 1],
            "volume": arr["volume"][start:date_idx + 1],
        }
    return result


def compute_ic_trend(code=None, window=20):
    """
    计算每日截面 IC（因子 vs 次日收益率）。

    若 code 为 None，用全市场截面计算每日 IC；
    若 code 指定，用时间序列计算该标的每日因子 IC。

    返回:
        {因子名: {
            current_ic: float,    # 最新一天 IC
            ic_mean: float,       # 窗口内均值
            ic_std: float,        # 窗口内标准差
            ic_ir: float,         # IC 稳定性比
            ic_trend: str,        # "上升" / "下降" / "平稳"
        }}
    """
    if code:
        return _compute_ic_trend_ts(code, window)
    return _compute_ic_trend_cross_sectional(window)


def _compute_ic_trend_cross_sectional(window=20):
    """
    全市场截面 IC：每日取所有标的的因子值 vs 次日收益率，算 Spearman 秩相关。
    """
    codes = _get_all_codes()
    if not codes:
        return {}

    # 加载所有标的的价格数据
    all_arrays = {}
    for c in codes:
        rows = get_price_history(c, days=MAX_FACTOR_WINDOW + window + 10)
        if len(rows) < 30:
            continue
        all_arrays[c] = _to_arrays(rows)

    if not all_arrays:
        return {}

    # 找共同的最早日期索引（所有标的都有数据的最大起始点）
    # 简化：对每个标的，对齐到每个日期
    # 用 date 作为键来对齐
    date_factor_map = defaultdict(lambda: {f: [] for f in SIGNAL_FACTORS})
    date_return_map = defaultdict(list)

    for code, arr in all_arrays.items():
        n = len(arr["close"])
        opens_o, closes_o = arr["open"], arr["close"]
        highs_o, lows_o = arr["high"], arr["low"]
        volumes_o = arr["volume"]
        dates_o = arr["dates"]

        # 从后往前，对每个可能的天数计算因子
        start = min(MAX_FACTOR_WINDOW, n - 2)
        for i in range(start, n - 1):
            date_str = dates_o[i]
            factors = _compute_daily_factors(
                opens_o[:i + 1], closes_o[:i + 1],
                highs_o[:i + 1], lows_o[:i + 1],
                volumes_o[:i + 1],
            )
            if not factors:
                continue
            ret = closes_o[i + 1] / closes_o[i] - 1.0 if closes_o[i] > 1e-8 else 0.0
            if abs(ret) > 0.20:
                continue
            for fname in SIGNAL_FACTORS:
                date_factor_map[date_str][fname].append(factors.get(fname, 0.0))
            date_return_map[date_str].append(ret)

    # 对每个日期计算截面 IC
    all_dates = sorted(date_factor_map.keys())
    ics_by_factor = {f: [] for f in SIGNAL_FACTORS}

    for date_str in all_dates:
        returns_arr = np.array(date_return_map.get(date_str, []))
        if len(returns_arr) < 5:
            continue
        for fname in SIGNAL_FACTORS:
            vals = np.array(date_factor_map[date_str].get(fname, []))
            if len(vals) < 5:
                continue
            ic = _spearmanr_wrapper(vals, returns_arr)
            ics_by_factor[fname].append(ic)

    # 聚合统计
    result = _aggregate_ic_stats(ics_by_factor, window)
    return result


def _compute_ic_trend_ts(code, window=20):
    """
    时间序列 IC：单只标的每日因子值 vs 次日收益率。
    """
    need = window + MAX_FACTOR_WINDOW + 10
    rows = get_price_history(code, days=need)
    if len(rows) < 30:
        return {}

    arr = _to_arrays(rows)
    opens_o, closes_o = arr["open"], arr["close"]
    highs_o, lows_o = arr["high"], arr["low"]
    volumes_o = arr["volume"]
    n = len(closes_o)

    returns = np.zeros(n)
    for i in range(n - 1):
        if closes_o[i] > 1e-8:
            returns[i] = closes_o[i + 1] / closes_o[i] - 1.0
    returns = np.where(np.abs(returns) > 0.20, 0.0, returns)

    ics_by_factor = {f: [] for f in SIGNAL_FACTORS}
    start = min(MAX_FACTOR_WINDOW, n - 2)
    for i in range(start, n - 1):
        factors = _compute_daily_factors(
            opens_o[:i + 1], closes_o[:i + 1],
            highs_o[:i + 1], lows_o[:i + 1],
            volumes_o[:i + 1],
        )
        if not factors:
            continue
        ret = returns[i]
        if abs(ret) < 1e-12:
            continue
        # 时间序列 IC = factor_value vs return（单样本，无法算秩相关）
        # 改用当天所有因子的值 vs return 的截面
        # 或简化为因子值与收益率的符号一致性
        for fname in SIGNAL_FACTORS:
            fv = factors.get(fname, 0.0)
            ics_by_factor[fname].append(fv)

    # 对时间序列，用滚动窗口算因子值与收益率的秩相关
    # 实际上这是因子值序列 vs 收益率序列的相关
    ts_ics = {}
    for fname in SIGNAL_FACTORS:
        vals = np.array(ics_by_factor[fname])
        if len(vals) < window + 5:
            continue
        rets_arr = returns[start:n - 1][-len(vals):]
        # 滚动窗口 IC
        daily_ics = []
        for j in range(window, len(vals)):
            ic = _spearmanr_wrapper(vals[j - window:j], rets_arr[j - window:j])
            daily_ics.append(ic)
        ts_ics[fname] = daily_ics

    return _aggregate_ic_stats(ts_ics, window)


def _aggregate_ic_stats(ics_by_factor, window):
    """对每个因子的 IC 序列做聚合统计"""
    result = {}
    for fname in SIGNAL_FACTORS:
        ics = ics_by_factor.get(fname, [])
        if len(ics) < 5:
            result[fname] = {
                "current_ic": 0.0,
                "ic_mean": 0.0,
                "ic_std": 0.0,
                "ic_ir": 0.0,
                "ic_trend": "数据不足",
            }
            continue

        arr_ics = np.array(ics)
        current_ic = float(arr_ics[-1])
        wdw = arr_ics[-window:] if len(arr_ics) > window else arr_ics
        ic_mean = float(np.mean(wdw))
        ic_std = float(np.std(wdw, ddof=1)) if len(wdw) > 1 else 0.0
        ic_ir = ic_mean / ic_std if ic_std > 1e-12 else 0.0

        # 趋势判定：最近5日均值 vs 前5日均值
        if len(arr_ics) >= 10:
            recent5 = float(np.mean(arr_ics[-5:]))
            prev5 = float(np.mean(arr_ics[-10:-5]))
            diff = recent5 - prev5
            if diff > 0.02:
                trend = "上升 ↑"
            elif diff < -0.02:
                trend = "下降 ↓"
            else:
                trend = "平稳 →"
        else:
            trend = "平稳 →"

        result[fname] = {
            "current_ic": round(current_ic, 4),
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ic_ir": round(ic_ir, 4),
            "ic_trend": trend,
        }

    return result


# ═══════════════════════════════════════════════════════════
# 3. detect_factor_decay
# ═══════════════════════════════════════════════════════════

def detect_factor_decay(code=None, long_window=60, short_window=10):
    """
    检测因子衰减。

    条件（满足任一）:
        1. short_ic_mean < long_ic_mean * 0.5
        2. short_ic 为负 且 long_ic 为正

    返回:
        [{factor, status, long_ic, short_ic, recommend}, ...]
    """
    # 用 compute_ic_trend 获取长短窗口 IC
    # 长窗口 IC ≈ 整体 mean_ic
    long_result = compute_ic_trend(code=code, window=long_window)
    # 用更短窗口再算一次
    short_result = compute_ic_trend(code=code, window=short_window)

    decay_list = []
    for fname in SIGNAL_FACTORS:
        long_info = long_result.get(fname, {})
        short_info = short_result.get(fname, {})

        long_ic = long_info.get("ic_mean", 0.0)
        short_ic = short_info.get("ic_mean", 0.0)

        # 判断衰减
        decayed = False
        reason = ""

        if abs(long_ic) > 1e-12 and short_ic < long_ic * 0.5:
            decayed = True
            reason = f"短期IC({short_ic:.3f}) < 长期IC({long_ic:.3f})×0.5"
        elif short_ic < 0 and long_ic > 0.01:
            decayed = True
            reason = f"短期IC({short_ic:.3f})转负，长期IC({long_ic:.3f})仍为正"

        if decayed:
            decay_list.append({
                "factor": fname,
                "label": FACTOR_LABELS.get(fname, fname),
                "status": "衰减 ⚠️",
                "long_ic": round(long_ic, 4),
                "short_ic": round(short_ic, 4),
                "reason": reason,
                "recommend": "建议降低该因子权重或暂停使用",
            })
        else:
            decay_list.append({
                "factor": fname,
                "label": FACTOR_LABELS.get(fname, fname),
                "status": "正常 ✅",
                "long_ic": round(long_ic, 4),
                "short_ic": round(short_ic, 4),
                "reason": "",
                "recommend": "",
            })

    # 按衰减严重程度排序：已衰减的排前面
    decay_list.sort(key=lambda x: (x["short_ic"] - x["long_ic"] if x["status"] == "衰减 ⚠️" else 999))
    return decay_list


# ═══════════════════════════════════════════════════════════
# 4. generate_factor_report
# ═══════════════════════════════════════════════════════════

def generate_factor_report(code=None):
    """
    生成因子归因日报（适合微信推送的文本）。

    Args:
        code: 标的代码，None 表示全市场

    Returns:
        str: 报告文本
    """
    from datetime import datetime

    lines = []
    scope = f"全市场" if code is None else code
    lines.append(f"📊 因子归因日报 | {scope}")
    lines.append(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # ── IC 趋势 ──
    ic_result = compute_ic_trend(code=code, window=20)
    if ic_result:
        lines.append("📈 IC 趋势 (20日窗口)")
        lines.append("─" * 30)

        # 按 |current_ic| 排序
        sorted_factors = sorted(
            [(f, ic_result[f]) for f in SIGNAL_FACTORS if f in ic_result],
            key=lambda x: abs(x[1].get("current_ic", 0)),
            reverse=True,
        )

        for fname, info in sorted_factors:
            label = FACTOR_LABELS.get(fname, fname)
            emoji = FACTOR_EMOJIS.get(fname, "")
            cur = info.get("current_ic", 0)
            mean = info.get("ic_mean", 0)
            ir = info.get("ic_ir", 0)
            trend = info.get("ic_trend", "")
            signal_emoji = "🟢" if cur > 0.05 else "🔴" if cur < -0.05 else "⚪"
            lines.append(
                f"{emoji} {label:<6} {signal_emoji} "
                f"最新IC:{cur:+.3f} 均值:{mean:+.3f} "
                f"IR:{ir:+.2f} {trend}"
            )
        lines.append("")

    # ── 衰减检测 ──
    decay_list = detect_factor_decay(code=code, long_window=60, short_window=10)
    decayed = [d for d in decay_list if d["status"] == "衰减 ⚠️"]
    if decayed:
        lines.append("⚠️ 因子衰减预警")
        lines.append("─" * 30)
        for d in decayed:
            lines.append(
                f"{FACTOR_EMOJIS.get(d['factor'], '')} {d['label']:<6} "
                f"短期IC:{d['short_ic']:+.3f} 长期IC:{d['long_ic']:+.3f}"
            )
            lines.append(f"  → {d['recommend']}")
        lines.append("")
    else:
        lines.append("✅ 无因子衰减")
        lines.append("")

    # ── 有效因子排名 ──
    if ic_result:
        lines.append("🏆 最有效因子 Top3")
        best = sorted(
            [(f, ic_result[f]) for f in SIGNAL_FACTORS if f in ic_result],
            key=lambda x: abs(x[1].get("current_ic", 0)),
            reverse=True,
        )[:3]
        for fname, info in best:
            label = FACTOR_LABELS.get(fname, fname)
            lines.append(f"  {FACTOR_EMOJIS.get(fname, '')} {label}: IC={info.get('current_ic', 0):+.4f}")
        lines.append("")

        lines.append("🔻 最无效因子 Bottom3")
        worst = sorted(
            [(f, ic_result[f]) for f in SIGNAL_FACTORS if f in ic_result],
            key=lambda x: abs(x[1].get("current_ic", 0)),
        )[:3]
        for fname, info in worst:
            label = FACTOR_LABELS.get(fname, fname)
            lines.append(f"  {FACTOR_EMOJIS.get(fname, '')} {label}: IC={info.get('current_ic', 0):+.4f}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="因子归因分析 — 贡献度 / IC 趋势 / 衰减检测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--code", default=None, help="标的代码（默认全市场）")
    parser.add_argument("--days", type=int, default=60, help="回溯天数")
    parser.add_argument("--window", type=int, default=20, help="IC 滚动窗口")
    parser.add_argument("--mode", choices=["report", "contribution", "ic", "decay"],
                        default="report", help="分析模式")
    parser.add_argument("--json", action="store_true", help="JSON 输出")

    args = parser.parse_args()

    import json

    if args.mode == "contribution":
        codes = [args.code] if args.code else _get_all_codes()
        all_results = {}
        for c in codes:
            contrib = compute_factor_contribution(c, days=args.days)
            if contrib:
                all_results[c] = contrib
        if args.json:
            print(json.dumps(all_results, ensure_ascii=False, indent=2))
        else:
            _print_contribution_report(all_results)
        return

    if args.mode == "ic":
        result = compute_ic_trend(code=args.code, window=args.window)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_ic_report(result)
        return

    if args.mode == "decay":
        decay = detect_factor_decay(code=args.code, long_window=60, short_window=10)
        if args.json:
            print(json.dumps(decay, ensure_ascii=False, indent=2))
        else:
            _print_decay_report(decay)
        return

    # default: report
    report = generate_factor_report(code=args.code)
    print(report)


def _print_contribution_report(all_results):
    """打印因子贡献报告"""
    print()
    print("📊 因子贡献分析")
    print("═" * 60)
    for code, result in all_results.items():
        meta = result.pop("_meta", {})
        print(f"\n📌 {code} (样本数: {meta.get('samples', 0)})")
        print("─" * 60)
        print(f"{'因子':<10} {'均贡献':>10} {'胜率':>8} {'总贡献':>10}")
        print("─" * 40)
        # 按均贡献绝对值排序
        items = []
        for fname, info in result.items():
            if isinstance(info, dict) and "avg_contribution" in info:
                items.append((fname, info))
        items.sort(key=lambda x: abs(x[1]["avg_contribution"]), reverse=True)
        for fname, info in items:
            label = FACTOR_LABELS.get(fname, fname)
            print(f"{label:<10} {info['avg_contribution']:>+10.4f} "
                  f"{info['positive_ratio']:>7.1f}% {info['total_contribution']:>+10.4f}")
    print()


def _print_ic_report(result):
    """打印 IC 趋势报告"""
    print()
    print("📈 IC 趋势报告")
    print("═" * 60)
    print(f"{'因子':<10} {'最新IC':>10} {'均值':>10} {'IC_IR':>10} {'趋势':<10}")
    print("─" * 60)
    for fname in SIGNAL_FACTORS:
        info = result.get(fname, {})
        if not info:
            continue
        label = FACTOR_LABELS.get(fname, fname)
        cur = info.get("current_ic", 0)
        mean = info.get("ic_mean", 0)
        ir = info.get("ic_ir", 0)
        trend = info.get("ic_trend", "")
        print(f"{label:<10} {cur:>+10.4f} {mean:>+10.4f} {ir:>+10.4f} {trend:<10}")
    print()


def _print_decay_report(decay_list):
    """打印衰减检测报告"""
    print()
    print("⚠️ 因子衰减检测")
    print("═" * 60)
    print(f"{'因子':<10} {'状态':<12} {'长期IC':>10} {'短期IC':>10} {'建议'}")
    print("─" * 60)
    for d in decay_list:
        label = d.get("label", d["factor"])
        print(f"{label:<10} {d['status']:<12} {d['long_ic']:>+10.4f} "
              f"{d['short_ic']:>+10.4f} {d.get('recommend', '')}")
    print()


if __name__ == "__main__":
    main()
