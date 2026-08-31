---
date: 2026-09-02
purpose: 记录 StockManager P5A-1 八年历史覆盖(v2 配置、coverage/generation、run-chunk v2 幂等回补)的实现、接口与离线验收结果。
project: StockManager
status: active
---

# P5A-1 八年覆盖与向前补齐

## 阶段定位

P5A-1 让本地数据库可证明地覆盖目标八年窗口(终点=最新已完成交易日,起点=终点向前 2080 个交易日),而不是只改保留天数。P5A-1 只改数据层与同步层:**v1 行为完全不变**(config 无 history 时走原有路径),现有一年数据不损坏。

## 已确认决策(2026-09-01,见 P5A_PLAN 第 1.1 节)

- 八年起止:终点=最新已完成交易日,向前 2080 个交易日;真实 coverage_start/coverage_end 落库。
- 复权口径统一 qfq(本阶段所有覆盖率与回补均按 qfq;结构上支持多口径并存)。
- 八年联网回补(约 3.8~4 h)本轮不执行,仅交付代码与离线测试。

## 新增/修改模块

```text
src/stock_manager/domain.py                    # +DataCoverageStatus/DatasetVersionStatus/
                                               #  BackfillRunStatus/DatasetCoverage/
                                               #  DatasetVersion/BackfillRunV2/BackfillChunkV2
src/stock_manager/sync/config.py               # v1/v2 加载器(history 块)
src/stock_manager/sync/data_sync_service.py    # +SyncHistoryConfig/backfill_history_v2/
                                               #  backfill_on_startup_v2/_resolve_targets_v2/
                                               #  _update_coverage_rows/_commit_generation
src/stock_manager/sync/history_plan.py         # 新增:plan_coverage/trading_day_lookback
src/stock_manager/storage/sqlite_repo.py       # +4 张 v2 表与读写方法
src/stock_manager/storage/integrity.py         # 新增:verify_database_integrity(只读)
src/stock_manager/sync/__init__.py             # 导出补齐
src/stock_manager/web/app.py                   # 启动回补按 history 有无路由 v2/v1
scripts/verify_db_integrity.py                 # 新增:迁移前后完整性报告 CLI

tests/test_sync_config_v2.py                   # 配置 v2/v1 兼容(12 项)
tests/test_history_plan.py                     # 规划器(8 项)
tests/test_backfill_v2.py                      # 回补/幂等/断点恢复/范围身份(11 项)
tests/test_integrity.py                        # 完整性报告(3 项)
```

## 配置 v2 契约

config/sync.json v1 保持可读(加载时发出迁移提示,history=None 行为与旧版完全一致)。v2 示例:

```json
{
  "metadata": { "version": 2, "timezone": "Asia/Shanghai" },
  "policy": { "cutoff_time": "17:30:00", "retry_cooldown_seconds": 300,
              "minimum_request_interval_seconds": 0.2,
              "calendar_horizon_days": 45, "dividend_lookback_years": 3 },
  "history": { "target_years": 8, "coverage_policy": "latest_completed_trading_day" }
}
```

- version 必须为 1 或 2;v1 携带 history 块直接拒绝。
- history.target_years 正整数;coverage_policy 仅支持 latest_completed_trading_day。
- v2 下 retention_days 可省略(默认 360),但 v2 回补**不按 retention 裁剪**——八年窗口由 target_years 决定,真实覆盖落库。

## 存储 v2 表

| 表 | 用途 | 关键约束 |
|---|---|---|
| dataset_versions | 不可变 generation(dataset/adjustment/generation/coverage 起止/状态) | 主键 (dataset_id, generation) |
| dataset_coverage | 各数据类型 earliest/latest/状态/缺口标记 | 主键 (dataset_id, adjustment, data_type) |
| backfill_runs_v2 | run 级身份:目标起止、状态、错误 | 主键 run_id |
| backfill_chunks_v2 | chunk 级 checkpoint:代码、**拉取区间**、bar 数 | 主键 (run_id, chunk_index, range_start, range_end) |

### checkpoint 身份设计(关键)

