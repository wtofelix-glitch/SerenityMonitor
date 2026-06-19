# Serenity Monitor 三线增强实施计划

## 环境约束

- **Python**: `/Users/mac/miniconda3/bin/python3`（必须！系统 Python 3.9 numpy arm64/x86_64 冲突）
- **代码库**: `~/workspace/SerenityMonitor/`
- **Dashbaord 管理**: launchd 守护 `com.serenity.dashboard`，修改代码后必须 unload/load
- **部署命令**: `launchctl unload ~/Library/LaunchAgents/com.serenity.dashboard.plist && launchctl load ~/Library/LaunchAgents/com.serenity.dashboard.plist`
- **语法验证**: `cd ~/workspace/SerenityMonitor && /Users/mac/miniconda3/bin/python3 -c "import monitoring_dashboard"`

---

## 任务 1: Dashboard 2.0 — 毛玻璃 UI 翻新 + 移动端适配

**文件**: `static/css/monitor.css`

在 :root 中新增毛玻璃设计令牌:

```css
--glass-bg: rgba(30,34,48,0.75);
--glass-border: rgba(255,255,255,0.10);
--glass-blur: 16px;
--glass-shadow: 0 8px 32px rgba(0,0,0,0.35);
--glass-shine: linear-gradient(135deg, rgba(255,255,255,0.05) 0%, transparent 50%);
```

所有卡片类(.kpi-card, .tab-content, .glass-card 等)添加:
```css
backdrop-filter: blur(var(--glass-blur));
-webkit-backdrop-filter: blur(16px);
background: var(--glass-bg);
border: 1px solid var(--glass-border);
box-shadow: var(--glass-shadow);
```

卡片顶部光泽:
```css
.glass-card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 50%;
  background: linear-gradient(135deg, rgba(255,255,255,0.06) 0%, transparent 50%);
  pointer-events: none;
  border-radius: inherit;
}
```

移动端增强:
- 最小触摸目标 44px (Apple HIG)
- 底部安全区: `padding-bottom: env(safe-area-inset-bottom)`
- 增大部分字号 (12px→14px, 14px→16px)
- KPI 数字用等宽数字: `font-variant-numeric: tabular-nums`
- 卡片间距增大 8px→12px

**文件**: `templates/monitor.html`
- 6个 KPI 卡片包裹在 `<div class="glass-panel">` 容器中
- 每个 kpi-card 添加 `glass-card` 类

**验证**: 浏览器打开 http://localhost:8401/monitor，手机/Chrome DevTools 移动视图查看效果

---

## 任务 2: NL 查询 API

**文件**: `monitoring_dashboard.py`

在 Quick APIs 附近（~line 1092）新增路由:

```python
@app.route("/api/nl-query")
def api_nl_query():
    """自然语言查询 — '今天谁该卖了' / '持仓盈亏' / '有什么警报'"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "缺少 q 参数，示例: /api/nl-query?q=今天谁该卖了"})

    intent = _nl_parse_intent(q)
    try:
        data = _nl_execute(intent)
        return jsonify({"ok": True, "intent": intent["type"], "query": q, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "intent": intent["type"], "error": str(e)})
```

意图识别（关键字匹配，不依赖 LLM）:

| intent | keywords | data source |
|--------|----------|-------------|
| sell_check | 卖/出/清仓/减仓/止损/跑了 | generate_execution_plan() sells |
| buy_candidates | 买/进/建仓/加仓/推荐/选股 | generate_execution_plan() buys |
| pnl_check | 亏/赚/盈亏/收益/成本 | DB stocks query |
| alert_check | 预警/警报/风险/跌/暴跌/踩雷 | get_unacknowledged_anomalies() |
| position_status | 持/仓位/配置/分散 | _get_position_advice() |
| market_status | 大盘/市场/行情/择时 | get_market_signal() |

`_nl_parse_intent(q)` 实现: 遍历关键词列表，返回第一个匹配 intent + 置信度。

`_nl_execute(intent)` 实现: switch intent type，调用对应数据函数，返回结构化 JSON。

返回格式示例:
```json
{"ok": true, "intent": "sell_check", "query": "今天谁该卖了",
 "data": {"sells": [{"code": "600141", "name": "兴发集团"}], "summary": "1个卖出候选"}}
```

**验证**: 
- `curl "http://localhost:8401/api/nl-query?q=今天谁该卖了"`
- `curl "http://localhost:8401/api/nl-query?q=有没有预警"`
- `curl "http://localhost:8401/api/nl-query?q=持仓盈亏"`

---

## 任务 3: 数据源扩展 — AKShare 备用行情源

**前置**: `pip install akshare` (用 miniconda3 pip)

**文件**: `data_engine.py`

新增函数:
```python
def akshare_fetch_realtime(code_list):
    """AKShare 东方财富备用行情源"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    results = []
    code_set = set(code_list)
    for _, row in df.iterrows():
        code = str(row['代码'])
        if code in code_set:
            results.append({
                "code": code, "name": row['名称'],
                "price": float(row['最新价']),
                "change_pct": float(row['涨跌幅']),
                "volume": int(row['成交量']), "amount": float(row['成交额']),
                "high": float(row['最高']), "low": float(row['最低']),
                "open": float(row['今开']), "close_yesterday": float(row['昨收']),
            })
    return results
```

修改 `fetch_realtime()` 支持多数据源 fallback:
```python
def fetch_realtime(code_list=None, sources=None):
    if sources is None:
        sources = ["sina", "akshare"]
    for source in sources:
        try:
            if source == "sina":
                return _extract_sina_data(code_list)  # 重构现有逻辑
            elif source == "akshare":
                return akshare_fetch_realtime(code_list)
        except Exception as e:
            log.warning(f"{source} failed: {e}")
    return []
```

**文件**: `monitoring_dashboard.py`

新增数据源状态端点:
```python
@app.route("/api/data-source-status")
def api_data_source_status():
    return jsonify({"sources": [
        {"name": "sina", "status": "ok"},
        {"name": "akshare", "status": "ok"},
    ]})
```

**验证**: `curl http://localhost:8401/api/data-source-status`

---

## 实施顺序

1. **数据源扩展**（底层改动，优先）
2. **NL 查询 API**（依赖数据层就位）
3. **Dashboard 2.0 UI**（CSS 独立，放最后）

## 最终验证

```bash
# 语法验证
cd ~/workspace/SerenityMonitor && /Users/mac/miniconda3/bin/python3 -c "import monitoring_dashboard; print('OK')"

# API 验证
curl http://localhost:8401/api/nl-query?q=今天谁该卖了
curl http://localhost:8401/api/nl-query?q=有没有预警
curl http://localhost:8401/api/data-source-status

# 重载服务
launchctl unload ~/Library/LaunchAgents/com.serenity.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.serenity.dashboard.plist

# 再次验证 API
sleep 3 && curl http://localhost:8401/api/quick | python3 -m json.tool | head -5
```
