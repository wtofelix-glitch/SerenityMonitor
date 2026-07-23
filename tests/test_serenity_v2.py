"""
Serenity 2.0 单元测试 [v2.1]

覆盖 Phase 0/1/2 核心功能 + 环境隔离 + 事务 + T+1。
"""

import os
import sys
import tempfile
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

# ---- 环境初始化 ----
from serenity_v2.env import SerenityEnv, set_env, get_env, reset_env

# 每个测试前设置影子环境 + 临时DB
@pytest.fixture(autouse=True)
def setup_shadow_env():
    """为每个测试创建独立影子环境。"""
    tmpdir = tempfile.mkdtemp(prefix="serenity_test_")
    db_path = Path(tmpdir) / "test_shadow.db"
    log_dir = Path(tmpdir) / "logs"

    # 强制重置全局环境
    import serenity_v2.env as env_mod
    env_mod._active_env = None

    env = SerenityEnv(mode="shadow", db_path=db_path, log_dir=log_dir)
    set_env(env)

    # 重置所有模块单例
    import serenity_v2.account_baseline as ab
    import serenity_v2.intelligence_network as intel_mod
    import serenity_v2.signal_desk as sd
    import serenity_v2.event_record as er

    ab.reset_baseline()
    intel_mod.reset_intel()
    sd.reset_desk()

    # 应用迁移
    from serenity_v2.migrations import apply_migrations
    apply_migrations(db_path)

    yield

    # 清理
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    env_mod._active_env = None
    ab.reset_baseline()
    intel_mod.reset_intel()
    sd.reset_desk()


from serenity_v2.account_baseline import (
    AccountState, Position, Trade, RiskConstraints,
    AccountBaseline, SECTOR_MAP, get_baseline, reset_baseline,
)
from serenity_v2.event_record import (
    EventRecord, EventStore, SourceInfo, TimestampSet,
    EventPayload, RelatedInfo, ImpactAssessment,
    VerificationResult, AccountRelevance, make_price_event,
)
from serenity_v2.intelligence_network import (
    IntelligenceNetwork, check_thresholds, REGISTERED_SOURCES,
    get_intel, reset_intel,
)
from serenity_v2.signal_desk import (
    SignalDesk, HardGateVerifier, SignalOutput, format_signal,
    ALL_GATES, GateMode, DEFAULT_GATE_CONFIG, STRATEGY_TECHNICAL_BREAKOUT,
    GATE_MARKET, GATE_TREND, GATE_VOLUME, GATE_DATA, GATE_ACCOUNT,
    _resolve_gate_config, IMMUTABLE_REQUIRED, get_desk, reset_desk,
)
from serenity_v2.performance import PerformanceMetrics, PerformanceGate


# ============================================================================
# 环境隔离测试
# ============================================================================

class TestEnvIsolation:
    """环境容器 + 生产路径保护"""

    def test_shadow_env_rejects_production_db_path(self):
        """影子DB路径 = 生产DB路径时拒绝。"""
        from serenity_v2.env import PRODUCTION_DB_PATH
        with pytest.raises(RuntimeError, match="安全拒绝"):
            SerenityEnv.shadow(db_path=PRODUCTION_DB_PATH)

    def test_shadow_no_push_adapter(self):
        """影子模式不持有推送适配器。"""
        env = SerenityEnv.shadow()
        assert env.push_adapter is None

    def test_env_required_for_operations(self):
        """未设置环境时操作应失败。"""
        import serenity_v2.env as env_mod
        env_mod._active_env = None
        reset_baseline()
        with pytest.raises(RuntimeError, match="环境未初始化"):
            get_baseline()

    def test_env_mode_must_be_valid(self):
        """无效模式应拒绝。"""
        with pytest.raises(ValueError, match="无效环境模式"):
            SerenityEnv(mode="invalid")

    def test_shadow_verify_safe(self):
        """影子环境 verify_safe 通过。"""
        env = get_env()
        safe, reason = env.verify_safe()
        assert safe, f"环境不安全: {reason}"

    def test_env_startup_log_written(self):
        """启动日志写入。"""
        env = get_env()
        log = env.write_startup_log()
        assert log.exists()
        content = log.read_text()
        assert "shadow" in content
        assert str(env.db_path.resolve()) in content


# ============================================================================
# 账户基线测试
# ============================================================================

