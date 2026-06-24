# SerenityMonitor 架构文档

> 最后更新: 2026-06-25 (v3.0)

## 系统概述

SerenityMonitor 是一个 A 股 7 维评分与半自动交易辅助系统，覆盖 14 只主板标的。

**核心流程**: 每日 07:00 研究采集 → 09:00-14:30 盘中信号 → 16:00 收盘评分 → 信号 → 绩效统计 → IC分析 → 调仓建议 → 推送

**工作模式**: 半自动化 — 系统生成信号 → 用户手动执行交易 → 通过 Hermes/微信反馈

---

## 评分体系 (v3.0)

### 7 维动态评分

| 维度 | 权重 | 说明 |
|------|------|------|
| `zone` | 20% | 动态 60 日价格通道 + 静态买入区（v3.0 重构，原 IC=-0.19 已修复）|
| `factor` | 20% | 14 Alpha 因子 + 三周期融合 |
| `momentum` | 18% | 趋势动量，非 MR 模式下使用原始方向 |
| `serenity` | 18% | Serenity 框架 CPO/AI 卡位匹配度 |
| `technical` | 10% | 技术面 (MA/RSI/布林) + 情绪 (80/20 合并) |
| `moat` | 10% | 护城河因子 (ROE/毛利率/负债/估值) |
| `volume` | 4% | 成交量（保留低权重，IC 长期为负） |

**已移除维度**: `base` (手动静态分 IC=-0.13), `guru_wisdom` (常数50无区分力), `mean_reversion` (含入因子翻转), `multi_cycle` (含入 factor), `sentiment` (合并到 technical), `UZI` (降为信息面板)

### 因子翻转策略 (v3.0 关键变更)

仅在均值回归模式 (MarketSense: 20日跌幅>3% 且非恐慌) 时翻转动量/量能/技术面因子。趋势市和中性市保留原始方向 — 消除"卖强买弱"的系统性偏向。

### IC 自动淘汰

`factor_ic.py:recommend_dimension_changes()` 每日运行，标记 IC 持续为负的维度 (ELIMINATE/DEGRADE/MONITOR)，集成到日终 Step 5c。

---

## 信号体系 (v3.0)

| 信号 | 阈值 | 说明 |
|------|------|------|
| STRONG_BUY | ≥74 | + 价格在 MA20 上方 + 量能不缩 + 4/5 确认 |
| BUY | ≥66 | + 技术确认 |
| CAUTION_BUY | 60-66 | + 精筛过滤 (不缩量/趋势配合/动量底线) |
| HOLD | 50-60 | 持有观察 |
| WATCH | 45-50 | 关注/弱持有 |
| SELL | <45 | + 3 道缓冲确认 (RSI≥35/不在买入区/非布林下轨) |

**SELL 缓冲**: 评分<45 的 SELL 信号需通过缓冲确认，防超卖反弹前误卖（原 80% 误判率）。

---

## 模块一览

### 核心引擎

| 模块 | 功能 |
|------|------|
| `scorer.py` | **v3.0 7 维评分引擎** (zone/momentum/volume/serenity/factor/technical/moat) |
| `factor_engine.py` | 14 Alpha 因子 + 三周期融合 |
| `factor_ic.py` | Rank IC 分析 + 维度自动淘汰建议 |
| `signal_engine.py` | 统一信号生成 (BUY/SELL/HOLD 等) + SELL 缓冲确认 + 超卖保护 |
| `sentiment_engine.py` | 新闻情绪评分（新浪财经 + 可选 LLM），合并入 technical |
| `data_engine.py` | 新浪行情抓取 + 重试装饰器 |
| `config.py` | 全局配置（标的、权重、风控参数） |
| `uzi_insight.py` | AI 产业链卡位分析面板（定性，不参与评分） |
| `uzi_evidence_collector.py` | UZI 证据自动采集（哨兵→uzi_evidence 桥接） |

### 数据分析

| 模块 | 功能 |
|------|------|
| `signal_performance.py` | **信号绩效分析** — 按信号类型统计胜率/平均收益、维度预测有效性、数据完整性检查 |
| `factor_ic.py` | Rank IC 归因分析 |
| `sector_rotation.py` | 行业轮动扫描 |
| `weight_adjuster.py` | 动态权重调整（基于 IC） |
| `market_timing.py` | 大盘择时信号 |

### 交易支持

