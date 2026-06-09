"""
shared_state.py — Serenity 共享评分运行时状态

借鉴 TradingAgents 的 AgentState 模式：
所有因子/评分函数读写同一个 SharedState 对象，
替代当前 scorer.py 中通过函数参数链传递数据的方式。

使用方式:
    state = SharedState(code="600487", name="亨通光电")
    state.set_snap(...)
    state.set_base(...)
    state.set_moat(...)
    result = state.to_result()
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class SharedState:
    """单只标的的运行时评分状态。
    
    所有评分维度读写同一个 dict-style 对象，
    减少函数参数链和临时变量传递。
    """
    
    # ====== 标的标识 ======
    code: str = ""
    name: str = ""
    
    # ====== 原始行情 ======
    price: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open_price: float = 0.0
    
    # ====== 九维评分 ======
    zone_score: float = 50.0
    zone_label: str = ""
    zone_class: str = ""
    base_score: float = 50.0
    momentum_score: float = 50.0
    volume_score: float = 50.0
    serenity_score: float = 50.0
    factor_score: float = 50.0
    technical_score: float = 50.0
    sentiment_score: float = 50.0
    moat_score: float = 50.0            # 护城河（巴菲特框架量化）
    multi_cycle_score: float = 50.0     # 多周期融合因子
    multi_cycle_detail: dict = field(default_factory=dict)
    
    # ====== 权重 ======
    weights: dict = field(default_factory=dict)
    total_score: float = 0.0
    
    # ====== 信号 ======
    signal_action: str = "HOLD"
    signal_confidence: float = 0.0
    
    # ====== 技术指标 ======
    tech: dict = field(default_factory=dict)
    
    # ====== 因子细节 ======
    factor_detail: dict = field(default_factory=dict)
    
    def set_snap(self, snap: dict):
        """从行情快照填充原始数据"""
        self.price = snap.get("close", 0)
        self.change_pct = snap.get("change_pct", 0)
        self.volume = snap.get("volume", 0)
        self.high = snap.get("high", 0)
        self.low = snap.get("low", 0)
        self.open_price = snap.get("open", 0)
    
    def compute_total(self):
        """计算加权总分

        weights 默认值从 weight_adjuster.DEFAULT_WEIGHTS 同步，
        确保与 scorer.py 一致。
        """
        if not self.weights:
            from weight_adjuster import load_adjusted_weights
            self.weights = load_adjusted_weights()
        w = self.weights
        self.total_score = (
            self.base_score * w.get("base", 0.14) +
            self.zone_score * w.get("zone", 0.14) +
            self.momentum_score * w.get("momentum", 0.14) +
            self.volume_score * w.get("volume", 0.04) +
            self.serenity_score * w.get("serenity", 0.14) +
            self.factor_score * w.get("factor", 0.14) +
            self.technical_score * w.get("technical", 0.09) +
            self.sentiment_score * w.get("sentiment", 0.09) +
            self.moat_score * w.get("moat", 0.10)
        )
        return self.total_score
    
    def to_result(self) -> dict:
        """输出为 scorer.py 兼容的 result 字典"""
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "change_pct": self.change_pct,
            "volume": self.volume,
            "total_score": round(self.total_score, 1),
            "base_score": round(self.base_score, 1),
            "zone_score": round(self.zone_score, 1),
            "zone_label": self.zone_label,
            "zone_class": self.zone_class,
            "momentum_score": round(self.momentum_score, 1),
            "volume_score": round(self.volume_score, 1),
            "serenity_score": round(self.serenity_score, 1),
            "factor_score": round(self.factor_score, 1),
            "technical_score": round(self.technical_score, 1),
            "sentiment_score": round(self.sentiment_score, 1),
            "moat_score": round(self.moat_score, 1),
            "multi_cycle_factor": round(self.multi_cycle_score, 1),
            "cycle_factors": self.multi_cycle_detail,
            "signal_action": self.signal_action,
            "signal_confidence": round(self.signal_confidence, 2),
            "tech_ma5": self.tech.get("ma5", 0),
            "tech_ma20": self.tech.get("ma20", 0),
            "tech_rsi": self.tech.get("rsi", 50),
            "tech_bb_pos": self.tech.get("bb_position", 50),
            "factor_signals": self.factor_detail.get("signals", {}),
            "details": {
                "price": self.price,
                "change_pct": self.change_pct,
                "volume": self.volume,
                "zone_label": self.zone_label,
                "signal_action": self.signal_action,
                "signal_confidence": round(self.signal_confidence, 2),
            },
        }
    
    def to_score_dict(self) -> dict:
        """输出为 save_score_history 兼容的 scores dict"""
        today = __import__("datetime").date.today().isoformat()
        return {
            "date": today,
            "total_score": round(self.total_score, 1),
            "base_score": round(self.base_score, 1),
            "zone_score": round(self.zone_score, 1),
            "momentum_score": round(self.momentum_score, 1),
            "volume_score": round(self.volume_score, 1),
            "serenity_score": round(self.serenity_score, 1),
            "factor_score": round(self.factor_score, 1),
            "technical_score": round(self.technical_score, 1),
            "sentiment_score": round(self.sentiment_score, 1),
            "moat_score": round(self.moat_score, 1),
            "multi_cycle_factor": round(self.multi_cycle_score, 1),
            "cycle_factors": self.multi_cycle_detail,
            "details": str(self.to_result().get("details", {})),
        }


@dataclass
class BatchState:
    """批量评分运行时状态（多只标的一起）"""
    stocks: dict[str, SharedState] = field(default_factory=dict)
    market_regime: str = "中性"
    
    def add(self, code: str, name: str = "") -> SharedState:
        st = SharedState(code=code, name=name)
        self.stocks[code] = st
        return st
    
    def get(self, code: str) -> Optional[SharedState]:
        return self.stocks.get(code)
    
    def all_results(self) -> list[dict]:
        return [s.to_result() for s in self.stocks.values()]
    
    def ranked(self) -> list[SharedState]:
        return sorted(self.stocks.values(), key=lambda s: s.total_score, reverse=True)