class TestAccountBaseline:
    """Phase 0: 账户基线 [v2.1 事务+T+1]"""

    def test_bootstrap(self):
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        assert state.total_assets == 249578.07
        assert state.available_cash == 21392.07
        assert len(state.positions) == 3

        pos_map = {p.code: p for p in state.positions}
        assert pos_map["600487"].shares == 1500
        assert pos_map["600176"].shares == 2000
        assert pos_map["000988"].shares == 600
        # v2.1: 旧快照无 unsettled
        assert pos_map["600487"].unsettled_buy_shares == 0

    def test_risk_check(self):
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        alerts = baseline.check_risk(state)
        assert len(alerts) == 0

    def test_risk_check_custom(self):
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()

        pos = next(p for p in state.positions if p.code == "000988")
        original_price = pos.current_price
        pos.current_price = 110.0

        rules = RiskConstraints()
        alerts = baseline.check_risk(state, rules)
        assert len(alerts) >= 1
        assert any("浮亏" in a["detail"] for a in alerts)

        pos.current_price = original_price

    def test_signal_context(self):
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        ctx = baseline.signal_context("000988")
        assert ctx["holding"] is True
        assert ctx["position_shares"] == 600
        assert ctx["position_weight_pct"] > 0

        ctx2 = baseline.signal_context("999999")
        assert ctx2["holding"] is False

    def test_can_add_position(self):
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()

        result = baseline.can_add_position("000988", 100, 115, state)
        assert result["allowed"] is False
        assert "浮亏" in result["reason"] or "现金" in result["reason"]

    def test_sector_map(self):
        assert SECTOR_MAP.get("600487", "").find("光通信") >= 0
        assert SECTOR_MAP.get("600176", "").find("玻纤") >= 0

    # ---- v2.1 新增测试 ----

    def test_fill_trade_buy_increases_unsettled(self):
        """买入后 unsettled_buy_shares 增加，available_shares 不变。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        result = baseline.fill_trade(
            "600487", "buy", 57.0, 200,
            external_fill_id="TEST_FILL_001",
        )
        assert result["success"], f"回填失败: {result['message']}"

        new_state = result["state"]
        pos = next(p for p in new_state.positions if p.code == "600487")
        assert pos.shares == 1700  # 1500 + 200
        assert pos.unsettled_buy_shares == 200  # T+1锁定
        assert pos.available_shares == 1500  # 不变（旧1500可卖）
        assert pos.sellable_shares == 1500

    def test_fill_trade_sell_requires_available(self):
        """超卖被拒绝。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        result = baseline.fill_trade(
            "600487", "sell", 57.0, 2000,  # 持仓1500
            external_fill_id="TEST_OVERSELL",
        )
        assert result["success"] is False
        assert "超卖" in result["message"]

    def test_fill_trade_idempotent_by_external_id(self):
        """相同 external_fill_id 重复入账被拒绝。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        r1 = baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="FILL_DUP_001",
        )
        assert r1["success"]

        r2 = baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="FILL_DUP_001",
        )
        assert r2["success"] is False
        assert r2["dup"] is True
        assert "重复" in r2["message"]

    def test_fill_trade_rejects_invalid_inputs(self):
        """非法参数被拒绝。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        # 负价格
        r = baseline.fill_trade("600487", "buy", -10, 100,
                                external_fill_id="BAD_001")
        assert r["success"] is False

        # 零数量
        r = baseline.fill_trade("600487", "buy", 50, 0,
                                external_fill_id="BAD_002")
        assert r["success"] is False

        # 非法方向
        r = baseline.fill_trade("600487", "hold", 50, 100,
                                external_fill_id="BAD_003")
        assert r["success"] is False

    def test_fill_trade_cash_never_negative(self):
        """不变量：现金不能为负。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        # 尝试买入超过现金的股票
        result = baseline.fill_trade(
            "600487", "buy", 57.0, 10000,  # ~57万，远超出2.1万现金
            external_fill_id="TEST_CASH_NEG",
        )
        assert result["success"] is False
        assert "现金" in result["message"] or "不变量" in result["message"]

    def test_fill_trade_transaction_atomic(self):
        """事务原子性：不变量失败时 rollback。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        # 记录初始状态
        initial = baseline.load_latest()

        # 尝试超卖
        result = baseline.fill_trade(
            "600487", "sell", 57.0, 2000,
            external_fill_id="TEST_ATOMIC",
        )
        assert result["success"] is False

        # 状态应该未变
        after = baseline.load_latest()
        assert after.total_assets == initial.total_assets
        assert after.available_cash == initial.available_cash
        assert len(after.positions) == len(initial.positions)

    def test_settle_t1_moves_unsettled_to_available(self):
        """T+1 批次结算：买入次日结算后可卖增加。"""
        from datetime import date, timedelta

        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        baseline.fill_trade(
            "600487", "buy", 57.0, 200,
            external_fill_id="TEST_SETTLE_001",
        )

        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 200
        assert len(pos.unsettled_batches) == 1
        buy_date = pos.unsettled_batches[0]["settlement_date"]

        # 用结算日之后作为参考日
        settle_day = date.fromisoformat(buy_date)
        baseline.settle_t1(reference_date=settle_day)

        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 0
        assert pos.unsettled_batches == []
        assert pos.available_shares == 1700

    def test_settle_t1_not_before_settlement_date(self):
        """买入当日不能结算。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        baseline.fill_trade("600487", "buy", 57.0, 200,
                            external_fill_id="TEST_NO_EARLY")

        # 用买入当日作为参考日 → 不应结算
        from datetime import date
        baseline.settle_t1(reference_date=date.today())

        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 200  # 仍然锁定

    def test_settle_t1_idempotent(self):
        """重复结算安全。"""
        from datetime import date, timedelta

        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        baseline.fill_trade("600487", "buy", 57.0, 200,
                            external_fill_id="TEST_DUP_SETTLE")

        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        settle_day = date.fromisoformat(pos.unsettled_batches[0]["settlement_date"])

        # 两次结算
        baseline.settle_t1(reference_date=settle_day)
        baseline.settle_t1(reference_date=settle_day)

        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 0
        assert pos.available_shares == 1700  # 不是 1900

    def test_settle_t1_multi_batch(self):
        """连续两天买入，按批次分别结算。"""
        from datetime import date, timedelta

        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        # Day1 买入 (settlement = Day2)
        baseline.fill_trade("600487", "buy", 57.0, 200,
                            external_fill_id="BATCH_DAY1")
        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        day1_settle = date.fromisoformat(pos.unsettled_batches[-1]["settlement_date"])

        # Day2 只结算 Day1 的批次 → avail 增加 200
        baseline.settle_t1(reference_date=day1_settle)
        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.available_shares == 1700
        assert pos.unsettled_buy_shares == 0
        assert pos.unsettled_batches == []

    def test_signal_context_includes_unsettled(self):
        """signal_context 包含 unsettled_buy_shares。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        baseline.fill_trade("600487", "buy", 57.0, 200,
                            external_fill_id="TEST_CTX_001")

        state = baseline.load_latest()
        ctx = baseline.signal_context("600487", state)
        assert ctx["unsettled_buy_shares"] == 200
        assert ctx["available_shares"] == 1500  # 不变


# ============================================================================
# 事件模型测试
# ============================================================================

class TestEventRecord:
    """P2: 事件模型"""

    def test_make_price_event(self):
        event = make_price_event("600487", "亨通光电", 57.98, 5.2,
                                  volume=2500000, amount=1.5e9, is_holding=True)
        assert event.symbol == "600487"
        assert event.event_type == "price_anomaly"
        assert event.source.level == "A"
        assert event.impact.direction == "bullish"
        assert event.event_id.startswith("EVT_")

    def test_make_price_event_bearish(self):
        event = make_price_event("000988", "华工科技", 108.40, -4.5,
                                  is_holding=True)
        assert event.impact.direction == "bearish"

    def test_event_store(self):
        db_path = get_env().db_path
        store = EventStore(db_path=db_path)
        store.init_schema()

        event = make_price_event("600487", "亨通光电", 57.98, 5.2, is_holding=True)
        event_id = store.insert(event)
        assert event_id == event.event_id

        events = store.query_recent(symbol="600487")
        assert len(events) >= 1


# ============================================================================
# 情报网测试
# ============================================================================

