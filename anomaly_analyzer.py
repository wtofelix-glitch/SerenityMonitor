"""
异动自动解读模块 — 价格异动 / 信号突变 / 因子突变检测

检测条件:
1. 价格异动：最近一日 change_pct > 5%
2. 信号突变：连续两天 signal_log action 跨级别变化（相差 ≥2 级）
3. 因子突变：alpha_score / tech_score 相比前次偏移 > 20

频率控制：同一标的同一触发类型 4 小时内不重复
（使用 ~/.anomaly_state.json 记录）

Usage:
    from anomaly_analyzer import run_anomaly_check
    result = run_anomaly_check()  # → list of anomaly dicts

CLI:
    python3 cli.py check-anomalies
"""

import json
import os
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import requests

from config import STOCK_MAP, ALL_CODES, STOCK_DETAILS
from db import (
    get_price_history,
    get_recent_signals,
    add_anomaly,
)

logger = logging.getLogger("Serenity.AnomalyAnalyzer")

# ============================================================
# 路径配置
# ============================================================

STATE_PATH = os.path.expanduser("~/.anomaly_state.json")
REPEAT_HOURS = 4  # 4 小时内不重复推送

# ============================================================
# 信号等级系统（用于检测跨级别变化）
# ============================================================

ACTION_LEVEL = {
    "STRONG_BUY": 8,
    "BUY": 7,
    "CAUTION_BUY": 6,
    "HOLD": 5,
    "WATCH": 4,
    "SELL": 3,
    "STRONG_SELL": 2,
    "STOP_LOSS": 1,
}

SELL_ACTIONS = {"SELL", "STRONG_SELL", "STOP_LOSS"}
BUY_ACTIONS = {"STRONG_BUY", "BUY", "CAUTION_BUY"}

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
        logger.warning(f"异动状态文件损坏，重置: {e}")
        return {}


def _save_state(state: dict) -> None:
    """保存推送记录到状态文件"""
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"保存异动状态失败: {e}")


def _can_push(code: str, trigger_type: str, state: dict) -> bool:
    """
    检查同一标的同一触发类型是否在 REPEAT_HOURS 内已推送。
    返回 True 表示可以推送。
    """
    key = f"{code}_{trigger_type}"
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


def _mark_pushed(code: str, trigger_type: str, state: dict) -> None:
    """标记推送时间"""
    key = f"{code}_{trigger_type}"
    state[key] = {"last_push": datetime.now().isoformat()}


# ============================================================
# 检测函数 — 条件 1: 价格异动
# ============================================================


def _check_price_anomaly(code: str) -> Optional[dict]:
    """
    检查价格异动：最近一日 change_pct > 5%
    """
    history = get_price_history(code, days=3)
    if not history:
        return None

    latest = history[0]
    change_pct = latest.get("change_pct")
    if change_pct is None:
        return None

    if abs(change_pct) <= 5:
        return None

    direction = "大涨" if change_pct > 0 else "大跌"
    return {
        "code": code,
        "trigger_type": "price_anomaly",
        "level": "A" if abs(change_pct) > 9 else "B",
        "price": latest.get("close", 0),
        "change_pct": round(change_pct, 2),
        "data": {
            "change_pct": change_pct,
            "close": latest.get("close"),
            "date": latest.get("date"),
            "direction": direction,
        },
    }


# ============================================================
# 检测函数 — 条件 2: 信号突变
# ============================================================


def _check_signal_mutation(code: str) -> Optional[dict]:
    """
    检查信号突变：连续两天 signal_log action 跨级别变化（相差 ≥2 级）
    """
    signals = get_recent_signals(code=code, days=2, limit=10)
    if len(signals) < 2:
        return None

    # 去重取最近两天不同日期的信号
    seen_dates = set()
    unique_signals = []
    for s in signals:
        d = s.get("date", "")
        if d not in seen_dates:
            seen_dates.add(d)
            unique_signals.append(s)
        if len(unique_signals) >= 2:
            break

    if len(unique_signals) < 2:
        return None

    latest = unique_signals[0]
    prev = unique_signals[1]

    action_now = latest.get("action", "")
    action_prev = prev.get("action", "")

    level_now = ACTION_LEVEL.get(action_now)
    level_prev = ACTION_LEVEL.get(action_prev)

    if level_now is None or level_prev is None:
        return None

    level_diff = level_now - level_prev
    if abs(level_diff) < 2:
        return None

    direction = "转强" if level_diff > 0 else "转弱"

    return {
        "code": code,
        "trigger_type": "signal_mutation",
        "level": "A",
        "price": latest.get("price", 0),
        "change_pct": 0,
        "data": {
            "action_prev": action_prev,
            "action_now": action_now,
            "level_prev": level_prev,
            "level_now": level_now,
            "level_diff": level_diff,
            "direction": direction,
            "date_prev": prev.get("date"),
            "date_now": latest.get("date"),
        },
    }


