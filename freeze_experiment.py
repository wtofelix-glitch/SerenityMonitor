"""
OOS 冻结实验 — freeze_experiment.py

成本后面外样本 alpha 验证。冻结全部策略参数，每日记录三条成本后
净值曲线（策略 / 月频等权 / 沪深300），在预先锁定的判定标准下检验
评分引擎是否有超越被动基准的加值。

判定标准在 freeze 时写入数据库，之后不可修改。任何冻结项的修改都会
导致 OOS 曲线作废、重新开始计时。

Usage:
    python3 freeze_experiment.py freeze     # 冻结当前配置，开始实验
    python3 freeze_experiment.py record     # 记录今日三条净值
    python3 freeze_experiment.py status     # 显示进度（不含相对排名）
    python3 freeze_experiment.py judge      # 判定点评估（仅 60/120 天可用）
"""

from __future__ import annotations

import json, os, subprocess, math
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

from db import get_conn
from config import (
    ALL_CODES, STOCK_MAP, STOCK_DETAILS, TIER_4_CODES,
    CAPITAL_CONFIG, RISK_CONFIG, SIGNAL_CONFIG, get_stock_name,
)
from serenity_logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 固定判定标准 — freeze 时存入 DB，之后不可修改
# ═══════════════════════════════════════════════════════════════

MIN_TRADING_DAYS = 120           # 正式判定需要的最少交易日
CHECKPOINT_DAYS = 60             # 中期观察点（只看不判）
SHARPE_REFERENCE_DELTA = 0.3     # Sharpe 差值需 > 0.3 才认

COST_MODEL = {
    "commission_rate": 0.00025,   # 万2.5 双向
    "min_commission": 5.0,        # 最低 5 元
    "stamp_tax_rate": 0.0005,     # 千1 卖出单向 (2023.8起)
    "transfer_fee_rate": 0.00001, # 万0.1 双向 过户费
    "slippage_bps": 15,           # 15bp 单边滑点
}

EQUAL_WEIGHT_REBALANCE = {
    "frequency": "monthly",       # 每月首个交易日
    "method": "equal_weight",     # 20 只等权
    "cost_model": "same_as_strategy",
    "suspended_handling": "skip_rebalance_keep_weight",
    "note": "月频等权再平衡基准。这是比 buy-and-hold 更强的对手——"
            "月频再平衡的等权组合长期常跑赢很多主动策略。"
            "跑不输它 → alpha 成立；跑输它 → 不丢人，但评分引擎没加值。",
}

JUDGMENT_CRITERIA = {
    "version": "1.0",
    "locked_at": "",  # freeze 时填入
    "min_trading_days": MIN_TRADING_DAYS,
    "checkpoint_days": CHECKPOINT_DAYS,

    "cost_model": COST_MODEL,
    "equal_weight_config": EQUAL_WEIGHT_REBALANCE,

    "pass_criteria": {
        "all_of": [
            {
                "id": "cumulative_return_vs_equal_weight",
                "metric": "cumulative_return_net",
                "operator": "gt",
                "reference": "equal_weight",
                "description": "策略成本后累计收益 > 等权基准",
            },
            {
                "id": "sharpe_vs_equal_weight",
                "metric": "annualized_sharpe_net",
                "operator": "gt",
                "reference": "equal_weight",
                "min_delta": SHARPE_REFERENCE_DELTA,
                "description": f"策略成本后年化 Sharpe > 等权基准, 差值 > {SHARPE_REFERENCE_DELTA}",
            },
        ],
    },

    "fail_criteria": {
        "any_of": [
            {
                "id": "return_lte_equal_weight",
                "metric": "cumulative_return_net",
                "operator": "lte",
                "reference": "equal_weight",
                "verdict": "FAIL — 评分引擎在样本外没有提供超越月频等权再平衡的加值",
            },
            {
                "id": "sharpe_lte_equal_weight",
                "metric": "annualized_sharpe_net",
                "operator": "lte",
                "reference": "equal_weight",
                "verdict": "FAIL — 评分引擎的风险调整后收益不优于等权基准",
            },
            {
                "id": "return_lte_hs300",
                "metric": "cumulative_return_net",
                "operator": "lte",
                "reference": "hs300",
                "verdict": "FAIL — 策略未跑赢沪深300全收益，绝对价值存疑",
            },
        ],
    },

    "boundary_case": {
        "condition": "strategy_beats_hs300_but_not_equal_weight",
        "verdict": "NOT_PASS — 主题选择有效（pool 有 beta），但评分引擎未提供"
                   "超越月频等权再平衡的加值",
    },

    "reset_rule": (
        "修改任何冻结项（权重/阈值/标的池/因子参数/Kelly/风控参数/"
        "IC淘汰/自适应机制）→ OOS 曲线作废，从改动日重新开始 120 天计时"
    ),

    "anti_cheat": [
        "判定标准在 freeze 时写入数据库，之后不可修改",
        "每周记录'想改什么、为什么忍住、净值多少'",
        "默认只看进度不看相对排名，每周/两周一次汇总",
    ],
}


