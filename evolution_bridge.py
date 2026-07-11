"""
进化内核集成桥 — evolution_bridge.py

连接 serenity_evolution 安全进化内核与 SerenityMonitor 现有系统。
五个集成边界在此集中实现，每个目标模块只做最小 hook 注入。

集成边界:
  1. factor_ic.py outcome 回填 — 只写 IC 证据，不改权重
  2. 周度任务 — 从 scoring_history / IC 历史生成候选
  3. 回测器 — 候选和 Frozen 统一走 AShareExecutionSimulator
  4. paper_trader.py — 读取 evolution_active_v2.environment='paper'
  5. auto_gate.py — 实盘只读 environment='live'；无批准则用 Frozen
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from serenity_evolution.candidate import FactorEvidence, WeightCandidateGenerator
from serenity_evolution.engine import EvolutionEngine, StrategySeries
from serenity_evolution.gate import GatePolicy, PromotionGate
from serenity_evolution.microstructure import (
    AShareExecutionSimulator,
    AShareRules,
    FillResult,
    MarketBar,
    Order,
    PositionLot,
    Side,
)
from serenity_evolution.models import ComparisonMetrics, EvolutionCandidate, GateResult, Stage
from serenity_evolution.store import EvolutionStore

# ── 默认数据库路径 ──────────────────────────────────────────────

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "serenity.db"


# ═══════════════════════════════════════════════════════════════
# 共享工具
# ═══════════════════════════════════════════════════════════════


def _get_store(db_path: str | Path | None = None) -> EvolutionStore:
    path = str(db_path or DEFAULT_DB_PATH)
    store = EvolutionStore(path)
    store.migrate()
    _ensure_evidence_table(store)
    return store


def _ensure_evidence_table(store: EvolutionStore) -> None:
    """Ensure evolution_ic_evidence exists — called on every bridge init."""
    with store.connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_ic_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at TEXT NOT NULL,
                factor TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                ic_values_json TEXT NOT NULL
            )
            """
        )


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# Boundary 1 — IC 证据回填（factor_ic.py 侧）
# ═══════════════════════════════════════════════════════════════

# IC 维度名 → evolution 因子名映射
IC_DIM_TO_EVO_FACTOR = {
    "zone_score": "zone",
    "momentum_score": "momentum",
    "volume_score": "volume",
    "serenity_score": "serenity",
    "factor_score": "factor",
    "technical_score": "technical",
    "moat_score": "moat",
}


def collect_ic_evidence(
    factor_ic_result: dict,
    *,
    min_samples: int = 50,
) -> tuple[FactorEvidence, ...]:
    """从 factor_ic.compute_rank_ic() 结果中提取 FactorEvidence。

    只收集证据，绝不修改权重。返回值可直接传给 WeightCandidateGenerator。
    """
    all_ics = factor_ic_result.get("all_ics", {})
    evidence: list[FactorEvidence] = []
    for ic_dim, evo_name in IC_DIM_TO_EVO_FACTOR.items():
        ics = all_ics.get(ic_dim, [])
        if len(ics) >= min_samples:
            evidence.append(FactorEvidence(evo_name, tuple(ics)))
    return tuple(evidence)


def backfill_evidence_to_store(
    factor_ic_result: dict,
    db_path: str | Path | None = None,
) -> int:
    """将 IC 证据持久化到 evolution 存储（只写，不改权重）。

    Returns:
        写入的证据维度数
    """
    evidence = collect_ic_evidence(factor_ic_result)
    # Always ensure store + table are ready, even when evidence is empty
    store = _get_store(db_path)
    if not evidence:
        return 0
    # 证据作为候选的元数据保存（不生成候选，只记录证据）
    with store.connect() as db:
        now = now_utc()
        for item in evidence:
            db.execute(
                "INSERT INTO evolution_ic_evidence (collected_at, factor, sample_count, ic_values_json) "
                "VALUES (?, ?, ?, ?)",
                (now, item.factor, len(item.ic_values), json.dumps(list(item.ic_values))),
            )
    return len(evidence)


# ═══════════════════════════════════════════════════════════════
# Boundary 2 — 周度候选生成（weight_adjuster.py / daily_workflow.py 侧）
# ═══════════════════════════════════════════════════════════════

# Frozen Baseline 的默认权重（与 scorer.py _SCORE_WEIGHT_DEFAULTS 对齐）
FROZEN_DEFAULT_WEIGHTS: dict[str, float] = {
    "zone": 0.20,
    "momentum": 0.18,
    "volume": 0.04,
    "serenity": 0.18,
    "factor": 0.17,
    "technical": 0.10,
    "moat": 0.13,
}


