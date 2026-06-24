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
| **Test framework** | pytest (319 tests) + Playwright E2E |
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
├── db.py                    # 19张表 SQLite 存储
├── paper_trader.py          # 纸面交易模拟
├── sentinel_backtest.py     # 信源绩效回测
├── cli.py                   # 76个CLI命令
│
├── static/
│   ├── css/monitor.css      # v4.0 iOS 风格 (768行)
│   └── js/monitor.js        # 前端渲染引擎 (1000+行)
├── templates/monitor.html   # v4.0 HTML (63行)
├── tests/                   # 319 tests, 31 E2E
│
├── portfolio/               # 包结构 (向后兼容)
├── db/                      # 包结构
├── signal_engine/           # 包结构
├── auto_execute/            # 包结构
└── factor_engine/           # 包结构
```

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
python3 -m pytest tests/ -q              # 319 tests
python3 -m pytest tests/ --e2e           # E2E (Playwright)

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
