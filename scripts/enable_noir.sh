#!/bin/bash
# Commit F: 启用 Terminal Noir 主题
# 完整流程: bootout 旧服务 → 终止 hermes 进程 → bootstrap 新配置 → kickstart → 验证

set -e
PLIST="$HOME/Library/LaunchAgents/com.serenity.dashboard.plist"
SERVICE="gui/$(id -u)/com.serenity.dashboard"

echo "=== R1 Terminal Noir 启用 ==="

# 1. bootout 旧服务（如果已加载）
echo "→ 1. bootout 旧服务"
launchctl bootout "gui/$(id -u)/com.serenity.dashboard" 2>/dev/null || echo "   (服务未加载，跳过)"

# 2. 终止非 launchd 管理的进程（hermes venv 自动重启的）
echo "→ 2. 终止 hermes 进程"
sleep 1
# 循环终止，防止 hermes 自动重启抢端口
for i in 1 2 3; do
  PID=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null || true)
  if [ -z "$PID" ]; then
    echo "   端口已释放"
    break
  fi
  CMD=$(ps -p "$PID" -o command= 2>/dev/null || true)
  echo "   终止 PID=$PID ($CMD)"
  kill -9 "$PID" 2>/dev/null || true
  sleep 1
done

# 3. 确认端口空闲
PID=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PID" ]; then
  echo "❌ 端口 8401 仍被 PID=$PID 占用，无法继续"
  exit 1
fi
echo "   ✅ 端口 8401 已释放"

# 4. bootstrap 新配置（含 SERENITY_THEME=terminal-noir）
echo "→ 3. bootstrap 新 plist"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "   ✅ bootstrap 完成"

# 5. kickstart
echo "→ 4. kickstart 启动"
launchctl kickstart -k "$SERVICE"

# 6. 等待并验证
echo "→ 5. 等待服务启动..."
for i in 1 2 3 4 5; do
  sleep 1
  if curl -s http://localhost:8401/monitor >/dev/null 2>&1; then
    break
  fi
  echo "   等待中... ($i/5)"
done

echo ""
echo "=== 验证结果 ==="
THEME=$(curl -s http://localhost:8401/monitor | grep -o 'data-theme="[^"]*"' || echo "获取失败")
echo "  Theme: $THEME"

LISTENER=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null | head -1)
if [ -n "$LISTENER" ]; then
  LISTENER_CMD=$(ps -p "$LISTENER" -o command= 2>/dev/null)
  echo "  监听进程: PID=$LISTENER $LISTENER_CMD"
fi

if echo "$THEME" | grep -q 'terminal-noir'; then
  echo "✅ Terminal Noir 已启用"
else
  echo "❌ 主题未切换到 noir，当前: $THEME"
fi
