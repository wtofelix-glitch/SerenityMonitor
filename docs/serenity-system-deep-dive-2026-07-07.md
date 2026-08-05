# SerenityMonitor 系统深度调研报告

> 生成日期: 2026-07-07 22:11 CST
> 调研范围: 完整代码库、数据库、调度基础设施、运行状态

---

## 1. 系统概述

**SerenityMonitor** 是一个全面、生产就绪的 A 股量化投资辅助系统，基于 Serenity 供应链瓶颈投资框架构建。它将 CPO/AI 产业链映射到 20 只 A 股主板标的，通过 7 维动态评分引擎、14 个 Alpha 因子、20 源多模态哨兵网络和自动进化机制生成交易信号。

| 维度 | 详情 |
|------|------|
| **定位** | A 股 7 维多因子评分与半自动交易辅助系统 |
| **语言** | Python 3.12+ |
| **代码规模** | ~58,000 行 Python（90+ 文件） |
| **数据库** | SQLite（50 张表, 4.1 MB） |
| **Web 服务** | Flask 看板（8401）+ Plotly Dash（8050）+ Hermes Bridge（8643） |
| **测试** | 47 个测试文件, 344 tests passing |
| **调度** | 6 个 launchd plist + ~20 个 Hermes cron 任务 |
| **通知** | 三通道微信（WxPusher / 企业微信 / Server酱）+ Telegram |
| **LLM** | DeepSeek（情绪分析、因子解读、研报生成） |

---

## 2. 架构全景

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Data Ingestion Layer                          │
│  sina/tencent API  │  TrendRadar 搜索  │  大师智库  │  20 信源哨兵   │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Scoring Engine (v3.0)                          │
│  7 dimensions → weighted fusion → 0-100 total score per stock        │
│  zone(20%) factor(20%) momentum(18%) serenity(18%)                   │
│  technical(10%) moat(10%) volume(4%)                                 │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Signal Engine (v3.0)                            │
│  STRONG_BUY(≥74) BUY(≥66) CAUTION_BUY(60-66)                        │
│  HOLD(50-60) WATCH(45-50) SELL(<45)                                  │
│  + SELL buffer confirmation + oversold protection                    │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Portfolio Manager (v4)                           │
│  Kelly position sizing │ risk limits │ stop-loss │ rebalance         │
│  Frozen vs Adaptive benchmark │ compound milestones                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Execution Layer                               │
│  auto_execute → signal_to_trade → broker_bridge (THS bridge)         │
│  → execution_log → trade_journal → outcome backfill                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Monitoring & Evolution                             │
│  Flask Dashboard (8401)  │  IC analysis  │  Signal performance        │
│  Sentinel performance  │  Weight evolution  │  Backtest validation    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 标的池（20 只主板）

### Tier 1 — 光通信/AI 算力核心（首选）
| 代码 | 名称 | 卡位 | Serenity 标签 |
|------|------|------|--------------|
| 002281 | 光迅科技 | 光器件全产业链 | CPO_chokepoint |
| 000988 | 华工科技 | 光模块+激光双主线 | laser+optical_engine |

### Tier 2 — 高弹性映射 + 机器人
| 代码 | 名称 | 卡位 | Serenity 标签 |
|------|------|------|--------------|
| 600141 | 兴发集团 | 磷化工/CPO 热管理 | phosphorus_chemicals |
| 603083 | 剑桥科技 | 高速光模块 800G/1.6T | optical_module |
| 600487 | 亨通光电 | 光纤光缆/FAU | fiber_infra |
| 000938 | 紫光股份 | AI 交换机/新华三 | ai_switch |
| 601689 | 拓普集团 | Tesla Optimus 执行器 | robot_actuator |
| 002050 | 三花智控 | 热管理/执行器 | robot_thermal |
| 601100 | 恒立液压 | 丝杠/人形机器人 | robot_ballscrew |
| 600580 | 卧龙电驱 | 伺服电机 | robot_servo |
| 002896 | 中大力德 | RV 减速器 | robot_reducer |

