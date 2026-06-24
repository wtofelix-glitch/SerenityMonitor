"""测试 db 模块 — 数据库 CRUD 操作"""

import os, sys, tempfile, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def db_conn(monkeypatch):
    """用临时 SQLite 文件替换真实 DB_PATH"""
    import db
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_path = tmp.name; tmp.close()
    monkeypatch.setattr(db, 'DB_PATH', tmp_path)
    db.init_db()
    yield db
    os.unlink(tmp_path)


class TestInitDB:
    def test_all_tables_created(self, db_conn):
        conn = db_conn.get_conn()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        table_names = {r['name'] for r in tables}
        expected = {'stocks', 'daily_snapshots', 'trades', 'alerts', 'anomalies',
                    'scoring_history', 'price_history', 'signal_log', 'trading_journal'}
        assert expected <= table_names, f"Missing: {expected - table_names}"
        conn.close()

    def test_stocks_table_columns(self, db_conn):
        conn = db_conn.get_conn()
        cols = conn.execute("PRAGMA table_info(stocks)").fetchall()
        col_names = {c['name'] for c in cols}
        assert {'code', 'name', 'stop_loss', 'is_active'} <= col_names
        conn.close()


class TestStockCRUD:
    def _make_stock(self, code='600001', **kw):
        return {'code': code, 'name': kw.get('name', '测试'), 'market': 'SH', 'tier': '主板',
                'buy_price': kw.get('buy_price', 10.5), 'buy_date': kw.get('buy_date', '2026-06-01'),
                'target_high': kw.get('target_high', 15.0), 'target_low': kw.get('target_low', 8.0),
                'stop_loss': kw.get('stop_loss', 9.0), 'is_active': kw.get('is_active', 1),
                'notes': kw.get('notes', '')}

    def test_upsert_and_get(self, db_conn):
        db_conn.upsert_stock(self._make_stock())
        s = db_conn.get_stock('600001')
        assert s is not None and s['stop_loss'] == 9.0

    def test_upsert_updates(self, db_conn):
        db_conn.upsert_stock(self._make_stock(notes='first'))
        db_conn.upsert_stock(self._make_stock(name='更新', stop_loss=10.0, notes='updated'))
        s = db_conn.get_stock('600001')
        assert s['name'] == '更新' and s['stop_loss'] == 10.0

    def test_get_nonexistent(self, db_conn):
        assert db_conn.get_stock('NONEXIST') is None

    def test_load_all_stocks(self, db_conn):
        for c in ['600001', '600002', '600003']: db_conn.upsert_stock(self._make_stock(c))
        assert len(db_conn.load_all_stocks()) == 3

    def test_set_clear_active(self, db_conn):
        db_conn.upsert_stock(self._make_stock(is_active=0))
        db_conn.set_active('600001', 10.5, '2026-06-23', 15.0, 8.0)
        assert db_conn.get_stock('600001')['is_active'] == 1
        db_conn.clear_active('600001')
        assert db_conn.get_stock('600001')['is_active'] == 0


class TestTradeCRUD:
    def test_add_trade_buy(self, db_conn):
        db_conn.add_trade('600001', 'buy', 10.5, 1000, '2026-06-23', 'test')
        trades = db_conn.get_trades('600001')
        assert len(trades) >= 1 and trades[0]['action'] == 'buy'

    def test_add_trade_sell(self, db_conn):
        db_conn.add_trade('600001', 'sell', 12.0, 500, '2026-06-23', 'test')
        assert len(db_conn.get_trades('600001')) >= 1


class TestSnapshotCRUD:
    def test_save_and_get(self, db_conn):
        db_conn.save_snapshot('600001', {'date': '2026-06-23', 'open': 10.0, 'close': 10.5, 'high': 11.0, 'low': 9.5, 'volume': 1000000})
        assert len(db_conn.get_snapshots('600001', days=5)) >= 1

    def test_get_latest(self, db_conn):
        db_conn.save_snapshot('600001', {'date': '2026-06-22', 'open': 10.0, 'close': 10.2, 'high': 10.5, 'low': 9.8, 'volume': 500000})
        s = db_conn.get_latest_snapshot('600001')
        assert s is not None and s['close'] == 10.2


