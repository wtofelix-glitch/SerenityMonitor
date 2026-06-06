#!/bin/bash
# 14因子独立信号推送包装脚本
# cron 入口: 静默模式（无买入信号时不输出）
# 手动运行: bash fourteen_factor_push.sh

HERMES_PYTHON="/Users/mac/.hermes/hermes-agent/.venv/bin/python3"
cd /Users/mac/workspace/SerenityMonitor
$HERMES_PYTHON fourteen_factor_push.py --silent 2>&1