### Tier 3 — 观察池
| 代码 | 名称 | 卡位 |
|------|------|------|
| 002428 | 云南锗业 | 衬底材料 |
| 600460 | 士兰微 | 功率半导体 |
| 603986 | 兆易创新 | NOR Flash/DRAM |
| 600176 | 中国巨石 | 玻纤/PCB 基材 |

### Tier 4 — 防御组合（高股息/低波动）
| 代码 | 名称 | 卡位 |
|------|------|------|
| 600036 | 招商银行 | 零售银行龙头 |
| 600585 | 海螺水泥 | 水泥龙头 |
| 600900 | 长江电力 | 水电龙头 |
| 601398 | 工商银行 | 全球最大银行 |
| 601006 | 大秦铁路 | 煤炭运输专线 |

---

## 4. 评分引擎（v3.0）

### 7 维动态评分体系

| 维度 | 权重 | 描述 | 数据来源 |
|------|------|------|---------|
| `zone` | 20% | 60 日价格通道 + 静态买入区 | 新浪实时行情 |
| `factor` | 20% | 14 Alpha 因子 + 三周期融合 | 日 K 数据 |
| `momentum` | 18% | 趋势动量（非 MR 模式下原始方向） | 价格序列 |
| `serenity` | 18% | CPO/AI 产业链卡位匹配度 | STOCK_DETAILS 映射表 |
| `technical` | 10% | MA/RSI/布林 + 情绪 80/20 融合 | 技术指标 |
| `moat` | 10% | ROE/毛利率/负债/估值 | 基本面财务数据 |
| `volume` | 4% | 成交量（低权重，IC 长期为负） | 量能数据 |

### 关键设计决策

1. **因子翻转仅在均值回归模式**：20 日跌幅 >3% 且非恐慌时翻转，避免"卖强买弱"系统性偏向
2. **IC 自动淘汰**：每日计算 Rank IC，标记持续负向维度（ELIMINATE/DEGRADE/MONITOR）
3. **防御组合 Serenity 补偿权重**：T4 标的自动切换到护城河 60% + 动量 25% 的权重配置
4. **辩论引擎**（`debate_engine.py` / `ensemble_voting.py`）：多视角投票确认信号
5. **消融框架**（`ablation_framework.py`）：系统性维度贡献归因

---

## 5. 信号体系（v3.0）

| 信号 | 阈值 | 确认要求 | 绩效数据 |
|------|------|---------|---------|
| STRONG_BUY | ≥74 | MA20 上方 + 不缩量 + 4/5 维度确认 | 14 条信号（收紧中） |
| BUY | ≥66 | 技术确认 | 22 条信号（收紧中） |
| CAUTION_BUY | 60-66 | 精筛过滤（不缩量/趋势配合/动量底线） | 54 条, 胜率 53.7% |
| HOLD | 50-60 | 持有观察 | 100 条, 胜率 60.0% |
| WATCH | 45-50 | 关注/弱持有 | — |
| SELL | <45 | 3 道缓冲确认（RSI≥35/不在买入区/非布林下轨） | 胜率 80%（缓冲后） |

**信号绩效追踪**：`signal_performance.py` 按信号类型、维度、IC 三个维度统计绩效。`outcome_1d/3d/5d/10d` 自动回填，形成完整闭环。

---

## 6. 数据库架构（50 张表）

### 核心数据表
| 表名 | 记录数 | 说明 |
|------|--------|------|
| `signal_log` | 478 | 每日每标的信号日志（UPSERT by code+date） |
| `scoring_history` | 543 | 评分历史（7 维分项明细） |
| `nav_history` | 28 | 净值历史（总值/现金/持仓/盈亏） |
| `trades` | 94 | 成交记录 |
| `signal_performance` | 66 | 信号类型绩效聚合 |
| `daily_snapshots` | 402 | 每日价格快照 |
| `sentinel_observations` | 136 | 哨兵观测记录 |
| `sentinel_sources` | 22 | 信源注册表 |

