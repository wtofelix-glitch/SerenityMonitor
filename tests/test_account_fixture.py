"""
P0-3 测试: 三篮 Fixture 加载与对账

12 个必测场景 + 集成测试。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from serenity_v2.account_fixture import (
    AccountFixture,
    FixturePosition,
    FIXTURE_SIGNAL_TAGS,
    EXPECTED_CODES,
    RECONCILIATION_TOLERANCE,
    load_fixture,
    load_and_set_fixture,
    get_fixture,
    reset_fixture,
    to_account_state,
    recompute_snapshot_id,
    _compute_file_hash,
    _compute_snapshot_id,
)

# ══════════════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════════════

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "shadow_data" / "b2"
VALID_FIXTURE_PATH = FIXTURE_DIR / "account_fixture.json"


def _make_fixture_json(data: dict) -> str:
    """序列化为 JSON 字符串。"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _write_temp_fixture(data: dict, suffix: str = ".json") -> Path:
    """写入临时 Fixture 文件，返回路径。"""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    )
    tmp.write(_make_fixture_json(data))
    tmp.close()
    return Path(tmp.name)


_VALID_FIXTURE_DATA = {
    "fixture_version": "1.0.0",
    "fixture_id": "test-fixture",
    "account_snapshot_as_of": "2026-07-22T15:00:00+08:00",
    "cash": 21392.07,
    "total_assets": 249578.07,
    "position_market_value": 228186.00,
    "positions": [
        {
            "code": "600487", "name": "亨通光电", "market": "sh",
            "shares": 1500, "available_shares": 1500,
            "unsettled_buy_shares": 0,
            "cost_basis": 55.23, "current_price": 55.12,
            "market_value": 82680.00, "pnl": -227.29, "pnl_pct": -0.20,
        },
        {
            "code": "600176", "name": "中国巨石", "market": "sh",
            "shares": 2000, "available_shares": 2000,
            "unsettled_buy_shares": 0,
            "cost_basis": 38.71, "current_price": 38.70,
            "market_value": 77400.00, "pnl": -77.02, "pnl_pct": -0.03,
        },
        {
            "code": "000988", "name": "华工科技", "market": "sz",
            "shares": 600, "available_shares": 600,
            "unsettled_buy_shares": 0,
            "cost_basis": 122.716, "current_price": 113.51,
            "market_value": 68106.00, "pnl": -5574.01, "pnl_pct": -7.50,
        },
    ],
}


def teardown_function():
    reset_fixture()


# ══════════════════════════════════════════════════════════════════════════
# T01: 三票全部成功加载
# ══════════════════════════════════════════════════════════════════════════

class TestT01_ThreeBasketLoaded:
    def test_all_three_loaded(self):
        fixture = load_fixture(VALID_FIXTURE_PATH)
        codes = {p.code for p in fixture.positions}
        assert codes == {"600487", "600176", "000988"}

    def test_each_position_has_required_fields(self):
        fixture = load_fixture(VALID_FIXTURE_PATH)
        for p in fixture.positions:
            assert p.code
            assert p.name
            assert p.market in ("sh", "sz")
            assert isinstance(p.shares, int) and p.shares > 0
            assert isinstance(p.available_shares, int)

    def test_fixture_metadata_populated(self):
        fixture = load_fixture(VALID_FIXTURE_PATH)
        assert fixture.fixture_id == "p0-3-three-basket-20260722"
        assert fixture.fixture_version == "1.0.0"
        assert fixture.snapshot_as_of == "2026-07-22T15:00:00+08:00"
        assert fixture.cash == 21392.07
        assert fixture.total_assets == 249578.07
        assert fixture.position_market_value == 228186.00
        assert fixture.snapshot_id
        assert fixture.file_hash

    def test_exact_values_match_spec(self):
        """验证每个持仓的具体数值与规格一致。"""
        fixture = load_fixture(VALID_FIXTURE_PATH)
        by_code = {p.code: p for p in fixture.positions}

        h = by_code["600487"]
        assert h.name == "亨通光电"
        assert h.shares == 1500
        assert h.available_shares == 1500
        assert h.market_value == 82680.00

        z = by_code["600176"]
        assert z.name == "中国巨石"
        assert z.shares == 2000
        assert z.available_shares == 2000
        assert z.market_value == 77400.00

        hg = by_code["000988"]
        assert hg.name == "华工科技"
        assert hg.shares == 600
        assert hg.available_shares == 600
        assert hg.market_value == 68106.00


# ══════════════════════════════════════════════════════════════════════════
# T02: 缺少任意一票时启动失败
# ══════════════════════════════════════════════════════════════════════════

