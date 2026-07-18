#!/bin/bash
set -e
# Commit C: launchd 进程迁移脚本
# 原子操作：杀旧 hermes 进程 → bootstrap serenity launchd → kickstart

OLD_PID=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$OLD_PID" ]; then
  echo "→ 终止旧进程 PID=$OLD_PID"
  kill -9 "$OLD_PID"
  sleep 1
fi

echo "→ bootstrap com.serenity.dashboard"
launchctl bootstrap "gui/$(id -u)" /Users/mac/Library/LaunchAgents/com.serenity.dashboard.plist

echo "→ kickstart"
launchctl kickstart -k "gui/$(id -u)/com.serenity.dashboard"

sleep 3
echo "→ 确认 launchd 状态"
launchctl print "gui/$(id -u)/com.serenity.dashboard" | head -12

echo ""
echo "→ 健康检查"
bash /Users/mac/workspace/SerenityMonitor/scripts/health_check.sh
