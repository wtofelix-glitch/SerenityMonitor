"""
Serenity Council — 5 Agent 投资委员会引擎
借鉴 FinceptTerminal Renaissance Technologies 模式

五角色决策链：
  SignalScientist → QuantResearcher → PortfolioManager → RiskQuant → InvestmentCommittee

统计门禁（Renaissance 标准）：
  • IC > 0.03（信息系数）
  • 多维度确认（≥2维度一致方向）
  • 风险调整后收益 > 1.5%

决策输出格式：
  DECISION: [APPROVED / REJECTED / NEEDS_REVISION]
  RATIONALE: 三条关键理由
  CONDITIONS: 执行条件 + 限制
  NEXT STEPS: 谁做什么

用法：
    python3 serenity_council.py              # 对所有标的开委员会
    python3 serenity_council.py 002281       # 单只标的全流程
    python3 serenity_council.py --report     # 委员会报告
"""

import json
import sys
from datetime import date, timedelta
from typing import Optional
from dataclasses import dataclass, field
from collections import defaultdict

from config import STOCK_MAP, ALL_CODES, STOCK_DETAILS, SERENITY_WEIGHTS
from db import (
    get_price_history, get_avg_volume, get_latest_scores, get_signal_performance,
    get_reflection_dimension_ic,
)


# ============================================================
# 数据类
# ============================================================

@dataclass
class CouncilVote:
    """单个 Agent 的投票"""
    agent: str
    decision: str       # APPROVED / REJECTED / ABSTAIN
    confidence: float   # 0.0 - 1.0
    rationale: list[str]  # 理由列表
    conditions: list[str]  # 条件列表
    score: float = 0.0  # Agent 给的综合评分


@dataclass
class CouncilDecision:
    """委员会终审决策"""
    code: str
    name: str
    date: str
    decision: str       # APPROVED / REJECTED / NEEDS_REVISION
    total_score: float
    confidence: float
    votes: list[CouncilVote]
    rationale: list[str]
    conditions: list[str]
    next_steps: list[str]
    veto_reason: str = ""


# ============================================================
# 统计门禁
# ============================================================

class StatisticalGate:
    """Renaissance 风格统计验证"""

    @staticmethod
    def check_ic(score_dim: str, min_ic: float = 0.03) -> tuple[bool, float]:
        """检查维度IC是否达标"""
        dim_ic = get_reflection_dimension_ic(days=20)
        ic_val = dim_ic.get(score_dim, 0.0)
        return abs(ic_val) >= min_ic, ic_val

    @staticmethod
    def check_signal_quality(code: str) -> dict:
        """
        检查信号质量：
        - 多维度一致性
        - 成交量确认
        - 趋势方向
        """
        scores = get_latest_scores([code])
        if not scores:
            return {"quality": "INSUFFICIENT_DATA", "signals": []}

        s = scores[0]

        # 提取各维度信号方向
        directions = {
            "base": 1 if s.get("base_score", 50) > 60 else (-1 if s.get("base_score", 50) < 40 else 0),
            "zone": 1 if s.get("zone_score", 50) > 60 else (-1 if s.get("zone_score", 50) < 40 else 0),
            "momentum": 1 if s.get("momentum_score", 50) > 60 else (-1 if s.get("momentum_score", 50) < 40 else 0),
            "serenity": 1 if s.get("serenity_score", 50) > 60 else (-1 if s.get("serenity_score", 50) < 40 else 0),
            "factor": 1 if s.get("factor_score", 50) > 60 else (-1 if s.get("factor_score", 50) < 40 else 0),
            "technical": 1 if s.get("technical_score", 50) > 60 else (-1 if s.get("technical_score", 50) < 40 else 0),
            "sentiment": 1 if s.get("sentiment_score", 50) > 60 else (-1 if s.get("sentiment_score", 50) < 40 else 0),
        }

        # 多数方向
        bullish = sum(1 for v in directions.values() if v > 0)
        bearish = sum(1 for v in directions.values() if v < 0)
        neutral = sum(1 for v in directions.values() if v == 0)

        # ≥2维度一致方向确认
        consensus = "MIXED"
        if bullish >= 4:
            consensus = "STRONG_BULLISH"
        elif bullish >= 2:
            consensus = "BULLISH"
        elif bearish >= 4:
            consensus = "STRONG_BEARISH"
        elif bearish >= 2:
            consensus = "BEARISH"

        return {
            "quality": "HIGH" if abs(bullish - bearish) >= 3 else ("MEDIUM" if abs(bullish - bearish) >= 2 else "LOW"),
            "consensus": consensus,
            "bullish_dims": bullish,
            "bearish_dims": bearish,
            "neutral_dims": neutral,
            "directions": directions,
        }

    @staticmethod
    def check_volume_confirmation(code: str) -> tuple[bool, str]:
        """检查量价配合"""
        prices = get_price_history(code, days=5)
        if len(prices) < 3:
            return False, "数据不足"
        avg_vol = get_avg_volume(code, days=10)
        if avg_vol <= 0:
            return False, "无成交量数据"
        latest_vol = prices[0].get("volume", 0) if prices else 0
        ratio = latest_vol / avg_vol if avg_vol > 0 else 1
        if ratio >= 1.5:
            return True, f"放量{ratio:.1f}倍"
        elif ratio <= 0.5:
            return False, f"缩量至{ratio:.1f}倍"
        else:
            return True, f"量能正常({ratio:.1f}x)"


