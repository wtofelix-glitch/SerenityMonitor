#!/usr/bin/env python3
"""
Serenity Monitor CLI
使用: python3 cli.py info <code>
     python3 cli.py status
     python3 cli.py report
     python3 cli.py top
     python3 cli.py buy <code> <price> [amount]
     python3 cli.py sell <code>
     python3 cli.py target <code> <high> [low]
     python3 cli.py stop <code> <price>
     python3 cli.py check <code>
     python3 cli.py alerts
     python3 cli.py init [--force]
     python3 cli.py list
     python3 cli.py sector
     python3 cli.py serenity
     python3 cli.py suggest
     python3 cli.py portfolio         # 🆕 投资组合状态
     python3 cli.py signal            # 🆕 买卖信号
     python3 cli.py buy-auto [code] [amount]   # 🆕 自动买入
     python3 cli.py sell-auto [code]  # 🆕 自动卖出
     python3 cli.py backtest [code] [strategy]  # 🆕 回测
     python3 cli.py compare           # 🆕 多策略对比
     python3 cli.py backtest-factors [code]  # 🆕 14因子信号回测
     python3 cli.py trade <code> <buy|sell> [amount]  # 🆕 手动交易
     python3 cli.py rebalance         # 🆕 因子信号组合优化调仓
     python3 cli.py factor-monitor    # 🆕 盘中因子信号监控 + 微信预警
     python3 cli.py push-rebalance    # 🆕 调仓建议微信推送
     python3 cli.py sector-rotation   # 🆕 行业轮动扫描
     python3 cli.py sector-rotation --all  # 🆕 详细版（含个股明细）
     python3 cli.py alerts-push        # 🅰 主动信号推送（三因子共振/三重确认/因子突变/极端信号）
     python3 cli.py scan-candidates    # 🆕 标的评分排名（含明细）
     python3 cli.py suggest-stock      # 🆕 今日重点关注推荐
     python3 cli.py signal-perf        # 🆕 信号绩效追踪（命中率）
     python3 cli.py weekly-review      # 🆕 每周策略复盘
     python3 cli.py check-anomalies    # 🆕 异动自动解读
     python3 cli.py perf-report        # 🆕 实盘绩效看板（图表+文本）
     python3 cli.py factor-interpret   # 🆕 AI因子解读（大白话）
     python3 cli.py scan-mainboard     # 🆕 主板全量扫描
     python3 cli.py sync-log           # 🆕 交易日志→gbrain/本地沉淀
     python3 cli.py trade-record <BUY/SELL> <code> <price> [amount] [note]  # 🆕 记录实际交易
     python3 cli.py trade-log [--compare]  # 🆕 查看交易记录 / 对比系统信号
     python3 cli.py health                 # 🔬 系统健康诊断
     python3 cli.py auto                  # 🆕 自动调仓计划
     python3 cli.py auto-exec             # 🚀 强制信号执行（含重试）
     python3 cli.py auto-stats            # 📊 信号执行统计
     python3 cli.py auto-premarket        # ⏰ 盘前简报推送
     python3 cli.py auto-push             # 🆕 自动调仓 + 微信推送
     python3 cli.py backtest-quick        # 🆕 快速回测快照
     python3 cli.py workflow              # 🆕 一站式每日工作流
     python3 cli.py tier1-reentry         # 🔄 T1 回补检查
     python3 cli.py tier1-reentry --push  # 🔄 检查+微信推送
     python3 cli.py tier1-reentry --status # 🔄 查看当前状态
"""
import sys
import os
from datetime import date

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    init_db, load_all_stocks, get_stock, upsert_stock,
    add_trade, set_active, clear_active, get_trades, get_conn
)
from data_engine import fetch_realtime, fetch_single, get_all_today_snapshots
from daily_report import generate_daily_report, generate_simple_status
from price_alert import check_alerts, get_pending_alerts, ack, check_suggested_targets
from config import get_default_stocks, SUGGESTED_TARGETS, STOCK_MAP, STOCK_DETAILS
from scorer import score_all
from monitor import monitor_all, get_monitor_summary
from portfolio import PortfolioManager, get_portfolio
from signal_engine import generate_signals
from notifier import test_push, push_daily_report, push_signal_summary, print_setup_guide
from rating_engine import get_rating, get_portfolio_rating, get_candidate_rank
from candidate_scanner import cmd_scan_candidates, cmd_suggest_stock
from signal_performance import cmd_signal_performance
from weekly_review import cmd_weekly_review
from anomaly_analyzer import cmd_check_anomalies
from performance_report import cmd_perf_report
from factor_interpreter import cmd_factor_interpret
from candidate_scanner_full import cmd_scan_mainboard
from trading_log_sync import cmd_sync_log
from auto_execute import generate_execution_plan, main as cmd_auto_execute
import quick_backtest
from trade_record import cmd_trade_record, cmd_trade_log
from factor_attribution import generate_factor_report, detect_factor_decay
from market_timing import get_market_signal, get_market_advice
from backtest_engine import (
    run_backtest, compare_strategies,
    TrendFollowingStrategy, MultiFactorStrategy,
    MeanReversionStrategy, HybridStrategy,
    MultiFactorWithSignalsStrategy,
    format_backtest_result, format_comparison
)
from dividend_engine import DividendEngine
from etf_momentum import ETFMomentumStrategy
try:
    from signal_push import build_signal_brief, format_push_message
except ImportError:
    build_signal_brief = format_push_message = None

# 板块分组
SECTORS = {
    "CPO光子器件":    {"codes": ["002281"],        "serenity_tags": ["CPO_chokepoint"]},
    "激光+光引擎":    {"codes": ["000988", "603083"], "serenity_tags": ["laser+optical_engine", "optical_module"]},
    "光纤基础设施":   {"codes": ["600487"],        "serenity_tags": ["fiber_infra"]},
    "半导体衬底材料": {"codes": ["002428"],        "serenity_tags": ["substrate_material"]},
    "AI存储":        {"codes": ["603986"],        "serenity_tags": ["ai_storage"]},
    "磷化工/特种气体": {"codes": ["600141"],      "serenity_tags": ["phosphorus_chemicals"]},
    "功率半导体":     {"codes": ["600460"],      "serenity_tags": ["power_semiconductor"]},
}

# 代码 -> 板块名 映射
CODE_SECTOR = {}
for sec_name, sec_info in SECTORS.items():
    for code in sec_info["codes"]:
        CODE_SECTOR[code] = sec_name

# 标签 -> 板块名 映射
TAG_SECTOR = {}
for sec_name, sec_info in SECTORS.items():
    for tag in sec_info["serenity_tags"]:
        TAG_SECTOR[tag] = sec_name

FACTOR_NAMES = {
    "ksft": "K线形态", "rank_20": "Rank", "rsv_20": "RSV",
    "beta_20": "Beta", "resi_20": "残差", "macd_signal": "MACD",
    "obv_trend": "OBV", "mfi_signal": "MFI", "cci_signal": "CCI",
    "wq_alpha1": "A1日内", "wq_alpha3": "A3均价", "wq_alpha5": "A5价偏",
    "wq_alpha15": "A15波幅", "wq_alpha19": "A19动量",
}

FACTOR_EMOJIS = {
    "ksft": "📊", "rank_20": "🏆", "rsv_20": "📈",
    "beta_20": "📉", "resi_20": "📐", "macd_signal": "🔄",
    "obv_trend": "📦", "mfi_signal": "💰", "cci_signal": "🌡️",
    "wq_alpha1": "📌", "wq_alpha3": "⚖️", "wq_alpha5": "🎯",
    "wq_alpha15": "📏", "wq_alpha19": "⏩",
}


def cmd_rebalance():
    """基于因子信号 + Rank IC 生成调仓计划"""
    from portfolio_optimizer import PositionOptimizer, format_rebalance_plan
    from factor_engine import get_current_signals
    from factor_ic import compute_rank_ic
    from db import load_all_stocks

    # 1. 获取因子信号
    signals = get_current_signals()
    if not signals:
        print("⚠️ 无因子信号数据（可能需要先运行 fetch_history）")
        return

    # 2. 获取 IC 数据
    try:
        ic_data = compute_rank_ic(days=30, window=20)
        if "error" in ic_data:
            print(f"⚠️ Rank IC 数据不可用: {ic_data['error']}")
            ic_data = None
    except Exception as e:
        print(f"⚠️ Rank IC 计算失败: {e}")
        ic_data = None

    # 3. 获取当前持仓
    stocks = load_all_stocks()
    positions = [s for s in stocks if s.get("is_active")]

    # 4. 获取可用现金
    from portfolio import get_portfolio
    pm = get_portfolio()
    cash = pm.get_cash()

    # 5. 运行优化
    opt = PositionOptimizer()
    allocation = opt.optimize_allocation(signals, positions, cash, ic_data)

    if not allocation:
        print("⚠️ 优化结果为空，无法生成调仓计划")
        return

    # 6. 构建 current_portfolio 格式
    current_portfolio = []
    for p in positions:
        current_portfolio.append({
            "code": p["code"],
            "name": p["name"],
            "trade_amount": p.get("trade_amount", 0),
            "buy_price": p.get("buy_price", 0),
        })
    # 加上所有有信号的标的（即使未持仓）
    signal_codes = {s["code"] for s in signals}
    for code in signal_codes:
        if code not in {p["code"] for p in positions}:
            current_portfolio.append({
                "code": code,
                "name": STOCK_MAP.get(code, {}).get("name", code),
                "trade_amount": 0,
                "buy_price": 0,
            })

    # 7. 生成调仓计划
    target_weights = {code: alloc["suggested_weight"] for code, alloc in allocation.items()}
    plan = opt.rebalance_plan(current_portfolio, target_weights)

    # 8. 输出
    print(format_rebalance_plan(plan))


