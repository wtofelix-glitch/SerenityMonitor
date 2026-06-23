#!/bin/bash
# Serenity 每日 Rank IC 评估
# 由 cron 调度（16:20 周一至周五），在 fetch_history(16:00) + rescore(16:15) 之后执行
SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
cd "$SCRIPT_DIR" || exit 1

# ── A股交易日检查：非交易日静默退出 ──
python3 -c "from check_trading_day import require_trading_day; require_trading_day()" 2>/dev/null || exit 0

/Users/mac/.hermes/hermes-agent/.venv/bin/python3 factor_ic.py --json 2>&1
