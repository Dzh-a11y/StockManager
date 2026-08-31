---
date: 2026-09-02
purpose: 记录 StockManager P5A-5 历史信号缓存键、运行状态机与 eligibility 存储的实现与离线验收结果。
project: StockManager
status: active
---

# P5A-5 历史信号缓存和运行存储

## 阶段定位

P5A-5 让筛选资格可复用(缓存命中不重跑规则引擎),并为异步任务提供可恢复的持久化状态机:创建、进度、取消、进程重启后的 INTERRUPTED 恢复、结果分页与清理。

## 新增/修改模块

```text
src/stock_manager/domain.py                              # +HistoricalRunStatus/HistoricalScreeningRun
src/stock_manager/storage/sqlite_repo.py                # +historical_screening_runs/eligibility_days/
                                                         #  eligibility_members 表与读写方法
src/stock_manager/services/historical_screening_cache.py# 新增:完整缓存键(historical_cache_key)
src/stock_manager/services/historical_screening_run_store.py# 新增:状态机/进度/取消/INTERRUPTED/清理
tests/test_historical_runs.py                           # 状态机/缓存键/eligibility 存储(9 项)
```

## 状态机

QUEUED → VALIDATING → BUILDING_SIGNALS → SUCCEEDED;各阶段可 → FAILED 或 CANCEL_REQUESTED → CANCELLED;非终态在进程重启后被 recover_interrupted 标为 INTERRUPTED,可 enqueue_interrupted 重新入队。非法迁移抛 InvalidRunTransitionError;进度更新仅允许 BUILDING_SIGNALS 且 0 <= completed <= total。

## 缓存键

historical_cache_key 覆盖:dataset_id + 不可变 generation + adjustment + universe_policy + 评估起止 + schedule + 模板 id/revision + plan fingerprint + 规则实现版本 + 交易日历 fingerprint。**策略层参数(手续费、初始资金、SMA 周期、持有日数)不进入键**——策略参数变化复用同一份纯资格信号。数据/模板/规则版本变化必失效(键变化)。

## 存储

- historical_screening_runs:run 级状态与进度(run_id 主键、cache_key 索引语义、起止、错误)。
- eligibility_days:(run_id, trading_day) → selected_count;eligibility_members:(run_id, trading_day, code)。
- 结果分页:list_eligibility_days(offset/limit)、list_eligible_codes(run_id, day);清理:prune_historical_runs 保留最新 N 个 run 并级联删除其 day/member 行。
- find_successful_run_by_cache_key:同一键的 SUCCESS run 供缓存命中。

## 离线测试验收

python3 -m pytest:**340 passed**(P5A-5 新增 9 项;无回归)。覆盖:完整生命周期迁移、非法迁移拒绝(终态后 FAILED 拒绝、QUEUED 直跳 BUILDING 拒绝)、失败记录错误、取消流程、INTERRUPTED 恢复(非终态 2 个被标、终态不受影响)与重新入队、进度约束、eligibility 落库与分页、缓存命中与清理(keep=1 删旧留新)、缓存键稳定与敏感(模板 revision/generation/规则版本/窗口变化必变)。

## 说明与后续

- P5A-8 的有界 job runner 消费本状态机(QUEUED→…→SUCCEEDED),取消通过 CANCEL_REQUESTED 传播。
- audit 明细存储(规则级 actual/threshold/reason)未在本阶段落库;compact 资格结果已可持久化,audit 抽样存储由 P5A-9 验收按需补充。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。
