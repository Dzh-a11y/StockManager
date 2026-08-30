---
date: 2026-08-31
purpose: 明确 StockManager 第四阶段可替换数据库只读访问、并发数据读取与分片筛选基础设施的架构、任务和验收标准。
project: StockManager
status: active
---

# StockManager 第四阶段计划：可替换数据库只读访问与分片筛选基础设施

StockManager 是 A 股研究型筛选平台，禁止自动交易；所有输出仅供研究参考，不构成任何投资建议。

本阶段的架构与任务拆分代理为 Codex；实现、测试、调试与代码审查代理为 DeepSeek V4 Flash；Qwen3.8:27b 仅在实现验收后按完整任务包起草注释和技术文档。

## 1. 阶段定位

P4 是一个小而独立的基础设施阶段，解决本地数据库的批量读取、分片并发、参数化筛选分片执行、稳定排序和数据库后端可替换问题。P4 不实现 CAPM、不实现回测、不实现主力追踪，也不改变 Baostock 同步串行策略。

P4 完成后，P5-A 回测、P5-B CAPM 和 P6 高维统计可以通过同一只读端口读取数据，不直接依赖 SQLite，也不重复实现并发逻辑。

## 2. 源码事实与可行性

截至 2026-08-31，源码和本地数据库呈现以下事实：

- `SQLiteRepository.get_daily_bars` 使用 `code IN (...)` 一次性读取并构造完整 `tuple[DailyBar, ...]`。
- `ParameterizedScreeningService.screen` 一次性读取全量数据，在单进程内逐只股票串行求值。
- `SQLiteRepository._connect` 每次调用创建独立连接，设置 `foreign_keys` 与 `busy_timeout`，但没有独立只读连接配置、查询快照对象或批次接口。
- `daily_bars` 主键为 `(code, trading_day, adjustment)`，适合按股票读取；现有表没有为按交易日横截面查询专门设计的索引。
- 当前 `data/market.sqlite3` 约 198 MB，包含约 124 万条 `daily_bars`，日期为 2025-09-01 至 2026-08-28，数据复权标记均为 `qfq`。
- 当前保留窗口为 360 个自然日。P4 只读取现有数据，不在本阶段改变保留策略。

因此，小型并发读取模块技术上可行，并且应先于 CAPM、回测和主力追踪建设。

## 3. 阶段目标

1. 将“查询意图”与 SQLite SQL 实现分离。
2. 支持按股票代码分片、受控并发读取和确定性合并。
3. 对参数化筛选提供单层分片执行：worker 自行读取本分片数据并执行 `RuleEngine`，避免主进程传递全量行情。
4. 每个 worker 使用自己的只读连接，禁止跨线程或跨进程共享连接。
5. 提供 `max_workers=1` 的串行参考路径和自动回退。
6. 对调用方返回稳定的领域对象或稳定列式批次，不泄漏 `sqlite3.Row`、SQL、连接或本机路径。
7. 为未来迁移到 DuckDB、PostgreSQL 或其他本地分析存储保留实现替换点；P4 不决定也不执行数据库迁移。
8. 保持筛选、CAPM、回测与 P6 研究路径全部离线，不持有 Provider。

## 4. 排除范围

- 不并发调用 Baostock；Provider 请求仍由 `DataSyncService` 受控串行执行。
- 不并发写 SQLite；成功同步、防重复、进程锁和文件锁语义保持不变。
- 不修改复权策略，不把 `qfq` 设为新的隐式默认值。
- 不在 P4 增加 CAPM、投资组合、撮合、交易成本或主力统计逻辑。
- 不直接切换数据库产品，不增加分布式任务系统。
- 不把 pandas DataFrame 作为 Storage 的唯一公共契约，以免锁死未来数据库实现。

## 5. 目标架构

```text
Screening / CAPM / Backtest / Main-force services
                    |
                    v
          MarketDataReadService
        (分片、并发、排序、合并)
                    |
                    v
       MarketDataReaderProtocol
          /                   \
SQLiteMarketDataReader     FutureReader
 (P4 实现)              (DuckDB/PostgreSQL 等)
          |
          v
  local SQLite, read-only

BaostockProvider -> DataSyncService -> LocalRepositoryWriter -> SQLite
                    （继续串行）
```

### 5.1 分层责任

- Domain：定义只读请求、数据批次、数据快照和错误语义，不包含 SQL。
- Storage：实现 SQLite 查询、连接生命周期、参数绑定和行到领域对象的转换。
- Services：负责股票代码分片、worker 调度、失败传播、进度和确定性合并。
- API/CLI：仅解析输入和格式化输出，不创建线程池、不拼 SQL。
- Providers/Sync：保持现状，不依赖并发读取服务。

## 6. 拟议公共契约

