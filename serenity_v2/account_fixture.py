"""
Serenity 2.0 — 账户 Fixture 加载与对账 (P0-3)

固定加载三票持仓 Fixture 并输出完整账户上下文。
所有从 Fixture 派生的信号必须携带:
  ACCOUNT_CONTEXT_FIXTURE  — 账户上下文来自 Fixture 而非实时数据
  ACCOUNT_CONTEXT_STALE    — 快照时间早于当前交易日
  NOT_FOR_EXECUTION        — 禁止实盘执行

不变量:
  cash + position_market_value = total_assets
  available_shares <= total_shares (each position)
  available_shares + unsettled_buy_shares <= total_shares
  SELL/REDUCE quantity <= available_shares
  BUY/ADD 不得超过现金与仓位风险约束
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# 对账容差（浮点精度）
RECONCILIATION_TOLERANCE = 0.02

# 期望的三票代码集合
EXPECTED_CODES = frozenset({"600487", "600176", "000988"})


# ══════════════════════════════════════════════════════════════════════════
# 数据模型
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class FixturePosition:
    """Fixture 中的单笔持仓。"""
    code: str
    name: str
    market: str
    shares: int
    available_shares: int
    unsettled_buy_shares: int = 0
    cost_basis: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class AccountFixture:
    """完整的 Fixture 账户状态。"""
    fixture_id: str
    fixture_version: str
    snapshot_as_of: str
    cash: float
    total_assets: float
    position_market_value: float
    positions: list[FixturePosition] = field(default_factory=list)

    # 计算字段
    _snapshot_id: str = ""            # 短 ID (16 hex, 用于显示)
    _snapshot_id_full: str = ""       # 完整 ID (64 hex, 用于幂等键)
    _file_hash: str = ""              # Fixture 文件 SHA-256 (64 hex)
    _fixture_size: int = 0
    _fixture_mtime: float = 0.0
    _fixture_realpath: str = ""
    _source_path: str = ""

    @property
    def snapshot_id(self) -> str:
        """16 位短 ID，用于显示。"""
        return self._snapshot_id

    @property
    def snapshot_id_full(self) -> str:
        """完整 64 位 SHA-256，用于幂等键和审计。"""
        return self._snapshot_id_full

    @property
    def file_hash(self) -> str:
        """Fixture 文件完整 SHA-256 (64 hex)。"""
        return self._file_hash

    @property
    def fixture_size(self) -> int:
        return self._fixture_size

    @property
    def fixture_mtime(self) -> float:
        return self._fixture_mtime

    @property
    def fixture_realpath(self) -> str:
        return self._fixture_realpath

    @property
    def source_path(self) -> str:
        return self._source_path

    def metadata_dict(self) -> dict:
        """审计用的完整元数据。"""
        return {
            "fixture_id": self.fixture_id,
            "fixture_version": self.fixture_version,
            "fixture_sha256": self._file_hash,
            "fixture_size": self._fixture_size,
            "fixture_mtime": self._fixture_mtime,
            "fixture_realpath": self._fixture_realpath,
            "account_snapshot_id": self._snapshot_id,
            "account_snapshot_id_full": self._snapshot_id_full,
            "account_snapshot_as_of": self.snapshot_as_of,
        }


# ══════════════════════════════════════════════════════════════════════════
# 加载
# ══════════════════════════════════════════════════════════════════════════

def load_fixture(path: Path) -> AccountFixture:
    """加载 Fixture JSON 文件。

    成功返回 AccountFixture; 任何问题抛出 ValueError 并附带详细原因。
    """
    return _Loader(path)._load()


class _Loader:
    """分步加载器 — 每步独立验证，错误信息精确。"""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> AccountFixture:
        # Step 1: 读取并解析 JSON
        data = self._read_json()

        # Step 2: 验证顶层结构
        self._validate_schema(data)

        # Step 3: 解析持仓
        positions = self._parse_positions(data["positions"])

        # Step 4: 验证持仓集合
        self._validate_position_set(positions)

        # Step 5: 不变量对账
        self._validate_invariants(data, positions)

        # Step 6: 计算哈希和文件元数据
        fixture = AccountFixture(
            fixture_id=data["fixture_id"],
            fixture_version=data["fixture_version"],
            snapshot_as_of=data["account_snapshot_as_of"],
            cash=data["cash"],
            total_assets=data["total_assets"],
            position_market_value=data["position_market_value"],
            positions=positions,
        )
        resolved = self.path.resolve()
        fixture._source_path = str(resolved)
        fixture._fixture_realpath = str(resolved)
        fixture._file_hash = _compute_file_hash(self.path)

        try:
            stat = self.path.stat()
            fixture._fixture_size = stat.st_size
            fixture._fixture_mtime = stat.st_mtime
        except OSError:
            fixture._fixture_size = -1
            fixture._fixture_mtime = -1.0

        fixture._snapshot_id_full = _compute_snapshot_id_full(fixture)
        fixture._snapshot_id = fixture._snapshot_id_full[:16]

        return fixture

    def _read_json(self) -> dict:
        if not self.path.exists():
            raise ValueError(f"Fixture 文件不存在: {self.path}")
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Fixture 文件读取失败: {self.path} — {exc}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Fixture JSON 解析失败: {self.path} — {exc}")
        if not isinstance(data, dict):
            raise ValueError(f"Fixture 顶层必须是 JSON 对象，收到 {type(data).__name__}")
        return data

    def _validate_schema(self, data: dict) -> None:
        required_top = {
            "fixture_id", "fixture_version", "account_snapshot_as_of",
            "cash", "total_assets", "position_market_value", "positions",
        }
        missing = required_top - set(data.keys())
        if missing:
            raise ValueError(f"Fixture 缺少必需字段: {missing}")

        # 类型检查
        for f in ("cash", "total_assets", "position_market_value"):
            if not isinstance(data[f], (int, float)):
                raise ValueError(f"字段 {f} 必须是数字，收到 {type(data[f]).__name__}")
            if data[f] < 0:
                raise ValueError(f"字段 {f} 不能为负: {data[f]}")

        if not isinstance(data["positions"], list):
            raise ValueError(
                f"positions 必须是数组，收到 {type(data['positions']).__name__}"
            )

    def _parse_positions(self, raw_positions: list) -> list[FixturePosition]:
        positions: list[FixturePosition] = []
        for i, p in enumerate(raw_positions):
            if not isinstance(p, dict):
                raise ValueError(f"positions[{i}] 必须是 JSON 对象")
            req = {"code", "name", "market", "shares", "available_shares"}
            missing = req - set(p.keys())
            if missing:
                raise ValueError(f"positions[{i}] 缺少字段: {missing}")

            # 负值拒绝
            for f_int in ("shares", "available_shares", "unsettled_buy_shares"):
                val = p.get(f_int, 0)
                if not isinstance(val, (int, float)) or val < 0:
                    raise ValueError(
                        f"positions[{i}].{f_int} 不能为负: {val}"
                    )

            for f_float in ("cost_basis", "current_price", "market_value"):
                val = p.get(f_float, 0)
                if not isinstance(val, (int, float)):
                    raise ValueError(
                        f"positions[{i}].{f_float} 必须是数字，收到 {type(val).__name__}"
                    )
                if isinstance(val, (int, float)) and val < 0:
                    raise ValueError(
                        f"positions[{i}].{f_float} 不能为负: {val}"
                    )

            pos = FixturePosition(
                code=str(p["code"]),
                name=str(p.get("name", "")),
                market=str(p.get("market", "")),
                shares=int(p["shares"]),
                available_shares=int(p.get("available_shares", p["shares"])),
                unsettled_buy_shares=int(p.get("unsettled_buy_shares", 0)),
                cost_basis=float(p.get("cost_basis", 0)),
                current_price=float(p.get("current_price", 0)),
                market_value=float(p.get("market_value", 0)),
                pnl=float(p.get("pnl", 0)),
                pnl_pct=float(p.get("pnl_pct", 0)),
            )
            positions.append(pos)
        return positions

    def _validate_position_set(self, positions: list[FixturePosition]) -> None:
        codes_found = {p.code for p in positions}

        # 重复代码拒绝
        if len(codes_found) != len(positions):
            # 找出重复的
            seen: set[str] = set()
            dupes: list[str] = []
            for p in positions:
                if p.code in seen:
                    dupes.append(p.code)
                seen.add(p.code)
            raise ValueError(f"持仓存在重复代码: {dupes}")

        # 缺失代码拒绝
        missing_codes = EXPECTED_CODES - codes_found
        if missing_codes:
            raise ValueError(
                f"缺少必需持仓: {sorted(missing_codes)}"
                f" (已有: {sorted(codes_found)})"
            )

        # 多余代码拒绝
        extra_codes = codes_found - EXPECTED_CODES
        if extra_codes:
            raise ValueError(
                f"存在未预期的持仓: {sorted(extra_codes)}"
                f" (期望: {sorted(EXPECTED_CODES)})"
            )

        # 每票检查: available_shares <= total_shares
        for p in positions:
            if p.available_shares > p.shares:
                raise ValueError(
                    f"{p.code}: 可卖 {p.available_shares} > 总持仓 {p.shares}"
                )

        # 每票检查: available_shares + unsettled_buy_shares <= total_shares
        for p in positions:
            if p.available_shares + abs(p.unsettled_buy_shares) > p.shares:
                raise ValueError(
                    f"{p.code}: 可卖 {p.available_shares} + "
                    f"未结算 {p.unsettled_buy_shares} > 总持仓 {p.shares}"
                )

    def _validate_invariants(self, data: dict, positions: list[FixturePosition]) -> None:
        cash = float(data["cash"])
        total_assets = float(data["total_assets"])
        declared_mv = float(data["position_market_value"])

        # 现金非负
        if cash < -RECONCILIATION_TOLERANCE:
            raise ValueError(f"现金为负: {cash}")

        # 持仓市值之和
        actual_mv = sum(p.market_value for p in positions)

        # cash + position_market_value = total_assets
        computed_total = round(cash + actual_mv, 2)
        if abs(computed_total - total_assets) > RECONCILIATION_TOLERANCE:
            raise ValueError(
                f"资产对账失败: cash({cash}) + position_market_value({actual_mv})"
                f" = {computed_total} ≠ total_assets({total_assets})"
            )

        # 声明市值与实际市值
        if abs(actual_mv - declared_mv) > RECONCILIATION_TOLERANCE:
            raise ValueError(
                f"市值对账失败: 声明 {declared_mv} ≠ 实际 {actual_mv}"
            )


# ══════════════════════════════════════════════════════════════════════════
# 哈希
# ══════════════════════════════════════════════════════════════════════════

def _compute_file_hash(path: Path) -> str:
    """Fixture 文件内容的 SHA256 指纹（完整 64 hex）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compute_snapshot_id_full(fixture: AccountFixture) -> str:
    """计算完整的账户快照 ID（64 hex SHA-256），用于幂等键和审计。"""
    payload = json.dumps({
        "fixture_id": fixture.fixture_id,
        "fixture_version": fixture.fixture_version,
        "snapshot_as_of": fixture.snapshot_as_of,
        "cash": fixture.cash,
        "total_assets": fixture.total_assets,
        "positions": sorted(
            [
                [p.code, p.shares, p.available_shares,
                 p.unsettled_buy_shares, p.market_value]
                for p in fixture.positions
            ],
            key=lambda x: x[0],
        ),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_snapshot_id(fixture: AccountFixture) -> str:
    """16 位短 ID（_snapshot_id_full 的前 16 位），用于显示。"""
    return _compute_snapshot_id_full(fixture)[:16]


def recompute_snapshot_id(fixture: AccountFixture) -> str:
    """重新计算短快照 ID。"""
    return _compute_snapshot_id(fixture)


def recompute_snapshot_id_full(fixture: AccountFixture) -> str:
    """重新计算完整快照 ID（64 hex）。"""
    return _compute_snapshot_id_full(fixture)


# ══════════════════════════════════════════════════════════════════════════
# 转换
# ══════════════════════════════════════════════════════════════════════════

def to_account_state(fixture: AccountFixture):
    """将 AccountFixture 转换为 AccountBaseline 的 AccountState。"""
    from .account_baseline import AccountState, Position

    positions = [
        Position(
            code=p.code, name=p.name, market=p.market,
            shares=p.shares,
            available_shares=p.available_shares,
            unsettled_buy_shares=p.unsettled_buy_shares,
            cost_basis=p.cost_basis,
            current_price=p.current_price,
            market_value=p.market_value,
            pnl=p.pnl, pnl_pct=p.pnl_pct,
        )
        for p in fixture.positions
    ]

    return AccountState(
        total_assets=fixture.total_assets,
        total_market_value=fixture.position_market_value,
        available_cash=fixture.cash,
        position_ratio_pct=round(
            fixture.position_market_value / fixture.total_assets * 100, 1
        ) if fixture.total_assets > 0 else 0,
        positions=positions,
        snapshot_at=fixture.snapshot_as_of,
        data_source="fixture",
        data_confidence="high",
        notes=(
            f"P0-3 Fixture: {fixture.fixture_id} "
            f"snapshot_id={fixture.snapshot_id}"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════
# 信号标签
# ══════════════════════════════════════════════════════════════════════════

FIXTURE_SIGNAL_TAGS = [
    "ACCOUNT_CONTEXT_FIXTURE",
    "ACCOUNT_CONTEXT_STALE",
    "NOT_FOR_EXECUTION",
]


def fixture_signal_tags() -> list[str]:
    """Fixture 派生信号必须携带的标签。"""
    return list(FIXTURE_SIGNAL_TAGS)


# ══════════════════════════════════════════════════════════════════════════
# 单例
# ══════════════════════════════════════════════════════════════════════════

_fixture: Optional[AccountFixture] = None


def get_fixture() -> AccountFixture:
    """获取已加载的 Fixture。未加载时抛出 RuntimeError。"""
    if _fixture is None:
        raise RuntimeError("AccountFixture 未加载，请先调用 load_and_set_fixture()")
    return _fixture


def load_and_set_fixture(path: Path) -> AccountFixture:
    """加载 Fixture 并设为全局单例。"""
    global _fixture
    _fixture = load_fixture(path)
    return _fixture


def reset_fixture() -> None:
    """重置 Fixture 单例（测试用）。"""
    global _fixture
    _fixture = None
