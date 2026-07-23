"""
Serenity 2.0 — 信号台 (Phase 2)  [重构版]

架构修正：
  · 五道硬门槛：全部通过才生成行动信号，任何一道失败即降级
  · 置信度评分：仅在硬门槛全部通过后计算，决定"有多可信"
  · 三维独立：事件优先级(EventRecord) ≠ 信号等级(signal_level) ≠ 交易动作(trade_action)

信号等级定义：
  ACTION   行动信号 — 五道硬门槛全过 → 明确买卖建议
  WATCH    关注信号 — 部分门槛失败 → 加入观察清单
  INFO     情报信号 — 仅信息归档

交易动作定义：
  BUY / ADD / HOLD / REDUCE / SELL / WATCH

当前为影子运行模式：生成信号但不自动推送，需人工验收。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, ClassVar

from .account_baseline import get_baseline, AccountState, RiskConstraints
from .event_record import EventRecord, EventStore

# ---------------------------------------------------------------------------
# 硬门槛名称常量
# ---------------------------------------------------------------------------

GATE_MARKET = "market_env"
GATE_TREND = "stock_trend"
GATE_VOLUME = "volume_price"
GATE_DATA = "data_quality"        # 数据可信度：验证状态 + 来源等级（始终REQUIRED）
GATE_ACCOUNT = "account_fit"

ALL_GATES = [GATE_MARKET, GATE_TREND, GATE_VOLUME, GATE_DATA, GATE_ACCOUNT]

# ---------------------------------------------------------------------------
# 门槛模式定义
# ---------------------------------------------------------------------------

class GateMode:
    """门槛模式。

    REQUIRED: 必须通过，失败则禁止生成 ACTION 信号。
    OPTIONAL: 最好通过，失败则降低置信度但不禁止 ACTION。
    NOT_APPLICABLE: 不参与当前策略，不评分，必须说明原因。
    """
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "na"

# 默认配置：所有门槛均为 REQUIRED（真正五道硬门槛）
DEFAULT_GATE_CONFIG: dict[str, str] = {
    GATE_MARKET: GateMode.REQUIRED,
    GATE_TREND: GateMode.REQUIRED,
    GATE_VOLUME: GateMode.REQUIRED,
    GATE_DATA: GateMode.REQUIRED,
    GATE_ACCOUNT: GateMode.REQUIRED,
}

# 策略级覆盖（示例）
STRATEGY_EVENT_DRIVEN: dict[str, str] = {
    # 事件驱动策略：event 必须通过，其余保持 REQUIRED
    GATE_DATA: GateMode.REQUIRED,
}

STRATEGY_TECHNICAL_BREAKOUT: dict[str, str] = {
    # 技术突破策略：趋势+量价必须，市场环境可放宽
    GATE_TREND: GateMode.REQUIRED,
    GATE_VOLUME: GateMode.REQUIRED,
    GATE_MARKET: GateMode.OPTIONAL,  # 可逆市操作
    # GATE_DATA 不可覆盖（IMMUTABLE_REQUIRED）
    # GATE_ACCOUNT 不可覆盖（IMMUTABLE_REQUIRED）
}

STRATEGY_DEFENSIVE: dict[str, str] = {
    # 防御策略：市场环境可降为 OPTIONAL（允许逆市持有）
    GATE_MARKET: GateMode.OPTIONAL,
}

# GATE_ACCOUNT 和 GATE_DATA(数据可信度) 始终 REQUIRED，不允许策略覆盖
IMMUTABLE_REQUIRED = {GATE_ACCOUNT, GATE_DATA}

# 门槛权重（用于置信度评分，仅传入参与门槛）
GATE_WEIGHTS = {
    GATE_MARKET: 0.15,
    GATE_TREND: 0.25,
    GATE_VOLUME: 0.25,
    GATE_DATA: 0.20,
    GATE_ACCOUNT: 0.15,
}

# ---------------------------------------------------------------------------
# 信号输出（重构版）
# ---------------------------------------------------------------------------

@dataclass
class SignalOutput:
    """统一信号输出格式（重构版 v2.1）。

    四个独立维度：
      signal_level  — DECISION / ACTION / WATCH / INFO
      trade_action  — BUY / ADD / HOLD / REDUCE / SELL / WATCH
      confidence    — high / medium / low

    DECISION: 门槛全过但无需执行新交易（如 HOLD 维持仓位）
    ACTION:   门槛全过且有具体买卖指令
    WATCH:    部分门槛失败，加入观察清单
    INFO:     仅信息归档
    """

    signal_id: str = ""
    schema_version: str = "2.1"       # 输出格式版本
    timestamp: str = ""

    # --- 四独立维度 ---
    symbol: str = ""
    name: str = ""
    signal_level: str = "INFO"       # DECISION / ACTION / WATCH / INFO
    trade_action: str = "WATCH"      # BUY / ADD / HOLD / REDUCE / SELL / WATCH
    confidence: str = "low"          # high / medium / low

    # --- 门槛结果 ---
    required_passed: bool = False
    required_failed: list[str] = field(default_factory=list)
    optional_failed: list[str] = field(default_factory=list)
    na_gates: list[str] = field(default_factory=list)
    gates_detail: dict[str, dict] = field(default_factory=dict)

    # --- 执行参数 ---
    # BUY/ADD 专用
    buy_price_low: float = 0
    buy_price_high: float = 0
    buy_shares: int = 0
    buy_amount: float = 0
    buy_stop_loss: float = 0
    buy_first_target: float = 0

    # SELL/REDUCE 专用
    sell_price_low: float = 0
    sell_price_high: float = 0
    sell_shares: int = 0
    sell_amount: float = 0
    sell_cut_loss: float = 0          # 跌破无条件执行
    sell_bounce_condition: str = ""   # 反弹减仓条件
    sell_cancel_condition: str = ""   # 取消卖出条件
    sell_remaining_shares: int = 0    # 剩余持仓

    # 兼容旧字段
    suggested_price_low: float = 0
    suggested_price_high: float = 0
    suggested_shares: int = 0
    suggested_amount: float = 0
    stop_loss: float = 0
    first_target: float = 0
    expected_days: int = 0

    # --- 账户状态 ---
    current_shares: int = 0
    current_weight_pct: float = 0
    after_weight_pct: float = 0

    # --- 时间轴 ---
    snapshot_as_of: str = ""               # 账户快照时点
    replay_as_of: str = ""                 # 回放模拟时点
    market_session: str = ""               # 交易时段（PREMARKET/OPENING_AUCTION/.../CLOSED）
    position_available_as_of: str = ""     # 持仓可卖结算已覆盖到哪个交易日
    eod_finalized_through_trade_date: str = ""  # 日终账户数据完成截止交易日
    t1_settlement_completed: bool = False  # T+1 结算已执行（无到期未释放批次）
    t1_due_batch_count: int = 0            # 到期未释放的批次数
    t1_due_shares_pending: int = 0         # 到期未释放的股数

    # [已弃用] 内部保留，不在格式化输出中展示
    settlement_cutoff_passed: bool = False  # 15:30截止是否已过

    # --- 时段安全 ---
    session_approximation: bool = False     # 当前时段判定为近似值（非精确行情驱动）
    action_suppressed: bool = False         # 当前时段禁止生成 ACTION
    suppression_reason: str = ""            # 抑制原因

    # --- 降级审计 ---
    candidate_signal_level: str = ""        # 降级前原始信号等级
    candidate_trade_action: str = ""        # 降级前原始交易动作
    effective_signal_level: str = ""        # 实际生效等级
    effective_trade_action: str = ""        # 实际生效动作
    normalization_reason: str = ""          # 降级/规范化原因

    # --- 依据与风险 ---
    trigger_reasons: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)

    # --- 元数据 ---
    data_updated_at: str = ""
    event_priority: str = ""
    event_id: str = ""
    execution_tags: list[str] = field(default_factory=lambda: ["SHADOW_ONLY", "NOT_FOR_EXECUTION"])

    # ------------------------------------------------------------------
    # 信号等级 × 交易动作合法组合（v2.1）
    # ------------------------------------------------------------------

    VALID_COMBINATIONS: ClassVar[dict[str, tuple[str, ...]]] = {
        "ACTION": ("BUY", "ADD", "REDUCE", "SELL"),
        "DECISION": ("HOLD",),
        "WATCH": ("WATCH", "HOLD"),
        "INFO": ("HOLD", "WATCH"),
    }

    def __post_init__(self):
        """验证 signal_level 与 trade_action 的合法性。"""
        if self.signal_level and self.trade_action:
            allowed = self.VALID_COMBINATIONS.get(self.signal_level, ())
            if self.trade_action not in allowed:
                raise ValueError(
                    f"非法信号组合: {self.signal_level} · {self.trade_action}。"
                    f"允许: {self.signal_level} → {allowed}"
                )

    @property
    def is_action(self) -> bool:
        return self.signal_level == "ACTION"

    @property
    def is_decision(self) -> bool:
        """DECISION: 门槛通过但无需新交易（维持现状）。"""
        return self.signal_level == "DECISION"

    @property
    def is_hold_decision(self) -> bool:
        """DECISION + HOLD: 主动维持仓位，禁止加减仓。"""
        return self.is_decision and self.trade_action == "HOLD"

    @property
    def has_executable_trade(self) -> bool:
        """是否存在可执行的交易动作。"""
        return self.is_action and self.trade_action in ("BUY", "ADD", "REDUCE", "SELL")

    @property
    def all_required_passed(self) -> bool:
        return self.required_passed and len(self.required_failed) == 0


# ---------------------------------------------------------------------------
# 门槛结果
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    name: str = ""
    mode: str = GateMode.REQUIRED       # REQUIRED / OPTIONAL / NA
    passed: bool = False
    detail: str = ""
    score: int = 0                      # 0-100 细粒度评分
    na_reason: str = ""                 # NA 时必须说明原因


# ---------------------------------------------------------------------------
# 策略化门槛验证器
# ---------------------------------------------------------------------------

def _resolve_gate_config(
    base: dict[str, str],
    overrides: Optional[dict[str, str]],
) -> dict[str, str]:
    """合并基础配置和策略覆盖，IMMUTABLE_REQUIRED 不可覆盖。"""
    config = dict(base)
    if overrides:
        for name, mode in overrides.items():
            if name in IMMUTABLE_REQUIRED and mode != GateMode.REQUIRED:
                continue  # 不可变门槛，拒绝覆盖
            if name in ALL_GATES:
                config[name] = mode
    return config


class HardGateVerifier:
    """
    策略化门槛验证器。

    规则:
      每道门槛模式: REQUIRED / OPTIONAL / NOT_APPLICABLE
      REQUIRED + 失败 → 禁止 ACTION（降级为 WATCH）
      OPTIONAL + 失败 → 可生成 ACTION，但降低置信度
      NA → 不参与评分，必须说明原因
      所有 REQUIRED 通过 → 才进入置信度计算

    GATE_ACCOUNT 和 GATE_DATA(数据可信度): 始终 REQUIRED，策略不可覆盖。
    """

    def __init__(self, strategy_overrides: Optional[dict[str, str]] = None):
        self.baseline = get_baseline()
        self.rules = RiskConstraints()
        self.gate_config = _resolve_gate_config(DEFAULT_GATE_CONFIG, strategy_overrides)

    def verify(
        self, event: EventRecord, market_data: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        执行门槛验证。

        返回:
          {
            "required_passed": bool,       # REQUIRED 门槛是否全部通过
            "required_failed": [str],      # 失败的 REQUIRED 门槛
            "optional_failed": [str],      # 失败的 OPTIONAL 门槛
            "na_gates": [str],             # NA 门槛 + 原因
            "gates": {name: GateResult},
            "confidence": "high"/"medium"/"low",
            "signal_level": "ACTION"/"WATCH"/"INFO"
          }
        """
        gates: dict[str, GateResult] = {}

        # 执行各门槛
        gates[GATE_MARKET] = self._gate_market(event, market_data)
        gates[GATE_TREND] = self._gate_trend(event)
        gates[GATE_VOLUME] = self._gate_volume(event)
        gates[GATE_DATA] = self._gate_data(event)
        gates[GATE_ACCOUNT] = self._gate_account(event)

        # 注入模式
        for name, g in gates.items():
            g.name = name
            g.mode = self.gate_config.get(name, GateMode.REQUIRED)

        # 分类
        required_failed = [
            name for name, g in gates.items()
            if g.mode == GateMode.REQUIRED and not g.passed
        ]
        optional_failed = [
            name for name, g in gates.items()
            if g.mode == GateMode.OPTIONAL and not g.passed
        ]
        na_gates = [
            name for name, g in gates.items()
            if g.mode == GateMode.NOT_APPLICABLE
        ]

        required_passed = len(required_failed) == 0

        # 信号等级
        if required_passed:
            signal_level = "ACTION"
        elif required_failed:
            signal_level = "WATCH"  # REQUIRED 失败 = 不能行动
        else:
            signal_level = "INFO"

        # 置信度：仅 REQUIRED 全部通过后计算
        confidence = "low"
        if required_passed:
            # 只计算参与的门槛（REQUIRED + OPTIONAL，不含 NA）
            participating = [
                name for name in ALL_GATES
                if self.gate_config.get(name) != GateMode.NOT_APPLICABLE
            ]
            if participating:
                # 重归一化权重
                raw_weights = {name: GATE_WEIGHTS[name] for name in participating}
                total_w = sum(raw_weights.values())
                normalized = {name: w / total_w for name, w in raw_weights.items()}

                weighted = sum(
                    gates[name].score * normalized[name]
                    for name in participating
                )

                # OPTIONAL 失败的惩罚
                weighted -= len(optional_failed) * 10

                if weighted >= 75:
                    confidence = "high"
                elif weighted >= 50:
                    confidence = "medium"
                else:
                    confidence = "low"

        return {
            "required_passed": required_passed,
            "required_failed": required_failed,
            "optional_failed": optional_failed,
            "na_gates": na_gates,
            "gate_config_snapshot": dict(self.gate_config),
            "gates": {
                name: {
                    "mode": g.mode,
                    "passed": g.passed,
                    "detail": g.detail,
                    "score": g.score,
                    "na_reason": g.na_reason,
                }
                for name, g in gates.items()
            },
            "confidence": confidence,
            "signal_level": signal_level,
        }

    # ---- 五道门槛实现（评分逻辑不变） ----

    def _gate_market(self, event: EventRecord, market_data: Optional[dict]) -> GateResult:
        if market_data is None:
            return GateResult(passed=True, detail="无市场数据", score=100,
                              na_reason="行情未接入")

        score = 100
        fails = []

        regime = market_data.get("market_regime", "ranging")
        if regime == "extreme":
            score -= 50
            fails.append("市场极端状态")
        elif regime == "trending":
            score -= 10
            fails.append("趋势市")

        if not market_data.get("liquidity_normal", True):
            score -= 40
            fails.append("流动性异常")

        market_pct = market_data.get("market_change_pct", 0)
        if abs(market_pct) > 5:
            score -= 30
            fails.append(f"大盘波动{market_pct:+.1f}%")
        elif abs(market_pct) > 3:
            score -= 15
            fails.append(f"大盘波动{market_pct:+.1f}%")

        passed = score >= 50
        detail = "; ".join(fails) if fails else "环境正常"
        return GateResult(passed=passed, detail=detail, score=max(0, score))

    def _gate_trend(self, event: EventRecord) -> GateResult:
        payload = event.payload.data
        change_pct = payload.get("change_pct", 0)
        score = 100
        fails = []

        if change_pct < -7:
            score -= 40
            fails.append(f"跌幅{change_pct:.1f}% > 7%")
        elif change_pct < -4:
            score -= 20
            fails.append(f"跌幅{change_pct:.1f}%")

        if event.impact.direction == "bearish":
            score -= 15
            fails.append("事件利空")

        passed = score >= 40
        detail = "; ".join(fails) if fails else "趋势正常"
        return GateResult(passed=passed, detail=detail, score=max(0, score))

    def _gate_volume(self, event: EventRecord) -> GateResult:
        payload = event.payload.data
        change_pct = payload.get("change_pct", 0)
        turnover = payload.get("turnover_rate", 0)
        amount = payload.get("amount", 0)
        score = 100
        fails = []

        if abs(change_pct) > 4 and turnover > 8:
            d = "涨" if change_pct > 0 else "跌"
            fails.append(f"放量{d}{abs(change_pct):.1f}%需确认")
        elif abs(change_pct) > 2 and turnover < 2:
            score -= 30
            fails.append("缩量波动")

        if amount < 5e8:
            score -= 20
            fails.append(f"成交清淡(¥{amount/1e8:.1f}亿)")

        passed = score >= 40
        detail = "; ".join(fails) if fails else "量价正常"
        return GateResult(passed=passed, detail=detail, score=max(0, score))

    def _gate_data(self, event: EventRecord) -> GateResult:
        score = 100
        fails = []

        if event.verification.status == "verified":
            score = 100
        elif event.verification.status == "self_verified":
            score = 70
            fails.append("仅单源验证")
        elif event.verification.status == "contradicting":
            score = 0
            fails.append("来源冲突")
        elif event.verification.status == "outdated":
            score = 0
            fails.append("数据已过期")
        else:
            score = 30
            fails.append(f"验证: {event.verification.status}")

        if event.source.level == "C":
            score -= 30
            fails.append("C级来源")

        passed = score >= 50
        detail = "; ".join(fails) if fails else "事件确认"
        return GateResult(passed=passed, detail=detail, score=max(0, score))

    def _gate_account(self, event: EventRecord) -> GateResult:
        ar = event.account_relevance
        score = 100
        fails = []

        if not ar.is_holding:
            score -= 30
            fails.append("非持仓")

        if ar.position_weight_pct > 45:
            score -= 40
            fails.append(f"仓位{ar.position_weight_pct:.1f}%超限")

        ctx = self.baseline.signal_context(event.symbol)
        if ctx["holding"] and ctx.get("available_cash", 0) <= 0:
            score -= 40
            fails.append("现金耗尽")

        passed = score >= 40
        detail = "; ".join(fails) if fails else "账户适配"
        return GateResult(passed=passed, detail=detail, score=max(0, score))


