"""
纸面交易模拟引擎 — 无风险验证层
从当前真实持仓克隆模拟账户，追踪模拟 P&L，对比模拟 vs 实际
"""
from datetime import date, datetime
from typing import Optional

from db import get_conn
from config import CAPITAL_CONFIG, STOCK_MAP
from data_engine import fetch_realtime
from serenity_logger import get_logger

log = get_logger(__name__)

PAPER_INITIAL_CAPITAL = CAPITAL_CONFIG.get("initial_capital", 50000)


def init_paper_tables():
    """在 DB 中创建纸面交易相关表（幂等）"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            date TEXT NOT NULL,
            reason TEXT DEFAULT '',
            trade_amount REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS paper_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL DEFAULT 'TOTAL',
            shares INTEGER DEFAULT 0,
            avg_cost REAL DEFAULT 0,
            price REAL DEFAULT 0,
            current_value REAL NOT NULL,
            pnl REAL DEFAULT 0,
            pnl_pct REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
    """)
    conn.commit()
    conn.close()


class PaperTrader:
    """纸面交易账户 — 独立于真实账户运行"""

    def __init__(self):
        init_paper_tables()
        self.initial_capital = PAPER_INITIAL_CAPITAL
        try:
            self._ensure_seeded()
        except Exception as e:
            log.warning("纸面账户初始化失败（将使用空白账户）: %s", e)

    def _ensure_seeded(self):
        """如果没有纸面持仓，从真实账户克隆初始状态"""
        conn = get_conn()
        count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        conn.close()
        if count > 0:
            return  # Already seeded

        # Clone from real portfolio
        from portfolio import PortfolioManager
        pm = PortfolioManager()
        try:
            pv = pm.get_portfolio_value()
        except Exception as e:
            log.warning("无法获取真实组合数据用于种子: %s", e)
            pv = {"cash": self.initial_capital, "positions": []}

        conn = get_conn()
        today = date.today().isoformat()

        # Seed cash
        conn.execute(
            "INSERT INTO paper_trades (code, action, price, quantity, date, reason, trade_amount) VALUES (?,?,?,?,?,?,?)",
            ("CASH", "buy", pv["cash"], 1, today, "种子资金", pv["cash"])
        )

        # Seed positions
        for pos in pv.get("positions", []):
            try:
                conn.execute(
                    "INSERT INTO paper_trades (code, action, price, quantity, date, reason, trade_amount) VALUES (?,?,?,?,?,?,?)",
                    (pos["code"], "buy", pos.get("buy_price", 0), pos.get("shares", 0), today,
                     "种子持仓", pos.get("shares", 0) * pos.get("buy_price", 0))
                )
            except Exception:
                continue

        conn.commit()
        conn.close()
        log.info("纸面账户已初始化: %.0f 现金 + %d 只持仓", pv["cash"], len(pv.get("positions", [])))

    # ── 核心查询 ──────────────────────────────────────────

    def get_paper_cash(self) -> float:
        conn = get_conn()
        cash_row = conn.execute(
            "SELECT price FROM paper_trades WHERE code='CASH' AND action='sell' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not cash_row:
            cash_row = conn.execute(
                "SELECT price FROM paper_trades WHERE code='CASH' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()

        if cash_row and cash_row["price"] > 0:
            result = cash_row["price"]
        else:
            result = self.initial_capital

        # Formula: initial - sum(buys) + sum(sells)
        rows = conn.execute("SELECT action, price, quantity, trade_amount FROM paper_trades WHERE code != 'CASH'").fetchall()
        bought = sum(
            (r["trade_amount"] or r["price"] * r["quantity"])
            for r in rows if r["action"] == "buy"
        )
        sold = sum(
            (r["trade_amount"] or r["price"] * r["quantity"])
            for r in rows if r["action"] == "sell"
        )
        conn.close()

        formula_cash = self.initial_capital - bought + sold
        return max(formula_cash, result, 0)

    def _get_net_shares(self, code: str) -> int:
        conn = get_conn()
        bought = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM paper_trades WHERE code=? AND action='buy'", (code,)
        ).fetchone()[0]
        sold = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM paper_trades WHERE code=? AND action='sell'", (code,)
        ).fetchone()[0]
        conn.close()
        return max(bought - sold, 0)

    def get_paper_positions(self) -> list[dict]:
        conn = get_conn()
        codes = conn.execute(
            "SELECT DISTINCT code FROM paper_trades WHERE code != 'CASH' AND action='buy'"
        ).fetchall()
        conn.close()

        positions = []
        rt_map = {}
        if codes:
            try:
                realtime = fetch_realtime([r["code"] for r in codes])
                rt_map = {r["code"]: r for r in realtime} if realtime else {}
            except Exception:
                pass  # Use avg_cost as current price

        for row in codes:
            code = row["code"]
            shares = self._get_net_shares(code)
            if shares <= 0:
                continue

            # Calculate average cost from paper trades
            conn = get_conn()
            cost_rows = conn.execute(
                "SELECT price, quantity, trade_amount FROM paper_trades WHERE code=? AND action='buy' ORDER BY rowid", (code,)
            ).fetchall()
            conn.close()

            total_cost = sum(
                (r["trade_amount"] or r["price"] * r["quantity"])
                for r in cost_rows
            )
            total_shares_bought = sum(r["quantity"] for r in cost_rows)
            avg_cost = total_cost / total_shares_bought if total_shares_bought > 0 else 0

            rt = rt_map.get(code, {})
            current_price = rt.get("price", avg_cost)
            current_value = shares * current_price
            profit_pct = (current_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0

            positions.append({
                "code": code,
                "name": STOCK_MAP.get(code, {}).get("name", code),
                "avg_cost": round(avg_cost, 2),
                "current_price": current_price,
                "shares": shares,
                "current_value": round(current_value, 2),
                "profit_pct": round(profit_pct, 2),
                "profit_amount": round(current_value - shares * avg_cost, 2),
            })

        return positions

    def get_paper_portfolio(self) -> dict:
        """获取模拟组合摘要"""
        cash = self.get_paper_cash()
        positions = self.get_paper_positions()
        holdings_value = sum(p["current_value"] for p in positions)
        total_value = cash + holdings_value
        total_profit_pct = (total_value - self.initial_capital) / self.initial_capital * 100

        return {
            "cash": round(cash, 2),
            "holdings_value": round(holdings_value, 2),
            "total_value": round(total_value, 2),
            "total_profit_pct": round(total_profit_pct, 2),
            "total_profit_amount": round(total_value - self.initial_capital, 2),
            "position_count": len(positions),
            "positions": positions,
        }

    # ── 模拟交易 ──────────────────────────────────────────

    def execute_signal(self, code: str, action: str, price: float, shares: int = 0,
                       amount: float = 0, reason: str = "") -> dict:
        """执行一笔模拟交易"""
        today = date.today().isoformat()
        if action == "buy":
            if shares <= 0 and amount > 0 and price > 0:
                shares = int(amount / price / 100) * 100
            if shares < 100:
                return {"status": "error", "reason": f"最小100股起买，需{shares}股"}
            trade_amount = shares * price
            cash = self.get_paper_cash()
            if trade_amount > cash:
                return {"status": "error", "reason": f"现金不足: 需{trade_amount:.0f}，有{cash:.0f}"}
        elif action == "sell":
            net_shares = self._get_net_shares(code)
            if net_shares <= 0:
                return {"status": "error", "reason": f"无{code}纸面持仓"}
            if shares <= 0 or shares > net_shares:
                shares = net_shares
            trade_amount = shares * price
        else:
            return {"status": "error", "reason": f"未知动作: {action}"}

        conn = get_conn()
        conn.execute(
            "INSERT INTO paper_trades (code, action, price, quantity, date, reason, trade_amount) VALUES (?,?,?,?,?,?,?)",
            (code, action, price, shares, today, reason[:200], trade_amount)
        )

        # Update cash
        if action == "buy":
            new_cash = self.get_paper_cash() - trade_amount
        else:
            new_cash = self.get_paper_cash() + trade_amount
        conn.execute(
            "INSERT INTO paper_trades (code, action, price, quantity, date, reason, trade_amount) VALUES (?,?,?,?,?,?,?)",
            ("CASH", "sell", max(new_cash, 0), 1, today, f"{action} {code}", new_cash)
        )
        conn.commit()
        conn.close()

        name = STOCK_MAP.get(code, {}).get("name", code)
        log.info("纸面%s: %s(%s) %d股 @%.2f", action, name, code, shares, price)

        return {
            "status": action,
            "code": code,
            "name": name,
            "price": price,
            "shares": shares,
            "amount": trade_amount,
            "reason": reason,
        }

    # ── 速度模拟 ──────────────────────────────────────────

    def fast_forward_signal(self, code: str, action: str, score: float = 0) -> dict:
        """快速模拟：以当前市价执行信号"""
        realtime = fetch_realtime([code])
        if not realtime:
            return {"status": "error", "reason": f"无法获取{code}行情"}

        price = realtime[0].get("price", 0)
        if price <= 0:
            return {"status": "error", "reason": f"{code}无有效价格"}

        # Use score-based sizing
        confidence = min(0.9, max(0.1, score / 100))
        if action in ("BUY", "STRONG_BUY", "CAUTION_BUY"):
            cash = self.get_paper_cash()
            kelly = 0.1 + confidence * 0.4
            amount = cash * kelly
            return self.execute_signal(code, "buy", price, amount=amount,
                                       reason=f"评分{score:.0f}, 信度{confidence:.0%}")
        elif action in ("SELL", "STOP_LOSS"):
            return self.execute_signal(code, "sell", price,
                                       reason=f"卖出信号: {action}")

        return {"status": "skip", "reason": f"不支持的操作: {action}"}

    # ── 对比分析 ──────────────────────────────────────────

    def compare_to_real(self) -> dict:
        """模拟 vs 实际对比"""
        from portfolio import PortfolioManager
        pm = PortfolioManager()
        try:
            real = pm.get_portfolio_value()
        except Exception:
            real = {"total_value": self.initial_capital, "total_profit_pct": 0,
                    "position_count": 0}
        paper = self.get_paper_portfolio()

        diff_total = paper["total_value"] - real["total_value"]
        diff_pct = paper["total_profit_pct"] - real["total_profit_pct"]

        return {
            "real_total": real["total_value"],
            "paper_total": paper["total_value"],
            "diff_amount": round(diff_total, 2),
            "real_return": real["total_profit_pct"],
            "paper_return": paper["total_profit_pct"],
            "diff_return": round(diff_pct, 2),
            "real_positions": real["position_count"],
            "paper_positions": paper["position_count"],
        }

    def get_stats(self) -> dict:
        """模拟交易统计"""
        conn = get_conn()
        trades = conn.execute(
            "SELECT action, COUNT(*) as cnt, SUM(trade_amount) as total_amount FROM paper_trades WHERE code != 'CASH' GROUP BY action"
        ).fetchall()
        conn.close()

        buys = sells = 0
        buy_amt = sell_amt = 0.0
        for t in trades:
            if t["action"] == "buy":
                buys = t["cnt"]
                buy_amt = t["total_amount"] or 0
            elif t["action"] == "sell":
                sells = t["cnt"]
                sell_amt = t["total_amount"] or 0

        return {
            "total_trades": buys + sells,
            "buys": buys,
            "sells": sells,
            "buy_amount": round(buy_amt, 2),
            "sell_amount": round(sell_amt, 2),
        }

    # ── 🆕 v3.2 自动纸面交易 ──────────────────────────────

    def auto_execute_signals(self, max_buy: int = 2) -> dict:
        """基于当前评分自动执行纸面买入（不卖，模拟建仓）

        每次最多买 max_buy 只，每只不超过可用现金的 40%。
        """
        from scorer import score_all
        results = score_all()
        cash = self.get_paper_cash()
        bought = 0
        trades = []

        held_codes = {p["code"] for p in self.get_paper_positions()}

        for r in results:
            if bought >= max_buy:
                break
            code = r["code"]
            if code in held_codes:
                continue
            action = r["signal_action"]
            score = r["total_score"]
            price = r["close"]

            if action not in ("STRONG_BUY", "BUY", "CAUTION_BUY") or score < 60:
                continue
            if price <= 0:
                continue

            max_spend = cash * 0.40
            shares = max(100, int(max_spend / price / 100) * 100)
            cost = shares * price
            if cost > cash:
                continue

            result = self.execute_signal(code, "buy", price, shares,
                                         reason=f"自动纸面 {action} {score:.0f}分")
            if result.get("status") == "buy":
                bought += 1
                cash -= cost
                trades.append({
                    "code": code, "name": r["name"],
                    "action": action, "shares": shares,
                    "price": price, "amount": cost,
                })

        return {
            "status": "auto_executed",
            "bought": bought,
            "remaining_cash": round(self.get_paper_cash(), 2),
            "trades": trades,
        }

    def reset(self) -> dict:
        """重置纸面账户"""
        conn = get_conn()
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM paper_snapshots")
        conn.commit()
        conn.close()
        self._ensure_seeded()
        return {"status": "reset", "msg": "纸面账户已重置"}

    # ── 🆕 v3.2 日终市值更新 ──────────────────────────────

    def mark_to_market(self) -> dict:
        """以当天收盘价更新所有纸面持仓的市值，写入 paper_snapshots"""
        today = date.today().isoformat()
        positions = self.get_paper_positions()
        if not positions:
            return {"status": "no_positions", "count": 0}

        codes = [p["code"] for p in positions]
        realtime = fetch_realtime(codes) if codes else []

        updated = 0
        total_value = self.get_paper_cash()
        conn = get_conn()

        for p in positions:
            code = p["code"]
            # 取实时价或快照
            rt = next((r for r in realtime if r.get("code") == code), {})
            price = rt.get("price", 0)
            if price <= 0:
                from db import get_latest_snapshot
                snap = get_latest_snapshot(code)
                price = snap.get("close", 0) if snap else 0
            if price <= 0:
                continue

            current_val = p["shares"] * price
            cost_val = p["shares"] * p["avg_cost"]
            pnl = current_val - cost_val
            pnl_pct = (price - p["avg_cost"]) / p["avg_cost"] * 100 if p["avg_cost"] > 0.01 else 0

            conn.execute(
                "INSERT INTO paper_snapshots (date, code, shares, avg_cost, price, current_value, pnl, pnl_pct) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (today, code, p["shares"], p["avg_cost"], price, current_val, round(pnl, 2), round(pnl_pct, 2)),
            )
            total_value += current_val
            updated += 1

        # 写总净值
        conn.execute(
            "INSERT INTO paper_snapshots (date, code, shares, avg_cost, price, current_value, pnl, pnl_pct) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (today, "TOTAL", 1, 0, total_value, total_value, total_value - self.initial_capital,
             round((total_value - self.initial_capital) / self.initial_capital * 100, 2)),
        )
        conn.commit()
        conn.close()

        return {
            "status": "marked",
            "date": today,
            "positions": updated,
            "total_value": round(total_value, 2),
            "total_pnl": round(total_value - self.initial_capital, 2),
            "total_pnl_pct": round((total_value - self.initial_capital) / self.initial_capital * 100, 2),
        }

    def get_paper_performance(self) -> dict:
        """返回纸面交易绩效汇总"""
        snapshots = []
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM paper_snapshots WHERE code='TOTAL' ORDER BY date"
        ).fetchall()
        conn.close()

        if not rows:
            return {"status": "empty", "current_value": self.initial_capital,
                    "pnl": 0, "pnl_pct": 0}

        latest = rows[-1]
        return {
            "status": "tracking",
            "start_value": self.initial_capital,
            "current_value": latest["current_value"],
            "pnl": round(latest["current_value"] - self.initial_capital, 2),
            "pnl_pct": round((latest["current_value"] - self.initial_capital) / self.initial_capital * 100, 2),
            "snapshots": [{"date": r["date"], "value": r["current_value"],
                           "pnl_pct": r["pnl_pct"]} for r in rows],
        }


# 全局单例
_paper_instance: Optional[PaperTrader] = None


def get_paper_trader() -> PaperTrader:
    global _paper_instance
    if _paper_instance is None:
        _paper_instance = PaperTrader()
    return _paper_instance
