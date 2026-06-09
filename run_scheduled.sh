#!/bin/bash
# Serenity 定时调度 — 由 launchd 按计划调用
# 自动判断当前时段，执行对应任务
# 07:30 → 盘前简报 | 15:05 → 收盘工作流 | 22:00 → 晚间复核

set -euo pipefail

SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
PYTHON="/Users/mac/miniconda3/bin/python3"
LOG_DIR="$SCRIPT_DIR/logs"
LOCK_DIR="/tmp/serenity_scheduler.lock"

# ── 防重叠锁 (mkdir 原子操作，兼容 macOS) ──────────────
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 上一任务尚未完成，跳过此次调度" >> "$LOG_DIR/scheduler.log"
    exit 1
fi
trap "rmdir '$LOCK_DIR' 2>/dev/null" EXIT

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"

HOUR=$(date '+%H')
MINUTE=$(date '+%M')
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⏰ 调度触发 (${HOUR}:${MINUTE})" >> "$LOG_DIR/scheduler.log"

run_task() {
    local label="$1" cmd="$2"
    echo "──────────────────────────────────────────────" >> "$LOG_DIR/scheduler.log"
    echo "  ▶ $label" >> "$LOG_DIR/scheduler.log"
    echo "──────────────────────────────────────────────" >> "$LOG_DIR/scheduler.log"
    if $PYTHON $cmd >> "$LOG_DIR/scheduler.log" 2>&1; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ $label 完成" >> "$LOG_DIR/scheduler.log"
    else
        local rc=$?
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ $label 失败 (exit=$rc)" >> "$LOG_DIR/scheduler.log"
        return $rc
    fi
}

# ── 时段分发 ──────────────────────────────────────────
case "$HOUR" in
    07)
        run_task "盘前简报" "auto_execute.py --premarket"
        ;;
    15)
        run_task "收盘工作流" "daily_workflow.py --push"
        ;;
    22)
        # 晚间加跑一次完整工作流（保险：15:05 若网络失败则 22:00 重补）
        run_task "晚间复核" "daily_workflow.py --push"
        ;;
    *)
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 非调度时段，跳过" >> "$LOG_DIR/scheduler.log"
        ;;
esac

echo "" >> "$LOG_DIR/scheduler.log"
exit 0
