"""
投资组合管理器 — 资金管理、仓位计算、实时盈亏跟踪
支持 5 万启动资金 → 3 个月 10 万目标
"""
from datetime import date, datetime
from typing import Optional
import json

from config import CAPITAL_CONFIG, RISK_CONFIG, STOCK_MAP, STOCK_DETAILS
from db import get_conn, add_trade, load_all_stocks, set_active, clear_active, get_price_history
from data_engine import fetch_realtime
from serenity_logger import get_logger

log = get_logger(__name__)


class PortfolioManager:
    """组合管理器 — 单一实例，管理全部资金与仓位"""

    def __init__(self, initial_capital: float = None):
        cfg = CAPITAL_CONFIG
        self.initial_capital = initial_capital or cfg["initial_capital"]
        self.target_capital = cfg["target_capital"]
        self.target_months = cfg["target_months"]
        self.max_positions = cfg["max_positions"]
        self.max_single_weight = cfg["max_single_weight"]
        self.min_single_weight = cfg["min_single_weight"]
        self.reserve_cash_ratio = cfg["reserve_cash_ratio"]
        self.commission_rate = cfg["commission_rate"]
        self.stamp_tax_rate = cfg["stamp_tax_rate"]

        # 风控
        self.stop_loss_pct = RISK_CONFIG["stop_loss_pct"]
        self.trailing_stop_pct = RISK_CONFIG["trailing_stop_pct"]

        # 浮动盈亏峰值追踪（用于移动止盈）
        self._peak_prices = {}  # code -> highest_price_since_entry
        self._entry_prices = {}  # code -> entry_price
        # 从 DB 加载持久化的峰值价格
        self._load_peaks_from_db()
        self.profit_take_levels = [
            (RISK_CONFIG["profit_take_level1"], RISK_CONFIG["partial_exit_level1"]),
            (RISK_CONFIG["profit_take_level2"], RISK_CONFIG["partial_exit_level2"]),
            (RISK_CONFIG["profit_take_level3"], 1.0),
        ]

    # ── 核心查询 ──────────────────────────────────────────

    @property
    def positions(self) -> list[dict]:
        """从数据库读取当前持仓"""
        stocks = load_all_stocks()
        return [s for s in stocks if s["is_active"]]

    @property
    def position_codes(self) -> list[str]:
        return [p["code"] for p in self.positions]

    def get_cash(self) -> float:
        """计算可用现金: 初始资金 - sum(买入金额) + sum(卖出金额)
           fallback: 若 trade_amount=0，用 price * quantity 计算
           健壮版：忽略 trade_amount=0 且 quantity=0 的无效记录"""
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT action, price, quantity, trade_amount FROM trades"
            ).fetchall()
        except Exception:
            return self.initial_capital
        finally:
            conn.close()
        bought = 0.0
        sold = 0.0
        for r in rows:
            amt = r["trade_amount"] or 0
            if amt == 0 and r["price"] and r["quantity"]:
                amt = r["price"] * r["quantity"]
            if amt == 0:
                continue  # 跳过无效记录
            if r["action"] == "buy":
                bought += amt
            elif r["action"] == "sell":
                sold += amt
        cash = self.initial_capital - bought + sold
        return max(cash, 0.0)  # 现金不可能为负，数据不全时截断到0

    def get_portfolio_value(self) -> dict:
        """计算当前组合总价值 = 现金 + 持仓市值 + 移动止盈追踪"""
        cash = self.get_cash()
        positions = self.positions
        if not positions:
            return {
                "cash": cash,
                "holdings_value": 0.0,
                "total_value": cash,
                "total_profit_pct": 0.0,
                "total_profit_amount": 0.0,
                "position_count": 0,
            }

        codes = [p["code"] for p in positions]
        realtime = fetch_realtime(codes)
        rt_map = {r["code"]: r for r in realtime}

        holdings_value = 0.0
        details = []
        for p in positions:
            code = p["code"]
            rt = rt_map.get(code, {})
            price = rt.get("price", 0)
            buy_price = p["buy_price"]
            amount = p.get("trade_amount", 0) or 0
            shares = int(amount / buy_price / 100) * 100 if buy_price > 0 and amount > 0 else 0
            current_value = shares * price
            holdings_value += current_value
            profit_pct = ((price - buy_price) / buy_price * 100) if buy_price > 0 else 0
            details.append({
                "code": code,
                "name": STOCK_MAP.get(code, {}).get("name", code),
                "buy_price": buy_price,
                "current_price": price,
                "shares": shares,
                "cost": amount,
                "current_value": current_value,
                "profit_pct": round(profit_pct, 2),
                "profit_amount": round(current_value - amount, 2),
                "weight": round(current_value / (cash + holdings_value) * 100, 1) if (cash + holdings_value) > 0 else 0,
            })

        total_value = cash + holdings_value
        total_profit_pct = (total_value - self.initial_capital) / self.initial_capital * 100

        return {
            "cash": cash,
            "holdings_value": holdings_value,
            "total_value": total_value,
            "total_profit_pct": round(total_profit_pct, 2),
            "total_profit_amount": round(total_value - self.initial_capital, 2),
            "position_count": len(positions),
            "positions": details,
        }

    # ── 仓位计算 ──────────────────────────────────────────

    def calc_position_size(self, code: str, signal_confidence: float, skip_limit_check: bool = False) -> dict:
        """
        基于 Kelly 公式计算仓位大小

        Parameters
        ----------
        code              : 股票代码
        signal_confidence : 信号置信度 [0, 1]
        skip_limit_check  : 跳过最大持仓数限制（用于计算已有持仓的Kelly）

        Returns
        -------
        dict with { shares, amount, cash_used_pct, reason }
        """
        from data_engine import fetch_single

        cfg = CAPITAL_CONFIG
        cash = self.get_cash()
        total = self.get_portfolio_value()["total_value"]
        positions = self.positions

        # 已有仓位数量限制（仅对新买入候选人）
        if not skip_limit_check and len(positions) >= self.max_positions:
            return {"shares": 0, "amount": 0, "cash_used_pct": 0, "reason": f"已达最大持仓数 {self.max_positions}"}

        # 保留现金
        max_usable = cash * (1 - self.reserve_cash_ratio)

        # 单只最大/最小仓位
        max_per_position = total * self.max_single_weight
        min_per_position = total * self.min_single_weight

        # Kelly 调整: high confidence → 更大仓位
        kelly_fraction = 0.2 + signal_confidence * 0.5  # 范围 0.2 ~ 0.7
        target_amount = min(max_usable * kelly_fraction, max_per_position)

        # 不能小于最小仓位
        if target_amount < min_per_position:
            target_amount = min_per_position

        # 计算股数（整百）
        data = fetch_single(code)
        price = data.get("price", 0) if data else 0
        if price <= 0:
            # 尝试从数据库获取最近收盘价
            rows = get_price_history(code, 1)
            price = float(rows[0]["close"]) if rows else 0
        if price <= 0:
            return {"shares": 0, "amount": 0, "cash_used_pct": 0, "reason": "无法获取价格"}

        shares = int(target_amount / price / 100) * 100
        actual_amount = shares * price
        cash_used_pct = actual_amount / cash * 100 if cash > 0 else 0

        return {
            "shares": shares,
            "amount": round(actual_amount, 2),
            "cash_used_pct": round(cash_used_pct, 1),
            "price": price,
            "reason": f"Kelly {kelly_fraction:.0%} 仓位, 信度 {signal_confidence:.0%}" if signal_confidence > 0 else "最小仓位试探",
        }

    # ── 执行买入 ──────────────────────────────────────────

    def execute_buy(self, code: str, signal_confidence: float = 0.5, force_amount: float = 0) -> dict:
        """执行买入，返回执行结果"""
        from data_engine import fetch_single

        # 风控检查
        try:
            from risk_manager import get_risk_manager
            risk = get_risk_manager()
            pv = self.get_portfolio_value()
            risk_check = risk.is_trade_allowed(
                code=code, action="BUY",
                holdings=self.positions,
                current_total_value=pv["total_value"],
                initial_capital=self.initial_capital,
                new_amount=force_amount or pv["total_value"] * self.max_single_weight,
            )
            if not risk_check["allowed"]:
                log.warning("风控拦截买入 %s: %s", code, "; ".join(risk_check["reasons"]))
                return {"status": "blocked", "reason": "; ".join(risk_check["reasons"])}
        except Exception:
            pass  # 风险模块异常不阻断执行

        # 计算仓位
        if force_amount > 0:
            amount = force_amount
            data = fetch_single(code)
            price = data.get("price", 0) if data else 0
            shares = int(amount / price / 100) * 100 if price > 0 else 0
            actual_amount = shares * price
        else:
            sizing = self.calc_position_size(code, signal_confidence)
            if sizing["shares"] == 0:
                return {"status": "skip", **sizing}
            shares = sizing["shares"]
            actual_amount = sizing["amount"]
            price = sizing["price"]

        if shares <= 0 or actual_amount <= 0:
            return {"status": "error", "reason": "无效仓位计算"}

        # 检查可用现金
        cash = self.get_cash()
        cost = actual_amount * (1 + self.commission_rate)
        if cost > cash:
            return {"status": "error", "reason": f"现金不足: 需 {cost:.0f}, 有 {cash:.0f}"}

        today = date.today().isoformat()
        detail = STOCK_DETAILS.get(code, {})

        # 计算动态止损价
        from signal_engine import get_dynamic_stop_loss
        dynamic_stop = get_dynamic_stop_loss(code, price)

        # 记录买入
        set_active(code, price, today, detail.get("target_sell", 0), detail.get("buy_zone_low", 0))

        conn = get_conn()
        conn.execute("UPDATE stocks SET trade_amount=? WHERE code=?", (actual_amount, code))
        conn.commit()
        conn.close()

        add_trade(code, "buy", price, shares, today,
                  f"信号买入 {actual_amount:.0f}元 (信度{signal_confidence:.0%})",
                  trade_amount=actual_amount)

        return {
            "status": "buy",
            "code": code,
            "name": STOCK_MAP.get(code, {}).get("name", code),
            "price": price,
            "shares": shares,
            "amount": actual_amount,
            "cash_remain": round(cash - cost, 2),
            "target_sell": detail.get("target_sell", 0),
            "stop_loss": dynamic_stop["stop_price"],
            "stop_method": dynamic_stop["method"],
            "reason": f"买入 {STOCK_MAP.get(code, {}).get('name', code)} {shares}股 @ {price:.2f}",
        }

    # ── 执行卖出 ──────────────────────────────────────────

    def execute_sell(self, code: str, reason: str = "") -> dict:
        """执行卖出（全部清仓），返回盈亏"""
        from data_engine import fetch_single

        stock = None
        for p in self.positions:
            if p["code"] == code:
                stock = p
                break
        if not stock:
            return {"status": "error", "reason": f"未持仓 {code}"}

        data = fetch_single(code)
        price = data.get("price", 0) if data else 0
        if price <= 0:
            rows = get_price_history(code, 1)
            price = float(rows[0]["close"]) if rows else 0

        buy_price = stock["buy_price"]
        amount = stock.get("trade_amount", 0) or 0
        shares = int(amount / buy_price / 100) * 100 if buy_price > 0 and amount > 0 else 0
        sell_value = shares * price
        cost = shares * buy_price
        fee = sell_value * (self.commission_rate + self.stamp_tax_rate)
        net_profit = sell_value - cost - fee
        profit_pct = (price - buy_price) / buy_price * 100

        today = date.today().isoformat()
        clear_active(code)

        # 记录风险事件：亏损卖出 → 连续亏损计数 + 黑名单
        try:
            from risk_manager import get_risk_manager
            risk = get_risk_manager()
            if profit_pct < 0:
                risk.record_loss(code, profit_pct)
                risk.record_stop_loss(code)
            else:
                risk.reset_consecutive_losses()
        except Exception:
            pass

        add_trade(code, "sell", price, shares, today,
                  f"卖出: {reason} (买入{buy_price:.2f})",
                  trade_amount=sell_value)

        return {
            "status": "sell",
            "code": code,
            "name": STOCK_MAP.get(code, {}).get("name", code),
            "sell_price": price,
            "buy_price": buy_price,
            "shares": shares,
            "sell_value": round(sell_value, 2),
            "cost": round(cost, 2),
            "fee": round(fee, 2),
            "profit_pct": round(profit_pct, 2),
            "net_profit": round(net_profit, 2),
            "reason": f"卖出 {STOCK_MAP.get(code, {}).get('name', code)}: {reason}",
        }

    # ── 止盈止损检查 ──────────────────────────────────────

    def check_stop_conditions(self, force_check: bool = False) -> list[dict]:
        """
        检查所有持仓的止盈止损条件
        返回触发的操作建议
        """
        from data_engine import fetch_realtime

        positions = self.positions
        if not positions:
            return []

        codes = [p["code"] for p in positions]
        realtime = fetch_realtime(codes)
        rt_map = {r["code"]: r for r in realtime}

        actions = []
        for p in positions:
            code = p["code"]
            rt = rt_map.get(code, {})
            price = rt.get("price", 0)
            if price <= 0:
                continue

            buy_price = p["buy_price"]
            profit_pct = (price - buy_price) / buy_price

            name = STOCK_MAP.get(code, {}).get("name", code)

            # 动态止损（ATR 或固定）
            from signal_engine import get_dynamic_stop_loss
            dynamic_stop = get_dynamic_stop_loss(code, buy_price)
            actual_stop_pct = dynamic_stop["stop_pct"]

            if profit_pct <= actual_stop_pct:
                actions.append({
                    "action": "SELL_STOP",
                    "code": code,
                    "name": name,
                    "price": price,
                    "profit_pct": round(profit_pct * 100, 2),
                    "reason": f"止损触发 ({dynamic_stop['method']}): {profit_pct*100:.1f}% ≤ {actual_stop_pct*100:.0f}%",
                    "urgency": "critical",
                })
                continue

            # 止盈分档
            for target_pct, exit_ratio in self.profit_take_levels:
                if profit_pct >= target_pct:
                    label = f"T{int(target_pct*100)}"
                    actions.append({
                        "action": f"SELL_PARTIAL_{label}",
                        "code": code,
                        "name": name,
                        "price": price,
                        "profit_pct": round(profit_pct * 100, 2),
                        "exit_ratio": exit_ratio,
                        "reason": f"止盈 {label}: {profit_pct*100:.1f}% ≥ {target_pct*100:.0f}%, 建议出 {exit_ratio*100:.0f}%",
                        "urgency": "high" if target_pct >= 0.3 else "medium",
                    })
                    break  # 只触发最高档

        return actions

    # ── 移动止盈追踪 ──────────────────────────────────────

    def _load_peaks_from_db(self):
        """从数据库加载持久化的峰值价格"""
        try:
            from db import get_conn
            conn = get_conn()
            rows = conn.execute(
                'SELECT code, buy_price, peak_price FROM stocks WHERE is_active = 1'
            ).fetchall()
            conn.close()
            for r in rows:
                self._entry_prices[r["code"]] = r["buy_price"]
                db_peak = r["peak_price"] or 0
                self._peak_prices[r["code"]] = max(db_peak, r["buy_price"])
        except Exception:
            pass

    def _save_peak_to_db(self, code: str, peak: float):
        """持久化单只标的的峰值价格"""
        try:
            from db import get_conn
            conn = get_conn()
            conn.execute('UPDATE stocks SET peak_price = ? WHERE code = ?', (peak, code))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def update_peaks(self, price_map: dict[str, float]):
        """更新持仓的最高价记录（每日调价时调用）"""
        positions = self.positions
        for p in positions:
            code = p["code"]
            entry_price = p["buy_price"]
            current = price_map.get(code, 0)
            if current <= 0:
                continue
            self._entry_prices[code] = entry_price
            if code not in self._peak_prices:
                self._peak_prices[code] = current
            else:
                self._peak_prices[code] = max(self._peak_prices[code], current)
            # 持久化峰值价格，防止进程重启丢失
            self._save_peak_to_db(code, self._peak_prices[code])

    def get_trailing_stop_levels(self) -> list[dict]:
        """计算所有持仓的移动止盈位"
        从最高点回撤 trailing_stop_pct → 触发止盈
        """
        from data_engine import fetch_realtime
        positions = self.positions
        if not positions:
            return []

        codes = [p["code"] for p in positions]
        realtime = fetch_realtime(codes)
        rt_map = {r["code"]: r for r in realtime}

        self.update_peaks({code: rt_map.get(code, {}).get("price", 0) for code in codes})

        results = []
        for p in positions:
            code = p["code"]
            entry = self._entry_prices.get(code, p["buy_price"])
            peak = self._peak_prices.get(code, 0)
            current = rt_map.get(code, {}).get("price", 0)

            if current <= 0 or peak <= 0:
                continue

            name = STOCK_MAP.get(code, {}).get("name", code)
            profit_pct = (current - entry) / entry * 100
            peak_profit_pct = (peak - entry) / entry * 100
            drawdown_from_peak = (current - peak) / peak * 100 if peak > 0 else 0
            trailing_trigger = self.trailing_stop_pct * -1  # 如拉回12%

            results.append({
                "code": code,
                "name": name,
                "current": current,
                "entry": entry,
                "peak": peak,
                "profit_pct": round(profit_pct, 2),
                "peak_profit_pct": round(peak_profit_pct, 2),
                "drawdown_from_peak": round(drawdown_from_peak, 2),
                "trailing_trigger_pct": round(trailing_trigger * 100, 1),
                "trailing_triggered": drawdown_from_peak <= trailing_trigger,
                "exceeds_profit_take1": profit_pct >= RISK_CONFIG["profit_take_level1"] * 100,
                "exceeds_profit_take2": profit_pct >= RISK_CONFIG["profit_take_level2"] * 100,
            })

        return results

    # ── 持仓操作建议 ──────────────────────────────────────

    def get_position_advice(self, signals: list[dict]) -> list[dict]:
        """为每只持仓生成具体操作建议"
        基于信号评分 + 止盈止损 + 移动止盈
        """
        trailing = self.get_trailing_stop_levels()
        trailing_map = {t["code"]: t for t in trailing}
        signal_map = {s["code"]: s for s in signals}
        positions = self.positions

        advice = []
        for p in positions:
            code = p["code"]
            name = STOCK_MAP.get(code, {}).get("name", code)
            ts = trailing_map.get(code, {})
            sig = signal_map.get(code, {})
            score = sig.get("total_score", 50)

            # 计算动态止损百分比
            buy_price = p["buy_price"]
            from signal_engine import get_dynamic_stop_loss
            _ds = get_dynamic_stop_loss(code, buy_price)
            dynamic_stop_pct = _ds["stop_pct"] * 100  # 转为百分比，用于和 profit_pct 比较
            stop_method = _ds["method"]

            # 判断建议
            action = "HOLD"
            reason = []

            # 移动止盈触发
            if ts.get("trailing_triggered"):
                action = "SELL_TRAILING"
                reason.append(f"移动止盈触发: 从高点回落{abs(ts['drawdown_from_peak']):.1f}%")

            # 动态止损触发
            if ts.get("profit_pct", 0) <= dynamic_stop_pct:
                action = "STOP_LOSS"
                reason.append(f"止损 ({stop_method}): 亏损{ts['profit_pct']:.1f}% ≤ {dynamic_stop_pct:.0f}%")

            # 止盈触发
            if ts.get("exceeds_profit_take2"):
                action = "SELL_PROFIT_TAKE"
                reason.append(f"止盈二档: +{ts['profit_pct']:.1f}% ≥ +30%，建议清仓")
            elif ts.get("exceeds_profit_take1") and action == "HOLD":
                action = "SELL_PARTIAL"
                reason.append(f"止盈一档: +{ts['profit_pct']:.1f}% ≥ +15%，建议减半")

            # 评分低 + 没触发别的 → 关注
            if action == "HOLD" and score < 55:
                action = "WEAK_HOLD"
                reason.append(f"信号转弱(评分{score:.0f})，关注反弹力度")
            elif action == "HOLD" and score >= 62:
                action = "STRONG_HOLD"
                reason.append(f"信号强劲(评分{score:.0f})，继续持有")

            # 可加仓判断
            if score >= 60 and ts.get("profit_pct", 0) < 5:
                action_add = "CONSIDER_ADD"
                reason.append("可考虑分批加仓")

            advice.append({
                "code": code,
                "name": name,
                "action": action,
                "score": score,
                "profit_pct": ts.get("profit_pct", 0),
                "peak_profit_pct": ts.get("peak_profit_pct", 0),
                "drawdown": ts.get("drawdown_from_peak", 0),
                "reasons": reason,
                "urgency": "high" if action in ("SELL_TRAILING","STOP_LOSS") else "medium" if "SELL" in action else "low",
            })

        return advice

    # ── 目标追踪 ──────────────────────────────────────────

    def get_target_tracker(self) -> dict:
        """
        追踪距离 10 万目标还有多远
        """
        pv = self.get_portfolio_value()
        current = pv["total_value"]
        remaining = self.target_capital - current
        progress_pct = (current - self.initial_capital) / (self.target_capital - self.initial_capital) * 100

        # 时间进度
        from datetime import date
            # dateutil not needed; using simple day math
        # 估算：假设从第一笔交易日期算起
        conn = get_conn()
        first_trade = conn.execute("SELECT MIN(date) as d FROM trades").fetchone()
        conn.close()
        if first_trade and first_trade["d"]:
            start = date.fromisoformat(first_trade["d"])
        else:
            start = date.today()
        days_elapsed = (date.today() - start).days
        days_total = self.target_months * 30
        time_pct = min(100, days_elapsed / days_total * 100) if days_total > 0 else 0

        # 所需月收益率
        if remaining > 0:
            months_left = max(0.5, (days_total - max(days_elapsed, 1)) / 30)
            required_monthly_return = ((self.target_capital / current) ** (1 / months_left) - 1) * 100
        else:
            required_monthly_return = 0

        return {
            "initial_capital": self.initial_capital,
            "current_value": current,
            "target_capital": self.target_capital,
            "remaining": round(remaining, 2),
            "progress_pct": round(progress_pct, 1),
            "days_elapsed": days_elapsed,
            "days_total": days_total,
            "time_pct": round(time_pct, 1),
            "required_monthly_return": round(required_monthly_return, 1),
        }

    # ── 格式化输出 ──────────────────────────────────────────

    def format_portfolio(self) -> str:
        """格式化的投资组合仪表盘"""
        pv = self.get_portfolio_value()
        target = self.get_target_tracker()

        lines = []
        lines.append("=" * 55)
        lines.append(f"  📊 Serenity 投资组合 | {date.today()}")
        lines.append("=" * 55)

        # 资金概览
        lines.append(f"\n💰 资金概览")
        lines.append(f"   总资产: {pv['total_value']:.2f} 元")
        lines.append(f"   可用现金: {pv['cash']:.2f} 元")
        lines.append(f"   持仓市值: {pv['holdings_value']:.2f} 元")
        lines.append(f"   总盈亏: {pv['total_profit_pct']:+.2f}% ({pv['total_profit_amount']:+.2f} 元)")

        # 目标追踪
        lines.append(f"\n🎯 目标追踪 (5 万 → 10 万 / 3 个月)")
        lines.append(f"   进度: {target['progress_pct']:.1f}% | "
                     f"时间: {target['time_pct']:.1f}% ({target['days_elapsed']}/{target['days_total']}天)")
        if target['remaining'] > 0:
            lines.append(f"   还需: {target['remaining']:.0f} 元 | "
                         f"需月收益: {target['required_monthly_return']:+.1f}%/月")
        else:
            lines.append(f"   🎉 已达成目标!")

        # 持仓明细
        if pv.get("positions"):
            lines.append(f"\n📈 持仓明细 ({pv['position_count']} 只)")
            lines.append(f"   {'代码':>6} {'名称':<8} {'买入价':>8} {'现价':>8} {'盈亏%':>8} {'盈亏额':>10} {'仓位':>6}")
            lines.append(f"   {'-'*56}")
            for pos in pv["positions"]:
                emoji = "🟢" if pos["profit_pct"] >= 0 else "🔴"
                lines.append(f"   {emoji} {pos['code']:>6} {pos['name']:<8} "
                             f"{pos['buy_price']:>8.2f} {pos['current_price']:>8.2f} "
                             f"{pos['profit_pct']:>+7.2f}% {pos['profit_amount']:>+9.2f} "
                             f"{pos['weight']:>5.1f}%")
        else:
            lines.append(f"\n📭 当前无持仓")

        # 止盈止损检查
        stop_actions = self.check_stop_conditions()
        if stop_actions:
            lines.append(f"\n⚠️ 止盈止损触发:")
            for a in stop_actions:
                icon = "🔴" if "STOP" in a["action"] else "🟢"
                lines.append(f"   {icon} [{a['urgency'].upper()}] {a['name']}: {a['reason']}")

        # 移动止盈追踪
        trailing = self.get_trailing_stop_levels()
        for t in trailing:
            if t["profit_pct"] > 5:  # 只显示浮盈 > 5% 的
                lines.append(f"\n📈 移动止盈: {t['name']}({t['code']})")
                lines.append(f"   浮盈 +{t['profit_pct']:.1f}% | 最高 +{t['peak_profit_pct']:.1f}%")
                lines.append(f"   从高点回撤 {abs(t['drawdown_from_peak']):.1f}% | "
                             f"移动止盈线 {t['trailing_trigger_pct']:.0f}% 回撤")
                if t["trailing_triggered"]:
                    lines.append(f"   🔴 移动止盈已触发！建议卖出")

        lines.append("\n" + "=" * 55)
        return "\n".join(lines)

    def format_signal_summary(self, signals: list[dict]) -> str:
        """格式化信号输出"""
        lines = []
        lines.append("=" * 55)
        lines.append(f"  📡 Serenity 交易信号 | {date.today()}")
        lines.append("=" * 55)
        lines.append("")

        if not signals:
            lines.append("  暂无信号输出")
            lines.append("\n" + "=" * 55)
            return "\n".join(lines)

        # 按信号强度排序
        signals_sorted = sorted(signals, key=lambda s: s.get("total_score", 0), reverse=True)

        for i, sig in enumerate(signals_sorted):
            action = sig.get("action", "HOLD")
            score = sig.get("total_score", 0)
            name = sig.get("name", sig.get("code", "?"))
            code = sig.get("code", "?")

            # 信号图标
            if action == "STRONG_BUY":
                icon = "🟢🟢🟢"
            elif action == "BUY":
                icon = "🟢🟢"
            elif action == "CAUTION_BUY":
                icon = "🟢"
            elif action == "HOLD":
                icon = "⚪"
            elif action == "SELL":
                icon = "🔴🔴"
            elif action == "STOP_LOSS":
                icon = "🔴🔴🔴"
            else:
                icon = "⚪"

            lines.append(f"  {icon} #{i+1} {name} ({code}) — {action}")
            lines.append(f"     综合评分: {score:.1f}")
            lines.append(f"     现价: {sig.get('price', 'N/A')} | "
                         f"买入区: {sig.get('buy_zone', 'N/A')} | "
                         f"目标: {sig.get('target_sell', 'N/A')}")

            # 建议仓位
            if "suggested_amount" in sig:
                lines.append(f"     建议仓位: {sig['suggested_amount']:.0f}元 "
                             f"({sig.get('suggested_shares', 0)}股 @ ~{sig.get('price', 0):.2f})")

            # 原因
            if sig.get("reason"):
                lines.append(f"     {sig['reason']}")

            lines.append("")

        lines.append("=" * 55)
        lines.append(f"  共 {len(signals)} 个信号 | 可用资金: {self.get_cash():.0f} 元")
        lines.append("=" * 55)
        return "\n".join(lines)


# 全局单例
_portfolio_instance = None


def get_portfolio() -> PortfolioManager:
    global _portfolio_instance
    if _portfolio_instance is None:
        _portfolio_instance = PortfolioManager()
    return _portfolio_instance
