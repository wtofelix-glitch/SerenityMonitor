"""
风险管理器 — 硬止损、连续亏损冷却、行业集中度、黑名单、熔断保护
集成已有 RISK_CONFIG 参数，统一风险检查入口。
"""
import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

from config import RISK_CONFIG, STOCK_MAP, CAPITAL_CONFIG
from serenity_logger import get_logger

log = get_logger(__name__)

# ── 行业映射（用于行业集中度检查） ─────────────────────
SECTOR_MAP: dict[str, str] = {
    "002281": "光通信",
    "000988": "光通信",
    "603083": "光通信",
    "600487": "光通信",
    "600141": "化工",
    "002428": "材料",
    "600460": "半导体",
    "603986": "半导体",
    "600176": "材料",
    "600036": "银行",
    "600585": "建材",
    "600900": "电力",
    "601398": "银行",
    "601006": "交通运输",
    "000938": "ICT设备",
}

# ── 状态文件 ──────────────────────────────────────────
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".risk_state")
os.makedirs(STATE_DIR, exist_ok=True)

CONSECUTIVE_LOSS_FILE = os.path.join(STATE_DIR, "consecutive_losses.json")
DAILY_LOSS_FILE = os.path.join(STATE_DIR, "daily_loss.json")
BLACKLIST_FILE = os.path.join(STATE_DIR, "blacklist.json")
COOLDOWN_FILE = os.path.join(STATE_DIR, "cooldown.json")


# ============================================================
# 工具函数 — JSON 持久化
# ============================================================

def _load_json(path: str, default=None):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default if default is not None else {}


