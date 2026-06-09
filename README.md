# SerenityMonitor

A股 9 维多因子评分与半自动交易辅助系统

覆盖 14 只主板标的，基于 CPO 供应链映射 + 量化因子 + 情绪分析的评分体系。

---

## 快速开始

```bash
cd ~/workspace/SerenityMonitor
python3 cli.py status          # 持仓状态
python3 cli.py signal-perf     # 信号绩效分析（最新）
python3 cli.py auto            # 自动调仓建议
python3 cli.py health          # 系统诊断
python3 cli.py workflow        # 一站式日终工作流
```

## CLI 命令

| 命令 | 功能 |
|------|------|
| `status` | 持仓状态 |
| `portfolio` | 持仓 + 盈亏明细 |
| `signal` | 当前交易信号 |
| `signal-perf` | **信号绩效分析**（胜率/收益/维度有效性） |
| `auto` | 自动调仓计划 |
| `auto-push` | 调仓计划 + 微信推送 |
| `workflow` | 日终工作流 |
| `dash` | 买入区间警报 + 盈亏快照 |
| `health` | 系统健康诊断 |
| `rescore` | 强制重评分 |
| `backtest-quick` | 快速回测 |
| `monitor` | 批量评分监控 |

## 监控看板

### 移动端看板 (端口 8401)

```bash
python3 monitoring_dashboard.py
```

功能：评分排行、净值走势、因子IC、信号绩效、维度预测力、行业轮动

### Dash 图表 (端口 8050)

```bash
python3 dash_dashboard.py
```

功能：K线图、散点图、雷达图、柱状图

## 每日工作流

```bash
python3 daily_workflow.py            # 评分+信号+绩效+反思+调仓
python3 daily_workflow.py --push     # 同上 + 微信/Telegram推送
python3 daily_workflow.py --full     # 含回测快照+IC评估
python3 daily_workflow.py --execute  # 🚀 自动执行交易+更新NAV
```

工作流包含 8 步：
0. 参考数据拉取
1. 多因子评分
2. 交易信号
3. Outcome 补填 + 绩效统计
4. 评分反思
5. 反思收益补填
6. 自动调仓
7. 净值简报 + 行业轮动 + 信号绩效简报
8. 推送

## 自动调度 (launchd)

系统通过 macOS launchd 自动运行，无需手动触发：

```bash
# 加载调度
launchctl load ~/Library/LaunchAgents/com.serenity.scheduler.plist

# 查看状态
launchctl list | grep serenity
```

| 时间 | 任务 |
|------|------|
| 07:30 | 盘前简报 |
| 15:05 | 收盘工作流 `--push` |
| 22:00 | 晚间复核 `--push` |

## 半自动交易流程

```
系统发出信号 → 用户手动操作同花顺 → 卖出信息通过微信反馈给 Hermes
→ Hermes 更新 trade 记录 → 系统自动更新 NAV、信号绩效
```

当前待执行：
- **亨通光电 TAKE_PROFIT** — 6/10 由 Hermes 执行

## 信号类型

| 信号 | 说明 | 当前胜率 |
|------|------|---------|
| STRONG_BUY | 强力买入 (>78分) | — |
| BUY | 买入 (>72分) | — |
| CAUTION_BUY | 谨慎买入 (62-72分) | 见 `signal-perf` |
| HOLD | 持有 | 见 `signal-perf` |
| STRONG_HOLD | 强烈持有 | 见 `signal-perf` |
| SELL | 卖出 | — |
| TAKE_PROFIT | 止盈 | 亨通光电 |
| STOP_LOSS | 止损 | — |

## 信号绩效追踪

```bash
python3 cli.py signal-perf
python3 signal_performance.py                          # 终端报告
python3 monitoring_dashboard.py                        # 看板查看
curl http://localhost:8401/api/signal-performance       # JSON API
```

统计维度：各信号类型胜率、平均收益、评分维度预测有效性（corr vs 1日收益）

## 持仓规则

- 最多 2 只，单只上限 60%
- 买入门槛 ≥72 分
- 持仓跌破 48 分 → 强制卖出
- 大盘危险 → 自动降仓至 1 只 / 40%

## 微信推送

三通道自动选择：WxPusher / 企业微信群机器人 / Server酱

```bash
export WXPUSHER_TOKEN="AT_xxx"
export WXPUSHER_UIDS="UID_xxx"
```

设置后 `--push` 参数自动生效。

## LLM 情绪引擎

```bash
export SERENITY_LLM_API_KEY="sk-xxx"
export SERENITY_LLM_API_BASE="https://api.deepseek.com/v1"
export SERENITY_LLM_MODEL="deepseek-chat"
```

设置后 `sentiment_engine.py` 自动启用 LLM 情绪分析。

## 详细文档

- [架构文档](docs/architecture.md) — 完整模块说明、数据库表结构、API 端点

## 数据文件

- `serenity.db` — SQLite 主数据库（信号日志、评分历史、净值历史）
- `logs/scheduler.log` — 调度执行日志
