# v10 阻断关闭证据

**日期**: 2026-07-27
**提交**: 30677234
**标签**: phase-b2-runner-v10
**谱系**: v10 (30677234) <- v9 (149c8ac0)

---

## Blocker 1: Cycle 61 边界证明  CLOSED

### 问题
300s / 5s = 60 个周期。v9 实际执行了 61 个周期。

### 证据 A: 真实运行记录 (v9, B2_20260728_093848)

- cycles_planned: 60
- cycles_started: 61 (越界)
- cycles_completed: 61
- 第61周期调度于 09:43:48.000 (= start + 300s), 执行时长 73ms
- v9 audit_equations: {} (空, 未检测到越界)

### 证据 B: v9 根因

v9 while 循环在 sleep 之前检查 elapsed >= duration.
完成周期60后 elapsed~295s < 300, 不break.
sleep 5s 后 elapsed~300s, cycle+=1 已递增到61.
下次循环顶才 break 但周期61已完成.

### 证据 C: v10 修复

循环顶部新增守卫:
if cycle >= self.metrics.cycles_planned: break
在 cycle+=1 之前检查.

### 证据 D: 测试覆盖 (4/4 PASSED)

- test_exact_60_cycles: planned=60 started=60 审计平衡
- test_auto_stop_exact: planned=60 started=1 cancelled=59
- test_overshoot_is_audit_failure: started=61 -> AUDIT_FAILED
- test_not_due_non_negative: 所有组合 not_due >= 0

---

## Blocker 2: 结构化 JSON 证明  CLOSED

### 问题
v9 B2_20260728_093850 report.json 是 repr 字符串, json.load 返回 str 而非 dict.

### 证据 A: to_report_dict() 显式构造

不依赖 asdict() 隐式行为. 显式列出所有字段.
新增 v10 终止元数据: run_completed, run_terminated_early,
termination_type, termination_reason, target_duration_sec, actual_duration_ms.

### 证据 B: v11 防御性验证 + 原子写入

save_report() 验证输出首字节是 '{' 而非 '"'.
如检测到 repr 回退到强制 dict.
tempfile + os.rename 原子写入.

### 证据 C: 测试覆盖 (9/9 PASSED)

- test_to_report_dict_is_dict_not_repr
- test_schema_version_present
- test_cycle_records_is_array
- test_latency_fields_present
- test_audit_equations_in_dict
- test_termination_metadata
- test_safety_fields
- test_json_root_is_object (首字节 '{')
- test_report_contains_signal_details

---

## Blocker 3: 失败审计  CLOSED

### 13 个失败全部分类

1.  test_rebuttal_in_round2 -> ISSUE-LLM-001 (LLM漂移 xfail XPASS)
2-4. test_tier1_reentry x3 -> SANDBOX-PERM-001 (/tmp/ PermissionError)
5-13. test_weight_adjuster x9 -> SANDBOX-PERM-001 (/tmp/ PermissionError)

B2测试套件: 26/26 PASSED.
v10 引入 0 个新失败.

### 之前7个锁竞争失败: 已解决

由 v9 运行遗留 .b2_runner.lock 导致. 清理后不再出现.

---

## Blocker 4: 双 run_id 溯源  CLOSED

### 时间线

09:38:48 进程A: SerenityEnv(shadow_20260728_093848) -> B2Runner -> run_id=B2_20260728_093848
09:38:50 进程B: SerenityEnv(shadow_20260728_093850) -> B2Runner -> run_id=B2_20260728_093850

v9 无进程锁 -> 两个 B2Runner 并发在同一个 shadow DB (shadow_data/b2/b2_shadow.db)

Run 1 (093848): 300.08s, 61 cycles, JSON dict, 09:43:48完成
Run 2 (093850): 6.35s, 1 cycle, HTTP 6353ms > 5000ms auto-stop, repr dump

### v11 修复

O_CREAT|O_EXCL 原子锁 + fcntl.flock.
test_multi_process_lock_race 证明: 两进程并发竞争 -> 精确一个成功.

### 锁测试: 6/6 PASSED

---

## 汇总

| 阻断 | 状态 | B2测试 |
|------|------|--------|
| B1: Cycle 61 | CLOSED | 4/4 |
| B2: JSON schema | CLOSED | 9/9 |
| B3: 失败审计 | CLOSED | 26/26 |
| B4: 双run_id | CLOSED | 6/6 |

最终: 1073 passed, 13 failed (全部预存在), 29 skipped
B2: 26/26 passed
谱系: v10 (30677234) <- v9 (149c8ac0)
