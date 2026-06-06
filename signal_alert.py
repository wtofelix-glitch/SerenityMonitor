"""
信号主动推送引擎 — 检测多维度信号并推送微信预警

检测条件（满足任一触发推送）:
a. 三因子共振：14因子中3个以上同时 ≥0.3 或 ≤-0.3
b. 技术面+基本面+因子三重确认：技术评分>70 + 基本面评分>0.3 + 因子综合>15
c. 因子突变：单因子相比前日变化 >0.3（用 factor_monitor.py 的缓存机制）
d. 极端信号：rsv_20≥80（超买）或 rsv_20≤20（超卖）且 macd_signal 同向

频率控制：同一标的同一类型警报 24 小时内不重复推送
（使用 ~/.signal_alert_state.json 记录）

Usage:
    from signal_alert import check_all_alerts
    pushed = check_all_alerts()  # → 返回已推送列表
"""

import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import STOCK_MAP, ALL_CODES
from factor_engine import AlphaFactorEngine
from signal_engine import compute_technical_factors, compute_trend_score
from fundamental_engine import FundamentalEngine
from scorer import score_all

logger = logging.getLogger("Serenity.SignalAlert")

# ============================================================
# 路径配置
# ============================================================

# 频率控制状态文件
STATE_PATH = os.path.expanduser("~/.signal_alert_state.json")

# 因子缓存文件（复用 factor_monitor.py 的缓存机制）
FACTOR_CACHE_PATH = os.path.expanduser("~/.serenity_factor_cache.json")

# ============================================================
# 阈值配置
# ============================================================

RESONANCE_THRESHOLD = 0.3       # 三因子共振阈值
RESONANCE_MIN_COUNT = 3         # 共振最少因子数

TRIPLE_TECH_THRESHOLD = 70      # 技术评分阈值 (0-100)
TRIPLE_FUND_THRESHOLD = 0.3     # 基本面评分阈值 (-1 to 1)
TRIPLE_FACTOR_THRESHOLD = 15    # 因子综合阈值 (0-100)

MUTATION_THRESHOLD = 0.3        # 因子突变阈值

RSV_OVERBOUGHT = 0.80           # RSV 超买 (raw rsv_20 in [0,1])
RSV_OVERSOLD = 0.20             # RSV 超卖

REPEAT_HOURS = 24               # 同一警报重复推送间隔

# ============================================================
# 因子中文名映射（用于消息展示）
# ============================================================

FACTOR_CN = {
    "ksft": "K线形态", "rank_20": "Rank排名", "rsv_20": "RSV",
    "beta_20": "Beta", "resi_20": "残差", "macd_signal": "MACD",
    "obv_trend": "OBV", "mfi_signal": "MFI", "cci_signal": "CCI",
    "wq_alpha1": "Alpha#1日内", "wq_alpha3": "Alpha#3均价",
    "wq_alpha5": "Alpha#5价偏", "wq_alpha15": "Alpha#15波幅",
    "wq_alpha19": "Alpha#19动量",
}

# 警报类型中文名
ALERT_TYPE_CN = {
    "triad_resonance": "三因子共振",
    "triple_confirm": "三重确认",
    "factor_mutation": "因子突变",
    "extreme_signal": "极端信号",
}


# ============================================================
# 频率控制
# ============================================================

def _load_state() -> dict:
    """从状态文件加载推送记录"""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"信号警报状态文件损坏，重置: {e}")
        return {}


def _save_state(state: dict) -> None:
    """保存推送记录到状态文件"""
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"保存信号警报状态失败: {e}")


def _can_push(code: str, alert_type: str, state: dict) -> bool:
    """
    检查同一标的同一类型警报是否在 REPEAT_HOURS 内已推送。
    返回 True 表示可以推送。
    """
    key = f"{code}_{alert_type}"
    entry = state.get(key)
    if not entry:
        return True
    last_push = entry.get("last_push")
    if not last_push:
        return True
    try:
        last_dt = datetime.fromisoformat(last_push)
        return datetime.now() - last_dt > timedelta(hours=REPEAT_HOURS)
    except (ValueError, TypeError):
        return True


