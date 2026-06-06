#!/bin/bash
# Serenity Monitor — 移动端监控看板 启动脚本
# 端口 8401，使用 Hermes venv Python（arm64 numpy 兼容）
cd /Users/mac/workspace/SerenityMonitor
exec /Users/mac/.hermes/hermes-agent/.venv/bin/python monitoring_dashboard.py
