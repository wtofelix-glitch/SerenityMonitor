"""测试 portfolio.py — PortfolioManager 资金计算、仓位管理和止盈止损"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

from datetime import date
from unittest.mock import ANY

import portfolio as portfolio_module
from portfolio import PortfolioManager, get_portfolio


# ============================================================
# 辅助函数
# ============================================================

def _make_stock(code: str, name: str, buy_price: float,
                trade_amount: float, is_active: int = 1, **kw) -> dict:
    """构造 stocks 表行 dict"""
    base = {
        "code": code, "name": name, "buy_price": buy_price,
        "trade_amount": trade_amount, "is_active": is_active,
        "buy_date": "2026-06-01", "tier": 2,
        "target_high": 0, "target_low": 0, "stop_loss": 0, "notes": "",
    }
    base.update(kw)
    return base


def _mock_trades_db(monkeypatch):
    """创建内存 SQLite DB，注入 trades 表，patch portfolio_module.get_conn"""
    import sqlite3
    import db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT, action TEXT, price REAL,
            quantity INTEGER, trade_amount REAL, date TEXT
        )
    """)
    conn.commit()
    monkeypatch.setattr(portfolio_module, 'get_conn', lambda: conn)
    monkeypatch.setattr(db, 'init_db', lambda: None)
    return conn


# ============================================================
# PortfolioManager 初始化
# ============================================================

class TestPortfolioManagerInit:
    """初始化和配置加载"""

    def test_initial_capital_default(self):
        pm = PortfolioManager()
        assert pm.initial_capital > 0

    def test_initial_capital_custom(self):
        pm = PortfolioManager(initial_capital=99999.99)
        assert pm.initial_capital == 99999.99

    def test_target_capital_from_config(self):
        pm = PortfolioManager()
        assert pm.target_capital > pm.initial_capital

    def test_max_positions_positive(self):
        pm = PortfolioManager()
        assert pm.max_positions > 0

    def test_reserve_cash_ratio_in_range(self):
        pm = PortfolioManager()
        assert 0 <= pm.reserve_cash_ratio < 1


# ============================================================
# get_cash — 可用现金计算
# ============================================================

class TestGetCash:
    """get_cash: 从 trades 表计算可用现金"""

    def _add_trade(self, conn, code, action, price, qty, amt):
        conn.execute(
            "INSERT INTO trades (code, action, price, quantity, trade_amount, date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, action, price, qty, amt, "2026-06-01"),
        )

    def test_empty_trades_returns_initial_capital(self, monkeypatch):
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        assert pm.get_cash() == 50000.0
        conn.close()

    def test_only_buys_deducts(self, monkeypatch):
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        self._add_trade(conn, "600487", "buy", 94.39, 200, 18878.0)
        assert pm.get_cash() == 50000 - 18878.0
        conn.close()

    def test_buy_and_sell_net(self, monkeypatch):
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        self._add_trade(conn, "600487", "buy", 94.39, 200, 18878.0)
        self._add_trade(conn, "600487", "sell", 105.02, 200, 21004.0)
        assert pm.get_cash() == 50000 - 18878.0 + 21004.0
        conn.close()

    def test_invalid_record_skipped(self, monkeypatch):
        """trade_amount=0 且 quantity=0 的记录跳过"""
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        self._add_trade(conn, "600487", "buy", 0, 0, 0)   # 无效
        self._add_trade(conn, "600487", "buy", 94.39, 200, 18878.0)  # 有效
        assert pm.get_cash() == 50000 - 18878.0
        conn.close()

    def test_trade_amount_fallback_to_price_qty(self, monkeypatch):
        """trade_amount=0 时用 price * quantity 回退"""
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        self._add_trade(conn, "600487", "buy", 100.0, 200, 0)
        assert pm.get_cash() == 50000 - 20000.0
        conn.close()

    def test_negative_sell_amount_skipped(self, monkeypatch):
        """sell 记录中负数金额（CASH 校准）跳过"""
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        self._add_trade(conn, "CASH", "sell", 0, 0, -1000.0)  # 校准记录
        self._add_trade(conn, "600487", "buy", 94.39, 200, 18878.0)
        self._add_trade(conn, "600487", "sell", 105.02, 200, 21004.0)
        assert pm.get_cash() == 50000 - 18878.0 + 21004.0
        conn.close()

    def test_db_exception_fallback(self, monkeypatch):
        """get_conn 异常时返回 initial_capital"""
        class BrokenConn:
            def execute(self, sql, params=None):
                raise RuntimeError("query error")
            def close(self):
                pass
        monkeypatch.setattr(portfolio_module, 'get_conn', lambda: BrokenConn())
        pm = PortfolioManager(initial_capital=50000)
        assert pm.get_cash() == 50000.0

    def test_multiple_buys_and_sells(self, monkeypatch):
        conn = _mock_trades_db(monkeypatch)
        pm = PortfolioManager(initial_capital=100000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        self._add_trade(conn, "600036", "buy", 35.0, 300, 10500.0)
        self._add_trade(conn, "600487", "buy", 94.39, 200, 18878.0)
        self._add_trade(conn, "600036", "sell", 38.5, 300, 11550.0)
        assert pm.get_cash() == 100000 - 10500.0 - 18878.0 + 11550.0
        conn.close()


# ============================================================
# positions — 当前持仓
# ============================================================

class TestPositions:
    """positions 属性：从 DB 读取活跃持仓"""

    def test_no_positions_returns_empty(self, monkeypatch):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: [])
        pm = PortfolioManager()
        assert pm.positions == []
        assert pm.position_codes == []

    def test_only_active_returned(self, monkeypatch):
        stocks = [
            _make_stock("600487", "亨通光电", 94.39, 18878.0, is_active=1,
                        buy_date="2026-06-08"),
            _make_stock("600036", "招商银行", 35.0, 0, is_active=0,
                        buy_date="2026-06-01"),
        ]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        pm = PortfolioManager()
        assert len(pm.positions) == 1
        assert pm.positions[0]["code"] == "600487"
        assert pm.position_codes == ["600487"]


