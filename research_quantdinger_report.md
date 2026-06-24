# QuantDinger LLM 研究报告生成模块调研报告

> 调研时间: 2026-06-09  
> 目标: 提炼可复用到 SerenityMonitor cli.py report 的精华

---

## 2026-06-24 增量调研与已落地融合

> 本次刷新使用 GitHub 插件 + agent-reach GitHub 后端，确认主仓为
> [brokermr810/QuantDinger](https://github.com/brokermr810/QuantDinger)，
> 2026-06-24 仍活跃更新，主仓约 8.6k stars；另有
> [QuantDinger-Vue](https://github.com/brokermr810/QuantDinger-Vue) 和
> [QuantDinger-Mobile](https://github.com/brokermr810/QuantDinger-Mobile)。

新增阅读/核对范围：

| 文件/文档 | 复用判断 |
|---|---|
| `README.md` | QuantDinger 当前定位是 self-hosted AI quant infrastructure，闭环是 research -> strategy -> backtest -> paper/live execution -> monitoring。 |
| `backend_api_python/app/services/fast_analysis.py` | 最大价值仍是 rule-based `objective_score`、多周期 consensus、quality multiplier、LLM 输出校正，而不是让 LLM 直接拍板。 |
| `docs/agent/AI_INTEGRATION_DESIGN.md` | Agent Gateway 分 R/W/B/N/C/T scope、token hash、rate limit、audit、idempotency；Serenity 先只吸收只读 API 与审计边界思想。 |
| `docs/agent/AGENT_QUICKSTART.md` | Backtest 走 async job + `Idempotency-Key`，策略脚本必须显式生成信号列；Serenity 后续接回测时应沿用幂等与 next-bar/open 对齐原则。 |
| `docs/agent/agent-openapi.json` | Agent 面向稳定 envelope + scoped Bearer token；Serenity 已新增只读 `/api/quantdinger-consensus`，不新增写操作。 |

已落地到 Serenity：

1. `quant_fusion.py` 新增 `QUANTDINGER_ESSENCE` 与 `build_quantdinger_consensus()`。
2. 共识层将 Serenity `scoring_history` 的总分/技术/Alpha/情绪/匹配/护城河/UZI 归一到 `-100~+100`，生成 objective score。
3. 用最新、5日、20日三个视角做加权共识，输出 `consensus_score`、`consensus_decision`、`agreement_ratio`、`quality_multiplier`、`confidence`、`trend_outlook`。
4. `cli.py fusion` 原入口无需改命令即可展示 QuantDinger 精华、全局倾向、Top 机会和风险标的。
5. `monitoring_dashboard.py` 新增只读 API：`GET /api/quantdinger-consensus`。
6. 新增/更新 `tests/test_quant_fusion.py`，覆盖精华清单、共识计算、只读性与报告输出。

验证结果：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_quant_fusion.py tests/test_dashboard_security.py -q
# 9 passed

.venv/bin/python cli.py fusion
# 输出 Serenity x QuantDinger 量化融合体检，真实库覆盖 14/14
```

---

## 一、调研范围

完整阅读了 QuantDinger (v3.0.31) 以下核心文件：

| 文件 | 行数 | 作用 |
|------|------|------|
| `app/services/fast_analysis.py` | 2779 | **核心分析引擎** |
| `app/services/market_data_collector.py` | ~2400 | 统一数据采集器 |
| `app/services/llm.py` | ~700 | LLM 多provider封装 |
| `app/routes/fast_analysis.py` | ~310 | API 路由 |
| `app/services/analysis_memory.py` | 未完整读 | 分析记忆层 |

---

## 二、整体架构

### 2.1 报告生成流水线

```
fast_analysis.py::FastAnalysisService.analyze()

用户请求 (symbol, market, timeframe)
  │
  ▼
Phase 1: 数据采集 (MarketDataCollector.collect_all)
  ├── 并行: price + kline (ThreadPoolExecutor, 4线程)
  ├── 并行: fundamental + company (美股Finnhub / 加密固定描述)
  ├── 本地计算: 技术指标 (RSI, MACD, MA, BB, ATR, volume_ratio)
  ├── 并行: crypto_factors (Coinglass + Binance API)
  ├── 并行: macro (VIX, DXY, TNX, Fear&Greed, 复用global_market缓存)
  └── 并行: news (Finnhub + 搜索引擎 fallback + 全球重大事件)
  │
  ▼
Phase 2: 多周期客观评分 (Multi-Timeframe Consensus)
  ├── 主周期 + 补充周期 (e.g. 1D + 4H + 1W)
  ├── 每个周期算 objective_score (rule-based, no LLM)
  └── 加权共识: 长周期权重更高 (1D=1.30, 1W=1.35)
  │
  ▼
Phase 3: LLM 调用
  ├── 构建 system_prompt + user_prompt (含所有原始数据)
  ├── 可选: ensemble voting (多个LLM模型投票)
  └── 输出结构化 JSON
  │
  ▼
Phase 4: 共识校正 (LLM + 客观分互相约束)
  ├── objective_score >= abs(threshold) → 用共识决策覆盖LLM
  └── 质量评分 (数据完整性) → 降低信心
  │
  ▼
Phase 5: 验证与约束 + 仓位规模调整
  │
  ▼
输出: 完整 JSON (含趋势展望、记忆存储)
```

### 2.2 关键设计原则

1. **数据为王** — 先做好数据获取（稳定、准确、统一数据源），再谈分析
2. **客观评分优先** — rule-based 评分作为"地面真理"，LLM 只负责解释和叙事
3. **LLM 不做决策** — 最终决策由客观评分+共识决定，LLM 建议仅作为参考
4. **强约束 prompt** — 价格必须 ±10% 内，止损/止盈方向必须正确
5. **多周期共识** — 单一周期容易误判，多周期加权显著提升稳定性

---

## 三、核心模块详解

### 3.1 数据采集器 (market_data_collector.py)

```
MarketDataCollector.collect_all()
  ├── _get_price()      → kline_service.get_realtime_price()
  ├── _get_kline()      → DataSourceFactory.get_kline()
  ├── _get_fundamental() → Finnhub (美股) / 固定描述 (加密)
  ├── _get_company()    → Finnhub company_profile2
  ├── _get_macro_data() → VIX/DXY/TNX/Fear&Greed → 复用global_market缓存
  ├── _get_news()       → Finnhub → 搜索引擎 fallback → 全球重大事件
  ├── _get_crypto_factors() → Coinglass + Binance API
  └── _calculate_indicators() → 本地计算 (无外部依赖)
```

**关键技术指标计算** (本地, no LLM):
- RSI(14): Wilder 平滑算法
- MACD: EMA12/26 + 信号线 EMA9 (SMA 种子)
- MA: SMA5/10/20 + 趋势判定
- 支撑/阻力: 枢轴点 + 摆动高低 + 布林带 三者平均
- ATR(14): Wilder 算法
- Bollinger Bands: 20 SMA ± 2σ
- Volume ratio: 最新/20日均量
- Price position: 20日区间百分位

### 3.2 客观评分系统 (no LLM)

所有评分范围: **-100 到 +100**

| 评分维度 | 子项 | 权重系数 |
|----------|------|----------|
| **Technical** | RSI(30%), MACD(25%), MA趋势(25%), 24h涨跌幅(20%), 价格位置, 布林带, 成交量比, 波动率 | 组合加权 |
| **Fundamental** | PE, ROE, 营收增长, 利润率, 负债率 | 各 1/5 |
| **Sentiment** | 新闻正负比例 + 地缘事件惩罚 (severe=-42, moderate=-18) | 60分满 |
| **Macro** | VIX(-50~+20), DXY(-30~+30), TNX(-30~+30), Fear&Greed | 归一化 |
| **Crypto Factor** | 资金费率, OI变化, 多空比, 交易所净流, 稳定币净流 | 求和 |

**决策阈值**:
- score >= +20 → BUY
- score <= -20 → SELL
- -20 < score < +20 → HOLD

### 3.3 LLM Prompt 模板 (可直接复用)

QuantDinger 的 prompt 设计是**最大的可复用资产**。核心结构：

```
SYSTEM PROMPT:
1. 角色设定 (20年资深分析师, 保守客观)
2. 语言指令 (强制中文/英文)
3. 决策规则 (多因子权重优先级)
4. Crypto 市场结构覆盖规则
5. 技术面决策指导 (pre-calculated: 支撑/阻力/ATR)
6. 价格约束 (止盈止损方向, ±10%)
7. 必须分析的维度 (技术/宏观/新闻/预测市场/基本面/风险评估)
8. JSON 输出 schema (含字段类型约束)
9. 客观评分参考 (作为锚点)

USER PROMPT:
1. 实时数据: 价格, 24h变化, 支撑/阻力
2. 技术指标: RSI, MACD, MA, 波动率, 价格位置
3. 宏观环境 (格式化的 DXY/VIX/TNX 摘要)
4. 新闻 (最多5条, 含情绪标签)
5. 基本面/公司信息
6. 历史相似模式 (从记忆层获取)
7. Crypto 专属因子
8. 强约束提示 (地缘事件最高优先级)
```

**关键设计特点**:
- 在 system prompt 中 **多次重复约束**，避免 LLM 忽略
- 输出 schema **嵌入在 prompt 中**，确保 JSON 格式稳定
- 客观评分作为"参考"灌入，但实际不依赖 LLM 算分
- 用 emoji 分段（📊🌐📰💼📈）提高 LLM 的结构理解

### 3.4 输出格式 (JSON)

```json
{
  "decision": "BUY|SELL|HOLD",
  "confidence": 0-100,
  "summary": "执行摘要 2-3句",
  "detailed_analysis": {
    "technical": "技术分析文字",
    "fundamental": "基本面分析",
    "sentiment": "情绪分析"
  },
  "trading_plan": {
    "entry_price": 数值,
    "stop_loss": 数值,
    "take_profit": 数值,
    "position_size_pct": 1-100,
    "timeframe": "short|medium|long"
  },
  "scores": {
    "technical": 0-100,
    "fundamental": 0-100,
    "sentiment": 0-100,
    "overall": 0-100
  },
  "objective_score": {
    "technical_score": -100~+100,
    "fundamental_score": -100~+100,
    "sentiment_score": -100~+100,
    "macro_score": -100~+100,
    "overall_score": -100~+100
  },
  "consensus": {
    "consensus_score": -100~+100,
    "consensus_decision": "BUY|SELL|HOLD",
    "agreement_ratio": 0.0-1.0,
    "quality_multiplier": 0.0-1.0,
    "market_regime": "trending|ranging"
  },
  "trend_outlook": {
    "next_24h": {"score", "trend", "strength"},
    "next_3d": {"score", "trend", "strength"},
    "next_1w": {"score", "trend", "strength"},
    "next_1m": {"score", "trend", "strength"}
  },
  "reasons": ["原因1", "原因2"],
  "risks": ["风险1", "风险2"],
  "analysis_time_ms": 数值
}
```

---

## 四、可复用精华提炼

### 4.1 可直接复用的设计模式

| 模式 | QuantDinger 实现 | Serenity 适配方案 |
|------|------------------|-------------------|
| **客观评分 + LLM 联合决策** | rule-based 评分 + LLM 叙事 → consensus 裁决 | Serenity 已有9维评分 → 增加 objective_score 层 |
| **多周期共识** | 多 K 线周期分别评分 → 加权综合 | 直接用日线数据，可简单加周线补充 |
| **强约束 Prompt** | 价格 ±10%, 止损方向校验 | **直接复用 prompt 模板** |
| **结构化 JSON 输出** | 嵌入在 prompt 中的 schema | 适配 Serenity 的回复风格 |
| **数据质量降权** | 缺失数据按比例降信心 | Serenity 数据源稳定，可以不降 |
| **地缘事件检测** | 正则匹配 + 分级惩罚 (-42/-18) | A股市场可简化或去除 |
| **趋势展望 (多时间 horizon)** | 24h/3d/1w/1m | Serenity focus 日线级别 |

### 4.2 需要改造的部分

| 组件 | QuantDinger | Serenity 改造 |
|------|-------------|---------------|
| 数据源 | Finnhub + Coinglass + Binance | A股: akshare + 新浪 + 东方财富 |
| 基本面评分 | PE/ROE/利润率/负债率 | 需适配 A 股数据可得性 |
| 宏观数据 | VIX/DXY/TNX (美股/加密视角) | 可改为 A 股宏观: 北向资金, 两融余额, 大盘指数 |
| 新闻获取 | Finnhub + 搜索引擎 | Serenity 已有 sentiment_engine |
| Crypto 因子 | 资金费率/OI/多空比 | **不需要** (Serenity 纯 A 股) |

### 4.3 最小可行实现路径

为 Serenity `cli.py report` 增加 LLM 文字研报：

**Step 1: 创建 `llm_report.py`** (~200 行)
- 输入: Serenity 已有的 9 维评分 + 价格数据 + 信号
- 格式化为 prompt (复用 QuantDinger 的模板结构)
- 调用外部 LLM API (OpenAI/DeepSeek 等)
- 输出 Markdown 报告文本

**Step 2: 修改 `cli.py report`**
- 在现有数字表格后追加 LLM 生成的文字研报
- 可选: `--llm` 参数控制是否启用

**Step 3: 可选增强 — 客观评分层**
- 借鉴 QuantDinger 的 `_calculate_technical_score()` 规则
- 作为 Serenity 现有评分的补充校准

---

## 五、关键代码片段

### 5.1 Prompt 模板核心 (fast_analysis.py 的 _build_analysis_prompt)

```
QuantDinger 的 system_prompt 约 1800 字, 包含:
├── 角色设定 + 语言指令
├── 决策规则 (多层因子优先级层次)
├── 多周期共识 + 置信度阈值
├── 技术面决策指导 (RSI/MACD/MA 结合解读)
├── 价格约束规则 (止损止盈方向检查)
├── 必需的 8 个分析维度
└── JSON 输出 schema (精确到字段类型)

user_prompt 包含:
├── 实时数据 (价格/24h变化/支撑阻力)
├── 技术指标摘要
├── 宏观环境格式化摘要
├── 新闻摘要
├── 基本面/公司信息
└── 历史相似模式
```

### 5.2 共识决策逻辑 (fast_analysis.py 的 analyze 方法)

```python
# 多周期加权
weights = {"1M": 0.75, "4H": 1.10, "1D": 1.30, "1W": 1.35}
consensus_score = Σ(score * w) / Σ(w)
consensus_decision = BUY if >= +20, SELL if <= -20

# 共识覆盖 LLM 的条件
if abs(consensus_score) >= min_abs_override  # 默认 15
    final_decision = consensus_decision
    # LLM 被覆盖但在 summary 中添加说明
```

### 5.3 地缘事件情绪检测

```python
严重惩罚 (severe): -42 分
  - war, invasion, airstrike, military attack, 战争爆发...
中等惩罚 (moderate): -18 分
  - geopolitical, sanctions, border clash, 地缘政治危机...
封顶: -55 分 (单次分析)
```

### 5.4 仓位规模调整

```python
position_size_pct *= quality_multiplier  # 数据缺失降权
position_size_pct *= agreement_scale     # 多周期不一致降权
if decision == HOLD:
    position_size_pct *= 0.25
```

---

## 六、总结

QuantDinger 的 LLM 研报模块最精华的设计是三点：

1. **Hybrid 架构**: 客观评分做地基 + LLM 做叙事 + 共识做裁判 → 避免 LLM 幻觉
2. **强约束 Prompt**: 2000 字的系统提示中包含多层约束、价格校验、JSON schema → 输出高度可控
3. **多周期共识**: 多时间框架加权决策 → 提高稳定性

**直接可复制到 Serenity 的**:
- Prompt 模板设计模式和 JSON schema
- 客观评分+LLM 联合决策架构
- 决策约束逻辑 (价格验证、方向检查)
- 趋势展望 (多时间 horizon 输出)

**需要自己实现的**:
- A 股数据适配器 (替代 Finnhub/Coinglass)
- Serenity 特有的 9 维评分整合
- 调用本地/可用的 LLM provider
