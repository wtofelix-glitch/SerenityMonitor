"""
Serenity 2.0 CLI — 统一命令行入口 [v2.1 环境隔离版]

所有命令必须先通过环境初始化。
影子模式命令统一经过 ShadowRunner，不允许绕过。

用法:
    # 影子模式（默认）
    python -m serenity_v2.cli shadow run                # 影子运行
    python -m serenity_v2.cli shadow run --offline-replay  # 离线回放
    python -m serenity_v2.cli shadow verify             # 验证隔离
    python -m serenity_v2.cli shadow status             # 影子状态

    # 账户（需先设置环境）
    python -m serenity_v2.cli --env shadow account status
    python -m serenity_v2.cli --env shadow account bootstrap
    python -m serenity_v2.cli --env shadow account fill ...
    python -m serenity_v2.cli --env shadow account risk

    # 情报（影子模式）
    python -m serenity_v2.cli --env shadow intel brief
    python -m serenity_v2.cli --env shadow intel check CODE CHG%

    # 信号（影子模式）
    python -m serenity_v2.cli --env shadow signal scan

    # 复盘
    python -m serenity_v2.cli --env shadow review today

环境安全规则:
    · --env shadow: 必须经过 ShadowIsolation 验证
    · --env production: 必须显式传入推送凭证
    · 不传 --env: 拒绝运行（无默认值）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================================
# 环境解析（必须最先执行）
# ============================================================================

def _resolve_env(args: list[str]) -> tuple[str, list[str]]:
    """
    解析 --env 参数。返回 (mode, remaining_args)。
    未指定 --env 时拒绝运行。
    """
    mode = None
    remaining = []
    i = 0
    while i < len(args):
        if args[i] == "--env" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1

    if mode is None:
        print("错误: 必须指定 --env shadow 或 --env production")
        print("示例: python -m serenity_v2.cli --env shadow account status")
        sys.exit(1)

    if mode not in ("shadow", "production"):
        print(f"错误: 无效环境模式 '{mode}'，必须为 shadow 或 production")
        sys.exit(1)

    return mode, remaining


def _init_shadow_env():
    """初始化影子环境。"""
    from serenity_v2.env import SerenityEnv, set_env

    env = SerenityEnv.shadow()
    set_env(env)

    # 打印启动信息
    print(f"🔒 Serenity 2.0 影子模式")
    print(f"   DB: {env.db_path.resolve()}")
    print(f"   Log: {env.log_dir.resolve()}")
    print(f"   推送: 已禁用")
    print(f"   环境ID: {env.env_id}")
    print()
    return env


def _init_production_env():
    """初始化生产环境。"""
    from serenity_v2.env import SerenityEnv, set_env

    print("⚠️  生产模式尚未实现推送/券商适配器注入")
    print("   当前仅支持影子模式。")
    sys.exit(1)


# ============================================================================
# 影子子命令
# ============================================================================

def cmd_shadow(args: list[str], env):
    """影子模式专用命令。"""
    from serenity_v2.shadow_runner import (
        ShadowRunner, ShadowIsolation, offline_replay, verify_invariants,
    )

    if not args or args[0] == "run":
        # 交互式影子运行
        print(ShadowIsolation.verify_and_report())
        print()

        runner = ShadowRunner()
        state = runner.baseline.load_latest()
        if state.total_assets <= 0:
            state = runner.baseline.bootstrap_from_doc_b()
            runner.baseline.save_snapshot(state)

        print("账户基线:")
        print(runner.baseline.summary(state))
        print()

        test_feed = [
            {"code": "600487", "name": "亨通光电", "price": 57.98, "change_pct": 5.2,
             "volume": 2500000, "amount": 1.5e9, "turnover": 7.5},
            {"code": "000988", "name": "华工科技", "price": 108.40, "change_pct": -4.5,
             "volume": 1800000, "amount": 2.0e9, "turnover": 5.8},
            {"code": "600176", "name": "中国巨石", "price": 40.20, "change_pct": 3.9,
             "volume": 5000000, "amount": 2.0e9, "turnover": 8.2},
        ]

        report = runner.run_cycle(price_feed=test_feed)

        print(f"事件: {report['ingested_count']} | 信号: {report['signal_count']}")
        if report["signals"]:
            for s in report["signals"]:
                print(f"\n{s['formatted']}")
                push = "Yes" if s["should_push"] else "No"
                print(f"  推送: {push} {s['push_reason']}")
        else:
            print("无候选信号")

        print(f"\n{'='*60}")
        print("  不变量验证")
        for inv in verify_invariants(runner):
            icon = "OK" if inv["ok"] else "FAIL"
            print(f"  {icon} {inv['name']}: {inv['detail']}")

    elif args[0] == "offline-replay" or "--offline-replay" in args:
        offline_replay()

    elif args[0] == "verify":
        print(ShadowIsolation.verify_and_report())

    elif args[0] == "status":
        from serenity_v2.shadow_runner import ShadowRunner
        runner = ShadowRunner()
        status = runner.status()
        print("影子环境状态:")
        for k, v in status.items():
            print(f"  {k}: {v}")

    else:
        print(f"未知影子子命令: {args[0]}")
        print("可用: run, offline-replay, verify, status")


# ============================================================================
# 账户命令
# ============================================================================

def cmd_account(args: list[str]) -> None:
    from serenity_v2.account_baseline import AccountBaseline, get_baseline, reset_baseline

    baseline = get_baseline()

    if not args or args[0] == "status":
        state = baseline.load_latest()
        if state.total_assets <= 0:
            print("账户基线未初始化")
            print("  运行: python -m serenity_v2.cli --env shadow account bootstrap")
            return
        print(baseline.summary(state))
        print(f"\n快照时间: {state.snapshot_at}")
        print(f"数据来源: {state.data_source} | 置信度: {state.data_confidence}")

    elif args[0] == "bootstrap":
        state = baseline.bootstrap_from_doc_b()
        row_id = baseline.save_snapshot(state)
        print(f"账户基线已初始化 (row_id={row_id})")
        print(baseline.summary(state))

    elif args[0] == "risk":
        alerts = baseline.check_risk()
        if not alerts:
            print("无风险告警")
            return
        for a in alerts:
            print(f"{a['severity']} {a['rule']}: {a['detail']}")
            if a.get("suggestion"):
                print(f"   -> {a['suggestion']}")

    elif args[0] == "fill":
        if len(args) < 5:
            print("用法: account fill <code> <buy|sell> <price> <quantity> [--ext-id ID] [note]")
            print("示例: account fill 600487 buy 55.23 1500 --ext-id FILL_001 亨通建仓")
            return

        code = args[1]
        side = args[2]
        price = float(args[3])
        quantity = int(args[4])

        # 解析可选参数
        ext_id = ""
        remaining_args = args[5:]
        i = 0
        while i < len(remaining_args):
            if remaining_args[i] == "--ext-id" and i + 1 < len(remaining_args):
                ext_id = remaining_args[i + 1]
                i += 2
            else:
                i += 1
        note = " ".join(a for a in remaining_args
                        if not a.startswith("--") and a != ext_id)

        result = baseline.fill_trade(
            code, side, price, quantity,
            note=note, external_fill_id=ext_id,
        )
        if result["success"]:
            print(f"回填成功: {side} {code} {quantity}股@{price}")
            if result.get("message"):
                print(f"  {result['message']}")
            print(baseline.summary(result["state"]))
        else:
            print(f"回填失败: {result['message']}")

    elif args[0] == "context":
        if len(args) < 2:
            print("用法: account context <code>")
            return
        ctx = baseline.signal_context(args[1])
        for k, v in ctx.items():
            print(f"  {k}: {v}")

    elif args[0] == "audit":
        records = baseline.audit(days=30)
        if not records:
            print("无审计记录")
            return
        for r in records:
            print(f"  {r['snapshot_at'][:19]} | 总资产{r['total_assets']:,.0f} | "
                  f"现金{r['cash']:,.0f} | 仓位{r['position_ratio_pct']:.1f}%")

    elif args[0] == "settle":
        result = baseline.settle_t1()
        print(f"T+1结算: {result['message']}")

    else:
        print(f"未知子命令: {args[0]}")
        print("可用: status, bootstrap, risk, fill, context, audit, settle")


# ============================================================================
# 情报命令
# ============================================================================

def cmd_intel(args: list[str]) -> None:
    from serenity_v2.intelligence_network import IntelligenceNetwork, get_intel, check_thresholds

    intel = get_intel()

    if not args or args[0] == "brief":
        brief = intel.daily_brief()
        if not brief:
            print("当日无高优先级情报")
            return
        for b in brief:
            icon = {"P0": "P0", "P1": "P1", "P2": "P2"}.get(b["priority"], "  ")
            print(f"{icon} [{b['priority']}] {b['symbol']} {b['headline']} | "
                  f"{b['direction']}/{b['strength']} | {b['time'][:19]}")

    elif args[0] == "check":
        if len(args) < 3:
            print("用法: intel check <code> <change_pct> [volume_ratio] [turnover]")
            return
        code = args[1]
        change_pct = float(args[2])
        volume_ratio = float(args[3]) if len(args) > 3 else 1.0
        turnover = float(args[4]) if len(args) > 4 else 0

        result = check_thresholds(code, change_pct, volume_ratio, turnover)
        print(f"标的 {code}: level={result['level']}")
        for r in result["reasons"]:
            print(f"  -> {r}")

    elif args[0] == "ingest":
        if len(args) < 5:
            print("用法: intel ingest <code> <name> <price> <change_pct> [holding=1|0]")
            return
        code = args[1]
        name = args[2]
        price = float(args[3])
        change_pct = float(args[4])
        is_holding = bool(int(args[5])) if len(args) > 5 else True

        event = intel.ingest_price_anomaly(code, name, price, change_pct, is_holding=is_holding)
        print(f"摄入: {event.event_id} | priority={event.priority} | eligible={event.signal_eligible}")
        should, reason = intel.should_push(event, signal_type="price_anomaly")
        print(f"  推送: {'是' if should else '否'} ({reason})")


# ============================================================================
# 信号命令
# ============================================================================

def cmd_signal(args: list[str]) -> None:
    from serenity_v2.signal_desk import SignalDesk, get_desk, format_signal
    from serenity_v2.env import get_env

    env = get_env()
    desk = get_desk()

    if not args or args[0] == "scan":
        signals = desk.process_events()
        if not signals:
            print("无候选信号")
            return
        print(f"共 {len(signals)} 条信号:\n")
        for sig in signals:
            print(format_signal(sig))
            print()

    elif args[0] == "shadow":
        # [v2.1] shadow 子命令现在重定向到统一的 Shadow subcommand
        print("提示: 'signal shadow' 已被 'shadow run' 替代。")
        print("请使用: python -m serenity_v2.cli --env shadow shadow run")
        print()
        # 仍然执行，但经过环境验证
        from serenity_v2.shadow_runner import ShadowRunner, verify_invariants
        from datetime import datetime, timezone, timedelta

        runner = ShadowRunner()
        state = runner.baseline.load_latest()
        if state.total_assets <= 0:
            state = runner.baseline.bootstrap_from_doc_b()
            runner.baseline.save_snapshot(state)

        print("=" * 60)
        print("  Serenity 2.0 信号台 — 影子运行")
        print(f"  时间: {datetime.now(tz=timezone(timedelta(hours=8))).isoformat(timespec='seconds')}")
        print(f"  模式: SHADOW (不推送，仅供审查)")
        print(f"  DB: {env.db_path.resolve()}")
        print("=" * 60)

        test_feed = [
            {"code": "600487", "name": "亨通光电", "price": 57.98, "change_pct": 5.2,
             "volume": 2500000, "amount": 1.5e9, "turnover": 7.5},
        ]

        report = runner.run_cycle(price_feed=test_feed)
        if not report["signals"]:
            print("\n无候选信号")
            return

        action_count = sum(1 for s in report["signals"] if s["signal_level"] == "ACTION")
        watch_count = sum(1 for s in report["signals"] if s["signal_level"] == "WATCH")
        print(f"\n信号统计: ACTION={action_count} WATCH={watch_count}")

        for s in report["signals"]:
            print(f"\n{s['formatted']}")

        print(f"\n验收清单:")
        print(f"  [ ] 每条的触发依据是否可验证")
        print(f"  [ ] 失效条件是否明确")
        print(f"  [ ] 建议仓位是否未超限")
        print(f"  [ ] 数据时间戳是否在合理范围内")

        # 不变量
        print(f"\n{'='*60}")
        print("  不变量验证")
        for inv in verify_invariants(runner):
            icon = "OK" if inv["ok"] else "FAIL"
            print(f"  {icon} {inv['name']}: {inv['detail']}")

    elif args[0] == "accept":
        print("验收功能需要配合具体信号ID使用")
        print("  影子信号仅供审查，验收需人工确认后通过 CLI account fill 回填")


# ============================================================================
# 复盘命令
# ============================================================================

def cmd_review(args: list[str]) -> None:
    from serenity_v2.account_baseline import get_baseline
    from serenity_v2.event_record import EventStore

    baseline = get_baseline()
    store = EventStore()

    if not args or args[0] == "today":
        print("=" * 60)
        print("  Serenity 2.0 日终复盘")
        print("=" * 60)

        state = baseline.load_latest()
        print(f"\n账户收盘状态:")
        print(f"  总资产: {state.total_assets:,.0f}")
        print(f"  现金:   {state.available_cash:,.0f}")
        print(f"  仓位:   {state.position_ratio_pct:.1f}%")
        print(f"  浮动盈亏: {state.floating_pnl:+,.0f}")

        print(f"\n今日成交回填:")
        for t in state.today_trades:
            side_icon = "+" if t.side == "buy" else "-"
            print(f"  {side_icon} {t.side} {t.code} {t.quantity}股@{t.price} | {t.source}")

        events = store.query_recent(limit=10)
        p0p1 = [e for e in events if e.priority in ("P0", "P1")]
        print(f"\n今日高优先级情报: {len(p0p1)}条")
        for e in p0p1:
            print(f"  [{e.priority}] {e.symbol} {e.headline}")

        print(f"\n账户快照历史:")
        for r in baseline.audit(5):
            print(f"  {r['snapshot_at'][:19]} | {r['total_assets']:,.0f} | 现金{r['cash']:,.0f}")

    elif args[0] == "gap":
        state = baseline.load_latest()
        print("战斗卡回填检查:")
        print("  [ ] 各标的持仓与券商一致")
        print("  [ ] 今日成交已全部回填")
        print("  [ ] 现金余额对平")


# ============================================================================
# 主入口
# ============================================================================

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    # 1. 解析环境
    env_mode, remaining = _resolve_env(sys.argv[1:])

    # 2. 初始化环境
    if env_mode == "shadow":
        env = _init_shadow_env()
    else:
        env = _init_production_env()

    # 3. 路由命令
    if not remaining:
        print(__doc__)
        return

    cmd = remaining[0]
    cmd_args = remaining[1:]

    commands = {
        "shadow": lambda a: cmd_shadow(a, env),
        "account": cmd_account,
        "intel": cmd_intel,
        "signal": cmd_signal,
        "review": cmd_review,
    }

    if cmd in commands:
        commands[cmd](cmd_args)
    else:
        print(f"未知命令: {cmd}")
        print(f"可用: {', '.join(commands.keys())}")


if __name__ == "__main__":
    main()