def cmd_factors():
    """展示所有标的的 Alpha 因子信号（9个因子矩阵）"""
    from factor_engine import get_current_signals
    from datetime import datetime

    results = get_current_signals()
    if not results:
        print("⚠️ 无因子数据（可能需要先运行 fetch_history）")
        return

    print(f"📊 {len(FACTOR_NAMES)}因子信号矩阵 [{datetime.now().strftime('%H:%M:%S')}]")
    print("═══════════════════════════════════════════════════════════════════")
    print()

    # 表头
    header = f"{'标的名':>8}"
    for k in FACTOR_NAMES:
        header += f"  {FACTOR_EMOJIS[k]:>1}{k.split('_')[0]:>4}"
    print(header)
    print("───────────────────────────────────────────────────────────────────")

    for r in results:
        signals = r["factors"].get("signals", {})
        line = f"{r['name']:>8}"
        for k in FACTOR_NAMES:
            v = signals.get(k)
            if v is None:
                line += f"  {'  --':>6}"
            else:
                color = "🟢" if v > 0.1 else "🔴" if v < -0.1 else "⚪"
                line += f"  {color}{v:>+.2f}"
        line += f"  ➡️  综合={r['signal']:+.3f}"
        print(line)

    print("═══════════════════════════════════════════════════════════════════")
    print(f"共 {len(results)} 只标的")


def cmd_adjust_weights():
    """基于 Rank IC 动态调整评分权重"""
    from weight_adjuster import adjust_weights, show_weights
    weights = adjust_weights()


def cmd_show_weights():
    """显示当前动态权重"""
    from weight_adjuster import show_weights
    show_weights()


def cmd_init(force: bool = False):
    """初始化数据库并导入默认标的
    注意：若数据库已有持仓，除非 --force，否则跳过初始化以保护现有持仓
    """
    # 安全守卫：检测已有持仓
    from db import load_all_stocks
    existing = load_all_stocks()
    active = [s for s in existing if s.get("is_active")]
    if active and not force:
        names = ", ".join(f"{s['name']}({s['code']})" for s in active)
        print(f"⚠️ 数据库已有 {len(active)} 只持仓：{names}")
        print("❌ 禁止执行 init — 这会覆盖持仓数据，丢失买入价/日期！")
        print("💡 如需强制重置，请使用： python3 cli.py init --force")
        return

    init_db()
    for stock in get_default_stocks():
        d = {
            "code": stock.code,
            "name": stock.name,
            "market": stock.market,
            "tier": stock.tier,
            "buy_price": 0,
            "buy_date": "",
            "target_high": 0,
            "target_low": 0,
            "stop_loss": 0,
            "is_active": 0,
            "notes": "",
        }
        # 如果有建议目标价，预填
        if stock.code in SUGGESTED_TARGETS:
            t = SUGGESTED_TARGETS[stock.code]
            d["target_high"] = t["target_high"]
            d["target_low"] = t["target_low"]
        upsert_stock(d)
    print(f"✅ 数据库初始化完成！已导入 {len(get_default_stocks())} 只候选标的")


def cmd_status():
    """显示当前行情速览"""
    print(generate_simple_status())


def cmd_report():
    """生成收盘简报"""
    print(generate_daily_report())


def cmd_llm_report():
    """生成 LLM 文字研报（基于 9 维评分 + 数据注入）"""
    print(_generate_llm_report_text())
    print()
    print("> 提示：文字研报通过已生成的 prompt 数据 + Hermes 自身能力生成。")


def _generate_llm_report_text() -> str:
    """生成 LLM 文字研报"""
    from datetime import date
    today = date.today().isoformat()

    # 1. 获取今日评分
    from scorer import score_all
    scores = score_all()

    # 2. 构建研报数据
    from llm_report import generate_llm_report
    report_data = generate_llm_report(scores)

    # 3. 输出结构化研报
    lines = []
    lines.append(f"\U0001f4ca **Serenity LLM 研报 | {today}**")
    lines.append("=" * 40)
    lines.append("")

    ranked = report_data["ranked"]

    # TOP3
    lines.append("\U0001f3c6 评分 TOP3")
    lines.append("\u2500" * 30)
    for r in ranked[:3]:
        lines.append(
            f"{r['name']}({r.get('code','?')}) {r['total_score']:.0f}分 | "
            f"基{r.get('base_score',0):.0f} 动{r.get('momentum_score',0):.0f} "
            f"量{r.get('volume_score',0):.0f} 护{r.get('moat_score',50):.0f} | "
            f"{r['zone_label']}"
        )
    lines.append("")

    # 评分区间分布
    score_brackets = {"强势(90+)": 0, "良好(75-89)": 0, "中性(60-74)": 0, "弱势(<60)": 0}
    for r in ranked:
        s = r["total_score"]
        if s >= 90: score_brackets["强势(90+)"] += 1
        elif s >= 75: score_brackets["良好(75-89)"] += 1
        elif s >= 60: score_brackets["中性(60-74)"] += 1
        else: score_brackets["弱势(<60)"] += 1

    lines.append("\U0001f4c8 评分分布")
    lines.append("\u2500" * 30)
    for k, v in score_brackets.items():
        bar = "\u2588" * v + "\u2591" * max(0, 7 - v)
        lines.append(f"  {k}: {bar} {v}只")
    lines.append("")

    # 信号分布
    actions = {}
    for r in ranked:
        a = r.get("signal_action", "?")
        actions[a] = actions.get(a, 0) + 1
    lines.append("\U0001f4e1 信号分布")
    lines.append("\u2500" * 30)
    for k in ["BUY", "CAUTION_BUY", "HOLD", "CAUTION", "SELL"]:
        if k in actions:
            e = {"BUY": "\U0001f7e2", "CAUTION_BUY": "\U0001f7e1", "HOLD": "\u26aa", "CAUTION": "\U0001f7e0", "SELL": "\U0001f534"}.get(k, "\u26aa")
            lines.append(f"  {e} {k}: {actions[k]}只")
    lines.append("")

    # 护城河 TOP3
    lines.append("\U0001f3db\ufe0f 护城河 TOP3")
    lines.append("\u2500" * 30)
    for r in sorted(ranked, key=lambda x: x.get("moat_score", 50), reverse=True)[:3]:
        lines.append(f"  {r['name']}: {r.get('moat_score', 50):.0f}分")
    lines.append("")

    # AI 总结
    lines.append("\U0001f9e0 AI 解读")
    lines.append("\u2500" * 30)

    top_name = report_data["top_name"]
    top_score = report_data["top_score"]
    buy_c = report_data["buy_count"]
    sell_c = report_data["sell_count"]

    avg_score = sum(r["total_score"] for r in ranked) / max(len(ranked), 1)
    strong_count = score_brackets["强势(90+)"] + score_brackets["良好(75-89)"]
    weak_count = score_brackets["中性(60-74)"] + score_brackets["弱势(<60)"]

    if avg_score >= 75:
        market_note = "市场情绪偏强，整体评分处于高位"
    elif avg_score >= 65:
        market_note = "市场中性偏正面，标的之间分化明显"
    elif avg_score >= 55:
        market_note = "市场偏弱，整体评分中枢下行"
    else:
        market_note = "市场弱势显著，建议控制仓位"

    lines.append(f"今日 {len(ranked)} 只标的综合均分 {avg_score:.0f} 分。{market_note}。")
    lines.append(f"TOP1 {top_name}({top_score:.0f}分)领跑，买入信号 {buy_c} 只，卖出 {sell_c} 只。")
    moat_top_name = report_data["moat_top"]
    if moat_top_name == ranked[0]["name"]:
        lines.append("高分优势集中于护城河赛道。")
    else:
        lines.append("高分优势集中于动量/技术赛道。")
    if sell_c > buy_c:
        lines.append("低分标的受困于情绪转弱。")
    else:
        lines.append("低分标的受困于基本面转弱。")

    return "\n".join(lines)


def cmd_conviction():
    """运行权重辩论 + 多周期共识（支持 --history / -h 查看历史记录）"""
    from conviction_cli import _run_conviction_analysis
    
    if "--history" in sys.argv or "-h" in sys.argv:
        # 查历史
        days = 30
        for i, arg in enumerate(sys.argv):
            if arg in ("--days", "-d") and i + 1 < len(sys.argv):
                try:
                    days = int(sys.argv[i + 1])
                except ValueError:
                    pass
        cmd_conviction_history(days)
        return
    
    print(_run_conviction_analysis())


