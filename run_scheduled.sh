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
    # WAL checkpoint: 将 WAL 数据合并回主 DB 文件
    sqlite3 "$SCRIPT_DIR/serenity.db" "PRAGMA wal_checkpoint(TRUNCATE);" >> "$LOG_DIR/scheduler.log" 2>&1 || true
}

# ── 时段分发 ──────────────────────────────────────────
case "$HOUR" in
    07)
        run_task "盘前简报" "auto_execute.py --premarket"
        # 哨兵后台: 结算+进化 (静默, 看板查看)
        $PYTHON -c "from sentinel_engine import get_sentinel; e=get_sentinel(); e.settle_outcomes(5); e.update_source_weights()" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        # 自主研究: 采集新闻+提取信号+映射标的
        $PYTHON research_engine.py --daily >> "$LOG_DIR/scheduler.log" 2>&1 || true
        # 大师智库同步: guru_wisdom → sentinel
        $PYTHON -c "from sentinel_engine import get_sentinel; get_sentinel().sync_guru_quotes()" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        # 价格告警检查
        $PYTHON -c "from price_alert import check; t=check(); print(f'告警检查: {len(t)}条触发') if t else None" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        ;;
    15)
        run_task "收盘工作流" "daily_workflow.py --push"
        # 哨兵收盘结算
        $PYTHON -c "from sentinel_engine import get_sentinel; e=get_sentinel(); e.settle_outcomes(5)" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        ;;
    22)
        # 晚间加跑一次完整工作流（保险：15:05 若网络失败则 22:00 重补）
        run_task "晚间复核" "daily_workflow.py --push"
        # 哨兵夜间自进化
        $PYTHON -c "from sentinel_engine import get_sentinel; get_sentinel().update_source_weights()" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        # 持仓复盘教练 (仅周日)
        if [ $(date +%u) -eq 7 ]; then
            $PYTHON -c "from trade_coach import coach_report; print(coach_report())" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        fi
        # 自主研究: 晚间二轮采集+同步哨兵
        $PYTHON research_engine.py --daily >> "$LOG_DIR/scheduler.log" 2>&1 || true
        # 大师智库同步: guru_wisdom → sentinel
        $PYTHON -c "from sentinel_engine import get_sentinel; get_sentinel().sync_guru_quotes()" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        # 价格告警检查
        $PYTHON -c "from price_alert import check; t=check(); print(f'告警检查: {len(t)}条触发') if t else None" >> "$LOG_DIR/scheduler.log" 2>&1 || true
        ;;
    *)
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 非调度时段，跳过" >> "$LOG_DIR/scheduler.log"
        ;;
esac

echo "" >> "$LOG_DIR/scheduler.log"
# DB 文件权限加固
chmod 600 "$SCRIPT_DIR/serenity.db" 2>/dev/null || true
exit 0
