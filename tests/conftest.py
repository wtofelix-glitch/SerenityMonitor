"""pytest 全局配置 — E2E + 单元测试共享 fixtures"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 确保 Playwright 可导入
try:
    import playwright  # noqa: F401
except ImportError:
    # 注意：不要插入系统 Python 3.13 的 site-packages，这会导致 numpy/其他
    # C 扩展的版本冲突。请在 venv 中安装 playwright 替代:
    #   pip install playwright && python -m playwright install chromium
    pass


# ── 命令行选项 ──────────────────────────────────────────────────
def pytest_addoption(parser):
    parser.addoption("--e2e", action="store_true", default=False, help="运行 E2E 测试")
    parser.addoption("--baseline", action="store_true", default=False, help="生成基线截图")
    parser.addoption("--update", action="store_true", default=False, help="更新基线截图")
    parser.addoption("--headless", action="store_true", default=True, help="无头模式")


# ── 在 collection 阶段跳过非 --e2e 的 E2E test files ──────────
def pytest_collection_modifyitems(config, items):
    if config.getoption("--e2e"):
        return

    skip_e2e = pytest.mark.skip(reason="需要 --e2e 参数运行")
    for item in items:
        fpath = item.fspath.basename if hasattr(item, "fspath") else ""
        if "e2e" in fpath:
            item.add_marker(skip_e2e)


# ── E2E fixtures（仅在 --e2e 时加载 Playwright） ────────────────
@pytest.fixture(scope="session")
def _e2e_enabled(request):
    return request.config.getoption("--e2e")


@pytest.fixture(scope="session")
def browser(_e2e_enabled, request):
    """Playwright Chromium browser session"""
    if not _e2e_enabled:
        pytest.skip("需要 --e2e 参数运行 Playwright 测试")

    from playwright.sync_api import sync_playwright

    headless = request.config.getoption("--headless", default=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            channel="chromium",  # 使用已缓存的 Chromium（而非 chromium_headless_shell）
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        yield browser
        browser.close()


@pytest.fixture(scope="function")
def page(browser, _e2e_enabled):
    """每个测试一个独立 page（桌面 viewport）"""
    if not _e2e_enabled:
        pytest.skip("需要 --e2e 参数运行 Playwright 测试")

    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,
    )
    p = context.new_page()
    p.set_default_timeout(15000)
    yield p
    context.close()
