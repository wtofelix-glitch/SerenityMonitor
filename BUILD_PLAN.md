# Serenity Monitor — 项目构建任务

## 项目概况
基于 Serenity（@aleabitoreddit）供应链瓶颈投资框架，构建一个A股监控小程序。

## 项目路径
~/workspace/SerenityMonitor/

## 技术栈
- Python 3.9+（系统默认）
- SQLite（轻量级持久化）
- Flask（移动端网页仪表盘）
- Sina Finance API（A股实时/历史数据，无需代理）
- Hermes cron + WeChat通知

## 核心功能

### 1. 数据引擎（stock_fetcher.py）
使用新浪API获取A股数据（不需要akshare，已测试可直接调用）：
```python
# 实时行情
url = 'http://hq.sinajs.cn/list=sh600519'
# 日K历史数据
url = 'http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/...'
```
必须支持：
- 单个股票实时行情（价格、涨跌幅、成交量、换手率）
- 日K线数据（过去60个交易日）
- 不需要代理（不通代理直连）

### 2. 数据库（db.py）
SQLite，路径 `~/workspace/SerenityMonitor/data/monitor.db`

**表结构：**

```sql
-- 候选标的（来自Serenity报告）
CREATE TABLE candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,          -- 代码，如 '300782'
    name TEXT NOT NULL,                 -- 名称，如 '源杰科技'
    market TEXT DEFAULT 'sz',           -- sh/sz/bj
    tier INTEGER DEFAULT 1,            -- 1=高度匹配, 2=部分匹配, 3=长期观察
    rationale TEXT,                     -- 匹配逻辑简述
    target_buy_low REAL,               -- 建议买入区间下限
    target_buy_high REAL,              -- 建议买入区间上限
    target_sell_low REAL,              -- 建议卖出区间下限
    target_sell_high REAL,             -- 建议卖出区间上限
    status TEXT DEFAULT 'pending',     -- pending|holding|sold|watching
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 持仓记录
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    buy_price REAL NOT NULL,           -- 买入价格
    buy_date TEXT NOT NULL,            -- 买入日期 YYYY-MM-DD
    quantity INTEGER DEFAULT 0,        -- 持仓数量
    sell_price REAL,                   -- 卖出价格
    sell_date TEXT,                    -- 卖出日期
    profit_pct REAL,                   -- 收益率 %
    status TEXT DEFAULT 'holding',     -- holding|sold
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 每日快照
CREATE TABLE daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    open REAL, close REAL, high REAL, low REAL,
    volume REAL,                        -- 成交量
    change_pct REAL,                    -- 涨跌幅 %
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(candidate_id, date)
);

-- 警报记录
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    type TEXT NOT NULL,                 -- 'daily_report'|'price_target'|'sell_signal'
    message TEXT NOT NULL,
    triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered INTEGER DEFAULT 0
);
```

### 3. 预填充候选标的
使用以下标的（从Serenity框架A股分析报告提取）：

**Tier 1（高度匹配）：**
1. 源杰科技（300782）— 光芯片，国产化率4%，对标$SIVE
2. 天孚通信（300394）— 光器件，光模块的"卖铲子的人"
3. 中科飞测（688361）— 测试设备，72小时测试收费站

**Tier 2（部分匹配）：**
4. 中际旭创（300308）— 全球光模块龙头
5. 有研新材（600206）— InP衬底国产替代
6. 圣邦股份（300661）— PMIC隐形瓶颈
7. 光迅科技（300281）— 光芯片整链

**Tier 3（长期观察）：**
8. 寒武纪 （688256）— 国产AI芯片
9. 北方华创（002371）— 设备龙头
10. 中微公司（688012）— 刻蚀设备

### 4. 核心模块

#### daily_report.py — 收盘简报生成器
- 处理单个持仓股票的当日数据
- 输出：当日开盘/收盘/最高/最低、涨跌幅、成交量、5日趋势
- 判断价格是否进入目标区间
- 输出结构化简报文本

#### server.py — 移动端仪表盘（Flask）
- 单页面：持仓概览 + 候选列表 + 每日快照
- 移动端响应式（bootstrap 或 纯CSS）
- 端口：8765
- 用Hermes的send_message推送链接

### 5. Hermes集成

**Cron任务（用Hermes的cronjob工具创建）：**
```yaml
# 收盘简报（交易日15:30）
名称: serenity-daily-report
定时: 30 15 * * 1-5  # 周一到周五15:30
```

**通知格式（推送到WeChat）：**
```
📊 【Serenity Monitor】源杰科技 收盘简报
━━━━━━━━━━━━━━━━
日期：2026-05-25
收盘：xx.xx 元（+x.xx%）
开盘：xx.xx / 最高：xx.xx / 最低：xx.xx
成交量：xxx 万手
5日趋势：📈 上涨 x%
━━━━━━━━━━━━━━━━
📌 距目标卖出区间：还有 x%
🎯 目标卖价：xx - xx 元
```

### 6. CLI工具（cli.py）
```bash
# 查看当前持仓
python3 cli.py status
# 买入（记录买入操作）
python3 cli.py buy --code 300782 --price 85.50 --quantity 100
# 卖出（记录卖出操作，然后推荐下一只）
python3 cli.py sell --code 300782 --price 128.00
# 查看候选列表
python3 cli.py candidates
# 查看价格距目标的距离
python3 cli.py check --code 300782
# 手动触发一次收盘简报
python3 cli.py report --code 300782
```

## 工程规则

1. **单一文件职责** — 每个模块一个文件，不要搞大杂烩
2. **错误处理** — API失败时优雅降级，不中断流程
3. **无外部云服务** — 全本地运行，不依赖任何付费API
4. **不联网的数据用默认值** — 节假日/周末无数据时不要报错
5. **新浪API不通代理** — `urllib.request.ProxyHandler({})` 绕过任何系统代理设置
6. **股票代码对照** — sh=上海(60开头), sz=深圳(00/30开头), bj=北京(688开头)
7. **报告格式** — 严格保留上面定义的分隔线和emoji格式

## 目录结构
```
~/workspace/SerenityMonitor/
├── README.md
├── requirements.txt       # flask, pandas (akshare不需要)
├── data/
│   └── monitor.db         # SQLite（初始化时自动创建）
├── stock_fetcher.py       # 数据引擎（新浪API）
├── db.py                  # 数据库操作
├── models.py              # 数据模型/常量
├── daily_report.py        # 收盘简报
├── cli.py                 # CLI工具
├── server.py              # Flask仪表盘
└── templates/
    └── index.html         # 移动端仪表盘模板
```

## 验收标准
1. ✅ `python3 cli.py status` 显示当前状态
2. ✅ `python3 cli.py buy --code 300782 --price 85.5` 成功记录持仓
3. ✅ `python3 cli.py report --code 300782` 输出格式完整的收盘简报
4. ✅ `python3 cli.py check --code 300782` 显示价格距目标区间的距离
5. ✅ `python3 server.py` 启动后浏览器可访问仪表盘
6. ✅ 数据库预填充10个候选标的
7. ✅ 新浪API在无代理环境下正常工作
