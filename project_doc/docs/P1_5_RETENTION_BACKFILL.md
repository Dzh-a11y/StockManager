---
date: 2026-08-25
purpose: 补充 P1-5 本地同步与存储的历史保留与首次回补设计。
project: StockManager
status: active
---

# P1-5 补充：历史保留与首次回补

> 本文是 [`P1_5_LOCAL_SYNC_STORAGE.md`](P1_5_LOCAL_SYNC_STORAGE.md) 的补充，聚焦历史数据保留策略与首次运行的区间回补机制。

## 背景与目标

原 P1-5 的同步是「单交易日快照」：每次 `sync` 只拉最新已完成交易日。这带来两个问题：

1. **历史积累无界**：`daily_bars` 与 `stocks` 每个交易日各新增约「股票数」行，磁盘只增不减。
2. **首次运行无历史**：全新安装没有任何历史日线，长回看规则（最长 180 个交易日）无法生效。

本补充引入两个机制：可配置的历史保留窗口，以及首次运行的区间回补。

## 配置：`retention_days`

`config/sync.json` 的 `policy` 新增 `retention_days`，整数、默认 `360`（自然日，约覆盖一年、244 个交易日）。

- 加载器 `load_sync_config` 缺省时回落 `360`。
- `SyncConfig.retention_days` 校验必须为正数。

示例：

```json
{
  "policy": {
    "retention_days": 360
  }
}
```

## 存储层：`prune_before`

`SQLiteRepository.prune_before(cutoff: date)` 删除严格早于 `cutoff` 的数据：

| 表 | 清理条件 |
|---|---|
| `daily_bars` | `trading_day < cutoff` |
| `stocks` | `as_of < cutoff` |
| `sync_runs` | `trading_day < cutoff` |
| `dataset_metadata` | `trading_day < cutoff` |

`sync_runs` 与 `dataset_metadata` 与市场数据一并删除，避免「数据已删但状态仍标 SUCCESS」导致误判为已同步。`fundamentals`、`dividends`、`trading_days` 增长缓慢或另有用途，不在清理范围。

## 同步层：区间回补

`DataSyncService.backfill_history(dataset_id, as_of, adjustment, *, batch_size=100)`：

- 计算窗口起点 `start = as_of - retention_days`。
- 用 `fetch_daily_bars(codes, start, as_of, adjustment)` 区间查询取窗口日线（Baostock `query_history_k_data_plus` 支持日期区间），而不是按交易日循环。
- **分块增量落库**：先保存交易日历与当前股票列表；随后按代码分块（默认每块 100 只）循环——每拉完一块立即写入 `daily_bars`、`fundamentals`、`dividends`，数据库行数实时增长。中途失败或中断时，已落库的块全部保留，不会整窗白拉。
- 全部完成后 `prune_before(start)` 清理窗口外旧数据，并记录 `SUCCESS`。
- 沿用与 `sync` 相同的锁、幂等状态与失败记录（`RUNNING`/`SUCCESS`/`FAILED`）。

## 启动编排：`backfill_on_startup`

`DataSyncService.backfill_on_startup(dataset_id, adjustment)`：

1. 读取该数据集的最近 `dataset_metadata`。
2. 无元数据（全新安装）→ 解析最新已完成交易日后调用 `backfill_history` 全量回补。
3. 有元数据 → 复用 `sync_missing_on_startup` 只补齐最近成功日之后的缺失交易日。

## Web 启动集成

`WebApp` 在启动时（当 `--sync-config` 与 `--lock-dir` 已配置）于后台线程执行 `backfill_on_startup("market", qfq)`，与前端默认数据集/复权一致，进度上报 `/api/sync/progress`。首次回补的 Provider 请求量可达数万次，因此不阻塞 HTTP 请求路径。

## 容量与性能

按 A 股约 5400 只股票、单日单复权约 1.14 MB 计，保留 360 天约 0.3–0.6 GB（视复权数）。删除旧数据后 SQLite 复用页、体积稳定在窗口峰值；如需回收磁盘可另行 `VACUUM`。

## 验收

离线测试覆盖：`retention_days` 默认值与校验、`prune_before` 删旧留新、`backfill_history` 区间拉取与删旧、`backfill_on_startup` 首次回补/二次增量。全部使用 `FixtureProvider`，不访问网络。
