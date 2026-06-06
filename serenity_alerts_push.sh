#!/bin/bash
# Serenity 主动信号推送 — no_agent cron 包装脚本（升级版）
# 使用 signal_push.py 的 build_signal_brief + format_push_message
# 有买入候选/风险提醒 → 推送，无信号 → 静默
# 注意: 系统 Python3.9 numpy 架构不兼容，必须用 Hermes venv
HERMES_PYTHON="/Users/mac/.hermes/hermes-agent/.venv/bin/python3"
cd /Users/mac/workspace/SerenityMonitor
$HERMES_PYTHON signal_push.py --silent 2>&1
