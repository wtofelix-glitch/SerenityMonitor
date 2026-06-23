"""测试 reflection_engine — 反思学习环（Mock DB + IC 计算）"""

import sys
sys.path.insert(0, '/Users/mac/workspace/SerenityMonitor')

import json
import sqlite3
from datetime import date as RealDate
from unittest.mock import ANY

import reflection_engine
from reflection_engine import (
    compute_dimension_ic, generate_reflection, generate_all_reflections,
    fill_outcomes, persist_dimension_ic, suggest_weight_adjustments,
    show_reflections, show_dimension_ic, apply_reflection_adjustments,
    DIMENSION_KEYS,
)


# ── FakeDate for deterministic IC tests ──────────────────

class FakeDate(RealDate):
    @classmethod
    def today(cls):
        return cls(2026, 6, 8)


# ── 辅助函数 ─────────────────────────────────────────────

def _make_score_dict(code: str, dim_values: list[float],
                     change_pct: float = 0) -> dict:
    """构造 scoring_history 行 dict（包含 details JSON 字符串）"""
    d = {"code": code}
    for k, v in zip(DIMENSION_KEYS, dim_values):
        d[k] = v
    d["details"] = json.dumps({"change_pct": change_pct})
    d["total_score"] = sum(dim_values) / len(dim_values)
    return d


def _perfect_positive_scores() -> dict:
    """返回 4 只标的评分数据，dim 值与 change_pct 正序对齐 → spearmanr=1.0"""
    return {
        "A": _make_score_dict("A", [90, 85, 80, 75, 70, 65, 60, 55], 5.0),
        "B": _make_score_dict("B", [80, 75, 70, 65, 60, 55, 50, 45], 3.0),
        "C": _make_score_dict("C", [70, 65, 60, 55, 50, 45, 40, 35], 1.0),
        "D": _make_score_dict("D", [60, 55, 50, 45, 40, 35, 30, 25], -1.0),
    }


def _perfect_negative_scores() -> dict:
    """dim 值与 change_pkt 反序对齐 → spearmanr = -1.0"""
    return {
        "A": _make_score_dict("A", [90, 85, 80, 75, 70, 65, 60, 55], -1.0),
        "B": _make_score_dict("B", [80, 75, 70, 65, 60, 55, 50, 45], 1.0),
        "C": _make_score_dict("C", [70, 65, 60, 55, 50, 45, 40, 35], 3.0),
        "D": _make_score_dict("D", [60, 55, 50, 45, 40, 35, 30, 25], 5.0),
    }


# ── Dimension IC ─────────────────────────────────────────

