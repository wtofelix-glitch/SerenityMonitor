# UI-P0: B2 Pipeline Observability Tab — Release Candidate

> 日期: 2026-07-27
> 分支: `feat/b2-dashboard`
> HEAD: `d8e98bc9` — fix(review-r2): dual-column audit display + strict xfail pre-existing E2E

---

## 一、发布范围

| 项目 | 状态 |
|:---|:---|
| B2 代码与离线验收 | ✅ 完成 |
| B2 v9 preflight (13/14 passed) | ✅ 已预检 |
| B2 v9 5分钟真实盘中验证 | ⏳ 待执行 (今天21:39已闭市，下一个窗口: 2026-07-28 周二 09:35-09:40 CST) |
| UI-P0 Provider + ViewModel | ✅ 完成 |
| UI-P0 B2 Pipeline Tab (Plotly Dash) | ✅ RC |
| Feature flag (`ENABLE_B2_DASHBOARD`) | 🔒 默认关闭 |

---

## 二、测试结果

### 2.1 单元测试

```
tests/test_b2_dashboard.py:  74 passed, 0 failed
```

覆盖范围:
- Provider 正常渲染 (10 tests)
- NO_DATA / INVALID_JSON / UNSUPPORTED_SCHEMA / STALE 状态 (12 tests)
- 八组审计方程独立复算 (12 tests)
- Mismatch 检测 — reported vs recalculated (4 tests)
- 13种失败类型 (3 tests)
- Cycle 分页 (3 tests)
- 大文件容限 (2 tests)
- 故障隔离 — 无 SQLite/网络导入 (3 tests)
- 无副作用 — 纯只读 (3 tests)
- 路径安全 — traversal/absolute 阻止 (2 tests)
- 旧 Tab 回归 (3 tests)
- Timing Invariant (3 tests)
- reported_passed=UNKNOWN 语义 (5 tests)
- Audit 比较语义 (9 tests)

### 2.2 E2E (Playwright)

```
test_e2e_b2_dashboard.py: 14 passed (HeadlessChrome 148, 前次运行)
test_e2e_b2_dashboard.py: 14 skipped (当前环境: Playwright Chromium 1223 Mach port sandbox 不兼容 macOS)
```

E2E 测试代码本身已验证可运行通过。当前运行环境存在 Chromium 基础设施问题（`bootstrap_check_in` permission denied 1100），不影响单元测试和代码质量判断。

### 2.3 旧 Tab 回归

```
tests/ (Dash/UZI related): 104 passed, 43 skipped
```

### 2.4 已知 strict xfail

| 测试 | 原因 |
|:---|:----|
| `test_rebuttal_in_round2` (ISSUE-LLM-001) | XPASS(strict) — LLM 模型输出漂移，固定字符串断言不再可靠。不涉及 B2/UI 模块。 |

---

## 三、Report Fixture 清单

| Fixture | 来源 | 用途 |
|:---|:---|:---|
| `sample_report_ok.json` | SYNTHETIC | 绿色路径 — 42 signals, audit 全过, 正常信号分布 |
| `sample_report_invalidated_environment_scope.json` | REAL (脱敏) | 负向路径 — 4175 signal storm, 4175/4175 ACTION→effective, 0 downgrade, missing_safety_tags=4175, 空 prod_file_hash, run_status=INVALIDATED_ENVIRONMENT_SCOPE |
| `sample_bad_json.json` | SYNTHETIC | 损坏 JSON → INVALID_JSON |
| `sample_not_json.json` | SYNTHETIC | 纯文本 → INVALID_JSON |
| `sample_unknown_schema.json` | SYNTHETIC | 未知 schema → UNSUPPORTED_SCHEMA |

### 负向 Fixture 元数据

```
original_run_id: B2_20260724_130525
original_source: serenity-p1/shadow_data/b2/
source_status: INVALIDATED_ENVIRONMENT_SCOPE
sanitization: signals sampled 30/4175, prices/amounts zeroed, cycles truncated 10/180
```

---

## 四、审计展示验收

| 场景 | 预期 | 实际 |
|:---|:---|:---|
| 合成正向 — 八组方程全过 | all_audit_passed=True, no mismatch | ✅ |
| 真实负向 — signal storm | all_audit_passed=False, has_mismatch=True, missing_tags=4175 | ✅ |
| 损坏 JSON | report_status=INVALID_JSON | ✅ |
| 纯文本 | report_status=INVALID_JSON | ✅ |
| 未知 schema | report_status=UNSUPPORTED_SCHEMA | ✅ |
| reported=None (UNKNOWN) | 不产生 mismatch | ✅ |
| reported≠recalculated | 产生 MISMATCH | ✅ |
| Timing invariant 违规 | detected | ✅ |

---

## 五、Feature Flag

```
ENABLE_B2_DASHBOARD=false  # 默认关闭
ENABLE_B2_DASHBOARD=true   # 启用 B2 管线 Tab
```

关闭时: 旧 4 Tab 正常渲染，B2 模块延迟加载，无 B2 import 副作用。
开启时: 第 5 个 Tab "B2 Pipeline" 出现在 Dash 看板中。

---

## 六、尚缺项目（阻塞正式上线）

1. **v9 真实盘中 5 分钟影子验证** — 今日 (7/27) 已闭市，最早明天 (7/28 周二) 09:35-09:40 CST
2. **真实正向 Report Fixture** — 待 v9 运行通过后生成
3. **Postflight 检查** — 待 v9 运行后执行
4. **B2 报告复审** — 审查影子链路输出与离线回放一致性
5. **UI 最终回归 (含真实 Fixture)** — 用真实报告跑完整 UI 验收
6. **Feature flag 切换为默认开启** — 需确认无生产影响后执行

---

## 七、最终标签条件

完成上述全部阻塞项后，创建不可移动标签:

```
ui-p0-b2-observability-v1
```

标签指向包含真实 Fixture 和最终验收修复的 UI 提交（不是 v9 Runner 提交）。

---

## 八、变更文件

```
 M tests/fixtures/b2/sample_report_ok.json         ← 添加 _fixture_meta (SYNTHETIC)
 ?? tests/fixtures/b2/sample_report_invalidated_environment_scope.json  ← 新增负向 fixture
 ?? docs/releases/ui-p0-b2-observability-rc.md     ← 本文档
```

