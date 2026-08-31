---
date: 2026-09-02
purpose: 记录 StockManager P5A-4 HistoricalScreeningExecutor(股票轴并行、时间轴顺序、compact/audit)的实现与离线验收结果。
project: StockManager
status: active
---

# P5A-4 HistoricalScreeningExecutor

## 阶段定位

P5A-4 复用现有规则能力,实现五年/八年逐日历史信号生成:并行只发生在股票轴(进程池分片),每个 worker 内严格沿评估日 T 顺序推进;每 shard 一次读入整段历史,禁止按交易日重复全市场查询;结果确定性合并;worker 失败整体失败;数据集 generation 绑定校验。

## 新增/修改模块

```text
src/stock_manager/services/historical_screening_executor.py  # 新增:请求/快照/结果契约、
                                                             #  worker、executor、参考实现
src/stock_manager/read/historical.py                        # +all_universe_snapshots(批量快照)
scripts/bench_p5a_historical.py                             # 新增:worker/shard 规模基准
tests/test_historical_screening.py                          # 一致性/并行/只读一次/门禁(8 项)
```

## 契约

HistoricalScreeningRequest:dataset_id、adjustment、generation(绑定)、warmup_start/score_start/score_end、evaluation_days(升序去重且位于计分窗口内)、output_mode(compact/audit)。

EligibilitySnapshot(trading_day, eligible_codes, selected_count);HistoricalScreeningResult(snapshots 按日升序、result_fingerprint、provenance)。

## 执行模型

1. **一次读取**:worker 用 PIT reader 一次性读取 bars_through(score_end)、fundamentals_through(score_end)、all_universe_snapshots()(新增批量快照接口),内存中按 code 组织;universe_as_of 由快照序列在内存推导,评估日内零数据库查询。
2. **时间轴顺序**:worker 内对每个评估日 T 计算当日股票池(最近快照 + 上市/退市过滤),对每只股票构造 RuleContext(T 当日合成元数据、截至 T 的 bars 窗口、published_on <= T 的最新基本面),调用 RuleEngine.evaluate。
3. **股票轴并行**:单层 ProcessPoolExecutor,每 shard 一个 payload(可 pickle:数据库路径 + 请求 + PicklableScreeningPlan + 分片代码);max_workers=1 或小分片数走同一逻辑的串行路径(逐 shard 处理全部,非只处理第一个)。
4. **确定性合并**:coordinator 按评估日归并各 shard 结果,eligible codes 排序合并;result_fingerprint 为逐日代码摘要的 sha256。
5. **失败语义**:任一 shard 失败(含数据库打开失败)抛 HistoricalScreeningError,禁止部分成功交付。
6. **generation 门禁**:request.generation 非空时先与 committed_generation 比对,不一致抛 DatasetGenerationMismatchError。

## 读取次数保证

每 shard 恰好一次 bars_through(测试用计数 reader 断言:2 个 shard → 2 次调用);全市场查询只发生在协调器 _all_codes(一次)与 worker 的批量快照读取(一次/worker),不存在按评估日循环的全市场查询。

## 离线基准(2026-09-02,fixture 300 股 × 120 交易日)

脚本 scripts/bench_p5a_historical.py(--stocks/--days/--real-db/--out):

| 配置 | 耗时 | fingerprint |
|---|---|---|
| w1_b100 | ~0.05s | 8a7724f459f8f613 |
| w2_b250 | ~0.05s | 8a7724f459f8f613 |
| w4_b250 | ~0.08s | 8a7724f459f8f613 |
| w4_b500 | ~0.08s | 8a7724f459f8f613 |

全部配置结果指纹一致(确定性)。

**真实库一年样本基准(2026-09-02,data/market.sqlite3,5200+ 股票,135 个评估交易日)**:

| 配置 | 耗时 | 相对 w1 |
|---|---|---|
| w1_b100 | 11.7s | 1x |
| w2_b100 | 6.4s | 1.8x |
| w4_b100 | 3.3s | 3.5x |
| w4_b250 | 11.8s | 1x |
| w4_b500 | 12.1s | 1x |

结论:**shard size 100 显著优于 250/500**(大 shard 下 SQLite 并发读取竞争与内存放大抵消并行收益),默认 batch_size 已按实测改为 100;4 worker x shard 100 相对串行有 3.5 倍真实收益(满足 P5A 第 13 节验收线)。各配置 fingerprint 一致(确定性)。八年规模(约 2080 交易日)收敛基准以同一脚本在八年数据就绪后复核。原始数据:scripts/bench_results/2026-09-02_p5a_historical_real1y.json。

## 离线测试验收

python3 -m pytest:**331 passed**(P5A-4 新增 8 项;无回归)。覆盖:串行结果语义(000002 连涨通过)、并行与参考结果逐项相等(snapshots + fingerprint)、重复运行确定性、每 shard 只读一次(bars_through 调用数)、generation 不匹配拒绝、worker 失败整体失败、请求参数校验、空 universe 空结果。

## 说明与后续

- audit 模式当前输出规则级 (rule_id, status) 摘要;规则级 actual/threshold/reason 明细与抽样解释由 P5A-5 运行存储承接。
- 八年全量运行的实测基准(worker 1/2/4 × shard 50/100/250/500)在八年数据就绪后执行(P5A-9 性能门禁),本阶段已交付脚本与一致性证据。
- 历史 ST 状态取最近快照 is_st(P5A-2 注明),逐日精确 ST 序列依赖 Provider 能力评估。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。