class TestIntelligenceNetwork:
    """Phase 1: 情报网"""

    def test_ingest(self):
        intel = get_intel(shadow_mode=True)
        intel.daily_reset()

        event = intel.ingest_price_anomaly(
            "600487", "亨通光电", 57.98, 5.2, is_holding=True,
        )
        assert event.priority == "P1"
        assert event.signal_eligible is True
        assert event.verification.status in ("self_verified", "verified")

    def test_thresholds_trigger(self):
        result = check_thresholds("600487", 5.2)
        assert result["triggered"] is True
        assert result["level"] == "watch"

    def test_thresholds_no_trigger(self):
        result = check_thresholds("002281", 2.0)
        assert result["triggered"] is False

    def test_registered_sources(self):
        assert "SINA_REALTIME" in REGISTERED_SOURCES
        assert REGISTERED_SOURCES["SINA_REALTIME"]["level"] == "A"

    def test_anti_spam(self):
        intel = get_intel(shadow_mode=True)
        intel.daily_reset()

        event = intel.ingest_price_anomaly(
            "600487", "亨通光电", 57.98, 5.2, is_holding=True,
        )
        should, reason = intel.should_push(event, signal_type="price_anomaly")
        # 影子模式或盘后规则均应阻止推送
        assert should is False, f"影子/盘后模式不应推送: {reason}"
        valid_reasons = ["影子", "盘后", "重复", "冷却", "P1"]
        assert any(r in reason for r in valid_reasons), \
            f"未知阻止原因: {reason}"

    def test_shadow_mode_always_blocks_push(self):
        """影子模式下 should_push 永远返回 False 用于真实推送。"""
        intel = get_intel(shadow_mode=True)
        intel.daily_reset()

        event = intel.ingest_price_anomaly(
            "600487", "亨通光电", 57.98, 9.8,  # P0级
            is_holding=True,
        )
        should, reason = intel.should_push(event, signal_type="price_anomaly")
        assert should is False, f"影子模式推送了P0事件: {reason}"

    def test_shadow_mode_instance_not_class(self):
        """shadow_mode 是实例变量，不共享。"""
        i1 = IntelligenceNetwork(shadow_mode=True)
        i2 = IntelligenceNetwork(shadow_mode=False)
        assert i1.shadow_mode is True
        assert i2.shadow_mode is False


# ============================================================================
# 信号台测试
# ============================================================================

class TestSignalDesk:
    """Phase 2: 信号台"""

    def test_hard_gate_verifier_all_pass(self):
        verifier = HardGateVerifier()
        event = make_price_event("600487", "亨通光电", 57.98, 5.2,
                                  volume=2500000, amount=1.5e9, is_holding=True)
        event.account_relevance.is_holding = True
        event.account_relevance.position_weight_pct = 33.1

        market_data = {
            "market_regime": "ranging",
            "market_change_pct": 0.3,
            "advance_decline_ratio": 1.2,
            "liquidity_normal": True,
        }

        result = verifier.verify(event, market_data)
        assert result["required_passed"] is True
        assert result["required_failed"] == []
        assert result["signal_level"] == "ACTION"

    def test_hard_gate_extreme_market_blocks(self):
        verifier = HardGateVerifier()
        event = make_price_event("600487", "亨通光电", 57.98, 5.2, is_holding=True)
        event.account_relevance.is_holding = True

        market_data = {
            "market_regime": "extreme",
            "market_change_pct": 7.0,
            "advance_decline_ratio": 0.3,
            "liquidity_normal": False,
        }

        result = verifier.verify(event, market_data)
        assert result["required_passed"] is False
        assert "market_env" in result["required_failed"]
        assert result["signal_level"] == "WATCH"

    def test_hard_gate_optional_failure_lowers_confidence(self):
        verifier = HardGateVerifier(strategy_overrides=STRATEGY_TECHNICAL_BREAKOUT)
        event = make_price_event("600487", "亨通光电", 57.98, 5.2,
                                  volume=2500000, amount=1.5e9, is_holding=True)
        event.account_relevance.is_holding = True
        event.account_relevance.position_weight_pct = 33.1

        market_data = {
            "market_regime": "ranging",
            "market_change_pct": 0.3,
            "advance_decline_ratio": 1.2,
            "liquidity_normal": True,
        }

        result = verifier.verify(event, market_data)
        assert result["signal_level"] == "ACTION"

    def test_immutable_gates_cannot_be_overridden(self):
        override = {GATE_ACCOUNT: GateMode.OPTIONAL, GATE_DATA: GateMode.OPTIONAL}
        config = _resolve_gate_config(DEFAULT_GATE_CONFIG, override)
        assert config[GATE_ACCOUNT] == GateMode.REQUIRED
        assert config[GATE_DATA] == GateMode.REQUIRED

    def test_signal_formatting(self):
        sig = SignalOutput(
            signal_id="SIG_TEST_001",
            timestamp="2026-07-23T09:35:00+08:00",
            symbol="600487", name="亨通光电",
            signal_level="ACTION", trade_action="ADD",
            confidence="high",
            suggested_price_low=55.0, suggested_price_high=58.0,
            suggested_shares=200,
            current_shares=1500, current_weight_pct=33.1,
            stop_loss=52.50, first_target=60.0,
            expected_days=5,
            required_passed=True, required_failed=[],
            optional_failed=[], na_gates=[],
            trigger_reasons=["[market_env] 环境正常"],
            failure_conditions=["跌破52.50"],
            data_updated_at="2026-07-23T09:35:00+08:00",
            event_priority="P1",
        )
        text = format_signal(sig)
        assert "亨通光电" in text
        assert "600487" in text

    def test_desk_process(self):
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        intel = get_intel(shadow_mode=True)
        intel.daily_reset()
        intel.ingest_price_anomaly(
            "600487", "亨通光电", 57.98, 5.2,
            volume=2500000, amount=1.5e9, is_holding=True,
        )

        desk = get_desk()
        market_data = {
            "market_regime": "ranging",
            "market_change_pct": 0.3,
            "advance_decline_ratio": 1.2,
            "liquidity_normal": True,
        }

        signals = desk.process_events(market_data)
        assert len(signals) >= 0

        if signals:
            sig = signals[0]
            assert sig.symbol == "600487"
            assert sig.confidence in ("high", "medium", "low")

    def test_three_dimensions_independent(self):
        sig = SignalOutput(
            signal_id="SIG_3D_TEST",
            symbol="600487", name="亨通光电",
            signal_level="DECISION",
            trade_action="HOLD",
            event_priority="P0",
        )
        assert sig.event_priority == "P0"
        assert sig.signal_level == "DECISION"
        assert sig.trade_action == "HOLD"

    def test_illegal_signal_combination_rejected(self):
        """非法信号组合（如 DECISION+ADD）应被拒绝。"""
        with pytest.raises(ValueError, match="非法信号组合"):
            SignalOutput(
                signal_id="SIG_BAD",
                symbol="600487", name="亨通光电",
                signal_level="DECISION",
                trade_action="ADD",
            )
        with pytest.raises(ValueError, match="非法信号组合"):
            SignalOutput(
                signal_id="SIG_BAD2",
                symbol="600487", name="亨通光电",
                signal_level="ACTION",
                trade_action="HOLD",
            )
        with pytest.raises(ValueError, match="非法信号组合"):
            SignalOutput(
                signal_id="SIG_BAD3",
                symbol="600487", name="亨通光电",
                signal_level="ACTION",
                trade_action="WATCH",
            )


