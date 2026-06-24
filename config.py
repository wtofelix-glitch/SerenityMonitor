"""
SerenityMonitor 配置模块 — 已适配 Serenity 最新策略
候选标的 — 仅限主板（无科创板/创业板）
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

# ============================================================
# 候选标的（全部主板，涛哥有交易资格）
# ============================================================

STOCK_MAP = {
    "002281": {"name": "光迅科技", "market": "sz", "tier": 1},
    "000988": {"name": "华工科技", "market": "sz", "tier": 1},
    "600141": {"name": "兴发集团", "market": "sh", "tier": 2},
    "603083": {"name": "剑桥科技", "market": "sh", "tier": 2},
    "600487": {"name": "亨通光电", "market": "sh", "tier": 2},
    "002428": {"name": "云南锗业", "market": "sz", "tier": 3},
    "600460": {"name": "士兰微", "market": "sh", "tier": 3},
    "603986": {"name": "兆易创新", "market": "sh", "tier": 3},
    "600176": {"name": "中国巨石", "market": "sh", "tier": 3},
    # 防御组合（高分红/低波动，震荡市对冲）
    "600036": {"name": "招商银行", "market": "sh", "tier": 4},
    "600585": {"name": "海螺水泥", "market": "sh", "tier": 4},
    "600900": {"name": "长江电力", "market": "sh", "tier": 4},
    "601398": {"name": "工商银行", "market": "sh", "tier": 4},
    "601006": {"name": "大秦铁路", "market": "sh", "tier": 4},
    # 实盘持仓（全盘扫描推荐）
    "000938": {"name": "紫光股份", "market": "sz", "tier": 2},
}

# Tier 1 首选（光通信/AI算力最核心）
TIER_1_CODES = ["002281", "000988"]
TIER_2_CODES = ["600141", "603083", "600487", "000938"]
TIER_3_CODES = ["002428", "600460", "603986", "600176"]
TIER_4_CODES = ["600036", "600585", "600900", "601398", "601006"]  # 防御组合

# 实盘扩展标的（不在原始14只池中但已持仓）
EXTENDED_CODES = ["000938"]

# 所有标的代码
ALL_CODES = list(STOCK_MAP.keys())

# 新浪 API 前缀
SINA_PREFIX = "http://hq.sinajs.cn/list="

# ============================================================
# 标的深度信息
# ============================================================
# ============================================================
# 标的深度信息 — 2026-06-18 收盘后校准
# 价格基准：光迅266.2 | 华工177.55 | 亨通111 | 剑桥214.54 | 兴发38.65
# ============================================================
STOCK_DETAILS = {
    "002281": {
        "score": 97,
        "buy_zone_low": 175.0,
        "buy_zone_high": 220.0,
        "target_sell": 300.0,
        "reason": "光器件全产业链龙头，AI光互联核心受益 — CPO检测/FAU直接映射Serenity的SIVE瓶颈逻辑",
        "serenity_tag": "CPO_chokepoint",
    },
    "000988": {
        "score": 90,
        "buy_zone_low": 130.0,
        "buy_zone_high": 165.0,
        "target_sell": 220.0,
        "reason": "光模块+激光双主线，AI算力配套 — 激光器+光引擎赛道与SIVE逻辑高度重合",
        "serenity_tag": "laser+optical_engine",
    },
    "603083": {
        "score": 77,
        "buy_zone_low": 155.0,
        "buy_zone_high": 190.0,
        "target_sell": 270.0,
        "reason": "高速光模块弹性标的，800G/1.6T放量 — 受益于CPO光学引擎需求爆发",
        "serenity_tag": "optical_module",
    },
    "600487": {
        "score": 75,
        "buy_zone_low": 82.0,
        "buy_zone_high": 115.0,
        "target_sell": 145.0,
        "reason": "光纤光缆龙头，CPO光纤阵列(FAU)受益 — 防守型+补涨逻辑",
        "serenity_tag": "fiber_infra",
    },
    "002428": {
        "score": 55,
        "buy_zone_low": 78.0,
        "buy_zone_high": 96.0,
        "target_sell": 120.0,
        "reason": "锗衬底材料，CPO用InP衬底的替代表达 — Serenity持有AXTI的映射逻辑",
        "serenity_tag": "substrate_material",
    },
    "600141": {
        "score": 68,
        "buy_zone_low": 30.0,
        "buy_zone_high": 36.0,
        "target_sell": 44.0,
        "reason": "磷化工龙头，红磷/磷化学品用于CPO光对准和Rubin半导体热管理 — Serenity红磷主题映射",
        "serenity_tag": "phosphorus_chemicals",
    },
    "603986": {
        "score": 60,
        "buy_zone_low": 410.0,
        "buy_zone_high": 505.0,
        "target_sell": 625.0,
        "reason": "NOR Flash/DRAM，AI数据中心存储需求外溢 — Serenity重仓SNDK的映射逻辑",
        "serenity_tag": "ai_storage",
    },
    "600460": {
        "score": 45,
        "buy_zone_low": 28.0,
        "buy_zone_high": 34.0,
        "target_sell": 42.0,
        "reason": "功率半导体IDM龙头，800VDC数据中心供电架构受益 — 映射Serenity NVTS/POWI功率半主题",
        "serenity_tag": "power_semiconductor",
    },
    # 防御组合
    "600036": {
        "score": 75,
        "buy_zone_low": 34.0,
        "buy_zone_high": 42.0,
        "target_sell": 52.0,
        "reason": "零售银行龙头，高股息防御 — 震荡市资金避风港，ROE行业领先",
        "serenity_tag": "defensive_dividend",
    },
    "600585": {
        "score": 68,
        "buy_zone_low": 16.0,
        "buy_zone_high": 20.0,
        "target_sell": 24.0,
        "reason": "水泥龙头+高分红 — 基建稳增长受益，低估值防御属性强",
        "serenity_tag": "infra_dividend",
    },
    "600900": {
        "score": 80,
        "buy_zone_low": 24.0,
        "buy_zone_high": 30.0,
        "target_sell": 36.0,
        "reason": "水电绝对龙头，现金流稳定 — 类债券属性，极端行情避险首选",
        "serenity_tag": "utility_defensive",
    },
    "601398": {
        "score": 78,
        "buy_zone_low": 6.6,
        "buy_zone_high": 8.2,
        "target_sell": 10.0,
        "reason": "全球最大银行，股息率5%+ — 国家队护盘标的，系统性重要",
        "serenity_tag": "bank_defensive",
    },
    "601006": {
        "score": 65,
        "buy_zone_low": 4.3,
        "buy_zone_high": 5.3,
        "target_sell": 6.6,
        "reason": "煤炭运输专线，稳定现金流 — 高股息+低波动，红利策略核心",
        "serenity_tag": "railway_dividend",
    },
    # 实盘持仓（全盘扫描推荐 — v3.0 加入评分池）
    "000938": {
        "score": 75,
        "buy_zone_low": 22.0,
        "buy_zone_high": 30.0,
        "target_sell": 38.0,
        "reason": "AI算力交换机龙头，新华三核心资产 — 数据中心交换机+服务器双主线，AI基建核心受益",
        "serenity_tag": "ai_switch",
    },

    "600176": {
        "score": 55,
        "buy_zone_low": 36.0,
        "buy_zone_high": 44.0,
        "target_sell": 54.0,
        "reason": "玻纤/电子布全球龙头，AI服务器PCB基材需求爆发 — 6月金股(7家券商推荐)，同赛道宏和科技已20倍",
        "serenity_tag": "electronic_fabric",
    },
}

# ============================================================
# 🗑️ [弃用] 旧 5 维 Serenity 静态权重
# 已迁移到 scorer.py 的 9 维动态评分体系（含辩论权重）
# 保留仅用于兼容旧函数调用，新流水线已不使用
# ============================================================
SERENITY_WEIGHTS_LEGACY = {
    "cpo_alignment": 0.35,
    "bottleneck_position": 0.25,
    "ai_capex_exposure": 0.20,
    "defensive_moat": 0.10,
    "momentum_fit": 0.10,
}

SERENITY_DIMENSIONS = {
    "002281": {"cpo_alignment": 95, "bottleneck_position": 85, "ai_capex_exposure": 90, "defensive_moat": 85, "momentum_fit": 70},
    "000988": {"cpo_alignment": 85, "bottleneck_position": 75, "ai_capex_exposure": 85, "defensive_moat": 75, "momentum_fit": 70},
    "603083": {"cpo_alignment": 80, "bottleneck_position": 55, "ai_capex_exposure": 80, "defensive_moat": 50, "momentum_fit": 65},
    "600487": {"cpo_alignment": 65, "bottleneck_position": 45, "ai_capex_exposure": 60, "defensive_moat": 70, "momentum_fit": 80},
    "002428": {"cpo_alignment": 40, "bottleneck_position": 30, "ai_capex_exposure": 50, "defensive_moat": 45, "momentum_fit": 70},
    "600141": {"cpo_alignment": 55, "bottleneck_position": 60, "ai_capex_exposure": 60, "defensive_moat": 65, "momentum_fit": 65},
    "603986": {"cpo_alignment": 35, "bottleneck_position": 30, "ai_capex_exposure": 70, "defensive_moat": 60, "momentum_fit": 65},
    "600460": {"cpo_alignment": 25, "bottleneck_position": 40, "ai_capex_exposure": 50, "defensive_moat": 45, "momentum_fit": 60},
    "600176": {"cpo_alignment": 30, "bottleneck_position": 35, "ai_capex_exposure": 55, "defensive_moat": 75, "momentum_fit": 60},
    # 实盘持仓（全盘扫描推荐）
    "000938": {"cpo_alignment": 60, "bottleneck_position": 55, "ai_capex_exposure": 80, "defensive_moat": 70, "momentum_fit": 65},
    # 防御组合 — 低CPO/瓶颈暴露，高护城河/动量
    "600036": {"cpo_alignment": 10, "bottleneck_position": 5, "ai_capex_exposure": 5, "defensive_moat": 95, "momentum_fit": 65},
    "600585": {"cpo_alignment": 15, "bottleneck_position": 10, "ai_capex_exposure": 10, "defensive_moat": 80, "momentum_fit": 60},
    "600900": {"cpo_alignment": 5, "bottleneck_position": 5, "ai_capex_exposure": 0, "defensive_moat": 98, "momentum_fit": 70},
    "601398": {"cpo_alignment": 5, "bottleneck_position": 5, "ai_capex_exposure": 0, "defensive_moat": 95, "momentum_fit": 65},
    "601006": {"cpo_alignment": 5, "bottleneck_position": 5, "ai_capex_exposure": 0, "defensive_moat": 85, "momentum_fit": 60},

}

SUGGESTED_TARGETS = {
    code: {
        "target_high": d["target_sell"],
        "target_low": d["buy_zone_low"],
        "buy_zone": f"{d['buy_zone_low']:.0f}-{d['buy_zone_high']:.0f}",
    }
    for code, d in STOCK_DETAILS.items()
}


@dataclass
class StockConfig:
    code: str
    name: str
    market: str
    tier: int
    buy_price: float = 0.0
    buy_date: str = ""
    target_high: float = 0.0
    target_low: float = 0.0
    stop_loss: float = 0.0
    is_active: bool = False
    notes: str = ""

    @property
    def sina_code(self) -> str:
        return f"{self.market}{self.code}"


def get_default_stocks() -> list[StockConfig]:
    stocks = []
    for code, info in STOCK_MAP.items():
        stocks.append(StockConfig(code=code, name=info["name"], market=info["market"], tier=info["tier"]))
    return stocks


def compute_serenity_score(code: str) -> float:
    """旧 5 维静态评分，已弃用，保留兼容旧调用"""
    dims = SERENITY_DIMENSIONS.get(code)
    if not dims:
        return 0.0
    total = sum(dims[k] * SERENITY_WEIGHTS_LEGACY[k] for k in SERENITY_WEIGHTS_LEGACY if k in dims)
    return round(total, 1)


# ============================================================
# 🆕 防御/周期标的 Serenity 补偿权重
# 旧五维（cpo_alignment 35% + bottleneck 25% + ai_capex 20%）对 T4 传统蓝筹天然不公
# 这些标的的 CPO/AI 维度是 5-15 分 vs 护城河 80-98 分
# 弱势/震荡市场激活补偿 → 护城河权重从 10% → 60%，让防御价值正确体现
# ============================================================
DEFENSIVE_SERENITY_WEIGHTS = {
    "cpo_alignment": 0.05,
    "bottleneck_position": 0.05,
    "ai_capex_exposure": 0.05,
    "defensive_moat": 0.60,
    "momentum_fit": 0.25,
}


def compute_serenity_score_compensated(code: str) -> float:
    """对防御组合（Tier 4）用补偿权重计算 Serenity 匹配分

    防御标的护城河高（80-98）但旧五维权重仅 10%，
    补偿后护城河权重 60% + 动量 25%，让防御价值正确体现。

    非 T4 标的直接返回原始分。
    """
    info = STOCK_MAP.get(code, {})
    if info.get("tier", 0) != 4:
        return compute_serenity_score(code)

    dims = SERENITY_DIMENSIONS.get(code)
    if not dims:
        return compute_serenity_score(code)

    total = sum(dims[k] * DEFENSIVE_SERENITY_WEIGHTS[k]
                for k in DEFENSIVE_SERENITY_WEIGHTS if k in dims)
    return round(total, 1)


# ============================================================
# 🆕 资金管理配置
# ============================================================
CAPITAL_CONFIG = {
    "initial_capital": 51066.41,       # 启动资金 (士兰微800x35.442 + 剑桥100x204.642 + 现金2248.61)
    "target_capital": 102133.0,        # 目标翻倍 → 102133
    "target_months": 3,                # 3 个月
    "max_positions": 2,                # 最多同时持仓 2 只（高集中度→翻倍目标）
    "max_single_weight": 0.85,         # 单只最大仓位 85%（翻倍目标→重仓集中）
    "min_single_weight": 0.30,         # 单只最小仓位 30%
    "enter_threshold": 68,             # 买入最低评分（放宽至68→抓更多机会）
    "exit_threshold": 48,              # 持仓评分跌破此值建议卖出
    "reserve_cash_ratio": 0.03,        # 保留 3% 现金（翻倍目标→现金=浪费）
    "commission_rate": 0.00025,        # 佣金万2.5
    "stamp_tax_rate": 0.001,           # 印花税千1（卖出时）
    # 🆕 激进翻倍模式 — 一键切换保守/激进策略参数
    # True: 高Kelly仓位 + 紧止损 + 低现金 + 更宽标的筛选
    # 翻倍模式建议: 震荡市/牛市开启，连续亏损>2笔后关闭
    "aggressive_mode": True,
    # Kelly 翻倍参数（portfolio.py 动态读取）
    "_kelly_base": 0.3,              # 翻倍模式: 0.3 (保守0.2)
    "_kelly_multiplier": 0.55,       # 翻倍模式: 0.55 → range 0.3~0.85 (保守0.5→0.2~0.7)
}


def get_effective_config() -> dict:
    """根据 aggressive_mode 返回实际生效的参数
    
    翻倍目标下激进模式的参数偏移：
    - Kelly fraction: 0.3-0.85 (保守 0.2-0.7)
    - min_single_weight: 0.15 (保守 0.30) 
    - reserve_cash: 0.01 (保守 0.03)
    - enter_threshold: 64 (保守 68)
    - exit_threshold: 42 (保守 48)
    - 震荡市自动启用（熊市/连续亏损>2时建议关闭）
    """
    cfg = dict(CAPITAL_CONFIG)
    risk = dict(RISK_CONFIG)
    
    if cfg.get("aggressive_mode"):
        cfg["min_single_weight"] = 0.10  # 翻倍目标: 0.15→0.10, 让现金充分利用不浪费槽位
        cfg["reserve_cash_ratio"] = 0.002  # 翻倍目标: 0.5%→0.2%, 趋势市现金=最大浪费
        cfg["enter_threshold"] = 56  # 翻倍目标: 60→56, 趋势市叠加-4→52, MR叠加-5→51
        cfg["exit_threshold"] = 36  # 翻倍目标: 38→36, 更宽松持有让利润奔跑
        risk["stop_loss_pct"] = -0.03      # 紧止损 -3%
        risk["trailing_stop_pct"] = 0.06   # 移动止盈回撤 6%
        risk["max_daily_loss_pct"] = -0.06  # 翻倍目标: 允许更大单日波动 -6%, 与跟踪止损6%对齐
        risk["profit_take_level1"] = 0.08  # 止盈一档 +8%
        risk["profit_take_level2"] = 0.16  # 止盈二档 +16%
        risk["profit_take_level3"] = 0.28  # 止盈三档 +28%
        # Kelly 翻倍系数（在 portfolio.py 中读取）
        cfg["_kelly_base"] = 0.40  # 翻倍目标: 0.35→0.40, 趋势牛市重仓
        cfg["_kelly_multiplier"] = 0.70  # 翻倍目标: 0.65→0.70 → range 0.40-1.0
    
    return {"capital": cfg, "risk": risk}


# ============================================================
# 🆕 风控参数
# ============================================================
RISK_CONFIG = {
    "stop_loss_pct": -0.04,            # 硬止损 -4%（翻倍路径不允许大回撤）
    "use_atr_stop": True,              # 是否启用 ATR 动态止损
    "atr_stop_multiplier": 1.5,        # ATR 倍数（从2.5收紧至1.5）
    "atr_stop_min_pct": -0.04,         # 最小止损百分比（从-5%收紧至-4%）
    "atr_stop_max_pct": -0.12,         # 最大止损百分比（从-15%收紧至-12%）
    "trailing_stop_pct": 0.08,         # 移动止损回撤 12%→8%（锁定利润更快）
    "max_daily_loss_pct": -0.04,       # 单日最大亏损 -5%→-4%
    "max_portfolio_drawdown": -0.12,   # 总资金最大回撤 -15%→-12%
    "profit_take_level1": 0.10,        # 止盈一档 +15%→+10%
    "profit_take_level2": 0.20,        # 止盈二档 +30%→+20%
    "profit_take_level3": 0.35,        # 止盈三档 +50%→+35%
    "partial_exit_level1": 0.50,       # 一档出50%（200股→出100股，与100股最小单位对齐）
    "partial_exit_level2": 0.50,       # 二档出剩余持仓的50%（剩余100股→全部卖出）
    "optimizer_min_signal": 0.05,      # 组合优化器最小有效信号阈值
    "optimizer_max_position_pct": 0.85,  # 翻倍目标: 与max_single_weight(85%)对齐
    "optimizer_min_trade": 3000,       # 翻倍目标: 5000→3000, 更灵活       # 最小调仓金额
    # 🆕 硬止损规则 — 保护本金
    "max_single_loss_pct": -0.06,      # 单只亏损不超过 -6%
    "max_consecutive_losses": 2,       # 连续亏损2笔 → 强制空仓
    "cool_down_days": 3,               # 强制空仓天数
}

# ============================================================
# 🆕 信号阈值
# ============================================================
SIGNAL_CONFIG = {
    # v3.0 阈值重校准 (2026-06-24)
    # 数据依据: 272条信号历史绩效
    #   STRONG_BUY(≥68): 14条 WR-5d=21.4% avg=+1.0% → 不合格，提高至74
    #   BUY(63-68):     22条 WR-5d=31.8% avg=+2.6% → 一般，提高至66
    #   CAUTION_BUY(60-66): 54条 WR-5d=53.7% avg=+3.8% → 合格
    #   HOLD(50-60):     100条 WR-5d=60.0% avg=+5.2% → 表现最好
    #   SELL(<45):       46条 后1日涨80% → 缓冲区已加(Phase 3)
    #   非MR模式下因子不翻转，评分分布上移约3-5分
    "buy_threshold": 66.0,             # 综合评分 > 66 → BUY 信号（原63→66, 基于绩效收紧）
    "strong_buy_threshold": 74.0,      # 综合评分 > 74 → 强力买入（原68→74, 仅真正优质信号）
    "sell_threshold": 45.0,            # 综合评分 < 45 → SELL 信号（不变,+sell缓冲）
    "hold_high": 60.0,                 # 评分在 60-66 → 谨慎买入 CAUTION_BUY
    "hold_low": 50.0,                  # 评分在 50-60 → 持有观察 HOLD
    "pos_exit_threshold": 50.0,        # 持仓评分跌破50 → 强制减仓建议
    # 🆕 动态门槛：震荡市放宽买入
    "market_buy_adjust": {             # 不同市况下的门槛调整值
        "危险": 5,                     # +5: 门槛更严
        "谨慎": 3,
        "中性": 0,
        "积极": 0,
        "机会": -2,                    # -2: 门槛放宽
        "震荡": -5,                    # 🆕 震荡市额外-5（翻倍目标放宽）
    },
    "factor_signal_confirm": 0.20,     # 因子信号 >= 0.20 确认买入
    "factor_signal_reject": -0.15,     # 因子信号 <= -0.15 拒绝买入
    "volume_surge_ratio": 3.0,         # 成交量突增 3 倍
    "volume_dry_ratio": 0.3,           # 成交量萎缩 70%
    "fourteen_factor_enabled": True,   # 启用14因子独立维度
}

# ============================================================
# 🆕 策略配置
# ============================================================
STRATEGY_CONFIG = {
    "momentum_lookback": 20,           # 动量回溯窗口
    "ma_short": 5,                     # 短均线
    "ma_long": 20,                     # 长均线
    "rsi_period": 14,                  # RSI 周期
    "rsi_oversold": 30,                # RSI 超卖
    "rsi_overbought": 70,              # RSI 超买
    "bb_period": 20,                   # 布林带周期
    "bb_std": 2.0,                     # 布林带标准差
    "atr_period": 14,                  # ATR 周期
    "volume_ma_period": 20,            # 成交量均线周期
}

# ============================================================
# 三策略分配（文档参考）
# ============================================================
STRATEGY_ALLOCATION = {
    "dividend_lowvol": {"name": "红利低波底仓", "weight": 0.50, "description": "高股息率+低波动标的，长期持有，季度调仓", "rebalance_freq": "quarterly"},
    "multi_factor_quant": {"name": "多因子量化进攻", "weight": 0.30, "description": "现有Serenity多因子评分系统，机动调仓", "rebalance_freq": "monthly"},
    "etf_momentum": {"name": "ETF动量轮动", "weight": 0.20, "description": "多ETF动量排名，定期轮动", "rebalance_freq": "weekly"},
}

MARKET_ADJUSTMENTS = {
    "牛市": {"dividend_weight": -0.05, "quant_weight": +0.05, "etf_weight": 0, "description": "牛市加仓量化进攻，减仓红利防御"},
    "熊市": {"dividend_weight": +0.15, "quant_weight": -0.10, "etf_weight": -0.05, "description": "熊市全面转向红利防御"},
    "震荡市": {"dividend_weight": -0.20, "quant_weight": +0.25, "etf_weight": -0.05, "description": "震荡市加仓量化择股，红利降至20%"},  # 🆕 震荡市变进取
    "结构性牛市": {"dividend_weight": -0.03, "quant_weight": +0.03, "etf_weight": 0, "description": "结构性牛市适当增量化减红利"},
}

# ============================================================
# 参考标的 (指数/ETF — 仅用于基准对比，不参与评分/交易)
# ============================================================
REFERENCE_SYMBOLS = {
    "sh000001": {"name": "上证指数", "market": "sh", "type": "index"},
    "sh512010": {"name": "医药ETF", "market": "sh", "type": "etf"},
    "sh512100": {"name": "1000ETF", "market": "sh", "type": "etf"},
    "sh512480": {"name": "半导体ETF", "market": "sh", "type": "etf"},
    "sh515050": {"name": "AI智能ETF", "market": "sh", "type": "etf"},
    "sh563000": {"name": "中国A50ETF", "market": "sh", "type": "etf"},
}
