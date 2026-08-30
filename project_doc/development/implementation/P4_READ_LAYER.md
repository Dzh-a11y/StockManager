---
date: 2026-08-31
purpose: 记录 StockManager P4 可替换数据库只读访问层、并发读取服务与参数化筛选分片执行的实现、接口与离线验收结果。
project: StockManager
status: active
---

# P4 可替换数据库只读访问与分片筛选基础设施

## 阶段定位

P4 是一个小而独立的基础设施阶段：解决本地数据库的批量读取、分片并发、参数化筛选分片执行、稳定排序和数据库后端可替换问题。P4 不实现 CAPM、不实现回测、不实现主力追踪，也不改变 Baostock 同步串行策略。

P4 完成后，P5-A 回测、P5-B CAPM 和 P6 高维统计可以通过同一只读端口读取数据，不直接依赖 SQLite，也不重复实现并发逻辑。

## 源码事实（2026-08-31 基准）

| 项 | 值 |
| --- | --- |
| 数据库文件 | `data/market.sqlite3`，207,163,392 字节（约 198 MB） |
| `daily_bars` 行数 | 1,243,232 |
| 股票数 | 5212 |
| 日期范围 | 2025-09-01 ~ 2026-08-28 |
| 复权标记 | 全部 `qfq` |
| 最新交易日 | 2026-08-28 |
| 基准机器 | Apple M5 Pro，48 GB 统一内存 |
| 软件版本 | macOS 26.6.2 / Python 3.14.7 / SQLite 3.50.4 |

## 公共契约

精确命名见 `ADR_P4_READ_LAYER.md`；以下为语义摘要。

### `MarketDataReadRequest`

- `dataset_id: str`、`codes: tuple[str, ...]`、`start: date`、`end: date`、`adjustment: AdjustmentMethod`、`batch_size: int`。
- 可选：`include_fundamentals: bool`、`dividends_start: date | None`。
- 校验：数据集非空、日期区间有效、复权显式提供、批次大小为正、代码去空白去重、分红窗口不晚于 `end`。
- 排序契约：`code ASC, trading_day ASC`。

### `DatasetReadSnapshot`

冻结数据集元数据（`dataset_id`、`trading_day`、`adjustment`、`source`、`synced_at`）。并发读取开始前固定；每个分片验证元数据一致，版本变化显式失败。

### `MarketDataReaderProtocol` / `MarketDataReaderFactoryProtocol`

单个 reader 只负责一个分片的确定性读取；协议禁止出现 `sqlite3.Connection`、SQL 文本或 SQLite PRAGMA。factory 每次调用创建独立 reader/连接。

### `MarketDataReadService`

- 按排序后的代码生成有界分片（`batch_size`，默认 500）。
- 受控线程池并发读取；每个任务通过 reader factory 创建独立 reader/连接。
- `max_workers=1` 或小数据量时使用同一逻辑的串行路径。
- 合并后按 `(code, trading_day)` 稳定排序。
- 任一分片失败时取消未开始任务并抛 `ShardReadError`，禁止返回部分成功结果。
- 进度回调只报告已完成分片或已完成股票数。

**默认配置（按实测证据）**：`max_workers=1`（SQLite 只读并发受 GIL 与磁盘带宽限制，实测并发更慢；保留并发能力供未来 DuckDB/PostgreSQL 后端）。

### `ScreeningShardExecutor`

只负责参数化筛选的分片编排，不重新定义规则、模板或组合语义。每个分片 worker：

- 使用 reader factory 创建自己的只读 reader/SQLite 连接。
- 读取本分片所需的行情、基本面和分红数据。
- 在 worker 内按股票构造 `RuleContext` 并调用 `RuleEngine.evaluate()`。
- 只返回本分片的结构化筛选结果与进度信息，不返回全量原始行情。

同一筛选调用链只允许一层并发调度；`max_workers=1` 复用相同逻辑并串行执行（逐股进度回调与旧实现一致）。默认 `max_workers=4`（进程池实测 3.8 倍加速）。

## 模块清单

```text
src/stock_manager/read/
├── __init__.py      # 公共导出
├── contracts.py     # MarketDataReadRequest / DatasetReadSnapshot / MarketDataBatch
├── errors.py        # ReadLayerError 体系
├── protocols.py     # MarketDataReaderProtocol / MarketDataReaderFactoryProtocol
├── plan_view.py     # PicklableScreeningPlan（进程池 worker 视图）
├── service.py       # MarketDataReadService（分片+线程池+合并）
└── sqlite_reader.py # SQLiteMarketDataReader / SQLiteMarketDataReaderFactory

src/stock_manager/services/
└── screening_shard_executor.py  # ScreeningShardExecutor（进程池分片筛选）

src/stock_manager/services/parameterized_screening_service.py  # 接入分片执行器

scripts/
├── bench_p4_baseline.py  # P4-0 串行路径基准
└── bench_p4_compare.py   # P4-6 串行 vs 并发对比基准
```

## 性能证据（P4-6，真实数据库，全市场 5212 只）

原始 JSON 证据归档于 `scripts/bench_results/2026-08-31_p4_baseline.json`（P4-0）与 `scripts/bench_results/2026-08-31_p4_compare.json`（P4-6），可由 `scripts/bench_p4_baseline.py` 与 `scripts/bench_p4_compare.py` 复现。

### 只读批量读取（365 自然日窗口，124 万行 bars + 基本面 + 分红）

