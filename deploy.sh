#!/bin/bash
# SerenityMonitor 一键部署 — cron 定时任务设置
# 运行: bash deploy.sh

SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
PYTHON="$(which python3)"

echo "🚀 SerenityMonitor 部署"
echo "========================"

# 检查 crontab 是否已有 serenity 任务
EXISTING=$(crontab -l 2>/dev/null | grep -c "serenity\|Serenity" || true)
if [ "$EXISTING" -gt 0 ]; then
    echo "⚠️  已有 $EXISTING 条 serenity cron 任务，跳过 crontab 设置"
    echo "   如需重置: crontab -l | grep -v serenity | crontab -"
else
    # 添加每日工作流 (16:30 收盘后)
    (crontab -l 2>/dev/null; echo "30 16 * * 1-5 cd $SCRIPT_DIR && $PYTHON daily_workflow.py --push >> serenity_cron.log 2>&1") | crontab -
    echo "✅ 每日工作流: 工作日 16:30"
    
    # 添加盘中监控 (每30分钟)
    (crontab -l 2>/dev/null; echo "*/30 9-15 * * 1-5 cd $SCRIPT_DIR && $PYTHON -c 'from monitor import monitor_all; monitor_all()' >> serenity_cron.log 2>&1") | crontab -
    echo "✅ 盘中监控: 工作日 9:00-15:00 每30分钟"
    
    # 添加价格警报 (每小时)
    (crontab -l 2>/dev/null; echo "0 10,13,14 * * 1-5 cd $SCRIPT_DIR && $PYTHON dashboard.py >> serenity_cron.log 2>&1") | crontab -
    echo "✅ 价格警报: 工作日 10:00, 13:00, 14:00"
fi

echo ""
echo "📋 当前 cron 任务:"
crontab -l | grep -i serenity || echo "  (无)"

echo ""
echo "🧪 运行首次健康检查..."
cd "$SCRIPT_DIR" && $PYTHON health_check.py 2>/dev/null | tail -5

echo ""
echo "✅ 部署完成"
echo "   CLI 命令: cd $SCRIPT_DIR && python3 cli.py <command>"
