#!/bin/bash
# ============================================================
# 周六复盘自动触发
# Cron: 每周六 09:00
# 功能: 周度策略复盘 + 信号扫描 + 候选标的刷新
# ============================================================
HERMES_PYTHON="/Users/mac/.hermes/hermes-agent/.venv/bin/python3"
SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
cd "$SCRIPT_DIR" || { echo "ERROR: cd failed" >&2; exit 1; }

echo "=== 周六复盘自动触发 ==="
echo "--- WEEKLY-REVIEW ---"
$HERMES_PYTHON cli.py weekly-review 2>&1 || true
echo "--- SIGNALS ---"
$HERMES_PYTHON cli.py signal 2>&1 || true
echo "--- CANDIDATES ---"
$HERMES_PYTHON cli.py scan-candidates 2>&1 || true
echo "--- DONE ---"
exit 0
