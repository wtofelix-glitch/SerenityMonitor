"""Regression tests for the July 2026 dashboard audit findings."""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import monitoring_dashboard as dashboard


ROOT = Path(__file__).resolve().parents[1]


def test_plan_execution_match_ignores_unrelated_trade(monkeypatch):
    class FakeConn:
        def execute(self, sql, params):
            return self

        def fetchall(self):
            return [{"code": "600585", "action": "buy"}]

        def close(self):
            pass

    monkeypatch.setattr(dashboard, "get_conn", FakeConn)

    plan = {"buys": [{"code": "000938"}], "sells": []}

    assert dashboard._plan_already_executed(plan) is False


def test_plan_execution_helper_has_one_definition():
    tree = ast.parse((ROOT / "monitoring_dashboard.py").read_text())
    definitions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_plan_already_executed"
    ]
    assert len(definitions) == 1


def test_cache_loader_is_single_flight(monkeypatch):
    dashboard._cache_invalidate("qd")
    calls = 0
    calls_lock = threading.Lock()

    def loader():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return {"value": 42}

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(dashboard._cache_load("qd", loader, {}))
        )
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert results == [{"value": 42}] * 12
    dashboard._cache_invalidate("qd")


def test_frontend_api_routes_are_registered():
    routes = {rule.rule for rule in dashboard.app.url_map.iter_rules()}
    assert "/api/anomalies" in routes
    assert "/api/nl-query" in routes
    assert "/api/operations-center" in routes


def test_template_has_accessible_tab_contract():
    template = (ROOT / "templates" / "monitor.html").read_text()

    assert "maximum-scale" not in template
    assert "user-scalable=no" not in template
    for tab in ("overview", "holdings", "sentinel", "risk", "operations"):
        assert f'id="tab-button-{tab}"' in template
        assert f'aria-controls="tab-{tab}"' in template
        assert f'aria-labelledby="tab-button-{tab}"' in template


def test_css_uses_one_design_token_root_and_visible_scrollbars():
    css = (ROOT / "static" / "css" / "monitor.css").read_text()

    assert css.count(":root {") == 1
    assert "::-webkit-scrollbar { width: 0" not in css
    assert ":focus-visible" in css


def test_javascript_has_skeleton_retry_and_no_legacy_chart_gold():
    js = (ROOT / "static" / "js" / "monitor.js").read_text()

    assert "dashboard-skeleton" in js
    assert "retryDashboardLoad" in js
    assert "#FFD700" not in js
    assert "visibilitychange" in js
