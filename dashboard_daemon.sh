#!/bin/bash
# Serenity 监控看板守护 — 工作日盘中自动拉起
# 由 Hermes cron 每15分钟检查一次（工作日 9:00-15:00）
# 职责：确保看板 + Cloudflare Tunnel 公网隧道始终在线
set -e

PORT=8401
SCRIPT_DIR="/Users/mac/workspace/SerenityMonitor"
HERMES_PYTHON="/Users/mac/.hermes/hermes-agent/.venv/bin/python"
TUNNEL_BIN="/opt/homebrew/bin/cloudflared"
LOGFILE="$SCRIPT_DIR/logs/dashboard_daemon.log"
TUNNEL_LOG="$SCRIPT_DIR/logs/cloudflared.log"
URL_FILE="$SCRIPT_DIR/.serenity_public_url"

mkdir -p "$SCRIPT_DIR/logs"

# ---- 日志轮转 ----
MAX_LOG_LINES=2000
for f in "$LOGFILE" "$TUNNEL_LOG"; do
    if [ -f "$f" ] && [ "$(wc -l < "$f")" -gt $MAX_LOG_LINES ]; then
        tail -n 500 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    fi
done

# ---- 启动看板 ----
start_dashboard() {
    if ! lsof -i :$PORT -sTCP:LISTEN > /dev/null 2>&1; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') 看板未运行，启动中..." >> "$LOGFILE"
        cd "$SCRIPT_DIR"
        nohup "$HERMES_PYTHON" monitoring_dashboard.py >> "$SCRIPT_DIR/logs/serenity.log" 2>&1 &
        disown  # 彻底脱离父进程，防止 cron/terminal 退出时 SIGTERM
        echo "$(date '+%Y-%m-%d %H:%M:%S') 看板已启动 (PID $!)" >> "$LOGFILE"
        sleep 3
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') 看板运行中 ✓" >> "$LOGFILE"
    fi
}

# ---- 启动 Cloudflare Tunnel ----
start_tunnel() {
    local existing
    existing=$(pgrep -f "cloudflared tunnel.*8401" 2>/dev/null || true)
    if [ -n "$existing" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') tunnel 运行中 (PID $existing)" >> "$LOGFILE"
        return 0
    fi

    echo "$(date '+%Y-%m-%d %H:%M:%S') 启动 Cloudflare Tunnel..." >> "$LOGFILE"
    nohup "$TUNNEL_BIN" tunnel --url http://127.0.0.1:$PORT --no-autoupdate >> "$TUNNEL_LOG" 2>&1 < /dev/null &
        disown  # 彻底脱离父进程
    echo "$(date '+%Y-%m-%d %H:%M:%S') tunnel 已启动 (PID $!)" >> "$LOGFILE"

    # 等待 URL 出现（最多等 30 秒）
    for i in $(seq 1 30); do
        sleep 2
        local url
        url=$(grep -o 'https://[^ ]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1 || true)
        if [ -n "$url" ]; then
            echo "$url" > "$URL_FILE"
            echo "$(date '+%Y-%m-%d %H:%M:%S') tunnel 就绪 → $url" >> "$LOGFILE"
            return 0
        fi
    done
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️ tunnel URL 获取超时" >> "$LOGFILE"
}

# ---- 主流程 ----
start_dashboard
# Cloudflare 隧道已切换为 ngrok（`com.ngrok.hermes-gateway` 管理），不再启动 cloudflared
echo "$(date '+%Y-%m-%d %H:%M:%S') 隧道由 ngrok 管理 (serenity-bridge)" >> "$LOGFILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') 守护检查完成" >> "$LOGFILE"
