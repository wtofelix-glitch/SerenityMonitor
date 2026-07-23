"""
Serenity 2.0 — 信息事件统一模型 (P2)

根据 docs/P2-信息事件统一模型.md 定义。
EventRecord 是情报网(C层)和信号台(D层)之间的接口。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any

# ---------------------------------------------------------------------------
# 枚举定义
# ---------------------------------------------------------------------------

EVENT_TYPES = [
    "price_anomaly", "volume_anomaly", "board_anomaly",
    "announcement", "regulatory", "policy",
    "index_movement", "sector_rotation",
    "capital_flow", "news",
    "rule_change", "technical_level",
    "system_status",
]

IMPACT_DIRECTIONS = ["bullish", "bearish", "neutral", "uncertain"]
IMPACT_STRENGTHS = ["high", "medium", "low"]
IMPACT_HORIZONS = ["intraday", "short_term", "medium_term", "long_term"]
VERIFICATION_STATUSES = ["verified", "self_verified", "contradicting", "pending", "outdated"]
SOURCE_LEVELS = ["S", "A", "B", "C"]
PRIORITY_LEVELS = ["P0", "P1", "P2", "P3"]

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class SourceInfo:
    """信息源"""
    name: str
    level: str                    # S / A / B / C
    url: str = ""
    publish_time: str = ""        # ISO 8601


@dataclass
class TimestampSet:
    """完整时间戳链"""
    event_time: str = ""          # 事件真正发生时间
    publish_time: str = ""        # 来源首次发布时间
    collected_at: str = ""        # 系统采集时间
    verified_at: str = ""         # 验证完成时间
    pushed_at: str = ""           # 推送到用户时间
    expires_at: str = ""          # 有效期截止


@dataclass
class EventPayload:
    """事件具体数据（按 event_type 不同字段不同）"""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelatedInfo:
    """关联标的"""
    direct_symbols: list[str] = field(default_factory=list)
    indirect_symbols: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    indices: list[str] = field(default_factory=list)


@dataclass
class ImpactAssessment:
    """影响评估"""
    direction: str = "neutral"     # bullish / bearish / neutral / uncertain
    strength: str = "low"          # high / medium / low
    horizon: str = "short_term"    # intraday / short_term / medium_term / long_term


@dataclass
class VerificationResult:
    """验证结果"""
    status: str = "pending"        # verified / self_verified / contradicting / pending / outdated
    method: str = "self_verified"
    sources_used: int = 0
    latency_seconds: float = 0
    conflicts: list[str] = field(default_factory=list)


@dataclass
class AccountRelevance:
    """账户相关性"""
    is_holding: bool = False
    position_shares: int = 0
    position_value: float = 0.0
    position_weight_pct: float = 0.0
    distance_to_stop: float = 0.0
    pnl_today: float = 0.0


@dataclass
class EventRecord:
    """
    统一事件模型 —— 所有情报源的标准化输出格式。

    对应 docs/P2-信息事件统一模型.md 的完整定义。
    """
    event_id: str = ""
    symbol: str = ""               # 主标的代码
    event_type: str = "news"       # 见 EVENT_TYPES
    headline: str = ""
    summary: str = ""

    source: SourceInfo = field(default_factory=SourceInfo)
    timestamps: TimestampSet = field(default_factory=TimestampSet)
    payload: EventPayload = field(default_factory=EventPayload)
    related: RelatedInfo = field(default_factory=RelatedInfo)
    impact: ImpactAssessment = field(default_factory=ImpactAssessment)
    verification: VerificationResult = field(default_factory=VerificationResult)
    account_relevance: AccountRelevance = field(default_factory=AccountRelevance)

    signal_eligible: bool = False
    action_eligible: bool = True     # 源数据是否可用于生成交易动作
    priority: str = "P3"             # P0 / P1 / P2 / P3
    raw_content_hash: str = ""


# ---------------------------------------------------------------------------
# DB 存储层
# ---------------------------------------------------------------------------

# DB_PATH 不再有模块级默认值。由 SerenityEnv 注入。
# EventStore 必须显式传入 db_path 参数。

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS serenity_events (
    event_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    headline TEXT DEFAULT '',
    summary TEXT DEFAULT '',

    -- 来源
    source_name TEXT DEFAULT '',
    source_level TEXT DEFAULT 'C',
    source_url TEXT DEFAULT '',
    source_publish_time TEXT DEFAULT '',

    -- 时间戳链
    event_time TEXT DEFAULT '',
    publish_time TEXT DEFAULT '',
    collected_at TEXT DEFAULT '',
    verified_at TEXT DEFAULT '',
    pushed_at TEXT DEFAULT '',
    expires_at TEXT DEFAULT '',

    -- 载荷
    payload_json TEXT DEFAULT '{}',

    -- 关联
    related_direct TEXT DEFAULT '[]',
    related_indirect TEXT DEFAULT '[]',
    related_sectors TEXT DEFAULT '[]',
    related_indices TEXT DEFAULT '[]',

    -- 影响
    impact_direction TEXT DEFAULT 'neutral',
    impact_strength TEXT DEFAULT 'low',
    impact_horizon TEXT DEFAULT 'short_term',

    -- 验证
    verification_status TEXT DEFAULT 'pending',
    verification_method TEXT DEFAULT '',
    verification_sources_used INTEGER DEFAULT 0,
    verification_latency REAL DEFAULT 0,
    verification_conflicts TEXT DEFAULT '[]',

    -- 账户
    account_is_holding INTEGER DEFAULT 0,
    account_position_shares INTEGER DEFAULT 0,
    account_position_value REAL DEFAULT 0,
    account_position_weight REAL DEFAULT 0,
    account_distance_to_stop REAL DEFAULT 0,
    account_pnl_today REAL DEFAULT 0,

    -- 信号
    signal_eligible INTEGER DEFAULT 0,
    priority TEXT DEFAULT 'P3',
    raw_content_hash TEXT DEFAULT '',

    -- 元数据
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


class EventStore:
    """EventRecord 的持久化存储。"""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            from .env import get_env
            try:
                db_path = get_env().db_path
            except RuntimeError:
                raise RuntimeError(
                    "EventStore 必须传入 db_path 或先调用 serenity_v2.env.set_env()"
                )
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_schema(self) -> None:
        """初始化 events 表（幂等）。"""
        conn = self._get_conn()
        try:
            conn.execute(CREATE_EVENTS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_serenity_events_symbol "
                "ON serenity_events(symbol)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_serenity_events_priority "
                "ON serenity_events(priority)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_serenity_events_collected "
                "ON serenity_events(collected_at)"
            )
            conn.commit()
        finally:
            conn.close()

    def insert(self, event: EventRecord) -> str:
        """写入一条事件。如果 event_id 已存在则跳过。返回 event_id。"""
        conn = self._get_conn()
        try:
            if not event.event_id:
                event.event_id = self._generate_id(event)

            conn.execute(
                """INSERT OR IGNORE INTO serenity_events
                (event_id, symbol, event_type, headline, summary,
                 source_name, source_level, source_url, source_publish_time,
                 event_time, publish_time, collected_at, verified_at, pushed_at, expires_at,
                 payload_json,
                 related_direct, related_indirect, related_sectors, related_indices,
                 impact_direction, impact_strength, impact_horizon,
                 verification_status, verification_method, verification_sources_used,
                 verification_latency, verification_conflicts,
                 account_is_holding, account_position_shares, account_position_value,
                 account_position_weight, account_distance_to_stop, account_pnl_today,
                 signal_eligible, priority, raw_content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?)""",
                (
                    event.event_id, event.symbol, event.event_type,
                    event.headline, event.summary,
                    event.source.name, event.source.level,
                    event.source.url, event.source.publish_time,
                    event.timestamps.event_time, event.timestamps.publish_time,
                    event.timestamps.collected_at, event.timestamps.verified_at,
                    event.timestamps.pushed_at, event.timestamps.expires_at,
                    json.dumps(event.payload.data, ensure_ascii=False),
                    json.dumps(event.related.direct_symbols, ensure_ascii=False),
                    json.dumps(event.related.indirect_symbols, ensure_ascii=False),
                    json.dumps(event.related.sectors, ensure_ascii=False),
                    json.dumps(event.related.indices, ensure_ascii=False),
                    event.impact.direction, event.impact.strength, event.impact.horizon,
                    event.verification.status, event.verification.method,
                    event.verification.sources_used, event.verification.latency_seconds,
                    json.dumps(event.verification.conflicts, ensure_ascii=False),
                    int(event.account_relevance.is_holding),
                    event.account_relevance.position_shares,
                    event.account_relevance.position_value,
                    event.account_relevance.position_weight_pct,
                    event.account_relevance.distance_to_stop,
                    event.account_relevance.pnl_today,
                    int(event.signal_eligible),
                    event.priority,
                    event.raw_content_hash,
                ),
            )
            conn.commit()
            return event.event_id
        finally:
            conn.close()

    def query_recent(
        self,
        symbol: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 20,
    ) -> list[EventRecord]:
        """查询最近的事件。"""
        conn = self._get_conn()
        try:
            where = ["1=1"]
            params: list[Any] = []
            if symbol:
                where.append("symbol = ?")
                params.append(symbol)
            if priority:
                where.append("priority = ?")
                params.append(priority)

            rows = conn.execute(
                f"SELECT * FROM serenity_events WHERE {' AND '.join(where)} "
                f"ORDER BY collected_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()

            return [self._row_to_event(r) for r in rows]
        finally:
            conn.close()

    def query_active(self, symbol: Optional[str] = None) -> list[EventRecord]:
        """查询仍然有效且未推送的事件（用于信号生成）。"""
        conn = self._get_conn()
        try:
            from .clock import get_clock
            now = get_clock().now().isoformat(timespec="seconds")
            where = [
                "verification_status IN ('verified', 'self_verified')",
                "signal_eligible = 1",
                "(expires_at = '' OR expires_at > ?)",
            ]
            params: list[Any] = [now]
            if symbol:
                where.append("symbol = ?")
                params.append(symbol)

            rows = conn.execute(
                f"SELECT * FROM serenity_events WHERE {' AND '.join(where)} "
                f"ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
                f"WHEN 'P2' THEN 2 ELSE 3 END, collected_at DESC",
                params,
            ).fetchall()

            return [self._row_to_event(r) for r in rows]
        finally:
            conn.close()

    def mark_pushed(self, event_id: str) -> None:
        """标记事件已推送。"""
        conn = self._get_conn()
        try:
            from .clock import get_clock
            now = get_clock().now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE serenity_events SET pushed_at = ?, updated_at = ? "
                "WHERE event_id = ?",
                (now, now, event_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _row_to_event(self, row: sqlite3.Row) -> EventRecord:
        """sqlite3.Row → EventRecord。"""
        return EventRecord(
            event_id=row["event_id"],
            symbol=row["symbol"],
            event_type=row["event_type"],
            headline=row["headline"] or "",
            summary=row["summary"] or "",
            source=SourceInfo(
                name=row["source_name"] or "",
                level=row["source_level"] or "C",
                url=row["source_url"] or "",
                publish_time=row["source_publish_time"] or "",
            ),
            timestamps=TimestampSet(
                event_time=row["event_time"] or "",
                publish_time=row["publish_time"] or "",
                collected_at=row["collected_at"] or "",
                verified_at=row["verified_at"] or "",
                pushed_at=row["pushed_at"] or "",
                expires_at=row["expires_at"] or "",
            ),
            payload=EventPayload(data=json.loads(row["payload_json"] or "{}")),
            related=RelatedInfo(
                direct_symbols=json.loads(row["related_direct"] or "[]"),
                indirect_symbols=json.loads(row["related_indirect"] or "[]"),
                sectors=json.loads(row["related_sectors"] or "[]"),
                indices=json.loads(row["related_indices"] or "[]"),
            ),
            impact=ImpactAssessment(
                direction=row["impact_direction"] or "neutral",
                strength=row["impact_strength"] or "low",
                horizon=row["impact_horizon"] or "short_term",
            ),
            verification=VerificationResult(
                status=row["verification_status"] or "pending",
                method=row["verification_method"] or "",
                sources_used=row["verification_sources_used"] or 0,
                latency_seconds=row["verification_latency"] or 0,
                conflicts=json.loads(row["verification_conflicts"] or "[]"),
            ),
            account_relevance=AccountRelevance(
                is_holding=bool(row["account_is_holding"]),
                position_shares=row["account_position_shares"] or 0,
                position_value=row["account_position_value"] or 0,
                position_weight_pct=row["account_position_weight"] or 0,
                distance_to_stop=row["account_distance_to_stop"] or 0,
                pnl_today=row["account_pnl_today"] or 0,
            ),
            signal_eligible=bool(row["signal_eligible"]),
            priority=row["priority"] or "P3",
            raw_content_hash=row["raw_content_hash"] or "",
        )

    def _generate_id(self, event: EventRecord) -> str:
        """生成唯一 event_id: EVT_YYYYMMDD_HHMMSS_hash6。"""
        from .clock import get_clock
        now = get_clock().now()
        ts = now.strftime("%Y%m%d_%H%M%S")
        digest = hashlib.sha256(
            f"{event.symbol}_{event.event_type}_{event.headline}_{ts}".encode()
        ).hexdigest()[:6]
        return f"EVT_{ts}_{digest}"


# ---------------------------------------------------------------------------
# 便捷构造器
# ---------------------------------------------------------------------------


def make_price_event(
    code: str, name: str, price: float, change_pct: float,
    volume: int = 0, amount: float = 0,
    turnover: float = 0,
    is_holding: bool = False,
) -> EventRecord:
    """快速创建行情异动事件。"""
    from .clock import get_clock
    now = get_clock().now()
    ts = now.isoformat(timespec="seconds")

    # 判定方向
    if change_pct > 4:
        direction = "bullish"
    elif change_pct < -4:
        direction = "bearish"
    else:
        direction = "neutral"

    # 判定强度
    abs_pct = abs(change_pct)
    if abs_pct > 7:
        strength = "high"
    elif abs_pct > 4:
        strength = "medium"
    else:
        strength = "low"

    event = EventRecord(
        symbol=code,
        event_type="price_anomaly",
        headline=f"{name}({code}) 涨跌{change_pct:+.2f}%",
        summary=f"现价{price:.2f}, 涨跌{change_pct:+.2f}%, "
                 f"成交额{amount/1e8:.2f}亿, 换手率{turnover:.2f}%",
        source=SourceInfo(
            name="Sina实时行情", level="A",
            url=f"https://hq.sinajs.cn/list={code}",
            publish_time=ts,
        ),
        timestamps=TimestampSet(
            event_time=ts, publish_time=ts,
            collected_at=ts, verified_at=ts,
            expires_at=(now + timedelta(minutes=30)).isoformat(timespec="seconds"),
        ),
        payload=EventPayload(data={
            "price": price, "change_pct": change_pct,
            "volume": volume, "amount": amount,
            "turnover_rate": turnover,
        }),
        related=RelatedInfo(
            direct_symbols=[code],
            indirect_symbols=[],
            sectors=[],
        ),
        impact=ImpactAssessment(
            direction=direction, strength=strength, horizon="intraday",
        ),
        verification=VerificationResult(
            status="self_verified", method="single_source",
            sources_used=1, latency_seconds=0,
        ),
        account_relevance=AccountRelevance(is_holding=is_holding),
        signal_eligible=bool(is_holding and abs_pct > 4),
        priority=_calc_priority(is_holding, abs_pct),
    )
    event.event_id = EventStore()._generate_id(event)
    return event


def _calc_priority(is_holding: bool, abs_pct: float) -> str:
    """根据持仓和涨跌幅计算优先级。"""
    if not is_holding:
        return "P3"
    if abs_pct > 7:
        return "P0"
    if abs_pct > 4:
        return "P1"
    return "P2"