def generate_weekly_candidate(
    baseline_version: str,
    baseline_weights: dict[str, float],
    ic_evidence: Sequence[FactorEvidence],
    *,
    db_path: str | Path | None = None,
    created_at: datetime | None = None,
) -> EvolutionCandidate:
    """生成周度候选（不超过 max_weekly_delta=2% 单维变化）。"""
    generator = WeightCandidateGenerator()
    candidate = generator.generate(
        baseline_version,
        baseline_weights,
        ic_evidence,
        created_at=created_at,
    )
    store = _get_store(db_path)
    engine = EvolutionEngine(store)
    engine.register(candidate)
    return candidate


# ═══════════════════════════════════════════════════════════════
# Boundary 3 — 统一成交模拟（回测路径）
# ═══════════════════════════════════════════════════════════════


def build_evolution_simulator(
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.0005,
    slippage_bps: float = 8.0,
) -> AShareExecutionSimulator:
    """构建进化内核成交模拟器，参数对齐 config.default.json。"""
    rules = AShareRules(
        commission_rate=commission_rate,
        stamp_tax_sell_rate=stamp_tax_rate,
        slippage_bps=slippage_bps,
    )
    return AShareExecutionSimulator(rules)


def market_bar_from_row(row: dict) -> MarketBar:
    """从数据库行情行构建 MarketBar。"""
    return MarketBar(
        trade_date=_parse_date(row.get("date", "")),
        code=str(row.get("code", "")),
        open=float(row.get("open", 0)),
        high=float(row.get("high", 0)),
        low=float(row.get("low", 0)),
        close=float(row.get("close", 0)),
        prev_close=float(row.get("prev_close", row.get("open", 0))),
        volume=int(row.get("volume", 0)),
        suspended=bool(row.get("suspended", False)),
    )


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def backtest_strategy_series(
    weights: dict[str, float],
    price_history: list[dict],
    scoring_history: list[dict],
    *,
    simulator: AShareExecutionSimulator | None = None,
    initial_capital: float = 50000.0,
) -> StrategySeries:
    """
    在给定权重下运行完整回测，返回 StrategySeries。

    参数:
        weights: 因子权重字典
        price_history: [{code, date, open, high, low, close, volume, ...}, ...]
        scoring_history: [{code, date, zone_score, momentum_score, ...}, ...]
        simulator: AShareExecutionSimulator 实例
        initial_capital: 初始资金

    返回:
        StrategySeries 可直接传给 EvolutionEngine.evaluate()
    """
    sim = simulator or build_evolution_simulator()

    # 按日期排序
    price_history = sorted(price_history, key=lambda r: (r["code"], r["date"]))
    scoring_history = sorted(scoring_history, key=lambda r: (r["code"], r["date"]))

    # 构建评分查找表
    score_lookup: dict[tuple[str, str], float] = {}
    for row in scoring_history:
        key = (row["code"], row["date"])
        total = sum(
            row.get(dim, 0) * weights.get(factor, 0)
            for dim, factor in [
                ("zone_score", "zone"),
                ("momentum_score", "momentum"),
                ("volume_score", "volume"),
                ("serenity_score", "serenity"),
                ("factor_score", "factor"),
                ("technical_score", "technical"),
                ("moat_score", "moat"),
            ]
        )
        score_lookup[key] = total

    # 按日期分组回测
    all_dates = sorted({r["date"] for r in price_history})
    all_codes = sorted({r["code"] for r in price_history})

    # 按代码索引价格
    price_by_code: dict[str, list[dict]] = {}
    for r in price_history:
        price_by_code.setdefault(r["code"], []).append(r)

    cash = initial_capital
    holdings: dict[str, int] = {}  # code → shares
    lots: list[PositionLot] = []
    daily_returns: list[float] = []
    trade_returns: list[float] = []
    total_trades = 0
    executable_attempts = 0
    executable_successes = 0

    for dt in all_dates:
        nav_before = cash + sum(
            holdings.get(code, 0) * _get_price_on_date(price_by_code, code, dt)
            for code in holdings
        )

        # 按评分排序选 top-N
        candidates = []
        for code in all_codes:
            sc = score_lookup.get((code, dt), 0)
            if sc > 0:
                candidates.append((code, sc))

        candidates.sort(key=lambda x: -x[1])
        top_n = min(3, len(candidates))  # 最多持有 3 只

        for code, _ in candidates[:top_n]:
            bar_dict = _get_bar_on_date(price_by_code, code, dt)
            if bar_dict is None:
                continue
            bar = market_bar_from_row(bar_dict)

            # 买入逻辑
            if code not in holdings or holdings.get(code, 0) == 0:
                executable_attempts += 1
                position_size = (nav_before * 0.2) if nav_before > 0 else initial_capital * 0.2
                qty = int(position_size / bar.open / 100) * 100
                if qty < 100:
                    continue
                if qty * bar.open > cash:
                    qty = int(cash * 0.9 / bar.open / 100) * 100
                if qty < 100:
                    continue

                order = Order(code=code, side=Side.BUY, quantity=qty, submitted_at=datetime.now(tz=timezone.utc))
                fill = sim.simulate(order, bar)
                if fill.filled:
                    holdings[code] = holdings.get(code, 0) + fill.quantity
                    cash -= (fill.gross_value + fill.commission)
                    lots.append(PositionLot(code=code, quantity=fill.quantity, acquired_on=bar.trade_date))
                    executable_successes += 1

        # 卖出逻辑：持仓超过 10 天或评分变负
        for code in list(holdings):
            lots_for_code = [lot for lot in lots if lot.code == code and lot.quantity > 0]
            if not lots_for_code:
                continue
            sc = score_lookup.get((code, dt), 0)
            oldest_lot = min(lots_for_code, key=lambda x: x.acquired_on)
            days_held = (dt - oldest_lot.acquired_on).days

            if sc < 0 or days_held > 10:
                bar_dict = _get_bar_on_date(price_by_code, code, dt)
                if bar_dict is None:
                    continue
                bar = market_bar_from_row(bar_dict)
                qty = holdings.get(code, 0)
                if qty <= 0:
                    continue

                order = Order(code=code, side=Side.SELL, quantity=qty, submitted_at=datetime.now(tz=timezone.utc))
                fill = sim.simulate(order, bar, tuple(lots_for_code))
                if fill.filled:
                    cash += (fill.gross_value - fill.commission - fill.stamp_tax)
                    trade_returns.append((fill.gross_value - fill.commission - fill.stamp_tax) / (fill.price * fill.quantity) - 1.0 if fill.price and fill.quantity else 0)
                    holdings[code] = 0
                    total_trades += 1

        # 日终估值
        nav_after = cash + sum(
            holdings.get(code, 0) * _get_price_on_date(price_by_code, code, dt)
            for code in holdings
        )
        if nav_before > 0:
            daily_returns.append(nav_after / nav_before - 1.0)
        else:
            daily_returns.append(0.0)

    executable_rate = executable_successes / executable_attempts if executable_attempts > 0 else 1.0
    turnover = total_trades / max(len(all_dates), 1)

    return StrategySeries(
        daily_returns=daily_returns,
        trade_returns=trade_returns,
        turnover=turnover,
        executable_rate=executable_rate,
    )


