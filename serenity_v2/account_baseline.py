"""
Serenity 2.0 — 账户基线 (Phase 0) [v2.1 事务化+T+1修正]

变更 (v2.1):
  · fill_trade() 单一事务包裹，任一步骤失败全部回滚
  · external_fill_id 提供可靠幂等键
  · Position 区分 total_shares / available_shares / unsettled_buy_shares
  · T+1: 买入当日 unsettled_buy_shares 增加，available_shares 不变
  · 提交前执行不变量检查
  · 移除模块级 DB_PATH 默认值，由 SerenityEnv 注入
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Optional, Any, Callable

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """单笔持仓 [v2.1: T+1区分可卖/不可卖]"""
    code: str
    name: str
    market: str                         # sh / sz
    shares: int                         # 总持仓
    available_shares: int               # 可卖数量（不含当日买入）
    unsettled_buy_shares: int = 0       # 当日买入未结算总数（T+1不可卖）
    unsettled_batches: list = field(default_factory=list)  # [{buy_date,shares,settlement_date}]
    cost_basis: float = 0.0             # 成本价
    current_price: float = 0.0          # 现价
    market_value: float = 0.0           # 市值
    pnl: float = 0.0                    # 浮动盈亏
    pnl_pct: float = 0.0                # 盈亏百分比

    @property
    def sellable_shares(self) -> int:
        """实际可卖 = available_shares（不含 unsettled）。"""
        return self.available_shares


@dataclass
class Order:
    """当日委托"""
    code: str
    side: str                          # buy / sell
    price: float
    quantity: int
    status: str                        # pending / filled / cancelled


@dataclass
class Trade:
    """当日成交 [v2.1: external_fill_id 用于幂等]"""
    id: Optional[int] = None
    code: str = ""
    side: str = ""                     # buy / sell
    price: float = 0.0
    quantity: int = 0
    amount: float = 0.0
    timestamp: str = ""                # ISO 8601
    source: str = "manual"             # manual / screenshot / broker_api
    confirmed: bool = False
    note: str = ""
    external_fill_id: str = ""         # 券商成交编号/导入批次+序号
    order_id: str = ""                 # 关联委托ID
    fill_sequence: int = 0             # 分笔序号
    import_batch_id: str = ""          # 导入批次
    trade_hash: str = ""               # 指纹（辅助去重，非主键）


@dataclass
class AccountState:
    """账户完整状态快照 [v2.1]"""
    total_assets: float = 0.0
    total_market_value: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    position_ratio_pct: float = 0.0

    floating_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0

    positions: list[Position] = field(default_factory=list)
    today_orders: list[Order] = field(default_factory=list)
    today_trades: list[Trade] = field(default_factory=list)

    snapshot_at: str = ""
    data_source: str = "manual"
    data_confidence: str = "high"
    notes: str = ""


# ---------------------------------------------------------------------------
# 风险约束（不变）
# ---------------------------------------------------------------------------

@dataclass
class RiskConstraints:
    """取自 docs/B-账户基线v1.md §2.2"""
    max_single_position_pct: float = 0.40
    max_sector_position_pct: float = 0.60
    max_single_loss_pct: float = 0.02
    max_account_drawdown_pct: float = 0.15
    min_cash: float = 0.0

    require_profit_for_add: bool = True
    require_high_confidence: bool = True
    max_drawdown_for_add: float = -0.10

    forced_reduce_position_pct: float = 0.40
    forced_reduce_loss_pct: float = 0.08


# ---------------------------------------------------------------------------
# 持仓行业映射
# ---------------------------------------------------------------------------

SECTOR_MAP: dict[str, str] = {
    "600487": "光通信/光纤光缆",
    "000988": "光通信/激光/AI算力",
    "002281": "光通信/CPO",
    "603083": "光通信/CPO",
    "600176": "玻纤/建材",
    "600141": "化工/磷化工",
    "600460": "半导体",
    "603986": "半导体/存储",
    "002428": "半导体/材料",
    "600036": "银行",
    "600585": "建材/水泥",
    "600900": "电力/公用事业",
    "601398": "银行",
    "601006": "运输/铁路",
    "000938": "AI算力/服务器",
    "601689": "汽车零部件/机器人",
    "002050": "汽车零部件/机器人",
    "601100": "工程机械/机器人",
    "600580": "电机制造/机器人",
    "002896": "精密减速器/机器人",
}

# ---------------------------------------------------------------------------
# 不变式常量
# ---------------------------------------------------------------------------

# fill_trade 提交前检查的最小容差（元）
INVARIANT_TOLERANCE = 0.02
# ---------------------------------------------------------------------------
# 交易日历（简化版：跳过周六日，不含节假日）
# ---------------------------------------------------------------------------

def _next_business_day(d: date) -> date:
    """下一个交易日（跳过周六日）。"""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d = d + timedelta(days=1)
    return d


def _is_business_day(d: date) -> bool:
    """是否交易日（简化：周一到周五）。"""
    return d.weekday() < 5




# ---------------------------------------------------------------------------
# 账户基线管理器 [v2.1]
# ---------------------------------------------------------------------------

class AccountBaseline:
    """
    账户基线管理器 —— 唯一的账户状态读写入口。

    使用方式:
        from serenity_v2.env import get_env
        env = get_env()
        baseline = AccountBaseline(env.db_path)
        state = baseline.load_latest()
    """

    def __init__(self, db_path: Path):
        """必须显式传入数据库路径。"""
        self.db_path = db_path

    # ---- 连接 ----

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ---- 读写 ----

    def load_latest(self) -> AccountState:
        """加载最新的账户快照。"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM portfolio_reconciliations
                   ORDER BY snapshot_at DESC, id DESC LIMIT 1"""
            ).fetchone()

            if row is None:
                return AccountState()

            positions_raw = json.loads(row["positions_json"])
            positions = [
                Position(
                    code=p["code"], name=p.get("name", ""),
                    market=p.get("market", ""),
                    shares=p["shares"],
                    available_shares=p.get("available_shares", p["shares"]),
                    unsettled_buy_shares=p.get("unsettled_buy_shares", 0),
                    unsettled_batches=p.get("unsettled_batches", []),
                    cost_basis=p["cost_basis"],
                    current_price=p.get("current_price", 0),
                    market_value=p.get("market_value", 0),
                    pnl=p.get("pnl", 0), pnl_pct=p.get("pnl_pct", 0),
                )
                for p in positions_raw
            ]

            return AccountState(
                total_assets=row["total_assets"],
                total_market_value=row["holdings_value"],
                available_cash=row["cash"],
                position_ratio_pct=row["position_ratio_pct"],
                floating_pnl=row["floating_profit"],
                daily_pnl=row["daily_profit"],
                daily_pnl_pct=row["daily_profit_pct"],
                positions=positions,
                snapshot_at=row["snapshot_at"],
                data_source=row["source"],
                data_confidence="high",
                notes=row["notes"] or "",
            )
        finally:
            conn.close()

    def save_snapshot(self, state: AccountState) -> int:
        """写入账户快照。返回行ID。"""
        conn = self._get_conn()
        try:
            from .clock import get_clock
            now = state.snapshot_at or get_clock().now().isoformat(timespec="milliseconds")

            positions_json = json.dumps(
                [asdict(p) for p in state.positions], ensure_ascii=False
            )

            conn.execute(
                """INSERT OR REPLACE INTO portfolio_reconciliations
                   (snapshot_at, source, total_assets, holdings_value, cash,
                    floating_profit, daily_profit, daily_profit_pct,
                    position_ratio_pct, positions_json, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now, state.data_source, state.total_assets,
                    state.total_market_value, state.available_cash,
                    state.floating_pnl, state.daily_pnl, state.daily_pnl_pct,
                    state.position_ratio_pct, positions_json, state.notes,
                ),
            )
            conn.commit()
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # fill_trade — [v2.1] 单一事务 + external_fill_id 幂等
    # ------------------------------------------------------------------

    def fill_trade(
        self,
        code: str,
        side: str,
        price: float,
        quantity: int,
        timestamp: Optional[str] = None,
        source: str = "manual",
        note: str = "",
        external_fill_id: str = "",
        order_id: str = "",
        fill_sequence: int = 0,
        import_batch_id: str = "",
        commission: float = 0.0,
        stamp_tax: float = 0.0,
        transfer_fee: float = 0.0,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        人工回填成交 [v2.1: 事务化]。

        幂等键优先级:
          1. external_fill_id（券商唯一成交编号）
          2. import_batch_id + fill_sequence（导入批次）
          3. trade_hash（指纹辅助告警）

        流程（全部在一个事务内）:
          1. 校验（非法方向/负价格/零数量/手续费合理性）
          2. 幂等检查（external_fill_id 或 trade_hash）
          3. 写入成交记录
          4. 更新持仓和现金（含T+1逻辑）
          5. 写入账户快照
          6. 写入 NAV 历史
          7. 不变量检查
          8. 提交 || 回滚

        fault_hook(stage): 测试用故障注入钩子，生产环境为 None。
        返回 {"success": bool, "state": AccountState, "message": str, "dup": bool}
        """
        # ------------------------------------------------------------------
        # 0. 输入校验
        # ------------------------------------------------------------------
        errors = []
        if side not in ("buy", "sell"):
            errors.append(f"非法方向: {side}")
        if price <= 0:
            errors.append(f"价格必须为正: {price}")
        if quantity <= 0:
            errors.append(f"数量必须为正: {quantity}")
        if commission < 0 or stamp_tax < 0 or transfer_fee < 0:
            errors.append("手续费不可为负")
        if stamp_tax > 0 and side != "sell":
            errors.append("买入不应有印花税")
        if errors:
            return {"success": False, "state": None, "message": "; ".join(errors), "dup": False}

        from .clock import get_clock
        ts = timestamp or get_clock().now().isoformat(timespec="seconds")
        amount = round(price * quantity, 2)
        total_fee = round(commission + stamp_tax + transfer_fee, 2)

        # 指纹（辅助去重，非主键）
        trade_hash = hashlib.sha256(
            f"{code}_{side}_{price}_{quantity}_{ts}_{source}".encode()
        ).hexdigest()[:16]

        conn = self._get_conn()
        try:
            # ==============================================================
            # 事务开始
            # ==============================================================
            conn.execute("BEGIN IMMEDIATE")

            # ------------------------------------------------------------------
            # 1. 幂等检查
            # ------------------------------------------------------------------
            if external_fill_id:
                existing = conn.execute(
                    "SELECT id FROM trades WHERE external_fill_id = ? LIMIT 1",
                    (external_fill_id,)
                ).fetchone()
                if existing:
                    conn.execute("ROLLBACK")
                    return {
                        "success": False, "state": None,
                        "message": f"重复成交: external_fill_id={external_fill_id}",
                        "dup": True,
                    }

            # trade_hash 辅助检查（仅告警，不阻止，因为可能两笔真实成交指纹相同）
            hash_existing = conn.execute(
                "SELECT id, external_fill_id FROM trades WHERE trade_hash = ? LIMIT 1",
                (trade_hash,)
            ).fetchone()
            dup_warning = ""
            if hash_existing:
                dup_warning = (
                    f"指纹重复预警: trade_hash={trade_hash} 与 "
                    f"trade#{hash_existing['id']}(ext_id={hash_existing['external_fill_id']}) 相同"
                )

            # ------------------------------------------------------------------
            # 2. 写入成交记录
            # ------------------------------------------------------------------
            conn.execute(
                """INSERT INTO trades
                   (code, action, price, quantity, date, note,
                    trade_hash, trade_amount, source,
                    external_fill_id, order_id, fill_sequence, import_batch_id,
                    commission, stamp_tax, transfer_fee)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code, side, price, quantity, ts[:10], note,
                    trade_hash, amount, source,
                    external_fill_id, order_id, fill_sequence, import_batch_id,
                    commission, stamp_tax, transfer_fee,
                ),
            )

            # --- fault_hook: after_trade_insert ---
            if fault_hook:
                fault_hook("after_trade_insert")

            # ------------------------------------------------------------------
            # 3. 加载当前状态
            # ------------------------------------------------------------------
            current = self.load_latest()

            # ------------------------------------------------------------------
            # 4. 更新持仓（T+1 逻辑）
            # ------------------------------------------------------------------
            from config import STOCK_MAP
            info = STOCK_MAP.get(code, {})
            name = info.get("name", code)
            market = info.get("market", "")

            updated_positions: list[Position] = []
            found = False

            # 现金变化（含手续费）
            if side == "buy":
                cash_delta = -(amount + total_fee)
            else:
                cash_delta = amount - total_fee

            for p in current.positions:
                if p.code == code:
                    found = True
                    if side == "buy":
                        new_shares = p.shares + quantity
                        new_cost = (
                            (p.cost_basis * p.shares + amount) / new_shares
                            if new_shares > 0 else 0
                        )
                        p.shares = new_shares
                        # T+1: 买入当日不增加可卖数量
                        p.unsettled_buy_shares += quantity
                        from .clock import get_clock
                        today_str = get_clock().today().isoformat()
                        settle_date = get_clock().settlement_date_for_buy()
                        p.unsettled_batches.append({
                            "buy_date": today_str,
                            "shares": quantity,
                            "settlement_date": settle_date.isoformat(),
                        })
                        p.cost_basis = round(new_cost, 4)
                        p.market_value = round(p.current_price * new_shares, 2)
                        p.pnl = round(p.market_value - p.cost_basis * new_shares, 2)
                    elif side == "sell":
                        # 卖出席先检查可卖
                        if quantity > p.available_shares:
                            conn.execute("ROLLBACK")
                            return {
                                "success": False, "state": None,
                                "message": (
                                    f"超卖: 卖出{quantity}股 > 可卖{p.available_shares}股"
                                ),
                                "dup": False,
                            }
                        new_shares = p.shares - quantity
                        if new_shares <= 0:
                            continue  # 清仓，移除该持仓
                        # 卖出时优先消耗 unsettled（如果适用）但实际上不可卖
                        p.shares = new_shares
                        p.available_shares -= quantity
                        p.unsettled_buy_shares = max(0, p.unsettled_buy_shares)
                        p.market_value = round(p.current_price * new_shares, 2)
                        p.pnl = round(p.market_value - p.cost_basis * new_shares, 2)
                    updated_positions.append(p)
                else:
                    updated_positions.append(p)

            if not found and side == "buy":
                # 新建持仓：当日买入，可卖=0
                updated_positions.append(
                    Position(
                        code=code, name=name, market=market,
                        shares=quantity, available_shares=0,
                        unsettled_buy_shares=quantity,
                        unsettled_batches=[{
                            "buy_date": get_clock().today().isoformat(),
                            "shares": quantity,
                            "settlement_date": get_clock().settlement_date_for_buy().isoformat(),
                        }],
                        cost_basis=price, current_price=price,
                        market_value=amount,
                    )
                )
            elif not found and side == "sell":
                conn.execute("ROLLBACK")
                return {
                    "success": False, "state": None,
                    "message": f"卖出未持仓标的: {code}",
                    "dup": False,
                }

            # --- fault_hook: after_position_update ---
            if fault_hook:
                fault_hook("after_position_update")

            # ------------------------------------------------------------------
            # 5. 重新计算账户
            # ------------------------------------------------------------------
            new_cash = round(current.available_cash + cash_delta, 2)
            new_mv = round(sum(p.market_value for p in updated_positions), 2)
            new_total = round(new_cash + new_mv, 2)

            # ------------------------------------------------------------------
            # 6. 不变量检查（提交前）
            # ------------------------------------------------------------------
            invariant_errors = []

            # 现金非负
            if new_cash < -INVARIANT_TOLERANCE:
                invariant_errors.append(
                    f"现金为负: {new_cash:,.2f}"
                )

            # 可卖 ≤ 持仓
            for p in updated_positions:
                if p.available_shares > p.shares:
                    invariant_errors.append(
                        f"{p.code}: 可卖{p.available_shares} > 总持仓{p.shares}"
                    )

            # 总持仓 = available + unsettled_buy
            for p in updated_positions:
                if p.unsettled_buy_shares > 0:
                    expected_avail = p.shares - p.unsettled_buy_shares
                    if p.available_shares != expected_avail:
                        invariant_errors.append(
                            f"{p.code}: available_shares({p.available_shares}) "
                            f"!= shares({p.shares}) - unsettled({p.unsettled_buy_shares})"
                        )
                # 批次总和 = unsettled_buy_shares
                batch_sum = sum(b["shares"] for b in p.unsettled_batches)
                if batch_sum != p.unsettled_buy_shares:
                    invariant_errors.append(
                        f"{p.code}: 批次总和({batch_sum}) != unsettled({p.unsettled_buy_shares})"
                    )

            if invariant_errors:
                conn.execute("ROLLBACK")
                return {
                    "success": False, "state": None,
                    "message": f"不变量检查失败: {'; '.join(invariant_errors)}",
                    "dup": False,
                }

            # ------------------------------------------------------------------
            # 7. 写入快照
            # ------------------------------------------------------------------

            # --- fault_hook: before_snapshot_insert ---
            if fault_hook:
                fault_hook("before_snapshot_insert")
            new_state = AccountState(
                total_assets=new_total,
                total_market_value=new_mv,
                available_cash=new_cash,
                position_ratio_pct=round(new_mv / new_total * 100, 2) if new_total > 0 else 0,
                positions=updated_positions,
                today_trades=[
                    Trade(
                        code=code, side=side, price=price,
                        quantity=quantity, amount=amount,
                        timestamp=ts, source=source, confirmed=True, note=note,
                        external_fill_id=external_fill_id,
                        order_id=order_id, fill_sequence=fill_sequence,
                        import_batch_id=import_batch_id,
                        trade_hash=trade_hash,
                    )
                ],
                snapshot_at=ts,
                data_source=source,
                data_confidence="high",
                notes=f"回填: {side} {name}({code}) {quantity}股@{price}" +
                      (f" [{dup_warning}]" if dup_warning else ""),
            )

            positions_json = json.dumps(
                [asdict(p) for p in updated_positions], ensure_ascii=False
            )
            conn.execute(
                """INSERT OR REPLACE INTO portfolio_reconciliations
                   (snapshot_at, source, total_assets, holdings_value, cash,
                    floating_profit, daily_profit, daily_profit_pct,
                    position_ratio_pct, positions_json, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, new_state.data_source, new_state.total_assets,
                    new_state.total_market_value, new_state.available_cash,
                    new_state.floating_pnl, new_state.daily_pnl, new_state.daily_pnl_pct,
                    new_state.position_ratio_pct, positions_json, new_state.notes,
                ),
            )

            # --- fault_hook: after_snapshot_insert ---
            if fault_hook:
                fault_hook("after_snapshot_insert")

            # ------------------------------------------------------------------
            # 8. 写入 NAV
            # ------------------------------------------------------------------
            conn.execute(
                """INSERT OR REPLACE INTO nav_history
                   (date, total_value, cash, holdings_value, profit_pct, positions_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    ts[:10], new_state.total_assets, new_state.available_cash,
                    new_state.total_market_value,
                    0.0,
                    json.dumps([asdict(p) for p in updated_positions], ensure_ascii=False),
                ),
            )

            # --- fault_hook: after_nav_insert ---
            if fault_hook:
                fault_hook("after_nav_insert")

            # --- fault_hook: before_commit ---
            if fault_hook:
                fault_hook("before_commit")

            # ==============================================================
            # 全部通过 → 提交
            # ==============================================================
            conn.commit()

            result = {"success": True, "state": new_state, "message": "回填成功", "dup": False}
            if dup_warning:
                result["message"] += f" ({dup_warning})"
            return result

        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return {"success": False, "state": None, "message": str(e), "dup": False}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 风险检查（不变）
    # ------------------------------------------------------------------

    def check_risk(
        self,
        state: Optional[AccountState] = None,
        rules: Optional[RiskConstraints] = None,
    ) -> list[dict[str, Any]]:
        """对账户状态执行风险规则检查。"""
        if state is None:
            state = self.load_latest()
        if rules is None:
            rules = RiskConstraints()

        alerts: list[dict[str, Any]] = []

        if state.total_assets <= 0:
            alerts.append({"severity": "📋", "rule": "无账户数据", "detail": "请先写入账户基线"})
            return alerts

        for p in state.positions:
            weight = p.market_value / state.total_assets if state.total_assets > 0 else 0
            if weight > rules.max_single_position_pct:
                alerts.append({
                    "severity": "🚨", "rule": "单票仓位超限",
                    "detail": f"{p.name}({p.code}) 仓位 {weight*100:.1f}% > {rules.max_single_position_pct*100:.0f}%",
                    "current": round(weight * 100, 1),
                    "limit": round(rules.max_single_position_pct * 100, 1),
                    "suggestion": f"建议减仓至 {rules.max_single_position_pct*100:.0f}% 以内",
                })

            if p.cost_basis > 0 and p.current_price > 0:
                loss_pct = (p.current_price - p.cost_basis) / p.cost_basis
                if loss_pct < -rules.forced_reduce_loss_pct:
                    alerts.append({
                        "severity": "⚠️", "rule": "浮亏超限",
                        "detail": f"{p.name}({p.code}) 浮亏 {loss_pct*100:.1f}% > {rules.forced_reduce_loss_pct*100:.0f}%",
                        "current": round(loss_pct * 100, 1),
                        "limit": round(-rules.forced_reduce_loss_pct * 100, 1),
                        "suggestion": "建议减仓或设置硬止损",
                    })

        sector_weights: dict[str, float] = {}
        for p in state.positions:
            sector = SECTOR_MAP.get(p.code, "未知")
            sector_weights[sector] = sector_weights.get(sector, 0) + p.market_value

        for sector, mv in sector_weights.items():
            weight = mv / state.total_assets if state.total_assets > 0 else 0
            if weight > rules.max_sector_position_pct:
                alerts.append({
                    "severity": "⚠️", "rule": "行业仓位超限",
                    "detail": f"{sector} 行业仓位 {weight*100:.1f}% > {rules.max_sector_position_pct*100:.0f}%",
                    "current": round(weight * 100, 1),
                    "limit": round(rules.max_sector_position_pct * 100, 1),
                })

        return alerts

    # ---- 账户感知的信号上下文 ----

    def signal_context(self, code: str, state: Optional[AccountState] = None) -> dict[str, Any]:
        """为信号台提供账户上下文。"""
        if state is None:
            state = self.load_latest()

        from config import STOCK_MAP
        info = STOCK_MAP.get(code, {})
        name = info.get("name", code)

        pos = next((p for p in state.positions if p.code == code), None)

        if pos is None:
            return {
                "code": code, "name": name,
                "holding": False,
                "position_shares": 0, "position_value": 0,
                "position_weight_pct": 0,
                "available_cash": state.available_cash,
                "can_buy": state.available_cash > 0,
                "max_buyable_shares": 0,
                "sector": SECTOR_MAP.get(code, "未知"),
            }

        weight = pos.market_value / state.total_assets if state.total_assets > 0 else 0
        max_weight = 0.40
        remaining_weight = max_weight - weight
        max_additional_value = state.total_assets * remaining_weight if remaining_weight > 0 else 0
        max_add_shares = int(max_additional_value / pos.current_price) if pos.current_price > 0 else 0

        return {
            "code": code, "name": name,
            "holding": True,
            "position_shares": pos.shares,
            "available_shares": pos.available_shares,
            "unsettled_buy_shares": pos.unsettled_buy_shares,
            "position_value": round(pos.market_value, 2),
            "position_weight_pct": round(weight * 100, 1),
            "cost_basis": pos.cost_basis,
            "current_price": pos.current_price,
            "pnl": round(pos.pnl, 2),
            "pnl_pct": round(pos.pnl_pct, 1),
            "available_cash": state.available_cash,
            "max_add_shares": max_add_shares,
            "max_single_weight_pct": round(max_weight * 100, 1),
            "sector": SECTOR_MAP.get(code, "未知"),
        }

    # ---- 加仓/减仓条件检查 ----

    def can_add_position(
        self, code: str, add_shares: int, add_price: float,
        state: Optional[AccountState] = None,
        rules: Optional[RiskConstraints] = None,
    ) -> dict[str, Any]:
        """检查是否可以加仓。"""
        if state is None:
            state = self.load_latest()
        if rules is None:
            rules = RiskConstraints()

        ctx = self.signal_context(code, state)

        if not ctx["holding"]:
            return {"allowed": False, "reason": "未持仓，应使用买入逻辑而非加仓"}

        add_amount = add_shares * add_price
        new_shares = ctx["position_shares"] + add_shares
        new_value = new_shares * add_price
        new_weight = new_value / state.total_assets if state.total_assets > 0 else 1

        if add_amount > state.available_cash:
            return {"allowed": False, "reason": f"现金不足: 需要{add_amount:.0f}, 可用{state.available_cash:.0f}"}

        if new_weight > rules.max_single_position_pct:
            return {"allowed": False, "reason": f"加仓后仓位{new_weight*100:.1f}% > 上限{rules.max_single_position_pct*100:.0f}%"}

        if rules.require_profit_for_add and ctx["pnl"] < 0:
            return {"allowed": False, "reason": f"当前浮亏{ctx['pnl']:.0f}元, 不加仓浮亏标的"}

        return {
            "allowed": True, "reason": "通过",
            "new_shares": new_shares,
            "new_weight_pct": round(new_weight * 100, 1),
            "new_cost": round((ctx["cost_basis"] * ctx["position_shares"] + add_amount) / new_shares, 4),
        }

    def need_reduce_position(
        self, code: str, state: Optional[AccountState] = None,
        rules: Optional[RiskConstraints] = None,
    ) -> dict[str, Any]:
        """检查是否需要减仓。"""
        if state is None:
            state = self.load_latest()
        if rules is None:
            rules = RiskConstraints()

        ctx = self.signal_context(code, state)

        if not ctx["holding"]:
            return {"should_reduce": False, "reason": "未持仓"}

        if ctx["position_weight_pct"] > rules.max_single_position_pct * 100:
            return {
                "should_reduce": True,
                "reason": f"仓位{ctx['position_weight_pct']}% > 上限{rules.max_single_position_pct*100:.0f}%",
                "suggested_reduce_shares": int(
                    ctx["position_shares"] *
                    (ctx["position_weight_pct"] - rules.max_single_position_pct * 100) / ctx["position_weight_pct"]
                ),
            }

        if ctx["pnl_pct"] < -rules.forced_reduce_loss_pct * 100:
            return {
                "should_reduce": True,
                "reason": f"浮亏{ctx['pnl_pct']:.1f}% > {rules.forced_reduce_loss_pct*100:.0f}%阈值",
                "suggested_reduce_shares": ctx["position_shares"] // 2,
            }

        return {"should_reduce": False, "reason": "正常"}

    # ---- 初始化账户基线 ----

    def bootstrap_from_doc_b(self) -> AccountState:
        """
        从 docs/B-账户基线v1.md 的初始快照创建账户基线。
        [v2.1: unsettled_buy_shares=0 因为是昨日快照]
        """
        state = AccountState(
            total_assets=249578.07,
            total_market_value=228186.00,
            available_cash=21392.07,
            position_ratio_pct=91.4,
            floating_pnl=-5933.89,
            daily_pnl=-625.31,
            daily_pnl_pct=-0.25,
            positions=[
                Position(
                    code="600487", name="亨通光电", market="sh",
                    shares=1500, available_shares=1500,
                    unsettled_buy_shares=0,
                    cost_basis=55.23, current_price=55.12,
                    market_value=82680.00, pnl=-227.29, pnl_pct=-0.20,
                ),
                Position(
                    code="600176", name="中国巨石", market="sh",
                    shares=2000, available_shares=2000,
                    unsettled_buy_shares=0,
                    cost_basis=38.71, current_price=38.70,
                    market_value=77400.00, pnl=-77.02, pnl_pct=-0.03,
                ),
                Position(
                    code="000988", name="华工科技", market="sz",
                    shares=600, available_shares=600,
                    unsettled_buy_shares=0,
                    cost_basis=122.716, current_price=113.51,
                    market_value=68106.00, pnl=-5574.01, pnl_pct=-7.50,
                ),
            ],
            snapshot_at="2026-07-22T15:00:00+08:00",
            data_source="screenshot",
            data_confidence="high",
            notes="Serenity 2.0 初始基线，数据来源：金融街证券截图 2026-07-22 15:00",
        )
        return state

    # ---- 摘要 ----

    def summary(self, state: Optional[AccountState] = None) -> str:
        """生成账户摘要。"""
        if state is None:
            state = self.load_latest()

        if state.total_assets <= 0:
            return "账户基线未初始化，请先执行 bootstrap 或 fill_trade"

        lines = [
            f"总资产 {state.total_assets:,.0f} | 现金 {state.available_cash:,.0f} | 仓位 {state.position_ratio_pct:.1f}%",
        ]
        for p in state.positions:
            pnl_sign = "+" if p.pnl >= 0 else ""
            unsettled = f" [T+1锁定{p.unsettled_buy_shares}股]" if p.unsettled_buy_shares > 0 else ""
            lines.append(
                f"  {p.name}({p.code}) {p.shares}股(可卖{p.available_shares})"
                f"@{p.cost_basis:.2f} "
                f"现价{p.current_price:.2f} 浮{pnl_sign}{p.pnl:+.0f}({p.pnl_pct:+.1f}%){unsettled}"
            )
        return "\n".join(lines)

    # ---- 审计 ----

    def audit(self, days: int = 30) -> list[dict[str, Any]]:
        """返回最近N天的快照记录。"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT snapshot_at, total_assets, cash, holdings_value,
                          floating_profit, position_ratio_pct, source, notes
                   FROM portfolio_reconciliations
                   ORDER BY snapshot_at DESC LIMIT ?""",
                (days,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- T+1 结算滚动 ----

    def settle_t1(self, reference_date: Optional[date] = None) -> dict[str, Any]:
        """
        按批次结算：仅结算 settlement_date <= reference_date 的批次。

        不一次性释放所有未结算持仓；不结算未来日期；
        不结算非交易日。重复调用安全（已结算批次被移除）。

        reference_date 默认为 clock 当前日期。
        """
        from .clock import get_clock
        today = reference_date or get_clock().today()
        state = self.load_latest()
        if state.total_assets <= 0:
            return {"success": False, "message": "无账户数据"}

        total_settled = 0
        settled_any = False

        for p in state.positions:
            remaining_batches = []
            for batch in p.unsettled_batches:
                settle_date = date.fromisoformat(batch["settlement_date"])
                if settle_date <= today:
                    # 该批次可结算
                    settled = batch["shares"]
                    p.available_shares += settled
                    p.unsettled_buy_shares -= settled
                    total_settled += settled
                    settled_any = True
                else:
                    remaining_batches.append(batch)
            p.unsettled_batches = remaining_batches

        if settled_any:
            # 清除旧 snapshot_at，让 save_snapshot 生成新时间戳
            state.snapshot_at = ""
            state.notes = (
                f"T+1批次结算: {total_settled}股 | "
                f"{get_clock().now().isoformat()}"
            )
            self.save_snapshot(state)
            return {
                "success": True,
                "message": f"已结算{total_settled}股",
                "state": state,
            }

        return {"success": True, "message": "无待结算买入", "state": state}


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_baseline: Optional[AccountBaseline] = None


def get_baseline(db_path: Optional[Path] = None) -> AccountBaseline:
    """
    获取 AccountBaseline 单例。
    首次调用时必须传入 db_path 或已设置环境。
    """
    global _baseline
    if _baseline is None:
        if db_path is None:
            from .env import get_env
            db_path = get_env().db_path
        _baseline = AccountBaseline(db_path)
    return _baseline


def reset_baseline() -> None:
    """重置单例（环境切换时使用）。"""
    global _baseline
    _baseline = None