### 治理与审计表
| 表名 | 说明 |
|------|------|
| `decision_audit_log` | 50+ 字段的完整决策审计追踪 |
| `evolution_log` | 权重/参数变更记录 |
| `data_quality_log` | 数据质量监控 |
| `portfolio_reconciliations` | NAV 对账记录 |
| `paper_snapshots` / `paper_trades` | 纸面交易模拟 |
| `backtest_results` | 策略回测结果 |
| `signal_to_trade` | 信号→交易映射 |
| `execution_log` | 执行结果日志 |
| `conviction_log` | 确信度评分记录 |

---

## 7. 基础设施

### 7.1 macOS launchd 服务（6 个）

| 服务 | 状态 | 说明 |
|------|------|------|
| `com.serenity.dashboard` | ✅ 运行中（PID 59436） | Flask 看板 :8401 |
| `com.serenity.bridge-server` | ✅ 运行中（PID 91343） | Hermes Bridge :8643 |
| `com.cloudflared.serenity` | ✅ 运行中（PID 87207） | 公网隧道 |
| `com.serenity.scheduler` | ⬜ 未加载 | 日终调度 |
| `com.serenity.monitor` | ⬜ 未加载 | 系统监控 |
| `com.serenity.research` | ⬜ 未加载 | 研究采集 |

### 7.2 Hermes Cron 任务（Serenity 相关，~10 个活跃）

| 时间 | 任务 | 状态 |
|------|------|------|
| 09:00, 15:00 | 大师智慧采集 | OK |
| 16:08 | 14 因子信号推送 | OK |
| 16:18 | 基本面因子更新 | OK |
| 16:33 | 信号绩效 outcome 补填 | OK |
| 16:43 | 交易日志沉淀→gbrain | OK |
| 16:45 | Serenity Bridge 同步 | OK |
| 23:30 | 每日知识沉淀 | OK |

### 7.3 外部服务

| 服务 | 端点 | 状态 |
|------|------|------|
| Serenity Dashboard | http://localhost:8401/monitor | ✅ |
| Serenity Bridge | http://localhost:8643 | ✅ |
| cloudflared 公网穿透 | 隧道 | ✅ |
| DeepSeek API | api.deepseek.com | ✅ |
| Hermes Gateway | WeChat + Telegram | ✅ |

---

## 8. 数据流

### 每日管线（8 步）

```
07:00 research_engine.run_daily_research()    ← TrendRadar 全网采集
     sentinel_engine.settle_outcomes()        ← 昨日信号结算
     sentinel_engine.update_source_weights()  ← 信源权重进化
     
09:00-14:30  盘中信号监控（signal_engine + 价格快照）

15:05 launchd → daily_workflow.py --push
     Step 0: 参考数据拉取（fetch_reference.py）
     Step 1: 多因子评分（scorer.py）
     Step 2: 交易信号生成（signal_engine.py）
     Step 3: Outcome 补填 + 绩效统计（signal_performance.py）
     Step 4: 评分反思（score_reflection / IC 分析）
     Step 5: 反思收益补填（factor_ic.py → 维度淘汰）
     Step 6: 自动调仓（auto_execute.py → portfolio.py）
     Step 7: 净值简报 + 行业轮动 + 信号绩效简报
     Step 8: 推送（WxPusher / Telegram）
     
22:00 晚间复核（保险重跑，与 15:05 同管线）
```

### 研究采集管线

```
TrendRadar 搜索 → research_engine.py → 话题→标的映射
                                      → guru_wisdom.py → 大师语录入库
                                      → sentinel_engine.py → 20 源融合信号
                                      → research_signals 表
```

### 信号→执行闭环