def backtest_frozen_baseline(
    price_history: list[dict],
    scoring_history: list[dict],
    *,
    simulator: AShareExecutionSimulator | None = None,
    frozen_weights: dict[str, float] | None = None,
) -> StrategySeries:
    """使用 Frozen Baseline 权重运行回测。"""
    weights = frozen_weights or FROZEN_DEFAULT_WEIGHTS
    return backtest_strategy_series(weights, price_history, scoring_history, simulator=simulator)


def _get_price_on_date(
    price_by_code: dict[str, list[dict]], code: str, target_date: date
) -> float:
    rows = price_by_code.get(code, [])
    target_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
    for r in rows:
        if str(r.get("date", ""))[:10] == target_str[:10]:
            return float(r.get("close", 0))
    return 0.0


def _get_bar_on_date(
    price_by_code: dict[str, list[dict]], code: str, target_date: date
) -> dict | None:
    rows = price_by_code.get(code, [])
    target_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
    for r in rows:
        if str(r.get("date", ""))[:10] == target_str[:10]:
            return r
    return None


# ═══════════════════════════════════════════════════════════════
# Boundary 4 — Paper Trader 读取进化权重
# ═══════════════════════════════════════════════════════════════


def get_active_paper_weights(
    db_path: str | Path | None = None,
) -> dict[str, float] | None:
    """读取当前 paper 环境的活跃候选权重。

    Returns:
        None 如果没有活跃的进化候选（应退回 Frozen Baseline）
    """
    store = _get_store(db_path)
    try:
        with store.connect() as db:
            row = db.execute(
                "SELECT candidate_id, weights_json FROM evolution_candidates_v2 "
                "WHERE stage IN ('PAPER_CANARY', 'LIVE') "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["weights_json"])
    except Exception:
        return None


