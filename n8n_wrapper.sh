#!/usr/bin/env bash
# n8n_wrapper.sh — n8n Execute Command 节点统一入口
# n8n 调用: bash /data/serenity/n8n_wrapper.sh <task> [args...]
set -euo pipefail

TASK="${1:-}"
shift || true

cd /data/serenity

case "$TASK" in
  fetch-history)
    # 每日历史数据拉取
    python3 fetch_history.py "$@"
    ;;
  rescore)
    # 多因子评分
    python3 cli.py rescore "$@"
    ;;
  adjust-weights)
    # 动态权重调整
    python3 cli.py adjust-weights "$@"
    ;;
  factor-report)
    # 因子报告
    python3 cli.py factor-report "$@"
    ;;
  daily-report)
    # 生成日报
    python3 daily_report.py "$@"
    ;;
  daily-workflow)
    # 完整每日工作流（包含评分+信号+推送）
    python3 daily_workflow.py "$@"
    ;;
  record-real-data)
    # 真实行情记录 + 自动闸门结算
    python3 cli.py record-real-data "$@"
    ;;
  auto-gate)
    # 查看自动交易统计闸门
    python3 cli.py auto-gate "$@"
    ;;
  status)
    # 系统状态
    python3 cli.py status
    ;;
  *)
    echo "Usage: $0 {fetch-history|rescore|adjust-weights|factor-report|daily-report|daily-workflow|record-real-data|auto-gate|status}"
    echo ""
    echo "Available tasks:"
    echo "  fetch-history     拉取全部标的历史K线数据"
    echo "  rescore           多因子评分重算"
    echo "  adjust-weights    动态权重调整"
    echo "  factor-report     因子表现报告"
    echo "  daily-report      生成收盘日报"
    echo "  daily-workflow    完整每日工作流"
    echo "  record-real-data  真实行情记录+闸门结算"
    echo "  auto-gate         自动交易统计闸门"
    echo "  status            系统状态查询"
    exit 1
    ;;
esac