```
signal_engine → signal_log (UPSERT)
             → auto_execute → trade_journal_complete → 用户手动执行
                                                        → Hermes 微信反馈 trade 信息
                                                        → trading_log_sync.py
                                                        → outcome 补填
                                                        → signal_performance 更新
```

---

## 9. 当前系统状态（2026-07-07）

### 9.1 持仓快照

| 指标 | 数值 |
|------|------|
| 总资产 (NAV) | ¥60,354 |
| 现金 | ¥1,769 |
| 持仓市值 | ¥58,585 |
| 累计收益 | +18.19% |
| 今日变动 | -1.43% |
| 启动资金 | ¥51,066 |

### 9.2 今日评分 TOP 8

| 排名 | 代码 | 名称 | 评分 | 信号 | Tier |
|------|------|------|------|------|------|
| 1 | 600900 | 长江电力 | 69.8 | HOLD | T4 |
| 2 | 600036 | 招商银行 | 69.5 | HOLD | T4 |
| 3 | 002281 | 光迅科技 | 68.2 | **BUY** | T1 |
| 4 | 601689 | 拓普集团 | 68.2 | HOLD | T2 |
| 5 | 601398 | 工商银行 | 66.7 | HOLD | T4 |
| 6 | 600585 | 海螺水泥 | 66.1 | HOLD | T4 |
| 7 | 601006 | 大秦铁路 | 65.6 | HOLD | T4 |
| 8 | 600487 | 亨通光电 | 64.6 | HOLD | T2 |

> ⚠️ 防御组合（T4）占据 TOP 8 中的 5 席，反映当前"震荡市"市况判断下防御标的的补偿权重生效。

### 9.3 今日评分 BOTTOM 4

| 排名 | 代码 | 名称 | 评分 | 信号 | Tier |
|------|------|------|------|------|------|
| 17 | 600176 | 中国巨石 | 42.4 | WATCH | T3 |
| 18 | 002896 | 中大力德 | 41.6 | SELL | T2 |
| 19 | 600580 | 卧龙电驱 | 41.3 | SELL | T2 |
| 20 | 603986 | 兆易创新 | 44.6 | WATCH | T3 |

> 机器人板块（T2）新入池标的表现疲弱 — 拓普(+68.2)除外。大盘 7/7 下跌背景。

---

## 10. 版本演进历史

| 版本 | 日期 | 核心变更 |
|------|------|---------|
| v3.0 | 2026-06 | 7 维评分引擎、因子翻转策略、IC 自动淘汰 |
| v3.1 | 2026-06 | 信号闭环、自适应止损、行业风控、回测、MC 压测 |
| v3.2 | 2026-06 | IC 衰减落地、动态权重、纸面交易、周报 v3 |
| v3.3 | 2026-06 | a-stock-data 集成、腾讯行情 PE/PB、资金面维度 |
| v3.4 | 2026-06 | Premium UI — 玻璃拟态信号卡、渐变盈亏 |
| v4.0 | 2026-06 | 交易内核冻结、Frozen vs Adaptive 基准对比、Kelly 仓位管理 |
| v5.x | 2026-07 | 回测+进化+交易+风控+UI 全栈交付、机器人标的入池 |

---

## 11. 研发管线（Research Pipeline）

### 11.1 研究引擎（`research_engine.py`）
- TrendRadar 全网关键词搜索 → 话题→标的映射
- 每日研究流程 `run_daily_research()`
- 周末综合周报 `generate_weekly_review()`
- 话题注册表 `research_topics`（25+ 关键词）

### 11.2 哨兵网络（`sentinel_engine.py`）
- 20 个多模态信源
- 权重自进化（基于信号绩效）
- 融合信号输出
- 绩效回测 `sentinel_backtest.py`

### 11.3 大师智库（`guru_wisdom.py`）
- 13 位投资大师语录（段永平/巴菲特/芒格/达利欧等）
- 每日定时采集（09:00 + 15:00）
- 与当前持仓/信号匹配建议

