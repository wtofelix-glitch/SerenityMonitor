"""
数据库层 — SQLite 存储股票配置、每日快照、交易记录、预警历史
"""
from __future__ import annotations
import sqlite3
import os
from datetime import datetime, date
from typing import Optional, Any

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serenity.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_conn()
    cur = conn.cursor()

    # 股票配置表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stocks (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
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

    # 每日快照表（一条记录 = 一天收盘）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            amount REAL,
            change_pct REAL,
            pe_ttm REAL,
            total_mv REAL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(code, date)
        )
    """)

    # 交易记录表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            action TEXT NOT NULL,  -- buy / sell
            price REAL NOT NULL,
            quantity INTEGER DEFAULT 0,
            date TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 预警记录表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            alert_type TEXT NOT NULL,  -- target_high / target_low / stop_loss
            price REAL NOT NULL,
            message TEXT,
            date TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 迁移：旧表增加 trade_amount 列
    for table, col in [("trades", "trade_amount"), ("stocks", "trade_amount")]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    # 评分历史表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scoring_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            total_score REAL DEFAULT 0,
            base_score REAL DEFAULT 0,
            zone_score REAL DEFAULT 0,
            momentum_score REAL DEFAULT 0,
            volume_score REAL DEFAULT 0,
            details TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(code, date)
        )
    """)
    # 迁移：增加评分维度列
    for col in ["factor_score", "serenity_score", "technical_score", "sentiment_score", "moat_score", "mr_score", "uzi_score", "multi_cycle_factor"]:
        try:
            conn.execute(f"ALTER TABLE scoring_history ADD COLUMN {col} REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    # UZI 证据账本：公告/订单/量产/认证/研报/风险事件等可验证证据
    cur.execute("""
        CREATE TABLE IF NOT EXISTS uzi_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            event_date TEXT NOT NULL,
            strength TEXT NOT NULL DEFAULT 'medium', -- strong / medium / weak
            source_type TEXT NOT NULL DEFAULT 'manual',
            title TEXT NOT NULL,
            summary TEXT DEFAULT '',
            url TEXT DEFAULT '',
            impact REAL DEFAULT 0,
            expires_on TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_uzi_evidence_code_active
        ON uzi_evidence(code, active, event_date)
    """)

    # 异常事件表（市场异动/黑天鹅预警）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            level TEXT NOT NULL,  -- A(紧急)/B(关注)/C(提示)
            alert_type TEXT NOT NULL,  -- price_drop / volume_surge / consecutive_decline / news_negative
            price REAL DEFAULT 0,
            message TEXT,
            data JSON DEFAULT '{}',
            acknowledged INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 每日行情历史表（用于成交量均值计算等）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, close REAL, high REAL, low REAL,
            volume REAL, change_pct REAL,
            UNIQUE(code, date)
        )
    """)

    # Serenity 推文建议表（ticker / chinese_idea）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS serenity_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,          -- 'ticker' 或 'chinese_idea'
            content TEXT NOT NULL,         -- 标的符号或中文描述
            context TEXT DEFAULT '',       -- 推文上下文片段
            is_new INTEGER DEFAULT 1,     -- 用户是否已查看
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 红利低波评分表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dividend_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            score_date TEXT NOT NULL,
            dividend_yield_score REAL DEFAULT 0,
            low_vol_score REAL DEFAULT 0,
            valuation_score REAL DEFAULT 0,
            quality_score REAL DEFAULT 0,
            total_score REAL DEFAULT 0,
            details TEXT DEFAULT '',  -- JSON格式额外信息
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # ETF动量评分表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS etf_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            etf_code TEXT NOT NULL,
            score_date TEXT NOT NULL,
            momentum_short REAL DEFAULT 0,
            momentum_long REAL DEFAULT 0,
            trend_strength REAL DEFAULT 0,
            total_score REAL DEFAULT 0,
            rank INTEGER DEFAULT 0,
            details TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 信号日志表（每次 generate_signals 发出的记录，用于绩效追踪）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            action TEXT NOT NULL,        -- STRONG_BUY / BUY / HOLD / SELL / STOP_LOSS 等
            total_score REAL DEFAULT 0,
            price REAL DEFAULT 0,
            is_holding INTEGER DEFAULT 0,
            tech_score REAL DEFAULT 0,
            serenity_score REAL DEFAULT 0,
            alpha_score REAL DEFAULT 0,
            fundamental_score REAL DEFAULT NULL,
            outcome_1d REAL DEFAULT NULL,   -- 1日后涨跌幅(%)
            outcome_3d REAL DEFAULT NULL,   -- 3日后涨跌幅(%)
            outcome_5d REAL DEFAULT NULL,   -- 5日后涨跌幅(%)
            outcome_10d REAL DEFAULT NULL,  -- 10日后涨跌幅(%)
            details TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_log_code_date
        ON signal_log(code, date)
    """)

    # 策略分配表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_allocation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alloc_date TEXT NOT NULL,
            dividend_weight REAL DEFAULT 0.50,
            quant_weight REAL DEFAULT 0.30,
            etf_weight REAL DEFAULT 0.20,
            market_regime TEXT DEFAULT '',  -- 牛市/震荡市/熊市/结构性牛市
            adjustments TEXT DEFAULT '',    -- JSON格式动态调整记录
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)



    # 信号绩效汇总表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            total_signals INTEGER DEFAULT 0,
            wins_1d INTEGER DEFAULT 0,
            wins_3d INTEGER DEFAULT 0,
            wins_5d INTEGER DEFAULT 0,
            avg_return_1d REAL DEFAULT 0,
            avg_return_3d REAL DEFAULT 0,
            avg_return_5d REAL DEFAULT 0,
            last_updated TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(code, action)
        )
    """)

    # 🆕 评分反思表（反思学习环）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS score_reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            total_score REAL DEFAULT 0,
            dimension_scores TEXT DEFAULT '{}',   -- JSON: 各维度得分
            predicted_direction TEXT DEFAULT '',   -- BUY/HOLD/SELL
            actual_return_1d REAL DEFAULT NULL,    -- 1日后实际收益(%)
            actual_return_3d REAL DEFAULT NULL,    -- 3日后实际收益(%)
            actual_return_5d REAL DEFAULT NULL,    -- 5日后实际收益(%)
            dimension_ic TEXT DEFAULT '{}',         -- JSON: 各维度Rank IC
            reflection_text TEXT DEFAULT '',        -- 反思总结
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(code, date)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_reflections_date
        ON score_reflections(date)
    """)

    # 🆕 执行日志表（force-execute 记录）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS execution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            action TEXT NOT NULL,        -- BUY/SELL
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/executed/failed
            price REAL DEFAULT 0,
            shares INTEGER DEFAULT 0,
            amount REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            attempt INTEGER DEFAULT 1,   -- 第几次重试
            max_attempts INTEGER DEFAULT 3,
            error_msg TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 🆕 权重辩论日志表（conviction_engine 持久化）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conviction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            regime TEXT NOT NULL DEFAULT '',
            debated_weights TEXT DEFAULT '{}',
            regime_weights TEXT DEFAULT '{}',
            score_avg REAL DEFAULT 0,
            high_count INTEGER DEFAULT 0,
            low_count INTEGER DEFAULT 0,
            position_advice TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date)
        )
    """)
    # ── trading_journal（原在 trading_journal.py，内联到此打破循环依赖）──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            date TEXT NOT NULL,
            price REAL,
            shares INTEGER,
            amount REAL,
            reason TEXT,
            reflection TEXT,
            score_at_entry REAL,
            score_at_exit REAL,
            profit_pct REAL,
            tags TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # ── 性能索引（幂等创建，已存在的自动跳过）──
    _index_sqls = [
        "CREATE INDEX IF NOT EXISTS idx_alerts_ack_created ON alerts(acknowledged, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_anomalies_ack_created ON anomalies(acknowledged, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_trades_code_date ON trades(code, date)",
        "CREATE INDEX IF NOT EXISTS idx_execution_log_status_created ON execution_log(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_suggestions_new_created ON serenity_suggestions(is_new, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_journal_code_action_date ON trading_journal(code, action, date)",
    ]

    # 🆕 v3.1 signal_to_trade 映射表
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signal_to_trade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,
            code TEXT NOT NULL,
            signal_action TEXT NOT NULL,
            signal_score REAL,
            trade_id INTEGER REFERENCES trades(id),
            trade_action TEXT,
            matched INTEGER DEFAULT 0,
            match_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_stt_code_date ON signal_to_trade(code, signal_date);
    """)

    for sql in _index_sqls:
        conn.execute(sql)
    conn.commit()
    conn.close()