def _mark_pushed(code: str, alert_type: str, state: dict) -> None:
    """标记推送时间"""
    key = f"{code}_{alert_type}"
    state[key] = {"last_push": datetime.now().isoformat()}


# ============================================================
# 因子缓存读取（复用 factor_monitor.py 的缓存文件）
# ============================================================

def _load_factor_cache() -> dict:
    """
    从 factor_monitor.py 的 ~/.serenity_factor_cache.json 加载前次因子快照。
    格式: { code: { factor_name: value, ... }, ... }
    """
    if not os.path.exists(FACTOR_CACHE_PATH):
        return {}
    try:
        with open(FACTOR_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


# ============================================================
# 获取当前因子
# ============================================================

_engine_instance = None


def _get_engine() -> AlphaFactorEngine:
    """获取或创建因子引擎单例"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AlphaFactorEngine()
    return _engine_instance


def _get_current_factors(code: str) -> dict:
    """获取某标的当前14因子信号 dict"""
    engine = _get_engine()
    try:
        all_factors = engine.compute_all_factors(code)
        return all_factors.get("signals", {})
    except Exception:
        return {}


# ============================================================
# 检测函数 — 条件 a: 三因子共振
# ============================================================

def _check_triad_resonance(code: str, name: str, signals: dict) -> Optional[dict]:
    """
    条件 a: 14因子中 3 个以上同时 ≥0.3 或 ≤-0.3
    """
    if not signals:
        return None

    high_factors = {}
    low_factors = {}

    for fname, val in signals.items():
        if val is None:
            continue
        if val >= RESONANCE_THRESHOLD:
            high_factors[fname] = val
        elif val <= -RESONANCE_THRESHOLD:
            low_factors[fname] = val

    if len(high_factors) >= RESONANCE_MIN_COUNT:
        direction = "超买看涨"
        icon = "🟢🟢🟢"
        matched = high_factors
        threshold_str = f">={RESONANCE_THRESHOLD}"
    elif len(low_factors) >= RESONANCE_MIN_COUNT:
        direction = "超卖看跌"
        icon = "🔴🔴🔴"
        matched = low_factors
        threshold_str = f"<=-{RESONANCE_THRESHOLD}"
    else:
        return None

    lines = [
        f"### {icon} {direction} — {name}({code})",
        "",
        f"**触发条件:** {len(matched)} 个因子 {threshold_str}",
        "",
        "**共振因子明细:**",
    ]
    for fname, val in matched.items():
        cn = FACTOR_CN.get(fname, fname)
        lines.append(f"- {cn}: {val:+.3f}")

    content = "\n".join(lines)

    return {
        "alert_type": "triad_resonance",
        "code": code,
        "name": name,
        "title": f"{icon} {name} 三因子共振 · {direction}",
        "content": content,
        "summary": f"{direction} | {len(matched)}因子{threshold_str}",
    }


# ============================================================
# 检测函数 — 条件 b: 三重确认
# ============================================================

def _check_triple_confirmation(code: str, name: str) -> Optional[dict]:
    """
    条件 b: 技术评分>70 + 基本面评分>0.3 + 因子综合>15
    """
    # 技术评分 (0-100)
    try:
        tech = compute_technical_factors(code)
        if not tech:
            return None
        technical_score = compute_trend_score(tech)
    except Exception as e:
        logger.debug(f"技术评分失败 {code}: {e}")
        return None

    # 基本面评分 (-1 to 1)
    try:
        fe = FundamentalEngine()
        fundamental_signal = fe.get_fundamental_signal(code)
    except Exception as e:
        logger.debug(f"基本面评分失败 {code}: {e}")
        fundamental_signal = None

    if fundamental_signal is None:
        return None

    # 因子综合评分 (0-100)
    try:
        all_results = score_all()
        factor_data = next((r for r in all_results if r["code"] == code), None)
    except Exception as e:
        logger.debug(f"综合评分失败 {code}: {e}")
        factor_data = None

    if factor_data is None:
        return None

    factor_score = factor_data.get("factor_score", 0)
    total_score = factor_data.get("total_score", 0)
    signal_action = factor_data.get("signal_action", "HOLD")

    if not (technical_score > TRIPLE_TECH_THRESHOLD and
            fundamental_signal > TRIPLE_FUND_THRESHOLD and
            factor_score > TRIPLE_FACTOR_THRESHOLD):
        return None

    content = (
        f"### 🎯 三重确认 — {name}({code})\n\n"
        f"**信号评级:** {signal_action} | **综合评分:** {total_score:.1f}\n\n"
        f"**三要素全部满足:**\n"
        f"- ✅ 技术评分 {technical_score:.0f} > {TRIPLE_TECH_THRESHOLD}\n"
        f"- ✅ 基本面评分 {fundamental_signal:+.3f} > {TRIPLE_FUND_THRESHOLD}\n"
        f"- ✅ 因子综合 {factor_score:.0f} > {TRIPLE_FACTOR_THRESHOLD}"
    )

    return {
        "alert_type": "triple_confirm",
        "code": code,
        "name": name,
        "title": f"🎯 {name} 三重确认买入信号",
        "content": content,
        "summary": f"技{technical_score:.0f}+基{fundamental_signal:+.2f}+因{factor_score:.0f}",
    }


# ============================================================
# 检测函数 — 条件 c: 因子突变
# ============================================================

def _check_factor_mutation(code: str, name: str) -> Optional[dict]:
    """
    条件 c: 单因子相比前日变化 >0.3
    使用 factor_monitor.py 的 ~/.serenity_factor_cache.json 缓存机制
    """
    cache = _load_factor_cache()
    old_factors = cache.get(code)
    if not old_factors:
        return None

    # 获取当前因子
    current_signals = _get_current_factors(code)
    if not current_signals:
        return None

    mutations = []
    for fname, new_val in current_signals.items():
        if new_val is None:
            continue
        old_val = old_factors.get(fname)
        if old_val is None:
            continue
        if not isinstance(old_val, (int, float)):
            continue
        delta = new_val - old_val
        if abs(delta) > MUTATION_THRESHOLD:
            cn = FACTOR_CN.get(fname, fname)
            direction = "🟢" if delta > 0 else "🔴"
            mutations.append(f"- {direction} **{cn}**: {old_val:+.3f} → {new_val:+.3f} (变化 {delta:+.3f})")

    if not mutations:
        return None

    lines = [
        f"### ⚡ 因子突变 — {name}({code})",
        "",
        f"**阈值:** 单因子变化 > {MUTATION_THRESHOLD}",
        "",
        "**突变因子:**",
    ]
    lines.extend(mutations)

    content = "\n".join(lines)

    return {
        "alert_type": "factor_mutation",
        "code": code,
        "name": name,
        "title": f"⚡ {name} 因子突变预警",
        "content": content,
        "summary": f"{len(mutations)} 个因子突变",
    }


# ============================================================
# 检测函数 — 条件 d: 极端信号
# ============================================================

def _check_extreme_signals(code: str, name: str, signals: dict) -> Optional[dict]:
    """
    条件 d: rsv_20≥0.80（超买）或 rsv_20≤0.20（超卖）且 macd_signal 同向
    注: signals 中 rsv_20 为 [0,1] 原始值, macd_signal 为 [-1,1]
    """
    rsv = signals.get("rsv_20")
    macd = signals.get("macd_signal")

    if rsv is None or macd is None:
        return None

    is_overbought = rsv >= RSV_OVERBOUGHT
    is_oversold = rsv <= RSV_OVERSOLD

    if not is_overbought and not is_oversold:
        return None

    # 同向判定: rsv 超买 + macd>0 (正向看涨), 或 rsv 超卖 + macd<0 (负向看跌)
    if is_overbought and macd > 0:
        direction = "🔴 超买极端"
        desc = (
            f"RSV({rsv:.0%}) 已进入超买区 (≥{RSV_OVERBOUGHT:.0%})，"
            f"同时 MACD({macd:+.3f}) 保持正向，均指向上方极端"
        )
        detail = f"RSV 超买 | MACD 正向确认"
    elif is_oversold and macd < 0:
        direction = "🟢 超卖极端"
        desc = (
            f"RSV({rsv:.0%}) 已进入超卖区 (≤{RSV_OVERSOLD:.0%})，"
            f"同时 MACD({macd:+.3f}) 保持负向，均指向下方极端"
        )
        detail = f"RSV 超卖 | MACD 负向确认"
    else:
        # RSV 极端但 MACD 不同向 → 不触发
        return None

    content = (
        f"### {direction} — {name}({code})\n\n"
        f"**{desc}**\n\n"
        f"**信号明细:**\n"
        f"- RSV(20日): {rsv:.1%} (区间 [0, 1])\n"
        f"- MACD信号: {macd:+.3f} (区间 [-1, 1])\n\n"
        f"**方向:** {detail}"
    )

    return {
        "alert_type": "extreme_signal",
        "code": code,
        "name": name,
        "title": f"{direction} · {name} 极端信号",
        "content": content,
        "summary": f"{detail}",
    }


# ============================================================
# 主入口
# ============================================================

def check_all_alerts() -> list[dict]:
    """
    对所有监控标的执行四种信号检测，符合条件的通过微信推送。

    Returns
    -------
    list[dict] — 已成功推送的警报列表（每个元素含 code/alert_type/title/content 等）
    """
    state = _load_state()
    pushed = []

    # 预获取所有标的的因子信号（避免重复计算）
    engine = _get_engine()
    all_factors_cache = {}

    for code in ALL_CODES:
        try:
            all_factors = engine.compute_all_factors(code)
            all_factors_cache[code] = all_factors.get("signals", {})
        except Exception:
            all_factors_cache[code] = {}
            continue

    for code in ALL_CODES:
        try:
            name = STOCK_MAP.get(code, {}).get("name", code)
            signals = all_factors_cache.get(code, {})

            checkers = [
                ("triad_resonance", _check_triad_resonance(code, name, signals)),
                ("triple_confirm", _check_triple_confirmation(code, name)),
                ("factor_mutation", _check_factor_mutation(code, name)),
                ("extreme_signal", _check_extreme_signals(code, name, signals)),
            ]

            for alert_type, alert_dict in checkers:
                if alert_dict is None:
                    continue
                if not _can_push(code, alert_type, state):
                    continue

                # 推送微信
                try:
                    from notifier import send_message
                    result = send_message(
                        alert_dict["title"],
                        alert_dict["content"],
                        content_type="markdown",
                        summary=alert_dict["summary"],
                    )
                    atype_label = ALERT_TYPE_CN.get(alert_type, alert_type)
                    logger.info(
                        "📤 信号推送 [%s(%s)] %s: %s",
                        name, code, atype_label, alert_dict["summary"],
                    )
                    _mark_pushed(code, alert_type, state)
                    alert_dict["pushed"] = True
                    pushed.append(alert_dict)
                except Exception as e:
                    logger.error(f"推送失败 [{name}({code})] {alert_type}: {e}")
                    alert_dict["pushed"] = False
                    alert_dict["error"] = str(e)
                    pushed.append(alert_dict)

        except Exception as e:
            logger.error(f"检查 {code} 信号时异常: {e}")
            continue

    # 保存状态
    _save_state(state)

    return pushed


# ============================================================
# CLI 入口
# ============================================================

def cmd_alerts_push() -> None:
    """CLI 命令: 主动信号推送检测"""
    print("🔍 运行信号主动推送检测...")
    print(f"  标的数: {len(ALL_CODES)} | 检测类型: 4 种")
    print(f"  频率控制: {REPEAT_HOURS}h | 状态文件: {STATE_PATH}")
    print()

    pushed = check_all_alerts()

    if not pushed:
        print("📭 无触发信号，无需推送")
        return

    print(f"📤 信号推送结果 ({len(pushed)} 条):")
    print("=" * 60)
    for p in pushed:
        status = "✅" if p.get("pushed") else "❌"
        err = f" | {p.get('error', '')}" if p.get("error") else ""
        atype = ALERT_TYPE_CN.get(p["alert_type"], p["alert_type"])
        print(f"  {status} [{atype}] {p['name']}({p['code']}): {p['summary']}{err}")
    print("=" * 60)


if __name__ == "__main__":
    cmd_alerts_push()
