"""
组合优化器 — 基于因子信号 + Rank IC 计算最优仓位配置
纯 numpy 实现，无新依赖。

用法:
    from portfolio_optimizer import PositionOptimizer
    opt = PositionOptimizer()
    plan = opt.optimize_allocation(factor_signals, positions, available_cash, ic_data)
"""

import numpy as np
from typing import Optional

from config import RISK_CONFIG, STOCK_MAP, ALL_CODES

__all__ = ["PositionOptimizer"]


class PositionOptimizer:
    """组合优化器 — 基于因子信号 + Rank IC 计算最优仓位比例"""

    def __init__(self):
        rc = RISK_CONFIG
        self.min_signal = rc.get("optimizer_min_signal", 0.05)
        self.max_position_pct = rc.get("optimizer_max_position_pct", 0.40)
        self.min_trade = rc.get("optimizer_min_trade", 5000)

    # ── 凯利公式 ──────────────────────────────────────────────

    def kelly_fraction(
        self,
        signal_strength: float,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
    ) -> float:
        """
        凯利公式计算最优仓位比例。

        f* = (p * b - q) / b

        其中:
            p = win_rate（胜率）
            q = 1 - p（败率）
            b = |avg_win_pct / avg_loss_pct|（赔率）

        Parameters
        ----------
        signal_strength : float — 因子综合信号 [-1, 1]
        win_rate        : float — 胜率 [0, 1]
        avg_win_pct     : float — 平均赢率（正数）
        avg_loss_pct    : float — 平均亏损率（负数）

        Returns
        -------
        float — 建议仓位比例 [0, 1]，以 signal_strength 调制
        """
        # 参数防御
        if avg_loss_pct >= 0:
            return 0.0
        b = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct < 0 else 0.0
        if b <= 0:
            return 0.0
        win_rate = float(np.clip(win_rate, 0.01, 0.99))
        q = 1.0 - win_rate
        f = (win_rate * b - q) / b
        # 用信号强度调制：信号弱时收缩仓位
        signal_mod = abs(signal_strength)
        f = f * signal_mod
        return float(np.clip(f, 0.0, 1.0))

    # ── 核心优化 ──────────────────────────────────────────────

    def optimize_allocation(
        self,
        factor_signals: list[dict],
        positions: list[dict],
        available_cash: float,
        ic_data: Optional[dict] = None,
    ) -> dict:
        """
        基于因子信号 + Rank IC 计算最优仓位比例。

        策略:
          1. 综合信号 < -0.1 → 减仓或卖出
          2. 综合信号 -0.1~0.1 → 维持当前仓位
          3. 综合信号 > 0.1 → 加仓（按信号强度比例分配）

        Parameters
        ----------
        factor_signals : list[dict]
            每个元素: {code, name, signal(float), factors:{signals:{...}}}
            对应 factor_engine.get_current_signals() 返回格式
        positions     : list[dict]
            当前持仓列表，每个元素至少含 {code, trade_amount, buy_price}
        available_cash : float — 可用资金
        ic_data       : dict, optional — Rank IC 数据
            含 latest:{dim:ic}, mean_ic:{dim:ic} 等（来自 factor_ic.compute_rank_ic）

        Returns
        -------
        dict:
            { code: { signal, suggested_weight, current_weight,
                      action, suggested_amount, reason }, ... }
        """
        if not factor_signals:
            return {}

        # ── 1. 构建信号映射 ──────────────────────────────
        signal_map: dict[str, float] = {}
        for s in factor_signals:
            signal_map[s["code"]] = s["signal"]

        # ── 2. 计算 IC 维度权重重调 ──────────────────────
        # 如果提供了 IC 数据，对维度稳定性打分
        dim_stability = self._compute_dim_stability(ic_data)

        # ── 3. 计算各标的综合信号强度（含 IC 调制）───────
        code_signals: dict[str, float] = {}
        code_signal_raw: dict[str, float] = {}
        for code, raw_signal in signal_map.items():
            code_signal_raw[code] = raw_signal
            # 将 raw_signal 按因子维度拆解后加权，IC 稳定的维度权重更高
            adjusted = self._ic_adjusted_signal(
                code, raw_signal, factor_signals, dim_stability
            )
            code_signals[code] = adjusted

        # ── 4. 当前仓位映射 ──────────────────────────────
        total_value = available_cash
        current_weight_map: dict[str, float] = {}
        position_amount_map: dict[str, float] = {}
        for p in positions:
            amt = float(p.get("trade_amount", 0) or 0)
            position_amount_map[p["code"]] = amt
            total_value += amt

        for code in signal_map:
            amt = position_amount_map.get(code, 0.0)
            current_weight_map[code] = (
                round(amt / total_value, 4) if total_value > 0 else 0.0
            )

        # ── 5. 计算目标权重 ──────────────────────────────
        # 过滤器：信号绝对值小于 min_signal → 维持
        active_codes = []
        for code in signal_map:
            adj = code_signals[code]
            if abs(adj) < self.min_signal:
                adj = 0.0  # 信号太弱，视为中性
            active_codes.append((code, adj))

        # 按信号分群
        sell_codes = [(c, s) for c, s in active_codes if s < -0.1]
        hold_codes = [(c, s) for c, s in active_codes if -0.1 <= s <= 0.1]
        buy_codes = [(c, s) for c, s in active_codes if s > 0.1]

        # --- 卖出标的：权重设为 0 ---
        target_weight_map: dict[str, float] = {}
        for code, _ in sell_codes:
            target_weight_map[code] = 0.0

        # --- 持有标的：维持当前权重 ---
        for code, _ in hold_codes:
            target_weight_map[code] = current_weight_map.get(code, 0.0)

        # --- 买入标的：按信号强度比例分配剩余权重 ---
        if buy_codes:
            buy_signal_values = np.array([s for _, s in buy_codes])
            # 将信号偏移到非负，最低信号保留基础权重
            signal_offset = buy_signal_values - (-0.1)  # 基准为 -0.1
            # 确保全部为正
            signal_offset = np.maximum(signal_offset, 0.0)
            total_signal = float(np.sum(signal_offset))

            if total_signal > 0:
                # 计算可用于加仓的权重上限
                # 当前买入标的已有持仓权重 + 卖出的权重 + 现金权重
                current_buy_weight = sum(
                    current_weight_map.get(c, 0.0) for c, _ in buy_codes
                )
                freed_weight = sum(target_weight_map.values())  # sell + hold
                # 可用权重 = 1.0 - freed_weight + current_buy_weight
                # 但实际上 freed_weight 已经包含了 current_buy_weight
                # 更简单：可用权重 = 1.0 - sum(hold 权重)
                hold_weight = sum(
                    current_weight_map.get(c, 0.0) for c, _ in hold_codes
                )
                available_weight = 1.0 - hold_weight

                # 按信号强度分配
                proportions = signal_offset / total_signal
                for i, (code, _) in enumerate(buy_codes):
                    raw_weight = available_weight * float(proportions[i])
                    # 单只上限
                    capped = min(raw_weight, self.max_position_pct)
                    target_weight_map[code] = round(capped, 4)
            else:
                for code, _ in buy_codes:
                    target_weight_map[code] = current_weight_map.get(code, 0.0)
        else:
            # 没有买入信号，但仍有卖出/持有标的
            for code, _ in hold_codes:
                target_weight_map[code] = current_weight_map.get(code, 0.0)

        # 确保所有权重之和不超过 1.0
        self._normalize_weights(target_weight_map)

        # ── 6. 生成输出 ──────────────────────────────────
        result = {}
        for code in signal_map:
            raw_sig = code_signal_raw.get(code, 0.0)
            adj_sig = code_signals.get(code, 0.0)
            cur_w = current_weight_map.get(code, 0.0)
            tgt_w = target_weight_map.get(code, 0.0)

            # 判断动作
            if abs(tgt_w - cur_w) < 0.005:
                action = "HOLD"
            elif tgt_w < cur_w:
                action = "REDUCE" if tgt_w > 0 else "SELL"
            elif tgt_w > cur_w:
                action = "BUY"
            else:
                action = "HOLD"

            suggested_amount = round(tgt_w * total_value, 2)
            current_amount = round(cur_w * total_value, 2)

            result[code] = {
                "signal": round(adj_sig, 4),
                "signal_raw": round(raw_sig, 4),
                "suggested_weight": tgt_w,
                "current_weight": cur_w,
                "action": action,
                "suggested_amount": suggested_amount,
                "current_amount": current_amount,
                "diff_amount": round(suggested_amount - current_amount, 2),
                "total_value": round(total_value, 2),
            }

        return result

    # ── 调仓计划 ──────────────────────────────────────────────

    def rebalance_plan(
        self,
        current_portfolio: list[dict],
        target_weights: dict[str, float],
        min_trade: Optional[float] = None,
    ) -> list[dict]:
        """
        生成调仓计划。

        Parameters
        ----------
        current_portfolio : list[dict]
            当前持仓列表，每项含 {code, name, trade_amount, buy_price, ...}
        target_weights   : dict {code: target_weight}
            目标仓位权重
        min_trade        : float, optional — 最小调仓金额，默认 self.min_trade

        Returns
        -------
        list[dict]:
            [{code, name, action, current_amount, target_amount,
              diff_amount, current_weight, target_weight, reason}]
        """
        if min_trade is None:
            min_trade = self.min_trade

        # 计算总资金
        total_value = 0.0
        current_amounts: dict[str, float] = {}
        name_map: dict[str, str] = {}
        for p in current_portfolio:
            amt = float(p.get("trade_amount", 0) or 0)
            current_amounts[p["code"]] = amt
            total_value += amt
            name_map[p["code"]] = p.get("name", p["code"])

        # 加上现金（不在持仓中的标的）
        # 我们从 target_weights 涵盖所有标的，total_value 只含持仓
        # 但现金应被视为待分配资金

        plan = []
        for code, tgt_w in target_weights.items():
            cur_amt = current_amounts.get(code, 0.0)
            tgt_amt = round(tgt_w * total_value, 2)
            diff = round(tgt_amt - cur_amt, 2)
            name = name_map.get(code, STOCK_MAP.get(code, {}).get("name", code))

            if abs(diff) < min_trade and abs(diff) > 0:
                # 差异小于最小调仓金额，但非零 → 标注微调但不执行
                action = "SKIP (小)"
                reason = f"差额 {diff:.0f} 元 < 最小调仓 {min_trade:.0f} 元，暂不操作"
            elif abs(diff) < 0.01:
                action = "HOLD"
                reason = "当前仓位已接近目标"
            elif diff > 0:
                action = "BUY"
                reason = f"信号加仓 {diff:.0f} 元"
            else:
                action = "SELL" if tgt_amt == 0 else "REDUCE"
                reason = f"信号{'清仓' if tgt_amt == 0 else '减仓'} {abs(diff):.0f} 元"

            plan.append({
                "code": code,
                "name": name,
                "action": action,
                "current_amount": cur_amt,
                "target_amount": tgt_amt,
                "diff_amount": diff,
                "current_weight": round(cur_amt / total_value, 4) if total_value > 0 else 0.0,
                "target_weight": tgt_w,
                "reason": reason,
            })

        # 按差额绝对值排序（先大幅调仓）
        plan.sort(key=lambda x: abs(x["diff_amount"]), reverse=True)
        return plan

    # ── 内部辅助 ────────────────────────────────────────────

    def _compute_dim_stability(self, ic_data: Optional[dict]) -> dict:
        """
        从 IC 数据计算各维度的稳定性得分。

        稳定性 = mean_ic 的绝对值 × ic_ir（IC 信息比率，均值/标准差）
        IC 越稳定（IR 越高），该维度在信号加权时权重越大。
        """
        stability: dict[str, float] = {}
        if not ic_data:
            return stability

        mean_ic = ic_data.get("mean_ic", {})
        ic_ir = ic_data.get("ic_ir", {})

        for dim in mean_ic:
            m = abs(mean_ic[dim])
            ir = abs(ic_ir.get(dim, 0.0))
            # 稳定性得分 = 均值 IC 绝对值 × min(IC_IR, 3) 限幅
            s = m * min(ir, 3.0)
            stability[dim] = round(s, 4)

        return stability

    def _ic_adjusted_signal(
        self,
        code: str,
        raw_signal: float,
        factor_signals: list[dict],
        dim_stability: dict[str, float],
    ) -> float:
        """
        用 IC 稳定性数据调制信号强度。

        若无 IC 数据，返回原始信号。
        若有，则对因子层面按稳定性加权后重新计算综合信号。
        """
        if not dim_stability or not factor_signals:
            return raw_signal

        # 找到该标的的详细因子信号
        fs = None
        for s in factor_signals:
            if s["code"] == code:
                fs = s.get("factors", {}).get("signals", {})
                break

        if not fs:
            return raw_signal

        # 9个信号因子映射到评分维度（近似映射）
        factor_to_dim = {
            "ksft": "technical_score",
            "rank_20": "momentum_score",
            "rsv_20": "momentum_score",
            "beta_20": "momentum_score",
            "resi_20": "technical_score",
            "macd_signal": "technical_score",
            "obv_trend": "volume_score",
            "mfi_signal": "volume_score",
            "cci_signal": "technical_score",
        }

        total_weight = 0.0
        weighted_sum = 0.0
        for factor_name, signal_val in fs.items():
            if signal_val is None:
                continue
            dim = factor_to_dim.get(factor_name, "total_score")
            # 基础权重 1.0，用稳定性调制
            stab = dim_stability.get(dim, 0.0)
            w = 1.0 + stab * 2.0  # 稳定性放大系数
            w = max(0.1, min(3.0, w))  # 限幅 [0.1, 3.0]
            weighted_sum += float(signal_val) * w
            total_weight += w

        if total_weight > 0:
            return float(weighted_sum / total_weight)
        return raw_signal

    def _normalize_weights(self, weight_map: dict[str, float], max_pct: Optional[float] = None):
        """
        归一化权重，使总和为 1.0，并施加单只上限。
        原地修改 weight_map。
        """
        if max_pct is None:
            max_pct = self.max_position_pct

        # 先逐个 cap
        for code in weight_map:
            weight_map[code] = min(weight_map[code], max_pct)

        total = sum(weight_map.values())
        if total > 1e-12:
            for code in weight_map:
                weight_map[code] = round(weight_map[code] / total, 4)

        # 再次 cap 并修正总和
        for code in weight_map:
            weight_map[code] = min(weight_map[code], max_pct)

        # 最后一次归一化
        total = sum(weight_map.values())
        if total > 1e-12:
            remainder = round(1.0 - total, 4)
            if abs(remainder) > 0.0001:
                # 加到最大权重的标的
                max_code = max(weight_map, key=weight_map.get)
                weight_map[max_code] = round(weight_map[max_code] + remainder, 4)


