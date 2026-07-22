# Serenity QMT 全自动部署

## 当前边界

- `xtquant 250807` 官方 Windows SDK 已校验并暂存于 Mac：
  `/Users/mac/.local/share/serenity/xtquant-windows-250807`
- 官方原始 RAR：`/Users/mac/.local/share/serenity/xtquant_250807.rar`
- RAR SHA-256：`905f645c4c0db2b6f2c598f70c03c2c02eb282c5ceb11b550e63da702e613c2b`
- QMT/MiniQMT 客户端由开户券商分发并授权，必须运行在 64 位 Windows。
- Serenity 的 QMT 后端无条件要求 HTTPS；真实下单还必须通过完整 `FULL_AUTO` 门禁。

## 1. 券商侧前置条件

1. 向客户经理获取本账户对应的 QMT/MiniQMT 安装包和量化交易权限。
2. 在 64 位 Windows 安装并登录 QMT。
3. 确认存在 `userdata_mini\up_queue_xtquant`。缺失时不要继续，要求券商开通 xtquant 下单权限。
4. 准备与官方 SDK 原生模块匹配的 64 位 Python。当前安装器只批准并校验
   官方 `250807` 包的 CPython 3.10—3.13 原生模块。

## 2. 安装 Windows 执行代理

先在 Mac 生成无账户、无 token、无证书私钥的离线部署包：

```bash
python3 cli.py qmt-deployment-bundle
```

命令默认生成 `dist/serenity-qmt-windows-deployment.zip`，并输出外层 ZIP 的
SHA-256。通过与文件传输不同的渠道把该指纹带到 Windows，解压前使用
`Get-FileHash -Algorithm SHA256` 核对；包内 `SHA256SUMS.json` 还会逐项校验源码、
安装器和官方 RAR。部署包故意不包含资金账号、token、证书、私钥或实盘配置。

解压后、运行安装器前执行随包提供的校验器；文件缺失、多出文件、重解析点、长度或
哈希不一致都会终止：

```powershell
.\VERIFY-BUNDLE.ps1
```

将部署包安全复制并解压到 Windows 本地 NTFS 目录。用管理员 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd C:\path\to\serenity-qmt-windows-deployment
.\install_qmt_agent.ps1 `
  -QmtUserdataPath "C:\path\to\QMT\userdata_mini" `
  -AccountId "券商资金账号" `
  -BundleManifestPath "$PWD\SHA256SUMS.json"
```

第一次不要添加 `-EnableTrading`。脚本会：

- 先把 RAR 复制到用户私有临时目录，并对这份私有副本校验完整固定 SHA-256；
  解压与哈希使用同一个不可变副本。随后再校验 Windows/Python 架构、
  对应 CPython 原生模块和券商权限目录；
- 安装代理与官方 SDK；
- 生成 3072 位 RSA 的 loopback TLS 证书和 48 字节随机 HMAC token；
- 限制敏感文件 ACL；
- 以普通用户权限注册登录一分钟后启动的 `SerenityQMTAgent` 计划任务，
  启动失败时每分钟重试，最多 12 次；每天 09:20 和 12:55 还会再次触发，
  已有实例运行时忽略重复启动，覆盖 QMT 晨间或午间重新登录后的代理恢复。

重复执行安装器会先停止既有 Agent，确认任务已经退出后才覆盖代码，避免新旧进程
同时连接同一账户。账户不变时默认复用原 HMAC token，因此从只读模式升级到
`-EnableTrading` 不需要重新改 Mac 钥匙串；只有明确传入 `-RotateToken`、更换账户
或旧 runtime 无效时才生成新 token。轮换后必须把新的 bootstrap 安全同步到 Mac。

QMT 登录后启动只读代理：

```powershell
Start-ScheduledTask -TaskName "SerenityQMTAgent"
```

## 3. 建立 Mac 到 Windows 的安全通道

代理默认只监听 Windows `127.0.0.1:18765`。Mac 上的 `8765` 已由 Hermes
Mnemosyne Dashboard 使用，禁止复用。推荐通过 Windows OpenSSH 建立隧道：

```bash
ssh -N -L 18765:127.0.0.1:18765 windows-user@windows-host
```

从 Windows 安全复制以下文件，不要通过聊天或明文邮件传输：

- `%ProgramData%\SerenityQMTAgent\mac-client.bootstrap.json`
- `%ProgramData%\SerenityQMTAgent\tls\serenity-qmt.crt`

