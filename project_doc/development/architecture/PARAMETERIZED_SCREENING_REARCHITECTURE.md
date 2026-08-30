---
date: 2026-08-29
purpose: 记录 ParameterizedScreeningService 从逐只串行筛选升级为分片并发流水线的架构方案。
project: StockManager
status: draft
---

# 参数化筛选重架构：分片并发流水线

## 背景

当前 `ParameterizedScreeningService.screen` 的流程是：

1. 一次性从 SQLite 读取全部相关数据。
2. 按 code 分组到内存。
3. `for code in selected` 逐只构建 `RuleContext`，再串行传给 `RuleEngine.evaluate()`。

股票数量多时，第 3 步是纯 Python 串行计算，是主要耗时来源。

## 目标

- 保持现有 Web / CLI 对外行为不变。
- 让筛选过程并发执行，显著缩短全市场筛选时间。
- 不在主进程持有全量 bars，改为按分片并行读取与计算。

## 新架构

```text
ParameterizedScreeningService  (薄门面)
        |
        v
ScreeningPlanner + ShardBuilder
        |
        +-- Shard A --+-- Shard B --+-- Shard N
        |             |             |
        v             v             v
     ShardReader    ShardReader   ShardReader   # 各自从 SQLite 读取分片数据
        |             |             |
        v             v             v
     ShardEvaluator ShardEvaluator ShardEvaluator
        |             |             |
        +-------------+-------------+
                      v
         ResultCollector / ProgressBus
```

## 核心模块

### 1. `ScreeningShard`

可序列化分片任务，包含：

- codes
- stocks
- metadata
- dataset_id / trading_day / adjustment
- market_start / dividend_start
- 原始 template dict

### 2. `ShardReader`

在 worker 内读取本分片数据：

- `get_daily_bars(codes, market_start, end, adjustment)`
- `get_fundamentals(codes, trading_day)`
- `get_dividends(codes, dividend_start, end)`

### 3. `ShardEvaluator`

在 worker 内按股票构建 `RuleContext`，调用 `RuleEngine.evaluate()`，返回该分片结果。

### 4. `ScreeningExecutor`

负责：

- 切分 shard
- 使用 `ProcessPoolExecutor` 并行执行
- 按原始顺序合并结果
- `max_workers=1` 时串行回退

## 并发建议

- 规则计算是 CPU 密集，使用进程池而非线程池。
- 目标机 i3 / i5：
  - `max_workers=2~4`
  - 8GB 内存建议 2
  - 16GB 内存建议 3~4
- `chunk_size` 建议 32~128 只/片。
- 小数据量时自动走单 shard，避免进程启动开销。

## 序列化注意

- `ScreeningPlan` 含 `MappingProxyType`，不可直接 pickle。
- worker 接收原始 template dict，在 worker 内重新 `parse_template` + `TemplateCompiler`。

## 兼容性

- 保持 `ParameterizedScreeningResult` 结构与返回顺序不变。
- 保持 `progress_callback` 每只股票回调一次的语义。
- Web 的 `POST /api/screen` 与 CLI 的 `screen-template` 无需改调用方式。
