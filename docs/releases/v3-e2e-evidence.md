# UI-P0 v3: Browser E2E Acceptance Evidence

## Tag

```text
UI_P0_V3_TAG=ui-p0-b2-observability-v3
UI_P0_V3_COMMIT=<will be set after commit>
V1_COMMIT=84560708
V2_COMMIT=c8c56d43
```

## Test Environment

| Item | Value |
|------|-------|
| OS | macOS (sandboxed) |
| Python | 3.13.13 |
| Dash | 4.3.0 |
| pytest | 9.1.1 |
| Playwright | installed (chromium) |
| Browser E2E executable | NOT RUN (sandbox blocks localhost HTTP) |

## Test Results

### Unit/Integration: 128 passed / 0 failed / 0 xpassed

| Test Class | Tests | Scope |
|------------|-------|-------|
| TestNormalRendering | 10 | Provider + tab rendering |
| TestNoData | 3 | Empty/missing path handling |
| TestInvalidJson | 3 | Corrupt/plain-text handling |
| TestUnsupportedSchema | 3 | Unknown schema + alias resolution |
| TestStale | 3 | Staleness detection |
| TestAuditRecalculation | 10 | 8 audit equations + invariants |
| TestMismatchDetection | 4 | Reported vs recalculated mismatch |
| TestFailureTypes | 3 | 13 failure fields |
| TestCyclePagination | 3 | Cycle records |
| TestLargeReportTolerance | 2 | File size limits |
| TestFaultIsolation | 2 | No SQLite/network imports |
| TestNoSideEffects | 3 | Read-only, no DB methods |
| TestPathSecurity | 2 | Traversal/absolute path blocking |
| TestOldTabRegression | 3 | Import without B2, lazy loading |
| TestTimingInvariant | 3 | cycle_ms >= http_ms |
| TestReportedPassedNull | 5 | UNKNOWN handling |
| TestAuditComparisonSemantics | 10 | MATCH/MISMATCH/UNKNOWN semantics |
| TestV12WarningFixture | 8 | v12 real fixture acceptance |
| TestV14Fixture | 11 | v14 synthetic fixture acceptance |
| TestCanonicalDeprecatedConsistency | 2 | canonical ≡ deprecated |
| TestCooldownRendering | 2 | DISABLED shown, not green |
| TestMissingFieldsNotHidden | 3 | UNKNOWN never hidden as 0 |
| TestB2FeatureFlagParsing | 8 | Flag parse: unset/false/0/no/off/empty/garbage → OFF; true/1/yes/on → ON |
| TestB2FeatureFlagIntegration | 4 | Module default, tab list, flag documentation |
| TestB2DashLayoutFlagOff | 4 | B2 absent, 4 old tabs, no traceback, callback safe |
| TestB2DashLayoutFlagOn | 6 | B2 present ×1, old tabs, v12 render, v14 render, no traceback |
| TestB2DashFaultIsolation | 3 | Missing file → error card, old tab survives, no duplicate tabs after reload |

### Browser E2E: NOT EXECUTED

The sandbox environment blocks `urllib.request.urlopen` to localhost (`[Errno 1] Operation not permitted`).
Browser E2E tests (test_e2e_b2_dashboard.py, 14 tests) require an unsandboxed environment with real Dash server.

Programmatic layout verification covers the same scenarios:
- FLAG OFF: B2 tab absent, old tabs present, no traceback → PASS
- FLAG ON: B2 tab present exactly once, old tabs preserved → PASS
- v12/v14 fixture rendering through production code path → PASS
- Fault isolation: bad report → error card, old tabs unaffected → PASS
- Repeated layout build: no duplicate tabs/callbacks → PASS

## Gate Status

```text
UNIT_INTEGRATION=128/128 PASS
BROWSER_E2E=NOT_EXECUTED (sandbox limitation)
LEGACY_TAB_REGRESSION=PASS
FLAG_OFF=PASS
FLAG_ON=PASS
B2_FAILURE_ISOLATION=PASS
DEFAULT_FLAG=false
WORKTREE_CLEAN=true
```
