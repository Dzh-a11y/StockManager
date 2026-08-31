---
date: 2026-08-31
purpose: 汇总 StockManager 总体架构、数据边界、规则与模板契约、Web 边界、同步存储、版本与启动等全部已接受架构决策，作为项目决策总 ADR。
project: StockManager
status: active
---

# ADR：StockManager 总体架构决策（总 ADR）

## 状态

已接受并实现（截至版本 1.4.3）。

## 背景与范围

本 ADR 汇总 StockManager 自 P1 至 P3 全部已接受的架构决策，作为项目决策总纲。各阶段的设计、契约与验收明细分别记录于 `development/implementation/` 下对应文档（`P1_*`、`P2_*`、`P3_*`），架构决策明细记录于 `development/architecture/` 下的 `ADR_P*` 文档；本 ADR 不与任何明细文档冲突；出现不一致时以本 ADR 为总纲、以对应明细文档的契约测试为准。

## 总体决策

### 1. 项目定位与约束

- 项目为 **A 股研究型筛选平台**，所有筛选结果仅供研究参考，**不构成投资建议**；**严禁实现自动交易功能**。
- 当前数据源为 Baostock，Provider 层保持接口可替换；业务代码禁止硬编码 Baostock 特定逻辑。

### 2. 分层架构

系统固定划分为六层，禁止跨层反向依赖：

```text
API（Web/CLI） -> Services -> Rules / Providers -> Storage(SQLite)
Domain（不可变领域对象）被各层共享引用
```

- Rules 与 Domain 层禁止直接操作数据库。
- API 层只负责解析、校验、状态码与 JSON 格式化，不实现筛选公式。
- 筛选路径（ScreeningService -> Rules）不得持有或调用 Provider 实例。

### 3. 本地优先与数据访问边界

- **单一入口**：Baostock 等外部数据源仅允许由 `DataSyncService` 调用。
- **只读本地存储**：筛选、Web API、CLI 查询只读取本地 SQLite（`data/market.sqlite3`），断网可运行。
- **成功即锁定**：同一数据集在特定交易日成功同步后标记状态，重复同步请求发出警告并返回本地数据，不发起网络请求。
- **并发保护**：同步使用持久化文件锁与进程锁，防止连点或并发进程重复拉取。
- **时间基准**：以 `Asia/Shanghai` 为准；同步目标为“最新已完成交易日”（按交易日历与数据截止时间计算，不用自然日当天）。
- **自动补齐**：程序进入新的已完成交易日后首次启动自动同步补齐；不承诺后台常驻。
- **失败处理**：同步失败明确记录 `FAILED`，绝不记为成功；重试由用户显式触发（如 `sync --retry`）；Provider 请求按受控速率串行执行，禁止无上限并发。
- **同步完整性判定**：某交易日仅当当日去重 bar 股票数 ≥ 股票池规模的 95%（阈值 `max(1, int(stocks_count * 0.95))`）时才视为完整同步；对无同步记录的历史交易日若覆盖不足则标记为 `incomplete`，界面以琥珀色提示，不得视为成功。
- **回补刷新失败日**：自动回补遇到标记为 `FAILED` 的交易日会重新拉取刷新，不得跳过；成功交易日仍受防重复拉取约束。
- **上游挂起防护**：Baostock SDK 调用绑定 socket 超时，防止上游无响应导致同步挂起。

### 4. 领域模型与协议

- 领域对象（`StockIdentity`、`DailyBar`、`FundamentalSnapshot`、`DividendRecord`、`DatasetMetadata`、`RuleResult`、`ScreeningResult`、`SyncRecord`）为不可变 dataclass。
- 价格、成交量、成交额及财务数值使用 `Decimal`；交易日使用 `date`；同步时间使用带时区的 `datetime`。
- 复权方式（`unadjusted`/`qfq`/`hfq`）必须在每个数据集与规则中显式记录，禁止擅自假定默认复权。

### 5. 规则层契约

- 筛选规则为**无副作用的纯函数**，输入结构化数据、输出结构化结果（`RuleResult`：`passed`/`actual_value`/`threshold`/`reason`）。
- 规则通过 `build_default_registry()` **显式注册**，不扫描目录、不从模板导入模块、不支持运行时上传代码。
- 规则执行三态：`PASSED` / `FAILED` / `SKIPPED`（仅模板禁用规则为 `SKIPPED`；启用规则样本不足仍是结构化 `FAILED`）。
- 每条规则声明强类型参数与 `RuleDataRequirement`（自然日窗口 vs 交易日窗口语义分离），由数据规划器合并计算读取范围；规则本身不查询数据库。
- 重复交易日、混合股票代码、目标日与元数据不一致、复权不匹配均显式抛异常，不吞错、不用日志代替异常。

