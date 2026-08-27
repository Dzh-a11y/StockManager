---
date: 2026-08-25
purpose: 记录 P1-2 领域模型、数据协议及其架构边界。
project: StockManager
status: active
---

# P1-2 领域模型与协议

## 领域模型

`stock_manager.domain` 定义不可变的 `StockIdentity`、`DailyBar`、`FundamentalSnapshot`、`DividendRecord`、`DatasetMetadata`、`RuleResult`、`ScreeningResult` 和 `SyncRecord`。价格、成交量、成交额及财务数值使用 `Decimal`；交易日使用 `date`；同步时间使用带时区的 `datetime`。

`DatasetMetadata` 显式记录 `dataset_id`、`trading_day`、`source`、`synced_at` 和 `adjustment`。`AdjustmentMethod` 只声明 `unadjusted`、`qfq` 和 `hfq` 三种标记，不提供默认复权策略。

`RuleResult` 固定包含 `rule_id`、`passed`、`actual_value`、`threshold` 和 `reason`。`SyncRecord` 通过 `PENDING`、`RUNNING`、`SUCCESS` 和 `FAILED` 表达同步状态；成功不得携带错误，失败必须有完成时间和明确错误。P1-5 新增 `SyncOutcome`，结构化区分正常成功与“数据已存在，跳过拉取”，跳过结果必须包含用户可见警告。

## 协议边界

- `ProviderProtocol` 返回标准化领域对象，日线数据请求必须显式传入复权方式。
- 只有 `DataSyncService` 可调用 `ProviderProtocol`。
- `LocalRepositoryProtocol` 同时定义同步写入和本地查询契约；所有数据集写入都携带元数据。P1-5 增加原子市场快照、原子交易日历覆盖写入和本地交易日查询契约，确保业务数据与 `SUCCESS` 状态同事务提交。
- 后续筛选路径只依赖 `LocalRepositoryProtocol`，不持有或调用 Provider。
- P1-5 已提供 SQLite 和 Provider 实现；协议仍不依赖具体实现。

## 验收

领域约束、复权显式性、时区要求、同步状态和协议结构均由离线单元测试覆盖。
