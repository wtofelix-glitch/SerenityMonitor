#!/usr/bin/env python3
"""
trading_log_sync.py — 交易日志 → gbrain 沉淀

将指定日期的信号日志 + 评分 + 持仓状态一键沉淀到 gbrain。
如果 gbrain 不可用，降级到本地 JSON 文件。

用法:
    python3 trading_log_sync.py                   # 同步今日
    python3 trading_log_sync.py 2026-06-03        # 同步指定日期
"""
import sys
import os
import json
import subprocess
import sqlite3
from datetime import date, datetime
from typing import Optional

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_conn, load_all_stocks, DB_PATH
from config import STOCK_MAP


def _check_gbrain() -> bool:
    """检查 gbrain 是否可用"""
    try:
        # 先尝试 which
        r = subprocess.run(
            ["which", "gbrain"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
        # 再尝试 import
        r = subprocess.run(
            ["python3", "-c", "import gbrain; print('ok')"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.expanduser("~/Documents/gbrain"),
        )
        return r.returncode == 0 and r.stdout.strip() == "ok"
    except Exception:
        return False


def _get_signals_by_date(date_str: str) -> list[dict]:
    """从 signal_log 获取指定日期的信号记录"""
    try:
        conn = get_conn()
        rows = conn.execute("""
            SELECT s.*, t.name FROM signal_log s
            LEFT JOIN stocks t ON s.code = t.code
            WHERE s.date=?
            ORDER BY s.total_score DESC
        """, (date_str,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"⚠️ 获取信号日志失败: {e}")
        return []


def _get_scores_by_date(date_str: str) -> list[dict]:
    """从 scoring_history 获取指定日期的评分记录"""
    try:
        conn = get_conn()
        rows = conn.execute("""
            SELECT s.*, t.name FROM scoring_history s
            LEFT JOIN stocks t ON s.code = t.code
            WHERE s.date=?
            ORDER BY s.total_score DESC
        """, (date_str,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"⚠️ 获取评分数据失败: {e}")
        return []


def _get_portfolio_state(date_str: str) -> list[dict]:
    """获取指定日期的持仓状态（基于当前持仓 + 快照价格估算）"""
    try:
        stocks = load_all_stocks()
        active = [s for s in stocks if s.get("is_active")]
        if not active:
            return []

        # 获取该日的收盘价
        codes = [s["code"] for s in active]
        placeholders = ",".join("?" for _ in codes)
        conn = get_conn()
        rows = conn.execute(f"""
            SELECT code, close, change_pct FROM daily_snapshots
            WHERE code IN ({placeholders}) AND date=?
            ORDER BY code
        """, codes + [date_str]).fetchall()
        conn.close()
        snap_map = {r["code"]: dict(r) for r in rows}

        result = []
        for s in active:
            code = s["code"]
            snap = snap_map.get(code, {})
            close = snap.get("close") or 0
            buy_price = s.get("buy_price") or close or 0
            pnl_pct = ((close - buy_price) / buy_price * 100) if buy_price > 0 else 0
            result.append({
                "code": code,
                "name": STOCK_MAP.get(code, {}).get("name", s.get("name", code)),
                "buy_price": buy_price,
                "current_price": close,
                "pnl_pct": round(pnl_pct, 2),
                "trade_amount": s.get("trade_amount", 0),
            })
        return result
    except Exception as e:
        print(f"⚠️ 获取持仓状态失败: {e}")
        return []


def _save_local_fallback(date_str: str, data: dict) -> str:
    """降级到本地 JSON 文件"""
    try:
        out_dir = os.path.expanduser("~/Documents/trading_logs")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{date_str}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return out_path
    except Exception as e:
        print(f"❌ 保存本地 JSON 失败: {e}")
        return ""


def _save_to_gbrain(date_str: str, data: dict) -> bool:
    """通过 gbrain CLI 写入数据"""
    try:
        key = f"trading_log:{date_str}"
        payload = json.dumps(data, ensure_ascii=False)
        r = subprocess.run(
            ["gbrain", "put", key, payload],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            print(f"✅ gbrain put {key} 成功")
            return True
        else:
            print(f"⚠️ gbrain put 失败: {r.stderr.strip() or r.stdout.strip()}")
            return False
    except Exception as e:
        print(f"⚠️ gbrain put 异常: {e}")
        return False


def sync_to_gbrain(date_str: Optional[str] = None) -> dict:
    """
    将指定日期的信号日志 + 评分 + 持仓状态沉淀到 gbrain（或本地 JSON）

    Args:
        date_str: 日期字符串 YYYY-MM-DD，默认今天

    Returns:
        dict: {"success": bool, "path": str, "data": dict}
    """
    if date_str is None:
        date_str = date.today().isoformat()

    print(f"🔄 同步交易日志 [{date_str}] ...")

    # 1. 获取数据
    signals = _get_signals_by_date(date_str)
    scores_raw = _get_scores_by_date(date_str)
    scores = [
        {
            "code": s.get("code", ""),
            "name": s.get("name", STOCK_MAP.get(s.get("code", ""), {}).get("name", "")),
            "total_score": s.get("total_score", 0),
            "serenity_score": s.get("serenity_score", 0),
            "technical_score": s.get("technical_score", 0),
            "factor_score": s.get("factor_score", 0),
        }
        for s in scores_raw
    ]
    portfolio = _get_portfolio_state(date_str)

    # 2. 统计摘要
    total_signals = len(signals)
    buy_signals = [s for s in signals if s.get("action", "").upper() in ("STRONG_BUY", "BUY", "CAUTION_BUY")]
    sell_signals = [s for s in signals if s.get("action", "").upper() in ("SELL", "STOP_LOSS")]
    buy_pct = round(len(buy_signals) / total_signals * 100, 1) if total_signals > 0 else 0
    sell_pct = round(len(sell_signals) / total_signals * 100, 1) if total_signals > 0 else 0

    pnl_values = [p["pnl_pct"] for p in portfolio if p.get("pnl_pct") is not None]
    avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else 0.0

    # 3. 构建数据
    data = {
        "date": date_str,
        "signals": [
            {
                "code": s.get("code", ""),
                "name": s.get("name", STOCK_MAP.get(s.get("code", ""), {}).get("name", "")),
                "action": s.get("action", ""),
                "total_score": s.get("total_score", 0),
                "tech_score": s.get("tech_score", 0),
                "serenity_score": s.get("serenity_score", 0),
                "alpha_score": s.get("alpha_score", 0),
                "fundamental_score": s.get("fundamental_score"),
                "price": s.get("price", 0),
                "is_holding": bool(s.get("is_holding", 0)),
            }
            for s in signals
        ],
        "scores": scores,
        "portfolio": portfolio,
        "summary": {
            "total_signals": total_signals,
            "buy_pct": buy_pct,
            "sell_pct": sell_pct,
            "pnl_pct": avg_pnl,
            "total_positions": len(portfolio),
        },
    }

    # 4. 写入目标
    gbrain_ok = _check_gbrain()

    if gbrain_ok:
        ok = _save_to_gbrain(date_str, data)
        if ok:
            print(f"✅ 已同步到 gbrain: trading_log:{date_str}")
            return {"success": True, "path": f"gbrain:trading_log:{date_str}", "data": data}
        else:
            print("⚠️ gbrain 写入失败，降级到本地 JSON ...")

    # 降级
    local_path = _save_local_fallback(date_str, data)
    if local_path:
        print(f"✅ 已保存到本地: {local_path}")
        return {"success": True, "path": local_path, "data": data}
    else:
        print("❌ 同步失败")
        return {"success": False, "path": "", "data": data}


def sync_today() -> dict:
    """自动 sync 今日数据"""
    return sync_to_gbrain(date.today().isoformat())


def cmd_sync_log() -> None:
    """CLI入口"""
    # sys.argv[1] 是命令名 sync-log，真正日期参数在 sys.argv[2]
    if len(sys.argv) > 2 and sys.argv[2] not in ("-h", "--help"):
        date_str = sys.argv[2]
        # 校验日期格式
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            print(f"❌ 日期格式错误: {date_str}，请使用 YYYY-MM-DD 格式")
            sys.exit(1)
    else:
        date_str = date.today().isoformat()

    result = sync_to_gbrain(date_str)
    if not result.get("success"):
        sys.exit(1)


# ============================================================
# 直接运行入口
# ============================================================
if __name__ == "__main__":
    cmd_sync_log()