class TestComputeDimensionIC:
    """compute_dimension_ic — Rank IC 核心计算"""

    def test_positive_ic(self, monkeypatch):
        """正相关 → IC ≈ 1.0"""
        monkeypatch.setattr(reflection_engine, 'date', FakeDate)

        scores = {"2026-06-07": _perfect_positive_scores(),
                  "2026-06-08": _perfect_positive_scores()}
        monkeypatch.setattr(reflection_engine, '_get_scores_on_date',
                            lambda d: scores.get(d, {}))

        result = compute_dimension_ic(days=1)
        for dim in DIMENSION_KEYS:
            assert result[dim] == 1.0, f"{dim} expected 1.0, got {result[dim]}"

    def test_negative_ic(self, monkeypatch):
        """负相关 → IC ≈ -1.0"""
        monkeypatch.setattr(reflection_engine, 'date', FakeDate)

        today_scores = _perfect_positive_scores()
        tomorrow_scores = _perfect_negative_scores()

        scores = {"2026-06-07": today_scores,
                  "2026-06-08": tomorrow_scores}
        monkeypatch.setattr(reflection_engine, '_get_scores_on_date',
                            lambda d: scores.get(d, {}))

        result = compute_dimension_ic(days=1)
        for dim in DIMENSION_KEYS:
            assert result[dim] == -1.0, f"{dim} expected -1.0, got {result[dim]}"

    def test_insufficient_pairs(self, monkeypatch):
        """少于 3 个有效配对 → 对应维度 IC = 0.0"""
        monkeypatch.setattr(reflection_engine, 'date', FakeDate)
        # Only 2 codes → len(pairs) < 3 → skip
        two_scores = {
            "A": _make_score_dict("A", [90, 85, 80, 75, 70, 65, 60, 55], 5.0),
            "B": _make_score_dict("B", [80, 75, 70, 65, 60, 55, 50, 45], 3.0),
        }
        scores = {"2026-06-07": two_scores,
                  "2026-06-08": two_scores}
        monkeypatch.setattr(reflection_engine, '_get_scores_on_date',
                            lambda d: scores.get(d, {}))
        result = compute_dimension_ic(days=1)
        for dim in DIMENSION_KEYS:
            assert result[dim] == 0.0, f"{dim} expected 0.0, got {result[dim]}"

    def test_constant_values_skipped(self, monkeypatch):
        """所有 dim 值相同 → 跳过（spearmanr 无法计算）"""
        monkeypatch.setattr(reflection_engine, 'date', FakeDate)
        const = _make_score_dict("A", [50]*8, 1.0)
        scores = {
            "A": const,
            "B": _make_score_dict("B", [50]*8, 2.0),
            "C": _make_score_dict("C", [50]*8, 3.0),
            "D": _make_score_dict("D", [50]*8, 4.0),
        }
        data = {"2026-06-07": scores, "2026-06-08": scores}
        monkeypatch.setattr(reflection_engine, '_get_scores_on_date',
                            lambda d: data.get(d, {}))
        result = compute_dimension_ic(days=1)
        for dim in DIMENSION_KEYS:
            assert result[dim] == 0.0, f"{dim} expected 0.0, got {result[dim]}"

    def test_no_data_returns_zero(self, monkeypatch):
        """无数据返回全 0"""
        monkeypatch.setattr(reflection_engine, 'date', FakeDate)
        monkeypatch.setattr(reflection_engine, '_get_scores_on_date',
                            lambda d: {})
        result = compute_dimension_ic(days=1)
        assert isinstance(result, dict)
        for dim in DIMENSION_KEYS:
            assert result[dim] == 0.0

    def test_as_of_date_uses_historical_window(self, monkeypatch):
        """指定 as_of 时使用该日期之前的窗口，而不是系统当天"""
        calls = []

        def mock_scores(day):
            calls.append(day)
            return {}

        monkeypatch.setattr(reflection_engine, '_get_scores_on_date', mock_scores)
        compute_dimension_ic(days=2, as_of="2026-06-10")
        assert calls == [
            "2026-06-09", "2026-06-10",
            "2026-06-08", "2026-06-09",
        ]


# ── Generate Reflection ──────────────────────────────────

class TestGenerateReflection:
    def test_success(self, monkeypatch):
        """正常生成反思报告"""
        monkeypatch.setattr(reflection_engine, '_get_latest_scores', lambda: [
            {"code": "002281", "total_score": 75,
             "base_score": 80, "zone_score": 70,
             "momentum_score": 75, "volume_score": 60,
             "serenity_score": 80, "factor_score": 65,
             "technical_score": 70, "sentiment_score": 55},
        ])
        monkeypatch.setattr(reflection_engine, 'get_reflections',
                            lambda code, days=3: [
                                {"actual_return_1d": 1.5},
                                {"actual_return_1d": -0.5},
                                {"actual_return_1d": 2.0},
                            ])
        monkeypatch.setattr(reflection_engine, 'compute_dimension_ic',
                            lambda days=10: {
                                "base_score": 0.12, "zone_score": -0.08,
                                "momentum_score": 0.05, "volume_score": 0.02,
                                "serenity_score": -0.15, "factor_score": 0.10,
                                "technical_score": 0.03, "sentiment_score": -0.06,
                            })

        result = generate_reflection("002281")
        assert "error" not in result
        assert result["code"] == "002281"
        assert result["total_score"] == 75
        assert result["dimension_ic"].get("base_score") == 0.12
        # dimension_ic 保存完整 IC，effective_dimension_ic 仅保留有效维度
        assert result["dimension_ic"].get("volume_score") == 0.02
        assert "volume_score" not in result["effective_dimension_ic"]
        assert "reflection_text" in result
        assert "002281" in result["reflection_text"]

    def test_no_scores(self, monkeypatch):
        """无评分数据返回 error"""
        monkeypatch.setattr(reflection_engine, '_get_latest_scores', lambda: [])
        result = generate_reflection("999999")
        assert "error" in result
        assert result["error"] == "无评分数据"


