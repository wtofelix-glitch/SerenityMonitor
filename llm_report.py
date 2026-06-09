"""
llm_report.py — Serenity 文字研报生成器

借鉴 QuantDinger 的 Hybrid 架构（客观评分 + LLM 叙事 + 共识裁决）
和 TradingAgents 的共享 State 模式。

输入: scorer.py 的 score_all() 返回的 result 列表（14只，9维评分）
输出: Markdown 格式文字研报，附带多维度总结和操作建议

使用方法:
    from llm_report import generate_llm_report
    report = generate_llm_report(scores)
    
或通过 Hermes cron 任务自动调用。
"""

from datetime import date
from typing import Any


def _build_prompt(ranked_scores: list[dict], all_scores: list[dict]) -> str:
    """构建注入评分数据的 prompt 模板。
    
    复用 QuantDinger 的风格：强约束 system prompt + 格式化数据输入。
    不需要外部 API key，直接用 Hermes 自身能力生成文字研报。
    """
    today = date.today().isoformat()
    
    # ====== 顶部分4只展示 ======
    top_lines = []
    for r in ranked_scores[:4]:
        rank_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r["rank"], f"{r['rank']}.")
        moat_str = f"护{r.get('moat_score', 50):.0f}" if r.get("moat_score") else ""
        top_lines.append(
            f"{rank_emoji} {r['name']}({r.get('code','?')}) "
            f"综合{r['total_score']:.0f} | "
            f"基{r.get('base_score',0):.0f} 动{r.get('momentum_score',0):.0f} "
            f"量{r.get('volume_score',0):.0f} 技{r.get('technical_score',0):.0f} "
            f"情{r.get('sentiment_score',0):.0f} "
            f"{moat_str} | {r['zone_label']}"
        )
    
    top_text = "\n".join(top_lines)
    
    # ====== 全量评分矩阵 ======
    header = f"{'名称':<8} {'综合':>4} {'区':>3} {'基':>3} {'动':>3} {'量':>3} {'技':>3} {'情':>3} {'护':>3}"
    rows = []
    for r in all_scores:
        rows.append(
            f"{r['name']:<8} {r['total_score']:>4.0f} "
            f"{r.get('serenity_score',0):>3.0f} {r.get('base_score',0):>3.0f} "
            f"{r.get('momentum_score',0):>3.0f} {r.get('volume_score',0):>3.0f} "
            f"{r.get('technical_score',0):>3.0f} {r.get('sentiment_score',0):>3.0f} "
            f"{r.get('moat_score',50):>3.0f}"
        )
    
    matrix = "\n".join([header] + rows)
    
    # ====== 信号摘要 ======
    buy_signals = [r for r in all_scores if r.get("signal_action") == "BUY"]
    sell_signals = [r for r in all_scores if r.get("signal_action") == "SELL"]
    hold_signals = [r for r in all_scores if r.get("signal_action") in ("HOLD", "CAUTION")]
    
    # ====== 护城河分析 ======
    moat_items = []
    for r in sorted(all_scores, key=lambda x: x.get("moat_score", 50), reverse=True)[:5]:
        moat_items.append(f"  {r['name']} 护{r.get('moat_score', 50):.0f}")
    moat_summary = "\n".join(moat_items)
    
    prompt = f"""今天 {today} Serenity Monitor AI 投研日报。

## 今日评分排名 TOP4
{top_text}

## 全量评分矩阵
{matrix}

## 信号分布
买入信号: {len(buy_signals)} 只
卖出信号: {len(sell_signals)} 只
持有观察: {len(hold_signals)} 只

## 护城河 TOP5
{moat_summary}

请根据以上数据生成一份专业的文字研报，包含以下维度：
1. 今日大盘点评（基于评分分布推断市场状态）
2. 评分排名解读（为什么前几名脱颖而出）
3. 信号分布分析（买入/卖出信号解读市场情绪）
4. 护城河点评（谁在构筑真正的竞争优势）
5. 操作参考（综合评分和信号给出的建议方向）

要求：
- 简洁专业，每点1-2句话
- 有判断有依据，不模棱两可
- 使用简体中文
- 总字数控制在 300 字以内
- 不允许引用任何名人或名句"""
    
    return prompt


def generate_llm_report(scores: list[dict]) -> dict[str, Any]:
    """生成 LLM 文字研报。
    
    输入: scorer.py 的 score_all() 返回的 result 列表
    输出: {"markdown": str, "top_code": str, "buy_count": int, "sell_count": int, "moat_top": str}
    """
    if not scores:
        return {"markdown": "暂无评分数据", "top_code": "", "buy_count": 0, "sell_count": 0, "moat_top": ""}
    
    ranked = sorted(scores, key=lambda x: x.get("total_score", 0), reverse=True)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    
    prompt = _build_prompt(ranked, scores)
    
    # 统计
    buy_count = sum(1 for r in scores if r.get("signal_action") == "BUY")
    sell_count = sum(1 for r in scores if r.get("signal_action") == "SELL")
    
    # 护城河第一
    moat_sorted = sorted(scores, key=lambda x: x.get("moat_score", 50), reverse=True)
    moat_top = moat_sorted[0]["name"] if moat_sorted else ""
    
    # 返回数据 + prompt（由上层决定如何调用 LLM 渲染）
    return {
        "prompt": prompt,
        "top_code": ranked[0].get("code", ""),
        "top_name": ranked[0]["name"],
        "top_score": ranked[0]["total_score"],
        "buy_count": buy_count,
        "sell_count": sell_count,
        "moat_top": moat_top,
        "ranked": ranked,
        "all_scores": scores,
    }
