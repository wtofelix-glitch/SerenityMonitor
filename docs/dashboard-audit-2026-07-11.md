# SerenityMonitor 看板深度调研报告与精简优化方案

> 调研日期: 2026-07-11  
> 调研范围: monitoring_dashboard.py (3021行), static/js/monitor.js (1878行),  
>  static/css/monitor.css (1452行), templates/monitor.html (57行),  
>  /api/monitor-data 真实响应体 (41KB JSON)  
> 调研方法: 全量数据审计 + 渲染逻辑追踪 + 每一行 JS 的分组归类

---

## 1. 现状诊断

### 1.1 信息架构问题：7 个 Tab、23 个数据分组、大量空内容

**当前 Tab 结构（7 个）：**

| Tab | 核心问题 |
|-----|---------|
| 总览 | 唯一有内容的 tab。承载了信号、持仓、因子、评分、共识、UZI 等 8+ 块内容 |
| 持仓 | 渲染了表格但在当前数据状态下几乎空白 |
| 风控 | 止损条件 + 回撤曲线 + 信号绩效 — 全天多数时段只有骨架 |
| 哨兵 | 依赖外部 API（sentinel/fusion），失败时只显示错误 |
| 运营 | 800 行 JS 渲染 reconciliation + data quality + broker — P0 闸门状态在所有场景都是 LOCKED |
| 盯盘 | 单独的心跳轮询，即使在非交易时段也在无意义地刷新 |
| 治理 | OOS + kernel_freeze + ablation — 这些是 CLI 管理工具，塞进看板 UI 增加了视觉噪音 |

**数据利用率极低。** /api/monitor-data 返回 41KB JSON、23 个顶级 key，但其中 16 个在大多数场景下为空或错配：

| 数据分组 | 实际状态 | 判断 |
|---------|---------|------|
| `portfolio` / `portfolio_summary` | 非交易时段返回空 dict | — |
| `scoring` | 凌晨返回空列表 | — |
| `signals` | 返回空 dict | — |
| `auto_gate` | 永远 LOCKED（策略在冻结期） | 看板展示它没有任何可执行信息 |
| `sectors` | 空列表 | 死数据 |
| `ic_analysis` | 有数据但渲染在总览底部，几乎不可见 | 埋没 |
| `dividend_top5` / `etf_top5` | 跟当前策略基本无关（你只交易 20 只主板标的） | 噪音 |
| `ratings` | 20 条，但看板没有清晰的可扫描布局 | 散落 |
| `factors` | 20 条因子数据，以长列表展示 | 视觉过载 |
| `signal_factors` | 14 条信号因子，跟 factors 分开渲染 | 重复感 |

### 1.2 代码规模问题

| 文件 | 行数 | 问题 |
|------|------|------|
| `monitoring_dashboard.py` | 3021 | 后端端点 + 数据聚合在一体；没有分层 |
| `static/js/monitor.js` | 1878 | 单文件，7 个 tab 的所有渲染逻辑混在一起；renderOverview 一个函数 500+ 行 |
| `static/css/monitor.css` | 1452 | 有大量 `.desktop-*` 前缀的规则——移动端设计被桌面风格的 CSS 覆盖 |
| `templates/monitor.html` | 57 | 简洁——这是唯一被正确抽象的文件 |

**最严重的问题是 `renderOverview()`**：一个函数 370-540 行之间，同时渲染了总览头部、NAV 卡片、持仓、信号、因子、量化共识、UZI 产业链 8 个不同区域。任何一个区域改了、其他区域的引用就可能断裂。

### 1.3 视觉冗余问题

- **7 个 Tab 按钮**挤在顶部导航栏，手机上每个按钮约 42px 宽，7 个就是 294px + 间距，在小屏上要么缩放、要么溢出
- **桌面 grid 布局**混入移动端单列设计（`.desktop-overview-grid` 类名暴露了设计意图的分裂）
- **自动闸门（auto_gate）**在总览页渲染了一个复杂的彩色状态条——但这个条的内容在所有场景下都是「LOCKED / 样本不足 50」，从未变过
- **量化丁格共识渲染**用了 60+ 行 JS 构建一个看起来像仪表盘的多维面板，里面包含 5 位大师的评分、操作建议、置信度——但这些数据在冻结期内不参与任何交易决策，纯粹是展示噪音
- **每天做一次的事（净值、评分、信号）**和**几乎从不需要看的事（自动闸门状态、量化共识、大师智慧）**混在同一个视图中，没有频率分层

### 1.4 移动端适配问题

虽然 HTML 有 `viewport-fit=cover` 和 `apple-mobile-web-app-capable`，但渲染逻辑没有针对移动端优化：

- 因子表格在手机上需要横向滚动
- 图表（Chart.js）在小屏上挤到 300px 宽以下
- toast 提示和 action bar 在 iPhone 底部 safe area 之外被截断

