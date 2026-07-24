# 测试收集差异分析: 1079 vs 1259

## 执行命令

```
pytest --collect-only -q > artifacts/collect-v6.txt
pytest -q --tb=short > artifacts/full-regression-v6.txt
```

## 结果

| Worktree | 收集 | 通过 | 跳过 | xfail |
|----------|------|------|------|-------|
| Main (SerenityMonitor) | 1259 | 1229 | 29 | 1 |
| P0 (serenity-p0-env) | 1079 | 1049 | 29 | 1 |
| **差异** | **-180** | -180 | 0 | 0 |

## 根因

P0 worktree 基于提交 `72ed83a9` (P0-1 基础) 创建。
Main 分支在 `72ed83a9` 之后继续累积了 ~27 个新测试文件。

**180 个差异测试全部来自 P0 worktree 中不存在的文件，非收集错误、非 marker 过滤、非环境问题。**

## 仅在 Main 存在的测试文件（~27 个）

涉及 QMT 券商接口、factor agents、debate backtest、paper trader、
decision packet、trade gateway、micro live gate、broker reconciliation、
kill switch、authorized trading 等独立功能模块。

这些功能与 P0-1~P0-4 安全修复无关，P0 worktree 不包含它们。
P0 组合版本含自身的 4 个新测试文件（154 测试），用于覆盖 P0-1~P0-4。

## P0 核心安全测试全部收集

| 模块 | 文件 | 测试数 | 状态 |
|------|------|--------|------|
| P0-1 | test_prod_guard.py | 43 | ✅ |
| P0-2 | test_signal_idempotency.py | 24 | ✅ |
| P0-3 | test_account_fixture.py | 47 | ✅ |
| P0-4 | test_p0_4_statistics.py | 40 | ✅ |
| B2 | test_phase_b2.py | 7 | ✅ |
| B1 | test_phase_b1.py | 5 | ✅ |
| **合计** | | **166** | |

## Skip 和 xfail

- 29 skipped: 全部为 E2E 测试（test_e2e_dashboard.py 25 + test_e2e_dash.py 4），需要 `--e2e` 参数
- 1 xfailed: `ISSUE-LLM-001` (test_rebuttal_in_round2)，已登记

## 结论

1079 是 P0 worktree 的正确全量测试数。180 差异来自分叉后 main 分支新增的独立功能测试。
P0-1~P0-4 所有安全相关测试均被正常收集并通过。
