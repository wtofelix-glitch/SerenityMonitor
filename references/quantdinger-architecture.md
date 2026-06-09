# QuantDinger 架构笔记 — 供 Serenity Monitor 复用参考

> 来源: [brokermr810/QuantDinger](https://github.com/brokermr810/QuantDinger) (7.4K⭐)
> 提取日期: 2026-06-09

---

## 一、为什么关注 QuantDinger

| 维度 | 我们现在 | QuantDinger 能给的 |
|------|---------|-------------------|
| 策略回测 | 无（只有评分信号） | Server-side backtest + equity curves + drawdown |
| 实盘执行 | 无（人工交易） | IBKR/MT5/Alpaca + CCXT 10+ 交易所 |
| AI Agent 集成 | Hermes MCP | **Agent Gateway** + PyPI MCP server |
| 多用户 | 单用户 | RBAC / OAuth / 多用户角色 |
| 部署 | Flask + SQLite | PostgreSQL 16 + Redis 7 + Docker Compose |

## 二、核心架构

### 分层设计

```
┌─────────────────────────────────────┐
│  Frontend (Vue SPA)                  │
│  - KLine 专业图表                     │
│  - Indicator IDE + Strategy Workflow  │
│  - AI 研究面板 + 机会雷达             │
└──────────────┬──────────────────────┘
               │ HTTP/WS
┌──────────────▼──────────────────────┐
│  Nginx (反向代理 + 静态资源)          │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│  Flask + Gunicorn (API Gateway)      │
│  ├── AI Analysis Services            │
│  │   ├── Multi-LLM ensemble          │
│  │   ├── NL→Indicator/Strategy       │
│  │   └── Post-backtest AI hints      │
│  ├── Strategy & Backtest Engine      │
│  │   ├── IndicatorStrategy           │
│  │   └── ScriptStrategy              │
│  ├── Execution & Quick Trade         │
│  │   ├── CCXT (10+ 交易所)           │
│  │   ├── IBKR (股票/期货)            │
│  │   ├── MT5 (外汇)                  │
│  │   └── Alpaca (美股/ETF)           │
│  ├── Billing & Membership            │
│  └── Agent Gateway 🎯               │
│      └── /api/agent/v1               │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│  State Layer                         │
│  ├── PostgreSQL 16 (系统记录)        │
│  ├── Redis 7 (缓存 + 任务队列)       │
│  └── 日志 / 运行时数据               │
└─────────────────────────────────────┘
```

### Agent Gateway 实现细节

QuantDinger 最值得复用的部分是它的 **Agent Gateway**：

```
/api/agent/v1/
├── /markets         # 行情数据（股票/期货/加密货币）
├── /strategies      # 策略 CRUD + 部署
├── /backtests       # 回测提交 + 结果查询
├── /orders          # 订单查询（只读 by default）
│── /portfolio       # 持仓查询
└── /stream          # SSE 任务流（回测进度推送）
```

**MCP 服务器实现** (`quantdinger-mcp` PyPI 包)：
- 包装 Agent Gateway 为 MCP 工具
- `uvx quantdinger-mcp` 一行启动
- 配置：`QUANTDINGER_BASE_URL` + `QUANTDINGER_AGENT_TOKEN`
- 每个 agent 调用都 audit-logged

**安全模型**：
- Agent token **paper-only by default**
- Live execution 需 `paper_only=false` + 服务端 `AGENT_LIVE_TRADING_ENABLED=true`
- Token hash at rest，exchange keys 不出用户部署环境

## 三、对 Serenity Monitor 的复用建议

### P1 — 短期（本周）

**不部署 QuantDinger 全栈**，而是：
1. 学习它的 **Agent Gateway + MCP** 设计模式
2. 为 Serenity 也加一个轻量 Agent Gateway（允许 Codex/Claude Code 通过 HTTP 查因子评分）

### P2 — 中期（本月）

当需要回测 Serenity 信号有效性时：
1. Docker 部署 QuantDinger `docker compose up -d`
2. 通过 MCP 调用 Serenity 的因子引擎
3. 把 Serenity 的信号转成 QuantDinger 的 `IndicatorStrategy`

### P3 — 长期

用 QuantDinger 作为回测验证层，Serenity 作为策略生成层：
```
Serenity 因子评分 → 转成 IndicatorStrategy → QuantDinger 回测
     ↑                                         ↓
  持仓监控 ←─────────────────── 回测结果反馈
```

## 四、安装要点

```bash
# 最小化安装（只需 Docker）
curl -fsSL https://raw.githubusercontent.com/brokermr810/QuantDinger/main/install.sh | bash

# 打开 http://localhost:8888
# 默认凭据: quantdinger / 123456

# 生成 Agent Token
# Profile → My Agent Token → Issue

# 配置 MCP
# .cursor/mcp.json:
{
  "mcpServers": {
    "quantdinger": {
      "command": "uvx",
      "args": ["quantdinger-mcp"],
      "env": {
        "QUANTDINGER_BASE_URL": "http://localhost:8888",
        "QUANTDINGER_AGENT_TOKEN": "qd_agent_xxxxx"
      }
    }
  }
}
```

---

## 五、UZI-Skill 的 65 评委集成思路

UZI-Skill 的 `investor-panel` 有 65 位评委的投票结果（v3.8.0）。

| 评委组 | 人数 | 对我们的价值 |
|--------|:---:|-------------|
| A 经典价值 | 6 | 护城河/安全边际交叉验证 |
| B 成长投资 | 9 | AI 赛道前景判断 |
| C 宏观对冲 | 7 | 市场时机参考 |
| D 技术趋势 | 4 | 量价分析验证 |
| E 中国价投 | 7 | A 股本土化判断 |
| F A 股游资 | 23 | 参与度/热度信号 |
| G 量化系统 | 4 | 统计套利信号 |
| H 科技领袖派 🆕 | 4 | **AI 产业链直接判断** |
| I Serenity 瓶颈 🆕 | 1 | **与我们方法论完全一致** |

**具体操作**：
1. 对持仓股，通过 UZI CLI 的 `--school I` 单独跑 Serenity 视角
2. 提取 `panel.json` 的 consensus 数据
3. 将 H 组（黄仁勋/Musk/Altman/Saylor）观点作为 AI Capex 暴露的交叉验证