# ============================================================
# Council Agents
# ============================================================

class CouncilMember:
    """Agent 基类"""
    name: str = ""
    role: str = ""
    weight: float = 1.0  # 投票权重

    def analyze(self, code: str) -> CouncilVote:
        raise NotImplementedError


class SignalScientist(CouncilMember):
    """
    信号科学家 — 识别交易信号模式
    
    Reference: FinceptTerminal signal_scientist.py
    "Pattern recognition specialist, discovers non-random patterns"
    """
    name = "SignalScientist"
    role = "信号识别与模式发现"
    weight = 1.0

    def analyze(self, code: str) -> CouncilVote:
        from signal_engine import generate_signals
        from portfolio import get_portfolio

        portfolio = get_portfolio()
        holding_codes = portfolio.position_codes
        signals = generate_signals(portfolio=portfolio)
        signal_map = {s["code"]: s for s in signals}
        sig = signal_map.get(code, {})

        action = sig.get("action", "HOLD")
        confidence = sig.get("buy_confirm", {}).get("confidence", 0.5)
        total_score = sig.get("total_score", 50)

        rationale = []
        conditions = []

        if action in ("STRONG_BUY", "BUY"):
            decision = "APPROVED"
            confidence = min(confidence * 1.2, 1.0)
            rationale.append(f"综合评分 {total_score:.0f} 触发 {action} 信号")
            rationale.append(f"信号置信度 {confidence:.1%}")
            conditions.append("确认成交量配合后执行")
        elif action == "CAUTION_BUY":
            decision = "APPROVED"
            confidence = confidence * 0.8
            rationale.append(f"评分 {total_score:.0f} 触发谨慎买入")
            rationale.append("需等待更多技术确认")
            conditions.append("降低初始仓位至正常水平的50%")
        elif action in ("SELL", "STOP_LOSS"):
            decision = "APPROVED"
            confidence = 0.9
            rationale.append(f"触发 {action} 信号")
            conditions.append("立即执行")
        else:
            decision = "ABSTAIN"
            confidence = 0.3
            rationale.append(f"当前信号: {action}，暂不推荐操作")

        return CouncilVote(
            agent=self.name,
            decision=decision,
            confidence=round(confidence, 2),
            rationale=rationale,
            conditions=conditions,
            score=total_score,
        )


