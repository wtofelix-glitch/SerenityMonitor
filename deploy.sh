#!/bin/bash
# SerenityMonitor 一键部署检查
# 所有定时任务已由 Hermes Gateway 管理，本脚本仅作健康检查
# 运行: bash deploy.sh

SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
PYTHON="/Users/mac/workspace/SerenityMonitor/.venv/bin/python3"

echo "🚀 SerenityMonitor 部署检查"
echo "============================"

echo ""
echo "📋 Hermes Cron 任务:"
hermes cron list 2>/dev/null | grep -i serenity | wc -l | xargs echo "  共"

echo ""
echo "🧪 系统健康检查..."
cd "$SCRIPT_DIR" && $PYTHON health_check.py 2>/dev/null

echo ""
echo "🔄 T1 回补检查:"
cd "$SCRIPT_DIR" && $PYTHON tier1_reentry.py --status 2>/dev/null

echo ""
echo "📡 看板状态:"
if lsof -i :8401 -sTCP:LISTEN > /dev/null 2>&1; then
    echo "  ✅ 看板运行中 → http://localhost:8401/monitor"
else
    echo "  ⚠️ 看板未运行，启动中..."
    bash "$SCRIPT_DIR/dashboard_daemon.sh"
fi

echo ""
echo "✅ 部署检查完成"
echo "   手动命令: cd $SCRIPT_DIR && python3 cli.py <command>"
echo "   启动看板: bash $SCRIPT_DIR/start_dashboard.sh"