def _save_json(path: str, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("无法写入风险状态文件 %s: %s", path, e)


# ============================================================
# 风险管理器
# ============================================================

class RiskManager:
    """统一风险检查入口 — 检查所有风控条件后返回是否允许交易"""

    def __init__(self, state_dir: str = None):
        self._cfg = RISK_CONFIG
        self._cap = CAPITAL_CONFIG
        self._today = date.today().isoformat()
        self._dt_today = date.today()

        # 允许测试注入临时状态目录
        if state_dir:
            self._state_dir = state_dir
        else:
            self._state_dir = STATE_DIR

        # 加载持久化状态（使用实例级路径）
        self._consecutive_loss_file = os.path.join(self._state_dir, "consecutive_losses.json")
        self._blacklist_file = os.path.join(self._state_dir, "blacklist.json")
        self._cooldown_file = os.path.join(self._state_dir, "cooldown.json")
        self._daily_loss_file = os.path.join(self._state_dir, "daily_loss.json")

        self._consecutive_losses: int = _load_json(self._consecutive_loss_file, {"count": 0, "date": ""}).get("count", 0)
        self._last_loss_date: str = _load_json(self._consecutive_loss_file, {"date": ""}).get("date", "")
        self._last_loss_code: str = ""  # 去重：当天已记录的标的
        self._blacklist: dict[str, str] = _load_json(self._blacklist_file, {})  # code -> expiry_date
        self._cooldown_until: str = _load_json(self._cooldown_file, {"until": ""}).get("until", "")
        self._daily_loss_start: str = _load_json(self._daily_loss_file, {"date": "", "loss": 0.0}).get("date", "")
        self._daily_loss_amount: float = _load_json(self._daily_loss_file, {"loss": 0.0}).get("loss", 0.0)

        # 重置日亏损（新的一天）
        if self._daily_loss_start != self._today:
            self._daily_loss_amount = 0.0
            self._daily_loss_start = self._today
            _save_json(self._daily_loss_file, {"date": self._today, "loss": 0.0})

        # 开盘市值占位（由 check_daily_loss_limit 首次调用时设置）
        self._daily_open_value: float = 0.0

    # ── 核心检查方法 ───────────────────────────────────

    def is_market_open(self) -> bool:
        """检查当前是否为交易时间（9:30-11:30, 13:00-15:00）"""
        now = datetime.now()
        # 非交易日
        if now.weekday() >= 5:
            return False
        hour, minute = now.hour, now.minute
        time_val = hour * 60 + minute
        morning_start = 9 * 60 + 30   # 9:30
        morning_end = 11 * 60 + 30    # 11:30
        afternoon_start = 13 * 60     # 13:00
        afternoon_end = 15 * 60       # 15:00
        return (morning_start <= time_val <= morning_end) or \
               (afternoon_start <= time_val <= afternoon_end)

    def check_hard_stop(self, code: str, buy_price: float, current_price: float) -> Optional[dict]:
        """
        硬止损检查
        如果当前价 <= 买入价 * (1 + stop_loss_pct)，触发止损

        Returns
        -------
        dict or None
            {"triggered": True, "reason": ..., "loss_pct": ...} or None
        """
        if buy_price <= 0 or current_price <= 0:
            return None
        max_loss_pct = self._cfg.get("max_single_loss_pct", -0.06)
        loss_pct = (current_price - buy_price) / buy_price
        if loss_pct <= max_loss_pct:
            return {
                "triggered": True,
                "reason": f"硬止损触发: {loss_pct*100:.1f}% ≤ {max_loss_pct*100:.0f}%",
                "loss_pct": round(loss_pct * 100, 2),
            }
        return None

    def check_trailing_stop(self, code: str, buy_price: float, current_price: float,
                           peak_price: float) -> Optional[dict]:
        """
        移动止损检查
        从持仓期间最高点回撤 trailing_stop_pct 时触发

        Returns
        -------
        dict or None
        """
        if peak_price <= 0 or current_price <= 0 or buy_price <= 0:
            return None
        trailing_stop = self._cfg.get("trailing_stop_pct", 0.08)
        drawdown = (current_price - peak_price) / peak_price
        if drawdown <= -trailing_stop:
            profit_pct = (current_price - buy_price) / buy_price * 100
            return {
                "triggered": True,
                "reason": f"移动止损: 从高点回撤{abs(drawdown)*100:.1f}% > {trailing_stop*100:.0f}%",
                "drawdown_pct": round(drawdown * 100, 2),
                "profit_pct": round(profit_pct, 2),
            }
        return None

    def check_daily_loss_limit(self, current_total_value: float, initial_capital: float) -> Optional[dict]:
        """
        单日亏损限额检查
        如果日内亏损超过 max_daily_loss_pct，禁止新开仓

        Returns
        -------
        dict or None
        """
        max_daily_loss = self._cfg.get("max_daily_loss_pct", -0.04)
        # 首次记录今日起始价值
        if self._daily_open_value <= 0:
            self._daily_open_value = current_total_value
            self._daily_loss_start = self._today

        daily_loss_pct = (current_total_value - self._daily_open_value) / self._daily_open_value if self._daily_open_value > 0 else 0
        if daily_loss_pct <= max_daily_loss:
            return {
                "triggered": True,
                "reason": f"日亏损限额触发: {daily_loss_pct*100:.2f}% ≤ {max_daily_loss*100:.0f}%",
                "daily_loss_pct": round(daily_loss_pct * 100, 2),
            }
        return None

    def check_max_drawdown(self, current_total_value: float, initial_capital: float) -> Optional[dict]:
        """
        最大回撤检查
        如果总资金回撤超过 max_portfolio_drawdown，触发熔断

        Returns
        -------
        dict or None
        """
        max_dd = self._cfg.get("max_portfolio_drawdown", -0.12)
        drawdown = (current_total_value - initial_capital) / initial_capital if initial_capital > 0 else 0
        if drawdown <= max_dd:
            return {
                "triggered": True,
                "reason": f"最大回撤熔断: {drawdown*100:.1f}% ≤ {max_dd*100:.0f}% — 强制清仓",
                "drawdown_pct": round(drawdown * 100, 2),
            }
        return None

    def check_consecutive_losses(self) -> Optional[dict]:
        """
        连续亏损检查
        连续亏损 max_consecutive_losses 笔 → 冷却
        """
        max_losses = self._cfg.get("max_consecutive_losses", 2)
        if self._consecutive_losses >= max_losses:
            return {
                "triggered": True,
                "reason": f"连续{self._consecutive_losses}笔亏损 ≥ {max_losses}上限，触发冷却",
                "loss_count": self._consecutive_losses,
                "max_allowed": max_losses,
            }
        return None

    def check_cooldown(self) -> Optional[dict]:
        """
        冷却期检查
        如果在冷却期内，禁止开新仓
        """
        if not self._cooldown_until:
            return None
        if self._dt_today.isoformat() < self._cooldown_until:
            remaining = (date.fromisoformat(self._cooldown_until) - self._dt_today).days
            return {
                "triggered": True,
                "reason": f"冷却中，还剩 {remaining} 天（至 {self._cooldown_until}）",
                "cooldown_until": self._cooldown_until,
                "remaining_days": remaining,
            }
        # 冷却期已过，清除
        self._clear_cooldown()
        return None

    def check_blacklist(self, code: str) -> Optional[dict]:
        """
        黑名单检查
        止损过的标的在冷却期内不能买入
        """
        if code not in self._blacklist:
            return None
        expiry = self._blacklist[code]
        if self._dt_today.isoformat() < expiry:
            remaining = (date.fromisoformat(expiry) - self._dt_today).days
            return {
                "triggered": True,
                "reason": f"{code} 在黑名单中（止损冷却至 {expiry}，还剩 {remaining} 天）",
                "expiry": expiry,
                "remaining_days": remaining,
            }
        # 过期清除
        del self._blacklist[code]
        _save_json(self._blacklist_file, self._blacklist)
        return None

    def check_sector_concentration(self, code: str, holdings: list[dict]) -> Optional[dict]:
        """
        行业集中度检查
        同一行业最多允许 2 只持仓
        """
        target_sector = SECTOR_MAP.get(code)
        if not target_sector:
            return None
        sector_count = 0
        for h in holdings:
            h_code = h["code"] if isinstance(h, dict) else h
            h_sector = SECTOR_MAP.get(h_code)
            if h_sector == target_sector:
                sector_count += 1
        if sector_count >= 2:
            return {
                "triggered": True,
                "reason": f"行业集中度超限: {target_sector} 已有 {sector_count} 只持仓，最多 2 只",
                "sector": target_sector,
                "current_count": sector_count,
                "max_allowed": 2,
            }
        return None

    def check_position_limits(self, holdings: list[dict], new_amount: float,
                              total_value: float) -> list[dict]:
        """
        仓位限制检查
        - 持仓数量上限
        - 单只最大权重
        - 单只最小权重
        """
        alerts = []
        max_pos = self._cap.get("max_positions", 2)
        max_weight = self._cap.get("max_single_weight", 0.60)
        # 🆕 使用 effective_config 的 min_single_weight（支持 aggressive_mode 覆盖）
        try:
            from config import get_effective_config
            _eff2 = get_effective_config()
            min_weight = _eff2["capital"].get("min_single_weight", 0.10)
        except Exception:
            min_weight = self._cap.get("min_single_weight", 0.30)

        if len(holdings) >= max_pos:
            alerts.append({
                "triggered": True,
                "reason": f"已达最大持仓数 {max_pos}",
            })

        if total_value > 0:
            new_weight = new_amount / total_value
            if new_weight > max_weight:
                alerts.append({
                    "triggered": True,
                    "reason": f"单只权重 {new_weight*100:.1f}% > {max_weight*100:.0f}% 上限",
                    "weight": round(new_weight * 100, 1),
                    "max": max_weight * 100,
                })
            if new_amount > 0 and new_weight < min_weight and new_weight > 0:
                # 金额太小→警告但不阻止
                alerts.append({
                    "triggered": False,
                    "warning": True,
                    "reason": f"单只权重 {new_weight*100:.1f}% < {min_weight*100:.0f}% 建议下限",
                    "weight": round(new_weight * 100, 1),
                    "min": min_weight * 100,
                })

        return alerts

    # ── 统一检查入口 ───────────────────────────────────

    def is_trade_allowed(self, code: str, action: str,
                         holdings: Optional[list[dict]] = None,
                         current_total_value: float = 0,
                         initial_capital: float = 0,
                         new_amount: float = 0) -> dict:
        """
        统一交易许可检查 — 买入前调用

        Parameters
        ----------
        code : str
            标的代码
        action : str
            "BUY"（买入）或 "SELL"（卖出，只检查最大回撤）
        holdings : list[dict], optional
            当前持仓列表
        current_total_value : float
            当前总权益
        initial_capital : float
            初始资金
        new_amount : float
            拟买入金额（用于仓位限制检查）

        Returns
        -------
        dict
            {"allowed": bool, "reasons": [str, ...], "risk_level": str}
        """
        reasons = []

        # 熔断检查
        if current_total_value > 0 and initial_capital > 0:
            dd_check = self.check_max_drawdown(current_total_value, initial_capital)
            if dd_check:
                reasons.append(dd_check["reason"])

            daily_check = self.check_daily_loss_limit(current_total_value, initial_capital)
            if daily_check:
                reasons.append(daily_check["reason"])

        # 冷却检查（对所有操作）
        cooldown_check = self.check_cooldown()
        if cooldown_check:
            reasons.append(cooldown_check["reason"])

        # 连续亏损检查
        loss_check = self.check_consecutive_losses()
        if loss_check:
            reasons.append(loss_check["reason"])

        # 买入特有检查
        if action == "BUY":
            # 黑名单
            bl_check = self.check_blacklist(code)
            if bl_check:
                reasons.append(bl_check["reason"])

            # 行业集中度
            if holdings:
                sector_check = self.check_sector_concentration(code, holdings)
                if sector_check:
                    reasons.append(sector_check["reason"])

            # 仓位限制
            if holdings is not None and current_total_value > 0:
                pos_alerts = self.check_position_limits(holdings, new_amount, current_total_value)
                for a in pos_alerts:
                    if a.get("triggered"):
                        reasons.append(a["reason"])

        risk_level = "critical" if reasons else "low"

        return {
            "allowed": len(reasons) == 0,
            "reasons": reasons,
            "risk_level": risk_level,
        }

    # ── 状态记录方法 ───────────────────────────────────

    def record_loss(self, code: str, profit_pct: float):
        """
        记录一笔亏损交易
        更新连续亏损计数，达到上限时启动冷却
        """
        if profit_pct >= 0:
            return

        # 冷却已启动 → 不再重复计数和日志
        if self._cooldown_until and self._dt_today.isoformat() < self._cooldown_until:
            return

        # 当天已记录过该标的亏损 → 去重
        if self._last_loss_date == self._today and self._last_loss_code == code:
            return

        self._consecutive_losses += 1
        self._last_loss_date = self._today
        self._last_loss_code = code
        _save_json(self._consecutive_loss_file, {
            "count": self._consecutive_losses,
            "date": self._today,
        })

        max_losses = self._cfg.get("max_consecutive_losses", 2)
        log.warning("连续亏损: %d/%d（%s亏损%.1f%%）",
                     self._consecutive_losses, max_losses, code, profit_pct)

        if self._consecutive_losses >= max_losses:
            self._activate_cooldown()
            log.warning("达到连续亏损上限，启动冷却至 %s", self._cooldown_until)

    def record_stop_loss(self, code: str):
        """
        记录止损事件 → 加入黑名单（冷却期内不可买入）
        """
        cool_days = self._cfg.get("cool_down_days", 3)
        expiry = (self._dt_today + timedelta(days=cool_days)).isoformat()
        self._blacklist[code] = expiry
        _save_json(self._blacklist_file, self._blacklist)
        log.info("止损黑名单: %s 加入至 %s", code, expiry)

    def _activate_cooldown(self):
        """启动冷却期"""
        cool_days = self._cfg.get("cool_down_days", 3)
        self._cooldown_until = (self._dt_today + timedelta(days=cool_days)).isoformat()
        _save_json(self._cooldown_file, {"until": self._cooldown_until})

    def _clear_cooldown(self):
        """清除冷却期"""
        self._cooldown_until = ""
        _save_json(self._cooldown_file, {"until": ""})

    def reset_consecutive_losses(self):
        """手工重置连续亏损计数（例如成功止盈后）"""
        self._consecutive_losses = 0
        self._last_loss_date = ""
        _save_json(self._consecutive_loss_file, {"count": 0, "date": ""})
        log.info("连续亏损计数已重置")

    # ── 报告与状态查询 ─────────────────────────────────

    def get_risk_report(self) -> dict:
        """返回完整风控状态报告"""
        return {
            "consecutive_losses": self._consecutive_losses,
            "max_consecutive_losses": self._cfg.get("max_consecutive_losses", 2),
            "in_cooldown": bool(self._cooldown_until) and self._dt_today.isoformat() < self._cooldown_until,
            "cooldown_until": self._cooldown_until,
            "blacklist": dict(self._blacklist),
            "blacklist_count": len(self._blacklist),
            "daily_loss": round(self._daily_loss_amount, 2),
            "daily_loss_limit_pct": self._cfg.get("max_daily_loss_pct", -0.04),
            "max_drawdown_pct": self._cfg.get("max_portfolio_drawdown", -0.12) * 100,
            "hard_stop_pct": self._cfg.get("max_single_loss_pct", -0.06) * 100,
            "trailing_stop_pct": self._cfg.get("trailing_stop_pct", 0.08) * 100,
        }

    def format_risk_report(self) -> str:
        """格式化风控状态仪表盘"""
        r = self.get_risk_report()
        lines = []
        lines.append("=" * 55)
        lines.append("  🛡️ Serenity 风控状态 | %s" % self._today)
        lines.append("=" * 55)

        # 连续亏损
        lines.append(f"\n📊 连续亏损: {r['consecutive_losses']}/{r['max_consecutive_losses']}")
        if r['in_cooldown']:
            lines.append(f"  🔴 冷却中至 {r['cooldown_until']}")
        else:
            lines.append(f"  🟢 正常")

        # 黑名单
        lines.append(f"\n📋 黑名单: {r['blacklist_count']} 只")
        for code, expiry in sorted(r['blacklist'].items()):
            name = STOCK_MAP.get(code, {}).get("name", code)
            lines.append(f"  ⛔ {name}({code}) 至 {expiry}")

        # 阈值
        lines.append(f"\n⚙️ 风控阈值:")
        lines.append(f"  硬止损: {r['hard_stop_pct']:.0f}%")
        lines.append(f"  移动止损: {r['trailing_stop_pct']:.0f}% 回撤")
        lines.append(f"  日亏损限额: {r['daily_loss_limit_pct']*100:.0f}%")
        lines.append(f"  最大回撤: {r['max_drawdown_pct']:.0f}%")

        lines.append("\n" + "=" * 55)
        return "\n".join(lines)


# 全局单例
_risk_instance: Optional[RiskManager] = None


def get_risk_manager() -> RiskManager:
    global _risk_instance
    if _risk_instance is None:
        _risk_instance = RiskManager()
    return _risk_instance


if __name__ == "__main__":
    rm = get_risk_manager()
    print(rm.format_risk_report())
    print()

    # 模拟检查
    result = rm.is_trade_allowed(
        code="002281",
        action="BUY",
        holdings=[{"code": "000988"}],
        current_total_value=60000,
        initial_capital=51066.41,
        new_amount=30000,
    )
    print(f"交易许可: {'✅ 允许' if result['allowed'] else '❌ 拒绝'}")
    for reason in result["reasons"]:
        print(f"  • {reason}")