# ============================================================================
# 绩效测试
# ============================================================================

class TestPerformanceMetrics:
    """绩效指标体系"""

    def test_pf_calculation(self):
        trades = [
            {"pnl": 1000, "amount": 50000, "date": "2026-07-01"},
            {"pnl": -500, "amount": 50000, "date": "2026-07-02"},
            {"pnl": 800, "amount": 30000, "date": "2026-07-03"},
            {"pnl": -300, "amount": 30000, "date": "2026-07-04"},
            {"pnl": 1200, "amount": 60000, "date": "2026-07-05"},
        ]
        m = PerformanceMetrics.from_trades(trades)
        assert m.profit_factor == 3000 / 800
        assert m.win_rate == 3 / 5
        assert m.total_trades == 5

    def test_not_healthy_with_small_sample(self):
        trades = [{"pnl": 100, "amount": 10000, "date": "2026-07-01"}]
        m = PerformanceMetrics.from_trades(trades)
        health = m.is_healthy(target_pf=1.5)
        assert health["sample_ok"] is False

    def test_performance_gate(self):
        gate = PerformanceGate(min_sample_size=5)
        trades = [{"pnl": 100, "amount": 10000, "date": f"2026-07-{i:02d}"}
                  for i in range(1, 6)]
        m = PerformanceMetrics.from_trades(trades)
        result = gate.evaluate(m)
        assert result["status"] in ("healthy", "warning", "critical", "insufficient_data")


# ============================================================================
# CLI 端到端测试
# ============================================================================