Windows 安装结束时会在控制台单独显示 `Mac CA SHA-256`。通过与文件传输不同的
可信通道记录该 64 位指纹；仅从 bootstrap 或同一压缩包读取的指纹不构成独立验证。
复制后先限制 bootstrap 权限并执行无副作用验证。报告只包含证书指纹、Agent URL
和布尔状态，不输出账号或 token：

```bash
chmod 600 /private/path/mac-client.bootstrap.json
EXPECTED_CA_SHA256='Windows控制台独立显示的64位SHA-256'
python3 cli.py qmt-bootstrap-validate \
  /private/path/mac-client.bootstrap.json \
  /private/path/serenity-qmt.crt \
  "$EXPECTED_CA_SHA256"
```

验证通过后，用事务式导入器写入私有 CA、macOS 钥匙串和无密钥配置。任何一步失败
都会恢复原证书、配置和已修改的钥匙串项：

```bash
python3 cli.py qmt-bootstrap-import \
  /private/path/mac-client.bootstrap.json \
  /private/path/serenity-qmt.crt \
  "$EXPECTED_CA_SHA256" \
  I_UNDERSTAND_QMT_BOOTSTRAP_IS_SENSITIVE \
  --delete-bootstrap
```

默认保持 `enabled=false`。只有 Windows bootstrap 明确记录
`trading_enabled=true` 时，才允许额外传入 `--enable-backend`。启用采用两阶段提交：
磁盘上的正式配置先保持禁用，导入器用临时候选配置完成 QMT 预检；只有检查全部通过
（或仅剩尚未安装的实盘调度器）才原子写入 `enabled=true`，失败则恢复 CA、配置与
钥匙串。不要手工导出账号/token，也不要直接编辑 `enabled=true` 绕过此流程。
未使用 `--delete-bootstrap` 时，返回结果会保持
`bootstrap_cleanup_required=true`，必须在核对后删除明文临时文件。

完成复制但尚未启用前，先运行不泄露凭据的部署预检：

```bash
python3 cli.py qmt-preflight
```

预检按安全依赖顺序检查官方 SDK 哈希、暂存包、配置权限、TLS/本地隧道、CA、
钥匙串或环境凭据、代理健康、账户验证、保护性退出、完整门禁和 LaunchAgent。
`next_action` 是当前允许执行的下一步；在 `ca_certificate`、`account_credential` 或
`token_credential` 尚未通过时，不得提前把 `enabled` 改为 `true`。

该实盘账户必须由 Serenity/QMT Agent 独占写入：启用无人值守交易后，不得再从
QMT GUI、手机端或其他程序手工下单。Agent 每次 BUY 前都会检查券商全部当日未决
委托与 `frozen_cash`；发现外部活动后会持久锁定新增 BUY，即使该委托随后消失也不
自动解锁，以避免快照和提交之间的竞态被静默忽略。完成券商订单、成交、持仓和
冻结资金人工核对后，才可显式执行：

```bash
python3 cli.py qmt-review-external I_HAVE_RECONCILED_EXTERNAL_ACTIVITY
```

## 4. 分阶段启用

先验证 TLS、账户与持仓查询，并运行门禁：

```bash
python3 cli.py full-auto-gate --json
```

在策略证据、微仓证据、仿真证据、交易日历和硬风险限制仍有任一失败时，禁止让
Windows Agent 具备下单能力。只有这些独立于 QMT 连通性的门禁已经通过、剩余失败
仅属于 QMT 账户/保护性退出/组合对账集成，并且该账户已停止所有人工及第三方写入时，
才允许在 Windows 重跑安装器并同时传入：