### 11.4 情绪引擎（`sentiment_engine.py`）
- 新浪财经新闻爬取 + NLP 评分
- DeepSeek LLM 情绪分析（可选）

---

## 12. 风控体系（v4）

| 层级 | 机制 | 参数 |
|------|------|------|
| **硬止损** | ATR 动态止损 | -4% min, 1.5x ATR multiplier |
| **移动止盈** | 利润回撤追踪 | 8% trailing stop |
| **单日限制** | 最大日亏损 | -4% |
| **组合限制** | 总资金最大回撤 | -12% |
| **连续亏损** | 强制空仓 | 连续 2 笔 → 冷却 3 天 |
| **T+1 锁定** | 单票/总仓上限 | 25% / 40% |
| **主题暴露** | 单主题仓位上限 | 50% |
| **复利里程碑** | Kelly 阶梯提升 | NAV 1.2x→+3%, 1.5x→+5% |

---

## 13. 回测框架

### 5 策略回测
- 默认策略（7 维评分）
- 仅 Alpha 因子
- 仅 Serenity 映射
- 红利低波
- ETF 动量轮动

### 验证机制
- **Frozen vs Adaptive**：基准策略 vs 进化策略对比
- **Monte Carlo 压力测试**：市场极端情景模拟
- **消融实验**：逐维度移除测试
- **纸面交易沙盒**：信号→模拟执行→绩效追踪

---

## 14. 系统健康评估

### 🟢 优势
1. **架构完整性** — 从数据采集到信号生成到执行追踪，全链路闭环
2. **自我进化** — IC 驱动权重调整 + 信源绩效淘汰 + 辩论引擎多视角验证
3. **工程成熟度** — 50 张表数据库、58K 行代码、344 tests、git 版本化演进
4. **多重冗余** — 三通道微信推送 + Telegram + 看板 + cloudflared 公网
5. **风控严谨** — 7 层风控（ATR/Kelly/T+1/主题暴露/复利里程碑）
6. **可观测性强** — 4-Tab 看板 + 决策审计 + 异常检测 + Prometheus metrics

### 🟡 待改进
1. **机器人标的绩效不足** — 5 只新入池标的 4 只排名倒数，需观察是否需要消融验证
2. **防御标的过度集中在 TOP 榜** — 震荡市 T4 补偿权重可能过度倾斜
3. **部分 launchd 服务未加载** — scheduler/monitor/research 未运行，依赖 Hermes cron 驱动
4. **LLM 情绪引擎可选** — .env 中 DEEPSEEK_API_KEY 标注"your-key-here"
5. **Anthropic API key 失效** — hermes doctor 报告，若有用 Claude 模型的任务会失败

### 🔴 需关注
1. **今日 NAV 回撤** — 60,354（昨日 61,229），日跌 1.43%，连续两日下行
2. **T2 机器人标的** — 恒立液压(46.2 WATCH)、卧龙电驱(41.3 SELL)、中大力德(41.6 SELL) 评分持续恶化
3. **仅光迅科技 1 只 BUY** — 市场机会稀缺，系统整体偏防御

---

## 15. 文件清单（按模块）

### 核心引擎（7 文件, ~10K 行）
```
scorer.py (991L)          — 7 维评分引擎
factor_engine.py (1341L)  — 14 Alpha 因子 + 三周期
signal_engine.py (1276L)  — 信号生成 + 缓冲确认
sentiment_engine.py       — 新闻情绪评分
data_engine.py            — 行情抓取
config.py (599L)          — 全局配置
portfolio.py (904L)       — 组合管理
```

### 研究/分析（8 文件, ~5K 行）
```
research_engine.py (624L)     — 全网研究
sentinel_engine.py (763L)     — 20 源哨兵
guru_wisdom.py (901L)         — 13 位大师智库
uzi_insight.py                — AI 产业链卡位
factor_ic.py (668L)           — Rank IC 分析
factor_attribution.py (774L)  — 因子归因
factor_interpreter.py         — 因子解读
anomaly_analyzer.py (655L)    — 异常检测
```

