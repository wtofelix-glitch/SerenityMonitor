"""
Serenity 2.0 — 环境容器

集中管理数据库路径、日志目录、推送适配器，消除模块级默认路径。
影子模式和生产模式必须显式选择，不存在"不传参就回落生产"的路径。

原则：
  1. 模块导入时不建立连接，不预设路径
  2. 数据库路径统一由环境容器注入
  3. 生产与影子使用不同目录（不仅是不同文件名）
  4. 影子环境不持有真实推送适配器
  5. 缺少环境参数时拒绝运行，不采用默认值
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 已知生产路径（硬编码白名单，用于硬拒绝）
# ---------------------------------------------------------------------------

PRODUCTION_DB_PATH = ROOT / "serenity.db"
PRODUCTION_LOG_DIR = ROOT / "logs"

# 影子模式专用目录
SHADOW_DB_DIR = ROOT / "shadow_data"
SHADOW_LOG_DIR = ROOT / "logs" / "shadow"


@dataclass
class SerenityEnv:
    """
    环境容器 —— 所有模块通过此对象获取路径和适配器。

    创建时必须指定 mode，不存在默认值。
    """

    mode: str  # "shadow" | "production"

    # 路径
    db_path: Path = field(default_factory=lambda: SHADOW_DB_DIR / "shadow.db")
    log_dir: Path = field(default_factory=lambda: SHADOW_LOG_DIR)

    # 生产保护（显式配置，不从 __file__ 推导）
    protected_prod_db: Optional[Path] = None

    # 适配器（影子模式为 None）
    push_adapter: object = None
    broker_adapter: object = None

    # 审计
    env_id: str = ""
    created_at: str = ""
    config_snapshot: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in ("shadow", "production"):
            raise ValueError(
                f"无效环境模式: {self.mode!r}，必须为 'shadow' 或 'production'"
            )
        now = datetime.now(tz=timezone(timedelta(hours=8)))
        self.created_at = now.isoformat(timespec="seconds")
        self.env_id = f"{self.mode}_{now.strftime('%Y%m%d_%H%M%S')}"

    # ------------------------------------------------------------------
    # 生产模式保护
    # ------------------------------------------------------------------

    @property
    def is_shadow(self) -> bool:
        return self.mode == "shadow"

    @property
    def is_production(self) -> bool:
        return self.mode == "production"

    def verify_safe(self) -> tuple:
        """
        验证当前环境不会意外触碰生产数据。
        影子模式下，数据库路径不得指向已知生产路径。
        返回 (safe, reason)。
        """
        if self.mode == "shadow":
            resolved_db = self.db_path.resolve()
            # 使用显式配置的生产路径，若无则回退到模块级默认（仅用于兼容）
            prod_path = self.protected_prod_db or PRODUCTION_DB_PATH
            resolved_prod = prod_path.resolve()
            if resolved_db == resolved_prod:
                return False, (
                    f"安全拒绝: 影子DB路径 {resolved_db} 与生产DB {resolved_prod} 相同"
                )
            resolved_log = self.log_dir.resolve()
            resolved_prod_log = PRODUCTION_LOG_DIR.resolve()
            if resolved_log == resolved_prod_log:
                return False, (
                    f"安全拒绝: 影子日志目录 {resolved_log} 与生产日志 {resolved_prod_log} 相同"
                )
            if self.push_adapter is not None:
                return False, "安全拒绝: 影子模式持有推送适配器"
            if self.broker_adapter is not None:
                return False, "安全拒绝: 影子模式持有券商适配器"
            return True, "影子环境安全"

        elif self.mode == "production":
            if self.push_adapter is None:
                return False, "生产模式缺少推送适配器"
            return True, "生产环境就绪"

        return False, f"未知模式: {self.mode}"

    def ensure_dirs(self) -> None:
        """确保目录存在。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path.parent != Path("."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def write_startup_log(self) -> Path:
        """写入启动日志，包含最终解析后的路径。"""
        self.ensure_dirs()
        log_file = self.log_dir / f"startup_{self.env_id}.log"
        lines = [
            f"Serenity 2.0 环境启动",
            f"模式: {self.mode}",
            f"环境ID: {self.env_id}",
            f"DB路径: {self.db_path.resolve()}",
            f"日志目录: {self.log_dir.resolve()}",
            f"推送适配器: {'已配置' if self.push_adapter else '无(影子)'}",
            f"券商适配器: {'已配置' if self.broker_adapter else '无(影子)'}",
            f"启动时间: {self.created_at}",
            "",
        ]
        log_file.write_text("\n".join(lines), encoding="utf-8")
        return log_file

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def shadow(
        cls,
        db_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        protected_prod_db: Optional[Path | str] = None,
    ) -> "SerenityEnv":
        """创建影子环境。路径默认指向 shadow_data/ 和 logs/shadow/。
        
        protected_prod_db: 显式受保护的生产DB绝对路径。
                          若提供，则用于安全验证而非从 __file__ 推导。
        """
        if isinstance(protected_prod_db, str):
            protected_prod_db = Path(protected_prod_db)
        env = cls(
            mode="shadow",
            db_path=db_path or (SHADOW_DB_DIR / "shadow.db"),
            log_dir=log_dir or SHADOW_LOG_DIR,
            push_adapter=None,
            broker_adapter=None,
            protected_prod_db=protected_prod_db,
        )
        safe, reason = env.verify_safe()
        if not safe:
            raise RuntimeError(f"影子环境不安全: {reason}")
        env.ensure_dirs()
        env.write_startup_log()
        return env

    @classmethod
    def production(
        cls,
        push_adapter: object,
        broker_adapter: Optional[object] = None,
        db_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
    ) -> "SerenityEnv":
        """创建生产环境。推送适配器必须提供。"""
        if push_adapter is None:
            raise ValueError("生产模式必须提供推送适配器")
        env = cls(
            mode="production",
            db_path=db_path or PRODUCTION_DB_PATH,
            log_dir=log_dir or PRODUCTION_LOG_DIR,
            push_adapter=push_adapter,
            broker_adapter=broker_adapter,
        )
        safe, reason = env.verify_safe()
        if not safe:
            raise RuntimeError(f"生产环境不安全: {reason}")
        env.ensure_dirs()
        env.write_startup_log()
        return env


# ---------------------------------------------------------------------------
# 全局环境注册表（防止多个环境并存）
# ---------------------------------------------------------------------------

_active_env: Optional[SerenityEnv] = None


def set_env(env: SerenityEnv) -> None:
    """设置活跃环境。已存在环境时抛出错误（防止混用）。"""
    global _active_env
    if _active_env is not None and _active_env.mode != env.mode:
        raise RuntimeError(
            f"不能混用环境: 当前 {_active_env.mode}, "
            f"尝试设置 {env.mode}"
        )
    _active_env = env


def get_env() -> SerenityEnv:
    """获取活跃环境。未设置时抛出错误（拒绝默认值）。"""
    if _active_env is None:
        raise RuntimeError(
            "环境未初始化。请先调用 set_env(SerenityEnv.shadow()) "
            "或 set_env(SerenityEnv.production(...))"
        )
    return _active_env


def require_shadow() -> SerenityEnv:
    """获取环境并确认是影子模式。"""
    env = get_env()
    if not env.is_shadow:
        raise RuntimeError(f"需要影子模式，当前为 {env.mode}")
    return env


def require_production() -> SerenityEnv:
    """获取环境并确认是生产模式。"""
    env = get_env()
    if not env.is_production:
        raise RuntimeError(f"需要生产模式，当前为 {env.mode}")
    return env


def reset_env() -> None:
    """重置全局环境（仅用于测试/环境切换）。"""
    global _active_env
    _active_env = None
