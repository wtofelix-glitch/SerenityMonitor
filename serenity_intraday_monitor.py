#!/usr/bin/env python3
"""Serenity 盘中监控脚本
每10分钟扫描持仓，仅异常时输出警报（含操作建议）

用法:
    python3 serenity_intraday_monitor.py

功能:
    1. 时间门控: 仅 A 股交易时间 9:30-15:00 + 工作日运行
    2. 持仓读取: 使用 portfolio.get_portfolio() 获取持仓（不再解析CLI文本）
    3. 信号检查: 使用 signal_engine.generate_signals() 获取信号
    4. 价格获取: 使用 data_engine.fetch_realtime() 获取实时价格
    5. 异常检测: SELL/STRONG_SELL信号、日内跌幅>5%、跌破止损线、信号降级
    6. 操作建议: 根据异常类型给出减仓/清仓/观望建议
    7. 输出规则: 有异常才输出, 无异常完全静默
"""

import sys
import json
import os
from datetime import datetime, time

# ── 使用 Python API 替代 CLI subprocess ──
# 所有数据通过模块内部 API 获取，不再解析 CLI 文本输出
from portfolio import get_portfolio, PortfolioManager
from signal_engine import generate_signals
from data_engine import fetch_realtime
from config import STOCK_MAP

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(WORK_DIR, ".intraday_state.json")

# 信号强度排序（索引越小信号越强）
SIGNAL_ORDER = [
    "STRONG_BUY",          # 0: 最强
    "BUY",                 # 1
    "CAUTION_BUY",         # 2
    "STRONG_HOLD",         # 3
    "CONSIDER_ADD",        # 4
    "HOLD",                # 5: 中性
    "WEAK_HOLD",           # 6
    "WATCH",               # 7
    "SELL",                # 8
    "STRONG_SELL",         # 9
    "STOP_LOSS",           # 10: 最差
]
SIGNAL_RANK = {s: i for i, s in enumerate(SIGNAL_ORDER)}

# 信号名称映射（generate_signals 可能返回简写或中文）
SIGNAL_NAME_ALIASES = {
    "strong_buy": "STRONG_BUY",
    "buy": "BUY",
    "caution_buy": "CAUTION_BUY",
    "strong_hold": "STRONG_HOLD",
    "consider_add": "CONSIDER_ADD",
    "hold": "HOLD",
    "weak_hold": "WEAK_HOLD",
    "watch": "WATCH",
    "sell": "SELL",
    "strong_sell": "STRONG_SELL",
    "stop_loss": "STOP_LOSS",
    "买入": "BUY",
    "强买入": "STRONG_BUY",
    "卖出": "SELL",
    "强卖出": "STRONG_SELL",
    "止损": "STOP_LOSS",
}


def normalize_signal(sig: str) -> str:
    """统一信号名称格式"""
    key = sig.strip().upper()
    if key in SIGNAL_RANK:
        return key
    lower = sig.strip().lower()
    if lower in SIGNAL_NAME_ALIASES:
        return SIGNAL_NAME_ALIASES[lower]
    return sig.strip()