# ---------- 股票配置 CRUD ----------

def load_all_stocks() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM stocks ORDER BY tier, code").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stock(code: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM stocks WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_stock(stock: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO stocks (code, name, market, tier, buy_price, buy_date,
                            target_high, target_low, stop_loss, is_active, notes)
        VALUES (:code, :name, :market, :tier, :buy_price, :buy_date,
                :target_high, :target_low, :stop_loss, :is_active, :notes)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name, market=excluded.market, tier=excluded.tier,
            buy_price=excluded.buy_price, buy_date=excluded.buy_date,
            target_high=excluded.target_high, target_low=excluded.target_low,
            stop_loss=excluded.stop_loss, is_active=excluded.is_active,
            notes=excluded.notes
    """, stock)
    conn.commit()
    conn.close()


def set_active(code: str, buy_price: float, buy_date: str, target_high: float = 0, target_low: float = 0):
    """标记某只股票为当前持有"""
    conn = get_conn()
    conn.execute("""
        UPDATE stocks SET is_active=1, buy_price=?, buy_date=?,
                          target_high=?, target_low=?
        WHERE code=?
    """, (buy_price, buy_date, target_high, target_low, code))
    conn.commit()
    conn.close()


def clear_active(code: str):
    """卖出后清除持有状态"""
    conn = get_conn()
    conn.execute("""
        UPDATE stocks SET is_active=0, buy_price=0, buy_date='',
                          target_high=0, target_low=0
        WHERE code=?
    """, (code,))
    conn.commit()
    conn.close()


# ---------- 每日快照 ----------

def save_snapshot(code: str, data: dict):
    """保存单只股票的日收盘快照"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO daily_snapshots (code, date, open, close, high, low,
                                     volume, amount, change_pct, pe_ttm, total_mv)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, date) DO UPDATE SET
            open=excluded.open, close=excluded.close, high=excluded.high,
            low=excluded.low, volume=excluded.volume, amount=excluded.amount,
            change_pct=excluded.change_pct, pe_ttm=excluded.pe_ttm,
            total_mv=excluded.total_mv
    """, (
        code, data.get("date"), data.get("open"), data.get("close"),
        data.get("high"), data.get("low"), data.get("volume"),
        data.get("amount"), data.get("change_pct"),
        data.get("pe_ttm"), data.get("total_mv")
    ))
    conn.commit()
    conn.close()


def get_latest_snapshot(code: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM daily_snapshots
        WHERE code=? ORDER BY date DESC LIMIT 1
    """, (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_snapshots(code: str, days: int = 30) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM daily_snapshots
        WHERE code=? ORDER BY date DESC LIMIT ?
    """, (code, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 交易记录 ----------

def add_trade(code: str, action: str, price: float, quantity: int, date_str: str,
              note: str = "", trade_amount: float = 0):
    conn = get_conn()
    conn.execute("""
        INSERT INTO trades (code, action, price, quantity, date, note, trade_amount)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (code, action, price, quantity, date_str, note, trade_amount))
    conn.commit()
    conn.close()
    # 同步到交易日志 (trading_journal)
    _sync_to_journal(code, action, date_str, price, quantity, note)