class QuantResearcher(CouncilMember):
    """
    量化研究员 — 统计验证
    
    Reference: FinceptTerminal quant_researcher.py
    "Statistical modeling, cross-validation, p-value testing"
    """
    name = "QuantResearcher"
    role = "统计验证与因子分析"
    weight = 1.2

    def analyze(self, code: str) -> CouncilVote:
        gate = StatisticalGate()
        quality = gate.check_signal_quality(code)
        vol_ok, vol_msg = gate.check_volume_confirmation(code)

        # 获取维度IC
        dim_ic = get_reflection_dimension_ic(days=20)

        rationale = []
        conditions = []
        score = 0

        # 1. 信号一致性检查
        if quality["consensus"] in ("STRONG_BULLISH", "BULLISH"):
            score += 30
            rationale.append(f"多维度确认:{quality['bullish_dims']}/{quality['bearish_dims']+quality['bullish_dims']+quality['neutral_dims']} 偏多")
            rationale.append(f"一致性评级: {quality['consensus']}")
        elif quality["consensus"] in ("STRONG_BEARISH", "BEARISH"):
            score += 10
            rationale.append(f"多维度偏空: {quality['bearish_dims']}/{quality['bullish_dims']+quality['bearish_dims']+quality['neutral_dims']}")
        else:
            score += 15
            rationale.append("信号混合，缺乏明确方向")

        # 2. IC验证
        valid_dims = []
        for dim, ic_val in dim_ic.items():
            if abs(ic_val) >= 0.03:
                valid_dims.append(f"{dim}={ic_val:+.3f}")
        if valid_dims:
            score += 20
            rationale.append(f"有效维度IC(>{0.03}): {', '.join(valid_dims[:3])}")
        else:
            score += 5
            rationale.append("无显著IC维度(>0.03)，信号统计意义弱")
            conditions.append("建议等待IC改善后再操作")

        # 3. 量价配合
        if vol_ok:
            score += 20
            rationale.append(f"量价配合正常: {vol_msg}")
        else:
            score += 5
            rationale.append(f"⚠️ 量价异常: {vol_msg}")
            conditions.append("等待量价恢复正常")

        # 4. 趋势强度
        scores = get_latest_scores([code])
        if scores:
            tech = scores[0].get("technical_score", 50)
            if tech >= 65:
                score += 15
                rationale.append(f"技术面强势(技{tech:.0f})")
            elif tech <= 35:
                score += 5
                rationale.append(f"技术面偏弱(技{tech:.0f})")
            else:
                score += 10

        # 决策
        confidence = score / 85  # 满分85
        confidence = round(min(confidence, 1.0), 2)

        if score >= 60:
            decision = "APPROVED"
        elif score >= 40:
            decision = "APPROVED"
            confidence = max(0.4, confidence)
            conditions.append("降低仓位至正常50%")
        else:
            decision = "REJECTED"
            rationale.append(f"量化验证不通过 (分{score}/85)")

        return CouncilVote(
            agent=self.name,
            decision=decision,
            confidence=confidence,
            rationale=rationale,
            conditions=conditions,
            score=float(score),
        )


class PortfolioManager(CouncilMember):
    """
    组合经理 — 仓位建议
    
    Reference: FinceptTerminal portfolio_manager.py
    "Capital allocation, portfolio construction, rebalancing"
    """
    name = "PortfolioManager"
    role = "仓位管理与资金分配"
    weight = 1.0

    def analyze(self, code: str) -> CouncilVote:
        from portfolio import get_portfolio
        from config import CAPITAL_CONFIG, RISK_CONFIG

        portfolio = get_portfolio()
        current_positions = portfolio.positions
        holding_codes = portfolio.position_codes

        max_positions = CAPITAL_CONFIG.get("max_positions", 3)
        max_single_weight = CAPITAL_CONFIG.get("max_single_weight", 0.50)
        min_single_weight = CAPITAL_CONFIG.get("min_single_weight", 0.20)

        rationale = []
        conditions = []
        score = 50

        # 1. 仓位容量检查
        if code in holding_codes:
            score += 20
            rationale.append("已持仓 — 调仓/加仓/持有")
            # 检查当前仓位
            pos = next((p for p in current_positions if p.get("code") == code), {})
            current_weight = pos.get("weight", 0)
            if current_weight >= max_single_weight:
                score -= 10
                conditions.append(f"仓位已达上限{max_single_weight*100:.0f}%，不再加仓")
            elif current_weight < min_single_weight:
                score += 5
                rationale.append(f"当前仓位{current_weight*100:.0f}%，有加仓空间")
        else:
            if len(holding_codes) >= max_positions:
                score -= 20
                rationale.append(f"持仓已满({len(holding_codes)}/{max_positions})")
                conditions.append("需先减仓后再开新仓")
            else:
                score += 15
                rationale.append(f"可开新仓(当前{len(holding_codes)}/{max_positions})")
                conditions.append(f"初始仓位{min_single_weight*100:.0f}%")

        # 2. 资金充足性
        pv = portfolio.get_portfolio_value()
        total_value = pv["total_value"]
        cash = pv["cash"]
        cash_ratio = cash / total_value if total_value > 0 else 0
        reserve_ratio = CAPITAL_CONFIG.get("reserve_cash_ratio", 0.10)

        if cash_ratio >= reserve_ratio * 2:
            score += 15
            rationale.append(f"现金充裕({cash_ratio*100:.0f}%)")
        elif cash_ratio >= reserve_ratio:
            score += 10
            rationale.append(f"现金充足({cash_ratio*100:.0f}%)")
        else:
            score += 5
            rationale.append(f"⚠️ 现金偏紧({cash_ratio*100:.0f}%)")
            conditions.append(f"保留{reserve_ratio*100:.0f}%现金底线")

        # 决策
        confidence = score / 85
        confidence = round(min(confidence, 1.0), 2)

        if score >= 55:
            decision = "APPROVED"
        elif score >= 35:
            decision = "APPROVED"
            confidence = max(0.3, confidence)
            conditions.append("仓位减半，谨慎操作")
        else:
            decision = "REJECTED"
            rationale.append("仓位管理条件不满足")

        return CouncilVote(
            agent=self.name,
            decision=decision,
            confidence=confidence,
            rationale=rationale,
            conditions=conditions,
            score=float(score),
        )


