"""E2E 冒烟测试 — B2 管线 Tab (Plotly Dash)

用法:
    python3 -m pytest tests/test_e2e_b2_dashboard.py --e2e -v
    python3 -m pytest tests/test_e2e_b2_dashboard.py --e2e --browser chromium -v

依赖:
    playwright install chromium
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent
FIXTURES_DIR = PROJECT_DIR / "tests" / "fixtures" / "b2"
SCREENSHOT_DIR = PROJECT_DIR / "tests" / "screenshots" / "dash_b2"

PORT_BASE = 8060


def _start_dash(port: int, env_extra: dict | None = None) -> subprocess.Popen:
    """Start dash_dashboard.py as a subprocess with custom env."""
    env = os.environ.copy()
    env.update(env_extra or {})
    env["DASH_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "dash_dashboard.py")],
        env=env,
        cwd=str(PROJECT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for server to be ready
    import urllib.request
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    return proc


def _kill(proc: subprocess.Popen):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _screenshot(page, name: str):
    os.makedirs(str(SCREENSHOT_DIR), exist_ok=True)
    path = str(SCREENSHOT_DIR / name)
    page.screenshot(path=path, full_page=True)
    return path


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


# ── Touch fixtures to prevent STALE in E2E ───────────────────

def _touch_fixtures():
    for name in ["sample_report_ok.json", "sample_bad_json.json",
                 "sample_not_json.json", "sample_unknown_schema.json"]:
        p = FIXTURES_DIR / name
        if p.exists():
            os.utime(str(p), None)

_touch_fixtures()

@pytest.fixture(scope="module")
def dash_disabled():
    """Dash with B2 disabled."""
    proc = _start_dash(PORT_BASE, {
        "ENABLE_B2_DASHBOARD": "false",
    })
    yield f"http://127.0.0.1:{PORT_BASE}"
    _kill(proc)


@pytest.fixture(scope="module")
def dash_enabled():
    """Dash with B2 enabled + fixture report."""
    proc = _start_dash(PORT_BASE + 1, {
        "ENABLE_B2_DASHBOARD": "true",
        "B2_REPORT_ROOT": str(FIXTURES_DIR.resolve()),
        "B2_REPORT_PATH": "sample_report_ok.json",
    })
    yield f"http://127.0.0.1:{PORT_BASE + 1}"
    _kill(proc)


@pytest.fixture(scope="module")
def dash_bad_report():
    """Dash with B2 enabled + corrupt report."""
    proc = _start_dash(PORT_BASE + 2, {
        "ENABLE_B2_DASHBOARD": "true",
        "B2_REPORT_ROOT": str(FIXTURES_DIR.resolve()),
        "B2_REPORT_PATH": "sample_bad_json.json",
    })
    yield f"http://127.0.0.1:{PORT_BASE + 2}"
    _kill(proc)


# ═══════════════════════════════════════════════════════════════
# AC-E2E-01: B2 disabled — tab absent, old tabs work
# ═══════════════════════════════════════════════════════════════

class TestB2Disabled:

    def test_page_loads(self, page, dash_disabled):
        page.goto(dash_disabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        assert "Serenity" in page.title() or "Dash" in page.title()

    def test_b2_tab_absent(self, page, dash_disabled):
        page.goto(dash_disabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        el = page.query_selector("text=🏭 B2 管线")
        assert el is None, "B2 tab should not exist when disabled"

    def test_four_old_tabs_present(self, page, dash_disabled):
        page.goto(dash_disabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        for label in ["📊 IC 归因", "⚖️ 权重对比", "📈 执行历史", "🛡️ 风控状态"]:
            assert page.query_selector(f"text={label}") is not None, \
                f"Missing tab: {label}"

    def test_old_tabs_render_charts(self, page, dash_disabled):
        """Old tabs load content (charts may be absent without DB)."""
        page.goto(dash_disabled)
        page.wait_for_load_state("networkidle")
        time.sleep(3)
        # Tab content area should have content
        tab_content = page.query_selector("#tab-content")
        assert tab_content is not None, "Tab content area missing"
        # Page should not have traceback
        body = page.inner_text("body")
        assert "Traceback" not in body

    def test_no_traceback_on_page(self, page, dash_disabled):
        page.goto(dash_disabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        body = page.inner_text("body")
        assert "Traceback" not in body, "Page shows Python traceback"


# ═══════════════════════════════════════════════════════════════
# AC-E2E-02: B2 enabled — tab renders report
# ═══════════════════════════════════════════════════════════════

class TestB2Enabled:

    def test_b2_tab_present(self, page, dash_enabled):
        page.goto(dash_enabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        assert page.query_selector("text=🏭 B2 管线") is not None

    def test_b2_tab_shows_banner(self, page, dash_enabled):
        page.goto(dash_enabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(2)
        assert page.query_selector("text=SHADOW OBSERVABILITY") is not None
        assert page.query_selector("text=NOT FOR EXECUTION") is not None
        assert page.query_selector("text=READ ONLY") is not None

    def test_b2_tab_shows_run_identity(self, page, dash_enabled):
        page.goto(dash_enabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(2)
        assert page.query_selector("text=B2_20260724") is not None
        assert page.query_selector("text=phase-b2-runner-v9") is not None

    def test_b2_tab_shows_audit_equations(self, page, dash_enabled):
        page.goto(dash_enabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(2)
        assert page.query_selector("text=scheduling") is not None
        assert page.query_selector("text=TIMING_INVARIANT") is not None

    def test_switch_back_to_old_tab_works(self, page, dash_enabled):
        """After B2 tab, old tabs are still navigable."""
        page.goto(dash_enabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(2)
        page.query_selector("text=📊 IC 归因").click()
        time.sleep(2)
        # Tab content area should still exist
        tab_content = page.query_selector("#tab-content")
        assert tab_content is not None, "Tab content area missing after B2 visit"
        body = page.inner_text("body")
        assert "Traceback" not in body, "Page showed error after B2 tab visit"

    def test_screenshot_b2_report(self, page, dash_enabled):
        page.goto(dash_enabled)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(3)
        path = _screenshot(page, "b2_report_ok.png")
        assert os.path.getsize(path) > 5000


# ═══════════════════════════════════════════════════════════════
# AC-E2E-03: Bad report — B2 degrades, old tabs OK
# ═══════════════════════════════════════════════════════════════

class TestB2BadReport:

    def test_b2_tab_shows_error(self, page, dash_bad_report):
        page.goto(dash_bad_report)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(2)
        assert page.query_selector("text=INVALID JSON") is not None

    def test_old_tabs_unaffected_by_bad_b2(self, page, dash_bad_report):
        """Bad B2 report must not break old tab navigation."""
        page.goto(dash_bad_report)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.query_selector("text=🏭 B2 管线").click()
        time.sleep(1)
        page.query_selector("text=📊 IC 归因").click()
        time.sleep(2)
        tab_content = page.query_selector("#tab-content")
        assert tab_content is not None, "Tab content area missing after B2 error"
        body = page.inner_text("body")
        assert "Traceback" not in body, "Page crashed after B2 error"


# ═══════════════════════════════════════════════════════════════
# Metadata
# ═══════════════════════════════════════════════════════════════

def test_record_environment(page, dash_enabled):
    os.makedirs(str(SCREENSHOT_DIR), exist_ok=True)
    info = {
        "user_agent": page.evaluate("navigator.userAgent"),
        "viewport": page.viewport_size,
        "url": dash_enabled,
    }
    with open(str(SCREENSHOT_DIR / "e2e_environment.json"), "w") as f:
        json.dump(info, f, indent=2)