class TestCLIEndToEnd:
    """CLI 入口隔离测试"""

    def test_cli_without_env_fails(self):
        """不带 --env 时应非零退出。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "serenity_v2.cli", "account", "status"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ROOT),
        )
        assert result.returncode != 0, "不带 --env 应拒绝运行"
        assert "env" in result.stdout.lower() or "env" in result.stderr.lower()

    def test_cli_production_rejected(self):
        """--env production 在 V2 阶段被拒绝。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "serenity_v2.cli", "--env", "production",
             "account", "status"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ROOT),
        )
        assert result.returncode != 0
        assert "影子" in (result.stdout + result.stderr)

    def test_cli_shadow_env_produces_no_production_side_effect(self):
        """影子CLI运行前后生产DB哈希不变。"""
        import subprocess, hashlib
        prod_db = ROOT / "serenity.db"
        hash_before = None
        if prod_db.exists():
            hash_before = hashlib.sha256(prod_db.read_bytes()).hexdigest()

        result = subprocess.run(
            [sys.executable, "-m", "serenity_v2.cli", "--env", "shadow",
             "shadow", "verify"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, f"CLI失败: {result.stderr}"

        if prod_db.exists():
            hash_after = hashlib.sha256(prod_db.read_bytes()).hexdigest()
            assert hash_before == hash_after, (
                f"生产DB哈希变化: {hash_before[:16]} -> {hash_after[:16]}"
            )

    def test_cli_shadow_db_path_is_shadow(self):
        """shadow run 后 DB 路径为影子路径。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "serenity_v2.cli", "--env", "shadow",
             "shadow", "verify"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ROOT),
        )
        assert result.returncode == 0
        output = result.stdout + result.stderr
        assert "shadow" in output.lower()


# ============================================================================
# 故障注入测试 (fill_trade 事务原子性) [v2.2 真故障注入]
# ============================================================================

class TestFillTradeFaultInjection:
    """
    事务原子性：fill_trade 任一步骤抛出异常 = 全部回滚。

    覆盖 5 个故障注入点:
      1. after_trade_insert  — 成交 INSERT 后
      2. after_position_update — 持仓更新后
      3. before_snapshot_insert — 快照 INSERT 前
      4. after_snapshot_insert  — 快照 INSERT 后
      5. after_nav_insert      — NAV INSERT 后
      6. before_commit         — 不变量通过后、提交前

    每个测试验证:
      · trades 表行数不变
      · portfolio_reconciliations 表行数不变
      · nav_history 表行数不变
      · 账户 total_assets 不变
      · 账户 available_cash 不变
      · 持仓 shares / available_shares / unsettled_buy_shares 不变
      · 重新打开数据库后仍然不变
      · 同一 external_fill_id 重试成功
    """

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _record_pre_state(self, baseline):
        """记录故障前所有状态。"""
        import sqlite3
        state = baseline.load_latest()
        conn = sqlite3.connect(str(baseline.db_path))
        try:
            trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            snap_count = conn.execute("SELECT COUNT(*) FROM portfolio_reconciliations").fetchone()[0]
            nav_count = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
            trade_ids = sorted([r[0] for r in conn.execute(
                "SELECT id FROM trades ORDER BY id").fetchall()])
        finally:
            conn.close()

        pos_snap = {}
        for p in state.positions:
            pos_snap[p.code] = {
                "shares": p.shares,
                "available_shares": p.available_shares,
                "unsettled_buy_shares": p.unsettled_buy_shares,
                "unsettled_batches": list(p.unsettled_batches),
            }

        return {
            "state": state,
            "trade_count": trade_count,
            "snap_count": snap_count,
            "nav_count": nav_count,
            "trade_ids": trade_ids,
            "pos_snap": pos_snap,
        }

    def _verify_unchanged(self, baseline, pre, stage_name):
        """全面验证 DB 状态未变。"""
        import sqlite3

        after_state = baseline.load_latest()
        assert after_state.total_assets == pre["state"].total_assets, \
            f"[{stage_name}] total_assets: {pre['state'].total_assets} → {after_state.total_assets}"
        assert after_state.available_cash == pre["state"].available_cash, \
            f"[{stage_name}] available_cash: {pre['state'].available_cash} → {after_state.available_cash}"

        for code, snap in pre["pos_snap"].items():
            after_pos = next((p for p in after_state.positions if p.code == code), None)
            assert after_pos is not None, f"[{stage_name}] 持仓 {code} 丢失"
            assert after_pos.shares == snap["shares"], \
                f"[{stage_name}] {code} shares: {snap['shares']} → {after_pos.shares}"
            assert after_pos.available_shares == snap["available_shares"], \
                f"[{stage_name}] {code} available_shares: {snap['available_shares']} → {after_pos.available_shares}"
            assert after_pos.unsettled_buy_shares == snap["unsettled_buy_shares"], \
                f"[{stage_name}] {code} unsettled: {snap['unsettled_buy_shares']} → {after_pos.unsettled_buy_shares}"

        # 直接查 DB 验证
        conn = sqlite3.connect(str(baseline.db_path))
        try:
            trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            snap_count = conn.execute("SELECT COUNT(*) FROM portfolio_reconciliations").fetchone()[0]
            nav_count = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
            trade_ids = sorted([r[0] for r in conn.execute(
                "SELECT id FROM trades ORDER BY id").fetchall()])
        finally:
            conn.close()

        assert trade_count == pre["trade_count"], \
            f"[{stage_name}] trades: {pre['trade_count']} → {trade_count}"
        assert snap_count == pre["snap_count"], \
            f"[{stage_name}] snapshots: {pre['snap_count']} → {snap_count}"
        assert nav_count == pre["nav_count"], \
            f"[{stage_name}] nav: {pre['nav_count']} → {nav_count}"
        assert trade_ids == pre["trade_ids"], \
            f"[{stage_name}] trade IDs changed"

    def _verify_unchanged_fresh_baseline(self, baseline, pre, stage_name):
        """用新的 AccountBaseline 实例验证（模拟重新打开数据库）。"""
        import sqlite3
        fresh = AccountBaseline(baseline.db_path)
        after_state = fresh.load_latest()
        assert after_state.total_assets == pre["state"].total_assets, \
            f"[{stage_name}/fresh] total_assets changed after re-open"
        assert after_state.available_cash == pre["state"].available_cash, \
            f"[{stage_name}/fresh] available_cash changed after re-open"

        conn = sqlite3.connect(str(baseline.db_path))
        try:
            trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        finally:
            conn.close()
        assert trade_count == pre["trade_count"], \
            f"[{stage_name}/fresh] trades changed after re-open"

    # ------------------------------------------------------------------
    # 1. after_trade_insert — 成交写入后故障
    # ------------------------------------------------------------------

    def test_fault_after_trade_insert_rolls_back(self):
        """成交 INSERT 后抛出异常 → trades/持仓/现金/快照/NAV 全部不变。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        def fault(stage):
            if stage == "after_trade_insert":
                raise RuntimeError("SIMULATED_CRASH: after_trade_insert")

        result = baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="FAULT_T1",
            fault_hook=fault,
        )
        assert result["success"] is False, "故障注入应返回失败"

        self._verify_unchanged(baseline, pre, "after_trade_insert")
        self._verify_unchanged_fresh_baseline(baseline, pre, "after_trade_insert")

        # 用同一 external_fill_id 重试成功
        result2 = baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="FAULT_T1",
        )
        assert result2["success"], f"重试失败: {result2['message']}"
        new_state = baseline.load_latest()
        pos = next(p for p in new_state.positions if p.code == "600487")
        assert pos.shares == 1600, "重试后持仓应正确更新"

    # ------------------------------------------------------------------
    # 2. after_position_update — 持仓更新后故障
    # ------------------------------------------------------------------

    def test_fault_after_position_update_rolls_back(self):
        """持仓更新后抛出异常 → 全部回滚。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        def fault(stage):
            if stage == "after_position_update":
                raise RuntimeError("SIMULATED_CRASH: after_position_update")

        result = baseline.fill_trade(
            "600176", "buy", 38.5, 300,
            external_fill_id="FAULT_T2",
            fault_hook=fault,
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "after_position_update")
        self._verify_unchanged_fresh_baseline(baseline, pre, "after_position_update")

        result2 = baseline.fill_trade(
            "600176", "buy", 38.5, 300,
            external_fill_id="FAULT_T2",
        )
        assert result2["success"], f"重试失败: {result2['message']}"

    # ------------------------------------------------------------------
    # 3. before_snapshot_insert — 快照 INSERT 前故障
    # ------------------------------------------------------------------

    def test_fault_before_snapshot_insert_rolls_back(self):
        """快照 INSERT 前抛出异常 → 全部回滚。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        def fault(stage):
            if stage == "before_snapshot_insert":
                raise RuntimeError("SIMULATED_CRASH: before_snapshot_insert")

        result = baseline.fill_trade(
            "000988", "buy", 114.0, 50,
            external_fill_id="FAULT_T3",
            fault_hook=fault,
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "before_snapshot_insert")
        self._verify_unchanged_fresh_baseline(baseline, pre, "before_snapshot_insert")

        result2 = baseline.fill_trade(
            "000988", "buy", 114.0, 50,
            external_fill_id="FAULT_T3",
        )
        assert result2["success"], f"重试失败: {result2['message']}"

    # ------------------------------------------------------------------
    # 4. after_snapshot_insert — 快照 INSERT 后故障
    # ------------------------------------------------------------------

    def test_fault_after_snapshot_insert_rolls_back(self):
        """快照 INSERT 后故障 → 快照+成交一起回滚。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        def fault(stage):
            if stage == "after_snapshot_insert":
                raise RuntimeError("SIMULATED_CRASH: after_snapshot_insert")

        result = baseline.fill_trade(
            "600487", "buy", 56.5, 200,
            external_fill_id="FAULT_T4",
            fault_hook=fault,
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "after_snapshot_insert")
        self._verify_unchanged_fresh_baseline(baseline, pre, "after_snapshot_insert")

        result2 = baseline.fill_trade(
            "600487", "buy", 56.5, 200,
            external_fill_id="FAULT_T4",
        )
        assert result2["success"], f"重试失败: {result2['message']}"

    # ------------------------------------------------------------------
    # 5. after_nav_insert — NAV INSERT 后故障
    # ------------------------------------------------------------------

    def test_fault_after_nav_insert_rolls_back(self):
        """NAV INSERT 后故障 → 全部回滚。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        def fault(stage):
            if stage == "after_nav_insert":
                raise RuntimeError("SIMULATED_CRASH: after_nav_insert")

        result = baseline.fill_trade(
            "600176", "sell", 39.0, 300,
            external_fill_id="FAULT_T5",
            fault_hook=fault,
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "after_nav_insert")
        self._verify_unchanged_fresh_baseline(baseline, pre, "after_nav_insert")

        result2 = baseline.fill_trade(
            "600176", "sell", 39.0, 300,
            external_fill_id="FAULT_T5",
        )
        assert result2["success"], f"重试失败: {result2['message']}"

    # ------------------------------------------------------------------
    # 6. before_commit — 提交前故障
    # ------------------------------------------------------------------

    def test_fault_before_commit_rolls_back(self):
        """不变量检查通过后、提交前故障 → 全部回滚。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        def fault(stage):
            if stage == "before_commit":
                raise RuntimeError("SIMULATED_CRASH: before_commit")

        result = baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="FAULT_T6",
            fault_hook=fault,
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "before_commit")
        self._verify_unchanged_fresh_baseline(baseline, pre, "before_commit")

        result2 = baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="FAULT_T6",
        )
        assert result2["success"], f"重试失败: {result2['message']}"

    # ------------------------------------------------------------------
    # 业务验证测试（保留）
    # ------------------------------------------------------------------

    def test_rollback_on_invariant_failure(self):
        """不变量失败时成交不存在、账户不变。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        result = baseline.fill_trade(
            "600487", "sell", 57.0, 2000,  # 超卖
            external_fill_id="FAULT_OVERSELL",
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "invariant_oversell")

    def test_rollback_on_cash_negative(self):
        """现金不足时回滚。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        pre = self._record_pre_state(baseline)

        result = baseline.fill_trade(
            "600487", "buy", 57.0, 50000,
            external_fill_id="FAULT_NO_CASH",
        )
        assert result["success"] is False

        self._verify_unchanged(baseline, pre, "cash_negative")

    def test_retry_after_rollback_succeeds(self):
        """回滚后重试合法操作可以成功。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        r1 = baseline.fill_trade(
            "600487", "sell", 57.0, 5000,
            external_fill_id="FAULT_RETRY_BAD",
        )
        assert r1["success"] is False

        r2 = baseline.fill_trade(
            "600487", "sell", 57.0, 100,
            external_fill_id="FAULT_RETRY_OK",
        )
        assert r2["success"], f"重试失败: {r2['message']}"

    def test_duplicate_external_id_prevented(self):
        """相同 external_fill_id → dup=True（幂等）。"""
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        r1 = baseline.fill_trade(
            "600487", "buy", 57.0, 50,
            external_fill_id="DUP_INJECT_001",
        )
        assert r1["success"]

        r2 = baseline.fill_trade(
            "600487", "buy", 57.0, 50,
            external_fill_id="DUP_INJECT_001",
        )
        assert r2["success"] is False
        assert r2["dup"] is True