# ═══════════════════════════════════════════════════════════════
# 冻结配置快照
# ═══════════════════════════════════════════════════════════════

def capture_config_snapshot() -> dict:
    """捕获当前全部可冻结参数的完整快照。"""
    from config import (
        CAPITAL_CONFIG, RISK_CONFIG, SIGNAL_CONFIG,
        ALL_CODES, STOCK_MAP, TIER_1_CODES, TIER_2_CODES,
        TIER_3_CODES, TIER_4_CODES,
    )
    from scorer import _SCORE_WEIGHT_DEFAULTS

    # 获取 git commit
    commit = ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
        )
        if r.returncode == 0:
            commit = r.stdout.strip()
    except Exception:
        pass

    # 获取因子列表
    try:
        from factor_metadata import SIGNAL_FACTORS
        factors = list(SIGNAL_FACTORS)
    except ImportError:
        factors = []

    return {
        "freeze_date": date.today().isoformat(),
        "commit_hash": commit,
        "scoring_weights": dict(_SCORE_WEIGHT_DEFAULTS),
        "signal_thresholds": {
            "buy_threshold": SIGNAL_CONFIG["buy_threshold"],
            "strong_buy_threshold": SIGNAL_CONFIG["strong_buy_threshold"],
            "sell_threshold": SIGNAL_CONFIG["sell_threshold"],
            "hold_high": SIGNAL_CONFIG["hold_high"],
            "hold_low": SIGNAL_CONFIG["hold_low"],
            "pos_exit_threshold": SIGNAL_CONFIG["pos_exit_threshold"],
            "fourteen_factor_enabled": SIGNAL_CONFIG["fourteen_factor_enabled"],
        },
        "capital_config": {
            k: v for k, v in CAPITAL_CONFIG.items()
            if not k.startswith("_")
        },
        "risk_config": dict(RISK_CONFIG),
        "stock_pool": {
            "all_codes": list(ALL_CODES),
            "tier_1": list(TIER_1_CODES),
            "tier_2": list(TIER_2_CODES),
            "tier_3": list(TIER_3_CODES),
            "tier_4": list(TIER_4_CODES),
            "stock_map": {k: dict(v) for k, v in STOCK_MAP.items()},
        },
        "factor_list": factors,
        "kernel_frozen_modules": [
            "weight_adjuster", "signal_thresholds", "sentinel_weights",
            "conviction", "llm_sentiment", "serenity_council",
            "debate_engine", "ensemble_voting", "market_sense_regime_shifts",
        ],
        "note": "以上所有参数在实验期一字不改。任何改动 → OOS 曲线作废 + 重新计时。",
    }


# ═══════════════════════════════════════════════════════════════
# 三条净值曲线计算
# ═══════════════════════════════════════════════════════════════

