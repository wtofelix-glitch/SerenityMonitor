#!/bin/bash
# 交易日志 cron 脚本 — 每日收盘后沉淀到 gbrain/本地
cd /Users/mac/workspace/SerenityMonitor
arch -arm64 python3 -c "
from trading_log_sync import sync_today
result = sync_today()
data = result.get('data', result)
print(f'✅ 交易日志已沉淀 ({data.get(\"date\",\"?\")}): {data.get(\"summary\",{}).get(\"total_positions\",0)}只持仓, {data.get(\"summary\",{}).get(\"total_signals\",0)}条信号')
"