def cmd_conviction_history(days: int = 30):
    """查看历史权重辩论记录"""
    from db import get_conviction_history
    
    records = get_conviction_history(days)
    if not records:
        print(f"📭 近 {days} 天无 conviction 记录")
        return
    
    lines = []
    lines.append(f"📊 **Serenity 权重辩论历史 | 近 {days} 天**")
    lines.append("")
    lines.append(f"{'日期':<12} {'市场':<6} {'均分':>4} {'护城河':>7} {'动量':>7} {'情绪':>7} {'仓位建议'}")
    lines.append("─" * 70)
    
    for r in records:
        weights = r.get("debated_weights", {})
        m = weights.get("moat", 0)
        momentum = weights.get("momentum", 0.12)
        sentiment = weights.get("sentiment", 0.08)
        regime_emoji = {"强势": "🟢", "震荡": "🟡", "弱势": "🔴"}.get(r["regime"], "⚪")
        
        lines.append(
            f"{r['date']:<12} "
            f"{regime_emoji}{r['regime']:<4} "
            f"{r['score_avg']:>4.0f} "
            f"{m:>6.1%} "
            f"{momentum:>6.1%} "
            f"{sentiment:>6.1%} "
            f"{r.get('position_advice', '')[:20]}"
        )
    
    lines.append("")
    
    # 趋势分析
    if len(records) >= 3:
        trends = []
        m_vals = [r.get("debated_weights", {}).get("moat", 0.10) for r in records]
        if m_vals:
            m_trend = (m_vals[-1] - m_vals[0]) / max(m_vals[0], 0.01) * 100
            trends.append(f"护城河权重 {m_vals[0]:.1%}→{m_vals[-1]:.1%} ({m_trend:+.0f}%)")
        lines.append("📈 **趋势分析**")
        lines.append(f"  {' | '.join(trends)}")
    
    print("\n".join(lines))


def cmd_analyze_discount(code: str):
    """深度折扣诊断：分析评分vs Serenity匹配度的分化原因"""
    if not code:
        print("用法: python3 cli.py analyze <code>")
        print("示例: python3 cli.py analyze 600585")
        return
    from discount_analyzer import analyze_discount
    print(analyze_discount(code))


def cmd_buy(code: str, price: float, amount: float = 0):
    """买入标的"""
    stock = get_stock(code)
    if not stock:
        print(f"❌ 标的 {code} 不存在，请先运行 init")
        return

    today = date.today().isoformat()
    set_active(code, price, today,
               stock["target_high"], stock["target_low"])

    # 如果持仓表有 trade_amount 字段，更新
    if amount:
        conn = get_conn()
        conn.execute("UPDATE stocks SET trade_amount=? WHERE code=?", (amount, code))
        conn.commit()
        conn.close()

    add_trade(code, "buy", price, 0, today, f"CLI买入{amount:.0f}元" if amount else "CLI买入", trade_amount=amount)
    msg = f"✅ 已标记买入 {stock['name']}({code}) 价格 {price:.2f}"
    if amount:
        msg += f" 金额 {amount:.0f}元"
    print(msg)


def cmd_sell(code: str):
    """卖出标的"""
    stock = get_stock(code)
    if not stock or not stock["is_active"]:
        print(f"❌ 标的 {code} 未持有，无需卖出")
        return

    # 获取当前价
    data = fetch_single(code)
    price = data.get("price", 0) if data else 0

    today = date.today().isoformat()
    clear_active(code)
    add_trade(code, "sell", price, 0, today,
              f"卖出 (买入{stock['buy_price']:.2f})")

    profit = ((price - stock["buy_price"]) / stock["buy_price"] * 100) if stock["buy_price"] else 0
    print(f"✅ 已标记卖出 {stock['name']}({code}) 价格 {price:.2f} 盈亏 {profit:+.2f}%")
    print(f"   → 现在可以寻找下一只最匹配标的")


def cmd_target(code: str, high: float, low: float = 0):
    """设置目标卖出价区间"""
    stock = get_stock(code)
    if not stock:
        print(f"❌ 标的 {code} 不存在")
        return
    stock["target_high"] = high
    stock["target_low"] = low
    upsert_stock(stock)
    print(f"✅ {stock['name']}({code}) 目标价设置为 上限{high:.2f} 下限{low:.2f}")


def cmd_stop(code: str, price: float):
    """设置止损价"""
    stock = get_stock(code)
    if not stock:
        print(f"❌ 标的 {code} 不存在")
        return
    stock["stop_loss"] = price
    upsert_stock(stock)
    print(f"✅ {stock['name']}({code}) 止损价设置为 {price:.2f}")


def cmd_check(code: str):
    """检查某个标的的买入区间"""
    result = check_suggested_targets(code)
    print(result["msg"])


def cmd_alerts():
    """查看未确认预警"""
    alerts = get_pending_alerts()
    if not alerts:
        print("✅ 暂无未确认预警")
        return
    for a in alerts:
        print(f"🆔 {a['id']} | {a['name']}({a['code']}) | {a['alert_type']} | {a['message']}")
        print(f"   触发时间: {a['created_at']}")
    print(f"\n共 {len(alerts)} 条未确认预警")
    print("使用: python3 cli.py ack <alert_id> 确认")


def cmd_ack(alert_id: int):
    """确认预警"""
    ack(alert_id)
    print(f"✅ 预警 {alert_id} 已确认")


def cmd_list():
    """列出所有标的及状态"""
    stocks = load_all_stocks()
    print(f"{'代码':>8} {'名称':<10} {'Tier':<5} {'持有':<6} {'买入价':<10} {'目标高':<10} {'目标低':<10} {'止损':<10}")
    print("-" * 75)
    for s in stocks:
        active = "⭐" if s["is_active"] else ""
        print(f"{s['code']:>8} {s['name']:<10} {s['tier']:<5} {active:<6} "
              f"{s['buy_price']:<10.2f} {s['target_high']:<10.2f} {s['target_low']:<10.2f} {s['stop_loss']:<10.2f}")


def cmd_trades(code: str = ""):
    """查看交易记录"""
    trades = get_trades(code if code else None, 30)
    if not trades:
        print("暂无交易记录")
        return
    for t in trades:
        emoji = "🟢" if t["action"] == "buy" else "🔴"
        print(f"{emoji} {t['code']} | {t['action']} | {t['price']:.2f} | {t['date']} | {t['note']}")


def cmd_rescore():
    """重新评分所有候选标的"""
    from datetime import date
    results = score_all()
    print(f"📊 Serenity 多因子评分 + 信号 | {date.today()}")
    print("=" * 70)
    for r in results:
        signal_icon = {"STRONG_BUY": "🟢🟢🟢", "BUY": "🟢🟢", "CAUTION_BUY": "🟢",
                       "HOLD": "⚪", "WATCH": "🟡", "SELL": "🔴🔴", "STOP_LOSS": "🔴🔴🔴"}.get(
            r.get("signal_action", ""), "⚪")
        print(f"#{r['rank']} {r['name']:6s} | 总分 {r['total_score']:5.1f} | "
              f"{signal_icon} {r['signal_action']:<12} | "
              f"技{r['technical_score']:.0f} 因{r['factor_score']:.0f} 位{r['zone_score']:.0f} "
              f"护{r.get('moat_score', 50):.0f} 匹{r['serenity_score']:.0f} | "
              f"{r['zone_label']}")
    print("=" * 70)


def cmd_portfolio():
    """查看投资组合状态（5万→10万追踪）"""
    pm = get_portfolio()
    print(pm.format_portfolio())


def cmd_signal():
    """查看所有标的的买卖信号"""
    pm = get_portfolio()
    signals = generate_signals(portfolio=pm)
    print(pm.format_signal_summary(signals))


def cmd_buy_auto(code: str = "", force_amount: float = 0):
    """基于信号自动买入"""
    pm = get_portfolio()
    if code:
        signals = [s for s in generate_signals(portfolio=pm) if s["code"] == code]
    else:
        signals = [s for s in generate_signals(portfolio=pm)
                   if s.get("action") in ("STRONG_BUY", "BUY")]
    if not signals:
        print("📭 当前无买入信号")
        return
    # 选评分最高的
    best = max(signals, key=lambda s: s.get("total_score", 0))
    if force_amount > 0:
        result = pm.execute_buy(best["code"], signal_confidence=0.8, force_amount=force_amount)
    else:
        confidence = best.get("buy_confirm", {}).get("confidence", 0.5)
        result = pm.execute_buy(best["code"], signal_confidence=confidence)
    if result["status"] == "buy":
        print(f"✅ 自动买入成功!")
        print(f"   {result['name']}({result['code']}) {result['shares']}股 @ {result['price']:.2f}")
        print(f"   金额: {result['amount']:.0f}元 | 止损: {result['stop_loss']:.2f}")
    else:
        print(f"❌ 买入失败: {result.get('reason', '未知错误')}")