### 6. 模板契约

- 模板为严格版本 2 JSON（`metadata`/`rules`/`composition`），必须经 `parse_template` 与 `TemplateCompiler` 编译为不可变 `ScreeningPlan` 后才能执行；未知字段、规则、参数、重复组合引用、启用规则遗漏和全禁用配置在执行前失败。
- 每条启用规则必须在组合中恰好出现一次；禁用规则不得进入组合表达式。
- 用户模板从 revision 1 开始，更新/删除必须携带 `expected_revision`，冲突抛 `TemplateRevisionConflictError`；保存采用同目录临时文件、`fsync` 与原子替换。
- 系统模板只读；模板 ID 仅允许小写字母、数字和连字符，最大 64 字符；符号链接目录/文件拒绝操作。
- 默认组合：基本面组全部满足；信号组任一满足；风险组全部满足。`annual_min_volume` 与 `annual_min_close_price` 位于信号组。

### 7. 本地 Web 工作台边界

- 使用 Python 标准库 `http.server`（`ThreadingHTTPServer`），无 Web 框架、无构建链、无外部 CDN；前端为纯 HTML/CSS/JavaScript。
- 默认只监听 `127.0.0.1`，端口可配置；静态资源仅白名单 `GET /`、`/styles.css`、`/app.js`，未知路径返回结构化 404。
- **元数据驱动**：规则目录（`GET /api/rules`）与参数控件由后端 `RuleDefinition` 自动生成，新增普通规则无需修改 HTTP 路由或前端清单。
- JSON 契约：`Decimal` 序列化为十进制字符串、`Enum` 为 `value`、`date`/`datetime` 为 ISO 8601；错误统一 `{"error": {"code": ..., "message": ...}}`，不泄露本机路径或堆栈。
- 模板解析、参数校验、组合校验与 revision 冲突由后端权威处理，前端校验不能替代。
- 系统模板拒绝修改（403）；本地数据不足返回明确业务错误（404），不调用同步服务。
- 顶部展示包版本（`GET /api/version`）；“数据状态”卡片由 `GET /api/sync/status` 驱动，渲染近 30 天逐日状态与近 360 天覆盖率带，`incomplete` 天以琥珀色提示，与同步完整性判定一致。

### 8. 版本控制

- 采用语义化版本 `MAJOR.MINOR.PATCH`：主版本=不兼容破坏；次版本=新增规则/功能（向后兼容）；修订版本=新增 UI 内容（向后兼容）。
- 版本号必须同步三处并保持一致：`pyproject.toml` 的 `version`、`src/stock_manager/__init__.py` 的 `__version__`、`tests/test_package_structure.py` 的版本断言。
- 当前基线版本：**1.7.2**。

### 9. 平台与启动

- 一键启动以 `scripts/launcher.py` 为核心（幂等：服务已运行则只打开浏览器；首次自动创建 `.venv`、安装依赖、启动服务、打开浏览器），日志写 `data/server.log`、进程信息写 `data/server.pid`，监听 `127.0.0.1:8000`。
- **macOS**：双击 `scripts/StockManager.command`（脚本内硬编码项目路径，项目移动需修改）；或 `scripts/make_app.sh` 生成带图标的 `StockManager.app`。
- **Windows**：双击 `scripts\StockManager.bat` 自动装依赖、启动并自动生成桌面快捷方式；`scripts\setup_windows.bat` 手动补救快捷方式；`scripts\build_windows_exe.bat` 可打包单文件 `dist\StockManager.exe`（免装 Python，数据存放于 exe 所在目录）。
- 手动启动等价命令：`stock-manager web --db data/market.sqlite3 --system-templates config/rule_templates --user-templates data/user-templates --static src/stock_manager/web/static --sync-config config/sync.json --lock-dir data/locks --host 127.0.0.1 --port 8000`。

### 10. P4 只读访问与分片筛选基础设施