# ---------------------------------------------------------------------------
# 信号台引擎（重构版）
# ---------------------------------------------------------------------------

class SignalDesk:
    """
    信号台核心引擎。

    三维独立：
      · event_priority: 事件优先级 (P0-P3) — 来自 EventRecord
      · signal_level: 信号等级 (ACTION/WATCH/INFO) — 由硬门槛决定
      · trade_action: 交易动作 (BUY/ADD/HOLD/REDUCE/SELL/WATCH) — 由方向+账户决定
    """

    def __init__(self):
        self.verifier = HardGateVerifier()
        self.baseline = get_baseline()
        self.store = EventStore()
        self._signal_counter = 0

    def process_events(
        self, market_data: Optional[dict[str, Any]] = None
    ) -> list[SignalOutput]:
        """处理所有活跃事件，生成信号。"""
        events = self.store.query_active()
        signals: list[SignalOutput] = []

        for event in events:
            sig = self._event_to_signal(event, market_data)
            if sig is not None:
                signals.append(sig)

        # 排序：ACTION > WATCH > INFO
        level_order = {"ACTION": 0, "WATCH": 1, "INFO": 2}
        signals.sort(key=lambda s: level_order.get(s.signal_level, 3))

        return signals

    def _event_to_signal(
        self, event: EventRecord, market_data: Optional[dict] = None
    ) -> Optional[SignalOutput]:
        """事件 → 信号。"""
        from .clock import get_clock

        result = self.verifier.verify(event, market_data)

        self._signal_counter += 1
        clock = get_clock()
        now = clock.now()
        ctx = self.baseline.signal_context(event.symbol)

        # 统计待释放批次
        pending_batches, pending_shares = self._count_pending_batches()

        # 先确定 signal_level + trade_action（在 SignalOutput 构造前）
        signal_level = result["signal_level"]
        trade_action = self._determine_action(event, ctx, result)

        # 保存降级前候选值
        candidate_level = signal_level
        candidate_action = trade_action
        normalization_reason = ""

        # 合法组合规范化
        if signal_level == "ACTION" and trade_action == "HOLD":
            signal_level = "DECISION"
            normalization_reason = "ACTION_HOLD_TO_DECISION"
        elif signal_level == "ACTION" and trade_action == "WATCH":
            signal_level = "WATCH"
            normalization_reason = "BEARISH_NO_HOLDING_NO_SHORT"
        elif signal_level == "WATCH" and trade_action not in ("WATCH", "HOLD"):
            trade_action = "WATCH"
            normalization_reason = "GATE_DOWNGRADED_NOT_EXECUTABLE"

        # 时段安全：非连续竞价时段抑制 ACTION
        market_session = clock.market_session()
        action_suppressed = False
        suppression_reason = ""
        session_approximation = False

        ACTION_SAFE_SESSIONS = {"CONTINUOUS_AM", "CONTINUOUS_PM"}
        if signal_level == "ACTION" and market_session not in ACTION_SAFE_SESSIONS:
            action_suppressed = True
            if market_session in ("OPENING_MATCH_EVENT",):
                suppression_reason = "OPENING_MATCH_APPROXIMATION"
                session_approximation = True
            elif market_session in ("OPENING_AUCTION_CANCELABLE", "OPENING_AUCTION_NO_CANCEL",
                                     "PRE_OPEN_PAUSE", "CLOSING_AUCTION"):
                suppression_reason = f"NON_CONTINUOUS_SESSION_{market_session}"
            elif market_session in ("PRE_OPEN_PAUSE", "LUNCH_BREAK", "POSTMARKET", "CLOSED"):
                suppression_reason = f"MARKET_{market_session}"
            else:
                suppression_reason = f"ACTION_SUPPRESSED_IN_{market_session}"
            # 降级为 DECISION+HOLD（门通过了但不执行）
            signal_level = "DECISION"
            trade_action = "HOLD"
            if not normalization_reason:
                normalization_reason = suppression_reason

        # 标记 OPENING_MATCH_EVENT 为近似
        if market_session == "OPENING_MATCH_EVENT":
            session_approximation = True

        sig = SignalOutput(
            signal_id=f"SIG_{now.strftime('%Y%m%d')}_{self._signal_counter:03d}",
            schema_version="2.1",
            timestamp=now.isoformat(timespec="seconds"),
            symbol=event.symbol,
            name=ctx["name"],
            signal_level=signal_level,
            trade_action=trade_action,
            confidence=result["confidence"],
            required_passed=result["required_passed"],
            required_failed=result["required_failed"],
            optional_failed=result["optional_failed"],
            na_gates=result["na_gates"],
            gates_detail=result["gates"],
            current_shares=ctx["position_shares"],
            current_weight_pct=ctx["position_weight_pct"],
            data_updated_at=now.isoformat(timespec="seconds"),
            event_priority=event.priority,
            event_id=event.event_id,
            snapshot_as_of=now.isoformat(timespec="seconds"),
            replay_as_of=now.isoformat(timespec="seconds"),
            settlement_cutoff_passed=clock.is_settlement_cutoff_passed(),
            market_session=market_session,
            position_available_as_of=clock.position_available_as_of().isoformat(),
            eod_finalized_through_trade_date=clock.eod_finalized_through_trade_date().isoformat(),
            t1_settlement_completed=self._check_t1_settlement_completed(ctx),
            t1_due_batch_count=pending_batches,
            t1_due_shares_pending=pending_shares,
            session_approximation=session_approximation,
            action_suppressed=action_suppressed,
            suppression_reason=suppression_reason,
            candidate_signal_level=candidate_level if normalization_reason else "",
            candidate_trade_action=candidate_action if normalization_reason else "",
            effective_signal_level=signal_level,
            effective_trade_action=trade_action,
            normalization_reason=normalization_reason,
        )

        sig.trigger_reasons = [
            f"[{name}] {info['detail']}"
            for name, info in result["gates"].items()
            if info["passed"]
        ]
        sig.risk_warnings = [
            f"[{name}] {info['detail']}"
            for name, info in result["gates"].items()
            if not info["passed"]
        ]

        # 仅可执行信号填充执行参数
        if sig.has_executable_trade:
            self._fill_execution_params(sig, event, ctx)

        sig.failure_conditions.append("数据源异常时暂停执行")
        if sig.is_action:
            if sig.trade_action in ("BUY", "ADD") and sig.buy_stop_loss > 0:
                sig.failure_conditions.append(f"买入后跌破{sig.buy_stop_loss}元止损")
            elif sig.trade_action in ("SELL", "REDUCE") and sig.sell_cut_loss > 0:
                sig.failure_conditions.append(f"跌破{sig.sell_cut_loss}元强制卖出")

        return sig

    def _check_t1_settlement_completed(self, ctx: dict) -> bool:
        """
        检查 T+1 结算是否已对所有到期批次完成。

        遍历所有持仓的 unsettled_batches，检查是否仍有
        settlement_date <= today 的批次未释放。
        若无到期未释放批次 → True（结算已完成）。
        """
        from .clock import get_clock
        today = get_clock().today()

        state = self.baseline.load_latest()
        for pos in state.positions:
            for batch in pos.unsettled_batches:
                from datetime import date
                settle_date = date.fromisoformat(batch["settlement_date"])
                if settle_date <= today:
                    # 存在到期未释放的批次
                    return False
        # 所有到期批次已释放，或无批次
        return True

    def _count_pending_batches(self) -> tuple[int, int]:
        """
        统计到期未释放的 T+1 批次。

        遍历所有持仓，统计 settlement_date <= today 但仍在
        unsettled_batches 中的批次。

        返回 (batch_count, total_shares)。
        """
        from .clock import get_clock
        from datetime import date
        today = get_clock().today()

        state = self.baseline.load_latest()
        batch_count = 0
        total_shares = 0
        for pos in state.positions:
            for batch in pos.unsettled_batches:
                settle_date = date.fromisoformat(batch["settlement_date"])
                if settle_date <= today:
                    batch_count += 1
                    total_shares += batch["shares"]
        return batch_count, total_shares

    def _determine_action(
        self, event: EventRecord,
        ctx: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        """确定交易动作。"""
        direction = event.impact.direction
        is_holding = ctx["holding"]
        weight = ctx["position_weight_pct"]
        pnl_pct = ctx["pnl_pct"]
        is_action = result["signal_level"] == "ACTION"
        is_watch = result["signal_level"] == "WATCH"

        if is_action:
            if direction == "bullish":
                if is_holding:
                    return "ADD" if weight < 30 else "HOLD"
                return "BUY"
            elif direction == "bearish":
                if is_holding:
                    return "SELL" if pnl_pct < -7 else "REDUCE"
                return "WATCH"

        if is_watch:
            # 关注信号：建议准备但不执行
            if direction == "bullish" and is_holding:
                return "HOLD"
            if direction == "bearish" and is_holding:
                return "WATCH"
            return "WATCH"

        return "WATCH"

    def _fill_execution_params(
        self, sig: SignalOutput,
        event: EventRecord,
        ctx: dict[str, Any],
    ) -> None:
        """填充执行参数。BUY/ADD 与 SELL/REDUCE 使用不同参数组。"""
        price = event.payload.data.get("price", ctx.get("current_price", 0))
        if price <= 0:
            return

        state = self.baseline.load_latest()
        total_assets = state.total_assets if state.total_assets > 0 else 250000

        # ---- BUY / ADD ----
        if sig.trade_action in ("BUY", "ADD"):
            sig.buy_price_low = round(price * 0.97, 2)
            sig.buy_price_high = round(price * 1.03, 2)

            available = state.available_cash
            if available > 0:
                qty = int(available * 0.3 / price)
                qty = (qty // 100) * 100
                sig.buy_shares = max(100, qty)
                sig.buy_amount = round(sig.buy_shares * price, 2)
                new_val = ctx.get("position_value", 0) + sig.buy_amount
                sig.after_weight_pct = round(new_val / total_assets * 100, 1)

                if sig.after_weight_pct > 40:
                    sig.risk_warnings.append(
                        f"执行后仓位{sig.after_weight_pct:.1f}%超单票上限40%"
                    )

            sig.buy_stop_loss = round(price * 0.95, 2)
            sig.buy_first_target = round(price * 1.05, 2)

            # 兼容旧字段
            sig.suggested_price_low = sig.buy_price_low
            sig.suggested_price_high = sig.buy_price_high
            sig.suggested_shares = sig.buy_shares
            sig.suggested_amount = sig.buy_amount
            sig.stop_loss = sig.buy_stop_loss
            sig.first_target = sig.buy_first_target
            sig.expected_days = 5

        # ---- SELL / REDUCE ----
        elif sig.trade_action in ("REDUCE", "SELL"):
            sig.sell_price_low = round(price * 0.97, 2)
            sig.sell_price_high = round(price * 1.03, 2)

            # 卖出数量不得超过可卖
            available_sell = min(
                ctx.get("available_shares", 0),
                ctx.get("position_shares", 0),
            )
            if sig.trade_action == "REDUCE":
                sig.sell_shares = min(
                    max(100, (ctx.get("position_shares", 0) // 4 // 100) * 100),
                    available_sell,
                )
            else:
                sig.sell_shares = available_sell  # SELL = 全部可卖

            if sig.sell_shares <= 0:
                sig.risk_warnings.append("无可卖数量，建议卖出操作无法执行")

            sig.sell_amount = round(sig.sell_shares * price, 2)
            new_val = ctx.get("position_value", 0) - sig.sell_amount
            sig.after_weight_pct = round(max(0, new_val) / total_assets * 100, 1)

            # cut_loss: 基于当前价的硬止损（浮亏仓位已在跌，再跌就卖）
            sig.sell_cut_loss = round(price * 0.92, 2)
            # bounce: 反弹至此价位可减仓（减少损失）
            sig.sell_bounce_condition = f"反弹至{round(price * 1.02, 2)}元考虑减仓"
            sig.sell_cancel_condition = "信号源失效或价格突破卖出区间上沿"
            sig.sell_remaining_shares = ctx.get("position_shares", 0) - sig.sell_shares

            # 兼容旧字段
            sig.suggested_shares = sig.sell_shares
            sig.suggested_amount = sig.sell_amount
            sig.expected_days = 3


# ---------------------------------------------------------------------------
# 信号格式化
# ---------------------------------------------------------------------------

def format_signal(sig: SignalOutput) -> str:
    """将信号格式化为人类可读文本。"""
    level_icon = {
        "DECISION": "🔵", "ACTION": "🚨",
        "WATCH": "⚠️", "INFO": "👀",
    }
    action_text = {
        "BUY": "🟢 买入", "ADD": "🟢 加仓",
        "HOLD": "🔵 持有", "WATCH": "⚪ 观望",
        "REDUCE": "🟡 减仓", "SELL": "🔴 卖出",
    }

    icon = level_icon.get(sig.signal_level, "📋")
    action_display = action_text.get(sig.trade_action, sig.trade_action)

    # 门槛状态显示
    if sig.signal_level == "INFO":
        gate_status = f"❌ REQUIRED失败: {', '.join(sig.required_failed)}"
    elif sig.optional_failed:
        gate_status = f"✅ REQUIRED全过 | OPTIONAL失败: {', '.join(sig.optional_failed)}"
    elif sig.required_passed:
        gate_status = "✅ 全部REQUIRED通过"
    else:
        gate_status = f"❌ {', '.join(sig.required_failed)}"

    # 等级说明
    level_note = ""
    if sig.is_hold_decision:
        level_note = " [主动维持仓位，禁止加减仓]"
    elif sig.has_executable_trade:
        level_note = " [需立即执行]"

    lines = [
        f"┌{'─'*52}┐",
        f"│ 信号 #{sig.signal_id} | {sig.timestamp[:19]}",
        f"│ 等级: {icon} {sig.signal_level} | 置信度: {sig.confidence}{level_note}",
        f"│ 事件优先级: {sig.event_priority} | 门槛: {gate_status}",
        f"│{'─'*52}│",
        f"│ 标的: {sig.name}({sig.symbol})",
        f"│ 动作: {action_display}",
    ]

    # BUY/ADD 参数
    if sig.has_executable_trade and sig.trade_action in ("BUY", "ADD") and sig.buy_shares > 0:
        lines.extend([
            f"│",
            f"│ ── 买入参数 ──",
            f"│ 买入区间: {sig.buy_price_low:.2f} — {sig.buy_price_high:.2f}",
            f"│ 买入数量: {sig.buy_shares}股 | ¥{sig.buy_amount:,.0f}",
            f"│ 保护性止损: {sig.buy_stop_loss:.2f} | 第一目标: {sig.buy_first_target:.2f}",
        ])

    # SELL/REDUCE 参数
    elif sig.has_executable_trade and sig.trade_action in ("SELL", "REDUCE") and sig.sell_shares > 0:
        lines.extend([
            f"│",
            f"│ ── 卖出参数 ──",
            f"│ 卖出区间: {sig.sell_price_low:.2f} — {sig.sell_price_high:.2f}",
            f"│ 卖出数量: {sig.sell_shares}股 | ¥{sig.sell_amount:,.0f}",
            f"│ 强制止损: {sig.sell_cut_loss:.2f} | 剩余: {sig.sell_remaining_shares}股",
        ])
        if sig.sell_bounce_condition:
            lines.append(f"│ 反弹条件: {sig.sell_bounce_condition}")
        if sig.sell_cancel_condition:
            lines.append(f"│ 取消条件: {sig.sell_cancel_condition}")

    lines.append(f"│")
    lines.append(f"│ 当前持仓: {sig.current_shares}股 | {sig.current_weight_pct:.1f}%")
    if sig.after_weight_pct > 0:
        lines.append(f"│ 执行后仓位: {sig.after_weight_pct:.1f}%")

    # 时间轴
    if sig.snapshot_as_of or sig.replay_as_of:
        lines.append(f"│")
        if sig.snapshot_as_of:
            lines.append(f"│ 快照时点: {sig.snapshot_as_of[:19]}")
        if sig.replay_as_of:
            lines.append(f"│ 回放时点: {sig.replay_as_of[:19]}")
        if sig.market_session:
            lines.append(f"│ 交易时段: {sig.market_session}")

    # 结算状态（独立于时间轴，始终显示）
    if sig.position_available_as_of or sig.eod_finalized_through_trade_date:
        if sig.position_available_as_of:
            lines.append(f"│ 可卖覆盖: {sig.position_available_as_of}")
        if sig.eod_finalized_through_trade_date:
            lines.append(f"│ 日终完成: {sig.eod_finalized_through_trade_date}")
    lines.append(f"│ T+1完成: {'是' if sig.t1_settlement_completed else '否'}"
                 f" | 到期待释放: {sig.t1_due_batch_count}批/{sig.t1_due_shares_pending}股")

    # 时段安全与抑制
    if sig.session_approximation or sig.action_suppressed:
        lines.append(f"│")
        if sig.session_approximation:
            lines.append(f"│ ⚠ 时段近似: 非精确行情驱动")
        if sig.action_suppressed:
            lines.append(f"│ ⛔ 动作抑制: {sig.suppression_reason}")

    # 降级审计
    if sig.normalization_reason:
        lines.append(f"│")
        if sig.candidate_signal_level:
            lines.append(f"│ 候选: {sig.candidate_signal_level} · {sig.candidate_trade_action}")
        lines.append(f"│ 生效: {sig.effective_signal_level} · {sig.effective_trade_action}")
        lines.append(f"│ 原因: {sig.normalization_reason}")

    if sig.trigger_reasons:
        lines.append(f"│")
        lines.append(f"│ 通过的门槛:")
        for r in sig.trigger_reasons:
            lines.append(f"│   ✓ {r}")

    if sig.risk_warnings:
        lines.append(f"│")
        lines.append(f"│ 风险:")
        for r in sig.risk_warnings:
            lines.append(f"│   ✗ {r}")

    if sig.failure_conditions:
        lines.append(f"│")
        lines.append(f"│ 失效条件:")
        for fc in sig.failure_conditions:
            lines.append(f"│   ❌ {fc}")

    lines.append(f"│{'─'*52}│")
    lines.append(f"│ 数据更新: {sig.data_updated_at[:19]}")
    lines.append(f"└{'─'*52}┘")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_desk: Optional[SignalDesk] = None


def get_desk() -> SignalDesk:
    global _desk
    if _desk is None:
        _desk = SignalDesk()
    return _desk


def reset_desk() -> None:
    """重置单例（环境切换时使用）。"""
    global _desk
    _desk = None
