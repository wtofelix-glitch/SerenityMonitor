#!/usr/bin/env python3
"""
AI 市场解读 — DeepSeek 生成自然语言收盘报告
喂入: 当日评分+信号+净值+新闻 → 输出: 2-3段中文解读
"""
import json, os, time
from datetime import date, datetime
from serenity_logger import get_logger

log = get_logger(__name__)

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


def gather_market_context() -> dict:
    """收集今日市场全貌"""
    from db import get_conn, get_latest_scores

    conn = get_conn()

    # 最新信号
    signals = conn.execute(
        "SELECT sl.code, s.name, sl.action, sl.total_score FROM signal_log sl "
        "LEFT JOIN stocks s ON s.code=sl.code "
        "WHERE sl.date=(SELECT MAX(date) FROM signal_log) ORDER BY sl.total_score DESC LIMIT 10"
    ).fetchall()

    # 净值
    nav = conn.execute("SELECT * FROM nav_history ORDER BY date DESC LIMIT 2").fetchall()

    # 持仓
    positions = conn.execute(
        "SELECT code, name, buy_price, score FROM stocks WHERE is_active=1 AND code!='CASH'"
    ).fetchall()

    # 闸门
    gate = conn.execute("SELECT * FROM auto_trade_gate ORDER BY date DESC LIMIT 1").fetchone()

    conn.close()

    return {
        "signals": [dict(r) for r in signals],
        "nav": [dict(r) for r in nav],
        "positions": [dict(r) for r in positions],
        "gate": dict(gate) if gate else {},
        "date": date.today().isoformat(),
    }


def build_prompt(ctx: dict) -> str:
    """构建发送给 DeepSeek 的 prompt"""
    signals = ctx["signals"]
    nav = ctx["nav"]
    positions = ctx["positions"]
    gate = ctx["gate"]

    top_buy = [s for s in signals if s["action"] in ("BUY", "STRONG_BUY", "CAUTION_BUY")][:3]
    top_sell = [s for s in signals if s["action"] in ("SELL", "STOP_LOSS")][:2]
    held = positions

    pnl = nav[0]["profit_pct"] if nav else 0
    total = nav[0]["total_value"] if nav else 0
    gate_state = gate.get("state", "?")

    prompt = f"""你是 Serenity 量化系统的 AI 分析师。请用中文写一段2-3段的今日市场解读(200-300字)。

今日数据:
- 日期: {ctx['date']}
- 组合净值: ¥{total:,.0f}, 累计盈亏: {pnl:+.2f}%
- 闸门状态: {gate_state}
- 持有: {', '.join(f"{h['name']}({h['code']})" for h in held) if held else '空仓'}
- 买入信号: {', '.join(f"{s['name']}({s['code']}) {s['action']} {s['total_score']:.0f}分" for s in top_buy) if top_buy else '无'}
- 卖出信号: {', '.join(f"{s['name']}({s['code']}) {s['action']}" for s in top_sell) if top_sell else '无'}

要求:
1. 第一段概述市场状态和组合表现
2. 第二段分析买卖信号背后的逻辑
3. 第三段给出操作建议和风险提示
4. 语气专业但易懂, 适合公众号读者

直接输出正文, 不需要标题。"""
    return prompt


def generate_commentary() -> str:
    """生成 AI 市场解读"""
    ctx = gather_market_context()

    if not ctx["signals"]:
        return "今日无交易信号, 市场平稳。"

    prompt = build_prompt(ctx)

    try:
        import urllib.request
        payload = json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.6,
        }, ensure_ascii=False).encode()

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("DeepSeek 解读生成失败, 使用回退: %s", e)
        return _fallback_commentary(ctx)


def _fallback_commentary(ctx: dict) -> str:
    """规则回退: 无需 API 的快速解读"""
    signals = ctx["signals"]
    nav = ctx["nav"]
    gate = ctx["gate"]
    pnl = nav[0]["profit_pct"] if nav else 0

    buy_count = sum(1 for s in signals if s["action"] in ("BUY", "STRONG_BUY", "CAUTION_BUY"))
    sell_count = sum(1 for s in signals if s["action"] in ("SELL", "STOP_LOSS"))

    mood = "积极" if pnl > 5 else ("稳健" if pnl > 0 else "谨慎")
    gate_label = "开放" if gate.get("state") == "SEMI_AUTO" else "锁定"

    return (
        f"今日市场整体{mood}，组合收益{pnl:+.2f}%。"
        f"系统生成{buy_count}个买入信号和{sell_count}个卖出信号。"
        f"自动交易闸门当前{gate_label}状态。"
        f"建议根据信号分布调整仓位，控制单只风险。"
    )


def push_to_wechat(commentary: str) -> bool:
    """推送到微信投递队列"""
    try:
        delivery = os.path.expanduser("~/.hermes/pending_deliveries/market_commentary.md")
        os.makedirs(os.path.dirname(delivery), exist_ok=True)
        header = f"# 📊 Serenity 市场解读\n*{date.today().isoformat()}*\n\n{commentary}"
        with open(delivery, "w") as f:
            f.write(header)
        log.info("市场解读已推送到微信队列")
        return True
    except Exception as e:
        log.warning("推送失败: %s", e)
        return False


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    print("🤖 生成 AI 市场解读...")
    commentary = generate_commentary()
    print(commentary)
    print(f"\n({len(commentary)} 字)")

    if "--push" in sys.argv:
        push_to_wechat(commentary)