def compute_equal_weight_nav(
    experiment_start: str,
    current_date: str,
    initial_capital: float,
    all_codes: list[str],
    cost_model: dict,
    prev_state: Optional[dict] = None,
) -> dict:
    """计算月频等权基准组合的当日成本后净值。

    规则：
    - 每月首个交易日再平衡回等权
    - 再平衡交易全额扣佣金+印花税+滑点(15bp)
    - 停牌票跳过再平衡，保留停牌前权重，剩余现金在可交易票中分配

    Returns:
        {nav, shares, cash, rebalanced_today, suspended_codes, daily_return}
    """
    from db import get_conn

    conn = get_conn()

    # 如果是起始日或月初首个交易日 → 再平衡
    start_dt = date.fromisoformat(experiment_start)
    cur_dt = date.fromisoformat(current_date)

    is_start = (current_date == experiment_start)
    is_first_trading_of_month = (
        not is_start
        and start_dt.month != cur_dt.month
    )
    # More precise: check if this is the first trading day of the month in our data
    if not is_start and not is_first_trading_of_month:
        # Check if earlier dates this month exist in our nav records
        nav_check = conn.execute(
            "SELECT COUNT(*) FROM oos_nav_curves WHERE date < ? AND date >= ?",
            (current_date, cur_dt.replace(day=1).isoformat())
        ).fetchone()
        if nav_check and nav_check[0] == 0:
            is_first_trading_of_month = True

    n_stocks = len(all_codes)

    # ── 获取当日收盘价 ──
    placeholders = ",".join("?" * n_stocks)
    prices = {}
    changed_pcts = {}
    suspended = set()
    try:
        rows = conn.execute(
            f"SELECT code, close, change_pct FROM daily_snapshots "
            f"WHERE code IN ({placeholders}) AND date = ?",
            (*all_codes, current_date)
        ).fetchall()

        for r in rows:
            code = r["code"]
            close = r["close"] or 0
            if close <= 0:
                suspended.add(code)
                continue
            prices[code] = close
            changed_pcts[code] = r["change_pct"] or 0
    except Exception:
        pass

    if not prices:
        conn.close()
        return {"nav": initial_capital, "shares": {}, "cash": initial_capital,
                "daily_return": 0.0, "error": "no_price_data"}

    # ── 初始化或加载前日状态 ──
    shares = {}
    cash = initial_capital

    if prev_state and prev_state.get("shares"):
        shares = dict(prev_state["shares"])
        cash = prev_state.get("cash", 0)

        # Mark-to-market: update NAV from price changes
        total_value = cash
        for code in all_codes:
            if code in shares and code in prices:
                total_value += shares[code] * prices[code]
        nav_before = total_value
    else:
        nav_before = initial_capital

    slippage_rate = cost_model["slippage_bps"] / 10000.0
    skipped: list[str] = []

    if is_start:
        # ── 起始日: 等权分配 ──
        # 小资金约束：部分标的可能买不起 1 手。买不起的标的其配资留在现金，
        # 重新分配给买得起的标的，保持可投资范围内的等权。
        active_codes = [c for c in all_codes if c not in suspended]
        if not active_codes:
            conn.close()
            return {"nav": initial_capital, "shares": {}, "cash": initial_capital,
                    "daily_return": 0.0}

        target_per = initial_capital / len(active_codes)

        # 分拣：买得起 vs 买不起
        buyable = []
        unallocated = 0.0
        for code in active_codes:
            px = prices.get(code, 0)
            if px <= 0:
                unallocated += target_per
                continue
            min_lot = px * 100
            if target_per >= min_lot:
                buyable.append(code)
            else:
                unallocated += target_per

        # 再分配：未投出资金均分给买得起的标的
        if buyable:
            extra = unallocated / len(buyable)
        else:
            extra = 0.0
        adjusted = target_per + extra

        for code in buyable:
            px = prices[code]
            raw_shares = int(adjusted / px / 100) * 100
            if raw_shares >= 100:
                cost = raw_shares * px
                commission = max(cost_model["min_commission"],
                               cost * cost_model["commission_rate"])
                slippage = cost * slippage_rate
                total_deduct = cost + commission + slippage
                if total_deduct <= cash:
                    shares[code] = raw_shares
                    cash -= total_deduct
                # else: can't afford → keep in cash, not inflated

        # 记录被跳过的标的（买不起的 + 资金不足跳过的）
        skipped = [c for c in active_codes if c not in shares]

    elif is_first_trading_of_month:
        # ── 月频再平衡 ──
        # 1. 计算当前总 NAV
        current_value = 0.0
        for code in all_codes:
            if code in shares and code in prices:
                current_value += shares[code] * prices[code]
        total_nav = cash + current_value

        # 2. 目标: 等权（停牌票保持现有权重，其余等权）
        active_codes = [c for c in all_codes if c not in suspended]
        suspended_codes = [c for c in all_codes if c in suspended]

        # 停牌票的当前价值
        suspended_value = sum(
            shares.get(c, 0) * prices.get(c, 0)
            for c in suspended_codes
        )

        target_nav_per_active = (total_nav - suspended_value) / len(active_codes) if active_codes else 0

        # 3. 模拟买卖
        slippage_rate = cost_model["slippage_bps"] / 10000.0
        for code in active_codes:
            px = prices[code]
            current_val = shares.get(code, 0) * px
            delta_val = target_nav_per_active - current_val

            if abs(delta_val) < px * 100:  # < 1 lot → skip
                continue

            delta_shares = int(abs(delta_val) / px / 100) * 100
            if delta_shares < 100:
                continue

            if delta_val > 0:
                # 买入
                buy_cost = delta_shares * px
                commission = max(cost_model["min_commission"],
                               buy_cost * cost_model["commission_rate"])
                slippage = buy_cost * slippage_rate
                total_cost = buy_cost + commission + slippage
                if total_cost <= cash:
                    shares[code] = shares.get(code, 0) + delta_shares
                    cash -= total_cost
            else:
                # 卖出
                current_shares = shares.get(code, 0)
                sell_shares = min(delta_shares, current_shares)
                if sell_shares < 100:
                    continue
                sell_value = sell_shares * px
                commission = max(cost_model["min_commission"],
                               sell_value * cost_model["commission_rate"])
                stamp_tax = sell_value * cost_model["stamp_tax_rate"]
                slippage = sell_value * slippage_rate
                net_cash = sell_value - commission - stamp_tax - slippage
                shares[code] = current_shares - sell_shares
                cash += net_cash

    # ── 计算当日 NAV ──
    nav = cash
    for code in all_codes:
        if code in shares and code in prices:
            nav += shares[code] * prices[code]

    daily_return = (nav / nav_before - 1.0) if nav_before > 0 else 0.0

    conn.close()
    return {
        "nav": round(nav, 2),
        "shares": shares,
        "cash": round(cash, 2),
        "daily_return": round(daily_return, 6),
        "rebalanced_today": is_first_trading_of_month,
        "suspended_codes": list(suspended),
        "n_stocks_invested": len(shares),
        "n_stocks_total": len(all_codes),
        "skipped_from_start": list(skipped) if is_start else [],
    }


