#!/usr/bin/env python3
"""
智能导入模块 — 支持三类方式将交易数据导入 serenity.db

使用方式:
    python smart_import.py --dry-run --text "紫光27.5买入300股"
    python smart_import.py --excel trades.xlsx
    python smart_import.py --image screenshot.png

类 SmartImporter 提供:
    - from_text:   自然语言文本解析
    - from_image:  截图识别（占位）
    - from_excel:  Excel 批量导入
    - resolve_code: 股票名称/拼音/别名 → 标准代码
    - apply_trades: 写入 trades + 更新 stocks
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

# ── 项目内依赖（同目录） ──────────────────────────────────────────
try:
    from db import get_conn  # noqa: E402
except ImportError:
    # Fallback: 直接在 smart_import.py 内自建连接
    _DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serenity.db")

    def get_conn(db_path: str = _DEFAULT_DB) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


logger = logging.getLogger("smart_import")


# ══════════════════════════════════════════════════════════════════
# SmartImporter
# ══════════════════════════════════════════════════════════════════


class SmartImporter:
    """智能导入器：从文本/图片/Excel 三种来源解析交易，写入 serenity.db。"""

    def __init__(
        self,
        db_path: str = "serenity.db",
        alias_db: str = "stock_aliases.db",
    ) -> None:
        self.db_path = db_path
        self.alias_db = alias_db

        # 确定数据库文件绝对路径（与脚本同目录或当前目录）
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isabs(db_path):
            self.db_path = os.path.join(_script_dir, db_path)
        if not os.path.isabs(alias_db):
            self.alias_db = os.path.join(_script_dir, alias_db)

    # ── resolve_code ──────────────────────────────────────────

    def resolve_code(self, query: str) -> Optional[str]:
        """股票代码/名称/拼音/别名 → 标准代码。

        匹配优先级:
            1. 精确匹配代码（6 位数字）
            2. 精确匹配全称 / 简称
            3. 拼音精确匹配（全拼 / 简拼）
            4. 别名 JSON 数组模糊匹配
            5. LIKE 模糊匹配（名称包含 query）

        Args:
            query: 用户输入，如 '紫光' '600519' 'gzmt' '贵州茅台' '茅台'

        Returns:
            标准 6 位股票代码，未匹配返回 None。
        """
        if not query or not query.strip():
            return None
        q = query.strip()

        # （1）纯数字 → 视为代码直接验证
        if q.isdigit() and len(q) == 6:
            if self._code_exists(q):
                return q
            return None

        conn = sqlite3.connect(self.alias_db)
        conn.row_factory = sqlite3.Row
        try:
            # （2）精确名称
            row = conn.execute(
                "SELECT code FROM stock_aliases WHERE name = ? AND is_active = 1",
                (q,),
            ).fetchone()
            if row:
                return row["code"]

            # （3）拼音匹配
            q_lower = q.lower()
            row = conn.execute(
                """SELECT code FROM stock_aliases
                   WHERE (pinyin = ? OR short_pinyin = ?) AND is_active = 1""",
                (q_lower, q_lower),
            ).fetchone()
            if row:
                return row["code"]

            # （4）别名数组模糊匹配
            all_rows = conn.execute(
                "SELECT code, aliases FROM stock_aliases WHERE is_active = 1 AND aliases != ''",
            ).fetchall()
            for r in all_rows:
                try:
                    alias_list: list[str] = json.loads(r["aliases"])
                    if q in alias_list:
                        return r["code"]
                except (json.JSONDecodeError, TypeError):
                    continue

            # （5）LIKE 模糊匹配（名称包含 query）
            row = conn.execute(
                "SELECT code FROM stock_aliases WHERE name LIKE ? AND is_active = 1 LIMIT 1",
                (f"%{q}%",),
            ).fetchone()
            if row:
                return row["code"]

            return None
        finally:
            conn.close()

    def _code_exists(self, code: str) -> bool:
        """检查代码是否存在于别名数据库。"""
        conn = sqlite3.connect(self.alias_db)
        try:
            row = conn.execute(
                "SELECT 1 FROM stock_aliases WHERE code = ? AND is_active = 1",
                (code,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ── from_text ─────────────────────────────────────────────

    def from_text(self, text: str) -> list[dict[str, Any]]:
        """从自然语言文本解析交易。

        支持格式:
            - '紫光27.5买入300股'
            - '海螺17.36买入300股，可用249.22'
            - '华工科技167.1出清'
            - '600519 1500 买入 200 股'
            - '贵州茅台 1500 卖出 100'

        模式说明:
            1. "名称|代码 价格 操作 数量" 格式（AI 风格）
            2. "名称价格操作数量" 格式（口语）
            3. "名称价格出清" 格式

        Args:
            text: 自然语言交易描述文本

        Returns:
            解析后的交易列表，每项 dict: {code, name, action, price, quantity, date}
        """
        if not text or not text.strip():
            return []

        trades: list[dict[str, Any]] = []
        today = date.today().isoformat()

        # ── 模式 1: "华工科技 167.1 出清" ──
        m = re.search(
            r"(?P<name>[^\d\s,，]+)\s*(?P<price>[\d.]+)\s*出清",
            text,
        )
        if m:
            name = m.group("name").strip()
            price = float(m.group("price"))
            code = self.resolve_code(name)
            if code:
                trades.append({
                    "code": code,
                    "name": name,
                    "action": "sell",
                    "price": price,
                    "quantity": 0,  # 出清时数量由 stocks 表确定
                    "date": today,
                    "note": "出清",
                })
                return trades

        # ── 模式 2: "紫光27.5买入300股" ──
        m = re.search(
            r"(?P<name>[^\d\s,，]+)"
            r"\s*(?P<price>[\d.]+)"
            r"\s*(?P<action>买入|卖出|购入|买进|全仓买入)"
            r"\s*(?P<quantity>\d+)\s*股?",
            text,
        )
        if m:
            name = m.group("name").strip()
            price = float(m.group("price"))
            action_raw = m.group("action")
            quantity = int(m.group("quantity"))

            code = self.resolve_code(name)
            if code:
                action = "buy" if "买" in action_raw else "sell"
                trades.append({
                    "code": code,
                    "name": name,
                    "action": action,
                    "price": price,
                    "quantity": quantity,
                    "date": today,
                    "note": f"智能导入: {text}",
                })

        # ── 模式 3: "600519 1500 买入 200 股" (代码在前) ──
        if not trades:
            m = re.search(
                r"(?P<code>\d{6})\s+"
                r"(?P<price>[\d.]+)\s+"
                r"(?P<action>买入|卖出|购入)\s+"
                r"(?P<quantity>\d+)\s*股?",
                text,
            )
            if m:
                code = m.group("code")
                price = float(m.group("price"))
                action_raw = m.group("action")
                quantity = int(m.group("quantity"))

                action = "buy" if "买" in action_raw else "sell"
                name = self._get_name(code) or code
                trades.append({
                    "code": code,
                    "name": name,
                    "action": action,
                    "price": price,
                    "quantity": quantity,
                    "date": today,
                    "note": f"智能导入: {text}",
                })

        # ── 模式 4: "名称 价格 操作 数量" (空格分隔) ──
        if not trades:
            # 匹配: 非数字开头的名称 + 价格(数字) + 操作(中文) + 数量(数字)
            m = re.search(
                r"(?P<name>[^\d\s].+?)\s+"
                r"(?P<price>[\d.]+)\s+"
                r"(?P<action>买入|卖出)\s+"
                r"(?P<quantity>\d+)",
                text,
            )
            if m:
                name = m.group("name").strip()
                price = float(m.group("price"))
                action_raw = m.group("action")
                quantity = int(m.group("quantity"))

                code = self.resolve_code(name)
                if code:
                    action = "buy" if "买" in action_raw else "sell"
                    trades.append({
                        "code": code,
                        "name": name,
                        "action": action,
                        "price": price,
                        "quantity": quantity,
                        "date": today,
                        "note": f"智能导入: {text}",
                    })

        return trades

    def _get_name(self, code: str) -> Optional[str]:
        """根据代码从别名数据库获取股票名称。"""
        conn = sqlite3.connect(self.alias_db)
        try:
            row = conn.execute(
                "SELECT name FROM stock_aliases WHERE code = ?",
                (code,),
            ).fetchone()
            return row["name"] if row else None
        finally:
            conn.close()

    # ── from_image ────────────────────────────────────────────

    def from_image(self, image_path: str) -> list[dict[str, Any]]:
        """从图片（券商 App 持仓截图）识别交易。

        当前为占位实现，在对话中需配合 vision_analyze 工具完成识别。
        直接调用本方法会打印提示信息并返回空列表。

        Args:
            image_path: 截图文件路径

        Returns:
            空列表。请在此 Agent 对话中通过 vision_analyze 工具解析后调用 apply_trades。
        """
        print(
            "⚠️  from_image: 需要 vision_analyze 工具，请在对话中调用。\n"
            f"   图片路径: {image_path}\n"
            "   建议流程:\n"
            "   1. 使用 vision_analyze 工具读取截图\n"
            "   2. 将识别出的交易文本传给 from_text() 解析\n"
            "   3. 调用 apply_trades() 写入数据库"
        )
        return []

    # ── from_excel ────────────────────────────────────────────

    def from_excel(self, excel_path: str) -> list[dict[str, Any]]:
        """从 Excel 文件导入交易。

        假设列名（不区分大小写，支持中英文）:
            - 代码 / code
            - 名称 / name
            - 操作 / action (买入/卖出/buy/sell)
            - 价格 / price
            - 数量 / quantity
            - 日期 / date

        Args:
            excel_path: Excel 文件路径 (.xlsx 或 .xls)

        Returns:
            解析后的交易列表。
        """
        if not os.path.exists(excel_path):
            raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")

        df = pd.read_excel(excel_path)

        # 列名标准化（小写 + 去空格 + 中文映射）
        col_map: dict[str, str] = {
            "code": "code",
            "代码": "code",
            "symbol": "code",
            "name": "name",
            "名称": "name",
            "stock": "name",
            "action": "action",
            "操作": "action",
            "type": "action",
            "price": "price",
            "价格": "price",
            "单价": "price",
            "quantity": "quantity",
            "数量": "quantity",
            "股数": "quantity",
            "qty": "quantity",
            "date": "date",
            "日期": "date",
            "trade_date": "date",
        }

        # 构建列映射
        actual_cols: dict[str, str] = {}
        for col in df.columns:
            col_clean = str(col).strip()
            if col_clean.lower() in col_map:
                actual_cols[col_map[col_clean.lower()]] = col
            elif col_clean in col_map:
                actual_cols[col_map[col_clean]] = col

        if "code" not in actual_cols:
            raise ValueError(
                f"Excel 文件中未找到代码列。可用列: {list(df.columns)}。"
                f"请确保包含 '代码' 或 'code' 列。"
            )

        trades: list[dict[str, Any]] = []
        today = date.today().isoformat()

        for _idx, row in df.iterrows():
            raw_code = str(row.get(actual_cols.get("code", ""), "")).strip()
            if not raw_code or raw_code.lower() in ("nan", "none", ""):
                continue

            # 解析代码
            code = self.resolve_code(raw_code)
            if not code and raw_code.isdigit() and len(raw_code) == 6:
                code = raw_code  # 纯数字直接当代码用

            if not code:
                logger.warning(f"无法解析代码: {raw_code}, 跳过该行")
                continue

            # 名称
            name = ""
            if "name" in actual_cols:
                name = str(row.get(actual_cols["name"], "")).strip()
            if not name or name.lower() in ("nan", "none"):
                name = self._get_name(code) or code

            # 操作
            action_raw = ""
            if "action" in actual_cols:
                action_raw = str(row.get(actual_cols["action"], "")).strip()

            if "买" in action_raw or action_raw.lower() == "buy":
                action = "buy"
            elif "卖" in action_raw or action_raw.lower() == "sell":
                action = "sell"
            else:
                action = "buy"  # 默认买入

            # 价格
            price: float = 0.0
            if "price" in actual_cols:
                try:
                    price = float(row[actual_cols["price"]])
                except (ValueError, TypeError):
                    price = 0.0

            # 数量
            quantity: int = 0
            if "quantity" in actual_cols:
                try:
                    quantity = int(float(row[actual_cols["quantity"]]))
                except (ValueError, TypeError):
                    quantity = 0

            # 日期
            trade_date = today
            if "date" in actual_cols:
                date_val = row[actual_cols["date"]]
                try:
                    if isinstance(date_val, (date, datetime)):
                        trade_date = date_val.strftime("%Y-%m-%d") if isinstance(date_val, datetime) else date_val.isoformat()
                    else:
                        trade_date = str(date_val).strip()[:10]
                except Exception:
                    trade_date = today

            if price <= 0 or quantity <= 0:
                logger.warning(f"跳过无效行: code={code}, price={price}, qty={quantity}")
                continue

            trades.append({
                "code": code,
                "name": name,
                "action": action,
                "price": price,
                "quantity": quantity,
                "date": trade_date,
                "note": f"Excel 导入: {os.path.basename(excel_path)}",
            })

        return trades

    # ── apply_trades ──────────────────────────────────────────

    def apply_trades(
        self,
        trades: list[dict[str, Any]],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """将交易写入 serenity.db 的 trades 表和 stocks 表。

        对每笔交易:
            1. INSERT 到 trades 表
            2. 更新 stocks 表: buy_price, buy_date, is_active
               - 买入 (buy): 设置 buy_price=buy 均价, buy_date, is_active=1
               - 卖出 (sell): 如果 quantity=0 (出清) → is_active=0

        Args:
            trades: 交易列表，每项如 from_text/from_excel 返回值
            dry_run: True 时仅打印不写入数据库

        Returns:
            {"inserted": N, "skipped": N, "errors": [...]}
        """
        result: dict[str, Any] = {"inserted": 0, "skipped": 0, "errors": []}

        if dry_run:
            print("=" * 60)
            print("  🔍 DRY RUN — 仅预览，不写入数据库")
            print("=" * 60)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            for trade in trades:
                code = trade.get("code", "")
                name = trade.get("name", code)
                action = trade.get("action", "buy")
                price = float(trade.get("price", 0))
                quantity = int(trade.get("quantity", 0))
                trade_date = str(trade.get("date", date.today().isoformat()))
                note = str(trade.get("note", ""))

                if not code or price <= 0:
                    result["skipped"] += 1
                    result["errors"].append(
                        f"跳过: 无效数据 code={code} price={price}"
                    )
                    continue

                if dry_run:
                    print(
                        f"  📝 [{action.upper():4s}] {name}({code}) "
                        f"@{price:.2f} × {quantity}股  {trade_date}"
                    )
                    result["inserted"] += 1
                else:
                    try:
                        # 1. INSERT trade
                        conn.execute(
                            """INSERT INTO trades (code, action, price, quantity, date, note)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (code, action, price, quantity, trade_date, note),
                        )

                        # 2. 更新/创建 stocks 记录
                        existing = conn.execute(
                            "SELECT code FROM stocks WHERE code = ?",
                            (code,),
                        ).fetchone()

                        if action == "buy":
                            if existing:
                                conn.execute(
                                    """UPDATE stocks
                                       SET buy_price = ?, buy_date = ?, is_active = 1
                                       WHERE code = ?""",
                                    (price, trade_date, code),
                                )
                            else:
                                # 从别名库获取 market
                                market = self._get_market(code) or "sh"
                                conn.execute(
                                    """INSERT INTO stocks
                                       (code, name, market, buy_price, buy_date, is_active)
                                       VALUES (?, ?, ?, ?, ?, 1)""",
                                    (code, name, market, price, trade_date),
                                )
                        elif action == "sell":
                            if existing:
                                conn.execute(
                                    """UPDATE stocks
                                       SET is_active = ? WHERE code = ?""",
                                    (0 if quantity == 0 else 1, code),
                                )

                        result["inserted"] += 1
                    except Exception as e:
                        result["skipped"] += 1
                        result["errors"].append(
                            f"写入失败 {code}: {e}"
                        )
                        logger.error(f"写入失败 {code}: {e}")

            if not dry_run:
                conn.commit()
                print(f"\n✅ 已写入 {result['inserted']} 条交易记录。")
        finally:
            conn.close()

        if dry_run:
            print(f"\n📊 预览: {result['inserted']} 条将写入, "
                  f"{result['skipped']} 条跳过")

        return result

    def _get_market(self, code: str) -> Optional[str]:
        """从别名数据库获取 market。"""
        conn = sqlite3.connect(self.alias_db)
        try:
            row = conn.execute(
                "SELECT market FROM stock_aliases WHERE code = ?",
                (code,),
            ).fetchone()
            return row["market"] if row else None
        finally:
            conn.close()

    # ── 批量入口 ──────────────────────────────────────────────

    def import_all(
        self,
        text: Optional[str] = None,
        image_path: Optional[str] = None,
        excel_path: Optional[str] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """统一入口：从任意来源导入交易。

        Args:
            text: 自然语言交易文本
            image_path: 截图路径
            excel_path: Excel 文件路径
            dry_run: 是否仅预览

        Returns:
            apply_trades 的结果字典。
        """
        all_trades: list[dict[str, Any]] = []

        if text:
            t = self.from_text(text)
            print(f"📝 文本解析: {len(t)} 条交易")
            all_trades.extend(t)

        if image_path:
            t = self.from_image(image_path)
            if t:
                all_trades.extend(t)

        if excel_path:
            t = self.from_excel(excel_path)
            print(f"📊 Excel 解析: {len(t)} 条交易")
            all_trades.extend(t)

        if not all_trades:
            print("⚠️  没有解析到任何交易。")
            return {"inserted": 0, "skipped": 0, "errors": []}

        return self.apply_trades(all_trades, dry_run=dry_run)


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SerenityMonitor 智能导入模块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python smart_import.py --dry-run --text "紫光27.5买入300股"
  python smart_import.py --text "紫光27.5买入300股"
  python smart_import.py --excel trades.xlsx
""",
    )
    parser.add_argument(
        "--text",
        type=str,
        default=None,
        help="自然语言交易文本，如 '紫光27.5买入300股'",
    )
    parser.add_argument(
        "--image",
        type=str,
        default=None,
        help="券商 App 持仓截图路径",
    )
    parser.add_argument(
        "--excel",
        type=str,
        default=None,
        help="Excel 交易文件路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="仅预览不写入数据库",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="确认写入数据库（与 --dry-run 互斥）",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="serenity.db",
        help="serenity.db 路径 (默认: serenity.db)",
    )
    parser.add_argument(
        "--alias-db",
        type=str,
        default="stock_aliases.db",
        help="stock_aliases.db 路径 (默认: stock_aliases.db)",
    )
    parser.add_argument(
        "--resolve",
        type=str,
        default=None,
        help="仅测试 resolve_code，如 '--resolve 紫光'",
    )

    args = parser.parse_args()

    importer = SmartImporter(
        db_path=args.db,
        alias_db=args.alias_db,
    )

    # 仅测试 resolve
    if args.resolve:
        code = importer.resolve_code(args.resolve)
        if code:
            print(f"✅ '{args.resolve}' → {code}")
        else:
            print(f"❌ 未找到 '{args.resolve}' 对应的代码")
        return

    # 导入
    if not any([args.text, args.image, args.excel]):
        parser.print_help()
        print("\n⚠️  请提供 --text / --image / --excel 参数之一。")
        return

    if args.dry_run and args.commit:
        print("❌ --dry-run 和 --commit 不能同时使用。")
        return

    dry_run = not args.commit if not args.dry_run else True

    importer.import_all(
        text=args.text,
        image_path=args.image,
        excel_path=args.excel,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    main()