def is_trading_time() -> bool:
    """时间门控：仅在 A 股交易时间 9:30-15:00 且工作日运行"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    market_open = time(9, 30)
    market_close = time(15, 0)
    return market_open <= now.time() <= market_close


def get_holdings() -> list[dict]:
    """通过 portfolio API 获取当前持仓列表"""
    pm = get_portfolio()
    pv = pm.get_portfolio_value()
    holdings = pv.get("holdings", [])
    return holdings


def get_realtime_prices() -> dict[str, dict]:
    """通过 data_engine API 获取实时行情"""
    data = fetch_realtime()
    price_map = {}
    for item in data:
        code = item.get("code", "")
        price_map[code] = {
            "price": item.get("price", 0.0),
            "close_yesterday": item.get("close_yesterday", 0.0),
            "change_pct": item.get("change_pct", 0.0),
        }
    return price_map


def get_signals() -> dict[str, str]:
    """通过 signal_engine API 获取所有信号"""
    signal_list = generate_signals()
    signal_map = {}
    for s in signal_list:
        code = s.get("code", "")
        action = s.get("action", "") or s.get("signal", "") or s.get("level", "")
        if code and action:
            signal_map[code] = normalize_signal(action)
    return signal_map


def load_previous_signals() -> dict:
    """读取上一次保存的信号状态"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_signals(signal_map: dict) -> None:
    """保存当前信号状态"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(signal_map, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def signal_is_downgrade(current: str, previous: str) -> bool:
    """判断信号是否从 BUY 级别降级"""
    if not previous or not current:
        return False
    cr = SIGNAL_RANK.get(current, 99)
    pr = SIGNAL_RANK.get(previous, 99)
    was_buy_level = pr <= SIGNAL_RANK.get("CAUTION_BUY", 2)
    is_worse = cr > pr
    return was_buy_level and is_worse


def generate_recommendation(alert: dict) -> str:
    """根据异常类型生成操作建议"""
    signal = alert["signal"]
    intraday_drop = alert["intraday_drop"]
    reasons = alert["reasons"]

    has_sell = signal in ("SELL", "STRONG_SELL", "STOP_LOSS")
    has_stop_breach = any("跌破止损" in r for r in reasons)
    has_downgrade = any("信号降级" in r for r in reasons)

    if has_stop_breach:
        return "已破止损线，建议立即清仓，保护本金"
    if has_sell:
        if signal in ("STRONG_SELL", "STOP_LOSS"):
            return "信号强卖出，建议减持半仓以上，剩余设紧止损"
        return "信号转卖出，建议减仓1/3，观察是否能企稳"
    if intraday_drop <= -8:
        return "日内暴跌超8%，若已接近止损线建议减仓，扛单风险大"
    if intraday_drop <= -5:
        if signal in ("HOLD", "WEAK_HOLD", "WATCH"):
            return f"日内大跌{intraday_drop:.0f}%+弱信号，建议减仓避险"
        return f"日内大跌{intraday_drop:.0f}%但信号尚可，观望等反弹，不急于割肉"
    if has_downgrade:
        return "信号降级，建议降低预期，收紧止损观望"
    return "异常信号出现，建议密切关注，暂时不动"


def main():
    # ── 1. 时间门控 ──
    if not is_trading_time():
        sys.exit(0)

    # ── 2. 通过 API 获取持仓 ──
    holdings = get_holdings()
    if not holdings:
        sys.exit(0)

    # ── 3. 通过 API 获取实时行情和信号 ──
    price_map = get_realtime_prices()
    signal_map = get_signals()
    if not signal_map and not price_map:
        # 都拿不到数据 -> 静默退出（可能API超时）
        sys.exit(0)

    prev_signals = load_previous_signals()
    current_signals: dict[str, str] = {}
    alerts: list[dict] = []

    for holding in holdings:
        code = holding.get("code", "")
        name = holding.get("name", "") or STOCK_MAP.get(code, code)
        buy_price = holding.get("buy_price", 0.0) or holding.get("price", 0.0)
        if not code:
            continue

        # 实时价格
        rt = price_map.get(code, {})
        price = rt.get("price", 0.0)
        close_yesterday = rt.get("close_yesterday", 0.0)
        if price <= 0:
            continue

        # 当前信号
        current_action = signal_map.get(code)
        if not current_action:
            continue
        current_signals[code] = current_action
        prev_action = prev_signals.get(code)

        # 计算关键指标
        stop_loss = round(buy_price * 0.88, 2)
        intraday_drop = (
            round(((price - close_yesterday) / close_yesterday) * 100, 2)
            if close_yesterday > 0
            else 0.0
        )
        dist_to_stop = (
            round(((price - stop_loss) / stop_loss) * 100, 2) if stop_loss > 0 else 0.0
        )

        reasons = []

        # 4a. 信号变为 SELL / STRONG_SELL
        if current_action in ("SELL", "STRONG_SELL", "STOP_LOSS"):
            reasons.append(f"信号: {current_action}")

        # 4b. 单日跌幅 > 5%
        if close_yesterday > 0 and intraday_drop <= -5.0:
            reasons.append(f"日内跌幅 {intraday_drop:.1f}%")

        # 4c. 跌破止损线
        if price < stop_loss:
            reasons.append(f"跌破止损线 {stop_loss:.2f}")

        # 4d. 信号降级
        if prev_action and signal_is_downgrade(current_action, prev_action):
            reasons.append(f"信号降级: {prev_action} → {current_action}")

        # 5. 有异常时记录
        if reasons:
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "price": price,
                    "close_yesterday": close_yesterday,
                    "intraday_drop": intraday_drop,
                    "signal": current_action,
                    "prev_signal": prev_action or "",
                    "stop_loss": stop_loss,
                    "dist_to_stop": dist_to_stop,
                    "reasons": reasons,
                }
            )

    # 保存当前信号供下次比较
    save_signals(current_signals)

    # 无异常：完全静默
    if not alerts:
        sys.exit(0)

    # 格式化的警报输出（含操作建议）
    output_parts = []
    for a in alerts:
        block = f"⚠️ {a['name']} ({a['code']}) 异常警报"
        block += (
            f"\n  现价: {a['price']:.2f} | "
            f"昨日收盘: {a['close_yesterday']:.2f} | "
            f"日内跌幅: {a['intraday_drop']:.1f}%"
        )
        if a.get("prev_signal"):
            block += f"\n  信号: {a['signal']}（前日: {a['prev_signal']}）"
        else:
            block += f"\n  信号: {a['signal']}"
        block += f"\n  止损线: {a['stop_loss']:.2f} | " f"距止损: {a['dist_to_stop']:+.1f}%"
        block += f"\n  💡 操作建议：{generate_recommendation(a)}"
        output_parts.append(block)

    output = "\n\n".join(output_parts)
    if len(output) > 2000:
        output = output[:1997] + "..."

    print(output)


if __name__ == "__main__":
    main()