def cmd_sell_auto(code: str = "", reason: str = "信号触发"):
    """基于信号自动卖出"""
    pm = get_portfolio()
    if code:
        result = pm.execute_sell(code, reason)
    else:
        # 检查止盈止损
        actions = pm.check_stop_conditions()
        sellables = [a for a in actions if "SELL" in a["action"]]
        if not sellables:
            print("📭 当前无卖出信号")
            return
        result = pm.execute_sell(sellables[0]["code"], sellables[0]["reason"])
    if result.get("status") == "sell":
        print(f"✅ 自动卖出成功!")
        print(f"   {result['name']}({result['code']})")
        print(f"   买入: {result['buy_price']:.2f} → 卖出: {result['sell_price']:.2f}")
        print(f"   盈亏: {result['profit_pct']:+.2f}% ({result['net_profit']:+.2f}元)")
    else:
        print(f"❌ 卖出失败: {result.get('reason', '未知错误')}")


def cmd_backtest(code: str = "", strategy_name: str = "hybrid"):
    """对指定标的回测指定策略"""
    from backtest_engine import (
        TrendFollowingStrategy, MultiFactorStrategy,
        MeanReversionStrategy, HybridStrategy
    )
    strategies = {
        "trend": TrendFollowingStrategy(),
        "multifactor": MultiFactorStrategy(),
        "meanreversion": MeanReversionStrategy(),
        "hybrid": HybridStrategy(),
    }
    strategy = strategies.get(strategy_name.lower(), HybridStrategy())
    codes_to_test = [code] if code else ["002281", "000988", "600487"]
    for c in codes_to_test:
        print(f"\n🔄 回测 {c} ...")
        result = run_backtest(c, strategy, initial_capital=50000)
        print(format_backtest_result(result))


def cmd_compare():
    """多策略对比回测"""
    print("🔄 运行多策略对比回测... (可能需要30秒)\n")
    results = compare_strategies()
    print(format_comparison(results))


def cmd_backtest_factor(code: str = ""):
    """使用14因子信号策略回测指定标的"""
    from backtest_engine import (
        MultiFactorWithSignalsStrategy, run_backtest, format_backtest_result
    )
    codes_to_test = [code] if code else ["002281", "000988", "600487"]
    strategy = MultiFactorWithSignalsStrategy(use_factors=True)
    for c in codes_to_test:
        print(f"\n🔄 14因子信号回测 {c} ...")
        result = run_backtest(c, strategy, initial_capital=50000)
        print(format_backtest_result(result))
    print("\n✅ 回测完成 (因子阈值: >0.2买入, <-0.2卖出)")


def cmd_backtest_viz(code: str = ""):
    """生成14因子回测可视化HTML报告"""
    from backtest_viz import generate_viz_report

    if not code:
        print("❌ 请指定标的代码: python3 cli.py backtest-viz <code>")
        return

    print(f"🔄 生成14因子回测可视化报告: {code} ...")
    path = generate_viz_report(code)
    if path:
        print(f"\n✅ 回测可视化报告已生成!")
        print(f"📄 文件路径: {path}")
        rel = os.path.relpath(path, os.path.dirname(os.path.abspath(__file__)))
        print(f"📎 相对路径: {rel}")
    else:
        print(f"❌ 数据不足，无法生成报告（{code}）")


def cmd_backtest_report(code: str = ""):
    """生成综合回测报告 (HTML)"""
    from backtest_report import generate_report
    if not code:
        code = "002281"
    path = generate_report(code)
    if path:
        print(f"📄 文件: {path}")


def cmd_backtest_comparison():
    """生成全策略对比报告 (HTML)"""
    from backtest_report import generate_comparison_report
    path = generate_comparison_report()
    if path:
        print(f"📄 文件: {path}")


def cmd_test_push():
    """测试微信推送是否正常"""
    test_push()


def cmd_factor_monitor():
    """盘中因子信号监控 — 检测因子偏移并推送微信预警"""
    from factor_monitor import check_factor_changes
    alerts = check_factor_changes()
    if not alerts:
        print("✅ 无因子偏移（或首次运行，已保存快照）")
    else:
        print(f"\n⚠️ 发现 {len(alerts)} 条因子偏移预警:")
        for a in alerts:
            print(f"  {a}")


def cmd_push_rebalance():
    """生成调仓建议并推送微信"""
    from daily_rebalance import generate_rebalance_push
    generate_rebalance_push()


def cmd_signal_push():
    """检测可执行信号并推送微信（买入候选+风险提醒）"""
    print("📡 检测交易信号...")
    brief = build_signal_brief()
    msg = format_push_message(brief)
    print(msg)
    from notifier import send_message
    from datetime import date
    title = f"📡 Serenity 信号 | {date.today().isoformat()}"
    buy_n = len(brief.get("buy_candidates", []))
    risk_n = len(brief.get("risk_alerts", []))
    send_message(title, msg, content_type="markdown", summary=f"买入{buy_n} 风险{risk_n}")
    print("\n✅ 信号推送完成")


def cmd_factor_ic():
    """因子 IC 分析 — 各维度与次日收益的秩相关性"""
    from factor_ic import compute_rank_ic, print_report
    args_days = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 30
    result = compute_rank_ic(days=args_days)
    print_report(result)


def cmd_perf_attr():
    """绩效归因 — Top N 选股回测 + 因子贡献度"""
    from performance_attribution import run_attribution, print_report
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
    result = run_attribution(top_n=top_n)
    print_report(result)


def cmd_reflection():
    """反思学习环 — 查看近期评分反思记录"""
    from reflection_engine import show_reflections
    days = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 7
    show_reflections(days=days)


def cmd_reflection_ic():
    """反思学习环 — 显示维度IC + 权重调整建议"""
    from reflection_engine import show_dimension_ic
    days = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
    show_dimension_ic(days=days)


def cmd_reflection_apply():
    """反思学习环 — 应用维度IC建议 → 自动调整权重"""
    from reflection_engine import apply_reflection_adjustments
    days = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
    apply_reflection_adjustments(days=days)


def cmd_council():
    """5 Agent 投资委员会 — 终审决策
    用法: python3 cli.py council [code]
    """
    from serenity_council import InvestmentCommittee, format_decision, format_report
    council = InvestmentCommittee()
    if len(sys.argv) > 2 and sys.argv[2].strip():
        code = sys.argv[2].strip()
        if code in ALL_CODES:
            dec = council.review(code)
            print(format_decision(dec))
        else:
            print(f"❌ 未知股票代码: {code}")
            print(f"   可用代码: {', '.join(ALL_CODES)}")
    else:
        decisions = council.review_all()
        for dec in decisions:
            print(format_decision(dec))
        print(format_report(decisions))


def cmd_council_report():
    """委员会总报告 — 所有标的概览
    用法: python3 cli.py council-report
    """
    from serenity_council import InvestmentCommittee, format_report
    council = InvestmentCommittee()
    decisions = council.review_all()
    print(format_report(decisions))


def cmd_push_report():
    """手动推送今日收盘简报 + 信号到微信"""
    from daily_report import generate_daily_report
    report = generate_daily_report()
    print(report)
    print("\n\n✅ 推送完成（请查看微信）")


def cmd_push_guide():
    """显示微信推送配置指引"""
    print_setup_guide()


def cmd_dashboard():
    """启动监控仪表盘 (Flask, port 8401)"""
    # 检测是否已在运行
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://localhost:8401/monitor", timeout=3)
        if resp.status == 200:
            print("✅ 监控仪表盘已在运行 → http://localhost:8401/monitor")
            return
    except (OSError, ValueError):
        pass
    # 未运行 → 后台启动（launchd 会自动接管）
    print("📱 启动监控仪表盘 → http://localhost:8401/monitor")
    pid = os.fork()
    if pid == 0:  # 子进程
        os.system("arch -arm64 /usr/bin/python3 monitoring_dashboard.py &")
        sys.exit(0)


def cmd_dash_dashboard():
    """启动 Dash 交互看板 (Plotly Dash, port 8050)"""
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://localhost:8050", timeout=3)
        if resp.status == 200:
            print("✅ Dash 看板已在运行 → http://localhost:8050")
            return
    except (OSError, ValueError):
        pass
    print("📊 启动 Dash 看板 → http://localhost:8050")
    pid = os.fork()
    if pid == 0:
        os.system("arch -arm64 /usr/bin/python3 dash_dashboard.py &")
        sys.exit(0)


def cmd_viz_report(code: str):
    """生成回测可视化HTML报告 (viz-report 是 backtest-viz 的别名)"""
    cmd_backtest_viz(code)