# ── 便捷函数 ──────────────────────────────────────────────────

def format_rebalance_plan(plan: list[dict]) -> str:
    """格式化调仓计划为可读字符串"""
    lines = []
    lines.append("=" * 65)
    lines.append("  📊 Serenity 组合优化调仓计划")
    lines.append("=" * 65)
    lines.append("")
    lines.append(
        f"  {'代码':>6} {'名称':<10} {'动作':<12} "
        f"{'当前金额':>10} {'目标金额':>10} {'差额':>10} "
        f"{'当前%':>7} {'目标%':>7}"
    )
    lines.append(f"  {'─' * 72}")

    total_buy = 0.0
    total_sell = 0.0
    for p in plan:
        action = p["action"]
        if action == "SELL":
            action_icon = "🔴 SELL"
        elif action == "REDUCE":
            action_icon = "🟡 REDUCE"
        elif action == "BUY":
            action_icon = "🟢 BUY"
        elif action.startswith("SKIP"):
            action_icon = "⚪ SKIP"
        else:
            action_icon = "⚪ HOLD"

        cur = p["current_amount"]
        tgt = p["target_amount"]
        diff = p["diff_amount"]

        if diff > 0:
            total_buy += diff
        elif diff < 0:
            total_sell += abs(diff)

        lines.append(
            f"  {p['code']:>6} {p['name']:<10} {action_icon:<12} "
            f"{cur:>10.0f} {tgt:>10.0f} {diff:>+10.0f} "
            f"{p['current_weight']*100:>6.1f}% {p['target_weight']*100:>6.1f}%"
        )

    lines.append(f"  {'─' * 72}")
    lines.append(f"  买入总计: {total_buy:>8.0f} 元  |  卖出总计: {total_sell:>8.0f} 元")
    lines.append("")
    lines.append("=" * 65)
    return "\n".join(lines)
