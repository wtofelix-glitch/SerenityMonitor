"""
AI因子解读 — 将14因子信号矩阵转为大白话中文解读
嵌入 daily_report.py 的收盘简报中，每只持仓标的 1-2 句解读
"""

import json
import os
import logging

logger = logging.getLogger("Serenity.FactorInterpreter")

# DeepSeek API 端点
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# 因子名称解释对照（用于降级模板）
FACTOR_NAMES_CN = {
    "ksft": "趋势强度",
    "rank_20": "20日排名",
    "rsv_20": "RSV随机值",
    "beta_20": "20日Beta",
    "resi_20": "20日残差",
    "macd_signal": "MACD趋势",
    "obv_trend": "OBV量能",
    "mfi_signal": "MFI资金流",
    "cci_signal": "CCI超买超卖",
    "wq_alpha1": "Alpha#1反转",
    "wq_alpha3": "Alpha#3动量",
    "wq_alpha5": "Alpha#5趋势",
    "wq_alpha15": "Alpha#15波动",
    "wq_alpha19": "Alpha#19综合",
}

FACTOR_EMOJIS = {
    "ksft": "📈", "rank_20": "🏅", "rsv_20": "📊", "beta_20": "⚡",
    "resi_20": "🔍", "macd_signal": "💹", "obv_trend": "📦",
    "mfi_signal": "💰", "cci_signal": "🌡️", "wq_alpha1": "🔄",
    "wq_alpha3": "🚀", "wq_alpha5": "🧭", "wq_alpha15": "🌊",
    "wq_alpha19": "🎯",
}


def _get_api_key():
    """获取 DeepSeek API key（环境变量）"""
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _build_factor_prompt(stock_name: str, code: str, factors: dict,
                         total_signal: float, change_pct: float) -> str:
    """构建单只标的的 LLM 解读 prompt"""
    # 找出前3正向因子和前3负向因子
    sorted_factors = sorted(
        [(k, v) for k, v in factors.items() if isinstance(v, (int, float)) and abs(v) > 0.05],
        key=lambda x: -abs(x[1])
    )
    top_pos = [f"{FACTOR_NAMES_CN.get(k, k)}={v:+.2f}" for k, v in sorted_factors if v > 0][:3]
    top_neg = [f"{FACTOR_NAMES_CN.get(k, k)}={v:+.2f}" for k, v in sorted_factors if v < 0][:3]

    return (
        f"{stock_name}({code})：今日涨跌{change_pct:+.2f}%，"
        f"综合信号{total_signal:+.3f}。"
        f"正向因子：{', '.join(top_pos) if top_pos else '无'}"
        f" | 负向因子：{', '.join(top_neg) if top_neg else '无'}。"
        f"用1句话做交易解读（仓位建议/风险提示/关键观察），避免术语堆砌。"
    )


def _call_deepseek(prompt: str) -> str:
    """调用 DeepSeek API 生成解读"""
    api_key = _get_api_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY 未设置")

    import urllib.request
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "你是A股量化分析助手。用大白话解读因子信号，每只股票只说1-2句话，关注仓位建议和风险提示。不说'根据数据'等废话。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 200,
    }).encode("utf-8")

    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"DeepSeek API 调用失败: {e}")
        raise


def _template_interp(stock_name: str, code: str, factors: dict,
                     total_signal: float, change_pct: float) -> str:
    """降级模板解读（API不可用时使用）"""
    parts = []

    # 综合信号判断
    if total_signal > 0.3:
        parts.append(f"{stock_name}因子信号偏多({total_signal:+.2f})")
    elif total_signal < -0.3:
        parts.append(f"{stock_name}因子信号偏空({total_signal:+.2f})")
    else:
        parts.append(f"{stock_name}因子信号中性({total_signal:+.2f})")

    # 趋势方向
    trend = factors.get("macd_signal", 0)
    if trend > 0.3:
        parts.append("MACD指向向上")
    elif trend < -0.3:
        parts.append("MACD指向向下")

    # 动量
    mom = factors.get("wq_alpha3", 0)
    if mom > 0.3:
        parts.append(f"动量因子偏强(+{mom:.2f})")
    elif mom < -0.3:
        parts.append(f"动量偏弱({mom:.2f})")

    # MFI资金流
    mfi = factors.get("mfi_signal", 0)
    if abs(mfi) > 0.3:
        if mfi > 0:
            parts.append("资金流入")
        else:
            parts.append("资金流出")

    # 今日涨跌联动
    if abs(change_pct) > 3:
        if change_pct > 0:
            parts.append(f"今日大涨{change_pct:+.2f}%")
        else:
            parts.append(f"今日大跌{change_pct:+.2f}%")

    if not parts:
        return f"{stock_name}信号平稳，暂无突出方向。"

    return "，".join(parts) + "。"


def interpret_stock(stock_name: str, code: str, factors: dict,
                    total_signal: float, change_pct: float) -> str:
    """
    解读单只标的的因子信号
    先尝试 DeepSeek API，失败则降级到模板
    """
    try:
        prompt = _build_factor_prompt(stock_name, code, factors, total_signal, change_pct)
        return _call_deepseek(prompt)
    except Exception:
        logger.info(f"AI解读降级到模板: {stock_name}")
        return _template_interp(stock_name, code, factors, total_signal, change_pct)


def interpret_all(held_factors: list, other_factors: list = None) -> str:
    """
    解读所有标的的因子信号
    held_factors: [{name, code, factors:{}, signal, change_pct}, ...]
    返回多行中文解读
    """
    lines = []

    if held_factors:
        lines.append("**持仓标的**")
        for item in held_factors:
            interp = interpret_stock(
                item["name"], item["code"],
                item.get("factors", {}),
                item.get("signal", 0),
                item.get("change_pct", 0)
            )
            lines.append(f"  - {interp}")
        lines.append("")

    if other_factors:
        lines.append("**候选关注**")
        for item in other_factors[:3]:  # 最多3只候选
            interp = interpret_stock(
                item["name"], item["code"],
                item.get("factors", {}),
                item.get("signal", 0),
                item.get("change_pct", 0)
            )
            lines.append(f"  - {interp}")

    return "\n".join(lines)


def cmd_factor_interpret():
    """CLI 命令：生成因子解读"""
    from factor_engine import get_current_signals
    from db import load_all_stocks
    from data_engine import get_all_today_snapshots

    factor_results = get_current_signals()
    stocks = load_all_stocks()
    active = {s["code"] for s in stocks if s["is_active"]}
    snapshots = {s["code"]: s for s in get_all_today_snapshots()}

    held = []
    other = []

    for r in factor_results:
        code = r["code"]
        snap = snapshots.get(code, {})
        item = {
            "name": r["name"],
            "code": code,
            "factors": r.get("factors", {}),
            "signal": r.get("signal", 0),
            "change_pct": snap.get("change_pct", 0),
        }
        if code in active:
            held.append(item)
        else:
            other.append(item)

    result = interpret_all(held, other)
    print(result)
    return result