def cmd_trade(code: str, action: str, amount: float = 0):
    """手动记录交易（便捷方式）"""
    pm = get_portfolio()
    if action == "buy":
        from data_engine import fetch_single
        data = fetch_single(code)
        price = data.get("price", 0) if data else 0
        if price <= 0:
            print(f"❌ 无法获取 {code} 价格")
            return
        shares = int(amount / price / 100) * 100 if amount > 0 else 0
        actual_amount = shares * price
        if actual_amount <= 0:
            print(f"❌ 无效金额")
            return
        from db import set_active, add_trade
        from config import STOCK_DETAILS
        detail = STOCK_DETAILS.get(code, {})
        today = date.today().isoformat()
        set_active(code, price, today, detail.get("target_sell", 0), detail.get("buy_zone_low", 0))
        conn = get_conn()
        conn.execute("UPDATE stocks SET trade_amount=? WHERE code=?", (actual_amount, code))
        conn.commit()
        conn.close()
        add_trade(code, "buy", price, shares, today, f"手动买入 {actual_amount:.0f}元", trade_amount=actual_amount)
        print(f"✅ 已记录买入 {STOCK_MAP.get(code,{}).get('name',code)} {shares}股 @ {price:.2f}")
    elif action == "sell" and amount > 0:
        # 部分卖出
        from data_engine import fetch_single
        data = fetch_single(code)
        price = data.get("price", 0) if data else 0
        shares = int(amount / price / 100) * 100 if price > 0 else 0
        if shares > 0:
            from db import add_trade
            add_trade(code, "sell", price, shares, date.today().isoformat(),
                      f"手动部分卖出 {amount:.0f}元", trade_amount=amount)
            print(f"✅ 已记录部分卖出 {shares}股 @ {price:.2f}")
    elif action == "sell":
        result = pm.execute_sell(code, "手动卖出")
        if result.get("status") == "sell":
            print(f"✅ 已记录清仓卖出: 盈亏 {result['profit_pct']:+.2f}%")
        else:
            print(f"❌ {result.get('reason', '卖出失败')}")


def cmd_monitor():
    """盘中监控所有持仓标的"""
    result = monitor_all()
    if result["status"] == "empty":
        print(result["message"])
        return
    print(f"⏱️ 盘中监控 [{result['check_time']}] — 检测 {result['checked_count']} 只持仓")
    print(f"触发 {result['alert_count']} 条预警")
    for a in result["alerts"]:
        level_icon = {"A": "🔴🚨", "B": "⚠️", "C": "💡"}.get(a.get("level", ""), "📌")
        print(f"\n{level_icon} [{a.get('level', '?')}] {a['msg']}")
    print()


def cmd_monitor_summary():
    """生成监控摘要——无持仓时静默"""
    summary = get_monitor_summary()
    if summary:
        print(summary)


def cmd_top():
    """查看持仓涨跌幅排名"""
    from datetime import datetime

    all_stocks = load_all_stocks()
    holdings = [s for s in all_stocks if s["is_active"]]

    if not holdings:
        print("📭 当前无持仓")
        return

    codes = [s["code"] for s in holdings]
    realtime_list = fetch_realtime(codes)

    # 按 code 索引实时数据
    rt_map = {item["code"]: item for item in realtime_list}

    results = []
    latest_time = ""
    for s in holdings:
        code = s["code"]
        rt = rt_map.get(code, {})
        price = rt.get("price", 0)
        close_yesterday = rt.get("close_yesterday", 0)
        if close_yesterday and close_yesterday > 0:
            change_pct = round((price - close_yesterday) / close_yesterday * 100, 2)
        else:
            change_pct = 0.0

        t = rt.get("time", "")
        if t and (not latest_time or t > latest_time):
            latest_time = t

        results.append({
            "code": code,
            "name": s["name"],
            "price": price,
            "change_pct": change_pct,
        })

    # 按涨跌幅从高到低排序
    results.sort(key=lambda x: x["change_pct"], reverse=True)

    display_time = latest_time or datetime.now().strftime("%H:%M:%S")

    print(f"📊 持仓涨跌幅排名 [{display_time}]")
    print("─────────────────────────────")

    for r in results:
        emoji = "🟢" if r["change_pct"] >= 0 else "🔴"
        change_str = f"{r['change_pct']:+.2f}%"
        print(f"{emoji} {change_str:>7}  {r['code']:>6}  {r['name']:<8}  {r['price']:.2f}")

    print("─────────────────────────────")
    avg_change = round(sum(r["change_pct"] for r in results) / len(results), 2)
    avg_str = f"{avg_change:+.2f}%"
    print(f"持仓 {len(results)} 只 · 平均涨跌幅 {avg_str}")


def cmd_sector():
    """📊 按 Serenity 板块分组展示所有候选标的"""
    from datetime import datetime

    stocks = load_all_stocks()
    active_codes = {s["code"] for s in stocks if s["is_active"]}

    # 获取所有实时数据
    all_codes = list(CODE_SECTOR.keys())
    realtime_list = fetch_realtime(all_codes)
    rt_map = {item["code"]: item for item in realtime_list}

    now_str = datetime.now().strftime("%H:%M:%S")
    name_map = {s["code"]: s["name"] for s in stocks}

    print(f"📊 Serenity 板块分布 [{now_str}]")
    print("═══════════════════════════════")
    print()

    total_count = 0
    holding_count = 0
    sectors_with_holdings = set()

    for sec_name, sec_info in SECTORS.items():
        print(f"🔷 {sec_name}")
        has_holding_in_sector = False
        for code in sec_info["codes"]:
            total_count += 1
            rt = rt_map.get(code, {})
            price = rt.get("price", 0)
            close_y = rt.get("close_yesterday", 0)
            if close_y and close_y > 0:
                change_pct = round((price - close_y) / close_y * 100, 2)
            else:
                change_pct = 0.0

            name = name_map.get(code, STOCK_MAP.get(code, {}).get("name", code))
            is_active = code in active_codes
            prefix = "  "
            star = ""
            if is_active:
                prefix = "  "
                star = " ⭐买入"
                holding_count += 1
                has_holding_in_sector = True

            sign = "+" if change_pct >= 0 else ""
            print(f"{prefix}◉ {code} {name:<8} {price:<8.2f} {sign}{change_pct:.2f}%{star}")

        if has_holding_in_sector:
            sectors_with_holdings.add(sec_name)
        print()

    print("═══════════════════════════════")
    print(f"{total_count} 只标的 / {holding_count} 只持仓 / {len(sectors_with_holdings)} 个板块有持仓")


def cmd_serenity():
    """📡 查看 Serenity 最新建议（未读）"""
    from db import get_new_serenity_suggestions, acknowledge_all_serenity_suggestions, init_db
    init_db()  # 确保表存在

    suggestions = get_new_serenity_suggestions()
    if not suggestions:
        print("📡 Serenity 暂无新建议")
        return

    print(f"📡 Serenity 最新建议 ({len(suggestions)} 条未读)")
    print("═══════════════════════════════")
    for s in suggestions:
        emoji = "💲" if s["source"] == "ticker" else "🇨🇳"
        label = "新关注标的" if s["source"] == "ticker" else "中文思路"
        print(f"\n  {emoji} {s['content']:<20}  {label}  {s['created_at']}")
        if s["context"]:
            # 截取前 100 字符展示
            ctx = s["context"][:100]
            prefix = "推文" if s["source"] == "ticker" else "Chinese tweet"
            print(f"  → {prefix}: \"{ctx}\"")
    print()

    acknowledge_all_serenity_suggestions()
    print(f"✅ 已将 {len(suggestions)} 条建议标记为已读")


def cmd_info(code):
    """查询单只股票实时行情"""
    results = fetch_realtime([code])
    data = results[0] if results else None
    if not data:
        print(f"❌ 无效代码或暂无数据: {code}")
        return

    close_y = data.get("close_yesterday", 0)
    price = data.get("price", 0)
    if close_y and close_y > 0:
        change_pct = (price - close_y) / close_y * 100
    else:
        change_pct = 0.0

    sign = "+" if change_pct >= 0 else ""
    change_str = f"{sign}{change_pct:.2f}%"

    # 涨红跌绿
    color = "\033[31m" if change_pct >= 0 else "\033[32m"
    reset = "\033[0m"

    name = data.get("name", code)
    open_price = data.get("open", 0)
    high = data.get("high", 0)
    low = data.get("low", 0)

    print(f"{color}📊 {code} {name}{reset}")
    print(f"  最新价: {color}{price:.2f}{reset}")
    print(f"  涨跌幅: {color}{change_str}{reset}")
    print(f"  昨  收: {close_y:.2f}")
    print(f"  今  开: {open_price:.2f}")
    print(f"  最  高: {color}{high:.2f}{reset}")
    print(f"  最  低: {color}{low:.2f}{reset}")