class TestGenerateAllReflections:
    def test_basic(self, monkeypatch):
        """对所有标的生成反思"""
        monkeypatch.setattr(reflection_engine, 'generate_reflection',
                            lambda code: {
                                "code": code,
                                "name": code,
                                "total_score": 70,
                                "dimension_scores": {},
                                "dimension_ic": {},
                                "reflection_text": f"Reflection for {code}",
                            })
        monkeypatch.setattr(reflection_engine, 'save_reflection',
                            lambda c, r: None)
        # ALL_CODES from config has 14 stocks
        results = generate_all_reflections()
        assert len(results) == len(reflection_engine.ALL_CODES)
        assert results[0]["code"] == reflection_engine.ALL_CODES[0]

    def test_exception_handling(self, monkeypatch):
        """某只标的异常不阻断其他标的"""
        call_count = [0]

        def mock_ref(code):
            call_count[0] += 1
            if code == "000988":
                raise RuntimeError("simulated failure")
            return {"code": code, "name": code, "total_score": 70,
                    "dimension_scores": {}, "dimension_ic": {},
                    "reflection_text": ""}

        monkeypatch.setattr(reflection_engine, 'generate_reflection', mock_ref)
        monkeypatch.setattr(reflection_engine, 'save_reflection',
                            lambda c, r: None)
        results = generate_all_reflections()
        # 000988 will be skipped due to exception, count should be total - 1
        assert len(results) == len(reflection_engine.ALL_CODES) - 1
        # generate_reflection was still called for all
        assert call_count[0] == len(reflection_engine.ALL_CODES)


class TestSaveReflection:
    def test_save_reflection_persists_dimension_ic(self, monkeypatch, tmp_path):
        """DB 保存反思时同步持久化维度 IC"""
        import db

        db_path = tmp_path / "serenity_test.db"

        def get_test_conn():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        conn = get_test_conn()
        conn.execute("""
            CREATE TABLE score_reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                total_score REAL DEFAULT 0,
                dimension_scores TEXT DEFAULT '{}',
                predicted_direction TEXT DEFAULT '',
                actual_return_1d REAL DEFAULT NULL,
                actual_return_3d REAL DEFAULT NULL,
                actual_return_5d REAL DEFAULT NULL,
                dimension_ic TEXT DEFAULT '{}',
                reflection_text TEXT DEFAULT '',
                UNIQUE(code, date)
            )
        """)
        conn.commit()
        conn.close()
        monkeypatch.setattr(db, 'get_conn', get_test_conn)

        db.save_reflection("002281", {
            "date": "2026-06-05",
            "total_score": 75,
            "dimension_scores": {"base_score": 80},
            "predicted_direction": "BUY",
            "dimension_ic": {"base_score": 0.12},
            "reflection_text": "ok",
        })

        conn = get_test_conn()
        row = conn.execute(
            "SELECT dimension_ic FROM score_reflections WHERE code=? AND date=?",
            ("002281", "2026-06-05"),
        ).fetchone()
        conn.close()
        assert json.loads(row["dimension_ic"]) == {"base_score": 0.12}


# ── Fill Outcomes ────────────────────────────────────────

class TestFillOutcomes:
    def test_basic_fill(self, monkeypatch):
        """补填实际收益"""
        monkeypatch.setattr(reflection_engine, 'get_unfilled_reflections',
                            lambda since_days=10: [
                                {"id": 1, "code": "002281", "date": "2026-06-05"},
                                {"id": 2, "code": "000988", "date": "2026-06-05"},
                            ])

        def mock_price(code, days=60):
            return [
                {"date": "2026-06-05", "close": 100.0},
                {"date": "2026-06-08", "close": 103.0},
                {"date": "2026-06-09", "close": 105.0},
                {"date": "2026-06-10", "close": 110.0},
            ]
        monkeypatch.setattr(reflection_engine, 'get_price_history', mock_price)

        updated = {}
        def mock_update(code, date_str, **kwargs):
            updated[(code, date_str)] = kwargs

        monkeypatch.setattr(reflection_engine, 'update_reflection_outcome',
                            mock_update)

        fill_outcomes(days_back=10)
        # prices sorted: [06-05=100, 06-08=103, 06-09=105, 06-10=110]
        # ref_idx for "2026-06-05" = 0, ref_price = 100.0
        # day+1=06-08=103 → 3.0%
        # day+3=06-10=110 → 10.0%
        key1 = ("002281", "2026-06-05")
        key2 = ("000988", "2026-06-05")
        assert key1 in updated
        assert key2 in updated
        assert updated[key1]["actual_return_1d"] == 3.0   # (103-100)/100*100
        assert updated[key1]["actual_return_3d"] == 10.0   # (110-100)/100*100

    def test_no_unfilled(self, monkeypatch):
        """无待补填数据时不调用 update"""
        monkeypatch.setattr(reflection_engine, 'get_unfilled_reflections',
                            lambda since_days=10: [])
        called = []
        monkeypatch.setattr(reflection_engine, 'update_reflection_outcome',
                            lambda *a, **kw: called.append(True))
        fill_outcomes(days_back=10)
        assert len(called) == 0

    def test_missing_ref_price(self, monkeypatch):
        """ref_date 之后无价格数据 → 跳过"""
        monkeypatch.setattr(reflection_engine, 'get_unfilled_reflections',
                            lambda since_days=10: [
                                {"id": 1, "code": "002281", "date": "2026-06-09"},
                            ])
        monkeypatch.setattr(reflection_engine, 'get_price_history',
                            lambda code, days=60: [
                                {"date": "2026-06-05", "close": 100.0},
                            ])
        called = []
        monkeypatch.setattr(reflection_engine, 'update_reflection_outcome',
                            lambda *a, **kw: called.append(True))
        fill_outcomes(days_back=10)
        assert len(called) == 0


