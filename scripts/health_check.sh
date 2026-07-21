#!/bin/bash
# Serenity 看板健康检查 — R0.4 五项验收
# 用法: scripts/health_check.sh [port]（默认 8401）
# 退出码: 0 = 全部通过；非 0 = 失败项数

set -u
PORT="${1:-8401}"
BASE="http://localhost:${PORT}"
CURL="curl -s --connect-timeout 3 --max-time 10"
FAIL=0

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

echo "── Serenity 健康检查 (port ${PORT}) ──"

# 1. /monitor → 200 且含有效 data-theme（legacy / terminal-noir）
THEME=$($CURL "${BASE}/monitor" | grep -o 'data-theme="[^"]*"' | head -1 | grep -o '"[^"]*"' | tr -d '"' || echo "")
if [ "$THEME" = "legacy" ] || [ "$THEME" = "terminal-noir" ]; then
  pass "/monitor 渲染正常 (data-theme=${THEME})"
else
  fail "/monitor 无响应或 data-theme 无效 (got: ${THEME:-none})"
fi

# 2. theme-legacy.css → 200
CSS_CODE=$($CURL -o /dev/null -w '%{http_code}' "${BASE}/static/css/theme-legacy.css")
if [ "$CSS_CODE" = "200" ]; then
  pass "theme-legacy.css 可访问 (200)"
else
  fail "theme-legacy.css 返回 ${CSS_CODE}"
fi

# 3. /api/monitor-data → 200 + no-store 头 + 关键字段结构
API_RESP=$($CURL "${BASE}/api/monitor-data")
if echo "$API_RESP" | jq -e '.data.portfolio_summary and .data.timestamp' >/dev/null 2>&1; then
  pass "/api/monitor-data 结构正确 (portfolio_summary + timestamp)"
else
  fail "/api/monitor-data 结构异常或无响应"
fi
if $CURL -I "${BASE}/api/monitor-data" | grep -qi 'cache-control: no-store'; then
  pass "/api/** 响应头含 Cache-Control: no-store"
else
  fail "/api/** 缺少 no-store 头"
fi

# 4. 进程校验：监听者必须是项目 venv 解释器
LISTEN_PID=$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | head -1)
if [ -n "$LISTEN_PID" ]; then
  PROC_CMD=$(ps -p "$LISTEN_PID" -o command= 2>/dev/null)
  if echo "$PROC_CMD" | grep -q '/Users/mac/workspace/SerenityMonitor/.venv/bin/python'; then
    pass "监听进程 (PID ${LISTEN_PID}) 使用项目 venv"
  else
    fail "监听进程解释器异常: ${PROC_CMD}"
  fi
else
  fail "端口 ${PORT} 无监听进程"
fi

# 5. launchd 为唯一管理者（服务已加载且非 exit 1 循环）
if launchctl print "gui/$(id -u)/com.serenity.dashboard" 2>/dev/null | grep -q 'state = running'; then
  pass "launchd 服务 running"
else
  fail "launchd 服务未加载或未运行"
fi

echo "──────────────────────────────"
if [ "$FAIL" -eq 0 ]; then
  echo "全部通过 ✅"
else
  echo "${FAIL} 项失败 ❌"
fi
exit "$FAIL"