- 新增数据库无关只读访问层 `stock_manager.read`：`MarketDataReadRequest`、`DatasetReadSnapshot`、`MarketDataBatch` 为不可变领域契约（无 SQL）；`MarketDataReaderProtocol` / `MarketDataReaderFactoryProtocol` 为数据库无关协议（禁止 sqlite3.Connection、SQL 文本与 PRAGMA）；`SQLiteMarketDataReader` 为 P4 唯一实现（只读 URI `mode=ro` + `PRAGMA query_only`）。
- `MarketDataReadService` 负责代码分片、受控线程池并发、稳定合并（`code ASC, trading_day ASC`）、`max_workers=1` 串行回退与失败传播（`ShardReadError` 携带分片标识与原始异常链）；快照在并发读取前冻结，版本变化抛 `SnapshotConsistencyError`，禁止返回混合数据。
- `ScreeningShardExecutor` 只属于参数化筛选通道：进程池 worker 各自创建只读 reader/连接，在 worker 内读取行情/基本面/分红并执行 `RuleEngine.evaluate()`，只回传结构化结果与进度；同一调用链禁止嵌套线程池/进程池；`max_workers=1` 或小数据量回退串行（逐股进度回调与旧实现一致）。
- 并发约束：SQLite 连接只读且每 worker 独立；worker 数配置化有上限（读取默认 1；筛选 Web 可配置 1-16、默认 4）；代码分片避免超大 IN 查询；合并结果与串行结果逐项相等；写入、同步、重试与上游限速不受 P4 影响。
- 性能证据（Apple M5 Pro / 48 GB，2026-08-31，全市场 5212 只）：SQLite 只读并发读取无收益（串行 5.73s vs 4 workers 15.21s），默认串行；参数化筛选进程池 4 workers 1.95s vs 串行 7.42s（3.8 倍加速），Web `/api/screen` 真实链路 2.35s；Web 工作台可在 1-16 内配置筛选 worker 数并显示本次筛选用时（`elapsed_seconds`）。
- 对后续阶段：P5-A 回测、P5-B CAPM、P6 只依赖 `MarketDataReadService` 与数据库无关类型；未来数据库升级只替换 reader 实现与连接配置；未来 CAPM/回测各自增加独立执行器，不得复用 `ScreeningPlan` 或 `RuleEngine` 承载其他模块业务。

### 12. 内置规则清单

| rule_id | 名称 | 归属 |
| --- | --- | --- |
| `pe_positive` | PE 下限 | 基本面组 |
| `non_st` | 排除 ST | 基本面组 |
| `volume_price_5d` | 量价信号 | 信号组 |
| `limit_up_breakout` | 炸板或假阴线 | 信号组 |
| `annual_min_volume` | 年度最低交易量 | 信号组 |
| `annual_min_close_price` | 年度最低收盘价 | 信号组 |
| `limit_up_3m` | 涨幅次数 | 风险组 |
| `volatility_multiple` | 波动倍数 | 风险组 |
| `consecutive_up_days` | 连阳 | 信号组 |
| `n_day_close_above` | N日收盘价下限 | 信号组 |
| `volume_sum_extreme` | 连续量能极值 | 信号组 |
| `price_range_ratio` | N日高低点倍率 | 信号组 |

## 关联决策记录

- `development/architecture/ADR_P4_READ_LAYER.md`：P4 只读访问层、并发读取服务与分片筛选执行的公共契约、并发约束与错误语义。
- `development/architecture/ADR_P2_RULE_EXECUTION.md`：规则三态、模板版本与编译、显式注册、信号组归属、revision 乐观并发。
- `development/architecture/ADR_P3_LOCAL_WEB_UI.md`：Web 技术选型、分层边界、静态资源白名单、JSON 表示与错误映射。
- 各阶段明细与验收：`development/implementation/P1_2_DOMAIN_PROTOCOLS.md`、`development/implementation/P1_4_PURE_RULES.md`、`development/implementation/P1_5_LOCAL_SYNC_STORAGE.md`、`development/implementation/P1_6_SCREENING_CLI.md`、`development/implementation/P2_PARAMETERIZED_RULES.md`、`development/implementation/P3_WEB_API.md`、`development/implementation/P4_READ_LAYER.md` 等。

## 结果

- 全部决策已实现并通过离线测试（当前 `pytest` 全量通过）。
- 后续新增规则、模板能力或 Web 功能若改变上述任一契约，必须先在本总 ADR 与对应明细 ADR 中同步修订。
