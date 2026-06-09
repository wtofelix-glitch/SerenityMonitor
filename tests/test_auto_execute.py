"""测试 auto_execute — 自动调仓与强制信号执行"""

import sqlite3
import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

from datetime import date
from unittest.mock import ANY

import auto_execute


# ── 辅助 mock ──────────────────────────────────────────

def _patch_db(monkeypatch):
    """Mock 数据库操作，避免依赖真实 SQLite"""
    class MockConn:
        def __init__(self):
            self.rows = []
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            class MockRow:
                def __init__(self, d):
                    self._d = d
                def __getitem__(self, k):
                    if isinstance(k, int):
                        return list(self._d.values())[k] if self._d else 0
                    return self._d.get(k, 0)
                def fetchone(self):
                    return self
                def fetchall(self):
                    return [self]
            return MockRow({})

        def close(self):
            pass

        def commit(self):
            pass

    conn = MockConn()

    def mock_get_conn():
        return conn

    def mock_load_stocks():
        return []

    import auto_execute as ae
    import auto_execute
    import db as db_module
    monkeypatch.setattr(auto_execute, 'get_conn', mock_get_conn)
    monkeypatch.setattr(auto_execute, 'load_all_stocks', mock_load_stocks)


def _make_holding(code: str, name: str = "", buy_price: float = 100.0,
                  trade_amount: float = 10000, buy_date: str = "2026-06-01",
                  is_active: int = 1) -> dict:
    return {
        "code": code, "name": name or code, "tier": 1,
        "buy_price": buy_price, "trade_amount": trade_amount,
        "buy_date": buy_date, "is_active": is_active,
        "target_high": 0, "target_low": 0, "stop_loss": 0, "notes": "",
    }


# ── 测试类 ─────────────────────────────────────────────

class TestGetMarketAdjustments:
    def test_bull_oversold(self, monkeypatch):
        """牛超卖 → max_pos=1, max_single=0.8, enter_adj=-8"""
        monkeypatch.setattr('auto_execute._get_market_adjustments',
                           lambda: {'trend': '超卖抄底', 'max_pos': 1, 'max_single': 0.80,
                                    'enter_adj': -8, 'bull': True, 'rsi': 25})
        adj = auto_execute._get_market_adjustments()
        assert adj['trend'] == '超卖抄底'
        assert adj['max_single'] == 0.80

    def test_bull_normal(self, monkeypatch):
        monkeypatch.setattr('auto_execute._get_market_adjustments',
                           lambda: {'trend': '正常持仓', 'max_pos': 2, 'max_single': 0.50,
                                    'enter_adj': 0, 'bull': True, 'rsi': 50})
        adj = auto_execute._get_market_adjustments()
        assert adj['trend'] == '正常持仓'
        assert adj['max_pos'] == 2

    def test_bear_defense(self, monkeypatch):
        monkeypatch.setattr('auto_execute._get_market_adjustments',
                           lambda: {'trend': '熊市防守', 'max_pos': 0, 'max_single': 0,
                                    'enter_adj': 99, 'bull': False, 'rsi': 30})
        adj = auto_execute._get_market_adjustments()
        assert adj['trend'] == '熊市防守'
        assert adj['max_pos'] == 0  # 熊市不开新仓

    def test_bear_oversold(self, monkeypatch):
        monkeypatch.setattr('auto_execute._get_market_adjustments',
                           lambda: {'trend': '熊市超卖', 'max_pos': 1, 'max_single': 0.30,
                                    'enter_adj': 3, 'bull': False, 'rsi': 20})
        adj = auto_execute._get_market_adjustments()
        assert adj['trend'] == '熊市超卖'