def cmd_suggest():
    """💡 候选标的买入建议 — 基于Serenity评分 + 区间位置 + 动量"""
    from datetime import datetime

    stocks = load_all_stocks()
    candidates = [s for s in stocks if not s["is_active"]]

    if not candidates:
        print("📭 当前无候选标的（所有标的均已持仓）")
        return

    codes = [s["code"] for s in candidates]
    realtime_list = fetch_realtime(codes)
    rt_map = {item["code"]: item for item in realtime_list}

    results = []
    for stock in candidates:
        code = stock["code"]
        rt = rt_map.get(code, {})
        if not rt:
            continue

        price = rt.get("price", 0)
        close_y = rt.get("close_yesterday", 0)
        if close_y and close_y > 0:
            change_pct = round((price - close_y) / close_y * 100, 2)
        else:
            change_pct = 0.0

        detail = STOCK_DETAILS.get(code, {})
        serenity_score = detail.get("score", 0)
        buy_low = detail.get("buy_zone_low", 0)
        buy_high = detail.get("buy_zone_high", 0)
        reason = detail.get("reason", "")
        target_sell = detail.get("target_sell", 0)

        # 区间评分：价格越靠近/低于买入区，得分越高
        if price <= 0 or buy_low <= 0:
            zone_score = 0
            zone_label = "数据缺失"
        elif price < buy_low:
            zone_score = 100
            zone_label = f"低于买入区({buy_low:.0f})"
        elif price <= buy_high:
            ratio = (price - buy_low) / (buy_high - buy_low) if buy_high > buy_low else 0
            zone_score = round(100 - ratio * 30)
            zone_label = "买入区内"
        elif price <= buy_high * 1.1:
            zone_score = 50
            zone_label = "略高于买入区"
        elif price <= buy_high * 1.3:
            zone_score = 30
            zone_label = "高于买入区"
        else:
            zone_score = 10
            zone_label = "远超买入区"

        # 动量评分：下跌越深越可能是买入机会
        if change_pct <= -3:
            momentum_score = 100
        elif change_pct <= -1:
            momentum_score = 80
        elif change_pct <= 0:
            momentum_score = 60
        elif change_pct <= 2:
            momentum_score = 40
        elif change_pct <= 5:
            momentum_score = 20
        else:
            momentum_score = 5

        # 综合评分：Serenity适配50% + 区间位置30% + 动量机会20%
        total_score = round(serenity_score * 0.5 + zone_score * 0.3 + momentum_score * 0.2, 1)

        results.append({
            "code": code,
            "name": stock["name"],
            "price": price,
            "change_pct": change_pct,
            "serenity_score": serenity_score,
            "zone_score": zone_score,
            "zone_label": zone_label,
            "momentum_score": momentum_score,
            "total_score": total_score,
            "reason": reason,
            "buy_zone": f"{buy_low:.0f}-{buy_high:.0f}",
            "target_sell": target_sell,
        })

    if not results:
        print("⚠️ 无法获取候选标的实时数据")
        return

    # 按综合评分降序排列
    results.sort(key=lambda x: x["total_score"], reverse=True)

    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"💡 Serenity 候选买入建议 [{now_str}]")
    print("═══════════════════════════════════════════")

    for i, r in enumerate(results, 1):
        if i == 1:
            emoji = "🥇"
        elif i == 2:
            emoji = "🥈"
        elif i == 3:
            emoji = "🥉"
        else:
            emoji = f"#{i}"
        sign = "+" if r["change_pct"] >= 0 else ""

        print(f"\n{emoji} {r['name']}({r['code']}) — 综合评分 {r['total_score']:.0f}")
        print(f"   现价: {r['price']:.2f} | 涨跌: {sign}{r['change_pct']:.2f}%")
        print(f"   Serenity适配: {r['serenity_score']}/100 | 区间: {r['zone_label']} | 买入区: {r['buy_zone']}")
        print(f"   目标卖出: {r['target_sell']:.0f} | 推荐理由: {r['reason']}")

    print(f"\n═══════════════════════════════════════════")
    print(f"共 {len(results)} 只候选标的 | 评分权重: Serenity 50% + 区间 30% + 动量 20%")


def cmd_q():
    """微信查询命令 — 紧凑格式"""
    if len(sys.argv) >= 3:
        code = sys.argv[2]
        _quick_query(code)
    else:
        _portfolio_overview()


def _quick_query(code: str):
    """快速查询单只标的，紧凑格式"""
    # 获取实时行情
    data = fetch_single(code)
    if not data:
        print(f"❌ 无法获取 {code} 行情数据")
        return

    name = STOCK_MAP.get(code, {}).get("name", data.get("name", code))
    price = data.get("price", 0)
    close_y = data.get("close_yesterday", 0)
    change_pct = round((price - close_y) / close_y * 100, 2) if close_y else 0
    change_emoji = "🟢" if change_pct >= 0 else "🔴"

    # 评级
    try:
        rating_info = get_rating(code)
        rating = rating_info["rating"]
        rating_emoji = rating_info["rating_emoji"]
        signal_label = rating_info["signal_label"]
        signal_emoji = rating_info["signal_emoji"]
        composite = rating_info["score"]
        factor_signal = rating_info["factors"]["factor_signal"]
        fund_signal = rating_info["factors"]["fundamental_signal"]
    except (KeyError, TypeError, ValueError):
        rating = "N/A"
        rating_emoji = "❓"
        signal_label = "N/A"
        signal_emoji = "⚪"
        composite = 0
        factor_signal = 0
        fund_signal = 0

    # 基本面PE/PB
    try:
        from fundamental_engine import FundamentalEngine
        fe = FundamentalEngine()
        pe, pb = fe.compute_pe_pb(code)
        fin = fe.get_financials(code)
        roe = fin.get("roe", "N/A") if fin else "N/A"
        pe_str = f"{pe:.1f}" if pe else "N/A"
        pb_str = f"{pb:.1f}" if pb else "N/A"
        roe_str = f"{roe:.1f}%" if isinstance(roe, (int, float)) else "N/A"
    except (TypeError, ValueError):
        pe_str = pb_str = roe_str = "N/A"

    # 第一行
    print(f"🔍 {name}({code}) | 评级:{rating_emoji}{rating}")
    # 第二行
    chg_str = f"{change_pct:+.2f}%" if change_pct != 0 else "0.00%"
    fund_str = f"{fund_signal:+.2f}" if fund_signal is not None else "N/A"
    print(f"📈 现价:{price:.2f} | 信号:{signal_emoji}{signal_label} | 因子:{factor_signal:+.2f} | 基本面:{fund_str}")
    # 第三行
    print(f"PE:{pe_str} PB:{pb_str} ROE:{roe_str} | {change_emoji}{chg_str}")


def _portfolio_overview():
    """持仓总览，紧凑格式"""
    from db import load_all_stocks
    stocks = load_all_stocks()
    active = [s for s in stocks if s.get("is_active")]

    if not active:
        print("📭 当前无持仓")
        return

    # 获取实时行情
    codes = [s["code"] for s in active]
    realtime = fetch_realtime(codes)
    rt_map = {r["code"]: r for r in realtime}

    # 构建每只仓位描述
    parts = []
    total_chg = 0
    for s in active:
        code = s["code"]
        rt = rt_map.get(code, {})
        price = rt.get("price", 0)
        close_y = rt.get("close_yesterday", 0)
        chg = round((price - close_y) / close_y * 100, 2) if close_y else 0
        total_chg += chg
        emoji = "🟢" if chg >= 0 else "🔴"
        parts.append(f"{s['name']}{emoji}{chg:+.1f}%")

    avg_chg = round(total_chg / len(active), 1)

    # 组合评级
    try:
        pr = get_portfolio_rating()
        overall_rating = pr["overall_rating"]
        pr_score = pr["overall_score"]
    except Exception:
        overall_rating = "N/A"
        pr_score = 0

    # 大盘趋势
    try:
        ms = get_market_signal()
        trend_emoji = "📈" if ms["overall_trend"] == "多头" else "📉" if ms["overall_trend"] == "空头" else "➡️"
        market_info = f"{trend_emoji}{ms['overall_trend']}"
    except Exception:
        market_info = "➡️未知"

    print(f"📊 持仓: {' | '.join(parts)}")
    print(f"综合评级: {overall_rating} | 均涨幅: {avg_chg:+.1f}% | 大盘: {market_info}")


def cmd_q_rank():
    """候选排名（按综合得分）"""
    try:
        ranks = get_candidate_rank()
    except Exception as e:
        print(f"❌ 获取候选排名失败: {e}")
        return

    if not ranks:
        print("📭 暂无候选标的")
        return

    print("🏆 候选排名")
    pairs = []
    for i, r in enumerate(ranks, 1):
        pairs.append(f"{i}. {r['name']} {r['score']:.2f}{r['rating_emoji']}")

    # 每行2个，用 | 分隔
    for i in range(0, len(pairs), 2):
        if i + 1 < len(pairs):
            print(f"{pairs[i]} | {pairs[i+1]}")
        else:
            print(pairs[i])


def cmd_market():
    """大盘择时信号"""
    try:
        signal = get_market_signal()
    except Exception as e:
        print(f"❌ 获取大盘信号失败: {e}")
        return

    sh = signal["sh"]
    hs300 = signal["hs300"]

    print(f"📊 大盘择时 | {signal['overall_signal']}")
    print(f"趋势:{signal['overall_trend']} | RSI均:{signal['avg_rsi']} | {signal['overall_advice']}")
    print(f"───")
    if sh.get("status") != "数据不足":
        print(f"沪|{sh['last_close']} MA20/{sh['ma20']:.0f} MA60/{sh['ma60']:.0f} "
              f"RSI{sh['rsi']}({sh['rsi_status']}) {sh['trend']} {sh.get('signals','')}")
    if hs300.get("status") != "数据不足":
        print(f"HS|{hs300['last_close']} MA20/{hs300['ma20']:.0f} MA60/{hs300['ma60']:.0f} "
              f"RSI{hs300['rsi']}({hs300['rsi_status']}) {hs300['trend']} {hs300.get('signals','')}")
    print(f"───")
    print(f"量能: 沪{sh.get('volume_trend','N/A')} HS{hs300.get('volume_trend','N/A')}")


