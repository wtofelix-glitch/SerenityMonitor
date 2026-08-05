"""
OOS 冻结实验 — freeze_experiment.py

成本后面外样本 alpha 验证。冻结全部策略参数，每日记录三条成本后
净值曲线（策略 / 日频等权 / 沪深300），在预先锁定的判定标准下检验
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

import json, os, subprocess, math, hashlib
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
    "frequency": "daily",           # 每日算术平均 (等价日频再平衡)
    "method": "equal_weight",       # 20 只等权 change_pct 的简单平均 → 叠乘
    "cost_model": "zero",           # 理论基准线, 不扣再平衡成本
    "suspended_handling": "treat_as_zero_return",
    "note": "日频等权再平衡基准 — 比月频更强的对手。"
            "每日取 20 只标的 change_pct 的简单平均后叠乘,"
            "等价于每天收盘后拉回等权、且不扣任何交易成本。"
            "这是故意设高的及格线: 策略不但要跑赢闭眼平均分配,"
            "还要覆盖自己的真实交易摩擦。"
            "跑不输它 → alpha 成立; 跑输它 → 不丢人, 但评分引擎没加值。",
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
                "verdict": "FAIL — 评分引擎在样本外没有提供超越日频等权基准的加值",
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
                   "超越日频等权基准的加值",
    },

    "reset_rule": (
        "修改任何冻结项（权重/阈值/标的池/因子参数/Kelly/风控参数/"
        "IC淘汰/自适应机制）→ OOS 曲线作废，从改动日重新开始 120 天计时"
    ),

    "anti_cheat": [
        "判定标准在 freeze 时写入数据库，之后不可修改",
        "criteria_hash (SHA-256) 在 freeze 时计算并存库，每次 judge 校验",
        "每周记录'想改什么、为什么忍住、净值多少'",
        "默认只看进度不看相对排名，每周/两周一次汇总",
    ],
}
"""JUDGMENT_CRITERIA dict ends here."""


def compute_criteria_hash(criteria: dict) -> str:
    """计算判定标准的 SHA-256 哈希。"""
    serialized = json.dumps(criteria, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════
# 除权除息事件日志 (B-7)
# ═══════════════════════════════════════════════════════════════

_DIVIDEND_CACHE: dict = {"date": "", "events": {}}

def _fetch_dividend_events(today: str) -> dict[str, dict]:
    """获取当日发生的除权除息事件。

    从 akshare stock_history_dividend_detail 获取每只标的的分红历史，
    筛选除权除息日 == today 且进度='实施' 的事件。

    Returns:
        {code: {cash_dividend_per_share, bonus_ratio, rights_ratio}}
    """
    global _DIVIDEND_CACHE
    if _DIVIDEND_CACHE["date"] == today:
        return _DIVIDEND_CACHE["events"]

    try:
        import akshare as ak
        from config import ALL_CODES
    except ImportError:
        return {}

    today_dt = date.fromisoformat(today)
    events = {}

    for code in ALL_CODES:
        try:
            df = ak.stock_history_dividend_detail(symbol=code)
            for _, row in df.iterrows():
                ex_date = row.get("除权除息日")
                if ex_date is None or str(ex_date) == "NaT":
                    continue
                ex_str = (ex_date.isoformat() if hasattr(ex_date, 'isoformat')
                          else str(ex_date)[:10])
                if ex_str != today:
                    continue
                if str(row.get("进度", "")) != "实施":
                    continue

                # 派息: 每10股派X元 → 每股 = X/10
                cash_per_share = float(row.get("派息", 0) or 0) / 10.0
                # 送股+转增: 每10股送X股 → 每股送 X/10
                bonus_raw = float(row.get("送股", 0) or 0) + float(row.get("转增", 0) or 0)
                bonus_ratio = bonus_raw / 10.0

                events[code] = {
                    "cash_dividend_per_share": cash_per_share,
                    "bonus_ratio": bonus_ratio,
                    "rights_ratio": 0.0,
                }
                log.info(f"除息事件: {code} 派{cash_per_share:.2f}元/股"
                         f"{' 送'+str(bonus_ratio) if bonus_ratio > 0 else ''}")
        except Exception:
            pass

    _DIVIDEND_CACHE = {"date": today, "events": events}
    return events


def cross_check_dividend_anomaly(all_codes: list[str], today: str) -> list[dict]:
    """交叉验证: 检测股价异常跳空但无除息记录的情况。

    对当日跌幅 > 8% 的非跌停标的，检查是否有除息事件。
    如果无记录 → 可能是数据源漏报除息，告警。
    """
    from db import get_conn
    anomalies = []
    conn = get_conn()
    try:
        placeholders = ",".join("?" * len(all_codes))
        rows = conn.execute(
            f"SELECT code, change_pct FROM daily_snapshots "
            f"WHERE code IN ({placeholders}) AND date = ?",
            (*all_codes, today)
        ).fetchall()

        dividend_events = _fetch_dividend_events(today)

        for r in rows:
            chg = r["change_pct"] or 0
            if chg <= -8.0 and chg > -10.0:
                if r["code"] not in dividend_events:
                    anomalies.append({
                        "code": r["code"],
                        "change_pct": chg,
                        "warning": (f"{r['code']} 今日跌幅 {chg:.1f}%，"
                                   f"无除息记录 — 可能是漏报除息事件"),
                    })
    finally:
        conn.close()
    return anomalies


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
# 三条归一化收益率曲线 — 全部从 1.0 启动
# ═══════════════════════════════════════════════════════════════
#
# 设计原则 (方案二 — 归一化到相同起点)：
#   三条曲线从同一天、同一数值 1.0 出发，之后只记录乘积累积收益率。
#   等权基准使用理论分数股，不受 A 股 100 股整手限制 — 基准是参照物，
#   不是可交易方案。与沪深 300 线的哲学一致（指数不可直接交易，但仍然
#   是最诚实的外部参照）。
#
#   日频等权基准：每日取 20 只标的 change_pct 的算术平均后叠乘，等价于
#   每天收盘后拉回等权。这是比月频再平衡更强的对手 — 故意设高的及格线，
#   策略不但要跑赢闭眼平均分配，还要覆盖自己的真实交易摩擦（佣金+印花税
#   +滑点）。基准零成本、策略有成本，不对称但有意为之：这是更干净的对照。


def compute_equal_weight_daily_return(all_codes: list[str],
                                       current_date: str) -> dict:
    """计算当日 20 只标的的等权平均日收益率。

    纯理论：所有标的不论价格高低、能否买得起 1 手，权重相等。
    停牌票按 0% 收益计入（权重保留，不对剩余票做补偿调整）。

    Returns:
        {daily_return, n_stocks, suspended_codes}
    """
    from db import get_conn

    conn = get_conn()
    placeholders = ",".join("?" * len(all_codes))
    try:
        rows = conn.execute(
            f"SELECT code, change_pct FROM daily_snapshots "
            f"WHERE code IN ({placeholders}) AND date = ?",
            (*all_codes, current_date)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"daily_return": 0.0, "n_stocks": 0, "suspended_codes": []}

    returns = []
    suspended = []
    for r in rows:
        chg = r["change_pct"]
        if chg is None:
            suspended.append(r["code"])
            returns.append(0.0)
        else:
            returns.append(chg / 100.0)

    avg = sum(returns) / len(returns) if returns else 0.0
    return {
        "daily_return": round(avg, 8),
        "n_stocks": len(returns),
        "suspended_codes": suspended,
    }


def compute_hs300_daily_return(current_date: str) -> dict:
    """获取沪深 300 全收益指数的当日涨跌幅。"""
    from db import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT change_pct FROM daily_snapshots "
            "WHERE code = '000300' AND date = ?",
            (current_date,)
        ).fetchone()
        if row and row["change_pct"] is not None:
            return {"daily_return": round(row["change_pct"] / 100.0, 8)}
    finally:
        conn.close()
    return {"daily_return": 0.0, "warning": "hs300_data_unavailable"}


def get_strategy_daily_return(prev_nav: float) -> dict:
    """获取当日策略组合相对于前一记录日的收益率。

    使用 portfolio.get_portfolio_value() 的当前净值，
    除以 prev_nav 计算日收益率。成本已在 portfolio 中扣除。
    """
    try:
        from portfolio import get_portfolio
        pm = get_portfolio()
        pv = pm.get_portfolio_value()
        current_nav = pv["total_value"]
        if prev_nav > 0:
            daily_ret = current_nav / prev_nav - 1.0
        else:
            daily_ret = 0.0
        return {
            "daily_return": round(daily_ret, 8),
            "nav": round(current_nav, 2),
            "cash": round(pv["cash"], 2),
            "holdings_value": round(pv["holdings_value"], 2),
            "profit_pct": pv["total_profit_pct"],
        }
    except Exception as e:
        return {"daily_return": 0.0, "nav": prev_nav, "error": str(e)}



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
            h = compute_criteria_hash(criteria)
            conn.execute(
                "INSERT INTO oos_experiments (name, started_at, commit_hash, "
                "config_snapshot, judgment_criteria, criteria_hash, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                ("oos-freeze", date.today().isoformat(), snapshot["commit_hash"],
                 json.dumps(snapshot, ensure_ascii=False, indent=2),
                 json.dumps(criteria, ensure_ascii=False, indent=2),
                 h)
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
        """记录今日三条曲线。

        在 daily_workflow 收盘后调用。

        Day 1：锚定基线。三条线全部 = 1.0000，日收益 = 0。Day 1 的
        实际净值/价格仅作为后续计算的基准锚点，不产生任何涨跌——因为
        "从冻结到第一次记录"之间没有样本外时间流逝。

        Day 2+：每条线记录"今日相对于前一记录日的变动"，叠乘到累计乘数上。
        """
        today = as_of or date.today().isoformat()

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
            config = json.loads(exp["config_snapshot"])
            all_codes = config["stock_pool"]["all_codes"]

            dup = conn.execute(
                "SELECT id FROM oos_nav_curves WHERE experiment_id=? AND date=?",
                (exp_id, today)
            ).fetchone()
            if dup:
                conn.close()
                return {"success": True, "skipped": True, "reason": f"{today} 已记录"}

            # ── 读取前日记录 (用于 Day 2+ 的基准对比) ──
            prev_row = conn.execute(
                "SELECT * FROM oos_nav_curves WHERE experiment_id=? "
                "ORDER BY date DESC LIMIT 1",
                (exp_id,)
            ).fetchone()

            is_day1 = (prev_row is None)

            if is_day1:
                # ═══════════════════════════════════════════
                # Day 1 — 锚定基线。三线全部 = 1.0000。
                # 冻结实验的诚实性取决于"线之前"的旧账不污染
                # "线之后"的样本外记录。Day 1 是起跑线，起跑线
                # 上没有样本外收益可言——第一个真实样本外数据
                # 点在 Day 2。
                # ═══════════════════════════════════════════

                # 获取策略当前真实净值 (存为锚点, 不进曲线)
                from portfolio import get_portfolio
                pm = get_portfolio()
                pv = pm.get_portfolio_value()
                anchor_strat_nav = pv["total_value"]

                conn.execute(
                    "INSERT INTO oos_nav_curves "
                    "(experiment_id, date, strategy_nav, equal_weight_nav, hs300_nav, "
                    " strategy_return_daily, equal_weight_return_daily, hs300_return_daily, "
                    " strategy_drawdown, equal_weight_drawdown, details_json) "
                    "VALUES (?, ?, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?)",
                    (exp_id, today,
                     json.dumps({
                         "is_anchor_day": True,
                         "anchor_strategy_nav": round(anchor_strat_nav, 2),
                         "anchor_strategy_cash": round(pv["cash"], 2),
                         "anchor_note": "Day 1 = 起跑线。三条线全部 = 1.0000。"
                                        "第一个样本外数据点在 Day 2。"
                                        "策略线基准锚点为今日实际净值(¥{:.0f})；"
                                        "实验启动前的历史回撤不计入OOS。"
                                        "等权和沪深300的第一日变动(相对于 Day 0)同属实验"
                                        "之前, 同样清零。".format(anchor_strat_nav),
                     }, ensure_ascii=False))
                )
                conn.commit()
                day_count = 1

                conn.close()
                return {
                    "success": True,
                    "experiment_id": exp_id,
                    "date": today,
                    "day": day_count,
                    "is_anchor": True,
                    "strategy_anchor_nav": round(anchor_strat_nav, 2),
                    "strategy_mult": 1.0,
                    "equal_weight_mult": 1.0,
                    "hs300_mult": 1.0,
                }

            # ═════════════════════════════════════════════
            # Day 2+ — 正常记录
            # ═════════════════════════════════════════════

            # ── 除权除息检查 (B-7) ──
            dividend_events = _fetch_dividend_events(today)
            dividend_anomalies = cross_check_dividend_anomaly(all_codes, today)

            prev_detail = json.loads(prev_row["details_json"])
            prev_strat_real_nav = prev_detail.get("anchor_strategy_nav", 0)

            # 如果前一条也是非锚点, 从前一条详情中取前一次真实净值
            if not prev_detail.get("is_anchor_day"):
                prev_strat_real_nav = prev_detail.get("strategy_real_nav", prev_strat_real_nav)

            prev_strat_mult = prev_row["strategy_nav"]
            prev_ew_mult = prev_row["equal_weight_nav"]
            prev_hs300_mult = prev_row["hs300_nav"] or 1.0

            # 1. 策略日收益率 (基于真实净值变动)
            strat = get_strategy_daily_return(prev_strat_real_nav)
            strat_ret = strat["daily_return"]
            strat_mult = prev_strat_mult * (1.0 + strat_ret)

            # 2. 等权基准日收益率 (20 只理论等权, 分数股)
            ew = compute_equal_weight_daily_return(all_codes, today)
            ew_ret = ew["daily_return"]
            ew_mult = prev_ew_mult * (1.0 + ew_ret)

            # 3. 沪深 300 日收益率
            hs300 = compute_hs300_daily_return(today)
            hs300_ret = hs300["daily_return"]
            hs300_mult = prev_hs300_mult * (1.0 + hs300_ret)

            # ── 最大回撤 ──
            prev_peaks = conn.execute(
                "SELECT MAX(strategy_nav) as sp, MAX(equal_weight_nav) as ep "
                "FROM oos_nav_curves WHERE experiment_id=?",
                (exp_id,)
            ).fetchone()

            strat_peak = max(prev_peaks["sp"] or strat_mult, strat_mult)
            ew_peak = max(prev_peaks["ep"] or ew_mult, ew_mult)
            strat_dd = (strat_mult - strat_peak) / strat_peak if strat_peak > 0 else 0.0
            ew_dd = (ew_mult - ew_peak) / ew_peak if ew_peak > 0 else 0.0

            conn.execute(
                "INSERT INTO oos_nav_curves "
                "(experiment_id, date, strategy_nav, equal_weight_nav, hs300_nav, "
                " strategy_return_daily, equal_weight_return_daily, hs300_return_daily, "
                " strategy_drawdown, equal_weight_drawdown, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (exp_id, today,
                 round(strat_mult, 8), round(ew_mult, 8), round(hs300_mult, 8),
                 round(strat_ret, 8), round(ew_ret, 8), round(hs300_ret, 8),
                 round(strat_dd, 8), round(ew_dd, 8),
                 json.dumps({
                     "strategy_real_nav": strat.get("nav"),
                     "strategy_cash": strat.get("cash"),
                     "anchor_strategy_nav": prev_detail.get("anchor_strategy_nav"),
                     "equal_weight_n_stocks": ew["n_stocks"],
                     "equal_weight_suspended": ew.get("suspended_codes", []),
                     "hs300_warning": hs300.get("warning"),
                     "dividend_events": dividend_events,
                     "dividend_anomalies": dividend_anomalies,
                 }, ensure_ascii=False))
            )
            conn.commit()

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
            "strategy_mult": round(strat_mult, 4),
            "equal_weight_mult": round(ew_mult, 4),
            "hs300_mult": round(hs300_mult, 4),
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
            # 乘数 (1.0 = 起始), 不展示排名
            result["latest_strategy_compound"] = round(latest["strategy_nav"], 4)
            result["latest_equal_weight_compound"] = round(latest["equal_weight_nav"], 4)
            result["latest_hs300_compound"] = round(latest["hs300_nav"] or 1.0, 4)

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

            # ── D-12: 判定标准完整性校验 ──
            stored_hash = exp["criteria_hash"] if "criteria_hash" in exp.keys() else ""
            current_hash = compute_criteria_hash(criteria)
            if stored_hash and current_hash != stored_hash:
                return {
                    "error": "CRITERIA_TAMPERED",
                    "stored_hash": stored_hash[:16],
                    "current_hash": current_hash[:16],
                    "detail": ("判定标准已被修改! 冻结时的 hash 与当前 DB 中的不一致。"
                              "拒绝判定。请用 git checkout 恢复原始 freeze_experiment.py。"),
                }

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

        # 所有乘数从 1.0 出发, 末尾乘数 - 1.0 = 累计收益率
        strategy_ret = last["strategy_nav"] - 1.0
        ew_ret = last["equal_weight_nav"] - 1.0
        hs300_ret = (last["hs300_nav"] or 1.0) - 1.0

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

    # ── Candidate Strategy Shadow Recording ──────────────────

    def record_candidates(self, as_of: Optional[str] = None) -> dict:
        """记录当前所有活跃候选策略的趋势质量快照。

        将 trend_quality 数据写入最近一条 OOS 记录的 details_json 中。
        候选策略不改变策略线净值，只记录"候选策略会怎么看当前市场"，
        供 120 天后与冻结基线对比。

        Returns:
            {candidates_recorded: int, details: [...]}
        """
        today = as_of or date.today().isoformat()
        conn = get_conn()
        try:
            exp = conn.execute(
                "SELECT id FROM oos_experiments WHERE status = 'active' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

            if not exp:
                conn.close()
                return {"success": False, "error": "没有活跃的 OOS 实验"}

            exp_id = exp["id"]

            latest = conn.execute(
                "SELECT id, date, details_json FROM oos_nav_curves "
                "WHERE experiment_id=? ORDER BY date DESC LIMIT 1",
                (exp_id,)
            ).fetchone()

            if not latest or latest["date"] != today:
                conn.close()
                return {"success": False, "error": f"{today} 尚无 OOS 记录, 请先运行 oos-record"}

            detail = json.loads(latest["details_json"])

            # ── 计算五福趋势质量 ──
            try:
                from trend_quality import TrendQuality
                tq = TrendQuality()
                snapshot = tq.compute_batch(as_of=today)
            except ImportError:
                conn.close()
                return {"success": False, "error": "trend_quality 模块不可用"}

            detail["candidates"] = {
                "wufu_momentum": {
                    "computed_at": today,
                    "summary": {
                        "avg_quality": round(
                            sum(s["quality_score"] for s in snapshot) / max(len(snapshot), 1), 1
                        ),
                        "n_strong_trend": sum(1 for s in snapshot if s["quality_score"] >= 60),
                        "n_weak_trend": sum(1 for s in snapshot if s["quality_score"] < 30),
                        "top3": [
                            {"code": s["code"], "name": s["name"],
                             "r_squared": s["r_squared"],
                             "quality_score": s["quality_score"],
                             "trend_label": s["trend_label"]}
                            for s in snapshot[:3]
                        ],
                    },
                    "per_stock": {
                        s["code"]: {
                            "r_squared": s["r_squared"],
                            "quality_score": s["quality_score"],
                            "trend_label": s["trend_label"],
                            "annualized_slope": s["annualized_slope"],
                        }
                        for s in snapshot
                    },
                    "note": "五福动量候选策略。高R²+正斜率 → 趋势可靠, 可追。"
                            "低R²+正斜率 → 波动大, 需谨慎。"
                            "高R²+负斜率 → 下降趋势, 应回避。"
                            "解冻后若样本外验证通过, 可将R²校准注入动量评分。",
                },
            }

            conn.execute(
                "UPDATE oos_nav_curves SET details_json=? WHERE id=?",
                (json.dumps(detail, ensure_ascii=False), latest["id"])
            )
            conn.commit()

            summary = detail["candidates"]["wufu_momentum"]["summary"]
            n_total = len(snapshot)
            return {
                "success": True,
                "date": today,
                "candidates_recorded": 1,
                "details": [{
                    "name": "wufu_momentum",
                    "avg_quality": summary["avg_quality"],
                    "n_strong": summary["n_strong_trend"],
                    "n_weak": summary["n_weak_trend"],
                    "n_total": n_total,
                    "top3": summary["top3"],
                }],
            }
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    import sys

    exp = FreezeExperiment()

    if len(sys.argv) < 2:
        print("Usage: python3 freeze_experiment.py {freeze|record|status|judge|candidate|weekly-log|abort}")
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
            elif result.get("is_anchor"):
                print(f"⚓ Day 1 — 锚定基线 (三线 = 1.0000, 第一个样本外数据点在 Day 2)")
                print(f"   策略锚点: ¥{result.get('strategy_anchor_nav', '?'):,}")
            else:
                print(f"📊 Day {result['day']} (三线从 1.0000 出发)")
                print(f"   策略:    {result['strategy_mult']:.4f}x ({result['strategy_mult']-1:+.2%})")
                print(f"   等权:    {result['equal_weight_mult']:.4f}x ({result['equal_weight_mult']-1:+.2%})")
                print(f"   沪深300: {result['hs300_mult']:.4f}x ({result['hs300_mult']-1:+.2%})")
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

    elif cmd == "candidate":
        sub = sys.argv[2] if len(sys.argv) > 2 else "record"
        if sub == "record":
            result = exp.record_candidates()
            if result.get("success"):
                for d in result["details"]:
                    print(f"\n🔬 候选策略: {d['name']}")
                    print(f"   avg_quality={d['avg_quality']:.0f} "
                          f"strong={d['n_strong']} weak={d['n_weak']} "
                          f"total={d['n_total']}")
                    for t in d["top3"]:
                        print(f"   {'🥇' if t['quality_score']>=80 else '🥈' if t['quality_score']>=60 else '📊'}"
                              f" {t['name']}({t['code']})"
                              f" R²={t['r_squared']:.2f}"
                              f" quality={t['quality_score']:.0f}"
                              f" [{t['trend_label']}]")
            else:
                print(f"⚠️  {result.get('error', 'record failed')}")
        elif sub == "status":
            conn = get_conn()
            try:
                latest = conn.execute(
                    "SELECT date, details_json FROM oos_nav_curves "
                    "WHERE experiment_id=(SELECT id FROM oos_experiments "
                    "WHERE status='active' ORDER BY started_at DESC LIMIT 1) "
                    "ORDER BY date DESC LIMIT 1"
                ).fetchone()
                if latest:
                    d = json.loads(latest["details_json"])
                    cands = d.get("candidates", {})
                    if cands:
                        for name, cdata in cands.items():
                            s = cdata["summary"]
                            days_with = conn.execute(
                                "SELECT COUNT(*) FROM oos_nav_curves "
                                "WHERE json_extract(details_json, '$.candidates') IS NOT NULL"
                            ).fetchone()[0]
                            print(f"\n🔬 候选策略: {name}")
                            print(f"   最新数据: {latest['date']}")
                            print(f"   累计记录: {days_with} 天")
                            print(f"   avg_quality={s['avg_quality']:.0f} "
                                  f"strong={s['n_strong_trend']} "
                                  f"weak={s['n_weak_trend']}")
                    else:
                        print("尚无候选策略数据。运行 python3 cli.py oos-candidate record 开始记录。")
            finally:
                conn.close()
        else:
            print(f"用法: python3 cli.py oos-candidate {{record|status}}")

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
