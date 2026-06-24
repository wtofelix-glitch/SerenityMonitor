#!/bin/bash
# Serenity Monitor — 手动启动（看板 + Cloudflare Tunnel）
# 自动启动由 Hermes cron "Serenity 看板守护"负责

set -euo pipefail

# ── launchd.log 轮转 (>5MB 时 rotate) ──────────────────
LOGFILE="$(dirname "$0")/launchd.log"
MAX_SIZE_MB=5
if [ -f "$LOGFILE" ]; then
    SIZE=$(stat -f%z "$LOGFILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt $((MAX_SIZE_MB * 1048576)) ]; then
        mv "$LOGFILE" "$LOGFILE.old"
        echo "--- launchd.log rotated $(date) ---" > "$LOGFILE"
    fi
fi

bash "$(dirname "$0")/dashboard_daemon.sh"

if [ -f "$(dirname "$0")/.serenity_public_url" ]; then
    URL="$(cat "$(dirname "$0")/.serenity_public_url")"
    echo ""
    echo "✅ Serenity 看板已启动"
    echo "   手机访问: $URL/monitor"
fi