```powershell
-EnableTrading `
-EnableTradingApproval "INDEPENDENT_EVIDENCE_GATES_PASSED_AND_QMT_ACCOUNT_ISOLATED"
```

随后导入 `trading_enabled=true` 的新 bootstrap，完成账户、保护性退出、kill switch
与组合强一致对账，再次运行 `full-auto-gate`。最终 `GO` 之前仍不得安装实盘调度器；
直接传入裸 `-EnableTrading` 会被安装器拒绝。

只有以下检查全部通过才允许无人值守委托：

- 策略独立样本达到 `SEMI_AUTO`；
- 微仓纸面闭环和独立 OOS 均通过；
- 纸面闭环必须来自当前策略版本、由不可变 `final_close` 决策包驱动的真实时间顺序
  买卖，旧交易、手工补行和空 `decision_id` 不计入；独立 OOS 至少连续记录
  20 个交易日并完成一次买卖往返，且冻结源码清单和判定标准哈希均保持完整；
- 至少 4 周自动仿真、20 个 canonical 信号、一次 3% 市场事件；
- 仿真使用下一交易日真实 OHLCV Bar，记录成交/未成交率，并在验证期内完成至少一次买卖往返；
- 仅允许与实盘同为 2% 单票、T+1、含费用/滑点的对齐模型贡献回测偏差证据；旧快速回测不再计入；
- QMT 代理在线、交易已启用、账号和净值可验证；
- 本地组合账本与 QMT 实时账户必须完成强一致对账：每个代码的股数完全一致，
  可用现金和持仓市值的差额不得超过 `max(10 元, QMT NAV × 0.1%)`。存在在途
  买单时，按 QMT 官方字段语义使用“本地总值 + `frozen_cash`”与券商总资产比较，
  并同时校验“可用现金 + 冻结资金 + 持仓市值 ≈ 总资产”。
  对账发生在生成订单之前；任何缺行、股数差异、不可解析值或快照失败都会新增
  `portfolio_reconciliation` 门禁失败，防止用陈旧的本地持仓生成实盘指令；
- 交易日历必须覆盖当天。超出已核验的交易所休市日历范围时，
  `trading_calendar` 门禁保持失败，不能仅凭“工作日”猜测开市；
- QMT 端保护线程必须先完成一次成功的持仓/订单对账，并持续保持心跳；未启动、
  对账失败或心跳过期时，代理会报告 `protective_exit_enabled=false`，且
  `trading_enabled=false`；
- 每笔买入必须携带纳入 `decision_id` 的 `stop_price`。Mac 执行层和 Windows
  Agent 都会以 `(买入价 - 止损价) × 股数` 独立重算名义计划亏损，声明值不一致
  时拒单；仓位上限还会采用 `max(止损距离, 买入价 × 10%) × 股数` 的 T+1
  跳空压力损失，超过净值 0.15% 时拒单；
- 买入成交后，Agent 在 SQLite 中持久化保护状态。价格触发止损时，沪市使用
  “最优五档即时成交剩余撤销”，深市使用对应指令；提交前先落盘，进程崩溃后
  通过确定性委托备注恢复，避免重复卖出；
- A 股 T+1 无法保证买入当日止损成交。当天不可卖时状态会锁存为
  `triggered_wait_t1`，禁止新增买入，并在出现可卖数量后提交保护卖出。跳空、
  日内高位买入后回落、连续跌停和流动性不足都可能令实际亏损显著超过 10%
  压力情景；因此 0.15% 只是保守仓位预算，不是最大成交损失保证；
- 保护卖出结果不确定、被拒、撤单或部分撤单时，保护线程保持门禁关闭，并经
  券商订单查询确认后恢复。重试仅在 A 股交易时段进行，间隔从 30 秒指数退避
  到最多 15 分钟，避免休市或跌停期间每 30 秒刷单；只要仓位仍存在，保护不会
  因达到固定次数而自动放弃；
- 券商持仓快照短暂缺行不会自动把保护标记为已平仓。若确由人工或其他已核对
  卖单完成平仓，必须先在券商端确认该标的持仓为零，再显式关闭对应保护：

```bash
python3 cli.py qmt-close-protection <decision_id> I_HAVE_VERIFIED_THE_BROKER_POSITION_IS_CLOSED
```
- Agent 第一次运行当日不会把任意时点的当前 NAV 冒充日初基准。只有持久化到
  紧邻上一工作日 15:00 之后的 NAV 观测，下一交易日的日亏损基准才视为可信；
- kill switch 清除，单票 2%、总账户 5%、主题 3%、单笔计划亏损 0.15% 硬上限一致。

在门禁为 `NO_GO` 时，`python3 cli.py workflow --full-auto` 会阻止所有新增 BUY；
已经获得显式全自动授权且带有效 canonical SELL 决策的风险降低卖出仍可执行，
kill switch 不得反向阻塞清仓。
任何提交结果不确定或未完成的订单，都必须先运行
`python3 cli.py qmt-reconcile` 对账；工作流检测到未决订单也会拒绝新单。
若 Agent 跨日重启后，昨日委托已从 QMT“当日委托”接口消失，系统不会猜测成交
结果，也不会自动重发。必须先在券商历史委托/成交中核对最终状态和累计成交股数，
再显式录入（示例为确认未成交撤单）：

```bash
python3 cli.py qmt-reconcile-history <decision_id> cancelled 0 I_HAVE_VERIFIED_THE_BROKER_ORDER_HISTORY
```

`filled` 必须填写全部委托股数，`partial_cancelled` 必须填写 0 与委托股数之间的
实际累计成交量；只要有成交，还必须从券商历史成交中填写累计成交均价和真实委托号：

```bash
python3 cli.py qmt-reconcile-history <decision_id> partial_cancelled 100 10.05 \
  <broker_order_id> I_HAVE_VERIFIED_THE_BROKER_ORDER_HISTORY
