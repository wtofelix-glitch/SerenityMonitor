"""
Serenity 2.0 DB 迁移 [v2.1]

为 trades 表添加 v2.1 新字段:
  · external_fill_id — 券商唯一成交编号（幂等主键）
  · order_id — 关联委托ID
  · fill_sequence — 分笔序号
  · import_batch_id — 导入批次
  · commission, stamp_tax, transfer_fee — 费税

迁移原则：
  · 所有 ALTER TABLE 使用 IF NOT EXISTS 模式（SQLite 不支持，用 try/except）
  · 迁移失败不阻止程序启动（仅日志告警）
  · 幂等：多次运行安全
"""

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# v2.1 迁移列表
MIGRATIONS_V21 = [
    # trades 表新字段
    ("ALTER TABLE trades ADD COLUMN external_fill_id TEXT DEFAULT ''", "external_fill_id"),
    ("ALTER TABLE trades ADD COLUMN order_id TEXT DEFAULT ''", "order_id"),
    ("ALTER TABLE trades ADD COLUMN fill_sequence INTEGER DEFAULT 0", "fill_sequence"),
    ("ALTER TABLE trades ADD COLUMN import_batch_id TEXT DEFAULT ''", "import_batch_id"),
    ("ALTER TABLE trades ADD COLUMN commission REAL DEFAULT 0.0", "commission"),
    ("ALTER TABLE trades ADD COLUMN stamp_tax REAL DEFAULT 0.0", "stamp_tax"),
    ("ALTER TABLE trades ADD COLUMN transfer_fee REAL DEFAULT 0.0", "transfer_fee"),
]

# 新索引
MIGRATIONS_V21_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trades_external_fill_id ON trades(external_fill_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_import_batch ON trades(import_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_trade_hash ON trades(trade_hash)",
]


def apply_migrations(db_path: Path) -> dict:
    """
    应用所有待处理迁移。幂等，多次运行安全。
    返回 {"applied": [str], "skipped": [str], "errors": [str]}
    """
    result = {"applied": [], "skipped": [], "errors": []}

    conn = sqlite3.connect(str(db_path))
    try:
        # 确保 trades 表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                date TEXT NOT NULL DEFAULT '',
                note TEXT DEFAULT '',
                trade_hash TEXT DEFAULT '',
                trade_amount REAL DEFAULT 0,
                source TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 应用字段迁移
        for sql, field_name in MIGRATIONS_V21:
            try:
                conn.execute(sql)
                result["applied"].append(field_name)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    result["skipped"].append(field_name)
                else:
                    result["errors"].append(f"{field_name}: {e}")
                    logger.warning(f"Migration failed: {field_name}: {e}")

        # 应用索引迁移
        for sql in MIGRATIONS_V21_INDEXES:
            try:
                conn.execute(sql)
                result["applied"].append(f"index: {sql[:60]}...")
            except sqlite3.OperationalError as e:
                result["skipped"].append(f"index: {e}")

        # 确保 portfolio_reconciliations 表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_at TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                total_assets REAL DEFAULT 0,
                holdings_value REAL DEFAULT 0,
                cash REAL DEFAULT 0,
                floating_profit REAL DEFAULT 0,
                daily_profit REAL DEFAULT 0,
                daily_profit_pct REAL DEFAULT 0,
                position_ratio_pct REAL DEFAULT 0,
                positions_json TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 确保 nav_history 表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nav_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                total_value REAL DEFAULT 0,
                cash REAL DEFAULT 0,
                holdings_value REAL DEFAULT 0,
                profit_pct REAL DEFAULT 0,
                positions_json TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        conn.commit()
    finally:
        conn.close()

    return result


def check_schema(db_path: Path) -> dict:
    """检查当前 schema 状态。"""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t[0] for t in tables]

        result = {"tables": table_names, "columns": {}}
        for table in table_names:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            result["columns"][table] = [c[1] for c in cols]

        return result
    finally:
        conn.close()