class RiskQuant(CouncilMember):
    """
    风险量化师 — 风险审查
    
    Reference: FinceptTerminal risk_quant.py
    "Risk budgets, tail risk, max drawdown monitoring"
    """
    name = "RiskQuant"
    role = "风险评估与止损审查"
    weight = 1.3  # 风控有更高权重

    def analyze(self, code: str) -> CouncilVote:
        from config import RISK_CONFIG, STRATEGY_CONFIG
        from portfolio import get_portfolio

        portfolio = get_portfolio()
        pv = portfolio.get_portfolio_value()
        total_value = pv["total_value"]
        current_positions = portfolio.positions
        stop_loss_pct = RISK_CONFIG.get("stop_loss_pct", -0.08)
        max_portfolio_dd = RISK_CONFIG.get("max_portfolio_drawdown", -0.15)
        atr_stop_multiplier = RISK_CONFIG.get("atr_stop_multiplier", 2.5)

        rationale = []
        conditions = []
        score = 50

        # 1. 回撤检查
        prices = get_price_history(code, days=20)
        if prices and len(prices) >= 5:
            highs = [p.get("high", p.get("close", 0)) for p in prices]
            peak = max(highs)
            current = prices[0].get("close", 0)
            if peak > 0 and current > 0:
                drawdown = (current - peak) / peak
                if drawdown <= stop_loss_pct:
                    score -= 20
                    rationale.append(f"🚨 触发止损线({drawdown*100:.1f}% ≤ {stop_loss_pct*100:.0f}%)")
                    conditions.append("建议立即止损")
                elif drawdown <= stop_loss_pct * 0.7:
                    score -= 5
                    rationale.append(f"⚠️ 接近止损线({drawdown*100:.1f}%)")
                    conditions.append(f"如继续下跌{stop_loss_pct*100:.0f}%则止损")
                else:
                    score += 10
                    max_dd = f"最大回撤{drawdown*100:.1f}%"
                    rationale.append(f"回撤可控: {max_dd}")
            else:
                score += 5
                rationale.append("价格数据不完整，跳过回撤分析")
        else:
            score += 5
            rationale.append("历史数据不足，跳过回撤分析")

        # 2. 组合风险预算
        total_exposure = sum(p.get("market_value", 0) for p in current_positions)
        exposure_ratio = total_exposure / total_value if total_value > 0 else 0

        if exposure_ratio <= 0.5:
            score += 10
            rationale.append(f"总敞口可控({exposure_ratio*100:.0f}%)")
        elif exposure_ratio <= 0.7:
            score += 5
            rationale.append(f"总敞口中等({exposure_ratio*100:.0f}%)")
        else:
            score -= 5
            rationale.append(f"⚠️ 总敞口偏高({exposure_ratio*100:.0f}%)")
            conditions.append(f"控制总敞口≤70%")

        # 3. 组合最大回撤检查
        total_profit_pct = pv.get("total_profit_pct", 0) / 100  # convert from percentage to decimal
        if total_profit_pct <= max_portfolio_dd:
            score -= 15
            rationale.append(f"🚨 组合回撤超限({total_profit_pct*100:.1f}%)")
            conditions.append("暂停新开仓，优先修复组合")
        elif total_profit_pct <= max_portfolio_dd * 0.5:
            score += 5
            rationale.append(f"⚠️ 组合回撤接近警告线({total_profit_pct*100:.1f}%)")
        else:
            score += 10
            rationale.append("组合风险在预算内")

        # 决策
        confidence = score / 70
        confidence = round(min(confidence, 1.0), 2)

        if score >= 45:
            decision = "APPROVED"
        elif score >= 30:
            decision = "APPROVED"
            confidence = max(0.3, confidence)
            conditions.append("风控偏紧，仓位≤正常30%")
        else:
            decision = "REJECTED"
            rationale.append("风险审查不通过")

        return CouncilVote(
            agent=self.name,
            decision=decision,
            confidence=confidence,
            rationale=rationale,
            conditions=conditions,
            score=float(score),
        )


