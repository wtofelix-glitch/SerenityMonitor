#!/bin/bash
# 交易日志 cron 脚本 — 每日收盘后沉淀到 gbrain/本地
SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
cd "$SCRIPT_DIR" || exit 1

# ── A股交易日检查：非交易日静默退出 ──
python3 -c "from check_trading_day import require_trading_day; require_trading_day()" 2>/dev/null || exit 0
arch -arm64 python3.13 -c "
from trading_log_sync import sync_today
result = sync_today()
data = result.get('data', result)
print(f'✅ 交易日志已沉淀 ({data.get(\"date\",\"?\")}): {data.get(\"summary\",{}).get(\"total_positions\",0)}只持仓, {data.get(\"summary\",{}).get(\"total_signals\",0)}条信号')
"
