---
date: 2026-08-25
purpose: 记录 P1-5 本地 SQLite 存储、受保护数据同步和失败恢复设计。
project: StockManager
status: active
---

# P1-5 本地同步与存储

## 数据边界

`DataSyncService` 是唯一持有 `ProviderProtocol` 的业务服务。Baostock 适配器只负责远程请求和领域对象标准化；SQLite 仓储不访问网络；规则层不依赖 Provider 或仓储。后续筛选服务只能读取 `LocalRepositoryProtocol`。

默认同步策略位于 `config/sync.json`，明确记录版本、`Asia/Shanghai`、17:30 数据截止时间、五分钟失败重试冷却、Provider 请求最小间隔、交易日历覆盖天数、分红回看年度，以及本地历史保留天数 `retention_days`（默认 360 天，可配置）。配置由严格加载器转换为 `SyncConfig`，错误类型或时区会显式失败。

## SQLite 结构

`SQLiteRepository` 使用调用方传入的数据库路径，不包含本机绝对路径。数据库包含 `stocks`、`daily_bars`、`fundamentals`、`dividends`、`trading_days`、`sync_runs` 和 `dataset_metadata`。

Decimal 使用文本保存，日期和带时区时间使用 ISO 8601。日线主键包含复权方式，查询时必须显式指定 `unadjusted`、`qfq` 或 `hfq`。

市场快照的数据、元数据和 `SUCCESS` 状态在同一 SQLite 事务提交。Provider 请求全部成功前不会写入业务数据；请求失败或数据不完整时只记录 `FAILED`，不会留下部分快照。

## 幂等与并发

同一 `dataset_id + trading_day` 同时使用进程内共享锁和 OS `flock` 持久化锁文件；Provider 另有全局进程锁和文件锁，确保不同数据集的外部请求仍然串行。

锁内会再次读取 `sync_runs`。若已经 `SUCCESS`，服务发出“数据已存在，跳过拉取”警告，返回本地元数据且不调用 Provider。相同数据集和交易日不能换用另一种复权方式覆盖。

## 失败、重试与限流

同步开始先记录 `RUNNING`。任何 Provider、校验或仓储异常都会转换为 `SyncFailedError`，同时持久化 `FAILED` 和具体错误。失败后普通调用会抛出 `RetryRequiredError`；只有显式 `retry=True` 且冷却期结束后才能再次请求。系统不做无限自动重试。

Provider 适配器对瞬时失败（网络 `OSError` 或 baostock 返回非零 `error_code`）按指数退避自动重试（默认 3 次、1s/2s/4s），解析错误等永久性失败不重试。请求限速只保留 Provider 内部这一层：每一次实际 SDK 查询（逐股票、逐年度的日线/财务请求）按 `minimum_request_interval_seconds` 间隔串行限速，不建立无上限并发池；同步服务本身不再叠加限速，避免双重 sleep。

历史回补 `backfill_history` 支持断点续传：重启后按代码批次检查本地 `daily_bars` 与 `fundamentals`，已完整落库的批次直接跳过，只重拉未完成部分。启动时若发现上次留下卡死的 `RUNNING` 记录（进程中断），会发出警告并接管重跑，不会从头下载整个保留窗口。

## 交易日和首次启动补齐

交易日判断基于本地 A 股交易日历和 `Asia/Shanghai` 截止时间，不使用“自然日前一天”。首次启动检查先按覆盖日期幂等更新交易日历，再计算最新已完成交易日，并按顺序同步本地缺失交易日。已经成功的日期不会重复拉取；历史失败不会自动重试。

全新安装（本地没有任何 `dataset_metadata`）时，Web 启动会触发一次历史回补：`backfill_history` 用 Provider 的日期区间查询一次拉取 `retention_days` 窗口内的日线（而不是按交易日循环），并同步当前股票列表、财务与分红。已有安装只补齐其最近成功同步日之后的缺失交易日。

## 历史保留与清理

本地数据库只保留 `retention_days` 窗口内的市场数据。每次成功同步或回补后，`prune_before` 删除 `daily_bars` 与 `stocks` 中早于窗口起点的行，并同步删除对应 `sync_runs` 与 `dataset_metadata`，避免已删除的日期被误判为“已成功同步”。`fundamentals`、`dividends` 与 `trading_days` 增长缓慢或另有用途，不在清理范围。

完整的回补与保留设计见补充文档 [`P1_5_RETENTION_BACKFILL.md`](P1_5_RETENTION_BACKFILL.md)。

## 数据完整性

同步成功前会校验目标交易日、股票代码与交易日唯一性、每只当日股票恰好一条目标日日线，以及财务和分红记录的代码范围。市场数据集固定同步 Provider 返回的完整当日股票集合，不允许用同一数据集标识保存部分股票后误记整日成功。停牌日线若 Baostock 返回空 OHLCV，则使用前收盘填充 OHLC、成交量和成交额置零，并保留 `is_trading=False`。

## 验收

离线测试覆盖 SQLite 往返、显式复权、重复同步、并发竞争、持久化锁、原子失败、空数据拒绝、非交易日、上海时区截止、周末回溯、首次启动补齐、显式重试、冷却、限流、停牌和 Baostock 复权映射。

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONPYCACHEPREFIX=/tmp/stockmanager-p1-5-pycache .venv/bin/python -m compileall -q src tests
```

结果：`72 passed`，字节码编译通过，测试没有访问网络。真实 Baostock 在线连通性留作显式 online 测试，不属于默认单元测试。