### 交易执行（6 文件, ~5K 行）
```
auto_execute.py (1526L)       — 自动调仓计划
auto_gate.py (1199L)          — 执行闸门
alpha_gate.py (930L)          — Alpha 准入
broker_bridge.py              — 券商桥接
ths_bridge.py                 — 同花顺桥接
paper_trader.py               — 纸面模拟
```

### 回测/验证（5 文件, ~4K 行）
```
backtest_engine.py (2006L)    — 5 策略回测
signal_backtest.py            — 信号回测
quick_backtest.py             — 快速回测
monte_carlo.py                — Monte Carlo
monte_carlo_stress.py         — 压力测试
```

### 监控/看板（4 文件, ~6K 行）
```
monitoring_dashboard.py (3020L)  — Flask 看板
dash_dashboard.py (1129L)        — Plotly Dash
cli.py (2364L)                   — 76 CLI 命令
db.py (2527L)                    — 50 表 SQLite
```

### 调度/推送（5 文件）
```
daily_workflow.py (672L)      — 8 步管线
notifier.py                   — 微信推送
signal_push.py                — Telegram 推送
run_scheduled.sh              — launchd 调度脚本
fourteen_factor_push.sh       — 14 因子推送
```

### 进化/优化（6 文件）
```
weight_adjuster.py            — IC 驱动权重
strategy_optimizer.py         — 策略优化
strategy_loader.py (780L)     — YAML 策略加载
portfolio_optimizer.py        — 组合优化
conviction_engine.py          — 确信度引擎
ensemble_voting.py            — 集成投票
```

### 审计/风控（7 文件）
```
risk_manager.py (669L)        — 风控引擎
security_check.py             — 安全检查
audit_logger.py               — 审计日志
operations_center.py (829L)   — 运维中心
anomaly_alerter.py            — 异常告警
phase4_checklist.py           — 阶段检查
conviction_cli.py             — 确信度 CLI
```

---

## 16. 技术栈摘要

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| Web | Flask (8401) + Plotly Dash (8050) |
| 数据库 | SQLite (serenity.db, 4.1MB, 50 tables) |
| 行情 | Sina Finance API / Tencent API / mootdx |
| 回测 | 自定义引擎（向量化回测） |
| 通知 | WxPusher / WeCom Robot / ServerChan / Telegram |
| LLM | DeepSeek (sentiment/factor analysis/report generation) |
| 调度 | macOS launchd + Hermes cron |
| 测试 | pytest (344 tests) |
| 版本控制 | git (15+ 版本标签) |
| 公网穿透 | cloudflared |
| MCP/Skills | 10+ Hermes 专用技能 |
| 前端 | 原生 HTML/CSS/JS（玻璃拟态 v4.0 iOS 风格） |

---

## 17. 结论

SerenityMonitor 是一个**工程成熟度极高**的个人量化投资系统。它的核心优势在于：

1. **完整的自我进化闭环**：信号→执行→绩效→IC→权重调整→因子淘汰，6 个月内从 v3.0 到 v5.x 的迭代速度证明了这个闭环的有效性
2. **深度的"为什么"**：不只是打分，而是 Serenity 框架的 CPO 供应链瓶颈映射 + 14 因子 + 辩论引擎 + 大师智慧，每一个信号都有对应解释
3. **工程纪律**：50 张表、344 tests、决策审计追踪、消融实验、Frozen vs Adaptive 基准对比 — 这些都是专业量化基金的工程标准

当前系统处于**防御模式**，震荡市市况下 T4 防御标的占主导，机器人新标的尚需验证期。核心风险点在于：过度依赖新浪/腾讯免费行情（API 稳定性不如付费数据），以及半自动执行模式可能导致信号→执行延迟。

---

*报告由 Claude Code 生成，基于 2026-07-07 实际代码库、数据库和运行状态。*
