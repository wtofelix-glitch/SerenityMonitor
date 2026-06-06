# SerenityMonitor

A股多因子评分系统 — CPO对齐/瓶颈/AIcapex/护城河/动量五维评分

## 快速开始

```bash
cd ~/workspace/SerenityMonitor
python3 cli.py status        # 持仓状态
python3 cli.py auto          # 自动调仓建议
python3 cli.py dash          # 价格区间 + 盈亏
python3 cli.py health        # 系统诊断
```

## 每日命令

| 命令 | 功能 |
|---|---|
| `python3 cli.py auto` | 自动调仓计划（含可执行命令） |
| `python3 cli.py auto-push` | 调仓计划 + 微信推送 |
| `python3 cli.py dash` | 买入区间警报 + 盈亏快照 |
| `python3 cli.py advise` | 相关性优化组合建议 |
| `python3 cli.py workflow` | 一站式日终工作流 |
| `python3 cli.py backtest-quick` | 快速回测快照 |
| `python3 cli.py health` | 9维度系统健康诊断 |
| `python3 cli.py signal` | 当前交易信号 |
| `python3 cli.py portfolio` | 持仓+盈亏明细 |

## 部署 Cron

```bash
bash deploy.sh     # 自动设置每日 16:30 工作流 + 盘中监控
```

## LLM 情绪引擎

```bash
export SERENITY_LLM_API_KEY="sk-xxx"
export SERENITY_LLM_API_BASE="https://api.deepseek.com/v1"  # 可选
export SERENITY_LLM_MODEL="deepseek-chat"                    # 可选
```

设置后自动启用，不需要改代码。

## 架构

```
scorer.py          → 8维评分 (base/zone/momentum/volume/serenity/factor/technical/sentiment)
factor_engine.py   → 14 Alpha因子 + WQ101精选 + 三周期融合
signal_engine.py   → 统一信号引擎 (BUY/SELL/HOLD + 持仓专属)
auto_execute.py    → 自动调仓 (卖出/买入/换仓 + 大盘择时 + 策略分配)
portfolio_advisor  → 相关性矩阵最优组合
quick_backtest.py  → ATR止损回测验证
dashboard.py       → 价格区间警报 + 盈亏快照
health_check.py    → 数据库/数据覆盖/评分缺口/信号填充率诊断
daily_workflow.py  → 6步日终工作流 (评分→信号→outcome→反思→调仓)
```

## 持仓规则

- 最多 2 只，单只上限 60%
- 买入门槛 ≥72 分 (仅 BUY/STRONG_BUY)
- 持仓跌破 48 分或 zone=done → 强制卖出
- 大盘危险时自动降仓至 1 只/40%

## 微信推送

三通道自动选择：WxPusher / 企业微信群机器人 / Server酱

```bash
export WXPUSHER_TOKEN="AT_xxx"
export WXPUSHER_UIDS="UID_xxx"
```

设置后 `--push` 参数自动生效。