# ============================================================
# 检测函数 — 条件 3: 因子突变
# ============================================================


def _check_factor_mutation(code: str) -> Optional[dict]:
    """
    检查因子突变：alpha_score / tech_score 偏移 > 20
    """
    signals = get_recent_signals(code=code, days=2, limit=10)
    if len(signals) < 2:
        return None

    seen_dates = set()
    unique_signals = []
    for s in signals:
        d = s.get("date", "")
        if d not in seen_dates:
            seen_dates.add(d)
            unique_signals.append(s)
        if len(unique_signals) >= 2:
            break

    if len(unique_signals) < 2:
        return None

    latest = unique_signals[0]
    prev = unique_signals[1]

    alpha_now = latest.get("alpha_score") or 0
    alpha_prev = prev.get("alpha_score") or 0
    tech_now = latest.get("tech_score") or 0
    tech_prev = prev.get("tech_score") or 0

    alpha_delta = alpha_now - alpha_prev
    tech_delta = tech_now - tech_prev

    if abs(alpha_delta) <= 20 and abs(tech_delta) <= 20:
        return None

    mutations = []
    if abs(alpha_delta) > 20:
        mutations.append({
            "factor": "alpha_score",
            "prev": round(alpha_prev, 1),
            "now": round(alpha_now, 1),
            "delta": round(alpha_delta, 1),
        })
    if abs(tech_delta) > 20:
        mutations.append({
            "factor": "tech_score",
            "prev": round(tech_prev, 1),
            "now": round(tech_now, 1),
            "delta": round(tech_delta, 1),
        })

    return {
        "code": code,
        "trigger_type": "factor_mutation",
        "level": "B" if all(abs(m["delta"]) <= 30 for m in mutations) else "A",
        "price": latest.get("price", 0),
        "change_pct": 0,
        "data": {
            "mutations": mutations,
            "date_prev": prev.get("date"),
            "date_now": latest.get("date"),
        },
    }


# ============================================================
# 新闻搜索（降级友好）
# ============================================================


