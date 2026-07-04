#!/usr/bin/env python3
"""
AI 信号解释引擎 — DeepSeek 生成中文自然语言信号解释
每次信号生成时调用，输出 3-5 行可理解的解释文本
"""
import json, os
from datetime import date
from serenity_logger import get_logger
log = get_logger(__name__)

# DeepSeek API 配置
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
EXPLAINER_MODEL = "deepseek-v4-flash"


def explain_signal(signal: dict) -> str:
    """
    为单个信号生成自然语言解释
    signal 包含: code, name, action, total_score, momentum_score, serenity_score,
                 factor_score, technical_score, zone_score, uzi_score 等维度
    返回: 中文解释文本(100-200字)
    """
    # 构建精简 prompt(减少 token 消耗)
    dims = []
    for k, v in signal.items():
        if "_score" in k and isinstance(v, (int, float)):
            dims.append(f"{k}: {v:.0f}")

    prompt = (
        f"信号: {signal.get('name','')}({signal.get('code','')}) {signal.get('action','')} "
        f"总分{signal.get('total_score',0):.0f}。维度: {', '.join(dims[:8])}。"
        f"用2-3句话解释这个信号的理由和主要风险。"
    )

    try:
        import urllib.request
        payload = json.dumps({
            "model": EXPLAINER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.3,
        }, ensure_ascii=False).encode()

        req = urllib.request.Request(DEEPSEEK_URL, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        # 回退: 基于规则的解释
        return _fallback_explain(signal)


def _fallback_explain(signal: dict) -> str:
    """基于规则的回退解释"""
    action = signal.get("action", "HOLD")
    name = signal.get("name", signal.get("code", "?"))
    score = signal.get("total_score", 50)
    momentum = signal.get("momentum_score", 50)
    factor = signal.get("factor_score", 50)

    reasons = []
    if momentum > 60:
        reasons.append("动量强劲(>" + str(int(momentum)) + "分)")
    elif momentum < 40:
        reasons.append("动量偏弱(" + str(int(momentum)) + "分)")

    if factor > 60:
        reasons.append("14因子信号偏多(>" + str(int(factor)) + "分)")
    elif factor < 40:
        reasons.append("因子信号偏空")

    if action in ("BUY", "STRONG_BUY"):
        return f"{name}: 建议买入。{'，'.join(reasons)}。综合评分{score:.0f}分，建议控制仓位。"
    elif action == "CAUTION_BUY":
        return f"{name}: 谨慎买入。{'，'.join(reasons)}。信号偏保守，建议小仓位试探。"
    elif action == "SELL":
        return f"{name}: 建议卖出。技术面转弱，评分{score:.0f}分，建议减仓或清仓。"
    else:
        return f"{name}: 持有观望。综合评分{score:.0f}分，无明显买卖信号。"


def explain_batch(signals: list[dict]) -> dict[str, str]:
    """批量解释多个信号(顺序调用, 避免 API 限流)"""
    results = {}
    for sig in signals:
        code = sig.get("code", "?")
        try:
            results[code] = explain_signal(sig)
        except Exception as e:
            results[code] = f"解释生成失败: {e}"
        import time
        time.sleep(0.5)  # API 限流保护
    return results


def store_explanations(explained: dict[str, str], signal_date: str = None):
    """将解释存储到 signal_log 的 details JSON"""
    from db import get_conn
    if signal_date is None:
        signal_date = date.today().isoformat()

    conn = get_conn()
    for code, explanation in explained.items():
        row = conn.execute(
            "SELECT details FROM signal_log WHERE code=? AND date=? ORDER BY id DESC LIMIT 1",
            (code, signal_date),
        ).fetchone()
        if row:
            details = {}
            try:
                details = json.loads(row["details"]) if row["details"] else {}
            except (json.JSONDecodeError, TypeError):
                details = {}
            details["ai_explanation"] = explanation
            conn.execute(
                "UPDATE signal_log SET details=? WHERE code=? AND date=?",
                (json.dumps(details, ensure_ascii=False), code, signal_date),
            )
    conn.commit()
    conn.close()
    log.info("已存储 %d 条AI解释", len(explained))


if __name__ == "__main__":
    # 测试: 解释今天的高分信号
    import sys
    sys.path.insert(0, ".")
    from db import get_conn

    conn = get_conn()
    rows = conn.execute(
        "SELECT sl.code, s.name, sl.action, sl.total_score FROM signal_log sl "
        "LEFT JOIN stocks s ON s.code=sl.code "
        "WHERE sl.date=(SELECT MAX(date) FROM signal_log) AND sl.action IN ('BUY','CAUTION_BUY','SELL') "
        "ORDER BY sl.total_score DESC LIMIT 3"
    ).fetchall()
    conn.close()

    sigs = [dict(r) for r in rows]
    explained = explain_batch(sigs)
    for code, text in explained.items():
        print(f"\n{code}: {text}")