class TestPersistDimensionIC:
    def test_persist_dimension_ic_updates_recent_reflection_rows(self, monkeypatch):
        """把每个反思日期对应的滚动 IC 写回 DB"""
        monkeypatch.setattr(reflection_engine, 'get_reflections',
                            lambda days=30: [
                                {"code": "002281", "date": "2026-06-05"},
                                {"code": "000988", "date": "2026-06-05"},
                                {"code": "600141", "date": "2026-06-06"},
                            ])
        monkeypatch.setattr(reflection_engine, 'compute_dimension_ic',
                            lambda days=20, as_of=None: {
                                "base_score": 0.1 if as_of == "2026-06-05" else -0.1,
                                "momentum_score": 0.2,
                            })
        updated = []
        monkeypatch.setattr(reflection_engine, 'update_reflection_outcome',
                            lambda code, date_str, **kw: updated.append((code, date_str, kw)))

        stats = persist_dimension_ic(days_back=30, window=20)

        assert stats == {"dates": 2, "rows": 3}
        assert updated[0][0:2] == ("002281", "2026-06-05")
        assert updated[0][2]["dimension_ic"]["base_score"] == 0.1
        assert updated[-1][0:2] == ("600141", "2026-06-06")
        assert updated[-1][2]["dimension_ic"]["base_score"] == -0.1

    def test_persist_dimension_ic_no_rows(self, monkeypatch):
        """没有反思记录时不写回"""
        monkeypatch.setattr(reflection_engine, 'get_reflections', lambda days=30: [])
        stats = persist_dimension_ic(days_back=30, window=20)
        assert stats == {"dates": 0, "rows": 0}


# ── Suggest Weight Adjustments ──────────────────────────

class TestSuggestWeightAdjustments:
    def test_positive_ic_raises_weight(self, monkeypatch):
        """正 IC → 权重上调"""
        monkeypatch.setattr(reflection_engine, 'get_reflection_dimension_ic',
                            lambda days=20: {
                                "base_score": 0.15, "zone_score": 0.10,
                                "momentum_score": 0.05, "volume_score": 0.02,
                                "serenity_score": 0.12, "factor_score": 0.08,
                                "technical_score": 0.03, "sentiment_score": 0.06,
                            })
        suggestions = suggest_weight_adjustments(days=20)
        assert len(suggestions) > 0
        # IC=0.15 for base_score → factor = 1 + 0.15*1.667 ≈ 1.25
        # new_weight = 0.15 * 1.25 = 0.1875
        from weight_adjuster import DEFAULT_WEIGHTS, IC_TO_WEIGHT
        expected_factor = 1.0 + 0.15 * 1.667
        expected_factor = max(0.5, min(1.5, expected_factor))
        expected_weight = round(DEFAULT_WEIGHTS["base"] * expected_factor, 4)
        assert suggestions["base"]["suggested"] == expected_weight
        assert suggestions["base"]["ic"] == 0.15

    def test_negative_ic_lowers_weight(self, monkeypatch):
        """负 IC → 权重下调"""
        monkeypatch.setattr(reflection_engine, 'get_reflection_dimension_ic',
                            lambda days=20: {
                                "base_score": -0.15, "zone_score": -0.10,
                                "momentum_score": 0.05, "volume_score": 0.02,
                                "serenity_score": -0.12, "factor_score": -0.08,
                                "technical_score": 0.03, "sentiment_score": -0.06,
                            })
        suggestions = suggest_weight_adjustments(days=20)
        from weight_adjuster import DEFAULT_WEIGHTS
        assert suggestions["base"]["suggested"] < DEFAULT_WEIGHTS["base"]
        assert suggestions["base"]["change_pct"] < 0

    def test_empty_ic_returns_empty(self, monkeypatch):
        """无 IC 数据 → 返回空字典"""
        monkeypatch.setattr(reflection_engine, 'get_reflection_dimension_ic',
                            lambda days=20: {})
        suggestions = suggest_weight_adjustments(days=20)
        assert suggestions == {}


