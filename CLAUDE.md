# CLAUDE.md — SerenityMonitor

> A-share 多因子评分 + 半自动交易辅助系统，含移动端看板。

---

## Project Overview

**SerenityMonitor** — 14 只 A 股主板标的的量化评分、信号生成、持仓管理、自动执行、回测验证系统。

| 维度 | 详情 |
|------|------|
| **Language** | Python 3.12+ |
| **Web** | Flask (port 8401, mobile-first UI) |
| **Database** | SQLite (`serenity.db`) |
| **Test framework** | pytest (751 passed, 29 skipped) |
| **Scheduling** | macOS launchd (4 plists) + crontab (7 entries) |
| **Push** | WxPusher / WeCom / ServerChan / Telegram |
| **LLM** | DeepSeek (sentiment_engine) |

## Architecture

```
SerenityMonitor/
├── monitoring_dashboard.py  # Flask 看板 (4-Tab: 总览/持仓/哨兵/风控)
├── scorer.py                # 7维动态评分引擎 (v3.0)
├── factor_engine.py         # 14因子计算 (MACD/OBV/MFI/CCI/WQ Alpha)
├── signal_engine.py         # 信号生成 (BUY/SELL/HOLD 等)
├── portfolio.py             # PortfolioManager: 资金/仓位/止盈止损
├── auto_execute.py          # 自动执行计划生成
├── backtest_engine.py       # 5策略回测引擎
├── sentinel_engine.py       # 20信源哨兵 + 权重自进化
├── research_engine.py       # TrendRadar 全网研究 + 话题→标的映射
├── guru_wisdom.py           # 13位大师智库 (段永平/巴菲特/芒格等)
├── reflection_engine.py     # IC驱动维度权重调优
├── daily_workflow.py        # 8步每日管线
├── db.py                    # 39张表 SQLite 存储
├── paper_trader.py          # 纸面交易模拟
├── sentinel_backtest.py     # 信源绩效回测
├── cli.py                   # 76+ CLI 命令
│
├── [Phase 1 — Microstructure & Audit]
│   ├── market_microstructure.py  # T+1锁定/涨跌停/停牌
│   ├── execution_simulator.py    # 成交模拟 (含佣金+印花税+滑点)
│   ├── fill_model.py             # 成交填充模型 (部分成交/延迟)
│   └── audit_logger.py           # 决策审计链 (1356条记录)
│
├── [Phase 2 — Verification]
│   ├── frozen_baseline.py        # 冻结基线 v2 (时钟重置)
│   └── equal_weight_basket.py    # 等权基准 (逐日账本)
│
├── [Phase 3 — Factor Quality]
│   ├── factor_audit.py           # 因子去冗余 (7维→2独立)
│   ├── correlation_cluster.py    # 相关性簇 (替代SECTOR_MAP)
│   ├── kernel_freeze.py          # 内核冻结开关 (9模块)
│   └── ablation_framework.py     # 消融实验框架
│
├── [Phase 4 — Validation]
│   ├── phase4_checklist.py       # 上线自检 (16/19 PASS)
│   └── stock_pool_audit.py       # 标的安全审计
│
├── [Phase 5 — Execution & Safety]
│   ├── trade_gateway.py          # 统一交易网关 (Paper/THS/QMT)
│   ├── kill_switch.py            # 熔断 (回撤12%/日亏2%/回撤8%)
│   ├── observation_mode.py       # 观察模式/紧急停机
│   ├── promotion_ceremony.py     # 模块晋级仪式
│   ├── sim_verification.py       # 模拟盘验证框架
│   └── weekly_comparison_report.py  # 周度三系统对比 (周六07:30)
│
├── static/
│   ├── css/monitor.css      # v4.0 iOS 风格 (768行)
│   └── js/monitor.js        # 前端渲染引擎 (1000+行)
├── templates/monitor.html   # v4.0 HTML (63行)
├── tests/                   # 751 passed, 29 skipped
│
├── portfolio/               # 包结构 (向后兼容)
├── db/                      # 包结构
├── signal_engine/           # 包结构
├── auto_execute/            # 包结构
└── factor_engine/           # 包结构
```

## Phase Status — All 5 Phases Complete

| Phase | Focus | Key Deliverables |
|-------|-------|------------------|
| **1** | A-share Microstructure & Audit Chain | market_microstructure (T+1/limit/suspended), execution_simulator, fill_model, audit_logger (1,356 records) |
| **2** | Verification Infrastructure | frozen_baseline v2 (clock reset on de-redundancy), equal_weight_basket (event_ledger_raw_prices) |
| **3** | Factor Quality & Kernel Freeze | correlation_cluster (replacing SECTOR_MAP), factor_audit (7 dims -> 2 independent), kernel_freeze (9 modules frozen), ablation_framework |
| **4** | Validation | phase4_checklist (16/19 PASS, 3 clock-dependent), stock_pool_audit |
| **5** | Execution & Safety | trade_gateway (Paper/THS/QMT backends), kill_switch (circuit+loss+drawdown), observation_mode, promotion_ceremony, sim_verification, weekly_comparison_report (Sat 07:30) |

**Key Metrics:** 751 tests passing / 29 skipped | Factor de-redundancy: 7 dimensions -> 2 independent | Frozen Baseline v2 with clock reset | T4 defensive floor at 20% | TradeGateway with 3 backends (Paper, THS Semi-Auto, Passthrough; QMT/xtquant future)

## Key Commands

```bash
# Dashboard
python3 monitoring_dashboard.py          # → http://localhost:8401/monitor

# CLI
python3 cli.py status                    # 系统状态
python3 cli.py rescore                   # 重新评分
python3 cli.py signal                    # 生成信号
python3 cli.py portfolio                 # 持仓报告

# Research
python3 research_engine.py --daily       # 每日研究流程
python3 guru_wisdom.py collect           # 大师语录采集
python3 sentinel_backtest.py --days 30   # 信源绩效回测

# Tests
python3 -m pytest tests/ -q              # 751 passed, 29 skipped

# Lint
ruff check .
```

## Data Flow

```
实时行情 (Sina/Tencent) → scorer → signal_engine → portfolio → auto_execute
                                  ↓                    ↓
                            sentinel_engine ← guru_wisdom ← research_engine
                                  ↓                    ↓
                            monitoring_dashboard (看板) ← 20信源融合
                                  ↓
                            notifier (WxPusher/Telegram)
```

## Self-Evolution Loop

```
07:00 launchd → research_engine.run_daily_research()
                sentinel_engine.settle_outcomes()
                sentinel_engine.update_source_weights()
                sentinel_engine.sync_guru_quotes()
 
22:00 launchd → 第二轮研究 + 权重进化

周日 19:00 → research_engine.generate_weekly_review()
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/monitor-data` | 看板全量数据 |
| `/api/hermes/trade` | Hermes 实时交易更新 |
| `/api/hermes/balance` | Hermes 资产校准 |
| `/api/hermes/health` | 数据完整性检查 |
| `/api/sentinel/status` | 哨兵信源面板 |
| `/api/sentinel/fusion` | 哨兵融合信号 |
| `/api/research/brief` | 研究简报 |
| `/api/guru/status` | 大师智库状态 |
| `/api/backtest/<code>` | 策略回测 |
| `/api/push/signal` | 即时推送告警 |
| `/api/health` | 系统健康检查 |
| `/api/paper-portfolio` | 纸面账户 |