| 路径 | 耗时 | 说明 |
| --- | --- | --- |
| 串行 `max_workers=1` | 5.73 s | 读取服务默认路径 |
| 线程池 `max_workers=4` | 15.21 s | 更慢：GIL + 磁盘带宽限制 |
| 线程池 `max_workers=8` | 50.21 s | 显著更慢，验证无并发收益 |

结论：**SQLite 只读并发读取无收益**。按计划 P4-6 条款保留架构并默认串行，不伪造加速结论。

### 参数化筛选（system-default 模板，全市场）

| 路径 | 耗时 | 说明 |
| --- | --- | --- |
| 串行 `max_workers=1` | 7.42 s | 旧路径基准 |
| 进程池 `max_workers=4` | 1.95 s | **3.8 倍加速**，结果逐项相等 |
| Web `/api/screen`（真实链路） | 2.35 s | 状态 200，5212 只，124 通过 |

### 一致性验收

- 并发读取与串行读取结果逐项相等（bars/fundamentals/dividends/snapshot）。
- 并发筛选与串行筛选结果逐项相等且顺序稳定。
- 多次运行顺序确定。

## 离线测试验收

执行 `python3 -m pytest`，离线环境全部通过：

```text
260 passed, 2 warnings in 1.34s
```

新增测试：

- `tests/test_read_layer.py`（17 项）：请求校验、快照、批次、SQLite reader 边界（空代码、重复代码、无序、空结果、缺失元数据、复权不匹配、大代码列表分块、严格只读）。
- `tests/test_read_service.py`（8 项）：并发与串行等价、小数据串行回退、重复运行稳定、分片失败传播、快照变化检测、max_workers 校验、进度事件、无 Provider 导入。
- `tests/test_read_faults.py`（14 项）：数据库锁不挂起、并发读一致快照、连接隔离、缺失数据库、拒绝写入、空数据集、超大股票池分片、串行/并发等价（1/2/4/8 workers）、close 幂等、空分片边界、并发读期间写入不混合快照。
- `tests/test_screening_shard_executor.py`（10 项）：并行与串行筛选等价、小数据串行、空代码、逐股进度回调（串行）、确定性、worker 失败传播。

## 独立代码审查与修复

P4 交付后由独立审查代理逐行审查全部 11 个相关文件并实测验证，结论为 CONDITIONAL（无 CRITICAL）。以下 MAJOR 问题已全部修复并补充回归测试：

| 编号 | 问题 | 修复 |
| --- | --- | --- |
| M1 | SQLite 读取器包装路径上 `ShardReadError.shard_index` 恒为 0，分片标识失真 | `MarketDataReadService` 串行/并发路径用真实分片 index 重包（保留 cause 链），回归测试 `test_shard_index_preserved_on_db_error` |
| M2 | 筛选分片路径完全绕过快照一致性验证，无法防止混合版本 | `_read_shard` 内比对 `batch.snapshot` 与冻结元数据，不一致抛 `SnapshotConsistencyError`；回归测试 `test_screening_worker_detects_snapshot_change` |
| M3 | 进程池失败路径不取消未完成任务，`with` 退出等待全部跑完 | 失败时 `other.cancel()` 取消未完成任务；回归测试 `test_parallel_failure_cancels_pending_shards` |
| M4 | worker 异常以原始异常透传，不带分片上下文 | worker 异常包装为带 `shard_index` 与 `cause` 的 `ShardReadError`；失败测试断言更新 |
| M5 | 协议表面缺少连接生命周期方法，服务层隐式依赖 context-manager | `MarketDataReaderProtocol` 显式声明 `close`/`__enter__`/`__exit__` |

MINOR 修复：fundamentals 平局排序加 `report_date`（m1）；dividends 排序加 `source`（m2）；`AdjustmentMismatchError` 保留原始异常链（m3）；`PicklableScreeningPlan` 保留原始参数与 schema 版本（m4）；`_serial_screen` 精确类型标注（m5）；补充无 database_path 的回退路径测试（m6）；executor 入口校验 codes 归属（m7）；清理多余空行（m8）。

## 进程池入口约束（spawn）

筛选分片执行使用 `ProcessPoolExecutor`（macOS/Windows 默认 spawn）。spawn 子进程会重新导入主模块，因此任何触发并发筛选路径的脚本入口必须带 `if __name__ == "__main__"` 保护，否则子进程会递归执行顶层代码导致 `BrokenProcessPool`。

- 内置入口已满足：`src/stock_manager/cli/main.py` 与 `src/stock_manager/cli/__main__.py`（P4 修复）均带保护；Web/CLI 调用方无需额外处理。
- 自定义脚本调用 `ParameterizedScreeningService.screen`（且数据量超过串行阈值、`max_workers > 1`）时必须自行添加主模块保护。

## 错误语义

| 场景 | 异常 |
| --- | --- |
| 数据集元数据不存在 | `DatasetUnavailableError` |
| 数据集存在但复权不匹配 | `AdjustmentMismatchError` |
| 分片查询失败 | `ShardReadError`（含分片标识与原始异常链） |
| 快照不一致 | `SnapshotConsistencyError`（不返回部分数据） |

## 对后续阶段的接口承诺

- P5-A 回测与 P5-B CAPM 只依赖 `MarketDataReadService` 和数据库无关请求/快照类型。
- P6 只依赖相同接口读取特征窗口和回测验证数据。
- 未来数据库升级只替换 reader 实现与连接配置。
- `ScreeningShardExecutor` 只属于参数化筛选通道；未来 CAPM/回测应分别增加独立执行器，共用 read 基础设施。

## 免责声明

所有筛选结果仅供研究参考，不构成任何投资建议。项目禁止实现自动交易功能。