# ============================================================
# 投资委员会 — 终审
# ============================================================

class InvestmentCommittee:
    """
    投资委员会主席
    汇总所有 Agent 投票，输出终审决策
    
    Reference: FinceptTerminal investment_committee.py
    "Final decision authority, capital allocation, risk limits"
    """
    
    REQUIRED_APPROVALS = 3  # 至少3个Agent通过才可通过
    VETO_THRESHOLD = 0.3    # 置信度低于此值视为无意义

    def __init__(self):
        self.members: list[CouncilMember] = [
            SignalScientist(),
            QuantResearcher(),
            PortfolioManager(),
            RiskQuant(),
        ]

    def review(self, code: str) -> CouncilDecision:
        """开全体委员会，投票决策"""
        name = STOCK_MAP.get(code, {}).get("name", code)
        today = date.today().isoformat()

        # 1. 收集所有Agent投票
        votes = []
        for member in self.members:
            try:
                vote = member.analyze(code)
                votes.append(vote)
            except Exception as e:
                votes.append(CouncilVote(
                    agent=member.name,
                    decision="ABSTAIN",
                    confidence=0.0,
                    rationale=[f"分析异常: {e}"],
                    conditions=[],
                ))

        # 2. 统计
        approved = [v for v in votes if v.decision == "APPROVED"]
        rejected = [v for v in votes if v.decision == "REJECTED"]
        abstained = [v for v in votes if v.decision == "ABSTAIN"]

        # 加权置信度
        total_weight = sum(m.weight for m in self.members)
        weighted_conf = sum(
            v.confidence * self.members[i].weight
            for i, v in enumerate(votes)
        ) / total_weight if total_weight > 0 else 0

        # 加权评分
        weighted_score = sum(
            v.score * self.members[i].weight
            for i, v in enumerate(votes)
        ) / total_weight if total_weight > 0 else 50

        # 3. 决策逻辑
        rationale = []
        conditions = []
        next_steps = []
        veto_reason = ""

        # 风控一票否决（RiskQuant权重1.3，有更高否决权）
        risk_vote = next((v for v in votes if v.agent == "RiskQuant"), None)
        if risk_vote and risk_vote.decision == "REJECTED" and risk_vote.confidence >= 0.5:
            decision = "REJECTED"
            veto_reason = "⚠️ RiskQuant 一票否决"
            rationale = risk_vote.rationale.copy()
            conditions = risk_vote.conditions.copy()
        elif len(rejected) >= 2:
            decision = "REJECTED"
            veto_reason = f"{len(rejected)}/{len(votes)} Agent反对"
            for v in rejected:
                rationale.extend(v.rationale)
        elif len(approved) >= self.REQUIRED_APPROVALS and weighted_conf >= self.VETO_THRESHOLD:
            decision = "APPROVED"
            # 汇总所有通过的rationale和conditions
            for v in approved:
                rationale.extend(v.rationale[:2])
                conditions.extend(v.conditions[:2])
            # 去重
            rationale = list(dict.fromkeys(rationale))[:5]
            conditions = list(dict.fromkeys(conditions))[:3]
            next_steps = [
                f"按{weighted_score:.0f}分执行，置信度{weighted_conf:.1%}",
                f"严格遵循止损线（-8%硬止损）",
            ]
        else:
            decision = "NEEDS_REVISION"
            veto_reason = f"通过{len(approved)}/{self.REQUIRED_APPROVALS}，需更多确认"
            for v in votes:
                rationale.extend(v.rationale)
            next_steps = ["等待信号改善", "降低仓位观察", "次日重新评估"]

        # 4. 输出
        return CouncilDecision(
            code=code,
            name=name,
            date=today,
            decision=decision,
            total_score=round(weighted_score, 1),
            confidence=round(weighted_conf, 2),
            votes=votes,
            rationale=list(dict.fromkeys(rationale))[:5],
            conditions=list(dict.fromkeys(conditions))[:3],
            next_steps=next_steps,
            veto_reason=veto_reason,
        )

    def review_all(self) -> list[CouncilDecision]:
        """对所有标的开委员会"""
        return [self.review(code) for code in ALL_CODES]