class TestT02_MissingPositionRejected:
    def test_missing_600487(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"] = [p for p in data["positions"] if p["code"] != "600487"]
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="缺少必需持仓"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_missing_600176(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"] = [p for p in data["positions"] if p["code"] != "600176"]
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="缺少必需持仓"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_missing_000988(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"] = [p for p in data["positions"] if p["code"] != "000988"]
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="缺少必需持仓"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_empty_positions(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"] = []
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="缺少必需持仓"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)


# ══════════════════════════════════════════════════════════════════════════
# T03: 重复代码拒绝
# ══════════════════════════════════════════════════════════════════════════

class TestT03_DuplicateCodeRejected:
    def test_duplicate_600487(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        # 复制 600487 的持仓
        dup = copy.deepcopy(data["positions"][0])
        data["positions"].append(dup)
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="重复代码"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_all_duplicates(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"] = data["positions"] * 2  # 每个重复一次
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="重复代码"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)


# ══════════════════════════════════════════════════════════════════════════
# T04: 负现金、负数量、负价格拒绝
# ══════════════════════════════════════════════════════════════════════════

class TestT04_NegativeValuesRejected:
    def test_negative_cash(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["cash"] = -1000.00
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_negative_shares(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"][0]["shares"] = -100
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_negative_available_shares(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"][1]["available_shares"] = -50
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_negative_price(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"][2]["current_price"] = -10.0
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_negative_market_value(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"][0]["market_value"] = -5000.0
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_negative_total_assets(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["total_assets"] = -1.0
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_negative_unsettled_buy(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"][0]["unsettled_buy_shares"] = -10
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="不能为负"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)


# ══════════════════════════════════════════════════════════════════════════
# T05: 总资产无法对账时失败关闭
# ══════════════════════════════════════════════════════════════════════════

class TestT05_ReconciliationFailure:
    def test_cash_plus_mv_not_equal_total(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["total_assets"] = 300000.00  # 明显错误
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="资产对账失败"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_position_market_value_mismatch(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["position_market_value"] = 200000.00  # 与实际不符
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="市值对账失败"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)

    def test_small_deviation_still_allowed(self):
        """小偏差在容差范围内应允许通过。"""
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["total_assets"] = 249578.08  # 0.01 差异
        fpath = _write_temp_fixture(data)
        try:
            fixture = load_fixture(fpath)
            assert fixture is not None
        finally:
            os.unlink(fpath)

    def test_available_exceeds_total_rejected(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        data["positions"][0]["available_shares"] = 2000  # 大于 shares=1500
        fpath = _write_temp_fixture(data)
        try:
            with pytest.raises(ValueError, match="可卖.*>.*总持仓"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)


# ══════════════════════════════════════════════════════════════════════════
# T06: Fixture 文件哈希变化可检测
# ══════════════════════════════════════════════════════════════════════════

class TestT06_FileHashChangeDetection:
    def test_same_file_same_hash(self):
        h1 = _compute_file_hash(VALID_FIXTURE_PATH)
        h2 = _compute_file_hash(VALID_FIXTURE_PATH)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_fixture_different_hash(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        fpath1 = _write_temp_fixture(data)

        data["cash"] = 21392.08  # 微小变化
        fpath2 = _write_temp_fixture(data)

        try:
            h1 = _compute_file_hash(fpath1)
            h2 = _compute_file_hash(fpath2)
            assert h1 != h2
        finally:
            os.unlink(fpath1)
            os.unlink(fpath2)

    def test_file_not_found(self):
        nonexistent = Path(tempfile.gettempdir()) / "nonexistent_fixture_test.json"
        with pytest.raises(ValueError, match="不存在"):
            load_fixture(nonexistent)

    def test_invalid_json_rejected(self):
        fpath = _write_temp_fixture({"bad": "data"})  # not a real fixture
        try:
            with pytest.raises(ValueError, match="缺少必需字段"):
                load_fixture(fpath)
        finally:
            os.unlink(fpath)


# ══════════════════════════════════════════════════════════════════════════
# T07: account_snapshot_id 对相同 Fixture 保持稳定
# ══════════════════════════════════════════════════════════════════════════

class TestT07_SnapshotIdStability:
    def test_same_fixture_same_id(self):
        f1 = load_fixture(VALID_FIXTURE_PATH)
        f2 = load_fixture(VALID_FIXTURE_PATH)
        assert f1.snapshot_id == f2.snapshot_id
        assert len(f1.snapshot_id) == 16

    def test_recompute_matches(self):
        fixture = load_fixture(VALID_FIXTURE_PATH)
        recalc = recompute_snapshot_id(fixture)
        assert fixture.snapshot_id == recalc

    def test_id_is_hex_string(self):
        fixture = load_fixture(VALID_FIXTURE_PATH)
        int(fixture.snapshot_id, 16)  # 不抛异常 = 有效 hex

    def test_different_loading_same_id(self):
        """确保每次加载相同的 Fixture 产生相同的 ID。"""
        ids = []
        for _ in range(5):
            f = load_fixture(VALID_FIXTURE_PATH)
            ids.append(f.snapshot_id)
        assert len(set(ids)) == 1


# ══════════════════════════════════════════════════════════════════════════
# T08: Fixture 变化产生新的快照 ID
# ══════════════════════════════════════════════════════════════════════════

class TestT08_FixtureChangeNewSnapshotId:
    def test_cash_change_new_id(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        fpath1 = _write_temp_fixture(data)
        # 调整 cash 同时调整 total_assets 保持对账通过
        new_cash = 22000.00
        mv = data["position_market_value"]
        data["cash"] = new_cash
        data["total_assets"] = round(new_cash + mv, 2)
        fpath2 = _write_temp_fixture(data)
        try:
            f1 = load_fixture(fpath1)
            f2 = load_fixture(fpath2)
            assert f1.snapshot_id != f2.snapshot_id
        finally:
            os.unlink(fpath1)
            os.unlink(fpath2)

    def test_position_change_new_id(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        fpath1 = _write_temp_fixture(data)
        data["positions"][0]["shares"] = 2000  # 600487: 1500 → 2000
        fpath2 = _write_temp_fixture(data)
        try:
            f1 = load_fixture(fpath1)
            f2 = load_fixture(fpath2)
            assert f1.snapshot_id != f2.snapshot_id
        finally:
            os.unlink(fpath1)
            os.unlink(fpath2)

    def test_fixture_id_change_new_snapshot_id(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        fpath1 = _write_temp_fixture(data)
        data["fixture_id"] = "different-fixture"
        fpath2 = _write_temp_fixture(data)
        try:
            f1 = load_fixture(fpath1)
            f2 = load_fixture(fpath2)
            assert f1.snapshot_id != f2.snapshot_id
        finally:
            os.unlink(fpath1)
            os.unlink(fpath2)

    def test_snapshot_as_of_change_new_id(self):
        data = copy.deepcopy(_VALID_FIXTURE_DATA)
        fpath1 = _write_temp_fixture(data)
        data["account_snapshot_as_of"] = "2026-07-23T10:00:00+08:00"
        fpath2 = _write_temp_fixture(data)
        try:
            f1 = load_fixture(fpath1)
            f2 = load_fixture(fpath2)
            assert f1.snapshot_id != f2.snapshot_id
        finally:
            os.unlink(fpath1)
            os.unlink(fpath2)


# ══════════════════════════════════════════════════════════════════════════
# T09: 信号血缘正确关联快照 ID
# ══════════════════════════════════════════════════════════════════════════

class TestT09_SignalLineage:
    def test_snapshot_id_present_in_to_account_state(self):
        fixture = load_fixture(VALID_FIXTURE_PATH)
        state = to_account_state(fixture)
        assert fixture.snapshot_id in state.notes
        assert "p0-3-three-basket" in state.notes

    def test_account_snapshot_id_preserved_in_b2runner(self):
        """B2Runner 的 _account_snapshot_id 应与 fixture snapshot_id 一致。"""
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.env import reset_env
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        prod_db = Path(tmpdir) / "serenity.db"
        prod_db.write_bytes(b'fake')

        try:
            runner = B2Runner(
                duration_seconds=30,
                interval_seconds=5,
                protected_prod_db=str(prod_db),
                manifest_path='',
            )
            fixture = load_fixture(VALID_FIXTURE_PATH)
            assert runner._account_snapshot_id == fixture.snapshot_id_full
            assert len(runner._account_snapshot_id) == 64
        finally:
            reset_env()
            reset_baseline()
            reset_fix()


# ══════════════════════════════════════════════════════════════════════════
# T10: 运行前后 Fixture 及账户状态完全不变
# ══════════════════════════════════════════════════════════════════════════

class TestT10_PrePostInvariance:
    def test_fixture_hash_invariant_across_loads(self):
        """多次加载不改变 Fixture 文件哈希。"""
        h_before = _compute_file_hash(VALID_FIXTURE_PATH)
        for _ in range(3):
            load_fixture(VALID_FIXTURE_PATH)
        h_after = _compute_file_hash(VALID_FIXTURE_PATH)
        assert h_before == h_after

    def test_snapshot_id_invariant_across_loads(self):
        """多次加载产生相同的 snapshot_id。"""
        f = load_fixture(VALID_FIXTURE_PATH)
        sid = f.snapshot_id
        for _ in range(3):
            f2 = load_fixture(VALID_FIXTURE_PATH)
            assert f2.snapshot_id == sid

    def test_fixture_data_not_mutated_by_load(self):
        """load_fixture 不改变 fixture 对象的内部数据。"""
        f1 = load_fixture(VALID_FIXTURE_PATH)
        f2 = load_fixture(VALID_FIXTURE_PATH)
        assert f1.cash == f2.cash
        assert f1.total_assets == f2.total_assets
        assert len(f1.positions) == len(f2.positions)
        for p1, p2 in zip(f1.positions, f2.positions):
            assert p1.shares == p2.shares
            assert p1.available_shares == p2.available_shares


# ══════════════════════════════════════════════════════════════════════════
# T11: 三票事件分别能获取正确持仓上下文
# ══════════════════════════════════════════════════════════════════════════

class TestT11_PositionContext:
    def test_signal_context_for_each_stock(self):
        """验证每票都能从 AccountBaseline.signal_context() 获取正确上下文。"""
        from serenity_v2.account_baseline import get_baseline, reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix
        from serenity_v2.env import SerenityEnv, set_env, reset_env

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        shadow_db = Path(tmpdir) / "shadow.db"

        try:
            env = SerenityEnv.shadow(db_path=shadow_db)
            set_env(env)

            from serenity_v2.migrations import apply_migrations
            apply_migrations(shadow_db)

            fixture = load_fixture(VALID_FIXTURE_PATH)
            state = to_account_state(fixture)

            baseline = get_baseline()
            baseline.save_snapshot(state)

            # 600487
            ctx = baseline.signal_context("600487", state)
            assert ctx["holding"] is True
            assert ctx["position_shares"] == 1500
            assert ctx["available_shares"] == 1500
            assert ctx["code"] == "600487"

            # 600176
            ctx = baseline.signal_context("600176", state)
            assert ctx["holding"] is True
            assert ctx["position_shares"] == 2000
            assert ctx["available_shares"] == 2000

            # 000988
            ctx = baseline.signal_context("000988", state)
            assert ctx["holding"] is True
            assert ctx["position_shares"] == 600
            assert ctx["available_shares"] == 600

            # 非持仓标的
            ctx = baseline.signal_context("000001", state)
            assert ctx["holding"] is False
        finally:
            reset_env()
            reset_baseline()
            reset_fix()

    def test_sell_reduce_constrained_by_available(self):
        """SELL/REDUCE 数量不能超过可卖数量。"""
        fixture = load_fixture(VALID_FIXTURE_PATH)
        for p in fixture.positions:
            assert p.available_shares <= p.shares
            # 任何卖出操作不能超过 available_shares
            max_sell = p.available_shares
            assert max_sell <= p.shares

    def test_buy_constrained_by_cash(self):
        """BUY/ADD 受现金约束。"""
        fixture = load_fixture(VALID_FIXTURE_PATH)
        # 最贵的票: 华工科技 113.51/股
        # 最多可买: 21392.07 / 113.51 ≈ 188 股 (整手 100)
        max_shares = int(fixture.cash / 113.51)
        assert max_shares >= 100  # 至少 1 手
        # 买入后不超 40% 单票仓位限制
        state = to_account_state(fixture)
        from serenity_v2.account_baseline import RiskConstraints
        rules = RiskConstraints()
        new_weight = (68106.00 + 100 * 113.51) / fixture.total_assets
        assert new_weight < rules.max_single_position_pct + 0.05


# ══════════════════════════════════════════════════════════════════════════
# T12: Stale Fixture 始终携带正确标签
# ══════════════════════════════════════════════════════════════════════════

class TestT12_StaleFixtureTags:
    def test_all_three_tags_present(self):
        assert "ACCOUNT_CONTEXT_FIXTURE" in FIXTURE_SIGNAL_TAGS
        assert "ACCOUNT_CONTEXT_STALE" in FIXTURE_SIGNAL_TAGS
        assert "NOT_FOR_EXECUTION" in FIXTURE_SIGNAL_TAGS
        assert len(FIXTURE_SIGNAL_TAGS) == 3

    def test_b2runner_injects_all_tags(self):
        """验证 B2Runner._record_signal() 注入所有三个标签。"""
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.env import reset_env
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        prod_db = Path(tmpdir) / "serenity.db"
        prod_db.write_bytes(b'fake')

        try:
            runner = B2Runner(
                duration_seconds=30,
                interval_seconds=5,
                protected_prod_db=str(prod_db),
                manifest_path='',
            )

            # 模拟一个信号对象
            from unittest.mock import MagicMock
            sig = MagicMock()
            sig.signal_id = "test-sig-001"
            sig.symbol = "600487"
            sig.candidate_signal_level = "WATCH"
            sig.effective_signal_level = "WATCH"
            sig.candidate_trade_action = "HOLD"
            sig.effective_trade_action = "HOLD"
            sig.primary_normalization_reason = ""
            sig.secondary_normalization_reasons = []
            sig.confidence = 0.5
            sig.market_session = "CONTINUOUS_AM"
            sig.event_id = "evt-001"
            sig.action_suppressed = False
            sig.execution_tags = []

            runner._record_signal(sig)

            assert "ACCOUNT_CONTEXT_FIXTURE" in sig.execution_tags
            assert "ACCOUNT_CONTEXT_STALE" in sig.execution_tags
            assert "NOT_FOR_EXECUTION" in sig.execution_tags
            assert "SHADOW_ONLY" not in [t for t in sig.execution_tags
                                         if t not in FIXTURE_SIGNAL_TAGS]
        finally:
            reset_env()
            reset_baseline()
            reset_fix()

    def test_shadow_tags_not_checked_against_fixture_tags(self):
        """确保 FIXTURE 标签只包含三个特定值。"""
        assert FIXTURE_SIGNAL_TAGS == [
            "ACCOUNT_CONTEXT_FIXTURE",
            "ACCOUNT_CONTEXT_STALE",
            "NOT_FOR_EXECUTION",
        ]


# ══════════════════════════════════════════════════════════════════════════
# 集成测试: B2Runner 与 Fixture 全链路
# ══════════════════════════════════════════════════════════════════════════

class TestIntegrationFixtureInB2Runner:
    def test_fixture_loaded_during_b2_init(self):
        """B2Runner._init_env() 应自动加载 Fixture。"""
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.env import reset_env
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        prod_db = Path(tmpdir) / "serenity.db"
        prod_db.write_bytes(b'fake')

        try:
            runner = B2Runner(
                duration_seconds=30,
                interval_seconds=5,
                protected_prod_db=str(prod_db),
                manifest_path='',
            )
            assert runner._fixture is not None
            assert runner._fixture.fixture_id == "p0-3-three-basket-20260722"
            assert runner._account_snapshot_id == runner._fixture.snapshot_id_full
            assert len(runner._fixture.positions) == 3
        finally:
            reset_env()
            reset_baseline()
            reset_fix()

    def test_verify_environment_includes_fixture_details(self):
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.env import reset_env
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        prod_db = Path(tmpdir) / "serenity.db"
        prod_db.write_bytes(b'fake')

        try:
            runner = B2Runner(
                duration_seconds=30,
                interval_seconds=5,
                protected_prod_db=str(prod_db),
                manifest_path='',
            )
            ok, details, violations = runner.verify_environment()
            assert details["account_state_mode"] == "FIXTURE"
            assert details["account_state_stale"] == "true"
            assert "account_snapshot_id" in details
            assert details["account_snapshot_id"] == runner._account_snapshot_id
            assert "account_snapshot_as_of" in details
            assert "fixture_file_hash" in details
        finally:
            reset_env()
            reset_baseline()
            reset_fix()

    def test_strategy_config_hash_stable(self):
        """B2 策略配置哈希应对相同参数保持稳定。"""
        from serenity_v2.phase_b2 import B2Runner
        from serenity_v2.env import reset_env
        from serenity_v2.account_baseline import reset_baseline
        from serenity_v2.account_fixture import reset_fixture as reset_fix

        reset_env()
        reset_baseline()
        reset_fix()

        import tempfile
        tmpdir = tempfile.mkdtemp()
        prod_db = Path(tmpdir) / "serenity.db"
        prod_db.write_bytes(b'fake')

        try:
            r1 = B2Runner(
                duration_seconds=30, interval_seconds=5,
                protected_prod_db=str(prod_db), manifest_path='',
            )
            h1 = r1._strategy_config_hash
        finally:
            reset_env()
            reset_baseline()
            reset_fix()

        reset_env()
        reset_baseline()
        reset_fix()

        try:
            r2 = B2Runner(
                duration_seconds=30, interval_seconds=5,
                protected_prod_db=str(prod_db), manifest_path='',
            )
            h2 = r2._strategy_config_hash
            assert h1 == h2
        finally:
            reset_env()
            reset_baseline()
            reset_fix()
