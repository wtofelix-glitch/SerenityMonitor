"""
Serenity 2.0 — 情报网 (Phase 1)

根据 docs/C-情报网设计v1.md 设计。
负责：多源采集 → 标准化 → 验证 → 分级 → 推送决策。

不直接改行情/推送代码，作为独立的情报层运行。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any

from .account_baseline import AccountBaseline, SECTOR_MAP
from .event_record import (
    EventRecord, EventStore, SourceInfo, TimestampSet,
    EventPayload, RelatedInfo, ImpactAssessment,
    VerificationResult, AccountRelevance,
    make_price_event, SOURCE_LEVELS,
)

# ---------------------------------------------------------------------------
# 信息源注册表（docs/C §3）
# ---------------------------------------------------------------------------

REGISTERED_SOURCES: dict[str, dict[str, Any]] = {
    "SINA_REALTIME": {
        "name": "Sina实时行情",
        "level": "A",
        "coverage": "全A股实时交易数据",
        "latency": "<3s",
        "reliability": 5,
        "fallback": ["SINA_REALTIME_ALT"],
    },
    "CNINFO": {
        "name": "巨潮资讯网",
        "level": "S",
        "coverage": "A股法定公告",
        "latency": "T+0~T+1",
        "reliability": 5,
        "fallback": ["EASTMONEY_ANNC"],
    },
    "TRENDRADAR": {
        "name": "TrendRadar聚合",
        "level": "B",
        "coverage": "热榜新闻/RSS",
        "latency": "<5min",
        "reliability": 4,
        "fallback": ["EASTMONEY_NEWS"],
    },
    "EASTMONEY": {
        "name": "东方财富API",
        "level": "A",
        "coverage": "板块/资金流/概念",
        "latency": "<30s",
        "reliability": 4,
        "fallback": ["THS_API"],
    },
    "SINA_SECTOR": {
        "name": "Sina板块数据",
        "level": "B",
        "coverage": "行业涨跌/资金流向",
        "latency": "<30s",
        "reliability": 4,
        "fallback": [],
    },
}

# ---------------------------------------------------------------------------
# 情报优先级规则（docs/C §10）
# ---------------------------------------------------------------------------

P0_TRIGGERS = [
    "持仓股停牌/复牌/ST公告",
    "持仓股收到监管处罚/交易所问询函",
    "持仓股业绩预告变化>50%",
    "持仓股盘中涨跌超过3倍日均波幅",
    "持仓股涨跌停封死(T+1受限)",
    "持仓股重大资产重组公告",
]

P1_TRIGGERS = [
    "持仓股涨跌>4%",
    "持仓股量比>2.0或成交量突增>150%",
    "持仓股所在板块涨跌>3%",
    "持仓股发布一般性公告(分红/减持/合同)",
    "候选池标的触发异常阈值",
    "指数盘中跌>2%(全市场风险)",
]

P2_TRIGGERS = [
    "候选池标的涨跌>4%",
    "行业板块轮动迹象",
    "外围市场变化(隔夜美股>2%)",
    "技术位接近(持仓股接近关键支撑/阻力)",
]

# 防打扰规则（重构版）
ANTI_SPAM_RULES = {
    "same_stock_cooldown_minutes": 30,     # 同一标的30分钟内不重复推送
    "no_post_market_anomaly": True,         # 盘后不推送行情异动
    "deduplicate_cross_source": True,       # 跨来源重复合并
    "max_p1_per_morning": 3,                # 上午P1限额3条
}

# 集合竞价静默规则：按信号类型区分，非全局静默
PREMARKET_SILENCE = {
    # 交易行动信号：竞价阶段抑制
    "trade_action": True,        # BUY/ADD/REDUCE/SELL 不推送
    # 重大情报：不停默
    "major_announcement": False,  # 停复牌、监管、重大公告仍然推送
    # 价格异动：不套连续竞价阈值，标记为竞价数据
    "price_anomaly": "flag_only",  # 标记但不抑制
}

# ---------------------------------------------------------------------------
# 情报管理器
# ---------------------------------------------------------------------------


class IntelligenceNetwork:
    """
    情报网核心引擎。

    职责：
    1. 接收各源原始数据 → 标准化为 EventRecord
    2. 执行多阶段验证
    3. 分配优先级
    4. 决策是否推送
    """

    def __init__(self, store: Optional[EventStore] = None, shadow_mode: bool = True):
        self.store = store or EventStore()
        self.store.init_schema()
        self.shadow_mode = shadow_mode  # 实例变量，不共享
        self._pushed_today: dict[str, int] = {}  # code -> last_push_minute
        self._p1_count_today: int = 0

    # ---- 事件摄入 ----

    def ingest(self, event: EventRecord) -> EventRecord:
        """摄入一条事件：标准化 + 验证 + 优先级计算 + 入库。"""
        from .clock import get_clock
        now = get_clock().now()
        ts = now.isoformat(timespec="seconds")

        # 补全时间戳
        if not event.timestamps.collected_at:
            event.timestamps.collected_at = ts

        # 计算原始内容哈希（去重用）
        event.raw_content_hash = hashlib.sha256(
            f"{event.symbol}_{event.event_type}_{event.headline}".encode()
        ).hexdigest()[:16]

        # 数据质量自检 (Stage 1)
        event = self._stage1_self_check(event)

        # 多源交叉验证 (Stage 2)
        event = self._stage2_cross_verify(event)

        # 时序验证 (Stage 3)
        event = self._stage3_timeliness(event)

        # 计算优先级
        event.priority = self._assign_priority(event)

        # 判定是否可生成信号
        event.signal_eligible = self._is_signal_eligible(event)

        # 入库
        self.store.insert(event)

        return event

    def ingest_price_anomaly(
        self, code: str, name: str, price: float, change_pct: float,
        volume: int = 0, amount: float = 0, turnover: float = 0,
        is_holding: bool = False,
    ) -> EventRecord:
        """快捷方法：从行情数据创建并摄入异动事件。"""
        event = make_price_event(
            code, name, price, change_pct,
            volume, amount, turnover, is_holding,
        )
        return self.ingest(event)

    # ---- 验证阶段 ----

    def _stage1_self_check(self, event: EventRecord) -> EventRecord:
        """Stage 1: 数据自检。"""
        issues = []

        # 格式检查
        if not event.symbol or len(event.symbol) != 6:
            issues.append("invalid_symbol")
        if event.event_type not in [
            "price_anomaly", "volume_anomaly", "board_anomaly",
            "announcement", "regulatory", "policy",
            "index_movement", "sector_rotation",
            "capital_flow", "news",
            "rule_change", "technical_level",
        ]:
            issues.append("unknown_event_type")

        # 边界检查（行情类）
        if event.event_type == "price_anomaly":
            pct = event.payload.data.get("change_pct", 0)
            if abs(pct) > 10.5:  # 超出涨跌停范围（含小误差）
                issues.append("change_pct_out_of_range")

        if issues:
            event.verification.status = "pending"
            event.verification.conflicts = issues
        else:
            event.verification.status = "self_verified"

        return event

    def _stage2_cross_verify(self, event: EventRecord) -> EventRecord:
        """Stage 2: 多源交叉验证。"""
        # 查找同类事件（同一symbol+同一event_type，30分钟内的已有事件）
        recent = self.store.query_recent(symbol=event.symbol, limit=10)

        same_events = [
            e for e in recent
            if e.event_type == event.event_type
            and e.raw_content_hash == event.raw_content_hash
        ]

        if len(same_events) > 0:
            # 多源确认 → 提升验证状态
            event.verification.status = "verified"
            event.verification.method = "cross_source"
            event.verification.sources_used = len(same_events) + 1
        elif event.verification.status == "self_verified":
            # 单源但来源可靠
            if event.source.level in ("S", "A"):
                event.verification.status = "self_verified"  # 保持
            elif event.source.level == "B":
                event.verification.status = "self_verified"  # 允许但标记为低可信
                event.verification.method = "single_source_B"
            else:
                event.verification.status = "pending"  # C级来源需要更多确认

        return event

    def _stage3_timeliness(self, event: EventRecord) -> EventRecord:
        """Stage 3: 时效性检查。"""
        from .clock import get_clock
        now = get_clock().now()

        if event.timestamps.expires_at:
            try:
                expires = datetime.fromisoformat(event.timestamps.expires_at)
                if now > expires:
                    event.verification.status = "outdated"
            except (ValueError, TypeError):
                pass

        # 延迟检查
        if event.timestamps.publish_time and event.timestamps.collected_at:
            try:
                pub = datetime.fromisoformat(event.timestamps.publish_time)
                col = datetime.fromisoformat(event.timestamps.collected_at)
                event.verification.latency_seconds = (col - pub).total_seconds()
            except (ValueError, TypeError):
                pass

        return event

    # ---- 优先级分配 ----

    def _assign_priority(self, event: EventRecord) -> str:
        """根据 docs/C §10 的规则分配 P0-P3。"""
        is_holding = event.account_relevance.is_holding
        event_type = event.event_type
        impact_strength = event.impact.strength
        source_level = event.source.level

        # --- P0: 持仓紧急事件 ---
        if is_holding and source_level == "S":
            if event_type in ("announcement", "regulatory"):
                if event.payload.data.get("is_major", False):
                    return "P0"

        if is_holding and event_type == "price_anomaly":
            pct = abs(event.payload.data.get("change_pct", 0))
            if pct > 9.5:  # 接近涨跌停
                return "P0"

        # --- P1: 持仓重要事件 ---
        if is_holding:
            if event_type == "price_anomaly":
                pct = abs(event.payload.data.get("change_pct", 0))
                if pct > 4:
                    return "P1"
            if event_type == "volume_anomaly":
                return "P1"
            if event_type in ("announcement", "regulatory") and source_level in ("S", "A"):
                return "P1"

        # --- P2: 关注 ---
        if is_holding or source_level in ("S", "A"):
            if impact_strength in ("high", "medium"):
                return "P2"

        if event_type == "index_movement":
            pct = abs(event.payload.data.get("change_pct", 0))
            if pct > 2:
                return "P2"

        # --- P3: 归档 ---
        return "P3"

    def _is_signal_eligible(self, event: EventRecord) -> bool:
        """判定事件是否可以进入信号台处理。"""
        # 源数据不可执行 → 禁止进入信号台
        if not event.action_eligible:
            return False

        # 已验证或自验证的事件可进入信号台
        if event.verification.status not in ("verified", "self_verified"):
            return False

        # 来源冲突
        if event.verification.status == "contradicting":
            return False

        # 已过期
        if event.verification.status == "outdated":
            return False

        # P3 不进入信号台
        if event.priority == "P3":
            return False

        # P0/P1 肯定进入
        if event.priority in ("P0", "P1"):
            return True

        # P2 + 持仓 → 进入
        if event.priority == "P2" and event.account_relevance.is_holding:
            return True

        return False

    # ---- 推送决策 (重构版) ----

    # 影子模式开关（实例变量，由构造函数设置，不可全局修改）

    def should_push(
        self, event: EventRecord,
        signal_type: str = "price_anomaly",  # trade_action / major_announcement / price_anomaly
    ) -> tuple[bool, str]:
        """
        判断是否应该推送。

        P0 边界（即使 P0 也不能绕过）:
          1. 数据源真实性检查 — verification 状态必须是 verified/self_verified
          2. 重复事件检查 — 同content_hash去重
          3. 事件新旧检查 — 不过期
          4. 账户相关性 — 非持仓P0不推送（进入日志）
          5. 影子模式总开关 — 影子模式不实际推送

        返回 (should_push, reason)
        """
        from .clock import get_clock
        now = get_clock().now()
        hour = now.hour + now.minute / 60.0

        # ========================================
        # P0 也不可绕过的边界层
        # ========================================

        # 1. 数据源真实性
        if event.verification.status == "contradicting":
            return False, "来源冲突，不可推送"
        if event.verification.status == "outdated":
            return False, "数据已过期"
        if event.verification.status not in ("verified", "self_verified"):
            return False, f"未经验证({event.verification.status})"

        # 2. 重复事件
        if self._is_duplicate(event):
            return False, "重复事件，已存在"

        # 3. 事件过期
        if event.timestamps.expires_at:
            try:
                expires = datetime.fromisoformat(event.timestamps.expires_at)
                if now > expires:
                    return False, "事件已过期"
            except (ValueError, TypeError):
                pass

        # ========================================
        # 时段规则（选择性静默，非全局）
        # ========================================

        # 盘后：行情类不推送
        if ANTI_SPAM_RULES["no_post_market_anomaly"]:
            if hour >= 15.0 and event.event_type in ("price_anomaly", "volume_anomaly"):
                return False, "盘后不推送行情异动"

        # 集合竞价：区分处理
        if 9.25 <= hour <= 9.5:
            if signal_type == "trade_action":
                if PREMARKET_SILENCE.get("trade_action", True):
                    return False, "集合竞价期间暂停交易信号推送"
            elif signal_type == "price_anomaly":
                if PREMARKET_SILENCE.get("price_anomaly") == "flag_only":
                    # 允许推送但标记为竞价数据
                    pass  # 继续后续判断
            # major_announcement → 不停默，继续

        # ========================================
        # 冷却与限额
        # ========================================

        # 同标的冷却
        cooldown = ANTI_SPAM_RULES["same_stock_cooldown_minutes"]
        last_push = self._pushed_today.get(event.symbol)
        if last_push is not None:
            minutes_ago = (now.hour * 60 + now.minute) - last_push
            if minutes_ago < cooldown:
                return False, f"冷却中(距上次{minutes_ago}分钟)"

        # ========================================
        # 优先级规则
        # ========================================

        if event.priority == "P0":
            # P0可以绕过冷却和限额，但不能绕过上面的边界层
            # 4. 账户相关性检查
            if not event.account_relevance.is_holding:
                return False, "P0事件非持仓标的 → 进入日志，不推送"
            # 5. 影子模式总开关
            if self.shadow_mode:
                return False, "影子模式: P0进入验收队列，不推送到生产渠道"
            return True, "P0推送（已通过全部边界检查）"

        if event.priority == "P1":
            if self._p1_count_today >= ANTI_SPAM_RULES["max_p1_per_morning"]:
                return False, f"上午P1限额已达{ANTI_SPAM_RULES['max_p1_per_morning']}条"
            if self.shadow_mode:
                return False, "影子模式: P1进入验收队列"
            return True, "P1推送"

        if event.priority == "P2":
            return False, "P2进入观察清单，不单独推送"

        return False, "P3归档"

    def _is_duplicate(self, event: EventRecord) -> bool:
        """检查是否为重复事件。"""
        if not event.raw_content_hash:
            return False
        recent = self.store.query_recent(symbol=event.symbol, limit=20)
        for e in recent:
            if e.raw_content_hash == event.raw_content_hash and e.event_id != event.event_id:
                return True
        return False

    def mark_pushed(self, event: EventRecord) -> None:
        """记录推送。"""
        from .clock import get_clock
        now = get_clock().now()
        self._pushed_today[event.symbol] = now.hour * 60 + now.minute
        if event.priority == "P1":
            self._p1_count_today += 1
        self.store.mark_pushed(event.event_id)

    # ---- 每日重置 ----

    def daily_reset(self) -> None:
        """开盘前重置当日计数器。"""
        self._pushed_today.clear()
        self._p1_count_today = 0

    # ---- 情报摘要 ----

    def daily_brief(self) -> list[dict[str, Any]]:
        """生成当日情报摘要。"""
        events = self.store.query_recent(limit=50)
        brief: list[dict[str, Any]] = []

        for e in events:
            if e.priority in ("P0", "P1"):
                brief.append({
                    "priority": e.priority,
                    "symbol": e.symbol,
                    "headline": e.headline,
                    "time": e.timestamps.collected_at,
                    "direction": e.impact.direction,
                    "strength": e.impact.strength,
                })

        return brief


# ---------------------------------------------------------------------------
# 持仓关联映射（docs/C §8）
# ---------------------------------------------------------------------------

def get_symbol_relations(code: str) -> dict[str, list[str]]:
    """
    返回标的的关联关系。

    {direct: [...], indirect: [...], sectors: [...]}
    """
    relations: dict[str, list[str]] = {
        "600487": {
            "direct": ["600487"],
            "indirect": ["002281", "000988", "603083"],
            "sectors": ["光通信", "光纤光缆"],
        },
        "600176": {
            "direct": ["600176"],
            "indirect": ["600585"],
            "sectors": ["玻纤", "建材"],
        },
        "000988": {
            "direct": ["000988"],
            "indirect": ["002281", "600487", "603083", "000938"],
            "sectors": ["光通信", "激光", "AI算力"],
        },
    }
    return relations.get(code, {"direct": [code], "indirect": [], "sectors": [SECTOR_MAP.get(code, "未知")]})


# ---------------------------------------------------------------------------
# 动态异动阈值（docs/C §9）
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "price_watch_pct": 4.0,       # 关注阈值
    "price_alert_pct": 7.0,       # 异常阈值
    "volume_ratio_watch": 2.0,    # 量比关注
    "volume_ratio_alert": 4.0,    # 量比异常
    "volume_surge_pct": 150,      # 成交量突增%
    "turnover_watch": 8.0,        # 换手率关注
    "turnover_alert": 15.0,       # 换手率异常
    "sector_move_watch": 3.0,     # 板块涨跌关注
}


def check_thresholds(
    code: str,
    change_pct: float,
    volume_ratio: float = 1.0,
    turnover: float = 0,
    sector_move: float = 0,
) -> dict[str, Any]:
    """
    检查是否触发异动阈值。

    返回 {"triggered": bool, "level": "watch"/"alert"/"none", "reasons": [...]}
    """
    t = DEFAULT_THRESHOLDS
    reasons = []
    level = "none"

    # 价格
    abs_pct = abs(change_pct)
    if abs_pct >= t["price_alert_pct"]:
        level = "alert"
        reasons.append(f"涨跌{change_pct:+.1f}% > 异常阈值{t['price_alert_pct']}%")
    elif abs_pct >= t["price_watch_pct"]:
        level = max(level, "watch")
        reasons.append(f"涨跌{change_pct:+.1f}% > 关注阈值{t['price_watch_pct']}%")

    # 量能
    if volume_ratio >= t["volume_ratio_alert"]:
        level = "alert"
        reasons.append(f"量比{volume_ratio:.1f} > 异常阈值{t['volume_ratio_alert']}")
    elif volume_ratio >= t["volume_ratio_watch"]:
        level = max(level, "watch")
        reasons.append(f"量比{volume_ratio:.1f} > 关注阈值{t['volume_ratio_watch']}")

    # 换手率
    if turnover >= t["turnover_alert"]:
        level = "alert"
        reasons.append(f"换手率{turnover:.1f}% > 异常阈值{t['turnover_alert']}%")
    elif turnover >= t["turnover_watch"]:
        level = max(level, "watch")
        reasons.append(f"换手率{turnover:.1f}% > 关注阈值{t['turnover_watch']}%")

    # 板块
    if abs(sector_move) >= t["sector_move_watch"]:
        level = max(level, "watch")
        reasons.append(f"板块涨跌{sector_move:+.1f}% > 关注阈值{t['sector_move_watch']}%")

    return {
        "triggered": level != "none",
        "level": level,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_intel: Optional[IntelligenceNetwork] = None


def get_intel(shadow_mode: bool = True) -> IntelligenceNetwork:
    """
    获取单例 IntelligenceNetwork。
    首次调用时 shadow_mode 生效；后续调用可传入但已被缓存忽略。
    如需切换模式，请调用 reset_intel() 后重新获取。
    """
    global _intel
    if _intel is None:
        _intel = IntelligenceNetwork(shadow_mode=shadow_mode)
    return _intel


def reset_intel() -> None:
    """重置单例（环境切换时使用）。"""
    global _intel
    _intel = None