---

## 2. 信息架构精简方案

### 核心原则：根据「你多久看一次」做频率分层，而不是「每样东西各开一个 tab」

```
第一层（高频 — 每看必查，< 1 屏）
  ├─ 净值 + 持仓盈亏（3 只票的清晰卡片）
  ├─ 今日评分排名（Top 5 + Bottom 2）
  ├─ 今日信号摘要（BUY/SELL/HOLD 各几条）
  └─ 三线 OOS 进度条（不显示排名，只显示"Day X/120"）

第二层（中频 — 每周看一次）
  ├─ 因子有效性（ICIR 排序，只显示前 3 和末 2）
  └─ 风控状态（观察模式 / 熔断 / 回撤）

第三层（低频 — 每月或判定点）
  ├─ OOS 三线对比（判定日看）
  ├─ 审计统计（decision_audit_log 覆盖率）
  └─ 内核冻结状态
```

### 2.1 Tab 精简：7 → 3

| 当前 Tab | 处理方式 |
|---------|---------|
| 总览 | **保留并精简为第一层高频信息** |
| 持仓 | **合并进总览**（3 只票不需要单独 tab） |
| 风控 | **合并进总览**（1-2 条状态行就够了） |
| 哨兵 | **下线**（冻结期内不参与交易，数据依赖外部 API 不稳定） |
| 运营 | **下线**（auto_gate 永远 LOCKED，reconciliation 是自动化任务不需要看板） |
| 盯盘 | **下线**（盘中不需要一个单独的 tab——可以合并为总览顶部一行彩色 tape） |
| 治理 | **下移到 CLI**（`python3 cli.py phase-status` 已经有完整信息，比手机看板更合适） |

精简后：**3 个 Tab — 总览、详情、OOS**

| Tab | 内容 | 刷新频率 |
|-----|------|---------|
| **总览** | 净值卡 + 3 票持仓卡 + 信号条 + OOS 进度 | 交易时段 60s 自动刷新 |
| **详情** | 评分表 + 因子中频数据 + 风控状态 | 手动/按时段刷新 |
| **OOS** | 三线进度 + 冻结状态 + 审计统计 | 手动刷新 |

---

## 3. 数据精简方案：API 响应体从 41KB → 8KB

### 3.1 移除的 API 字段（不再在前端渲染）

| 字段 | 理由 |
|------|------|
| `auto_gate` | 冻结期内永远 LOCKED |
| `dividend_top5` | 与当前 20 只策略标的无关 |
| `etf_top5` | ETF 动量轮动不在当前策略范围内 |
| `sectors` | 空列表 — 死数据 |
| `ratings` | 与 `scoring` 重复（ratings 是旧评分系统残留） |
| `quantdinger_consensus` | 冻结期内不参与交易，纯展示噪音 |
| `signal_factors` | 与 `factors` 重复展示 |
| `operational_mode` | 终端用户不需要看到 MR 模式细节 |
| `uzi_chain` | 冻结期内不参与评分 |
| `yesterday_summary` | 非必要 — 日线已在 scoring history 中 |
| `ui_metadata` | 前端不需要从后端拿到自己的版本号 |

### 3.2 保留并精简的字段

```json
{
  "date": "2026-07-11",
  "portfolio": {
    "nav": 62549,
    "cash": 7314,
    "holdings_value": 55235,
    "profit_pct": 22.5,
    "positions": [
      {"code": "600036", "name": "招商银行", "shares": 700, "price": 36.88, "value": 25816, "profit_pct": 3.0},
      {"code": "000988", "name": "华工科技", "shares": 100, "price": 158.07, "value": 15807, "profit_pct": 13.4},
      {"code": "600141", "name": "兴发集团", "shares": 400, "price": 34.03, "value": 13612, "profit_pct": -0.1}
    ]
  },
  "scoring": {
    "top5": [...],
    "current_holdings": [...]
  },
  "signals": {
    "buy": 0,
    "sell": 0,
    "caution_buy": 1,
    "hold": 16,
    "summary": "光迅 BUY | 华工 HOLD | 招行 BUY"
  },
  "oos": {
    "day": 1,
    "total": 120,
    "progress_pct": 0.8,
    "anchor_nav": 62549
  },
  "risk": {
    "observation_mode": "NORMAL",
    "kill_switch": false,
    "max_drawdown": -15.2,
    "daily_loss_locked": false
  },
  "factors": {
    "top3": [{"dim": "momentum", "icir": 0.54}, ...],
    "worst2": [{"dim": "volume", "icir": -0.36}, ...]
  }
}
```

### 3.3 后端改造：新增轻量端点

不修改 `monitoring_dashboard.py` 的现有逻辑（防止意外引入 bug）——**新增一个独立的 `/api/dashboard` 端点**，只返回精简后的数据。现有 `/api/monitor-data` 保持不变，`/api/dashboard` 作为新前端的唯一数据源。