def _sync_to_journal(code: str, action: str, date_str: str, price: float,
                     quantity: int, note: str):
    """内部：将新增 trade 同步到 trading_journal 表（幂等）"""
    try:
        conn = get_conn()
        exists = conn.execute(
            "SELECT COUNT(*) FROM trading_journal WHERE code=? AND action=? AND date=?",
            (code, action, date_str)
        ).fetchone()[0]
        if exists == 0:
            conn.execute("""
                INSERT INTO trading_journal (code, action, date, price, shares, amount, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (code, action, date_str, price, quantity, price * quantity,
                  f"auto: {note[:100]}" if note else "auto"))
            conn.commit()
        conn.close()
    except Exception:
        pass  # 静默容错，不阻塞主流程


def get_trades(code: Optional[str] = None, limit: int = 20) -> list[dict]:
    conn = get_conn()
    if code:
        rows = conn.execute("""
            SELECT * FROM trades WHERE code=? ORDER BY date DESC LIMIT ?
        """, (code, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM trades ORDER BY date DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 预警记录 ----------

def add_alert(code: str, alert_type: str, price: float, message: str):
    today = date.today().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO alerts (code, alert_type, price, message, date)
        VALUES (?, ?, ?, ?, ?)
    """, (code, alert_type, price, message, today))
    conn.commit()
    conn.close()


def get_unacknowledged_alerts() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.*, s.name FROM alerts a
        LEFT JOIN stocks s ON a.code = s.code
        WHERE a.acknowledged=0 ORDER BY a.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def acknowledge_alert(alert_id: int):
    conn = get_conn()
    conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()


# ---------- 评分历史 ----------


def _safe_json_dumps(obj):
    """安全序列化，递归转换 numpy 类型为原生 Python 类型"""
    import json
    try:
        import numpy as np
    except ImportError:
        np = None

    def convert(o):
        if np is not None and isinstance(o, (np.floating, np.integer)):
            return float(o) if isinstance(o, np.floating) else int(o)
        if np is not None and isinstance(o, np.ndarray):
            return o.tolist()
        if np is not None and isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, dict):
            return {k: convert(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [convert(i) for i in o]
        return o

    return json.dumps(convert(obj), ensure_ascii=False)

def save_score_history(code: str, scores: dict):
    """保存每日评分"""
    conn = get_conn()
    for col in ["uzi_score", "multi_cycle_factor"]:
        try:
            conn.execute(f"ALTER TABLE scoring_history ADD COLUMN {col} REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        INSERT INTO scoring_history (code, date, total_score, base_score,
            zone_score, momentum_score, volume_score, details,
            serenity_score, factor_score, technical_score, sentiment_score,
            moat_score, mr_score, uzi_score, multi_cycle_factor)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, date) DO UPDATE SET
            total_score=excluded.total_score, base_score=excluded.base_score,
            zone_score=excluded.zone_score, momentum_score=excluded.momentum_score,
            volume_score=excluded.volume_score, details=excluded.details,
            serenity_score=excluded.serenity_score, factor_score=excluded.factor_score,
            technical_score=excluded.technical_score,
            sentiment_score=excluded.sentiment_score,
            moat_score=excluded.moat_score,
            mr_score=excluded.mr_score,
            uzi_score=excluded.uzi_score,
            multi_cycle_factor=excluded.multi_cycle_factor
    """, (
        code, scores["date"], scores["total_score"],
        scores.get("base_score", 0), scores.get("zone_score", 0),
        scores.get("momentum_score", 0), scores.get("volume_score", 0),
        _safe_json_dumps(scores.get("details", {})),
        scores.get("serenity_score", 0), scores.get("factor_score", 0),
        scores.get("technical_score", 0),
        scores.get("sentiment_score", 0),
        scores.get("moat_score", 50),
        scores.get("mr_score", 50.0),
        scores.get("uzi_score", 0),
        scores.get("multi_cycle_factor", 0)
    ))
    conn.commit()
    conn.close()


def get_latest_scores(codes: list[str] = None) -> list[dict]:
    """获取所有标的的最新评分"""
    conn = get_conn()
    if codes:
        placeholders = ",".join("?" for _ in codes)
        rows = conn.execute(f"""
            SELECT s1.* FROM scoring_history s1
            INNER JOIN (
                SELECT code, MAX(date) as max_date FROM scoring_history
                WHERE code IN ({placeholders})
                GROUP BY code
            ) s2 ON s1.code = s2.code AND s1.date = s2.max_date
            ORDER BY s1.total_score DESC
        """, codes).fetchall()
    else:
        rows = conn.execute("""
            SELECT s1.* FROM scoring_history s1
            INNER JOIN (
                SELECT code, MAX(date) as max_date FROM scoring_history
                GROUP BY code
            ) s2 ON s1.code = s2.code AND s1.date = s2.max_date
            ORDER BY s1.total_score DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- UZI 证据账本 ----------

def _ensure_uzi_evidence_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uzi_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            event_date TEXT NOT NULL,
            strength TEXT NOT NULL DEFAULT 'medium',
            source_type TEXT NOT NULL DEFAULT 'manual',
            title TEXT NOT NULL,
            summary TEXT DEFAULT '',
            url TEXT DEFAULT '',
            impact REAL DEFAULT 0,
            expires_on TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_uzi_evidence_code_active
        ON uzi_evidence(code, active, event_date)
    """)


def add_uzi_evidence(
    code: str,
    title: str,
    *,
    strength: str = "medium",
    source_type: str = "manual",
    event_date: str | None = None,
    summary: str = "",
    url: str = "",
    impact: float = 0,
    expires_on: str = "",
) -> int:
    """新增一条 UZI 证据记录，返回记录 id。"""
    strength = (strength or "medium").lower()
    if strength not in {"strong", "medium", "weak"}:
        raise ValueError("strength must be one of: strong, medium, weak")
    if not code or not title:
        raise ValueError("code and title are required")
    conn = get_conn()
    _ensure_uzi_evidence_table(conn)
    cur = conn.execute("""
        INSERT INTO uzi_evidence
            (code, event_date, strength, source_type, title, summary, url, impact, expires_on)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        code,
        event_date or date.today().isoformat(),
        strength,
        source_type or "manual",
        title,
        summary,
        url,
        float(impact or 0),
        expires_on or "",
    ))
    conn.commit()
    new_id = int(cur.lastrowid)
    conn.close()
    return new_id