| 模块 | 功能 |
|------|------|
| `portfolio.py` | 组合管理（现金计算、仓位管理、止盈止损） |
| `auto_execute.py` | 自动调仓计划生成 |
| `tier1_reentry.py` | T1 标的回补检查 |

### 工作流与调度

| 模块 | 功能 |
|------|------|
| `daily_workflow.py` | 日终工作流（8 步：评分→信号→绩效→反思→调仓→简报→推送） |
| `run_scheduled.sh` | launchd 调度脚本（07:30 盘前 / 15:05 收盘 / 22:00 复核） |
| `com.serenity.scheduler.plist` | launchd 配置文件 |

### 监控与仪表盘

| 模块 | 端口 | 功能 |
|------|------|------|
| `monitoring_dashboard.py` | 8401 | 移动端监控看板（评分排行、净值图表、信号绩效、因子IC） |
| `dash_dashboard.py` | 8050 | Plotly Dash 图表面板 |
| `metrics.py` | 8401/metrics | Prometheus 指标端点 |

### 通知

| 模块 | 功能 |
|------|------|
| `notifier.py` | 微信推送（WxPusher / 企业微信 / Server酱 三通道自动选择） |
| `signal_push.py` | Telegram 推送 |

### 工具

| 模块 | 功能 |
|------|------|
| `db.py` | SQLite 数据库操作 |
| `cli.py` | 命令行入口（20+ 命令） |
| `health_check.py` | 系统健康诊断 |
| `serenity_logger.py` | 日志配置 |

---

## 数据库表结构

### `signal_log` — 信号日志
```
id, code, date, time, action, total_score, price, is_holding
tech_score, serenity_score, alpha_score, fundamental_score
outcome_1d, outcome_3d, outcome_5d, outcome_10d  ← 回填的涨跌幅
details, created_at
```
UPSERT 按 `(code, date)` 唯一约束，每天每标的最新信号覆盖旧信号。

### `signal_performance` — 信号绩效统计
```
code, action, total_signals, wins_1d, wins_3d, wins_5d
avg_return_1d, avg_return_3d, avg_return_5d, last_updated
UNIQUE(code, action)
```

### `scoring_history` — 评分历史
```
code, date, total_score, base_score, zone_score, momentum_score,
volume_score, serenity_score, factor_score, technical_score,
sentiment_score, moat_score, details
UNIQUE(code, date)
```

### `nav_history` — 净值历史
```
date, total_value, cash, holdings_value, profit_pct, positions_json
```

---

## 调度配置

### launchd (macOS)

```bash
# 加载
launchctl load ~/Library/LaunchAgents/com.serenity.scheduler.plist

# 查看状态
launchctl list | grep serenity

# 卸载
launchctl unload ~/Library/LaunchAgents/com.serenity.scheduler.plist
```

### 调度时间
| 时间 | 任务 | 说明 |
|------|------|------|
| 07:30 | 盘前简报 | `auto_execute.py --premarket` |
| 15:05 | 收盘工作流 | `daily_workflow.py --push` |
| 22:00 | 晚间复核 | `daily_workflow.py --push`（保险重跑） |

---

## CLI 命令大全

```bash
python3 cli.py status              # 持仓状态
python3 cli.py portfolio           # 持仓+盈亏明细
python3 cli.py signal              # 当前交易信号
python3 cli.py signal-perf         # 信号绩效分析报告
python3 cli.py auto                # 自动调仓计划
python3 cli.py auto-push           # 调仓计划+推送
python3 cli.py workflow            # 日终工作流
python3 cli.py dash                # 价格区间+盈亏
python3 cli.py health              # 系统健康诊断
python3 cli.py rescore             # 强制重评分
python3 cli.py backtest-quick      # 快速回测
python3 cli.py monitor             # 批量评分监控
```

---

## 监控面板

### 移动端看板 (端口 8401)
- **评分排行** — 14 只标的实时评分与信号
- **因子矩阵** — 14 因子分项
- **综合评级** — 标的评级概览
- **ETF 动量 + 红利低波** — 策略标的排名
- **因子 IC 归因** — 各维度 Rank IC
- **信号类型绩效** — 全部历史胜率与收益
- **评分维度预测力** — 维度分 vs 1日收益相关性
- **近 7 天买入信号** — 信号历史明细
- **净值走势图** — Canvas 渲染

### Dash 图表 (端口 8050)
- K 线图、散点图、雷达图、柱状图
