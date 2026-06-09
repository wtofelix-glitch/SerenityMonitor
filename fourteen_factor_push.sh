#!/bin/bash
# 14因子独立信号推送包装脚本
# 推送逻辑：微信优先 → 微信不通则走 Telegram
# cron 入口: 静默模式（无买入信号时不输出）
# 手动运行: bash fourteen_factor_push.sh

HERMES_BIN="/Users/mac/.local/bin/hermes"
HERMES_PYTHON="/Users/mac/.hermes/hermes-agent/.venv/bin/python3"
cd /Users/mac/workspace/SerenityMonitor

# 生成信号简报
OUTPUT=$($HERMES_PYTHON fourteen_factor_push.py --silent 2>&1)
EXIT_CODE=$?

# 无输出（无买入信号）→ 静默退出
if [ $EXIT_CODE -ne 0 ] || [ -z "$OUTPUT" ]; then
    exit 0
fi

# 输出内容到stdout（cron本地日志）
echo "$OUTPUT"
echo ""

# 微信优先推送
WX_RESULT=$($HERMES_BIN send --to weixin "$OUTPUT" 2>&1)
WX_OK=$?

if [ $WX_OK -eq 0 ]; then
    echo "[推送] ✅ 微信推送成功"
    exit 0
fi

echo "[推送] ⚠️ 微信推送失败: $WX_RESULT"
echo "[推送] ↪ 降级走 Telegram..."

# 降级 → Telegram
TG_RESULT=$($HERMES_BIN send --to telegram:8703799832 "$OUTPUT" 2>&1)
TG_OK=$?

if [ $TG_OK -eq 0 ]; then
    echo "[推送] ✅ Telegram 推送成功（降级）"
else
    echo "[推送] ❌ Telegram 也失败: $TG_RESULT"
    exit 1
fi