def list_uzi_evidence(code: str | None = None, *, active_only: bool = True, limit: int = 50) -> list[dict]:
    """按时间倒序读取 UZI 证据账本。"""
    conn = get_conn()
    _ensure_uzi_evidence_table(conn)
    clauses = []
    params: list[Any] = []
    if code:
        clauses.append("code = ?")
        params.append(code)
    if active_only:
        clauses.append("active = 1")
        today = date.today().isoformat()
        clauses.append("(expires_on = '' OR expires_on IS NULL OR expires_on >= ?)")
        params.append(today)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"""
        SELECT * FROM uzi_evidence
        {where}
        ORDER BY event_date DESC, created_at DESC, id DESC
        LIMIT ?
    """, (*params, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_uzi_evidence_summary(code: str) -> dict:
    """汇总单只股票的有效 UZI 证据，用于评分层。"""
    rows = list_uzi_evidence(code, active_only=True, limit=100)
    counts = {"strong": 0, "medium": 0, "weak": 0}
    titles = []
    latest_date = ""
    total_impact = 0.0
    for row in rows:
        strength = row.get("strength", "medium")
        if strength in counts:
            counts[strength] += 1
        if row.get("title"):
            titles.append(row["title"])
        latest_date = max(latest_date, row.get("event_date") or "")
        total_impact += float(row.get("impact") or 0)

    if counts["strong"]:
        grade = "strong"
    elif counts["medium"]:
        grade = "medium"
    elif counts["weak"]:
        grade = "weak"
    else:
        grade = "none"

    return {
        "code": code,
        "grade": grade,
        "counts": counts,
        "total": sum(counts.values()),
        "latest_date": latest_date,
        "titles": titles[:6],
        "total_impact": round(total_impact, 2),
        "records": rows[:10],
    }


# ---------- 异常事件 ----------

def add_anomaly(code: str, level: str, alert_type: str, price: float, message: str, data: dict = None):
    """记录异常事件"""
    conn = get_conn()
    import json
    conn.execute("""
        INSERT INTO anomalies (code, level, alert_type, price, message, data)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (code, level, alert_type, price, message, json.dumps(data or {})))
    conn.commit()
    conn.close()


def get_unacknowledged_anomalies(limit: int = 20) -> list[dict]:
    """获取未确认的异常事件"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT a.*, s.name FROM anomalies a
        LEFT JOIN stocks s ON a.code = s.code
        WHERE a.acknowledged=0
        ORDER BY a.created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_today_anomalies(code: str = None) -> list[dict]:
    """获取今日异常事件"""
    from datetime import date
    today = date.today().isoformat()
    conn = get_conn()
    if code:
        rows = conn.execute("""
            SELECT a.*, s.name FROM anomalies a
            LEFT JOIN stocks s ON a.code = s.code
            WHERE date(a.created_at)=? AND a.code=?
            ORDER BY a.created_at DESC
        """, (today, code)).fetchall()
    else:
        rows = conn.execute("""
            SELECT a.*, s.name FROM anomalies a
            LEFT JOIN stocks s ON a.code = s.code
            WHERE date(a.created_at)=?
            ORDER BY a.created_at DESC
        """, (today,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def acknowledge_anomaly(anomaly_id: int):
    conn = get_conn()
    conn.execute("UPDATE anomalies SET acknowledged=1 WHERE id=?", (anomaly_id,))
    conn.commit()
    conn.close()


# ---------- 行情历史 ----------

def save_price_history(code: str, data: dict):
    """保存每日行情"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO price_history (code, date, open, close, high, low, volume, change_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, date) DO UPDATE SET
            open=excluded.open, close=excluded.close,
            high=excluded.high, low=excluded.low,
            volume=excluded.volume, change_pct=excluded.change_pct
    """, (
        data.get("code"), data.get("date"),
        data.get("open"), data.get("close"),
        data.get("high"), data.get("low"),
        data.get("volume"), data.get("change_pct")
    ))
    conn.commit()
    conn.close()


def get_price_history(code: str, days: int = 20) -> list[dict]:
    """获取最近N天的行情数据"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM price_history WHERE code=?
        ORDER BY date DESC LIMIT ?
    """, (code, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_avg_volume(code: str, days: int = 10) -> float:
    """获取最近N天的平均成交量"""
    rows = get_price_history(code, days)
    volumes = [r["volume"] for r in rows if r.get("volume")]
    return sum(volumes) / len(volumes) if volumes else 0


# ---------- Serenity 建议 ----------

def save_serenity_suggestion(source: str, content: str, context: str = ""):
    """保存一条 Serenity 推文建议"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO serenity_suggestions (source, content, context)
        VALUES (?, ?, ?)
    """, (source, content, context))
    conn.commit()
    conn.close()


def get_new_serenity_suggestions() -> list[dict]:
    """获取所有未读建议，按时间倒序"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM serenity_suggestions
        WHERE is_new=1
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def acknowledge_all_serenity_suggestions():
    """将所有建议标记为已读"""
    conn = get_conn()
    conn.execute("UPDATE serenity_suggestions SET is_new=0 WHERE is_new=1")
    conn.commit()
    conn.close()


# ---------- 信号日志 ----------

def save_signal_log(code: str, action: str, total_score: float, price: float,
                    is_holding: bool = False, tech_score: float = 0,
                    serenity_score: float = 0, alpha_score: float = 0,
                    fundamental_score: float = None, details: dict = None):
    """记录一次信号发出（每天每标仅保留最新一条，UPSERT 模式）"""
    from datetime import date, datetime
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M")
    conn = get_conn()

    # 确保唯一约束（先删后建，防 "IF NOT EXISTS 跳过非唯一索引" 问题）
    try:
        conn.execute("DROP INDEX IF EXISTS idx_signal_log_code_date")
        conn.execute("CREATE UNIQUE INDEX idx_signal_log_code_date ON signal_log(code, date)")
    except Exception:
        pass

    conn.execute("""
        INSERT INTO signal_log (code, date, time, action, total_score, price,
            is_holding, tech_score, serenity_score, alpha_score,
            fundamental_score, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, date) DO UPDATE SET
            time=excluded.time,
            action=excluded.action,
            total_score=excluded.total_score,
            price=excluded.price,
            is_holding=excluded.is_holding,
            tech_score=excluded.tech_score,
            serenity_score=excluded.serenity_score,
            alpha_score=excluded.alpha_score,
            fundamental_score=excluded.fundamental_score,
            details=excluded.details
    """, (code, today, now, action, total_score, price,
          int(is_holding), tech_score, serenity_score, alpha_score,
          fundamental_score, str(details or {})))
    conn.commit()
    conn.close()


def update_signal_outcome(signal_id: int, field: str, value: float):
    """更新信号 outcomes（1d/3d/5d/10d）"""
    allowed = {"outcome_1d", "outcome_3d", "outcome_5d", "outcome_10d"}
    if field not in allowed:
        raise ValueError(f"Invalid outcome field: {field}")
    conn = get_conn()
    conn.execute(f"UPDATE signal_log SET {field}=? WHERE id=?", (value, signal_id))
    conn.commit()
    conn.close()


# ── 🆕 v3.1 信号→实盘闭环 ────────────────────────────────────

def link_signal_to_trade(code: str, trade_action: str, trade_id: int,
                         trade_date: str, trade_price: float) -> int | None:
    """将一笔实盘交易关联到最近的同方向信号

    匹配逻辑:
    - BUY 交易 → 匹配最近 3 天内的 BUY/STRONG_BUY/CAUTION_BUY 信号
    - SELL 交易 → 匹配最近 3 天内的 SELL/STOP_LOSS 信号
    - 优先匹配同一天的信号

    Returns: signal_log.id if matched, None otherwise
    """
    from datetime import date as dt, timedelta

    if trade_action.upper() == "BUY":
        signal_types = ("STRONG_BUY", "BUY", "CAUTION_BUY")
    elif trade_action.upper() == "SELL":
        signal_types = ("SELL", "STOP_LOSS", "WATCH", "WEAK_HOLD")
    else:
        return None

    since = (dt.fromisoformat(trade_date) - timedelta(days=3)).isoformat()
    conn = get_conn()

    try:
        # 同天信号优先
        placeholders = ",".join("?" * len(signal_types))
        row = conn.execute(
            f"SELECT id, action, total_score FROM signal_log "
            f"WHERE code=? AND date=? AND action IN ({placeholders}) "
            f"ORDER BY total_score DESC LIMIT 1",
            (code, trade_date, *signal_types),
        ).fetchone()

        if not row:
            # 放宽到 3 天内
            row = conn.execute(
                f"SELECT id, action, total_score FROM signal_log "
                f"WHERE code=? AND date>=? AND date<=? AND action IN ({placeholders}) "
                f"ORDER BY date DESC, total_score DESC LIMIT 1",
                (code, since, trade_date, *signal_types),
            ).fetchone()

        if row:
            # 写入映射
            try:
                conn.execute(
                    "INSERT INTO signal_to_trade "
                    "(signal_date, code, signal_action, signal_score, "
                    "trade_id, trade_action, matched, match_date, notes) "
                    "VALUES (?,?,?,?,?,?,1,?,?)",
                    (trade_date, code, row["action"], row["total_score"],
                     trade_id, trade_action, dt.today().isoformat(),
                     "auto-matched"),
                )
                conn.commit()
            except Exception:
                pass  # 幂等忽略
            return row["id"]
    finally:
        conn.close()
    return None


def get_signal_execution_rate(days: int = 30) -> dict:
    """统计近 N 天信号执行率

    Returns:
        {by_action: {STRONG_BUY: {signals, executed, rate}, ...},
         overall: {total, executed, rate}}
    """
    from datetime import date as dt, timedelta

    since = (dt.today() - timedelta(days=days)).isoformat()
    conn = get_conn()

    by_action = {}
    try:
        # 总信号数
        rows = conn.execute(
            "SELECT action, COUNT(*) as cnt FROM signal_log "
            "WHERE date>=? AND action IN ('STRONG_BUY','BUY','SELL','STOP_LOSS')"
            "GROUP BY action", (since,)
        ).fetchall()

        for r in rows:
            action = r[0]
            total = r[1]
            # 匹配数
            matched = conn.execute(
                "SELECT COUNT(*) FROM signal_to_trade stt "
                "JOIN signal_log sl ON stt.signal_date=sl.date AND stt.code=sl.code "
                "AND stt.signal_action=sl.action "
                "WHERE sl.action=? AND sl.date>=?",
                (action, since),
            ).fetchone()[0]

            rate = round(matched / total * 100, 1) if total > 0 else 0
            by_action[action] = {
                "signals": total, "executed": matched, "rate_pct": rate,
            }

        total = sum(v["signals"] for v in by_action.values())
        executed = sum(v["executed"] for v in by_action.values())
        overall = {
            "total": total, "executed": executed,
            "rate_pct": round(executed / total * 100, 1) if total > 0 else 0,
        }
    finally:
        conn.close()

    return {"by_action": by_action, "overall": overall, "window": f"{days}d"}


def classify_trade_discipline(days: int = 60) -> dict:
    """分析交易纪律：每笔 trade 是否按信号执行

    Returns:
        {disciplined: count, impulsive: count, no_signal: count,
         trades: [{code, action, date, signal_action, score, is_disciplined}]}
    """
    from datetime import date as dt, timedelta

    since = (dt.today() - timedelta(days=days)).isoformat()
    conn = get_conn()

    disciplined = 0
    impulsive = 0
    no_signal = 0
    trades = []

    try:
        rows = conn.execute(
            "SELECT id, code, action, date, price, note FROM trades "
            "WHERE code!='CASH' AND date>=? ORDER BY date DESC",
            (since,),
        ).fetchall()

        for t in rows:
            matched = conn.execute(
                "SELECT signal_action, signal_score FROM signal_to_trade "
                "WHERE trade_id=? AND matched=1", (t["id"],)
            ).fetchone()

            if matched:
                disciplined += 1
                tag = "disciplined"
                sig_act = matched[0]
                sig_score = matched[1]
            else:
                # 看是否同方向信号存在但未匹配
                if t["action"].upper() == "BUY":
                    signal_types = ("STRONG_BUY", "BUY", "CAUTION_BUY")
                else:
                    signal_types = ("SELL", "STOP_LOSS", "WATCH")

                ph = ",".join("?" * len(signal_types))
                sig = conn.execute(
                    f"SELECT action FROM signal_log WHERE code=? AND date=? "
                    f"AND action IN ({ph}) LIMIT 1",
                    (t["code"], t["date"], *signal_types),
                ).fetchone()

                if sig:
                    impulsive += 1
                    tag = "impulsive"
                    sig_act = f"IGNORED {sig[0]}"
                    sig_score = 0
                else:
                    no_signal += 1
                    tag = "no_signal"
                    sig_act = "NONE"
                    sig_score = 0

            trades.append({
                "code": t["code"],
                "action": t["action"],
                "date": t["date"],
                "signal_action": sig_act,
                "score": sig_score,
                "is_disciplined": tag == "disciplined",
                "tag": tag,
            })
    finally:
        conn.close()

    return {
        "disciplined": disciplined,
        "impulsive": impulsive,
        "no_signal": no_signal,
        "total": disciplined + impulsive + no_signal,
        "discipline_rate": round(
            disciplined / max(1, disciplined + impulsive) * 100, 1
        ),
        "trades": trades[:30],
        "window": f"{days}d",
    }


def refresh_signal_performance():
    """从 signal_log 聚合计算各标的各信号类型的绩效"""
    conn = get_conn()
    conn.execute("DELETE FROM signal_performance")
    conn.execute("""
        INSERT INTO signal_performance (code, action, total_signals,
            wins_1d, wins_3d, wins_5d,
            avg_return_1d, avg_return_3d, avg_return_5d)
        SELECT
            code, action, COUNT(*) as total,
            SUM(CASE WHEN outcome_1d > 0 THEN 1 ELSE 0 END) as w1,
            SUM(CASE WHEN outcome_3d > 0 THEN 1 ELSE 0 END) as w3,
            SUM(CASE WHEN outcome_5d > 0 THEN 1 ELSE 0 END) as w5,
            ROUND(AVG(outcome_1d), 2) as r1,
            ROUND(AVG(outcome_3d), 2) as r3,
            ROUND(AVG(outcome_5d), 2) as r5
        FROM signal_log
        WHERE outcome_1d IS NOT NULL
        GROUP BY code, action
    """)
    conn.commit()
    conn.close()


def get_signal_performance(code: str = None, action: str = None, days: int = None):
    """查询信号绩效

    两种模式：
    1. 无 days 参数（默认）：从 signal_performance 预聚合表查询，返回 list[dict]
    2. 有 days 参数：从 signal_log 按天过滤并聚合，返回 dict[action] = {count, outcomes}
    """
    from datetime import date, timedelta

    conn = get_conn()

    # ---- 模式 2：按天过滤 + 聚合 ----
    if days is not None:
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT action,
                   COUNT(*) AS cnt,
                   SUM(CASE WHEN outcome_1d > 0 THEN 1 ELSE 0 END) AS wins_1d,
                   SUM(CASE WHEN outcome_3d > 0 THEN 1 ELSE 0 END) AS wins_3d,
                   SUM(CASE WHEN outcome_5d > 0 THEN 1 ELSE 0 END) AS wins_5d,
                   AVG(outcome_1d) AS avg_return_1d,
                   AVG(outcome_3d) AS avg_return_3d,
                   AVG(outcome_5d) AS avg_return_5d
            FROM signal_log
            WHERE date >= ?
               AND (? IS NULL OR action = ?)
            GROUP BY action
            ORDER BY cnt DESC
        """, (since, action, action) if action else (since, None, None)).fetchall()

        conn.close()
        result = {}
        for r in rows:
            cnt = r["cnt"]
            result[r["action"]] = {
                "count": cnt,
                "outcomes": {
                    "outcome_1d": {
                        "hit_rate": round(r["wins_1d"] / cnt * 100, 1) if cnt else 0,
                        "avg_return": round(r["avg_return_1d"] * 100, 2) if r["avg_return_1d"] else 0,
                    },
                    "outcome_3d": {
                        "hit_rate": round(r["wins_3d"] / cnt * 100, 1) if cnt else 0,
                        "avg_return": round(r["avg_return_3d"] * 100, 2) if r["avg_return_3d"] else 0,
                    },
                    "outcome_5d": {
                        "hit_rate": round(r["wins_5d"] / cnt * 100, 1) if cnt else 0,
                        "avg_return": round(r["avg_return_5d"] * 100, 2) if r["avg_return_5d"] else 0,
                    },
                },
            }
        return result

    # ---- 模式 1：从预聚合表查询（旧行为） ----
    q = "SELECT * FROM signal_performance WHERE 1=1"
    params = []
    if code:
        q += " AND code=?"
        params.append(code)
    if action:
        q += " AND action=?"
        params.append(action)
    q += " ORDER BY total_signals DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_signals(code: str = None, days: int = 7, limit: int = 50) -> list[dict]:
    """获取最近N天的信号记录"""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn()
    if code:
        rows = conn.execute("""
            SELECT s.*, t.name FROM signal_log s
            LEFT JOIN stocks t ON s.code = t.code
            WHERE s.code=? AND s.date>=?
            ORDER BY s.created_at DESC LIMIT ?
        """, (code, since, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT s.*, t.name FROM signal_log s
            LEFT JOIN stocks t ON s.code = t.code
            WHERE s.date>=?
            ORDER BY s.created_at DESC LIMIT ?
        """, (since, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]





def get_unfilled_outcomes(since_days: int = 30) -> list[dict]:
    """获取 outcomes 还未填充的信号（用于每日补填）"""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=since_days)).isoformat()
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, code, date, price FROM signal_log
        WHERE (outcome_1d IS NULL OR outcome_3d IS NULL
               OR outcome_5d IS NULL OR outcome_10d IS NULL)
          AND date>=?
        ORDER BY date ASC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 评分反思 ----------

def save_reflection(code: str, reflection: dict):
    """保存每日评分反思记录"""
    import json
    conn = get_conn()
    conn.execute("""
        INSERT INTO score_reflections (code, date, total_score, dimension_scores,
            predicted_direction, dimension_ic, reflection_text)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, date) DO UPDATE SET
            total_score=excluded.total_score,
            dimension_scores=excluded.dimension_scores,
            predicted_direction=excluded.predicted_direction,
            dimension_ic=excluded.dimension_ic,
            reflection_text=excluded.reflection_text
    """, (
        code, reflection.get("date"),
        reflection.get("total_score", 0),
        json.dumps(reflection.get("dimension_scores", {})),
        reflection.get("predicted_direction", ""),
        json.dumps(reflection.get("dimension_ic", {})),
        reflection.get("reflection_text", ""),
    ))
    conn.commit()
    conn.close()


def update_reflection_outcome(code: str, date_str: str,
                               actual_return_1d: float = None,
                               actual_return_3d: float = None,
                               actual_return_5d: float = None,
                               dimension_ic: dict = None):
    """更新反思的实际收益和维度IC"""
    import json
    conn = get_conn()
    updates = []
    params = []
    if actual_return_1d is not None:
        updates.append("actual_return_1d = ?")
        params.append(actual_return_1d)
    if actual_return_3d is not None:
        updates.append("actual_return_3d = ?")
        params.append(actual_return_3d)
    if actual_return_5d is not None:
        updates.append("actual_return_5d = ?")
        params.append(actual_return_5d)
    if dimension_ic is not None:
        updates.append("dimension_ic = ?")
        params.append(json.dumps(dimension_ic))
    if not updates:
        return
    params.extend([code, date_str])
    conn.execute(f"UPDATE score_reflections SET {', '.join(updates)} WHERE code=? AND date=?",
                 params)
    conn.commit()
    conn.close()


def get_reflections(code: str = None, days: int = 30) -> list[dict]:
    """获取评分反思记录"""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn()
    if code:
        rows = conn.execute("""
            SELECT r.*, s.name FROM score_reflections r
            LEFT JOIN stocks s ON r.code = s.code
            WHERE r.code=? AND r.date>=?
            ORDER BY r.date DESC
        """, (code, since)).fetchall()
    else:
        rows = conn.execute("""
            SELECT r.*, s.name FROM score_reflections r
            LEFT JOIN stocks s ON r.code = s.code
            WHERE r.date>=?
            ORDER BY r.date DESC
        """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unfilled_reflections(since_days: int = 30) -> list[dict]:
    """获取actual_return还未填充的反思记录"""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=since_days)).isoformat()
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, code, date, total_score FROM score_reflections
        WHERE actual_return_1d IS NULL AND date>=?
        ORDER BY date ASC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_reflection_dimension_ic(days: int = 30) -> dict:
    """聚合最近N天的维度IC均值"""
    from datetime import date, timedelta
    import json
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn()
    rows = conn.execute("""
        SELECT dimension_ic FROM score_reflections
        WHERE dimension_ic IS NOT NULL AND dimension_ic != '{}' AND date>=?
    """, (since,)).fetchall()
    conn.close()

    if not rows:
        return {}

    # 聚合所有维度IC
    from collections import defaultdict
    ic_sums = defaultdict(float)
    ic_counts = defaultdict(int)
    for r in rows:
        try:
            ic_data = json.loads(r["dimension_ic"])
        except (json.JSONDecodeError, TypeError):
            continue
        for dim, val in ic_data.items():
            if val is not None:
                try:
                    ic_sums[dim] += float(val)
                    ic_counts[dim] += 1
                except (TypeError, ValueError):
                    continue

    return {
        dim: round(ic_sums[dim] / ic_counts[dim], 4)
        for dim in ic_sums
        if ic_counts[dim] > 0
    }

# ---------- 权重辩论日志 ----------

def save_conviction_log(entry: dict):
    """保存一次权重辩论结果（每天一条，UPSERT）"""
    import json
    conn = get_conn()
    conn.execute("""
        INSERT INTO conviction_log (date, regime, debated_weights, regime_weights,
            score_avg, high_count, low_count, position_advice)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            regime=excluded.regime,
            debated_weights=excluded.debated_weights,
            regime_weights=excluded.regime_weights,
            score_avg=excluded.score_avg,
            high_count=excluded.high_count,
            low_count=excluded.low_count,
            position_advice=excluded.position_advice
    """, (
        entry.get("date", ""),
        entry.get("regime", ""),
        json.dumps(entry.get("debated_weights", {})),
        json.dumps(entry.get("regime_weights", {})),
        entry.get("score_avg", 0),
        entry.get("high_count", 0),
        entry.get("low_count", 0),
        entry.get("position_advice", ""),
    ))
    conn.commit()
    conn.close()


def get_conviction_history(days: int = 30) -> list[dict]:
    """获取历史权重辩论记录"""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM conviction_log
        WHERE date>=?
        ORDER BY date DESC
    """, (since,)).fetchall()
    conn.close()
    result = []
    import json
    for r in rows:
        d = dict(r)
        try:
            d["debated_weights"] = json.loads(d.get("debated_weights", "{}"))
        except (json.JSONDecodeError, TypeError):
            d["debated_weights"] = {}
        try:
            d["regime_weights"] = json.loads(d.get("regime_weights", "{}"))
        except (json.JSONDecodeError, TypeError):
            d["regime_weights"] = {}
        result.append(d)
    return result


def get_latest_conviction() -> Optional[dict]:
    """获取最新一条权重辩论记录"""
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM conviction_log
        ORDER BY date DESC LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    import json
    for field in ["debated_weights", "regime_weights"]:
        try:
            d[field] = json.loads(d.get(field, "{}"))
        except (json.JSONDecodeError, TypeError):
            d[field] = {}
    return d