# ============================================================
# get_portfolio_value — 组合估值
# ============================================================

class TestGetPortfolioValue:
    """get_portfolio_value: 总资产 = 现金 + 持仓市值"""

    def _mock_stocks_and_realtime(self, monkeypatch, stocks, price_map):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(
            portfolio_module, 'fetch_realtime',
            lambda codes: [{"code": k, "price": v} for k, v in price_map.items()],
        )

    def test_no_positions_returns_cash_only(self, monkeypatch):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: [])
        conn = _mock_trades_db(monkeypatch)  # 避免真实 trades 数据干扰
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        pv = pm.get_portfolio_value()
        assert pv["holdings_value"] == 0.0
        assert pv["position_count"] == 0
        assert pv["total_value"] == 50000.0
        assert pv["total_profit_pct"] == 0.0

    def test_single_position_value(self, monkeypatch):
        stocks = [_make_stock("600487", "亨通光电", 94.39, 18878.0)]
        self._mock_stocks_and_realtime(monkeypatch, stocks, {"600487": 105.02})
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        pv = pm.get_portfolio_value()
        assert pv["position_count"] == 1
        assert round(pv["holdings_value"], 0) == 21004.0
        assert pv["total_value"] == pv["cash"] + pv["holdings_value"]

    def test_buy_price_zero_does_not_crash(self, monkeypatch):
        stocks = [_make_stock("600487", "亨通光电", 0, 0)]
        self._mock_stocks_and_realtime(monkeypatch, stocks, {"600487": 100.0})
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        pv = pm.get_portfolio_value()
        assert pv["holdings_value"] == 0.0
        assert pv["position_count"] == 1

    def test_position_detail_keys(self, monkeypatch):
        stocks = [_make_stock("600487", "亨通光电", 94.39, 18878.0)]
        self._mock_stocks_and_realtime(monkeypatch, stocks, {"600487": 105.02})
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        pv = pm.get_portfolio_value()
        pos = pv["positions"][0]
        assert "code" in pos and "name" in pos
        assert "buy_price" in pos and "current_price" in pos
        assert "shares" in pos and "profit_pct" in pos
        assert pos["code"] == "600487"
        assert pos["profit_pct"] > 0


# ============================================================
# calc_position_size — 仓位计算
# ============================================================