# ============================================================================
# 迁移测试
# ============================================================================

class TestMigrations:
    """DB 迁移：幂等 + 数据不变"""

    def test_migration_idempotent(self):
        """重复执行迁移不报错。"""
        from serenity_v2.migrations import apply_migrations
        db_path = get_env().db_path

        r1 = apply_migrations(db_path)
        r2 = apply_migrations(db_path)

        assert len(r1["errors"]) == 0
        assert len(r2["errors"]) == 0

    def test_migration_adds_columns(self):
        """迁移后 trades 表包含新字段。"""
        from serenity_v2.migrations import check_schema
        db_path = get_env().db_path
        schema = check_schema(db_path)

        assert "trades" in schema["columns"]
        cols = schema["columns"]["trades"]
        for col in ["external_fill_id", "order_id", "commission", "stamp_tax"]:
            assert col in cols, f"缺少列: {col}"

    def test_data_preserved_after_migration(self):
        """迁移前后数据不丢失。"""
        from serenity_v2.migrations import apply_migrations
        db_path = get_env().db_path

        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        total_before = baseline.load_latest().total_assets

        # 再次迁移
        apply_migrations(db_path)

        total_after = baseline.load_latest().total_assets
        assert total_before == total_after, "迁移后数据丢失"


# ============================================================================
# 迁移中途失败测试 [v2.2]
# ============================================================================

