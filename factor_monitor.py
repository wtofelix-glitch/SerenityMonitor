"""
盘中因子信号监控 — 检测因子信号偏移并推送微信预警

Usage:
    from factor_monitor import check_factor_changes
    alerts = check_factor_changes()
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional

from factor_engine import get_current_signals
from config import STOCK_MAP

logger = logging.getLogger("Serenity.FactorMonitor")

# 因子中文名映射（用于消息展示）
FACTOR_CN = {
    "ksft": "K线形态", "rank_20": "Rank排名", "rsv_20": "RSV",
    "beta_20": "Beta", "resi_20": "残差", "macd_signal": "MACD",
    "obv_trend": "OBV", "mfi_signal": "MFI", "cci_signal": "CCI",
    "wq_alpha1": "Alpha#1日内", "wq_alpha3": "Alpha#3均价",
    "wq_alpha5": "Alpha#5价偏", "wq_alpha15": "Alpha#15波幅",
    "wq_alpha19": "Alpha#19动量",
}

CACHE_PATH = os.path.expanduser("~/.serenity_factor_cache.json")
THRESHOLD = 0.15  # 信号偏移阈值


def _load_cache() -> dict:
    """从缓存文件加载上一轮因子快照。文件不存在时返回空 dict。"""
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"因子缓存文件损坏，重置: {e}")
        return {}


def _save_cache(data: dict) -> None:
    """保存当前因子快照到缓存文件。"""
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"保存因子缓存失败: {e}")


def get_factor_alert(
    stock_name: str,
    stock_code: str,
    factor_name: str,
    old_val: float,
    new_val: float,
    delta: float,
) -> str:
    """
    格式化单条因子预警消息。

    Parameters
    ----------
    stock_name  : 标的名称
    stock_code  : 标的代码
    factor_name : 因子英文名
    old_val     : 旧值
    new_val     : 新值
    delta       : 变化量

    Returns
    -------
    str — 格式化消息行
    """
    cn_name = FACTOR_CN.get(factor_name, factor_name)
    direction = "🟢" if delta > 0 else "🔴"
    return (
        f"{direction} {stock_name}({stock_code}) "
        f"{cn_name}: {old_val:+.3f} → {new_val:+.3f} "
        f"({delta:+.3f})"
    )


def check_factor_changes() -> list[str]:
    """
    检测因子信号偏移，推送微信预警。

    流程:
        1. 获取当前所有标的因子信号 (factor_engine.get_current_signals())
        2. 从 ~/.serenity_factor_cache.json 加载上一轮快照
        3. 对比每个因子值，变化 > THRESHOLD 的标的记为预警
        4. 保存新快照
        5. 如有预警，通过 notifier 推送

    Returns
    -------
    list[str] — 预警消息列表（无预警时返回空列表）
    """
    alerts = []

    # 1. 获取当前因子信号
    try:
        current_signals = get_current_signals()
    except Exception as e:
        logger.error(f"获取因子信号失败: {e}")
        return alerts

    if not current_signals:
        logger.info("无因子信号数据，跳过盘中监控")
        return alerts

    # 2. 构建当前快照: { code: { factor_name: value, ... }, ... }
    current_snapshot: dict[str, dict[str, float]] = {}
    for item in current_signals:
        code = item["code"]
        signals = item["factors"].get("signals", {})
        current_snapshot[code] = dict(signals)

    # 3. 加载旧快照
    old_snapshot = _load_cache()

    # 4. 对比检测
    if old_snapshot:
        for code, factors in current_snapshot.items():
            old_factors = old_snapshot.get(code, {})
            if not old_factors:
                continue  # 新标的，跳过（首次出现不算偏移）

            name = STOCK_MAP.get(code, {}).get("name", code)
            for fname, new_val in factors.items():
                old_val = old_factors.get(fname)
                if old_val is None:
                    continue

                delta = new_val - old_val
                if abs(delta) > THRESHOLD:
                    alert_line = get_factor_alert(name, code, fname, old_val, new_val, delta)
                    alerts.append(alert_line)
                    logger.info(f"因子偏移: {alert_line}")

    # 5. 保存新快照
    _save_cache(current_snapshot)

    # 6. 推送微信
    if alerts:
        try:
            from notifier import send_message
            today = datetime.now().strftime("%m-%d %H:%M")
            title = f"📡 因子信号偏移预警 | {today}"
            content_lines = [
                f"# 📡 盘中因子信号偏移 ({today})",
                "",
                f"共 {len(alerts)} 条预警（阈值 ±{THRESHOLD}）",
                "",
            ]
            content_lines.extend(alerts)
            content_lines.extend([
                "",
                "---",
                "> SerenityMonitor 自动推送",
            ])
            content = "\n".join(content_lines)
            send_message(
                title,
                content,
                content_type="markdown",
                summary=f"因子偏移 {len(alerts)} 条",
            )
        except Exception as e:
            logger.error(f"因子偏移推送失败: {e}")

    return alerts


if __name__ == "__main__":
    alerts = check_factor_changes()
    if alerts:
        print(f"\n⚠️ 发现 {len(alerts)} 条因子偏移预警:")
        for a in alerts:
            print(f"  {a}")
    else:
        print("✅ 无因子偏移（或首次运行，已保存快照）")