# ── CLI 函数 ────────────────────────────────────────────

class TestShowFunctions:
    def test_show_reflections_empty(self, monkeypatch, capsys):
        """无反思记录时正确显示"""
        monkeypatch.setattr(reflection_engine, 'get_reflections',
                            lambda days=7: [])
        show_reflections(days=7)
        captured = capsys.readouterr()
        assert "暂无" in captured.out

    def test_show_reflections_with_data(self, monkeypatch, capsys):
        """有反思记录时格式化输出"""
        monkeypatch.setattr(reflection_engine, 'get_reflections',
                            lambda days=7: [
                                {"code": "002281", "name": "光迅科技",
                                 "date": "2026-06-07", "total_score": 75,
                                 "predicted_direction": "BUY",
                                 "actual_return_1d": 2.5},
                            ])
        show_reflections(days=7)
        captured = capsys.readouterr()
        assert "光迅科技" in captured.out
        assert "BUY" in captured.out
        assert "75" in captured.out

    def test_show_dimension_ic(self, monkeypatch, capsys):
        """维度 IC 报告输出"""
        monkeypatch.setattr(reflection_engine, 'compute_dimension_ic',
                            lambda days=20: dict.fromkeys(DIMENSION_KEYS, 0.05))
        monkeypatch.setattr(reflection_engine, 'persist_dimension_ic',
                            lambda days_back=20, window=20: {"dates": 1, "rows": 2})
        monkeypatch.setattr(reflection_engine, 'suggest_weight_adjustments',
                            lambda days=20: {})
        show_dimension_ic(days=20)
        captured = capsys.readouterr()
        assert "维度 IC" in captured.out
        assert "已写回" in captured.out

    def test_apply_adjustments_saves(self, monkeypatch):
        """apply_reflection_adjustments 保存权重（通过 weight_adjuster）"""
        monkeypatch.setattr(reflection_engine, 'suggest_weight_adjustments',
                            lambda days=20: {
                                "base": {"suggested": 0.16, "suggested_normalized": 0.16,
                                         "current": 0.15, "ic": 0.1, "change_pct": 6.7},
                                "zone": {"suggested": 0.15, "suggested_normalized": 0.15,
                                         "current": 0.15, "ic": 0.0, "change_pct": 0.0},
                                "momentum": {"suggested": 0.15, "suggested_normalized": 0.15,
                                             "current": 0.15, "ic": 0.0, "change_pct": 0.0},
                                "volume": {"suggested": 0.05, "suggested_normalized": 0.05,
                                           "current": 0.05, "ic": 0.0, "change_pct": 0.0},
                                "serenity": {"suggested": 0.15, "suggested_normalized": 0.15,
                                             "current": 0.15, "ic": 0.0, "change_pct": 0.0},
                                "factor": {"suggested": 0.15, "suggested_normalized": 0.15,
                                           "current": 0.15, "ic": 0.0, "change_pct": 0.0},
                                "technical": {"suggested": 0.10, "suggested_normalized": 0.10,
                                              "current": 0.10, "ic": 0.0, "change_pct": 0.0},
                                "sentiment": {"suggested": 0.10, "suggested_normalized": 0.10,
                                              "current": 0.10, "ic": 0.0, "change_pct": 0.0},
                            })
        import weight_adjuster as wa
        saved = {}
        monkeypatch.setattr(wa, 'save_adjusted_weights',
                            lambda w, ic_report=None: saved.update(w))
        monkeypatch.setattr(reflection_engine, 'show_dimension_ic',
                            lambda days=20: None)
        apply_reflection_adjustments(days=20)
        assert len(saved) > 0