class TestCalcPositionSize:
    """calc_position_size: Kelly 仓位大小"""

    def test_return_dict_keys(self, monkeypatch):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: [])
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 50000)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes=None: [{"code": "600487", "price": 100.0}])
        monkeypatch.setattr(portfolio_module, 'get_price_history',
                            lambda code, days: [{"close": 100.0}])

        sizing = pm.calc_position_size("600487", 0.6)
        assert "shares" in sizing
        assert "amount" in sizing
        assert "price" in sizing
        assert "reason" in sizing

    def test_max_positions_blocked(self, monkeypatch):
        stocks = [
            _make_stock("600036", "招商银行", 35.0, 10500.0),
            _make_stock("600487", "亨通光电", 94.39, 18878.0),
        ]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 20000)
        result = pm.calc_position_size("002281", 0.5)
        assert result["shares"] == 0
        assert "最大持仓数" in result["reason"]


# ============================================================
# execute_sell — 卖出执行
# ============================================================

class TestExecuteSell:
    """execute_sell: 清仓卖出"""

    def test_stock_not_found(self, monkeypatch):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: [])
        pm = PortfolioManager()
        result = pm.execute_sell("999999")
        assert result["status"] == "error"
        assert "未持仓" in result["reason"]

    def test_successful_sell(self, monkeypatch, tmp_path):
        """在临时目录中执行完整卖出流程"""
        import os
        import db
        os.chdir(tmp_path)
        db.init_db()

        stocks = [_make_stock("600487", "亨通光电", 94.39, 18878.0,
                              buy_date="2026-06-08")]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes: [{"code": "600487", "price": 105.02}])

        pm = PortfolioManager(initial_capital=50000)
        result = pm.execute_sell("600487")
        assert result["status"] == "sell"
        assert result["shares"] == 200
        assert result["profit_pct"] > 0

    def test_sell_loss(self, monkeypatch, tmp_path):
        """亏损卖出返回负盈亏"""
        import os
        import db
        os.chdir(tmp_path)
        db.init_db()

        stocks = [_make_stock("600036", "招商银行", 40.0, 12000.0)]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes: [{"code": "600036", "price": 35.0}])

        pm = PortfolioManager(initial_capital=100000)
        result = pm.execute_sell("600036")
        assert result["status"] == "sell"
        assert result["profit_pct"] < 0


# ============================================================
# check_stop_conditions — 止盈止损检查
# ============================================================

class TestCheckStopConditions:
    """check_stop_conditions: 止盈止损触发检查"""

    def test_no_actions_when_no_positions(self, monkeypatch):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: [])
        pm = PortfolioManager()
        assert pm.check_stop_conditions() == []

    def test_stop_loss_triggered(self, monkeypatch):
        """亏损 >= 止损线时触发 SELL_STOP"""
        stocks = [_make_stock("600036", "招商银行", 100.0, 10000.0)]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes: [{"code": "600036", "price": 70.0}])

        pm = PortfolioManager()
        actions = pm.check_stop_conditions()
        stop_actions = [a for a in actions if a["action"] == "SELL_STOP"]
        assert len(stop_actions) >= 1

    def test_take_profit_triggered(self, monkeypatch):
        """盈利 >= 止盈线时触发操作"""
        stocks = [_make_stock("600487", "亨通光电", 50.0, 10000.0)]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes: [{"code": "600487", "price": 65.0}])

        pm = PortfolioManager()
        actions = pm.check_stop_conditions()
        assert len(actions) >= 1

    def test_no_action_within_normal_range(self, monkeypatch):
        stocks = [_make_stock("600487", "亨通光电", 100.0, 10000.0)]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes: [{"code": "600487", "price": 102.0}])

        pm = PortfolioManager()
        actions = pm.check_stop_conditions()
        assert actions == []


# ============================================================
# format_portfolio — 组合格式化输出
# ============================================================

class TestFormatPortfolio:
    """format_portfolio: 格式化输出"""

    def test_returns_string_empty(self, monkeypatch):
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: [])
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 50000)
        text = pm.format_portfolio()
        assert "无持仓" in text
        assert "50000" in text

    def test_returns_string_with_positions(self, monkeypatch):
        stocks = [_make_stock("600487", "亨通光电", 94.39, 18878.0)]
        monkeypatch.setattr(portfolio_module, 'load_all_stocks', lambda: stocks)
        monkeypatch.setattr(portfolio_module, 'fetch_realtime',
                            lambda codes: [{"code": "600487", "price": 105.02}])
        pm = PortfolioManager(initial_capital=50000)
        monkeypatch.setattr(pm, 'get_actual_cash', lambda: 0)
        text = pm.format_portfolio()
        assert "亨通光电" in text
        assert "+" in text
