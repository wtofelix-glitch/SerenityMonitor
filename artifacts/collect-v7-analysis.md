# 测试收集差异分析 v7 (1090 vs 1259)

## 采集方法

```bash
# P0 工作树
cd /Users/mac/workspace/serenity-p0-env
python -m pytest --collect-only -q | grep "::" | sort > /tmp/p0_tests.txt  # 1090 nodes

# 主工作树
cd /Users/mac/workspace/SerenityMonitor
python -m pytest --collect-only -q | grep "::" | sort > /tmp/main_tests.txt  # 1259 nodes
```

## 集合论分解

```
P0 worktree (1090)        Main worktree (1259)
┌─────────────┐          ┌─────────────┐
│   165       │          │             │
│  P0-only    │          │   334       │
│ (5 files)   │   925    │ main-only   │
│             │  common  │ (35 files)  │
└─────────────┘          └─────────────┘

Net gap: 1259 - 1090 = (925+334) - (925+165) = 169
```

## P0-only: 5 个新文件 = 165 tests

| File | Tests | 来源 |
|------|-------|------|
| `test_prod_guard.py` | 43 | P0-1 生产路径保护 |
| `test_signal_idempotency.py` | 24 | P0-2 信号幂等 |
| `test_account_fixture.py` | 47 | P0-3 账户 Fixture |
| `test_p0_4_statistics.py` | 40 | P0-4 统计修复 |
| `test_replay_p0_combined.py` | 11 | P0 组合回放 |
| **合计** | **165** | |

## Main-only: 35 个已有文件中的新增测试 = 334 tests

这些都是 main 在 fork 点 (72ed83a9) 之后对已有文件增加的功能测试：

| 类别 | Tests | 所在文件 |
|------|-------|----------|
| QMT Agent + Bootstrap + 部署 | 100 | test_qmt_agent.py(61), test_qmt_bootstrap.py(15), test_qmt_agent_server.py(3), test_qmt_deployment_bundle.py(5), test_qmt_fill_sync.py(5), test_qmt_preflight.py(9), test_qmt_windows_install.py(2) |
| Debate Backtest + LLM | 39 | test_debate_backtest.py(18), test_debate_engine.py(9), test_debate_llm.py(12) |
| Factor Agents | 24 | test_factor_agents.py(24) |
| Sim Verification | 21 | test_sim_verification.py(21) |
| Decision Packet | 17 | test_decision_packet.py(17) |
| Paper Trader Lifecycle | 17 | test_paper_trader_lifecycle.py(17) |
| Trade Gateway (QMT 安全) | 16 | test_trade_gateway.py(16) |
| Micro Live Gate | 16 | test_micro_live_gate.py(16) |
| Daily Workflow Auto Gate | 11 | test_daily_workflow_auto_gate.py(11) |
| Freeze Experiment | 9 | test_freeze_experiment_independence.py(9) |
| Full Auto Cycle | 9 | test_full_auto_cycle.py(9) |
| Authorized Trading | 7 | test_authorized_trading.py(7) |
| Full Auto Gate | 7 | test_full_auto_gate.py(7) |
| OOS Portfolio | 6 | test_oos_portfolio.py(6) |
| Broker Reconciliation | 5 | test_broker_reconciliation.py(5) |
| Auto Execute | 5 | test_auto_execute.py(5) |
| Kill Switch | 5 | test_kill_switch.py(5) |
| Auto Gate | 4 | test_auto_gate.py(4) |
| Post-Close Evidence Audit | 4 | test_post_close_evidence_audit.py(4) |
| Alpha Validation | 2 | test_alpha_validation.py(2) |
| Frozen Baseline Integrity | 2 | test_frozen_baseline_integrity.py(2) |
| Quick Backtest Details | 2 | test_quick_backtest_details.py(2) |
| QMT Accounting Sync | 2 | test_qmt_accounting_sync.py(2) |
| DB Snapshot | 1 | test_db.py(1) |
| Database Isolation | 1 | test_database_isolation.py(1) |
| Signal Engine | 1 | test_signal_engine.py(1) |
| Trading Calendar | 1 | test_trading_calendar.py(1) |
| **合计** | **334** | |

## 结论

1. **无遗漏**: 334 个 main-only 测试全部是 main 在 fork 后添加的功能测试（QMT 实盘链路、决策包、交易网关、风控断路器、经纪商对账等），与 P0-1~P0-4 范围无关。

2. **P0 测试完整覆盖**: 所有 165 个 P0-only 测试覆盖了 P0-1 生产路径保护、P0-2 信号幂等、P0-3 Fixture 加载与对账、P0-4 统计修复、及组合回放。

3. **Net gap 169 = 334(main新增) - 165(P0新增)**，纯粹是 fork 后各自方向独立演进的结果。