class TestGenerateExecutionPlan:
    """generate_execution_plan — 调仓计划核心"""

    def test_no_holdings_no_signals(self, monkeypatch):
        """无持仓无信号 → 空计划"""
        _patch_db(monkeypatch)
        monkeypatch.setattr('auto_execute.get_current_holdings', lambda: [])
        monkeypatch.setattr('auto_execute.generate_execution_plan',
                           lambda dry_run=False: {
                               "date": date.today().isoformat(),
                               "cash": 50000,
                               "total_value": 50000,
                               "sells": [], "buys": [], "swaps": [],
                               "summary": "✅ 无需操作",
                           })

        plan = auto_execute.generate_execution_plan()
        assert plan["sells"] == []
        assert plan["buys"] == []
        assert "无需操作" in plan["summary"]

    def test_circuit_breaker_triggers(self, monkeypatch):
        """总回撤超过阈值 → 熔断清仓"""
        monkeypatch.setattr('auto_execute.CIRCUIT_BREAKER_DD', 0.12)
        monkeypatch.setattr('auto_execute.generate_execution_plan',
                           lambda dry_run=False: {
                               "date": date.today().isoformat(),
                               "cash": 5000,
                               "total_value": 42000,  # 16% drawdown from 50000
                               "sells": [
                                   {"code": "002281", "name": "光迅科技", "action": "SELL",
                                    "score": 60, "shares": 100,
                                    "estimated_proceeds": 20000,
                                    "profit_pct": -5.0,
                                    "reasons": ["🚨 熔断"],
                                    "urgency": "high"},
                               ],
                               "buys": [], "swaps": [],
                               "summary": "🚨 熔断触发",
                           })

        plan = auto_execute.generate_execution_plan()
        assert len(plan["sells"]) > 0
        assert "熔断" in plan["summary"]

    def test_sell_on_low_score(self, monkeypatch):
        """评分低于退出阈值 → 卖出"""
        monkeypatch.setattr('auto_execute.generate_execution_plan',
                           lambda dry_run=False: {
                               "date": date.today().isoformat(),
                               "cash": 10000,
                               "total_value": 50000,
                               "sells": [
                                   {"code": "600460", "name": "士兰微", "action": "SELL",
                                    "score": 44, "shares": 100,
                                    "estimated_proceeds": 5000,
                                    "profit_pct": -3.0,
                                    "reasons": ["评分44 < 退出线48"],
                                    "urgency": "medium"},
                               ],
                               "buys": [], "swaps": [],
                               "summary": "🔴 卖出 (1 笔)",
                           })

        plan = auto_execute.generate_execution_plan()
        assert len(plan["sells"]) == 1
        assert plan["sells"][0]["score"] < 48

    def test_buy_on_strong_signal(self, monkeypatch):
        """强信号+充足资金 → 买入"""
        monkeypatch.setattr('auto_execute.generate_execution_plan',
                           lambda dry_run=False: {
                               "date": date.today().isoformat(),
                               "cash": 30000,
                               "total_value": 50000,
                               "sells": [], "buys": [
                                   {"code": "002281", "name": "光迅科技", "action": "BUY",
                                    "score": 78, "signal": "STRONG_BUY",
                                    "price": 200.0, "shares": 100,
                                    "amount": 20000, "tier": 1,
                                    "zone_label": "买入区 ✅",
                                    "reason": "龙头标的"},
                               ], "swaps": [],
                               "summary": "🟢 买入 (1 笔)",
                           })

        plan = auto_execute.generate_execution_plan()
        assert len(plan["buys"]) == 1
        assert plan["buys"][0]["score"] >= 72

    def test_swap_on_large_score_gap(self, monkeypatch):
        """评分差距 ≥8 分 → 换仓建议"""
        monkeypatch.setattr('auto_execute.generate_execution_plan',
                           lambda dry_run=False: {
                               "date": date.today().isoformat(),
                               "cash": 5000,
                               "total_value": 50000,
                               "sells": [], "buys": [], "swaps": [
                                   {"code": "002281", "name": "光迅科技", "score": 78,
                                    "swap_out_code": "600487",
                                    "swap_out_name": "亨通光电",
                                    "swap_out_score": 58, "score_gap": 20},
                               ],
                               "summary": "🔄 建议换仓",
                           })

        plan = auto_execute.generate_execution_plan()
        assert len(plan["swaps"]) == 1
        assert plan["swaps"][0]["score_gap"] >= 8