精确名称在 P4-0 冻结；以下语义必须保持。

### 6.1 `MarketDataReadRequest`

至少包含：

- `dataset_id: str`
- `codes: tuple[str, ...]`
- `start: date`
- `end: date`
- `adjustment: AdjustmentMethod`
- `batch_size: int`
- 明确排序：`code ASC, trading_day ASC`

校验规则：数据集非空、日期区间有效、代码去空白并去重、批次大小为正数、复权显式提供。

### 6.2 `DatasetReadSnapshot`

至少包含：

- `dataset_id`
- `trading_day`
- `adjustment`
- `source`
- `synced_at`

并发读取开始前固定快照；每个分片必须验证元数据一致。读取过程中若目标数据集版本变化，应显式失败或按已冻结快照完成，不得静默混合两个版本。

### 6.3 `MarketDataReaderProtocol`

单个 reader 只负责一个分片的确定性读取。协议禁止出现 `sqlite3.Connection`、SQL 文本或 SQLite PRAGMA。

### 6.4 `MarketDataReadService`

负责：

- 按排序后的代码生成有界分片。
- 使用受控 `ThreadPoolExecutor` 并发读取；每个任务通过 reader factory 创建独立 reader/连接。
- `max_workers=1` 时使用同一逻辑的串行路径。
- 收集所有分片后按 `(code, trading_day)` 稳定排序。
- 任一分片失败时取消未开始任务并向上抛出明确异常，禁止返回部分成功结果冒充完整数据。
- 回调只报告已完成分片或已完成股票数，不从 worker 直接修改 Web 共享状态。

### 6.5 `ScreeningShardExecutor`

该执行器只负责参数化筛选的分片编排，不重新定义规则、模板或组合语义。每个分片 worker 必须：

- 使用 reader factory 创建自己的只读 reader/SQLite 连接。
- 读取本分片所需的行情、基本面和分红数据。
- 在 worker 内按股票构造 `RuleContext` 并调用 `RuleEngine.evaluate()`。
- 只返回本分片的结构化筛选结果与进度信息，不返回全量原始行情。

同一筛选调用链只允许一层并发调度：分片执行器不能在 worker 内再次创建读取线程池或进程池。`max_workers=1` 必须复用相同逻辑并串行执行。

> **PS：后续扩展约定**：`ScreeningShardExecutor` 只属于参数化筛选通道，不承载 CAPM、回测或其他研究模块的业务计算。未来新增 CAPM 或回测时，应分别增加独立的 `CapmShardExecutor`、`BacktestExecutor`（具体名称在对应阶段冻结）；它们可以共用 `MarketDataReadService`、reader factory、分片调度、连接隔离和结果收集基础设施，但必须拥有各自的参数计划、数据需求、计算引擎、状态语义和结果契约。不得为了复用并发代码而把不同模块塞进 `ScreeningPlan` 或 `RuleEngine`。

## 7. 并发与数据库约束

1. SQLite 连接必须使用只读 URI 或等价安全方式，并启用 `query_only`；P4 读取不得创建数据库或表。
2. 一个连接只属于一个 worker，不在线程之间传递。
3. worker 数必须配置化且有上限；默认值由 P4-0 基准测试决定，不按 CPU 数无限扩张。
4. 股票代码必须分片，避免单条超大 `IN` 查询和 SQLite 参数数量限制。
5. 合并结果必须与串行结果逐项相等、顺序一致。
6. 并发只用于本地只读查询和无副作用的筛选计算。写入、同步、重试和上游限速完全不受 P4 影响。
7. 同一调用链禁止嵌套线程池与进程池；读取型调用使用受控线程池，筛选分片执行可使用受控进程池，二者不得在 worker 内叠加。
8. 查询不得在完整数据返回后才进行无界复制；应记录峰值内存，并为后续流式批次保留扩展点。

## 8. 错误语义

- 数据集元数据不存在：`DatasetUnavailableError` 或冻结后的等价业务异常。
- 复权不一致：显式 `ValueError` 或专用契约异常。
- 分片查询失败：包含分片标识与原始数据库异常链，禁止吞掉异常。
- 快照不一致：专用一致性异常，不返回部分数据。
- 用户取消：若后续增加取消能力，应区分取消与失败。

## 9. 任务分解

### P4-0：接口冻结与真实负载基准

- 只读测量现有串行路径的耗时、返回行数、峰值内存和查询计划。
- 固定请求、快照、reader、reader factory、并发服务和错误接口。
- 确定默认 `batch_size`、`max_workers` 上限及小数据串行阈值。
- 本机性能基准环境先记录为：Apple M5 Pro，48 GB 统一内存。正式采集时另行记录 macOS 版本、Python 版本、SQLite 版本、数据库文件大小、数据行数、请求股票数和实际并发参数。
- 产出 ADR；本任务不修改业务行为。