def _fetch_news_headline(name: str) -> str:
    """
    搜索股票相关新闻头条。
    降级策略：失败时只返回因子归因，不影响整体流程。
    """
    try:
        url = f"https://www.baidu.com/s?wd={name}+公告&tn=news"
        resp = requests.get(url, timeout=5, headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        resp.encoding = "utf-8"
        html = resp.text

        # 尝试提取新闻标题
        # 常用新闻标题模式
        patterns = [
            r'<h3[^>]*>.*?<a[^>]*>(.*?)</a>.*?</h3>',
            r'class="c-title"[^>]*>.*?<a[^>]*>(.*?)</a>',
            r'<a[^>]*href="https?://[^"]*"[^>]*>(.*?)</a>',
        ]
        for pat in patterns:
            match = re.search(pat, html, re.DOTALL)
            if match:
                title = re.sub(r'<[^>]+>', '', match.group(1)).strip()
                title = re.sub(r'\s+', ' ', title)
                if len(title) > 5:
                    return title[:60]

        # 尝试找任何标题类内容
        titles = re.findall(r'<h[23][^>]*>(.*?)</h[23]>', html, re.DOTALL)
        for t in titles:
            cleaned = re.sub(r'<[^>]+>', '', t).strip()
            cleaned = re.sub(r'\s+', ' ', cleaned)
            if len(cleaned) > 5:
                return cleaned[:60]

        return ""

    except Exception as e:
        logger.debug(f"新闻搜索失败 [{name}]: {e}")
        return ""


# ============================================================
# 解读生成
# ============================================================


def _build_price_change_desc(data: dict) -> str:
    """构建价格变化描述"""
    change_pct = data.get("change_pct", 0)
    if change_pct == 0:
        return ""
    direction = "大涨" if change_pct > 0 else "大跌"
    return f"{direction} {abs(change_pct):.1f}%"


def _build_signal_change_desc(data: dict) -> str:
    """构建信号变化描述"""
    action_prev = data.get("action_prev", "")
    action_now = data.get("action_now", "")
    direction = data.get("direction", "")
    if not action_prev or not action_now:
        return ""
    return f"信号由{action_prev}→{action_now}（{direction}）"


def _build_factor_desc(data: dict) -> str:
    """构建因子归因描述"""
    mutations = data.get("mutations", [])
    if not mutations:
        return ""
    parts = []
    for m in mutations:
        cn = "Alpha因子" if m["factor"] == "alpha_score" else "技术因子"
        direction = "↑" if m["delta"] > 0 else "↓"
        parts.append(f"{cn}{direction}{abs(m['delta']):.0f}（{m['prev']}→{m['now']}）")
    return "因子异动：" + "；".join(parts)


def explain_anomaly(code: str, trigger_type: str, data: dict) -> str:
    """
    生成 150-200 字异动解读。
    包含：价格变化% + 信号变化 + 因子归因 + 新闻头条
    """
    name = STOCK_MAP.get(code, {}).get("name", code)
    now = datetime.now().strftime("%m/%d")

    # 1. 价格变化
    price_desc = _build_price_change_desc(data)
    if not price_desc and data.get("change_pct"):
        price_desc = f"{'大涨' if data['change_pct'] > 0 else '大跌'} {abs(data['change_pct']):.1f}%"

    # 2. 信号变化
    signal_desc = _build_signal_change_desc(data)

    # 3. 因子归因
    factor_desc = _build_factor_desc(data)

    # 4. 新闻头条
    headline = _fetch_news_headline(name)

    # 构建解读
    trigger_labels = {
        "price_anomaly": "价格异动",
        "signal_mutation": "信号突变",
        "factor_mutation": "因子突变",
    }
    trigger_label = trigger_labels.get(trigger_type, trigger_type)

    parts = [f"【{now}异动解读·{trigger_label}】{name}({code})"]

    if trigger_type == "price_anomaly":
        close = data.get("close", "N/A")
        tag = STOCK_DETAILS.get(code, {}).get("serenity_tag", "")
        reason = STOCK_DETAILS.get(code, {}).get("reason", "")
        brief_reason = reason[:40] if reason else f"Serenity{tag}方向标的" if tag else "A股标的"
        parts.append(
            f"日内{price_desc}，收盘{close}元。"
            f"该标的为{brief_reason}，"
            f"短期波动需结合盘口量价及行业催化判断。"
        )

    elif trigger_type == "signal_mutation":
        direction = data.get("direction", "")
        action_prev = data.get("action_prev", "?")
        action_now = data.get("action_now", "?")
        diff = abs(data.get("level_diff", 0))
        action_cn = {"STRONG_BUY": "强力买入", "BUY": "买入", "CAUTION_BUY": "谨慎买入",
                     "HOLD": "持有", "WATCH": "观察", "SELL": "卖出", "STRONG_SELL": "强力卖出",
                     "STOP_LOSS": "止损"}
        prev_cn = action_cn.get(action_prev, action_prev)
        now_cn = action_cn.get(action_now, action_now)
        parts.append(
            f"信号跨级别{direction}，"
            f"由{prev_cn}({action_prev})跳变至{now_cn}({action_now})，"
            f"等级差{diff}级，暗示模型对该标的后市判断发生重大转向。"
        )
        if price_desc:
            parts.append(f"盘面{price_desc}，印证了信号转向方向。若量价配合，趋势可能延续。")
        else:
            parts.append(f"建议结合RSI、MACD等技术面指标验证有效性。")

    elif trigger_type == "factor_mutation":
        parts.append(f"评分因子出现显著偏移")
        if factor_desc:
            parts.append(factor_desc)
        if price_desc:
            parts.append(f"同期{price_desc}，量价配合需关注。")
        else:
            parts.append(f"因子突变可能预示模型对当前行情的重新定价。")

    # 信号变化补充
    if trigger_type != "signal_mutation" and signal_desc:
        parts.append(f"信号层面{signal_desc}。")

    # 因子归因补充
    if trigger_type != "factor_mutation" and factor_desc:
        parts.append(factor_desc.replace("因子异动：", ""))

    # 新闻
    if headline:
        parts.append(f"相关新闻：{headline}")

    full_text = "；".join(parts)

    # 补齐到 150-200 字
    if len(full_text) < 150:
        extra = ""
        if trigger_type == "price_anomaly":
            direction = data.get("direction", "")
            extra = (
                f"建议关注量能持续性及板块联动效应，"
                f"{'不宜盲目追高' if direction == '大涨' else '评估止损或补仓机会'}。"
            )
        elif trigger_type == "signal_mutation":
            direction = data.get("direction", "")
            extra = (
                f"策略上建议{'确认卖出信号、控制回撤' if direction == '转弱' else '等待买入确认信号、择机建仓'}，"
                f"设置合理止损位。"
            )
        elif trigger_type == "factor_mutation":
            extra = (
                f"建议重新审视评分权重配置，"
                f"结合基本面变化判断异动是趋势性还是噪声。"
            )
        if extra:
            full_text += "；" + extra

    if len(full_text) > 200:
        full_text = full_text[:197] + "..."

    return full_text


# ============================================================
# 主入口：检查所有异动
# ============================================================


def check_anomalies() -> list[dict]:
    """
    对所有标的执行三种异动检测。

    Returns
    -------
    list[dict] — 触发的异动列表，每个元素含:
        - code, trigger_type, level, price, change_pct, data
    """
    anomalies = []
    for code in ALL_CODES:
        try:
            # 条件 1: 价格异动
            result = _check_price_anomaly(code)
            if result:
                anomalies.append(result)

            # 条件 2: 信号突变
            result = _check_signal_mutation(code)
            if result:
                anomalies.append(result)

            # 条件 3: 因子突变
            result = _check_factor_mutation(code)
            if result:
                anomalies.append(result)

        except Exception as e:
            logger.error(f"检查异动 {code} 时异常: {e}")
            continue

    return anomalies


# ============================================================
# 全流程
# ============================================================


def run_anomaly_check() -> list[dict]:
    """
    全流程：检查异动 → 生成解读 → 推送微信 → 记录数据库
    同时执行频率控制。

    Returns
    -------
    list[dict] — 已处理的异动列表
    """
    state = _load_state()
    all_anomalies = check_anomalies()
    processed = []

    for anomaly in all_anomalies:
        code = anomaly["code"]
        trigger_type = anomaly["trigger_type"]

        # 频率控制
        if not _can_push(code, trigger_type, state):
            continue

        name = STOCK_MAP.get(code, {}).get("name", code)
        data = anomaly["data"]

        # 生成解读
        explanation = explain_anomaly(code, trigger_type, data)
        anomaly["explanation"] = explanation

        # 记录到数据库
        try:
            add_anomaly(
                code=code,
                level=anomaly["level"],
                alert_type=anomaly["trigger_type"],
                price=anomaly["price"],
                message=explanation,
                data=data,
            )
        except Exception as e:
            logger.error(f"记录异动到数据库失败 [{name}({code})]: {e}")

        # 推送微信
        try:
            from notifier import send_message
            title = f"⚠️ 异动预警 · {name}({code})"
            trigger_labels = {
                "price_anomaly": "价格异动",
                "signal_mutation": "信号突变",
                "factor_mutation": "因子突变",
            }
            tl = trigger_labels.get(trigger_type, trigger_type)
            send_message(
                title,
                explanation,
                content_type="text",
                summary=f"[{tl}] {name}",
            )
            logger.info("📤 异动推送 [%s(%s)] %s", name, code, trigger_type)
            anomaly["pushed"] = True
        except Exception as e:
            logger.error(f"异动推送失败 [{name}({code})]: {e}")
            anomaly["pushed"] = False

        # 标记推送时间
        _mark_pushed(code, trigger_type, state)
        processed.append(anomaly)

    # 保存状态
    _save_state(state)

    return processed


# ============================================================
# CLI 入口
# ============================================================


def cmd_check_anomalies() -> None:
    """
    CLI 命令：python3 cli.py check-anomalies
    运行全流程异动检查并输出结果。
    """
    import sys

    print("🔍 异动自动解读扫描中...\n")

    results = run_anomaly_check()

    if not results:
        print("📭 无异常触发，一切正常")
        return

    print(f"⚠️ 发现 {len(results)} 条异动:\n")
    print("=" * 60)

    trigger_labels = {
        "price_anomaly": "价格异动",
        "signal_mutation": "信号突变",
        "factor_mutation": "因子突变",
    }

    for r in results:
        name = STOCK_MAP.get(r["code"], {}).get("name", r["code"])
        tl = trigger_labels.get(r["trigger_type"], r["trigger_type"])
        status = "✅" if r.get("pushed") else "❌"
        level_icon = {"A": "🔴", "B": "🟡", "C": "🟢"}.get(r["level"], "⚪")

        print(f"\n{status} {level_icon} [{tl}] {name}({r['code']})")
        print(f"   等级: {r['level']} | 价格: {r['price']}")
        if r.get("change_pct"):
            print(f"   涨跌幅: {r['change_pct']:+.2f}%")
        print(f"   解读: {r.get('explanation', '')}")

    print("\n" + "=" * 60)
    print(f"共 {len(results)} 条异动")


# ============================================================
# 直接运行
# ============================================================

if __name__ == "__main__":
    cmd_check_anomalies()