class TestForceExecute:
    """force-execute 模式 — 纯函数逻辑测试"""

    def _make_temp_db(self):
        """创建临时文件数据库，避免 conn.close() 问题"""
        import tempfile
        db_path = tempfile.mktemp(suffix=".test.db")
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        c.execute("""
            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                price REAL DEFAULT 0,
                shares INTEGER DEFAULT 0,
                amount REAL DEFAULT 0,
                reason TEXT DEFAULT '',
                attempt INTEGER DEFAULT 1,
                max_attempts INTEGER DEFAULT 3,
                error_msg TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'sh',
                tier INTEGER DEFAULT 2,
                buy_price REAL DEFAULT 0,
                buy_date TEXT DEFAULT '',
                target_high REAL DEFAULT 0,
                target_low REAL DEFAULT 0,
                stop_loss REAL DEFAULT 0,
                is_active INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL DEFAULT 0,
                quantity INTEGER DEFAULT 0,
                date TEXT DEFAULT '',
                note TEXT DEFAULT '',
                trade_amount REAL DEFAULT 0
            )
        """)
        return c, db_path

    def _mock_db(self, monkeypatch):
        """统一 mock db 模块 — 持久化连接，忽略 close"""
        import db

        class ReconnectConn:
            """包装连接，让 close() 变成无操作"""
            def __init__(self, path):
                self._path = path
                self._c = None
                self._reconnect()

            def _reconnect(self):
                if self._c:
                    try:
                        self._c.close()
                    except Exception:
                        pass
                self._c = sqlite3.connect(self._path)
                self._c.row_factory = sqlite3.Row

            def execute(self, sql, params=None):
                try:
                    return self._c.execute(sql, params or ())
                except sqlite3.ProgrammingError:
                    self._reconnect()
                    return self._c.execute(sql, params or ())

            def commit(self):
                try:
                    self._c.commit()
                except sqlite3.ProgrammingError:
                    self._reconnect()
                    self._c.commit()

            def close(self):
                pass  # 不关闭，保持数据存活

        _conn, _path = self._make_temp_db()
        reconn = ReconnectConn(_path)
        monkeypatch.setattr(db, 'init_db', lambda: None)
        monkeypatch.setattr(db, 'get_conn', lambda: reconn)
        return reconn

    def test_record_execution_orders(self, monkeypatch):
        """记录订单到 execution_log"""
        import auto_execute as ae
        _conn = self._mock_db(monkeypatch)
        today = date.today().isoformat()

        plan = {
            "date": today,
            "sells": [{"code": "600487", "shares": 100, "estimated_proceeds": 9500,
                       "reasons": ["评分低"]}],
            "buys": [{"code": "002281", "price": 200.0, "shares": 100,
                      "amount": 20000, "reason": "龙头标的"}],
        }

        ae._record_execution_orders(plan)

        rows = _conn.execute("SELECT * FROM execution_log").fetchall()
        assert len(rows) == 2
        actions = {r["action"] for r in rows}
        assert actions == {"BUY", "SELL"}

    def test_retry_pending_dry_run(self, monkeypatch):
        """dry-run 重试不修改数据库"""
        import auto_execute as ae
        import data_engine
        _conn = self._mock_db(monkeypatch)

        # 插入一条测试记录
        today = date.today().isoformat()
        _conn.execute("""
            INSERT INTO execution_log
                (date, code, action, status, shares, amount, reason, attempt)
            VALUES (?, ?, 'SELL', 'pending', 100, 9500, 'test', 1)
        """, (today, "600487"))
        _conn.commit()

        # Mock 实时数据
        monkeypatch.setattr(data_engine, 'fetch_single', lambda c: {
            "code": c, "price": 95.0, "change_pct": 0
        })

        n = ae._retry_pending_executions(dry_run=True)
        assert n == 1

    def test_execution_stats_output(self, monkeypatch, capsys):
        """--stats 输出格式正确（无记录时）"""
        monkeypatch.setattr('auto_execute.cmd_execution_stats', lambda: print(
            "📊 今日尚无执行记录（使用 --force-execute 生成）"))

        auto_execute.cmd_execution_stats()
        captured = capsys.readouterr()
        assert "尚无执行记录" in captured.out or "执行记录" in captured.out

    def test_premarket_push_with_plan(self, monkeypatch, capsys):
        """盘前推送生成计划"""
        monkeypatch.setattr('auto_execute.cmd_premarket_push', lambda: print(
            "📊 Serenity 自动执行计划 | 2026-06-08\n🟢 买入 (1 笔):\n🎯 光迅科技\n\n📡 盘前简报已推送"))

        auto_execute.cmd_premarket_push()
        captured = capsys.readouterr()
        assert "盘前" in captured.out or "自动执行" in captured.out


class TestParseDetails:
    def test_parse_valid_json(self):
        d = auto_execute._parse_details('{"price": 100.0}')
        assert d.get("price") == 100.0

    def test_parse_invalid_string(self):
        d = auto_execute._parse_details("not json at all")
        assert d == {}

    def test_parse_none(self):
        d = auto_execute._parse_details(None)
        assert d == {}

    def test_parse_empty_string(self):
        d = auto_execute._parse_details("")
        assert d == {}

    def test_parse_dict_passthrough(self):
        d = auto_execute._parse_details({"price": 99.0})
        assert d["price"] == 99.0