验收：基准命令、硬件环境、数据库规模和结果可复现；不得用主观“更快”代替数据。

### P4-1：只读领域契约与协议

- 增加不可变请求、快照与批次类型。
- 增加数据库无关 `MarketDataReaderProtocol` 与 reader factory 协议。
- 完整类型标注与输入校验。

验收：协议不导入 sqlite3、pandas、Provider 或 Web 类型。

### P4-2：SQLite 只读 reader

- 从现有 Repository 查询代码提取只读实现。
- 使用独立只读连接、参数绑定、稳定排序和分片查询。
- 保留现有 `SQLiteRepository` 公共行为，通过适配器渐进迁移。

验收：空代码、重复代码、无序代码、空结果、缺失元数据和复权不匹配均有离线测试。

### P4-3：并发读取服务

- 实现受控线程池、代码分片、reader factory、稳定合并与进度事件。
- 支持 `max_workers=1` 串行回退。
- 任一 worker 失败时不返回部分结果。

验收：并发与串行结果字节级等价或领域对象逐项等价；重复运行顺序稳定。

### P4-4：参数化筛选分片执行接入

- 将 `ParameterizedScreeningService` 接入分片执行器，按股票分片读取并在 worker 内完成 `RuleContext` 构造与 `RuleEngine` 求值。
- 保持 `ScreeningPlan`、规则参数、三态结果和组合语义不变；不把全量 `DailyBar` 传入进程池。
- 使用单层受控进程池承载 CPU 密集的筛选计算；小数据量或 `max_workers=1` 时回退串行路径。
- 保持 Web/CLI 请求和响应完全不变。

验收：并发与串行筛选结果逐项等价且顺序稳定；现有 Web/CLI 契约测试不变；断网运行；Provider 不可达时仍能筛选。

### P4-5：故障、并发与跨平台测试

- 覆盖 worker 异常、规则计算异常、数据库锁、空分片、分片边界、超大股票池、连接关闭和取消。
- 在 macOS/Windows 支持的 Python 版本上验证线程/进程 worker 的连接隔离。
- 验证并发读取期间同步写入不会产生脏读或混合快照。

验收：无吞异常、无死锁、无连接泄漏、无不受控线程增长。

### P4-6：性能验收与文档同步

- 在 P4-0 同一数据集上分别比较：只读批量读取的串行/并发路径，以及参数化筛选的串行/分片并发路径。
- 性能报告必须标注基准机器：Apple M5 Pro、48 GB 统一内存，并同时记录采集时的软件版本、数据库规模、`batch_size`、`max_workers` 和小数据串行阈值。
- 只有在全量结果一致且峰值内存受控后才接受性能结果。
- 更新架构、配置和扩展数据库 reader 的开发文档并同步 `project_doc`。

验收：记录性能证据；若 SQLite 已受磁盘带宽限制且并发无收益，允许保留架构并默认串行，但不得伪造加速结论。

## 10. 测试矩阵

- `max_workers=1` 与多 worker 的读取结果和筛选结果完全等价。
- 输入代码为空、单只、重复、无序、全市场。
- 日期边界相等、反向日期、非交易日、缺失交易日。
- `unadjusted`、`qfq`、`hfq` 元数据匹配与不匹配。
- 空表、缺少元数据、部分分片无数据。
- 分片恰好位于 batch 边界以及超过单条 SQL 参数安全范围。
- worker 中途抛出 SQLite 或规则计算错误，不返回部分结果。
- 并发期间数据库有其他只读请求。
- 离线运行并证明不导入或调用 Provider。
- 稳定排序和多次运行确定性。

## 11. 完成定义

P4 只有在以下条件全部满足时完成：

1. 已有串行行为保持兼容。
2. 并发读取结果与串行读取结果完全一致且顺序稳定。
3. 并发筛选结果与串行筛选结果完全一致且顺序稳定。
4. Provider 与写入路径没有被并发化。
5. 公共协议中没有 SQLite 实现细节。
6. 离线测试覆盖关键边界、错误和并发分支。
7. 性能报告包含真实基准，不承诺未测量的收益。
8. DeepSeek V4 Flash 完成实现、测试和代码审查验收。
9. 验收后的技术文档包含规定 frontmatter，并同步到 `/Users/douzihao/StockManager/project_doc`。

## 12. 对后续阶段的接口承诺

- P5-A 回测与 P5-B CAPM 只依赖 `MarketDataReadService` 和数据库无关请求/快照类型。
- P6 只依赖相同接口读取特征窗口和回测验证数据。
- 未来数据库升级只替换 reader 实现与连接配置，不要求改写 CAPM、回测或主力模型。