def cmd_sector_rotation(detailed: bool = False):
    """📊 行业轮动扫描报告"""
    from sector_rotation import get_sector_rotation_summary, get_sector_rotation_detail
    try:
        if detailed:
            result = get_sector_rotation_detail()
        else:
            result = get_sector_rotation_summary()
        print(result)
    except Exception as e:
        print(f"❌ 行业轮动扫描失败: {e}")


def cmd_multi_cycle_factors(code: str):
    """🅱 三周期因子对比矩阵（日线/周线/月线）"""
    from factor_engine import AlphaFactorEngine, SIGNAL_FACTORS, FACTOR_NAMES, FACTOR_EMOJIS
    engine = AlphaFactorEngine()
    mcf = engine.compute_multi_cycle_factors(code)

    if not mcf.get("daily"):
        print(f"⚠️ {code} 数据不足，无法计算三周期因子")
        return

    name = STOCK_MAP.get(code, {}).get("name", code)
    print(f"🅱 三周期因子对比矩阵 | {code} {name}")
    print("═══════════════════════════════════════════════════════════════════")
    print()

    # 表头
    print(f"{'因子':>14}  {'日线':>8}  {'周线':>8}  {'月线':>8}  {'趋势':>6}")
    print("───────────────────────────────────────────────────────────────────")

    cycles = ["daily", "weekly", "monthly"]
    for fname in SIGNAL_FACTORS:
        emoji = FACTOR_EMOJIS.get(fname, "📊")
        label = FACTOR_NAMES.get(fname, fname)
        dv = mcf["daily"].get(fname)
        wv = mcf["weekly"].get(fname)
        mv = mcf["monthly"].get(fname)

        d_str = f"{emoji}{dv:+.3f}" if dv is not None else "  --"
        w_str = f"{wv:+.3f}" if wv is not None else "  --"
        m_str = f"{mv:+.3f}" if mv is not None else "  --"

        # 趋势判断：三个信号中 >0 的数量
        vals = [v for v in [dv, wv, mv] if v is not None]
        if len(vals) == 3:
            bullish = sum(1 for v in vals if v > 0.05)
            bearish = sum(1 for v in vals if v < -0.05)
            if bullish >= 2:
                trend = "🟢多"
            elif bearish >= 2:
                trend = "🔴空"
            else:
                trend = "⚪震荡"
        else:
            trend = "  --"

        print(f"  {label:>12}  {d_str:>8}  {w_str:>8}  {m_str:>8}  {trend:>6}")

    # 底部综合信号
    print("───────────────────────────────────────────────────────────────────")

    def _avg_signal(d):
        vals = [float(v) for v in d.values() if v is not None]
        return sum(vals) / len(vals) if vals else 0.0

    avg_d = _avg_signal(mcf["daily"])
    avg_w = _avg_signal(mcf["weekly"])
    avg_m = _avg_signal(mcf["monthly"])
    fused = avg_d * 0.40 + avg_w * 0.40 + avg_m * 0.20
    fused_score = max(0, min(100, 50 + fused * 2))

    print(f"  {'综合信号':>12}  {avg_d:>+.3f}  {avg_w:>+.3f}  {avg_m:>+.3f}")
    print(f"  {'三周期融合分':>12}  {fused_score:.1f}/100")
    print()
    print("  权重: 日线40% + 周线40% + 月线20%")
    print(f"  日线信号数: {len(mcf['daily'])} | 周线: {len(mcf['weekly'])} | 月线: {len(mcf['monthly'])}")
    print("═══════════════════════════════════════════════════════════════════")


def cmd_factor_report():
    """📊 因子归因日报 — IC趋势/衰减/有效因子排名"""
    code = sys.argv[2] if len(sys.argv) > 2 else None
    report = generate_factor_report(code=code)
    print(report)


def cmd_factor_decay():
    """⚠️ 因子衰减检测 — 短期IC vs 长期IC"""
    code = sys.argv[2] if len(sys.argv) > 2 else None
    decay = detect_factor_decay(code=code)
    for d in decay:
        if d["status"] == "衰减 ⚠️":
            emoji = FACTOR_EMOJIS.get(d["factor"], "")
            print(f"⚠️ {emoji} {d['label']:<6} 短期IC:{d['short_ic']:+.3f} 长期IC:{d['long_ic']:+.3f}")
            print(f"   → {d['recommend']}")
    active_decay = [d for d in decay if d["status"] == "衰减 ⚠️"]
    if not active_decay:
        print("✅ 所有因子状态正常，未检测到衰减")


def cmd_optimize_atr(code: str):
    """🅴 ATR 止损参数优化"""
    from backtest_engine import optimize_atr_params, format_optimize_result
    print(f"🔄 ATR 参数优化: {code} ...")
    results = optimize_atr_params(code)
    if not results:
        print(f"⚠️ {code} 无有效回测结果（数据不足或无交易）")
        return
    print(format_optimize_result(results))


def cmd_stop_track(code: str):
    """🅴 止损有效性分析"""
    from backtest_engine import track_stop_loss_effectiveness, format_stop_track_result
    print(f"🔄 止损有效性分析: {code} ...")
    result = track_stop_loss_effectiveness(code)
    print(format_stop_track_result(result))


def cmd_optimize_atr_all():
    """🅴 所有标的 ATR 参数优化"""
    from backtest_engine import recommend_atr_params, format_recommend_result
    print("🔄 全标的 ATR 参数优化 ...")
    recommendations = recommend_atr_params()
    if not recommendations:
        print("⚠️ 无有效优化结果")
        return
    print(format_recommend_result(recommendations))


def cmd_alerts_push():
    """主动信号推送 — 检测三因子共振/三重确认/因子突变/极端信号"""
    from signal_alert import check_all_alerts, ALERT_TYPE_CN
    pushed = check_all_alerts()
    if not pushed:
        print("📭 无触发信号，无需推送")
        return
    print(f"📤 信号推送结果 ({len(pushed)} 条):")
    print("=" * 60)
    for p in pushed:
        status = "✅" if p.get("pushed") else "❌"
        err = f" | {p.get('error', '')}" if p.get("error") else ""
        atype = ALERT_TYPE_CN.get(p["alert_type"], p["alert_type"])
        print(f"  {status} [{atype}] {p['name']}({p['code']}): {p['summary']}{err}")
    print("=" * 60)


def cmd_dividend():
    """红利低波评分 — 高股息标的四维评分（股息/低波/估值/质量）"""
    de = DividendEngine()
    results = de.score_all()
    print(f"📊 红利低波评分 | {date.today()}")
    print("=" * 60)
    for r in results:
        print(f"{r['name']:6s} | 总分 {r['total_score']:.1f} | "
              f"股息{r['dividend_yield_score']:.0f} "
              f"低波{r['low_vol_score']:.0f} "
              f"估值{r['valuation_score']:.0f} "
              f"质量{r['quality_score']:.0f}")


def cmd_etf():
    """ETF 动量轮动 — 10只 ETF 三周期动量排名"""
    ems = ETFMomentumStrategy()
    ranks = ems.rank_all()
    print(f"📊 ETF 动量轮动排名 | {date.today()}")
    print("=" * 70)
    for r in ranks:
        print(f"#{r['rank']} {r['name']:6s} | 总分 {r['total_score']:.1f} | "
              f"短{r['momentum_short']:.0f} 中{r['momentum_medium']:.0f} "
              f"长{r['momentum_long']:.0f} 趋势{r['trend_strength']:.0f}")

def cmd_health():
    """🔬 系统健康诊断"""
    from health_check import main as health_main
    health_main()

def cmd_tier1_reentry():
    """🔄 T1 回补检查 — Tier 1 标的回调至买入区提醒"""
    from tier1_reentry import check_tier1_reentry, cmd_status
    do_push = "--push" in sys.argv
    do_status = "--status" in sys.argv
    if do_status:
        cmd_status()
    else:
        results = check_tier1_reentry(push=do_push)
        if results:
            print(f"\n✅ {len(results)} 个回补提醒")
        else:
            print("\n✅ 暂无回补机会")

