#!/bin/bash
# Serenity 监控看板守护 — 工作日盘中自动拉起
# 由 cron 每15分钟检查一次，端口8401无监听则启动
PORT=8401
SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
HERMES_PYTHON="/Users/mac/.hermes/hermes-agent/.venv/bin/python"
LOGFILE="$SCRIPT_DIR/logs/dashboard_daemon.log"

mkdir -p "$SCRIPT_DIR/logs"

if lsof -i :$PORT -sTCP:LISTEN > /dev/null 2>&1; then
    # 看板已在运行，检查 ngrok
    if ! pgrep -f "ngrok http 8401" > /dev/null 2>&1; then
        nohup /opt/homebrew/bin/ngrok http 8401 --log=stdout >> "$SCRIPT_DIR/logs/ngrok.log" 2>&1 &
        echo "$(date '+%Y-%m-%d %H:%M:%S') ngrok 隧道已启动" >> "$LOGFILE"
    fi
    exit 0
fi

# 看板未运行 → 全部拉起
echo "$(date '+%Y-%m-%d %H:%M:%S') 看板未运行，启动中..." >> "$LOGFILE"
cd "$SCRIPT_DIR"
nohup "$HERMES_PYTHON" monitoring_dashboard.py >> "$LOGFILE" 2>&1 &
echo "$(date '+%Y-%m-%d %H:%M:%S') 看板已启动 (PID $!)" >> "$LOGFILE"

# ngrok 隧道
if ! pgrep -f "ngrok http 8401" > /dev/null 2>&1; then
    sleep 2
    nohup /opt/homebrew/bin/ngrok http 8401 --log=stdout >> "$SCRIPT_DIR/logs/ngrok.log" 2>&1 &
    echo "$(date '+%Y-%m-%d %H:%M:%S') ngrok 隧道已启动 (PID $!)" >> "$LOGFILE"
fi
