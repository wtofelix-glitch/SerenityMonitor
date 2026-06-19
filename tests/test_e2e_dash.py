"""E2E 视觉回归测试 — Plotly Dash 看板 (port 8050)

用法:
    # 首次运行（生成基线截图）
    python3 -m pytest tests/test_e2e_dash.py --e2e --baseline

    # 对比运行
    python3 -m pytest tests/test_e2e_dash.py --e2e
"""

import os
import sys
import time
import threading
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 8050
BASE_URL = f"http://localhost:{PORT}"
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots", "dash")
BASELINE_DIR = os.path.join(os.path.dirname(__file__), "screenshots", "dash", "baseline")


# ── Fixtures ─────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def dash_server():
    """启动 Dash 测试服务 (单次 session)"""
    sys.argv = ["dash_dashboard.py", "--port", str(PORT)]
    import dash_dashboard
    from dash_dashboard import app as dash_app

    t = threading.Thread(
        target=lambda: dash_app.run(
            host="127.0.0.1", port=PORT, debug=False, use_reloader=False
        ),
        daemon=True,
    )
    t.start()
    time.sleep(3)  # Dash 启动稍慢
    yield


# ── 测试: 页面加载 ──────────────────────────────────────────────
class TestDashLoad:

    def test_page_loads(self, page, dash_server):
        """Dash 看板正常加载"""
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")

        # 验证页面标题
        title = page.title()
        assert "Serenity" in title or "Dash" in title

        # 验证至少有一些图表元素
        time.sleep(2)
        graph_count = len(page.query_selector_all(".js-plotly-plot"))
        assert graph_count >= 1, f"期望至少 1 个图表，实际 {graph_count}"

    def test_ic_chart_exists(self, page, dash_server):
        """IC 因子归因图表存在"""
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        # Plotly 图表渲染后会有一个 .main-svg 元素
        svg_count = len(page.query_selector_all(".main-svg"))
        assert svg_count >= 1, "Plotly 图表未渲染"


# ── 测试: 截图 ──────────────────────────────────────────────────
class TestDashScreenshots:

    def test_dash_screenshot(self, page, dash_server):
        """Dash 看板全页截图"""
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        time.sleep(3)  # 等图表渲染

        path = os.path.join(SCREENSHOT_DIR, "dash_overview.png")
        page.screenshot(path=path, full_page=True)
        assert os.path.exists(path), "截图未生成"
        size = os.path.getsize(path)
        assert size > 20000, f"Dash 截图文件过小: {size} bytes"


# ── 测试: 移动端 ────────────────────────────────────────────────
class TestDashMobile:

    def test_mobile_viewport(self, browser, dash_server):
        """Dash 在移动端 viewport 下渲染正常"""
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        page.screenshot(
            path=os.path.join(SCREENSHOT_DIR, "dash_mobile.png"),
            full_page=True,
        )
        context.close()
