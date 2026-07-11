"""
pytest 配置 — 所有测试自动使用临时数据库，防止污染生产 DB。
"""
import os
import tempfile
import atexit
from pathlib import Path


# 会话级别的临时目录和数据库
_temp_dir: str | None = None
_temp_db: str | None = None


def pytest_configure(config):
    """pytest 启动时创建临时数据库并设置为 SERENITY_DB_PATH。"""
    global _temp_dir, _temp_db

    if os.environ.get("SERENITY_DB_PATH"):
        # 已显式设置，不覆盖（CI/手动指定）
        return

    _temp_dir = tempfile.mkdtemp(prefix="serenity_test_")
    _temp_db = os.path.join(_temp_dir, "serenity_test.db")
    os.environ["SERENITY_DB_PATH"] = _temp_db

    # 初始化基础表结构
    import sqlite3
    db = sqlite3.connect(_temp_db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER DEFAULT 0,
            date TEXT NOT NULL,
            note TEXT DEFAULT '',
            trade_amount REAL DEFAULT 0,
            trade_hash TEXT DEFAULT '',
            source TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_hash ON trades(trade_hash)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS stocks (
            code TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            tier INTEGER DEFAULT 2,
            buy_price REAL DEFAULT 0,
            buy_date TEXT,
            target_high REAL DEFAULT 0,
            target_low REAL DEFAULT 0,
            stop_loss REAL DEFAULT 0,
            is_active INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            trade_amount REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    db.commit()
    db.close()


def pytest_unconfigure(config):
    """清理临时数据库。"""
    global _temp_dir

    if _temp_dir and os.path.isdir(_temp_dir):
        import shutil
        try:
            shutil.rmtree(_temp_dir, ignore_errors=True)
        except Exception:
            pass
