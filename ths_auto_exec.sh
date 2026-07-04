#!/bin/bash
# ths_auto_exec.sh — 同花顺自动下单 (macOS AppleScript)
# 读取 ~/.hermes/ths_orders/ 下的 pending 订单, 模拟键盘操作填单
# 用法: bash ths_auto_exec.sh [--dry-run]

ORDERS_DIR="$HOME/.hermes/ths_orders"
ARCHIVE_DIR="$ORDERS_DIR/executed"

mkdir -p "$ARCHIVE_DIR"

DRY_RUN=false
[[ "$1" == "--dry-run" ]] && DRY_RUN=true

COUNT=0
for f in "$ORDERS_DIR"/*.json; do
    [[ ! -f "$f" ]] && continue

    # 解析订单
    CODE=$(python3 -c "import json; print(json.load(open('$f'))['code'])")
    ACTION=$(python3 -c "import json; print(json.load(open('$f'))['action'])")
    PRICE=$(python3 -c "import json; print(json.load(open('$f'))['price'])")
    SHARES=$(python3 -c "import json; print(json.load(open('$f'))['shares'])")

    if $DRY_RUN; then
        echo "🧪 DRY RUN: $ACTION $CODE @¥$PRICE x $SHARES 股"
        mv "$f" "$ARCHIVE_DIR/"
        COUNT=$((COUNT + 1))
        continue
    fi

    echo "📋 执行: $ACTION $CODE ¥$PRICE x $SHARES 股"

    # AppleScript: 激活同花顺 → 按 F1 买入/F2 卖出 → 输入代码 → Tab → 价格 → Tab → 数量 → Enter
    if [[ "$ACTION" == "buy" ]]; then
        KEY="1"  # 同花顺 F1=买入面板
    else
        KEY="2"  # 同花顺 F2=卖出面板
    fi

    osascript <<EOSCRIPT
tell application "同花顺" to activate
delay 1
-- 打开交易面板 (F1=买 F2=卖)
tell application "System Events"
    tell process "同花顺"
        keystroke "$CODE"
        delay 0.3
        keystroke tab
        delay 0.3
        keystroke "$PRICE"
        delay 0.3
        keystroke tab
        delay 0.3
        keystroke "$SHARES"
        delay 0.5
        -- 不自动回车, 需人工最后确认
        display dialog "请在 同花顺 确认下单:\\n\\n$ACTION $CODE\\n价格: ¥$PRICE\\n数量: $SHARES 股\\n\\n确认后点 OK 继续" buttons {"OK"} default button "OK"
    end tell
end tell
EOSCRIPT

    # 归档已处理的订单
    mv "$f" "$ARCHIVE_DIR/"
    COUNT=$((COUNT + 1))
    echo "  ✅ $CODE 已处理"
    sleep 2
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 处理完成: $COUNT 笔订单"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━"