def compute_hs300_nav(experiment_start: str, current_date: str,
                       initial_nav: float, prev_nav: float) -> dict:
    """获取沪深300全收益指数的当日净值变化。"""
    from db import get_conn
    conn = get_conn()

    try:
        row = conn.execute(
            "SELECT change_pct FROM daily_snapshots "
            "WHERE code = '000300' AND date = ?",
            (current_date,)
        ).fetchone()
        conn.close()

        if row and row["change_pct"] is not None:
            daily_ret = row["change_pct"] / 100.0
            nav = prev_nav * (1.0 + daily_ret)
            return {"nav": round(nav, 2), "daily_return": round(daily_ret, 6)}
    except Exception:
        pass

    conn.close()
    return {"nav": prev_nav, "daily_return": 0.0, "warning": "hs300_data_unavailable"}


def get_strategy_nav() -> dict:
    """获取当前策略组合的净值（成本后）。"""
    try:
        from portfolio import get_portfolio
        pm = get_portfolio()
        pv = pm.get_portfolio_value()
        return {
            "nav": round(pv["total_value"], 2),
            "cash": round(pv["cash"], 2),
            "holdings_value": round(pv["holdings_value"], 2),
            "profit_pct": pv["total_profit_pct"],
        }
    except Exception as e:
        return {"nav": CAPITAL_CONFIG["initial_capital"], "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 实验管理
# ═══════════════════════════════════════════════════════════════

class FreezeExperiment:
    """OOS 冻结实验管理器。"""

    def __init__(self):
        db = __import__('db')
        db.init_db()

    # ── Freeze ────────────────────────────────────────────

    def freeze(self) -> dict:
        """冻结当前配置，开始 OOS 实验。

        检查是否已有活跃实验，有则报错（一个时间段只能有一个实验）。
        """
        conn = get_conn()
        try:
            existing = conn.execute(
                "SELECT id, started_at FROM oos_experiments WHERE status = 'active'"
            ).fetchone()
            if existing:
                conn.close()
                return {
                    "success": False,
                    "error": f"已有活跃实验 (id={existing['id']}, started={existing['started_at']})。"
                             f"请先完成或中止当前实验后再开启新的。"
                }
        finally:
            conn.close()

        snapshot = capture_config_snapshot()
        criteria = dict(JUDGMENT_CRITERIA)
        criteria["locked_at"] = date.today().isoformat()

        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO oos_experiments (name, started_at, commit_hash, "
                "config_snapshot, judgment_criteria, status) VALUES (?, ?, ?, ?, ?, 'active')",
                ("oos-freeze", date.today().isoformat(), snapshot["commit_hash"],
                 json.dumps(snapshot, ensure_ascii=False, indent=2),
                 json.dumps(criteria, ensure_ascii=False, indent=2))
            )
            conn.commit()
            exp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

        # git tag
        tag_name = f"freeze-oos-{date.today().isoformat()}"
        try:
            subprocess.run(
                ["git", "tag", tag_name, snapshot["commit_hash"]],
                capture_output=True, check=True,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
        except Exception as e:
            log.warning(f"git tag 创建失败: {e}")

        log.warning(f"🔒 OOS 冻结实验已启动: id={exp_id} tag={tag_name}")
        log.warning(f"   判定标准已锁入 DB, {JUDGMENT_CRITERIA['min_trading_days']} 交易日后可判定")
        log.warning(f"   任何冻结项改动 → OOS 曲线作废 + 重新计时")

        return {
            "success": True,
            "experiment_id": exp_id,
            "tag": tag_name,
            "commit": snapshot["commit_hash"][:8],
            "started_at": date.today().isoformat(),
            "min_trading_days": MIN_TRADING_DAYS,
            "judgment_date_estimate": (
                date.today() + timedelta(days=MIN_TRADING_DAYS * 7 // 5 + 10)
            ).isoformat(),
        }

    # ── Daily Record ───────────────────────────────────────

    def record(self, experiment_id: Optional[int] = None,
               as_of: Optional[str] = None) -> dict:
        """记录今日三条净值曲线。

        在 daily_workflow 收盘后调用。
        """
        today = as_of or date.today().isoformat()

        # 获取活跃实验
        conn = get_conn()
        try:
            if experiment_id:
                exp = conn.execute(
                    "SELECT * FROM oos_experiments WHERE id = ? AND status = 'active'",
                    (experiment_id,)
                ).fetchone()
            else:
                exp = conn.execute(
                    "SELECT * FROM oos_experiments WHERE status = 'active' "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()

            if not exp:
                conn.close()
                return {"success": False, "error": "没有活跃的 OOS 实验。请先 python3 freeze_experiment.py freeze"}

            exp_id = exp["id"]
            started = exp["started_at"]
            config = json.loads(exp["config_snapshot"])
            criteria = json.loads(exp["judgment_criteria"])
            cost_model = criteria["cost_model"]
            all_codes = config["stock_pool"]["all_codes"]
            initial_capital = config["capital_config"]["initial_capital"]

            # 检查是否已记录
            dup = conn.execute(
                "SELECT id FROM oos_nav_curves WHERE experiment_id=? AND date=?",
                (exp_id, today)
            ).fetchone()
            if dup:
                conn.close()
                return {"success": True, "skipped": True, "reason": f"{today} 已记录"}

            # ── 三条净值 ──

            # 1. 策略净值
            strat = get_strategy_nav()

            # 2. 等权基准净值
            prev_row = conn.execute(
                "SELECT * FROM oos_nav_curves WHERE experiment_id=? "
                "ORDER BY date DESC LIMIT 1",
                (exp_id,)
            ).fetchone()

            prev_ew_state = None
            if prev_row:
                prev_detail = json.loads(prev_row["details_json"])
                prev_ew_state = prev_detail.get("equal_weight_state")
                prev_strat_nav = prev_row["strategy_nav"]
                prev_ew_nav = prev_row["equal_weight_nav"]
                prev_hs300_nav = prev_row["hs300_nav"] or initial_capital
            else:
                prev_strat_nav = initial_capital
                prev_ew_nav = initial_capital
                prev_hs300_nav = initial_capital

            ew_result = compute_equal_weight_nav(
                experiment_start=started,
                current_date=today,
                initial_capital=initial_capital,
                all_codes=all_codes,
                cost_model=cost_model,
                prev_state=prev_ew_state,
            )

            # 3. 沪深300净值
            hs300 = compute_hs300_nav(
                experiment_start=started,
                current_date=today,
                initial_nav=initial_capital,
                prev_nav=prev_hs300_nav,
            )

            # ── 写库 ──
            strat_nav = strat.get("nav", initial_capital)
            ew_nav = ew_result["nav"]
            hs300_nav = hs300["nav"]

            strat_ret = (strat_nav / prev_strat_nav - 1.0) if prev_strat_nav > 0 else 0.0
            ew_ret = ew_result["daily_return"]

            # 最大回撤
            prev_peaks = conn.execute(
                "SELECT MAX(strategy_nav) as sp, MAX(equal_weight_nav) as ep "
                "FROM oos_nav_curves WHERE experiment_id=?",
                (exp_id,)
            ).fetchone()

            strat_peak = max(prev_peaks["sp"] or strat_nav, strat_nav)
            ew_peak = max(prev_peaks["ep"] or ew_nav, ew_nav)
            strat_dd = (strat_nav - strat_peak) / strat_peak if strat_peak > 0 else 0.0
            ew_dd = (ew_nav - ew_peak) / ew_peak if ew_peak > 0 else 0.0

            conn.execute(
                "INSERT INTO oos_nav_curves "
                "(experiment_id, date, strategy_nav, equal_weight_nav, hs300_nav, "
                " strategy_return_daily, equal_weight_return_daily, hs300_return_daily, "
                " strategy_drawdown, equal_weight_drawdown, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (exp_id, today,
                 round(strat_nav, 2), round(ew_nav, 2), round(hs300_nav, 2),
                 round(strat_ret, 6), round(ew_ret, 6), round(hs300["daily_return"], 6),
                 round(strat_dd, 6), round(ew_dd, 6),
                 json.dumps({
                     "strategy_cash": strat.get("cash"),
                     "equal_weight_state": {
                         "shares": ew_result.get("shares"),
                         "cash": ew_result.get("cash"),
                     },
                     "equal_weight_rebalanced": ew_result.get("rebalanced_today"),
                     "equal_weight_suspended": ew_result.get("suspended_codes", []),
                     "hs300_warning": hs300.get("warning"),
                 }, ensure_ascii=False))
            )
            conn.commit()

            # 计数
            day_count = conn.execute(
                "SELECT COUNT(*) FROM oos_nav_curves WHERE experiment_id=?",
                (exp_id,)
            ).fetchone()[0]

        finally:
            conn.close()

        return {
            "success": True,
            "experiment_id": exp_id,
            "date": today,
            "day": day_count,
            "strategy_nav": round(strat_nav, 2),
            "equal_weight_nav": round(ew_nav, 2),
            "hs300_nav": round(hs300_nav, 2),
            "equal_weight_rebalanced": ew_result.get("rebalanced_today", False),
        }

    # ── Status ─────────────────────────────────────────────

    def status(self) -> dict:
        """显示实验进度。默认不显示谁输谁赢，只显示进度。"""
        conn = get_conn()
        try:
            exp = conn.execute(
                "SELECT * FROM oos_experiments WHERE status = 'active' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

            if not exp:
                conn.close()
                return {"active": False, "message": "没有活跃的 OOS 实验"}

            exp_id = exp["id"]
            criteria = json.loads(exp["judgment_criteria"])
            min_days = criteria["min_trading_days"]
            checkpoint_days = criteria["checkpoint_days"]

            day_count = conn.execute(
                "SELECT COUNT(*) FROM oos_nav_curves WHERE experiment_id=?",
                (exp_id,)
            ).fetchone()[0]

            latest = conn.execute(
                "SELECT * FROM oos_nav_curves WHERE experiment_id=? "
                "ORDER BY date DESC LIMIT 1",
                (exp_id,)
            ).fetchone()

        finally:
            conn.close()

        result = {
            "active": True,
            "experiment_id": exp_id,
            "started_at": exp["started_at"],
            "commit_hash": exp["commit_hash"][:8],
            "status": exp["status"],
            "trading_days": day_count,
            "min_required": min_days,
            "progress_pct": round(day_count / min_days * 100, 1),
            "next_checkpoint": checkpoint_days,
            "next_judgment": min_days,
            "estimated_judgment_date": (
                date.fromisoformat(exp["started_at"]) + timedelta(days=min_days * 7 // 5 + 10)
            ).isoformat(),
        }

        if latest:
            result["latest_date"] = latest["date"]
            # 不展示相对排名，只展示最新净值
            result["latest_strategy_nav"] = latest["strategy_nav"]

        return result

    # ── Judge ──────────────────────────────────────────────

    def judge(self, force: bool = False) -> dict:
        """在判定点评估实验结果。

        仅在 60 天 checkpoint 或 120 天判定点可用。
        force=True 跳过天数检查（仅用于测试）。
        """
        conn = get_conn()
        try:
            exp = conn.execute(
                "SELECT * FROM oos_experiments WHERE status = 'active' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

            if not exp:
                conn.close()
                return {"error": "没有活跃的 OOS 实验"}

            exp_id = exp["id"]
            criteria = json.loads(exp["judgment_criteria"])
            min_days = criteria["min_trading_days"]
            checkpoint_days = criteria["checkpoint_days"]

            rows = conn.execute(
                "SELECT * FROM oos_nav_curves WHERE experiment_id=? ORDER BY date",
                (exp_id,)
            ).fetchall()

        finally:
            conn.close()

        n_days = len(rows)
        if n_days == 0:
            return {"error": "尚无数据", "trading_days": 0}

        if not force and n_days < checkpoint_days:
            return {
                "ready": False,
                "trading_days": n_days,
                "next_checkpoint": checkpoint_days,
                "message": f"数据不足，最早 {checkpoint_days} 天 checkpoint。当前 {n_days} 天。"
            }

        is_checkpoint = (not force and n_days < min_days)
        is_judgment = (force or n_days >= min_days)

        # ── 计算绩效 ──
        first = rows[0]
        last = rows[-1]

        strategy_ret = (last["strategy_nav"] / first["strategy_nav"] - 1.0)
        ew_ret = (last["equal_weight_nav"] / first["equal_weight_nav"] - 1.0)
        hs300_ret = ((last["hs300_nav"] or first["strategy_nav"]) / first["strategy_nav"] - 1.0)

        # Sharpe (daily returns annualized)
        risk_free_daily = 0.02 / 252
        def _sharpe(daily_rets: list[float]) -> float:
            if len(daily_rets) < 10:
                return 0.0
            mean = sum(daily_rets) / len(daily_rets)
            var = sum((r - mean) ** 2 for r in daily_rets) / (len(daily_rets) - 1)
            std = math.sqrt(var) if var > 0 else 1e-6
            return (mean - risk_free_daily) / std * math.sqrt(252)

        strategy_rets = [r["strategy_return_daily"] for r in rows if r["strategy_return_daily"] is not None]
        ew_rets = [r["equal_weight_return_daily"] for r in rows if r["equal_weight_return_daily"] is not None]

        strategy_sharpe = _sharpe(strategy_rets)
        ew_sharpe = _sharpe(ew_rets)

        # 最大回撤
        strat_dd = min(r["strategy_drawdown"] for r in rows) if rows else 0
        ew_dd = min(r["equal_weight_drawdown"] for r in rows) if rows else 0

        # ── 判定 ──
        pass_checks = []
        fail_checks = []

        # 通过条件
        pass_criteria = criteria.get("pass_criteria", {}).get("all_of", [])
        for pc in pass_criteria:
            if pc["id"] == "cumulative_return_vs_equal_weight":
                met = strategy_ret > ew_ret
                pass_checks.append({
                    "id": pc["id"],
                    "met": met,
                    "detail": f"策略 {strategy_ret:+.2%} vs 等权 {ew_ret:+.2%}",
                })
            elif pc["id"] == "sharpe_vs_equal_weight":
                delta = strategy_sharpe - ew_sharpe
                met = delta > SHARPE_REFERENCE_DELTA
                pass_checks.append({
                    "id": pc["id"],
                    "met": met,
                    "detail": f"策略 Sharpe {strategy_sharpe:.2f} vs 等权 {ew_sharpe:.2f}, Δ={delta:+.2f}",
                })

        # 失败条件
        fail_criteria = criteria.get("fail_criteria", {}).get("any_of", [])
        for fc in fail_criteria:
            if fc["id"] == "return_lte_equal_weight":
                trig = strategy_ret <= ew_ret
                fail_checks.append({
                    "id": fc["id"],
                    "triggered": trig,
                    "detail": fc["verdict"] if trig else "OK",
                })
            elif fc["id"] == "sharpe_lte_equal_weight":
                trig = strategy_sharpe <= ew_sharpe
                fail_checks.append({
                    "id": fc["id"],
                    "triggered": trig,
                    "detail": fc["verdict"] if trig else "OK",
                })
            elif fc["id"] == "return_lte_hs300":
                trig = strategy_ret <= hs300_ret
                fail_checks.append({
                    "id": fc["id"],
                    "triggered": trig,
                    "detail": fc["verdict"] if trig else "OK",
                })

        all_pass_met = all(c["met"] for c in pass_checks)
        any_fail_triggered = any(c["triggered"] for c in fail_checks)

        # 边界情况
        boundary = strategy_ret > hs300_ret and strategy_ret <= ew_ret

        if is_checkpoint:
            verdict = "CHECKPOINT_ONLY — 仅观察，不做正式判定"
        elif any_fail_triggered:
            verdict = "NOT_PASS"
        elif all_pass_met:
            verdict = "PASS"
        elif boundary:
            verdict = criteria.get("boundary_case", {}).get("verdict", "BOUNDARY")
        else:
            verdict = "INCONCLUSIVE"

        return {
            "ready": True,
            "is_checkpoint": is_checkpoint,
            "is_judgment": is_judgment,
            "trading_days": n_days,
            "metrics": {
                "strategy": {
                    "cumulative_return": round(strategy_ret, 4),
                    "annualized_sharpe": round(strategy_sharpe, 2),
                    "max_drawdown": round(strat_dd, 4),
                    "start_nav": round(rows[0]["strategy_nav"], 2),
                    "end_nav": round(rows[-1]["strategy_nav"], 2),
                },
                "equal_weight": {
                    "cumulative_return": round(ew_ret, 4),
                    "annualized_sharpe": round(ew_sharpe, 2),
                    "max_drawdown": round(ew_dd, 4),
                    "start_nav": round(rows[0]["equal_weight_nav"], 2),
                    "end_nav": round(rows[-1]["equal_weight_nav"], 2),
                },
                "hs300": {
                    "cumulative_return": round(hs300_ret, 4),
                    "start_nav": round(rows[0]["hs300_nav"] or rows[0]["strategy_nav"], 2),
                    "end_nav": round(rows[-1]["hs300_nav"] or rows[-1]["strategy_nav"], 2),
                },
            },
            "pass_checks": pass_checks,
            "fail_checks": fail_checks,
            "boundary_triggered": boundary,
            "verdict": verdict,
        }

    # ── Weekly log ─────────────────────────────────────────

    def weekly_log(self, note: str) -> dict:
        """记录每周的"想改什么、为什么忍住"笔记。"""
        conn = get_conn()
        try:
            exp = conn.execute(
                "SELECT id FROM oos_experiments WHERE status = 'active' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if not exp:
                conn.close()
                return {"error": "没有活跃实验"}

            # Append to details_json in most recent nav record
            latest = conn.execute(
                "SELECT id, details_json FROM oos_nav_curves "
                "WHERE experiment_id=? ORDER BY date DESC LIMIT 1",
                (exp["id"],)
            ).fetchone()

            if latest:
                detail = json.loads(latest["details_json"])
                detail.setdefault("weekly_logs", []).append({
                    "date": date.today().isoformat(),
                    "note": note,
                })
                conn.execute(
                    "UPDATE oos_nav_curves SET details_json=? WHERE id=?",
                    (json.dumps(detail, ensure_ascii=False), latest["id"])
                )
                conn.commit()
        finally:
            conn.close()

        return {"success": True, "note": note}

    # ── Abort ──────────────────────────────────────────────

    def abort(self, reason: str = "") -> dict:
        """中止当前实验。"""
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE oos_experiments SET status='aborted', completed_at=?, "
                "verdict=? WHERE status='active'",
                (date.today().isoformat(), f"ABORTED: {reason}")
            )
            conn.commit()
            updated = conn.total_changes
        finally:
            conn.close()

        return {"success": updated > 0, "aborted_experiments": updated}


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    import sys

    exp = FreezeExperiment()

    if len(sys.argv) < 2:
        print("Usage: python3 freeze_experiment.py {freeze|record|status|judge|weekly-log|abort}")
        return

    cmd = sys.argv[1]

    if cmd == "freeze":
        result = exp.freeze()
        if result["success"]:
            print(f"\n🔒 OOS 冻结实验已启动!")
            print(f"   ID: {result['experiment_id']}")
            print(f"   Git tag: {result['tag']}")
            print(f"   Commit: {result['commit']}")
            print(f"   启动日期: {result['started_at']}")
            print(f"   最少交易日: {result['min_trading_days']}")
            print(f"   预计判定日期: {result['judgment_date_estimate']}")
            print(f"\n   所有参数已锁。任何改动 → 重新计时。")
        else:
            print(f"❌ {result['error']}")

    elif cmd == "record":
        result = exp.record()
        if result.get("success"):
            if result.get("skipped"):
                print(f"⏭  {result['date']} 已记录")
            else:
                print(f"📊 Day {result['day']} 已记录")
                print(f"   策略: ¥{result['strategy_nav']:,.0f}")
                print(f"   等权: ¥{result['equal_weight_nav']:,.0f}")
                print(f"   HS300: ¥{result['hs300_nav']:,.0f}")
                if result.get("equal_weight_rebalanced"):
                    print(f"   🔄 等权基准本月再平衡")
        else:
            print(f"⚠️  {result.get('error', 'record failed')}")

    elif cmd == "status":
        s = exp.status()
        if s.get("active"):
            print(f"\n🔬 OOS 实验 #{s['experiment_id']}")
            print(f"   启动: {s['started_at']}")
            print(f"   交易日: {s['trading_days']}/{s['min_required']} ({s['progress_pct']:.0f}%)")
            print(f"   下次 checkpoint: {s['next_checkpoint']} 天")
            print(f"   正式判定: {s['next_judgment']} 天 (约 {s['estimated_judgment_date']})")
            if s.get("latest_date"):
                print(f"   最新记录: {s['latest_date']}")
            if s["trading_days"] >= s["next_checkpoint"]:
                print(f"   💡 已到 checkpoint，可运行 python3 freeze_experiment.py judge")
        else:
            print(f"   {s.get('message', '无活跃实验')}")

    elif cmd == "judge":
        force = "--force" in sys.argv
        result = exp.judge(force=force)
        if result.get("error"):
            print(f"   {result['error']}")
        elif not result.get("ready"):
            print(f"   ⏳ {result.get('message', '数据不足')}")
        else:
            print(f"\n{'═' * 56}")
            print(f"  OOS 判定报告 — {result['trading_days']} 交易日")
            print(f"{'═' * 56}")
            m = result["metrics"]
            print(f"\n  策略:   收益 {m['strategy']['cumulative_return']:+.2%}  "
                  f"Sharpe {m['strategy']['annualized_sharpe']:.2f}  "
                  f"MaxDD {m['strategy']['max_drawdown']:.1%}")
            print(f"  等权:   收益 {m['equal_weight']['cumulative_return']:+.2%}  "
                  f"Sharpe {m['equal_weight']['annualized_sharpe']:.2f}  "
                  f"MaxDD {m['equal_weight']['max_drawdown']:.1%}")
            print(f"  沪深300: 收益 {m['hs300']['cumulative_return']:+.2%}")

            if result.get("is_checkpoint"):
                print(f"\n  ⚠️ 仅为 checkpoint 观察，非正式判定。")

            print(f"\n  判定: {result['verdict']}")

            for c in result.get("pass_checks", []):
                icon = "✅" if c["met"] else "❌"
                print(f"  {icon} {c['detail']}")
            for c in result.get("fail_checks", []):
                icon = "⚠️" if c["triggered"] else "✅"
                print(f"  {icon} {c['detail']}")

    elif cmd == "weekly-log":
        note = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not note:
            note = input("这周想改什么、为什么忍住了？ ")
        result = exp.weekly_log(note)
        print(f"✅ 已记录" if result.get("success") else f"❌ {result.get('error')}")

    elif cmd == "abort":
        reason = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        result = exp.abort(reason)
        print(f"🛑 实验已中止" if result["success"] else "无活跃实验可中止")

    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()

def main_after(args: list[str]) -> None:
    """CLI 入口 — 允许通过 import 调用。"""
    import sys
    _orig = list(sys.argv)
    sys.argv = ["freeze_experiment.py"] + args
    try:
        main()
    finally:
        sys.argv = _orig