def get_active_live_weights(
    db_path: str | Path | None = None,
) -> dict[str, float] | None:
    """读取当前 live 环境的活跃候选权重。

    仅当 evolution_active_v2.environment='live' 且候选存在时返回。
    """
    store = _get_store(db_path)
    try:
        with store.connect() as db:
            row = db.execute(
                "SELECT c.weights_json FROM evolution_active_v2 a "
                "JOIN evolution_candidates_v2 c ON a.candidate_id = c.candidate_id "
                "WHERE a.environment = 'live' AND c.stage = 'LIVE'"
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["weights_json"])
    except Exception:
        return None


def has_live_approval(
    candidate_id: str,
    db_path: str | Path | None = None,
) -> bool:
    """检查候选是否有显式人工批准记录。"""
    store = _get_store(db_path)
    try:
        with store.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM evolution_transitions_v2 "
                "WHERE candidate_id = ? AND to_stage = 'LIVE' AND approval_ref IS NOT NULL",
                (candidate_id,),
            ).fetchone()
            return (row["cnt"] if row else 0) > 0
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Boundary 5 — Auto Gate 实盘守卫
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LiveGateStatus:
    """实盘闸门状态 — auto_gate.py 在每次交易决策前检查。"""
    live_allowed: bool
    candidate_id: str | None
    reason: str
    frozen_active: bool  # True = 使用 Frozen Baseline


def check_live_gate(
    db_path: str | Path | None = None,
    *,
    live_apply_enabled: bool = False,
) -> LiveGateStatus:
    """
    三道锁检查 — 缺一不可进入实盘。

    1. live_apply_enabled 必须为 True
    2. evolution_active_v2 中必须有 environment='live' 且 stage='LIVE' 的候选
    3. 必须有显式人工批准记录 (transitions with approval_ref)
    """
    if not live_apply_enabled:
        return LiveGateStatus(
            live_allowed=False,
            candidate_id=None,
            reason="live_apply_enabled is False — 系统配置禁止实盘自动进化",
            frozen_active=True,
        )

    store = _get_store(db_path)
    try:
        with store.connect() as db:
            row = db.execute(
                "SELECT a.candidate_id, c.stage, c.created_at "
                "FROM evolution_active_v2 a "
                "JOIN evolution_candidates_v2 c ON a.candidate_id = c.candidate_id "
                "WHERE a.environment = 'live'"
            ).fetchone()

            if row is None or row["candidate_id"] is None:
                return LiveGateStatus(
                    live_allowed=False,
                    candidate_id=None,
                    reason="无活跃 live 候选 — 继续使用 Frozen Baseline",
                    frozen_active=True,
                )

            cid = row["candidate_id"]
            if row["stage"] != "LIVE":
                return LiveGateStatus(
                    live_allowed=False,
                    candidate_id=cid,
                    reason=f"候选 {cid} stage={row['stage']}，非 LIVE",
                    frozen_active=True,
                )

            # 检查人工批准记录
            approval = db.execute(
                "SELECT COUNT(*) as cnt FROM evolution_transitions_v2 "
                "WHERE candidate_id = ? AND to_stage = 'LIVE' AND approval_ref IS NOT NULL "
                "AND approval_ref != ''",
                (cid,),
            ).fetchone()

            if not approval or approval["cnt"] == 0:
                return LiveGateStatus(
                    live_allowed=False,
                    candidate_id=cid,
                    reason=f"候选 {cid} 缺少显式人工批准记录",
                    frozen_active=True,
                )

            return LiveGateStatus(
                live_allowed=True,
                candidate_id=cid,
                reason=f"候选 {cid} 三道锁全部通过",
                frozen_active=False,
            )
    except Exception as e:
        return LiveGateStatus(
            live_allowed=False,
            candidate_id=None,
            reason=f"进化存储查询失败: {e}",
            frozen_active=True,
        )


def promote_to_live(
    candidate_id: str,
    approval_ref: str,
    *,
    canary_days: int = 20,
    live_apply_enabled: bool = True,
    db_path: str | Path | None = None,
) -> None:
    """执行实盘晋级 — 调用 EvolutionStore.promote_live()。"""
    store = _get_store(db_path)
    store.promote_live(
        candidate_id,
        approval_ref=approval_ref,
        canary_days=canary_days,
        live_apply_enabled=live_apply_enabled,
    )