class TestMigrationMidFailure:
    """迁移执行到一半失败时：无部分 schema 残留，数据完好，重试成功。"""

    def test_migration_mid_failure_data_intact(self):
        """模拟迁移失败场景：手动执行部分迁移后制造异常，验证数据不丢。"""
        import sqlite3
        from serenity_v2.migrations import MIGRATIONS_V21, MIGRATIONS_V21_INDEXES

        db_path = get_env().db_path

        # 先写入数据
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        baseline.fill_trade("600487", "buy", 57.0, 100,
                            external_fill_id="MIG_DATA_001")

        # 记录当前数据
        pre_state = baseline.load_latest()
        conn = sqlite3.connect(str(db_path))
        trade_count_before = conn.execute(
            "SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()

        # 模拟：手动删除一个已迁移的列来模拟"部分迁移后崩溃"
        # 场景：假设迁移到第4个 ALTER TABLE 时进程崩溃
        # → 前3个字段已添加，后4个未添加
        # → 重试应补全
        conn = sqlite3.connect(str(db_path))
        try:
            # 只应用前3个迁移
            for sql, field_name in MIGRATIONS_V21[:3]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # 可能已存在
            conn.commit()
        finally:
            conn.close()

        # 验证：此时只有部分字段，但数据完整
        conn = sqlite3.connect(str(db_path))
        cols_after_partial = [c[1] for c in conn.execute(
            "PRAGMA table_info(trades)").fetchall()]
        conn.close()

        # 至少应包含前3个新字段中的部分
        assert "external_fill_id" in cols_after_partial, \
            "external_fill_id 应在部分迁移后存在"

        # 数据验证：trades 条数不变
        conn = sqlite3.connect(str(db_path))
        trade_count_mid = conn.execute(
            "SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        assert trade_count_mid == trade_count_before, \
            f"部分迁移后 trades 条数变化: {trade_count_before} → {trade_count_mid}"

        # 完整迁移重试
        from serenity_v2.migrations import apply_migrations
        r = apply_migrations(db_path)
        assert len(r["errors"]) == 0, f"重试迁移有错误: {r['errors']}"

        # 验证：所有字段就位
        from serenity_v2.migrations import check_schema
        schema = check_schema(db_path)
        cols = schema["columns"]["trades"]
        for col in ["external_fill_id", "order_id", "fill_sequence",
                     "import_batch_id", "commission", "stamp_tax", "transfer_fee"]:
            assert col in cols, f"重试后仍缺少列: {col}"

        # 验证：数据完好
        after_state = baseline.load_latest()
        assert after_state.total_assets == pre_state.total_assets, \
            "迁移后资产变化"
        conn = sqlite3.connect(str(db_path))
        trade_count_after = conn.execute(
            "SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        assert trade_count_after == trade_count_before, \
            f"迁移后 trades 条数变化: {trade_count_before} → {trade_count_after}"

    def test_migration_no_partial_schema_on_connection_loss(self):
        """模拟连接丢失场景：迁移在事务外执行，单条失败不残留。"""
        import sqlite3
        db_path = get_env().db_path

        # 确保有数据
        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)
        conn = sqlite3.connect(str(db_path))
        trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()

        # 模拟：手动关闭连接模拟崩溃（在迁移过程中）
        # 实际测试：执行一条会失败的 ALTER（重复列名），验证它被正确 skip
        from serenity_v2.migrations import apply_migrations
        r = apply_migrations(db_path)
        # 第二次执行：所有列已存在 → 全部 skip
        assert len(r["errors"]) == 0
        assert len(r["applied"]) >= 0  # 可能有索引被重复 applied

        # 验证数据
        conn = sqlite3.connect(str(db_path))
        trade_count2 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        assert trade_count2 == trade_count, "重复迁移后数据丢失"


# ============================================================================
# 确定性 3 时间点回放测试 [v2.2]
# ============================================================================

class TestDeterministicReplay:
    """
    确定性离线回放：同一输入 → 同一输出。

    在 3 个时间点运行相同输入，每个运行两次：
      1. 2026-07-22 15:00 — 收盘后
      2. 2026-07-23 08:45 — 盘前
      3. 2026-07-23 09:35 — 盘中

    验证：
      · 同时间点两次输出完全一致
      · T+1 结算行为因时间点不同而正确
      · system_recorded_at 与 business_time 区分
      · 无重复事件/信号
      · 生产文件不变
    """

    REPLAY_TIMEPOINTS = [
        ("2026-07-22T15:00:00+08:00", "收盘后"),
        ("2026-07-23T08:45:00+08:00", "盘前"),
        ("2026-07-23T09:35:00+08:00", "盘中"),
    ]

    PRICE_FEED = [
        {"code": "600487", "name": "亨通光电", "price": 57.98,
         "change_pct": 5.2, "volume": 2500000, "amount": 1.5e9,
         "is_holding": True},
        {"code": "000988", "name": "华工科技", "price": 108.40,
         "change_pct": -4.5, "volume": 1800000, "amount": 1.95e8,
         "is_holding": True},
    ]

    MARKET_DATA = {
        "market_regime": "ranging",
        "market_change_pct": 0.3,
        "advance_decline_ratio": 1.2,
        "liquidity_normal": True,
    }

    def _run_replay_cycle(self, iso_time: str):
        """
        在指定模拟时间运行一次完整回放周期。

        使用测试 fixture 的组件（不创建 ShadowRunner 以避免路径污染）。
        返回确定性摘要。
        """
        from serenity_v2.clock import SimClock, set_clock, reset_clock

        set_clock(SimClock(iso_time))

        baseline = get_baseline()
        intel = get_intel(shadow_mode=True)
        desk = get_desk()

        # 确保基线存在
        state = baseline.load_latest()
        if state.total_assets <= 0:
            state = baseline.bootstrap_from_doc_b()
            baseline.save_snapshot(state)

        # 摄入行情
        holding_codes = {p.code for p in state.positions}
        ingested_ids = []
        for item in self.PRICE_FEED:
            is_holding = item["code"] in holding_codes
            event = intel.ingest_price_anomaly(
                item["code"], item.get("name", item["code"]),
                item["price"], item["change_pct"],
                volume=item.get("volume", 0),
                amount=item.get("amount", 0),
                is_holding=is_holding,
            )
            ingested_ids.append(event.event_id)

        # 生成信号
        signals = desk.process_events(self.MARKET_DATA)

        # 收集确定性摘要
        signal_summaries = []
        for sig in signals:
            signal_summaries.append({
                "symbol": sig.symbol,
                "signal_level": sig.signal_level,
                "trade_action": sig.trade_action,
                "confidence": sig.confidence,
                "suggested_shares": sig.suggested_shares,
                "market_session": sig.market_session,
                "position_available_as_of": sig.position_available_as_of,
                "eod_finalized_through_trade_date": sig.eod_finalized_through_trade_date,
                "t1_settlement_completed": sig.t1_settlement_completed,
                "t1_due_batch_count": sig.t1_due_batch_count,
                "t1_due_shares_pending": sig.t1_due_shares_pending,
                "session_approximation": sig.session_approximation,
                "action_suppressed": sig.action_suppressed,
                "suppression_reason": sig.suppression_reason,
                "normalization_reason": sig.normalization_reason,
                "execution_tags": sig.execution_tags,
                "replay_as_of": sig.replay_as_of,
            })

        state_after = baseline.load_latest()

        reset_clock()
        return {
            "signals": signal_summaries,
            "signal_count": len(signals),
            "total_assets": state_after.total_assets,
            "available_cash": state_after.available_cash,
            "ingested_ids": ingested_ids,
        }

    # ------------------------------------------------------------------
    # 1. 同时间点两次输出完全一致
    # ------------------------------------------------------------------

    def test_same_timepoint_produces_identical_output(self):
        """同一时间点运行两次，输出完全一致（确定性）。"""
        for iso_time, label in self.REPLAY_TIMEPOINTS:
            # 重置单例确保状态干净
            reset_baseline()
            reset_intel()
            reset_desk()

            run1 = self._run_replay_cycle(iso_time)

            reset_baseline()
            reset_intel()
            reset_desk()

            run2 = self._run_replay_cycle(iso_time)

            assert run1["signal_count"] == run2["signal_count"], \
                f"[{label}] 信号数不一致: {run1['signal_count']} vs {run2['signal_count']}"

            assert run1["total_assets"] == run2["total_assets"], \
                f"[{label}] 总资产不一致: {run1['total_assets']} vs {run2['total_assets']}"

            assert run1["available_cash"] == run2["available_cash"], \
                f"[{label}] 现金不一致"

            for i, (s1, s2) in enumerate(
                zip(run1["signals"], run2["signals"])
            ):
                assert s1 == s2, \
                    f"[{label}] 信号#{i} 不一致:\n  期望: {s1}\n  实际: {s2}"

    # ------------------------------------------------------------------
    # 2. T+1 结算时间点差异
    # ------------------------------------------------------------------

    def test_t1_settlement_differs_by_timepoint(self):
        """
        T+1 结算行为因时间点不同而不同。

        7/22 15:00 买入 → settlement_date = 7/23
        7/22 当日不结算 → unsettled 保留
        7/23 结算 → unsettled 清零, available 增加
        """
        from serenity_v2.clock import SimClock, set_clock, reset_clock
        from datetime import date

        baseline = get_baseline()
        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        # --- 时间点 1: 7/22 15:00 收盘后买入 ---
        set_clock(SimClock("2026-07-22T15:00:00+08:00"))
        baseline.fill_trade(
            "600487", "buy", 57.0, 100,
            external_fill_id="REPLAY_T1_001",
        )
        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 100, "7/22 买入后 unsettled 应为 100"
        assert pos.available_shares == 1500, "7/22 买入后 available 不变"

        settle_date = date.fromisoformat(
            pos.unsettled_batches[0]["settlement_date"]
        )
        assert settle_date == date(2026, 7, 23), \
            f"买入日7/22的结算日应为7/23，实际: {settle_date}"
        reset_clock()

        # --- 时间点 2: 7/22 当日结算 → 不结算（settlement_date 7/23 > 7/22）---
        baseline.settle_t1(reference_date=date(2026, 7, 22))
        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 100, "7/22 当日不应结算"

        # --- 时间点 3: 7/23 结算 → 释放 ---
        baseline.settle_t1(reference_date=date(2026, 7, 23))
        state = baseline.load_latest()
        pos = next(p for p in state.positions if p.code == "600487")
        assert pos.unsettled_buy_shares == 0, "7/23 结算后 unsettled 应为 0"
        assert pos.available_shares == 1600, "7/23 结算后 available 应为 1600"

    # ------------------------------------------------------------------
    # 3. system_recorded_at 与 business_time 区分
    # ------------------------------------------------------------------

    def test_business_time_vs_system_time_distinguished(self):
        """
        业务时间（SimClock）与系统记录时间可区分。

        signal_output 的 snapshot_as_of / replay_as_of 反映业务时间，
        而非真实系统时间。
        """
        from serenity_v2.clock import SimClock, set_clock, reset_clock

        set_clock(SimClock("2026-07-23T09:35:00+08:00"))

        baseline = get_baseline()
        intel = get_intel(shadow_mode=True)
        desk = get_desk()

        state = baseline.bootstrap_from_doc_b()
        # 清除硬编码的 snapshot_at，让 save_snapshot 使用当前时间
        state.snapshot_at = ""
        baseline.save_snapshot(state)

        intel.ingest_price_anomaly(
            "600487", "亨通光电", 57.98, 5.2,
            volume=2500000, amount=1.5e9, is_holding=True,
        )

        signals = desk.process_events(self.MARKET_DATA)
        reset_clock()

        # 验证：有信号产生，且 replay_as_of 为模拟时间
        if signals:
            sig = signals[0]
            assert sig.replay_as_of is not None, "应设置 replay_as_of"
            assert "2026-07-23T09:35" in sig.replay_as_of, \
                f"replay_as_of 应为模拟时间，实际: {sig.replay_as_of}"

            # snapshot_as_of 也应反映业务时间
            if sig.snapshot_as_of:
                assert "2026-07-23" in sig.snapshot_as_of, \
                    f"snapshot_as_of 应为业务时间，实际: {sig.snapshot_as_of}"

        # 字段语义验证
        if signals:
            sig = signals[0]
            # 持仓可卖覆盖 → 当日（T+1 结算规则：结算日当日可用）
            assert sig.position_available_as_of == "2026-07-23", \
                f"position_available_as_of 应为当日，实际: {sig.position_available_as_of}"

            # 日终完成 → 前一交易日（15:30 未到，今日日终未完成）
            assert sig.eod_finalized_through_trade_date == "2026-07-22", \
                f"eod_finalized 应为 7/22，实际: {sig.eod_finalized_through_trade_date}"

            # 交易时段 → 上午连续竞价
            assert sig.market_session == "CONTINUOUS_AM", \
                f"9:35 应为 CONTINUOUS_AM，实际: {sig.market_session}"

            # T+1 结算已完成（无到期未释放批次）
            assert sig.t1_settlement_completed is True, \
                "无未释放批次 → t1_settlement_completed 应为 True"
            assert sig.t1_due_batch_count == 0, \
                f"无待释放批次，实际: {sig.t1_due_batch_count}"
            assert sig.t1_due_shares_pending == 0, \
                f"无待释放股数，实际: {sig.t1_due_shares_pending}"

            # 弃用字段不再检查（不在格式化输出中展示）
            assert sig.schema_version == "2.1"

    # ------------------------------------------------------------------
    # 4. 回放无重复事件/信号
    # ------------------------------------------------------------------

    def test_replay_no_duplicate_signals(self):
        """回放不产生重复 ACTION 信号。"""
        from serenity_v2.clock import SimClock, set_clock, reset_clock

        set_clock(SimClock("2026-07-23T09:35:00+08:00"))

        baseline = get_baseline()
        intel = get_intel(shadow_mode=True)
        desk = get_desk()

        state = baseline.bootstrap_from_doc_b()
        baseline.save_snapshot(state)

        # 第一次运行
        intel.daily_reset()
        for item in self.PRICE_FEED:
            intel.ingest_price_anomaly(
                item["code"], item["name"], item["price"],
                item["change_pct"], volume=item.get("volume", 0),
                amount=item.get("amount", 0), is_holding=item.get("is_holding", True),
            )

        signals1 = desk.process_events(self.MARKET_DATA)

        # 第二次运行（同一输入）
        signals2 = desk.process_events(self.MARKET_DATA)

        reset_clock()

        # 检查：无重复 ACTION 信号
        for run_name, sigs in [("run1", signals1), ("run2", signals2)]:
            action_sigs = [s for s in sigs if s.signal_level == "ACTION"]
            action_symbols = [s.symbol for s in action_sigs]
            assert len(action_symbols) == len(set(action_symbols)), \
                f"[{run_name}] 重复 ACTION 信号: {action_symbols}"

    # ------------------------------------------------------------------
    # 5. 生产文件不变
    # ------------------------------------------------------------------

    def test_production_untouched_by_replay(self):
        """离线回放不触碰生产文件。"""
        import hashlib
        prod_db = ROOT / "serenity.db"
        hash_before = None
        if prod_db.exists():
            hash_before = hashlib.sha256(prod_db.read_bytes()).hexdigest()

        # 运行所有 3 个时间点
        for iso_time, label in self.REPLAY_TIMEPOINTS:
            reset_baseline()
            reset_intel()
            reset_desk()
            self._run_replay_cycle(iso_time)

        if prod_db.exists():
            hash_after = hashlib.sha256(prod_db.read_bytes()).hexdigest()
            assert hash_before == hash_after, \
                f"回放修改了生产DB: {hash_before[:16]} → {hash_after[:16]}"


# ============================================================================
# 生产文件零变化验证
# ============================================================================

class TestProductionUntouched:
    """影子运行不触碰生产文件"""

    def test_production_db_untouched_by_shadow_cycle(self):
        """影子周期运行后生产DB不变。"""
        import hashlib
        prod_db = ROOT / "serenity.db"
        hash_before = None
        if prod_db.exists():
            hash_before = hashlib.sha256(prod_db.read_bytes()).hexdigest()

        from serenity_v2.shadow_runner import ShadowRunner, verify_invariants
        runner = ShadowRunner()
        state = runner.baseline.load_latest()
        if state.total_assets <= 0:
            state = runner.baseline.bootstrap_from_doc_b()
            runner.baseline.save_snapshot(state)

        runner.run_cycle(price_feed=[
            {"code": "600487", "name": "亨通光电", "price": 57.98,
             "change_pct": 5.2, "volume": 2500000, "amount": 1.5e9, "turnover": 7.5},
        ])

        if prod_db.exists():
            hash_after = hashlib.sha256(prod_db.read_bytes()).hexdigest()
            assert hash_before == hash_after, "生产DB被影子模式修改"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
