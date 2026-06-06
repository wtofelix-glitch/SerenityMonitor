# Codex Task: 完善 Serenity Monitor

## 背景
Serenity Monitor 是 A 股多因子评分系统（5 维：CPO 对齐/瓶颈/AIcapex/护城河/动量）。
项目路径：`~/workspace/SerenityMonitor/`
数据库已定义在 `db.py` 但从未初始化。

## 任务

### 1. 修复 DB 路径并初始化
- `db.py` 第 9 行 `DB_PATH` 指向 `serenity.db`，但已有空文件 `data/monitor.db`
- 统一为 `data/monitor.db`（修改 `db.py` 第 9 行）
- 运行 `init_db()` 创建所有表结构（stocks, daily_snapshots, trades, alerts 等）
- 预填充 5 只主板标的（仅限 000/002/600/601/603/605）：
  - 剑桥科技 603083 — CPO 光模块（已持仓，成本 204.59，止损 188）
  - 士兰微 600460 — 功率半导体（已持仓，成本 35.65，止损 32.8）
  - 有研新材 600206 — InP 衬底国产替代
  - 北方华创 002371 — 设备龙头
  - 中际旭创 300308 — ⚠️ 这是创业板，跳过。换成：长电科技 600584 — 封测瓶颈

### 2. 实现仪表盘"调仓"功能
- `monitoring_dashboard.py` 第 201 行：`onclick="alert('调仓功能待实现')"` 
- 替换为实际调仓界面：
  - 显示当前持仓列表 + 评分
  - 提供买入/卖出按钮
  - 通过 `/api/trades` POST 接口提交
- 在 Flask app 中新增 `/api/trades` 路由（如果不存在）

### 3. 实现仪表盘"设置"功能
- `monitoring_dashboard.py` 第 202 行：`onclick="alert('设置功能待实现')"`
- 替换为简单设置面板：
  - 止损线调整
  - 止盈目标调整
  - 通过 `/api/config` 接口读写

## 约束
- **不修改交易/仓位计算核心逻辑**
- **不引入新依赖**
- **仅限主板标的**（000/002/600/601/603/605），禁止创业板(300/301)和科创板(688)
- CLI 命令保持在 `cli.py` 的 commands dict 中
- 数据库操作通过 `db.py` 封装的函数
- 现有 Flask app 在 `monitoring_dashboard.py`，端口 8400

## 预期产出
1. `db.py` — DB_PATH 修复
2. 数据库初始化完成（含预填充）
3. `monitoring_dashboard.py` — 调仓和设置功能
4. 验证：`python3 cli.py status` 能正常显示