def rollback_live(
    reason: str,
    approval_ref: str,
    db_path: str | Path | None = None,
) -> str | None:
    """回滚实盘进化权重到前一个版本。"""
    store = _get_store(db_path)
    return store.rollback_live(reason=reason, approval_ref=approval_ref)


def get_evolution_status(
    db_path: str | Path | None = None,
) -> dict:
    """获取进化系统状态（用于 dashboard / CLI）。"""
    store = _get_store(db_path)
    base = store.status()
    extra = {
        "frozen_weights": FROZEN_DEFAULT_WEIGHTS,
        "live_gate": None,
        "paper_active": get_active_paper_weights(db_path),
    }
    live = get_active_live_weights(db_path)
    gate = check_live_gate(db_path)
    extra["live_gate"] = {
        "live_allowed": gate.live_allowed,
        "candidate_id": gate.candidate_id,
        "reason": gate.reason,
        "frozen_active": gate.frozen_active,
    }
    extra["live_active"] = live is not None
    return {**base, **extra}


# ═══════════════════════════════════════════════════════════════
# 便捷 CLI 入口（供 daily_workflow.py 调用）
# ═══════════════════════════════════════════════════════════════


def run_weekly_evolution_cycle(
    db_path: str | Path | None = None,
    *,
    baseline_version: str = "frozen-v1",
    dry_run: bool = False,
) -> dict:
    """运行一次完整的周度进化周期。

    步骤:
      1. 收集 IC 证据
      2. 生成候选权重
      3. 运行回测（候选 vs Frozen）
      4. 闸门评估
      5. 通过 → PAPER_CANARY；失败 → REJECTED

    Returns:
        {candidate_id, passed, stage, ...}
    """
    import factor_ic

    ic_result = factor_ic.compute_rank_ic(days=60, window=20)
    evidence = collect_ic_evidence(ic_result)
    if len(evidence) < 3:
        return {"status": "insufficient_evidence", "dimensions": len(evidence)}

    if dry_run:
        # 只生成候选，不评估
        candidate = WeightCandidateGenerator().generate(
            baseline_version, FROZEN_DEFAULT_WEIGHTS, evidence
        )
        return {
            "status": "dry_run",
            "candidate_id": candidate.candidate_id,
            "weights": candidate.weights,
        }

    # 从数据库获取价格和评分历史
    from db import get_conn

    conn = get_conn()
    price_rows = [
        dict(r) for r in conn.execute(
            "SELECT code, date, open, high, low, close, volume FROM price_history "
            "ORDER BY code, date"
        ).fetchall()
    ]
    score_rows = [
        dict(r) for r in conn.execute(
            "SELECT code, date, zone_score, momentum_score, volume_score, "
            "serenity_score, factor_score, technical_score, moat_score "
            "FROM scoring_history ORDER BY code, date"
        ).fetchall()
    ]
    conn.close()

    if len(price_rows) < 120 or len(score_rows) < 120:
        return {"status": "insufficient_data", "price_days": len(price_rows), "score_days": len(score_rows)}

    # 生成候选
    candidate = generate_weekly_candidate(
        baseline_version, FROZEN_DEFAULT_WEIGHTS, evidence, db_path=db_path
    )

    # 回测候选
    candidate_series = backtest_strategy_series(
        candidate.weights, price_rows, score_rows
    )

    # 回测 Frozen Baseline
    baseline_series = backtest_frozen_baseline(
        price_rows, score_rows, frozen_weights=FROZEN_DEFAULT_WEIGHTS
    )

    # 评估
    store = _get_store(db_path)
    engine = EvolutionEngine(store)
    comparison, gate_result = engine.evaluate(
        candidate.candidate_id,
        candidate_series,
        baseline_series,
        data_quality_ok=True,
        costs_included=True,
        market_rules_included=True,
    )

    return {
        "status": "evaluated",
        "candidate_id": candidate.candidate_id,
        "stage": gate_result.stage.value,
        "passed": gate_result.passed,
        "failures": list(gate_result.failures) if not gate_result.passed else [],
        "excess_return": round(comparison.excess_return, 4),
        "sharpe_delta": round(comparison.sharpe_delta, 4),
        "bootstrap_probability": round(comparison.bootstrap_probability, 4),
    }
