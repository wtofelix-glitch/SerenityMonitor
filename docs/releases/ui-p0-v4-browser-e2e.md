# UI-P0 v4: External Browser E2E Acceptance

## Tag

```text
UI_P0_V4_TAG=ui-p0-b2-observability-v4
UI_P0_V4_COMMIT=<will be set after commit>
```

## v3 Errata

v3 证据文档声明 `BROWSER_E2E=NOT_EXECUTED (sandbox limitation)`。
v4 在外部终端（unsandboxed macOS zsh）中完成全部 14 项浏览器 E2E。

## Test Environment

| Item | Value |
|------|-------|
| OS | macOS (external terminal, unsandboxed) |
| Python | 3.13.13 |
| Dash | 4.3.0 |
| pytest | 9.1.1 |
| Playwright | 0.8.0 (chromium) |
| TMPDIR | /tmp/serenity-e2e (external to sandbox) |

## Test Results — 14/14 PASS (61.59s)

### Flag OFF (B2 disabled) — 5/5

| Test | Result |
|------|--------|
| test_page_loads | PASS |
| test_b2_tab_absent | PASS |
| test_four_old_tabs_present | PASS |
| test_old_tabs_render_charts | PASS |
| test_no_traceback_on_page | PASS |

### Flag ON (B2 enabled) — 6/6

| Test | Result |
|------|--------|
| test_b2_tab_present | PASS |
| test_b2_tab_shows_banner | PASS |
| test_b2_tab_shows_run_identity | PASS |
| test_b2_tab_shows_audit_equations | PASS |
| test_switch_back_to_old_tab_works | PASS |
| test_screenshot_b2_report | PASS |

### Bad Report (B2 error isolation) — 2/2

| Test | Result |
|------|--------|
| test_b2_tab_shows_error | PASS |
| test_old_tabs_unaffected_by_bad_b2 | PASS |

### Metadata — 1/1

| Test | Result |
|------|--------|
| test_record_environment | PASS |

## Full Node IDs

```text
tests/test_e2e_b2_dashboard.py::TestB2Disabled::test_page_loads
tests/test_e2e_b2_dashboard.py::TestB2Disabled::test_b2_tab_absent
tests/test_e2e_b2_dashboard.py::TestB2Disabled::test_four_old_tabs_present
tests/test_e2e_b2_dashboard.py::TestB2Disabled::test_old_tabs_render_charts
tests/test_e2e_b2_dashboard.py::TestB2Disabled::test_no_traceback_on_page
tests/test_e2e_b2_dashboard.py::TestB2Enabled::test_b2_tab_present
tests/test_e2e_b2_dashboard.py::TestB2Enabled::test_b2_tab_shows_banner
tests/test_e2e_b2_dashboard.py::TestB2Enabled::test_b2_tab_shows_run_identity
tests/test_e2e_b2_dashboard.py::TestB2Enabled::test_b2_tab_shows_audit_equations
tests/test_e2e_b2_dashboard.py::TestB2Enabled::test_switch_back_to_old_tab_works
tests/test_e2e_b2_dashboard.py::TestB2Enabled::test_screenshot_b2_report
tests/test_e2e_b2_dashboard.py::TestB2BadReport::test_b2_tab_shows_error
tests/test_e2e_b2_dashboard.py::TestB2BadReport::test_old_tabs_unaffected_by_bad_b2
tests/test_e2e_b2_dashboard.py::test_record_environment
```

## Console Errors

**未检查。** 当前测试实现没有 `page.on("console")` 或 `pageerror` 监听器。
`test_no_traceback_on_page` 只检查页面 body 文字中的 "Traceback"，不捕获浏览器 console 错误。
此限制在 v4 scope 中已明确记录；不应从 `test_no_traceback_on_page` 推导 `console_errors=0`。

## Fixture Coverage

E2E 测试使用的 `sample_report_ok.json` 是基本 B2 report（status、cycles、timing），
**不包含** v12 warning fixture 或 v14 synthetic fixture。
v12/v14 fixture acceptance 由单元测试覆盖（`TestV12WarningFixture`、`TestV14Fixture`，共 19 tests），不在 E2E scope 内。

| Fixture | E2E Covered | Unit Covered |
|----------|:-----------:|:------------:|
| v12 warning | ❌ | ✅ (8 tests) |
| v14 synthetic | ❌ | ✅ (11 tests) |
| B2 basic (ok) | ✅ | — |
| B2 bad JSON | ✅ | — |

## Screenshot Comparison

| Item | Dimensions | SHA-256 |
|------|-----------|---------|
| v3 baseline | 2880×3252 | `c3e4ca6317bad3a25ca7a197919d95f99830db8bb1e284c0cbbcbc90f274f5d3` |
| v4 generated | 2880×3272 | `1b16da99f31bdbf0ef098584706268c40150f39f81c8109bf12c94decc80779e` |

截图尺寸不同（3252→3272 高度），哈希不同 — 确认了动态内容或渲染引擎版本差异。
v4 截图已归档到外部审计目录，不提交到 repo。工作树中的截图已从 HEAD 恢复。

## Auditable Artifacts

| File | Location |
|------|----------|
| Generated screenshot | `v4-browser-e2e-20260729T214325/b2_report_ok.generated.png` |
| Generated SHA-256 | `v4-browser-e2e-20260729T214325/b2_report_ok.generated.sha256` |
| Generated dimensions | `v4-browser-e2e-20260729T214325/b2_report_ok.generated.dimensions.txt` |
| v3 baseline screenshot | `v4-browser-e2e-20260729T214325/b2_report_ok.v3.png` |
| v3 baseline SHA-256 | `v4-browser-e2e-20260729T214325/b2_report_ok.v3.sha256` |
| Pytest output | `v4-browser-e2e-20260729T214325/pytest-e2e.txt` |

## Code Changes

**无 Dashboard 业务代码变化。** 此 tag 仅新增 `docs/releases/ui-p0-v4-browser-e2e.md` 证据文档。
`tests/screenshots/dash_b2/b2_report_ok.png` 不在本次 commit 中（已恢复为 v3 baseline）。

## Gate Status

```text
BROWSER_E2E=14/14_PASS
PYTEST_EXIT_CODE=0
FLAG_OFF=PASS
FLAG_ON=PASS
OLD_TABS=PASS
B2_ERROR_ISOLATION=PASS
SCREENSHOT_CAPTURE=PASS
UNHANDLED_CONSOLE_ERRORS=NOT_CHECKED (no page.on("console") listener)
FIXTURE_COVERAGE=V12_WARNING_NOT_IN_E2E, V14_SYNTHETIC_NOT_IN_E2E
BASE_COMMIT=c23c69af
BASE_TAG=ui-p0-b2-observability-v3
WORKTREE_CLEAN=true
```
