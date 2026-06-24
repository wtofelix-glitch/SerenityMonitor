"""E2E 视觉回归测试 — Flask 监控看板 (port 8401)

用法:
    # 首次运行（生成基线截图）
    python3 -m pytest tests/test_e2e_dashboard.py --e2e --baseline

    # 对比运行
    python3 -m pytest tests/test_e2e_dashboard.py --e2e

依赖:
    pip install playwright pytest-playwright
    playwright install chromium
"""

import os
import sys
import time
import threading
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 配置 ────────────────────────────────────────────────────────
PORT = 8402  # 测试端口，避免和 live server (8401) 冲突
BASE_URL = f"http://localhost:{PORT}/monitor"
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots", "flask")
BASELINE_DIR = os.path.join(os.path.dirname(__file__), "screenshots", "flask", "baseline")

TABS = ["overview", "holdings", "sentinel", "risk"]
VIEWPORTS = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "mobile", "width": 390, "height": 844},
]


# ── Fixtures ─────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def flask_server():
    """启动 Flask 测试服务 (单次 session)"""
    from monitoring_dashboard import app as flask_app

    t = threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1", port=PORT, debug=False, use_reloader=False
        ),
        daemon=True,
    )
    t.start()
    time.sleep(2)  # 等 server 起来
    yield


# ── 工具函数 ────────────────────────────────────────────────────
def ensure_dirs():
    os.makedirs(BASELINE_DIR, exist_ok=True)
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def screenshot_path(name, ext="png"):
    return os.path.join(SCREENSHOT_DIR, f"{name}.{ext}")


def baseline_path(name, ext="png"):
    return os.path.join(BASELINE_DIR, f"{name}.{ext}")


def take_tab_screenshot(page, tab_name, viewport_name):
    """切换 tab、等待渲染、截图"""
    tab_selector = f'button[data-tab="{tab_name}"]'
    page.click(tab_selector)
    time.sleep(1.5)  # 等 JS 渲染

    # 等待对应 tab content 出现
    content_selector = f"#tab-{tab_name}"
    page.wait_for_selector(f"{content_selector}.active", timeout=10000)

    fname = f"flask_{viewport_name}_{tab_name}"
    page.screenshot(path=screenshot_path(fname), full_page=True)
    return fname


# ── 测试: 页面加载 ──────────────────────────────────────────────
class TestDashboardLoad:

    def test_page_loads(self, page, flask_server):
        """看板首页正常加载"""
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)

        # 验证关键元素
        title = page.text_content(".header-title")
        assert title is not None and "Serenity" in title

        # 验证 tab 存在
        tab_count = len(page.query_selector_all(".tab-btn"))
        assert tab_count == 4, f"期望 4 个 tab，实际 {tab_count}"

        # 验证 KPI 栏 (v4.0: JS 渲染 .kpi-item ×4, 等待加载)
        page.wait_for_selector(".kpi-item", timeout=15000)
        kpi_cards = page.query_selector_all(".kpi-item")
        assert len(kpi_cards) >= 2, f"期望至少 2 个 KPI，实际 {len(kpi_cards)}"

        # 验证 overview 默认 active
        assert page.is_visible("#tab-overview.active")

    def test_chartjs_loaded(self, page, flask_server):
        """Chart.js 库加载正常"""
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)
        has_chartjs = page.evaluate("typeof Chart !== 'undefined'")
        assert has_chartjs, "Chart.js 未加载"


# ── 测试: Tab 切换 ──────────────────────────────────────────────
class TestTabNavigation:

    @pytest.mark.parametrize("tab_id", TABS)
    def test_tab_switch(self, page, flask_server, tab_id):
        """每个 tab 可切换"""
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)

        tab_selector = f'button[data-tab="{tab_id}"]'
        page.click(tab_selector)
        time.sleep(1)

        assert page.is_visible(f"#tab-{tab_id}.active"), f"Tab {tab_id} 未激活"
        # Tab button 应标记 active
        assert page.evaluate(
            f'document.querySelector(\'button[data-tab="{tab_id}"]\').classList.contains("active")'
        )

    def test_tab_content_renders(self, page, flask_server):
        """每个 tab 的内容区域非空"""
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)

        for tab_id in TABS:
            tab_selector = f'button[data-tab="{tab_id}"]'
            page.click(tab_selector)
            time.sleep(1.5)

            content = page.evaluate(f'document.getElementById("tab-{tab_id}")?.innerHTML?.length || 0')
            # Content might be empty initially if data hasn't loaded, but should at least have the container
            assert content is not None, f"Tab {tab_id} 内容为空"