# ============================================================
# 输出格式化
# ============================================================

def format_decision(dec: CouncilDecision) -> str:
    """按 Fincept 标准格式输出决策"""
    lines = []
    lines.append(f"\n{'='*70}")
    lines.append(f"🏛️  投资委员会终审 | {dec.name}({dec.code}) | {dec.date}")
    lines.append(f"{'='*70}")

    # 决策
    icon = {"APPROVED": "🟢", "REJECTED": "🔴", "NEEDS_REVISION": "🟡"}.get(dec.decision, "⚪")
    lines.append(f"\n  DECISION: {icon} {dec.decision}")
    if dec.veto_reason:
        lines.append(f"  {dec.veto_reason}")
    lines.append(f"  综合评分: {dec.total_score:.0f} | 置信度: {dec.confidence:.1%}")

    # 各Agent投票
    lines.append(f"\n  {'─'*50}")
    lines.append(f"  {'Agent':<22} {'决定':<14} {'置信度':>8} {'评分':>6}")
    lines.append(f"  {'─'*50}")
    for v in dec.votes:
        dec_icon = {"APPROVED": "🟢", "REJECTED": "🔴", "ABSTAIN": "⚪"}.get(v.decision, "")
        lines.append(f"  {v.agent:<22} {dec_icon} {v.decision:<10} {v.confidence:>7.0%} {v.score:>6.0f}")

    # RATIONALE
    lines.append(f"\n  RATIONALE:")
    for r in dec.rationale:
        lines.append(f"    • {r}")

    # CONDITIONS
    if dec.conditions:
        lines.append(f"\n  CONDITIONS:")
        for c in dec.conditions:
            lines.append(f"    • {c}")

    # NEXT STEPS
    if dec.next_steps:
        lines.append(f"\n  NEXT STEPS:")
        for s in dec.next_steps:
            lines.append(f"    → {s}")

    lines.append(f"\n{'='*70}")
    return "\n".join(lines)


def format_report(decisions: list[CouncilDecision]) -> str:
    """生成委员会总报告"""
    lines = []
    lines.append(f"📊 Serenity 投资委员会 — {date.today().isoformat()}")
    lines.append("=" * 70)

    approved = [d for d in decisions if d.decision == "APPROVED"]
    rejected = [d for d in decisions if d.decision == "REJECTED"]
    pending = [d for d in decisions if d.decision == "NEEDS_REVISION"]

    lines.append(f"\n  通过 🟢: {len(approved)} | 否决 🔴: {len(rejected)} | 待审 🟡: {len(pending)}")

    if approved:
        lines.append(f"\n  {'─'*50}")
        lines.append(f"  ✅ 通过标的:")
        for d in approved:
            lines.append(f"    {d.name}({d.code}) — 评分{d.total_score:.0f} 置信{d.confidence:.1%}")

    if rejected:
        lines.append(f"\n  {'─'*50}")
        lines.append(f"  ❌ 否决标的:")
        for d in rejected:
            lines.append(f"    {d.name}({d.code}) — {d.veto_reason or '条件不满足'}")

    lines.append(f"\n{'='*70}")
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def main():
    council = InvestmentCommittee()

    if "--report" in sys.argv:
        decisions = council.review_all()
        print(format_report(decisions))
    elif len(sys.argv) > 1 and sys.argv[1] in ALL_CODES:
        code = sys.argv[1]
        dec = council.review(code)
        print(format_decision(dec))
    else:
        # 默认：全量开委员会
        decisions = council.review_all()
        for dec in decisions:
            print(format_decision(dec))
        print(format_report(decisions))


if __name__ == "__main__":
    main()
