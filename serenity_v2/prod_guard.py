"""
Serenity 2.0 — 生产路径保护 (P0-1)

绝不从 __file__ / cwd / git root 推导生产DB路径。
生产路径必须显式配置；配置缺失或不合法时拒绝运行。

三态文件状态:
  PRESENT  — 文件存在，有哈希
  ABSENT   — 文件不存在
  ERROR    — 读取失败

连接级防护: 影子环境下所有 SQLite 连接必须通过统一工厂，
拒绝打开生产文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

CST = timezone(timedelta(hours=8))
logger = logging.getLogger("serenity_v2.prod_guard")

# ---------------------------------------------------------------------------
# 文件状态
# ---------------------------------------------------------------------------

FILE_STATE_PRESENT = "PRESENT"
FILE_STATE_ABSENT = "ABSENT"
FILE_STATE_ERROR = "ERROR"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class FileSnapshot:
    """单个文件的快照。"""
    path: str                         # 绝对真实路径
    state: str = FILE_STATE_ABSENT    # PRESENT / ABSENT / ERROR
    sha256: Optional[str] = None      # 64 位 hex
    size: Optional[int] = None
    mtime_ns: Optional[int] = None
    inode: Optional[int] = None
    device: Optional[int] = None
    error: str = ""                   # 读取失败原因
    collected_at: str = ""


@dataclass
class ProdFileSet:
    """受保护的生产文件集合。"""
    main: FileSnapshot = field(default_factory=lambda: FileSnapshot(path=""))
    wal: FileSnapshot = field(default_factory=lambda: FileSnapshot(path=""))
    shm: FileSnapshot = field(default_factory=lambda: FileSnapshot(path=""))

    def all_present_hashes(self) -> dict[str, Optional[str]]:
        return {
            self.main.path: self.main.sha256,
            self.wal.path: self.wal.sha256,
            self.shm.path: self.shm.sha256,
        }

    def summary(self) -> dict:
        return {
            "main": _snap_dict(self.main),
            "wal": _snap_dict(self.wal),
            "shm": _snap_dict(self.shm),
        }


def _snap_dict(s: FileSnapshot) -> dict:
    return {
        "path": s.path, "state": s.state, "sha256": s.sha256,
        "size": s.size, "mtime_ns": s.mtime_ns,
        "inode": s.inode, "device": s.device, "error": s.error,
        "collected_at": s.collected_at,
    }


# ---------------------------------------------------------------------------
# 快照工具
# ---------------------------------------------------------------------------

def snap_file(path: Path) -> FileSnapshot:
    """对单文件进行快照。"""
    now = datetime.now(tz=CST).isoformat(timespec="seconds")
    snap = FileSnapshot(path=str(path.resolve()), collected_at=now)

    if not path.exists():
        snap.state = FILE_STATE_ABSENT
        return snap

    try:
        stat = path.stat()
        snap.size = stat.st_size
        snap.mtime_ns = stat.st_mtime_ns
        snap.inode = stat.st_ino
        snap.device = stat.st_dev
        snap.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        snap.state = FILE_STATE_PRESENT
    except Exception as exc:
        snap.state = FILE_STATE_ERROR
        snap.error = str(exc)

    return snap


def snap_prod_set(main_path: Path) -> ProdFileSet:
    """快照整个生产集合（main + wal + shm）。"""
    main = main_path.resolve()
    return ProdFileSet(
        main=snap_file(main),
        wal=snap_file(main.with_suffix(".db-wal")),
        shm=snap_file(main.with_suffix(".db-shm")),
    )


def diff_snapshots(before: ProdFileSet, after: ProdFileSet) -> list[str]:
    """比较前后快照，返回变更列表。"""
    changes: list[str] = []
    for label, b, a in [
        ("main", before.main, after.main),
        ("wal", before.wal, after.wal),
        ("shm", before.shm, after.shm),
    ]:
        if b.state != a.state:
            changes.append(f"{label}: {b.state} → {a.state}")
        elif b.state == FILE_STATE_PRESENT and a.state == FILE_STATE_PRESENT:
            if b.sha256 != a.sha256:
                changes.append(f"{label}: hash changed ({b.sha256[:12]}... → {a.sha256[:12]}...)")
    return changes


# ---------------------------------------------------------------------------
# 路径验证
# ---------------------------------------------------------------------------

def validate_production_path(path_str: str) -> tuple:
    """验证生产路径配置是否合法。

    返回 (ok, resolved_path, error_message)。
    """
    if not path_str or not path_str.strip():
        return False, None, "生产路径未配置（空字符串）"

    p = Path(path_str)

    # 禁止相对路径
    if not p.is_absolute():
        return False, None, f"生产路径必须是绝对路径: {path_str}"

    # realpath 规范化
    resolved = p.resolve()

    # 必须存在
    if not resolved.exists():
        return False, resolved, f"生产DB不存在: {resolved}"

    # 必须是普通文件
    if not resolved.is_file():
        return False, resolved, f"生产DB不是普通文件: {resolved}"

    return True, resolved, ""


def validate_shadow_identity(
    shadow_path: Path, prod_path: Path,
) -> tuple:
    """验证影子DB不会指向生产DB（路径 + inode/device）。

    返回 (ok, error_message)。
    """
    prod_real = prod_path.resolve()

    # 确保 shadow 目录存在
    shadow_parent = shadow_path.parent.resolve()
    shadow_parent.mkdir(parents=True, exist_ok=True)

    # 影子 DB 已存在 → 检查文件身份
    if shadow_path.exists():
        shadow_real = shadow_path.resolve()
        if shadow_real == prod_real:
            return False, f"影子DB指向生产DB: {shadow_real}"

        # 检查 inode/device
        try:
            s_stat = shadow_real.stat()
            p_stat = prod_real.stat()
            if (s_stat.st_ino, s_stat.st_dev) == (p_stat.st_ino, p_stat.st_dev):
                return False, (
                    f"影子DB与生产DB inode/device 相同: "
                    f"ino={s_stat.st_ino} dev={s_stat.st_dev}"
                )
        except Exception:
            pass

    # 检查生产路径是否在影子目录内
    try:
        shadow_real = shadow_parent.resolve()
        if str(prod_real).startswith(str(shadow_real)):
            return False, f"生产DB位于影子目录内: {prod_real} 在 {shadow_real} 下"
    except Exception:
        pass

    return True, ""


# ---------------------------------------------------------------------------
# 连接工厂
# ---------------------------------------------------------------------------

class ShadowConnectionFactory:
    """影子模式 SQLite 连接工厂。

    白名单: 影子 DB、影子输出目录。
    拒绝: protected_prod_db / wal / shm 以及其他所有路径。
    """

    def __init__(self, prod_main_path: Path, shadow_db_path: Path):
        self._prod_main = prod_main_path.resolve()
        self._prod_wal = self._prod_main.with_suffix(".db-wal").resolve()
        self._prod_shm = self._prod_main.with_suffix(".db-shm").resolve()
        self._shadow_db = shadow_db_path.resolve()
        self._audit_log: list[dict] = []

    @property
    def audit_log(self) -> list[dict]:
        return list(self._audit_log)

    def connect(self, requested_path: Path, mode: str = "rw") -> sqlite3.Connection:
        """创建 SQLite 连接（白名单检查）。"""
        now = datetime.now(tz=CST).isoformat(timespec="seconds")
        resolved = requested_path.resolve()

        entry = {
            "requested_path": str(requested_path),
            "resolved_path": str(resolved),
            "mode": mode,
            "caller": self._caller_info(),
            "opened_at": now,
            "allowed": False,
        }

        # 检查是否被禁止
        forbidden = {self._prod_main, self._prod_wal, self._prod_shm}
        if resolved in forbidden:
            entry["rejected_reason"] = f"禁止访问生产文件: {resolved}"
            self._audit_log.append(entry)
            raise PermissionError(entry["rejected_reason"])

        # 白名单检查
        allowed = {self._shadow_db}
        if resolved not in allowed:
            # 允许影子目录下的其他文件（报告等）
            shadow_root = self._shadow_db.parent.resolve()
            try:
                if str(resolved).startswith(str(shadow_root)):
                    allowed.add(resolved)
            except Exception:
                pass
        if resolved not in allowed:
            entry["rejected_reason"] = (
                f"未声明的路径不在白名单中: {resolved}"
            )
            self._audit_log.append(entry)
            raise PermissionError(entry["rejected_reason"])

        entry["allowed"] = True
        self._audit_log.append(entry)

        conn = sqlite3.connect(str(resolved))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _caller_info() -> str:
        import traceback
        frames = traceback.extract_stack(limit=4)
        if len(frames) >= 2:
            return f"{frames[-2].filename}:{frames[-2].lineno}"
        return "unknown"


# ---------------------------------------------------------------------------
# 生产保护器（集成所有 P0-1 检查）
# ---------------------------------------------------------------------------

@dataclass
class ProdGuardConfig:
    """显式生产保护配置。"""
    protected_prod_db: str = ""                    # 绝对路径
    shadow_db_dir: str = ""                         # 影子输出目录


class ProductionGuard:
    """生产保护器 — 启动前验证、前后快照、身份检查。"""

    def __init__(self, config: ProdGuardConfig):
        self.config = config
        self.before: Optional[ProdFileSet] = None
        self.after: Optional[ProdFileSet] = None
        self._prod_main: Optional[Path] = None

    def preflight(self) -> tuple:
        """启动前所有 P0-1 检查。

        返回 (ok, details_dict, violations)。
        """
        violations: list[str] = []

        # 1. 验证路径配置
        ok, resolved, err = validate_production_path(self.config.protected_prod_db)
        if not ok:
            violations.append(err)
            return False, {}, violations

        self._prod_main = resolved

        # 2. 收集启动前快照
        self.before = snap_prod_set(self._prod_main)

        # 3. 主DB必须存在
        if self.before.main.state != FILE_STATE_PRESENT:
            violations.append(
                f"生产主DB必须存在: {self._prod_main} "
                f"(状态={self.before.main.state})"
            )
            return False, self._details(), violations

        details = self._details()
        ok = len(violations) == 0
        return ok, details, violations

    def postflight(self, shadow_db_path: Path) -> tuple:
        """运行后检查。

        返回 (ok, changes, violations)。
        """
        violations: list[str] = []
        changes: list[str] = []

        # 0. 确保 _prod_main 已初始化
        if self._prod_main is None:
            ok, resolved, err = validate_production_path(self.config.protected_prod_db)
            if not ok:
                violations.append(err)
                return False, changes, violations
            self._prod_main = resolved

        # 1. 收集运行后快照
        self.after = snap_prod_set(self._prod_main)

        if self.before is None:
            violations.append("缺少启动前快照")
            return False, changes, violations

        # 2. 差分
        changes = diff_snapshots(self.before, self.after)

        # 3. 主DB哈希必须未变
        if self.after.main.state == FILE_STATE_ERROR:
            violations.append(f"生产DB运行后读取失败: {self.after.main.error}")
        elif (self.before.main.sha256 is not None
              and self.after.main.sha256 is not None
              and self.before.main.sha256 != self.after.main.sha256):
            violations.append(
                f"生产DB哈希变化: {self.before.main.sha256[:12]}..."
                f" → {self.after.main.sha256[:12]}..."
            )

        # 4. 影子身份检查
        ok_identity, id_err = validate_shadow_identity(
            shadow_db_path, self._prod_main,
        )
        if not ok_identity:
            violations.append(f"影子DB身份检查失败: {id_err}")

        ok = len(violations) == 0
        return ok, changes, violations

    def create_connection_factory(self, shadow_db_path: Path) -> ShadowConnectionFactory:
        return ShadowConnectionFactory(self._prod_main, shadow_db_path)

    def _details(self) -> dict:
        return {
            "protected_prod_db": str(self._prod_main.resolve()),
            "prod_db_device": self.before.main.device if self.before else None,
            "prod_db_inode": self.before.main.inode if self.before else None,
            "before_snapshot": self.before.summary() if self.before else {},
        }