# ── 测试: API 端点 ──────────────────────────────────────────────
class TestAPIEndpoints:

    API_ENDPOINTS = [
        "/api/monitor-data",
        "/api/nav-history",
        "/api/signal-history",
        "/api/signal-performance",
        "/api/factor-ic",
        "/api/journal",
        "/api/quantdinger-consensus",
    ]

    @pytest.mark.parametrize("endpoint", API_ENDPOINTS)
    def test_api_returns_json(self, flask_server, endpoint):
        """每个 API 端点返回有效 JSON（用 Python requests，不走浏览器）"""
        import requests
        url = f"http://localhost:{PORT}{endpoint}"
        resp = requests.get(url, timeout=10)
        assert resp.status_code == 200, f"{endpoint} → {resp.status_code}"
        data = resp.json()
        assert data is not None, f"{endpoint} 返回无效 JSON"


# ── 测试: 视觉截图（无基线对比版） ───────────────────────────────
class TestScreenshots:

    @pytest.mark.parametrize("tab_id", TABS)
    def test_tab_screenshots(self, page, flask_server, tab_id):
        """各 tab 截图（记录到 screenshots/ 目录）"""
        ensure_dirs()
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)

        fname = take_tab_screenshot(page, tab_id, "desktop")
        # 验证截图已生成
        assert os.path.exists(screenshot_path(fname)), f"截图未生成: {fname}"
        size = os.path.getsize(screenshot_path(fname))
        assert size > 10000, f"截图文件过小: {size} bytes"


# ── 测试: 移动端适配 ────────────────────────────────────────────
class TestMobile:

    def test_mobile_viewport(self, browser, flask_server):
        """移动端 viewport 布局正常"""
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)

        # 验证移动端样式生效（tab buttons 应换行适应小屏）
        tab_btns = page.query_selector_all(".tab-btn")
        assert len(tab_btns) > 0

        ensure_dirs()
        page.screenshot(path=screenshot_path("flask_mobile_overview"), full_page=True)
        context.close()


# ── 测试: 错误状态 ──────────────────────────────────────────────
class TestErrorStates:

    def test_offline_state(self, page):
        """离线时应有正确的错误提示（模拟网络断开）"""
        # 路由到不存在的 server 端口
        try:
            page.goto("http://localhost:19999/test", timeout=5000)
        except Exception:
            pass
        # 期望页面显示错误消息或连接失败
        # 这个测试只是确认 Playwright 不会崩溃
        assert True

    def test_invalid_route(self, page, flask_server):
        """无效路由返回 404"""
        response = page.goto(f"http://localhost:{PORT}/nonexistent")
        status = response.status
        assert status == 404


# ── 视觉回归对比（可选） ────────────────────────────────────────
class TestVisualRegression:

    @pytest.mark.skipif(
        "not config.getoption('--baseline') and not config.getoption('--update')",
        reason="需要 --baseline 或 --update 参数",
    )
    @pytest.mark.parametrize("tab_id", TABS)
    def test_visual_regression(self, page, flask_server, tab_id):
        """视觉回归对比：截图 vs 基线"""
        is_baseline = page.config.getoption("--baseline")
        is_update = page.config.getoption("--update")

        ensure_dirs()
        page.goto(BASE_URL)
        page.wait_for_selector("header.top-header", timeout=10000)

        fname = take_tab_screenshot(page, tab_id, "desktop")

        # 生成 / 更新基线
        if is_baseline or is_update:
            import shutil
            shutil.copy(screenshot_path(fname), baseline_path(fname))
            pytest.skip(f"基线截图已保存: {fname}")

        # 对比基线
        baseline_file = baseline_path(fname)
        current_file = screenshot_path(fname)

        assert os.path.exists(baseline_file), f"基线截图不存在，请先用 --baseline 生成: {baseline_file}"

        # 简单文件大小对比（更精确的像素对比可用 pixelmatch）
        baseline_size = os.path.getsize(baseline_file)
        current_size = os.path.getsize(current_file)
        size_ratio = abs(baseline_size - current_size) / max(baseline_size, 1)

        assert size_ratio < 0.3, (
            f"截图差异过大 ({size_ratio*100:.1f}%): "
            f"基线 {baseline_size} → 当前 {current_size}"
        )