class TestAnomalyCRUD:
    def test_add_and_get(self, db_conn):
        # add_anomaly(code, level, alert_type, price, message, data=None)
        db_conn.add_anomaly('600001', 'A', 'price_spike', 12.5, 'Price spike >5%')
        anoms = db_conn.get_unacknowledged_anomalies(limit=5)
        assert len(anoms) >= 1 and anoms[0]['code'] == '600001'

    def test_acknowledge(self, db_conn):
        db_conn.add_anomaly('600001', 'B', 'factor_change', 11.0, 'Warning')
        aid = db_conn.get_unacknowledged_anomalies(limit=5)[0]['id']
        db_conn.acknowledge_anomaly(aid)
        remaining_ids = {a['id'] for a in db_conn.get_unacknowledged_anomalies(limit=5)}
        assert aid not in remaining_ids

    def test_get_today(self, db_conn):
        db_conn.add_anomaly('600001', 'C', 'factor_change', 10.0, 'Factor')
        assert len(db_conn.get_today_anomalies()) >= 1


class TestSignalLog:
    def test_save_and_get(self, db_conn):
        # save_signal_log(code, action, total_score, price, is_holding=False, ...)
        db_conn.save_signal_log('600001', 'BUY', 75.0, 10.5)
        sigs = db_conn.get_recent_signals(days=7, limit=5)
        assert len(sigs) >= 1 and sigs[0]['code'] == '600001'

    def test_update_outcome(self, db_conn):
        db_conn.save_signal_log('600001', 'BUY', 75.0, 10.5)
        sig_id = db_conn.get_recent_signals(days=7, limit=1)[0]['id']
        db_conn.update_signal_outcome(sig_id, 'outcome_1d', 1.5)
        # Should not raise
        assert True

    def test_get_performance_empty(self, db_conn):
        assert isinstance(db_conn.get_signal_performance(), list)

    def test_get_recent_signals_all(self, db_conn):
        db_conn.save_signal_log('600001', 'BUY', 70.0, 10.0)
        db_conn.save_signal_log('600002', 'SELL', 30.0, 12.0)
        sigs = db_conn.get_recent_signals(days=30, limit=10)
        assert len(sigs) >= 2


class TestAlertCRUD:
    # add_alert(code, alert_type, price, message)
    def test_add_and_get(self, db_conn):
        db_conn.add_alert('600001', 'stop_loss', 9.0, 'Hit stop')
        alerts = db_conn.get_unacknowledged_alerts()
        assert len(alerts) >= 1 and alerts[0]['code'] == '600001'

    def test_acknowledge(self, db_conn):
        db_conn.add_alert('600001', 'target', 15.0, 'Target hit')
        aid = db_conn.get_unacknowledged_alerts()[0]['id']
        db_conn.acknowledge_alert(aid)
        assert aid not in {a['id'] for a in db_conn.get_unacknowledged_alerts()}


class TestPriceHistory:
    def test_save_and_get(self, db_conn):
        db_conn.save_price_history('600001', {'code': '600001', 'date': '2026-06-22', 'open': 10.0, 'close': 10.5, 'high': 11.0, 'low': 9.5, 'volume': 1000000, 'change_pct': 5.0})
        hist = db_conn.get_price_history('600001', days=5)
        assert len(hist) >= 1 and hist[0]['close'] == 10.5

    def test_get_avg_volume(self, db_conn):
        for i in range(1, 21):
            db_conn.save_price_history('600001', {'code': '600001', 'date': f'2026-06-{i:02d}', 'open': 10.0, 'close': 10.0, 'high': 10.0, 'low': 10.0, 'volume': 1000000 + i * 100000, 'change_pct': 0})
        avg = db_conn.get_avg_volume('600001', days=10)
        assert avg is not None and avg > 0