```python
@app.route("/api/dashboard")
def api_dashboard_compact():
    """精简看板数据 — 只返回当前策略需要的信息"""
    return jsonify({
        "date": ..., "portfolio": ..., "scoring": ...,
        "signals": ..., "oos": ..., "risk": ..., "factors": ...
    })
```

---

## 4. 视觉精简方案

### 4.1 手机端三卡布局（总览 Tab）

```
┌──────────────────────────┐
│  Serenity     📡 16:05   │  ← header
├──────────────────────────┤
│  ¥62,549  +22.5%          │  ← 净值卡（大号数字）
│  💰 ¥7,314  📈 ¥55,235   │
├──────────────────────────┤
│  招商  700股  36.88       │  ← 持仓卡 (3 张)
│  +3.0%  ¥25,816          │    水平滚动的
│  华工  100股  158.07      │    紧凑卡片
│  +13.4%  ¥15,807         │
│  兴发  400股  34.03       │
│  −0.1%  ¥13,612          │
├──────────────────────────┤
│  📊 OOS  Day 1/120        │  ← OOS 进度条
│  ▓░░░░░░░░░░░░░  0.8%    │
├──────────────────────────┤
│  信号: 买入0  卖出0       │  ← 信号摘要行
│  谨慎买1  持有16          │
├──────────────────────────┤
│  Tab: 总览 | 详情 | OOS  │  ← 精简后的 3-tab bar
└──────────────────────────┘
```

### 4.2 三个 Tab 的内容分配

- **总览**（一屏内完成）：净值 + 持仓 + OOS 进度 + 信号摘要
- **详情**（可滚动）：评分表 + 因子 ICIR + 风控状态
- **OOS**（手动刷新）：三线进度 + 冻结模块清单 + 审计统计

### 4.3 CSS/JS 精简

| 当前 | 目标 |
|------|------|
| `monitor.css` 1452 行 | ~400 行（只保留移动端单列布局） |
| `monitor.js` 1878 行 | ~500 行（3 个 tab，每个 tab ≤100 行渲染逻辑） |
| Chart.js (CDN, ~200KB) | **移除** — 3 只票不需要折线图，数字就是最好的可视化 |
| 桌面 grid 样式 | **全部删除** — 只保留移动端单列设计 |

---

## 5. 实施计划

### Phase A：后端精简（不破坏现有看板）— 1 小时

| # | 任务 | 文件 |
|---|------|------|
| A1 | 新增 `/api/dashboard` 端点 | `monitoring_dashboard.py` |
| A2 | 只返回 7 个分组：portfolio / scoring / signals / oos / risk / factors / date | 同上 |
| A3 | 验证新旧端点同时可用 | 手工 curl 测试 |

### Phase B：前端重建（独立目录，不影响旧看板）— 2-3 小时

| # | 任务 | 文件 |
|---|------|------|
| B1 | 新建 `templates/dashboard.html`（~60 行，3-tab 结构） | 新建 |
| B2 | 新建 `static/js/dashboard.js`（~400 行，3 个 render 函数） | 新建 |
| B3 | 新建 `static/css/dashboard.css`（~300 行，纯移动端） | 新建 |
| B4 | 在 `monitoring_dashboard.py` 添加 `/dashboard` 路由 | 修改 |

### Phase C：旧看板下线 — 1 分钟

| # | 任务 |
|---|------|
| C1 | 将 `/monitor` 路由重定向到 `/dashboard` |
| C2 | 旧文件保留不删，git 里始终可回退 |

---

## 6. 风险

- **Chart.js 移除**：如果后续需要净值折线图，可以在 OOS tab 中用小尺寸 canvas 加回来，不是永久放弃而是"现在不需要"
- **哨兵 Tab 下线**：哨兵数据本身不丢——它仍然在 sentinel_engine 里运行、存入 DB，只是从看板 UI 中移除（可以在 CLI 中查看：`python3 cli.py sentinel-status`）
- **运营 Tab 下线**：auto_gate 状态可以在 OOS tab 显示一行文字就够了——"闸门: LOCKED (样本不足 50)"

---

## 7. 建议审核项

1. **是否同意 7 tab → 3 tab？** 如果不同意，哪些 tab 必须保留？
2. **量化共识（quantdinger）数据是否保留在研究/详情 tab？** 建议移除（冻结期内不参与交易）
3. **Charts.js 是否完全移除？** 建议是——净值就是 3 只票的数字，比折线图直观
4. **新看板端口是否沿用 8401？** 建议 `/dashboard` 路由挂在同一 Flask 实例下，不需要新端口

---

> 报告作者: Claude Code  
> 报告日期: 2026-07-11  
> 下一步: 等待审核后进入 Phase A 实施
