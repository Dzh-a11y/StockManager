---
date: 2026-08-31
purpose: 冻结 StockManager P4 可替换数据库只读访问层、并发读取服务与参数化筛选分片执行的公共契约、并发约束与错误语义。
project: StockManager
status: accepted
---

# ADR：P4 可替换数据库只读访问与分片筛选执行契约

## 状态

已接受并实现。

## 背景

P3 完成本地 Web 工作台后，`SQLiteRepository.get_daily_bars` 使用 `code IN (...)` 一次性读取并构造完整 `tuple[DailyBar, ...]`；`ParameterizedScreeningService.screen` 在单进程内逐只股票串行求值。随着数据规模增长（2026-08-31 时 `data/market.sqlite3` 约 198 MB、124 万条 `daily_bars`、5212 只股票），P5-A 回测、P5-B CAPM 与 P6 高维统计需要同一只读端口批量、可并发地读取本地数据，且不直接依赖 SQLite。

P4 决定：将“查询意图”与 SQL 实现分离，建设小型并发只读基础设施，并把参数化筛选接入单层分片执行。P4 不实现 CAPM、不回测、不追踪主力、不改变 Baostock 串行同步策略、不切换数据库产品。

## 决策

### 1. 只读访问层（read 包）

新增 `src/stock_manager/read/` 包，作为数据库无关的只读基础设施：

- `contracts.py`：`MarketDataReadRequest`、`DatasetReadSnapshot`、`MarketDataBatch`（不可变领域契约，无 SQL）。
- `protocols.py`：`MarketDataReaderProtocol`（单分片确定性读取）、`MarketDataReaderFactoryProtocol`（每任务创建独立 reader/连接）。
- `errors.py`：`ReadLayerError` 体系（`DatasetUnavailableError`、`AdjustmentMismatchError`、`SnapshotConsistencyError`、`ShardReadError`）。
- `sqlite_reader.py`：`SQLiteMarketDataReader` / `SQLiteMarketDataReaderFactory`（P4 唯一 SQLite 实现）。
- `service.py`：`MarketDataReadService`（分片、线程池、稳定合并、`max_workers=1` 串行回退、失败传播、进度事件）。
- `plan_view.py`：`PicklableScreeningPlan`（进程池 worker 可序列化的 ScreeningPlan 视图）。

### 2. 公共契约

#### `MarketDataReadRequest`

至少包含：

- `dataset_id: str`
- `codes: tuple[str, ...]`
- `start: date`、`end: date`
- `adjustment: AdjustmentMethod`（必须显式提供）
- `batch_size: int`
- `include_fundamentals: bool = False`
- `dividends_start: date | None = None`

排序契约：结果按 `code ASC, trading_day ASC`。构造时校验：数据集非空、日期区间有效、复权显式提供、批次大小为正、代码去空白并去重、分红窗口不晚于 `end`。

#### `DatasetReadSnapshot`

冻结数据集元数据（`dataset_id`、`trading_day`、`adjustment`、`source`、`synced_at`），在并发读取开始前固定；每个分片必须验证元数据一致，版本变化时显式失败，不得静默混合两个版本。

#### `MarketDataReaderProtocol`

单个 reader 只负责一个分片的确定性读取，协议禁止出现 `sqlite3.Connection`、SQL 文本或 SQLite PRAGMA；提供 `read_snapshot` 与 `read_batch`。

#### `MarketDataReadService`

- 按排序后的代码生成有界分片（`batch_size`）。
- 使用受控 `ThreadPoolExecutor` 并发读取；每个任务通过 reader factory 创建独立 reader/连接。
- `max_workers=1` 或小数据量（≤ `small_data_serial_threshold`）时使用同一逻辑的串行路径。
- 收集所有分片后按 `(code, trading_day)` 稳定排序。
- 任一分片失败时取消未开始任务并向上抛出 `ShardReadError`，禁止返回部分成功结果。
- 回调只报告已完成分片或已完成股票数。

#### `ScreeningShardExecutor`

只负责参数化筛选的分片编排，不重新定义规则、模板或组合语义。每个分片 worker：

- 使用 reader factory 创建自己的只读 reader/SQLite 连接。
- 读取本分片所需的行情、基本面和分红数据。
- 在 worker 内按股票构造 `RuleContext` 并调用 `RuleEngine.evaluate()`。
- 只返回本分片的结构化筛选结果与进度信息，不返回全量原始行情。

同一筛选调用链只允许一层并发调度：分片执行器不能在 worker 内再次创建读取线程池或进程池。`max_workers=1` 复用相同逻辑并串行执行（逐股进度回调保持与旧实现一致）。

### 3. 并发与数据库约束

1. SQLite 连接必须使用只读 URI（`file:...?mode=ro`）并启用 `PRAGMA query_only = ON`；P4 读取不得创建数据库或表。
2. 一个连接只属于一个 worker，不在线程或进程之间传递。
3. worker 数必须配置化且有上限（默认 4），不按 CPU 数无限扩张。
4. 股票代码必须分片，避免单条超大 `IN` 查询和 SQLite 参数数量限制。
5. 合并结果必须与串行结果逐项相等、顺序一致。
6. 并发只用于本地只读查询和无副作用的筛选计算；写入、同步、重试和上游限速完全不受 P4 影响。
7. 同一调用链禁止嵌套线程池与进程池；读取型调用使用受控线程池，筛选分片执行使用受控进程池，二者不得在 worker 内叠加。
8. 读取结果记录峰值内存；为后续流式批次保留扩展点。

### 4. 错误语义

- 数据集元数据不存在：`DatasetUnavailableError`。
- 数据集存在但复权不匹配：`AdjustmentMismatchError`。
- 分片查询失败：`ShardReadError`（携带分片标识与原始数据库异常链），禁止吞掉异常。
- 快照不一致：`SnapshotConsistencyError`，不返回部分数据。
- 用户取消：P4 不增加取消能力；若后续增加，应区分取消与失败。

### 5. 对后续阶段的接口承诺

- P5-A 回测与 P5-B CAPM 只依赖 `MarketDataReadService` 和数据库无关请求/快照类型。
- P6 只依赖相同接口读取特征窗口和回测验证数据。
- 未来数据库升级只替换 reader 实现与连接配置，不要求改写 CAPM、回测或主力模型。
- `ScreeningShardExecutor` 只属于参数化筛选通道；未来 CAPM/回测应分别增加独立的执行器，共用 read 基础设施但不复用筛选的计划/引擎。

## 验收要点

- 并发读取结果与串行读取结果完全一致且顺序稳定（逐项相等）。
- 并发筛选结果与串行筛选结果完全一致且顺序稳定。
- Provider 与写入路径没有被并发化。
- 公共协议中没有 SQLite 实现细节。
- 离线测试覆盖关键边界、错误和并发分支。
