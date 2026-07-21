#!/bin/bash
# Commit F: Terminal Noir 回滚演练
# 从 plist 移除 SERENITY_THEME → bootout → bootstrap → kickstart → 验证恢复 legacy

set -e
PLIST="$HOME/Library/LaunchAgents/com.serenity.dashboard.plist"
SERVICE="gui/$(id -u)/com.serenity.dashboard"

echo "=== R1 回滚演练 — 恢复到 legacy ==="

# 1. 备份当前 plist
cp "$PLIST" "${PLIST}.noir-backup"
echo "→ plist 已备份到 ${PLIST}.noir-backup"

# 2. 从 plist 移除 EnvironmentVariables 键
echo "→ 移除 SERENITY_THEME 环境变量"
/usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables" "$PLIST" 2>/dev/null || echo "   (已不存在)"
plutil -lint "$PLIST"

# 3. bootout
echo "→ bootout 当前服务"
launchctl bootout "$SERVICE" 2>/dev/null || echo "   (服务未加载)"

# 4. 杀端口（防止 hermes 自动重启）
sleep 1
for i in 1 2 3; do
  PID=$(lsof -tiTCP:8401 -sTCP:LISTEN 2>/dev/null || true)
  if [ -z "$PID" ]; then break; fi
  kill -9 "$PID" 2>/dev/null || true
  sleep 1
done

# 5. bootstrap
echo "→ bootstrap（不含 SERENITY_THEME）"
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 6. kickstart
echo "→ kickstart"
launchctl kickstart -k "$SERVICE"

# 7. 验证
sleep 3
THEME=$(curl -s http://localhost:8401/monitor | grep -o 'data-theme="[^"]*"' || echo "获取失败")
echo ""
echo "=== 回滚验证 ==="
echo "  Theme: $THEME"

if echo "$THEME" | grep -q 'legacy'; then
  echo "✅ 回滚成功 — data-theme 已恢复为 legacy"
else
  echo "❌ 回滚异常 — 当前: $THEME"
fi
