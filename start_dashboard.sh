#!/bin/bash
# Serenity Monitor — 手动启动（看板 + Cloudflare Tunnel）
# 自动启动由 Hermes cron “Serenity 看板守护”负责

bash "$(dirname "$0")/dashboard_daemon.sh"

if [ -f "$(dirname "$0")/.serenity_public_url" ]; then
    URL="$(cat "$(dirname "$0")/.serenity_public_url")"
    echo ""
    echo "✅ Serenity 看板已启动"
    echo "   手机访问: $URL/monitor"
fi