def cmd_strategy():
    """查看当前策略配置（红利/ETF动量/量化因子）"""
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    print("📊 当前策略配置")
    print("=" * 60)
    # 策略分配
    cur.execute("SELECT * FROM strategy_allocation ORDER BY alloc_date DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        print(f"  配置日期: {row['alloc_date']}")
        print(f"  红利低波: {row['dividend_weight']*100:.0f}%")
        print(f"  量化因子: {row['quant_weight']*100:.0f}%")
        print(f"  ETF动量: {row['etf_weight']*100:.0f}%")
        print(f"  市场阶段: {row['market_regime'] if 'market_regime' in row.keys() else 'N/A'}")
    else:
        print("  ⚠️ 策略分配表为空")

    # 红利/ETF 名称映射
    DIVIDEND_NAMES = {
        "600585": "海螺水泥", "601088": "中国神华", "600036": "招商银行",
        "601398": "工商银行", "600900": "长江电力", "601857": "中国石油",
        "600019": "宝钢股份", "601006": "大秦铁路"
    }
    ETF_NAMES = {
        "510300": "沪深300", "510050": "上证50", "510500": "中证500",
        "159915": "创业板", "159995": "芯片ETF", "516160": "新能源ETF",
        "512100": "中证1000", "512880": "证券ETF", "512690": "酒ETF",
        "513100": "纳指ETF"
    }

    # 红利 Top 3（取每只标的最新评分，去重）
    print("\n--- 红利低波 Top 3 ---")
    cur.execute("""
        SELECT code, MAX(total_score) as total_score, MAX(score_date) as score_date
        FROM dividend_scores
        GROUP BY code
        ORDER BY total_score DESC LIMIT 3
    """)
    for r in cur.fetchall():
        name = DIVIDEND_NAMES.get(r['code'], r['code'])
        print(f"  {name:6s} | {r['total_score']:.0f}分 | {r['score_date']}")

    # ETF Top 3（取每只最新，去重）
    print("\n--- ETF 动量 Top 3 ---")
    cur.execute("""
        SELECT etf_code, MAX(total_score) as total_score, MIN(rank) as rank,
               MAX(score_date) as score_date
        FROM etf_scores
        GROUP BY etf_code
        ORDER BY rank LIMIT 3
    """)
    for r in cur.fetchall():
        name = ETF_NAMES.get(r['etf_code'], r['etf_code'])
        print(f"  #{r['rank']} {name:8s} | {r['total_score']:.0f}分 | {r['score_date']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    commands = {
        "init": lambda: cmd_init(force="--force" in sys.argv),
        "status": lambda: cmd_status(),
        "report": lambda: cmd_report(),
        "llm-report": lambda: cmd_llm_report(),
        "conviction": lambda: cmd_conviction(),
        "analyze": lambda: cmd_analyze_discount(sys.argv[2] if len(sys.argv) > 2 else ""),
        "top": lambda: cmd_top(),
        "list": lambda: cmd_list(),
        "alerts": lambda: cmd_alerts(),
        "trades": lambda: cmd_trades(sys.argv[2] if len(sys.argv) > 2 else ""),
        "ack": lambda: cmd_ack(int(sys.argv[2])),
        "rescore": lambda: cmd_rescore(),
        "monitor": lambda: cmd_monitor(),
        "msummary": lambda: cmd_monitor_summary(),
        "sector": lambda: cmd_sector(),
        "serenity": lambda: cmd_serenity(),
        "suggest": lambda: cmd_suggest(),
        "portfolio": lambda: cmd_portfolio(),
        "signal": lambda: cmd_signal(),
        "compare": lambda: cmd_compare(),
        "backtest-factors": lambda: cmd_backtest_factor(sys.argv[2] if len(sys.argv) > 2 else ""),
        "backtest-viz": lambda: cmd_backtest_viz(sys.argv[2] if len(sys.argv) > 2 else ""),
        "backtest-report": lambda: cmd_backtest_report(sys.argv[2] if len(sys.argv) > 2 else ""),
        "backtest-compare": lambda: cmd_backtest_comparison(),
        "test-push": lambda: cmd_test_push(),
        "push-report": lambda: cmd_push_report(),
        "push-guide": lambda: cmd_push_guide(),
        "dashboard": lambda: cmd_dashboard(),
        "factors": lambda: cmd_factors(),
        "adjust-weights": lambda: cmd_adjust_weights(),
        "show-weights": lambda: cmd_show_weights(),
        "rebalance": lambda: cmd_rebalance(),
        "factor-monitor": lambda: cmd_factor_monitor(),
        "push-rebalance": lambda: cmd_push_rebalance(),
        "q": lambda: cmd_q(),
        "q-rank": lambda: cmd_q_rank(),
        "market": lambda: cmd_market(),
        "sector-rotation": lambda: cmd_sector_rotation("--all" in sys.argv),
        "multi-cycle-factors": lambda: cmd_multi_cycle_factors(sys.argv[2] if len(sys.argv) > 2 else ""),
        "factor-report": lambda: cmd_factor_report(),
        "factor-decay": lambda: cmd_factor_decay(),
        "alerts-push": lambda: cmd_alerts_push(),
        "optimize-atr": lambda: cmd_optimize_atr(sys.argv[2] if len(sys.argv) > 2 else ""),
        "stop-track": lambda: cmd_stop_track(sys.argv[2] if len(sys.argv) > 2 else ""),
        "optimize-atr-all": lambda: cmd_optimize_atr_all(),
        "scan-candidates": lambda: cmd_scan_candidates(),
        "suggest-stock": lambda: cmd_suggest_stock(),
        "signal-perf": lambda: cmd_signal_performance(),
        "weekly-review": lambda: cmd_weekly_review(),
        "check-anomalies": lambda: cmd_check_anomalies(),
        "perf-report": lambda: cmd_perf_report(),
        "factor-interpret": lambda: cmd_factor_interpret(),
        "scan-mainboard": lambda: cmd_scan_mainboard(),
        "sync-log": lambda: cmd_sync_log(),
        "trade-record": lambda: cmd_trade_record(sys.argv[2:]),
        "trade-log": lambda: cmd_trade_log(sys.argv[2:]),
        "dividend": cmd_dividend,
        "etf": cmd_etf,
        "strategy": cmd_strategy,
        "signal-push": cmd_signal_push,
        "factor-ic": cmd_factor_ic,
        "perf-attr": cmd_perf_attr,
        "reflection": cmd_reflection,
        "reflection-ic": cmd_reflection_ic,
        "reflection-apply": cmd_reflection_apply,
        "council": cmd_council,
        "council-report": cmd_council_report,
        "health": cmd_health,
        "tier1-reentry": cmd_tier1_reentry,
        "auto": lambda: cmd_auto_execute(),
        "auto-exec": lambda: (__import__('sys').argv.append('--force-execute'), cmd_auto_execute())[1],
        "auto-stats": lambda: (__import__('sys').argv.append('--stats'), cmd_auto_execute())[1],
        "auto-premarket": lambda: (__import__('sys').argv.append('--premarket'), cmd_auto_execute())[1],
        "auto-push": lambda: (__import__('sys').argv.append('--push'), cmd_auto_execute())[1],
        "backtest-quick": lambda: quick_backtest.main(),
        "workflow": lambda: (__import__('daily_workflow').main()),
        "advise": lambda: (__import__('portfolio_advisor').main()),
        "dash": lambda: cmd_dash_dashboard(),
    }

    if cmd in commands:
        commands[cmd]()
    elif cmd == "info" and len(sys.argv) >= 3:
        cmd_info(sys.argv[2])
    elif cmd == "buy" and len(sys.argv) >= 4:
        amount = float(sys.argv[4]) if len(sys.argv) >= 5 else 0
        cmd_buy(sys.argv[2], float(sys.argv[3]), amount)
    elif cmd == "sell" and len(sys.argv) >= 3:
        cmd_sell(sys.argv[2])
    elif cmd == "target" and len(sys.argv) >= 4:
        low = float(sys.argv[4]) if len(sys.argv) >= 5 else 0
        cmd_target(sys.argv[2], float(sys.argv[3]), low)
    elif cmd == "buy-auto" and len(sys.argv) >= 3:
        amount = float(sys.argv[3]) if len(sys.argv) >= 4 else 0
        cmd_buy_auto(sys.argv[2], amount)
    elif cmd == "buy-auto":
        cmd_buy_auto()
    elif cmd == "sell-auto" and len(sys.argv) >= 3:
        reason = sys.argv[3] if len(sys.argv) >= 4 else "信号触发"
        cmd_sell_auto(sys.argv[2], reason)
    elif cmd == "sell-auto":
        cmd_sell_auto()
    elif cmd == "backtest" and len(sys.argv) >= 3:
        strategy = sys.argv[3] if len(sys.argv) >= 4 else "hybrid"
        cmd_backtest(sys.argv[2], strategy)
    elif cmd == "viz-report" and len(sys.argv) >= 3:
        cmd_viz_report(sys.argv[2])
    elif cmd == "backtest":
        cmd_backtest()
    elif cmd == "trade" and len(sys.argv) >= 4:
        amount = float(sys.argv[4]) if len(sys.argv) >= 5 else 0
        cmd_trade(sys.argv[2], sys.argv[3], amount)
    elif cmd == "stop" and len(sys.argv) >= 4:
        cmd_stop(sys.argv[2], float(sys.argv[3]))
    elif cmd == "check" and len(sys.argv) >= 3:
        cmd_check(sys.argv[2])
    else:
        print(f"未知命令或参数不足: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