1. **run_id 绑定目标范围**:sha256(dataset|adjustment|target_start|as_of) 前 16 位。范围变化(如一年 → 八年)必然产生新 run_id,旧一年分片**不可能**冒充八年分片。
2. **chunk checkpoint 绑定拉取区间**:主键含 (range_start, range_end)。同一 chunk 跨多个缺口(前缀 + 尾部)各自成行,缺口之间中断不会误判已拉。
3. 断点恢复:run FAILED/RUNNING 重跑时,仅当 checkpoint 的 codes **且** 区间完全匹配才跳过,否则重拉该缺口;幂等:同范围 SUCCESS run 直接警告跳过,Provider 零调用。

## CoveragePlanner 语义

- trading_day_lookback(days, end, count):终点向前第 N 个交易日;日历不足时回到最早可用日(真实覆盖另行落库,不静默假定)。
- plan_coverage(...):对 daily_bars/fundamentals/stocks/dividends 各自报告 COMPLETE / PARTIAL / UNAVAILABLE 与缺口。
- 缺口端点为**自然日**(拉取区间语义),Provider 内部按交易日过滤;状态判定按交易日边界比较。

## v2 回补流程

backfill_history_v2(dataset_id, adjustment, *, target_start, as_of, batch_size=100):

1. 计算 run_id;进程锁 + 持久化文件锁(复用 locks.py)。
2. 幂等:同 run_id 且 SUCCESS → 警告跳过;RUNNING → 警告接管。
3. 保存 RUNNING run;用 plan_coverage 计算缺口(以 daily_bars 驱动拉取区间)。
4. 无缺口:仅刷新 coverage 行与 generation 后结束。
5. 有缺口:Provider 锁内串行——拉交易日历(目标区间)、as_of 股票池、逐 chunk 按缺口区间拉 bars + fundamentals(as_of),逐 chunk 落库并写 checkpoint。
6. 完成后:刷新 dataset_coverage、提交 COMPLETE generation(仅当全部数据类型 COMPLETE)、写 SUCCESS run。

backfill_on_startup_v2:解析八年窗口(_resolve_targets_v2,按 target_years×366+45 天拉取交易日历)→ 规划 → 有缺口回补,无缺口仅刷新 coverage/generation。Web 启动线程在 config.history 非空时路由到 v2,否则保持 v1(消息与行为均不变)。

### generation 门禁

仅当 plan.overall_status == COMPLETE 才提交 generation(market-<target_end>-<hash8>);PARTIAL/UNAVAILABLE 不产生可回测 generation。回测绑定 generation 的机制由 P5A-2 接入。

## 完整性校验

verify_database_integrity(db)(只读 URI + query_only,绝不写入)输出:PRAGMA integrity_check、各表行数、daily_bars 重复键计数、按复权的覆盖边界、交易日历缺口抽样、每代码边界抽样、EXPLAIN QUERY PLAN 样本(历史读取形态的索引证据)、文件大小。CLI:python3 scripts/verify_db_integrity.py data/market.sqlite3 [--out report.json],迁移前后各跑一次比对。

## 离线测试验收

python3 -m pytest:**298 passed**(新增 32 项;266 基线全绿,v1 同步测试无回归)。

关键覆盖:

- 配置:v2 加载、v1 兼容 + 迁移提示、v1+history 拒绝、版本/时区/参数错误。
- 规划器:lookback 边界(第 1/21/101 个、日历不足回退、非法输入)、COMPLETE/PARTIAL/UNAVAILABLE、自然日缺口端点。
- 回补:首次落库(run/chunk/coverage/bars)、同范围幂等(Provider 零调用)、**中断恢复只拉缺失缺口**(fetch 调用数精确断言)、范围变化新 run_id、失败记录 FAILED 并抛 SyncFailedError、参数校验、v1 配置下 startup_v2 拒绝、generation 仅在 COMPLETE 提交、存储实体往返与幂等覆盖。
- 完整性:空库/有数据报告、只读不写入。

## 说明与后续

- 八年真实回补需联网约 3.8~4 h(串行受控),由用户按 v2 配置自行执行;执行后应跑完整性脚本留档。
- 历史股票池(P5A-2 契约):v2 回补仍只保存 as_of 快照,历史逐日股票池/ST/退市状态属于 P5A-2 point-in-time 契约,不在本阶段声称已解决。
- 种子 ADR(qfq + 对策 a)已在 P5A-0 定稿,种子生成=本阶段 backfill 的离线模式,随 P5A-1 后打包流程使用。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。
