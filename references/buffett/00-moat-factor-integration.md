# 巴菲特护城河因子 → Serenity Monitor 集成方案

> 创建日期: 2026-06-09
> 来源: [agi-now/buffett-skills](https://github.com/agi-now/buffett-skills) 参考文件 03/04/05

---

## 现状审计

### Serenity Monitor 当前护城河评分

| 问题 | 详情 |
|------|------|
| `defensive_moat` | 在 `config.py` 中**静态硬编码**，未从数据源动态计算 |
| 权重 | 仅占 Serenity 五维评分的 **10%**（`SERENITY_WEIGHTS.defensive_moat: 0.10`） |
| 集成状态 | **未接入** `scorer.py` 实际评分流水线（scorer 用 8 维因子：base/zone/momentum/volume/serenity/factor/technical/sentiment，无独立 moat 因子） |
| 现存护城河分 | 纯人工赋值（如招商银行 95、长江电力 98、光迅科技 85） |

### 差距分析 vs. UZI-Skill 的 deep-analysis

| 维度 | UZI-Skill | Serenity Monitor |
|------|-----------|-----------------|
| 护城河评分 | 22 维中的 `14_moat`，调 akshare + 社交热榜 | 无量化计算 |
| 同行对标 | Comps 模块 + PE/PB 分位 | 无 |
| 管理层评估 | DDGS 定性搜索 + 评委规则 | 无 |
| DCF 估值 | 完整 WACC + 5×5 敏感性 | 无 |
| 财务质量 | 180 条量化规则 | 仅有 `factor_engine` 基础因子 |

---

## 两阶段集成方案

### 阶段 A：快速植入（1-2 小时）

新增 `moat_factor.py` 模块 → 接入 scorer 9 维评分体系

#### A1. 护城河 5 维度量化计算（全部通过 akshare 免费源）

```python
# 新增文件: moat_factor.py
# 计算逻辑:

def compute_moat_score(code: str) -> dict:
    """
    返回: {
        "moat_raw": {         # 五项子维度分 0-100
            "roic_stability": 85,     # ROIC 稳定性 + 10年持续性
            "gross_margin_trend": 70, # 毛利率趋势（持续提升→高分）
            "debt_safety": 90,        # 负债安全垫
            "market_position": 75,    # 市场地位（龙头/寡头/竞争性）
            "capex_efficiency": 80,   # 资本效率（FCF/营收比）
        },
        "moat_score": 80,     # 加权总分
        "moat_type": "cost_advantage | brand | switching | network | scale",
        "moat_trend": "widening | stable | narrowing",
        "red_flags": []       # 风险信号列表
    }
    """
```

**数据源映射（全部免费）:**

| 维度 | 数据源 | akshare 接口 |
|------|--------|-------------|
| ROIC 稳定性 | 历年财报 ROE/ROA | `ak.stock_financial_abstract_ths()` |
| 毛利率趋势 | 5 年毛利率序列 | `ak.stock_financial_abstract_ths()` |
| 负债安全垫 | 资产负债率 + 利息覆盖 | `ak.stock_financial_abstract_ths()` |
| 市场地位 | 营收排名/行业集中度 | 东方财富行业板块 |
| 资本效率 | FCF/营收比 | 财报 + 现金流量表 |

#### A2. 接入 scorer.py 流水线

```python
# scorer.py 修改点:

# 新增 import
from moat_factor import compute_moat_score

# score_all() 内部:
moat_result = compute_moat_score(code)
moat_score = moat_result["moat_score"]

# 总分变成 9 维:
total = (
    base_score * score_weight["base"] +
    zone_score * score_weight["zone"] +
    momentum_score * score_weight["momentum"] +
    volume_score * score_weight["volume"] +
    serenity_score * score_weight["serenity"] +
    factor_score * score_weight["factor"] +
    technical_score * score_weight["technical"] +
    sentiment_score * score_weight["sentiment"] +
    moat_score * score_weight["moat"]  # 新增
)
```

#### A3. 权重初始化

```python
# weight_adjuster.py 或 scorer.py 添加默认权重组:
score_weight["moat"] = 0.10  # 护城河占 10%
# 原 8 维等比例缩小: base/zone/momentum → 0.13, volume → 0.04
# serenity/factor → 0.13, technical → 0.09, sentiment → 0.09
```

---

### 阶段 B：深度集成（后续迭代）

从 UZI-Skill 的 `deep-analysis` skill 中移植方法论：

#### B1. 借用 UZI 的 65 评委评审逻辑

UZI-Skill 的 `investor-panel` skill 有 65 位评委的评分数据。我们可以：

1. **定期对持仓股调 UZI API** 获取评委投票结果
2. 将其中的 H 组（科技领袖派）+ I 组（Serenity 瓶颈派）观点提取为情绪信号
3. 将巴菲特/芒格等价值派评分作为护城河交叉验证

```python
# 思路: 通过 CLI 调用 UZI-Skill 的中间结果
import subprocess
result = subprocess.run(
    ["python3", "run.py", code, "--depth", "lite", "--output", "json"],
    cwd=UZI_PATH, capture_output=True, text=True, timeout=120
)
# 提取 panel.json 中评委的 consensus 分 + 护城河评分
```

#### B2. 借鉴 QuantDinger 的回测引擎

当 Serenity 的信号需要验证时，**QuantDinger 的 Agent Gateway + MCP 集成** 可让：
- Codex/Claude Code 通过 MCP 调用 Serenity 的因子引擎
- 在 QuantDinger 后台运行回测验证信号有效性

详见 `references/quantdinger-architecture.md`

---

## 数据验证要点

| 检查项 | 要求 |
|--------|------|
| ROIC 数据至少 5 年 | akshare 提供 10 年财报，取最少 5 年连续数据 |
| 毛利率趋势 | 取近 5 年报（2019-2024），回归斜率 >0 → 加分 |
| 负债安全 | 地产/金融股用不同阈值（招行负债率高但结构好） |
| 同行比较基准 | 使用中证二级行业分类，取同行业所有标的 |
| 红涨绿跌 | 护城河评分本身无涨跌语义，仅数值越高表示壁垒越强 |

## 投用路线

1. ✅ **Phase 0**（今天完成）: 参考文档入库 + 集成方案
2. 🔲 **Phase 1**（本周内）: 通过 Claude Code 编写 `moat_factor.py` 模块
3. 🔲 **Phase 2**（测试验证）: 用 UZI-Skill 对 5 只持仓做交叉验证
4. 🔲 **Phase 3**（上线）: 接入 cron 每日评分流水线
