#!/bin/bash
# Commit F: 启用 Terminal Noir 主题（v3 — 竞速版）
# kill + bootstrap 零间隔执行，利用 KeepAlive 赢回端口
set -e
PLIST="$HOME/Library/LaunchAgents/com.serenity.dashboard.plist"
SERVICE="gui/$(id -u)/com.serenity.dashboard"

echo "=== R1 Terminal Noir 启用 v3 ==="

# 1. bootout
echo "→ bootout"
launchctl bootout "$SERVICE" 2>/dev/null || true

# 2. kill + bootstrap 原子操作（不检查端口空闲，直接抢）
echo "→ kill + bootstrap"
PID=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null || true)
[ -n "$PID" ] && kill -9 "$PID" 2>/dev/null || true
# 立即 bootstrap，不等待端口释放 — launchd KeepAlive 会重试
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 3. 多次 kickstart 直到 launchd 赢得端口
echo "→ kickstart（重试直到 launchd 绑定端口）"
for i in $(seq 1 8); do
  sleep 2
  launchctl kickstart -k "$SERVICE" 2>/dev/null || true

  # 检查是否 launchd 的进程在监听
  LISTENER=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null | head -1)
  if [ -n "$LISTENER" ]; then
    CMD=$(ps -p "$LISTENER" -o command= 2>/dev/null)
    if echo "$CMD" | grep -q '/Users/mac/workspace/SerenityMonitor/.venv/bin/python'; then
      echo "   ✅ launchd 已绑定端口 (PID=$LISTENER)"
      break
    else
      echo "   端口被 $CMD 占用，继续重试..."
      kill -9 "$LISTENER" 2>/dev/null || true
    fi
  fi
done

# 4. 验证
sleep 2
THEME=$(curl -s http://localhost:8401/monitor | grep -o 'data-theme="[^"]*"' || echo "获取失败")
echo ""
echo "=== 验证 ==="
echo "  Theme: $THEME"

LISTENER=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null | head -1)
CMD=$(ps -p "$LISTENER" -o command= 2>/dev/null || true)
echo "  进程: $CMD"

if echo "$THEME" | grep -q 'terminal-noir'; then
  echo "✅ Terminal Noir 已启用"
else
  echo "❌ 主题: $THEME (launchd 可能未读取到环境变量)"
fi