```

命令只接受前一交易日遗留的非终态委托。BUY 若确认有成交，保护状态会恢复为
armed 并立即接受实时持仓/止损监控；缺少成交均价或委托号时拒绝确认，避免用猜测
价格污染本地损益和后续仓位计算。

## 5. 安装 Mac 盘中执行周期

决策包的 `valid_until` 使用“下一交易日同一时刻”，因此周五 15:05 生成的决策
可在周一 15:05 前执行，不会在周六错误过期。控制器只允许在上海时间
09:35–11:25、13:05–14:50 进入账户预检；保护性退出仍由 Windows Agent 的
两秒心跳独立执行，不依赖 Mac 调度。

必须使用第 3 节的事务式 bootstrap 导入器写入钥匙串。不要把 token 放入 shell
命令参数或长生命周期环境变量；导入器通过标准输入交给 macOS `security`，避免
凭据出现在进程参数列表中。

先手动运行只读周期。没有 `--execute` 时，即使所有门禁通过也只做 dry-run：

```bash
python3 cli.py qmt-auto-cycle
```

只有 `python3 cli.py full-auto-gate --json` 返回 `GO` 后，才安装实盘调度：

```bash
SERENITY_PYTHON="$(command -v python3)" \
  ./scripts/install_qmt_cycle_launchd.sh /Users/mac/workspace/SerenityMonitor
```

安装器会在写入 runner 或 LaunchAgent 之前再次直接调用 FULL_AUTO 门禁；门禁不是
`GO` 时以非零状态退出，不会留下一个看似已安装的实盘计划任务。`qmt-preflight`
也不再把 plist 文件存在视为安装成功：它会校验私有权限、固定 Label、09:40/13:10
时间表、唯一 runner 路径，以及 runner 只能包含严格模式、两项钥匙串取密、工作目录
和 `qmt-auto-cycle --execute` 六行规范结构。任何额外命令或明文环境变量都会使
`scheduler_installed=false`。

安装器不把账号或 token 写入 plist/脚本，只在每次执行时从钥匙串读入进程环境；
它要求 Mac 系统时区为 `Asia/Shanghai`，并在每天 09:40、13:10 触发
`qmt-auto-cycle --execute`。控制器使用内核文件锁防止重叠运行，周末/休市、盘外、
日历未覆盖、QMT 快照不可用或本地与券商账本不一致时均在生成计划前退出。

每个盘中周期首先查询公共委托并同步 Windows Agent 的只读成交账本。公共订单和
Agent 自动发出的保护卖出都会以 `decision_id + 累计成交股数` 幂等回填；累计均价
变化时按累计成交额推导本次增量成交价，并在同一 SQLite 事务内更新交易记录和
本地持仓。数据库写入失败时，终态公共订单继续保留在待对账队列；保护卖出成交则
持续保留在 Agent 成交接口，下一周期重试。只有成交回填完成后才执行本地/QMT
强一致对账和计划生成。由于本地成交公式无法独立推导券商佣金、印花税和其他费用，
专用 QMT 账户的可用现金同时以已验证的券商账户快照覆盖本地现金；持仓股数仍必须
由每笔成交事件逐笔闭环，不能用现金覆盖掩盖漏单。

## 6. 自动积累科学仿真证据

同日信号价成交不能用于晋级。`auto_trade_from_signals` 只把带 canonical
`decision_id` 的交易动作标记为等待；真正撮合由下一交易日收盘后的独立周期完成，
且只读取该信号紧邻下一交易日的真实 OHLCV Bar。限价未触及只记录一次未成交，
不会在更晚 Bar 重试；若 Bar 数据尚未入库，则整个周期阻断且不消耗这次机会。

手动检查：

```bash
python3 cli.py sim-evidence-cycle
```

安装独立调度：

```bash
SERENITY_PYTHON="$(command -v python3)" \
  ./scripts/install_sim_evidence_launchd.sh /Users/mac/workspace/SerenityMonitor
```

LaunchAgent 在上海时间 16:10 运行，22:10 再重试一次。周期使用内核文件锁；任何
持仓缺少有效价格时不会写入低估的组合净值。周度汇总只接纳
`observed_next_bar_v1` 的尝试和成交，历史合成成交不会进入成交率、滑点、往返或
回测偏差证据。该调度只操作独立纸面账户，不会连接 QMT 或发送真实委托。
