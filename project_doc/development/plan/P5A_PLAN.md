---
date: 2026-09-02
purpose: 定义 StockManager P5A 的历史筛选、股票轴并行与 Backtrader 组合回测一体化架构、任务包和验收标准。
project: StockManager
status: active
---

# P5A：历史筛选与 Backtrader 一体化回测方案

## 0. 文档定位

P5A 是 P5 的第一阶段，名称正式采用 **P5A**。它不是另起一套 Backtrader 产品，而是在 StockManager 现有筛选模板、规则引擎、本地 SQLite 数据和 P4 并行执行器之上，增加“历史信号生成 → 组合回测 → 结果展示”的研究闭环。

本方案根据当前源码和 P4 基准制定。它在通过架构决策后，取代旧 `PHASE_5_PLAN.md` 中 P5 第一阶段使用 `bt` 的部分；旧文档中的后续研究能力不在本次直接实施。

### 0.1 AI 分工声明

- 架构与任务拆分代理：**Codex**。
- 实现、测试、调试、代码审查与文档事实核对代理：**DeepSeek V4 Flash**。
- Codex 本轮只交付架构、接口边界、任务包和验收标准，不承担 P5A 实现。

### 0.2 产品边界

- StockManager 继续定位为 A 股研究型筛选平台。
- P5A 只做离线研究回测，严禁实盘下单、券商连接、自动交易和在线行情订阅。
- 所有筛选与回测结果仅供研究参考，不构成投资建议。

## 1. 已确认决策与待决策项

### 1.1 用户已经确认

1. 回测引擎采用 **Backtrader**，不采用 `bt`。
2. 本地行情历史目标长度从约一年扩展到 **八年**。
3. 产品保持同一个前端工作台，不为 Backtrader 单独开发一套 UI。
4. 用户先在 StockManager 制定并运行筛选模板，再把该模板用于历史回测。
5. 历史筛选默认在回测区间的**每个有效交易日**重新生成信号。
6. 时间轴不做并行；只对股票代码分片并行。
7. **复权口径（2026-09-01）**：筛选输入与回测成交价格统一采用 `qfq`；除权拼接断层按对策 a（检测除权事件后重拉受影响股票）处理，见 `ADR_P5A_SEED_DISTRIBUTION.md`。
8. **成交时点（2026-09-01）**：T 日收盘数据完整后生成 `EligibilitySnapshot(T)`，策略 T 日收盘形成目标订单，市价单最早 T+1 日开盘尝试成交；严禁默认开启 cheat-on-close/open。
9. **A 股成交约束等级（2026-09-01）**：P5A-7 实现完整组合——100 股整手与余股、T+1 可卖数量、停牌不可成交、涨跌停不可买/卖与价格边界、佣金最低收费、印花税、过户费及生效日期、滑点、现金不足与部分成交政策；未实现项显式显示，不得静默降级。
10. **候选排名策略（2026-09-01）**：当日通过候选超过最大持仓数时，按评估日及之前 **20 个交易日平均成交金额降序**取前 N，成交额相同按股票代码升序（稳定同分键）；禁止隐含或随机截断。
11. **八年数据起止定义（2026-09-01）**：终点为**最新已完成交易日**，起点为终点向前第 **2080 个交易日**（约 8×260）；真实 `coverage_start`/`coverage_end` 必须落库，缺口按实际报告，不机械换算自然日。

### 1.2 待决策项（已全部确认，2026-09-01）

原待决策 5 项已于 2026-09-01 由用户全部确认，结论见 1.1 节第 7~11 项：统一 qfq（含除权对策 a）、T+1 开盘成交、A 股完整约束组合、近 20 日日均成交额降序 + 代码升序同分键、终点=最新已完成交易日向前 2080 个交易日并落库真实覆盖。此后如要改变任一口径，必须另立决策。

## 2. 源码现状与直接结论

### 2.1 当前可复用模块

| 能力 | 当前源码事实 | P5A 用法 |
|---|---|---|
| 参数化筛选 | `ParameterizedScreeningService` 接收 `ScreeningPlan`、交易日、复权方式和代码集合 | 保留当前筛选入口；历史筛选复用同一模板编译结果和规则引擎 |
| 数据需求规划 | `ScreeningDataPlanner` 根据规则计算日历日或交易日回看范围 | 用于计算回测前置 warm-up 数据范围 |
| 规则上下文 | `RuleContext` 强制交易日和数据元数据一致 | 历史执行时为每个 T 构造严格的当日上下文 |
| 并行筛选 | `ScreeningShardExecutor` 使用单层 `ProcessPoolExecutor`，按股票分片 | 演化为 `HistoricalScreeningExecutor`，继续只按股票分片 |
| 可序列化计划 | `PicklableScreeningPlan` 可跨进程传递 | 直接作为历史工作进程输入之一 |
| 只读数据库 | `SQLiteMarketDataReader` 使用只读连接并按代码、日期读取 | 每个工作进程创建独立只读连接，不跨进程共享连接 |
| 模板版本 | 筛选模板包含模板 ID、revision、adjustment 和规则配置 | 作为历史信号缓存与回测可复现性的关键键值 |
| 本地 Web | `WebApp`、`ThreadingHTTPServer` 和静态 HTML/CSS/JS | 在现有筛选工作台增加回测步骤，不另建 Backtrader 页面 |

### 2.2 P4 基准事实

当前基准数据约包含 5,212 只股票、1,243,232 根日线，覆盖约一年：

- 串行读取全市场约 4.93–5.73 秒。
- 串行完成一次参数化筛选约 6.44–7.42 秒。
- 4 进程股票分片筛选约 1.95 秒，结果与串行一致。
- 4 路并行纯读取约 15.21 秒，反而慢于串行读取。
- 已观测峰值内存约 2.42–3.25 GB。

因此不能把“一天的 1.95 秒筛选”简单循环约 1,250 个交易日。仅按该结果外推也需要约 40 分钟，而且会重复查询、重复构造对象。P5A 必须把每个股票分片所需的整段历史一次读入，在工作进程内部沿 T 轴滚动计算。

### 2.3 现有数据层的四个阻塞项

1. **历史基本面读取存在未来信息风险**：当前 Reader 对请求终点只保留每只股票最新一条基本面；它不能直接支持五年期间逐日的 point-in-time 基本面。
2. **历史股票池不完整**：`stocks` 以 `(code, as_of)` 保存快照，但当前 backfill 只拉取终点股票池。退市股票、历史 ST 状态和历史上市状态可能缺失，直接回测会产生幸存者偏差。
3. **八年扩容不能只改配置**：当前 `retention_days` 约为 360；启动补齐主要处理尾部增量，不会自动向前补齐缺失前缀。
4. **旧 checkpoint 会误判已完成**：`backfill_chunks` 的身份没有完整覆盖起止范围。仅把保留期改为八年，旧完成分片可能导致历史前缀被跳过。

上述问题必须先修复或明确限制规则范围。严禁用“今天的股票状态/基本面”回填历史日期后给出无警告的正式结果。

## 3. P5A 目标与非目标

### 3.1 目标

P5A 完成后，用户能够：

1. 在现有界面创建或选择一个已版本化筛选模板。
2. 运行“当前筛选”，查看当前符合条件的股票。
3. 点击“用此模板回测”，选择回测区间、策略模块、资金与执行参数。
4. StockManager 对每个历史交易日以当时可知的数据重新生成股票资格信号。
5. Backtrader 顺序推进组合状态，按模块化的入场、退出、调仓、仓位和执行策略下单。
6. 返回净值、回撤、收益、成交、持仓、暴露、错误和完整数据来源信息。
7. 同一个历史信号结果可以被不同交易策略重复利用，无需重新筛选五年历史。

### 3.2 非目标

- 不做实盘交易、模拟券商接入或自动执行。
- 不让 Backtrader 访问 Baostock 或任何外部网络。
- 不把 Backtrader 对象、pickle 或策略类路径暴露给 API 和数据库。
- 不在 P5A 引入 CAPM、因子归因、组合优化器或分布式计算集群。
- 不承诺任意第三方 Backtrader 策略脚本可以直接上传执行。
- 不为了追求速度改变既有筛选规则语义。

## 4. 用户工作流与前端行为

现有筛选模板页扩展为一个连续研究工作台：

```text
规则编辑/模板版本
        ↓
当前筛选预览
        ↓ “用此模板回测”
回测配置（区间、策略、仓位、费用、复权）
        ↓
后台历史信号生成
        ↓
Backtrader 顺序组合回测
        ↓
结果摘要 / 净值 / 回撤 / 成交 / 持仓 / 运行来源
```

### 4.1 页面改造

1. **模板区**：显示模板 ID、revision、规则版本、复权方式和最近保存时间。
2. **当前筛选区**：沿用现有结果，同时显示“使用的有效交易日”和数据新鲜度。
3. **回测配置抽屉或步骤区**：不出现独立 Backtrader 品牌页，只展示研究业务参数。
4. **任务进度区**：区分数据校验、历史信号、回测引擎和结果整理四阶段。
5. **结果区**：摘要卡片、净值与回撤曲线、年度/月度结果、订单、持仓和运行来源。
6. **警告区**：明确显示复权、幸存者偏差、缺失历史基本面、停牌/涨跌停模拟能力等限制。

### 4.2 前端禁止行为

- 不发送 Python 类名、模块导入路径或任意代码。
- 不允许用户通过页面绕过模板 revision，直接修改已运行任务的规则定义。
- 不把长任务绑定在一个同步 HTTP 请求上。
- 页面刷新后不得丢失任务状态或结果入口。

## 5. 目标架构

### 5.1 依赖方向

```text
API / Web UI
    ↓
ResearchBacktestService
    ├── TemplateCompiler / ScreeningPlan
    ├── HistoricalScreeningService
    │       ├── ScreeningDataPlanner
    │       ├── HistoricalScreeningExecutor
    │       ├── RuleEngine
    │       └── MarketDataReader（本地只读）
    ├── EligibilityRepository / SignalCache
    ├── BacktraderBacktestEngine
    │       ├── StockManagerPortfolioStrategy
    │       ├── Policy Registries
    │       ├── AShareExecutionModel
    │       └── ResultNormalizer
    └── BacktestRunRepository

DataSyncService → Provider（唯一外部数据入口）
DataSyncService → Storage（八年历史与 point-in-time 状态）
```

### 5.2 分层要求

- **Domain**：研究策略规格、历史资格信号、回测运行、订单事件和指标等纯数据对象。
- **Rules**：继续保持纯函数，不知道 Backtrader、数据库或 Web。
- **Providers**：只由 `DataSyncService` 调用；Backtrader 严禁持有 Provider。
- **Services**：编排历史筛选、缓存和回测运行。
- **Storage**：提供 point-in-time 查询、数据版本、信号与结果持久化接口。
- **API**：只解析请求、验证字段、提交任务和格式化响应。
- **Adapters**：Backtrader 位于适配器边界，不进入 Domain 或 Rules。

## 6. 核心领域契约

### 6.1 ResearchStrategySpec

建议新增不可变、可序列化的研究策略规格：

```python
@dataclass(frozen=True)
class ResearchStrategySpec:
    strategy_spec_id: str
    screening_template_id: str
    screening_template_revision: int
    screening_plan_fingerprint: str
    adjustment: AdjustmentMode
    evaluation_schedule: EvaluationSchedule
    entry_policy: PolicySpec
    exit_policy: PolicySpec
    rebalance_policy: PolicySpec
    allocation_policy: PolicySpec
    execution_policy: PolicySpec
    initial_cash: Decimal
    backtest_start: date
    backtest_end: date
```

要求：

- `PolicySpec` 只能包含注册表中的稳定 ID、版本和 JSON 可序列化参数。
- 禁止保存任意 Python import path。
- 模板 revision、规则实现版本、数据集版本和策略政策版本必须一起进入 fingerprint。
- 金额和费率使用明确精度，API 层不得依赖二进制浮点隐式舍入。

### 6.2 EligibilityTimeline

StockManager 历史筛选层只回答“某交易日哪些股票通过模板”，不直接下单：

```python
@dataclass(frozen=True)
class EligibilitySnapshot:
    trading_day: date
    eligible_codes: tuple[str, ...]
    selected_count: int
    provenance: SignalProvenance
```

`SignalProvenance` 至少包含：

- dataset ID 与不可变 generation/version；
- 交易日历版本或 fingerprint；
- adjustment；
- 模板 ID、revision、plan fingerprint；
- 规则实现版本；
- 股票池口径；
- 生成时间、执行器版本和失败/缺失统计。

### 6.3 策略政策接口

Backtrader 适配层组合以下稳定政策，而不是为每种组合复制一整个 Strategy 类：

- `EntryPolicy`：资格股票在何种技术条件下允许进入。
- `ExitPolicy`：资格失效、均线、持有期或止损等退出条件。
- `RebalancePolicy`：每天、每周或每月何时重新计算目标组合。
- `AllocationPolicy`：等权、固定仓位、最大持仓数和现金保留。
- `ExecutionPolicy`：订单类型、撮合时点、费用、滑点和 A 股约束。
- `CandidateRankingPolicy`：候选超过容量时的明确排序与同分处理。

P5A 首个纵向切片建议提供：

1. `selection_rebalance_v1`：按资格名单进入，资格失效退出。
2. `selection_sma_timing_v1`：资格名单 + Backtrader SMA/CrossOver 指标择时。
3. `selection_fixed_holding_v1`：首次进入后持有 N 个交易日，仍受不可成交约束。

Backtrader 提供 Cerebro、Strategy、Broker、订单 API、指标和 Analyzers；它不提供符合本项目语义的完整 A 股筛选策略。因此上述业务策略必须由 StockManager 适配层定义和测试。

## 7. 时间语义与防未来函数

### 7.1 每日执行顺序

P5A 默认采用以下严格顺序：

1. T 日成为有效交易日。
2. 只使用截至 T 日可知且满足 `published_on <= T` 的数据。
3. T 日收盘数据完整后生成 `EligibilitySnapshot(T)`。
4. 策略在 T 日收盘后形成目标订单。
5. 市价单最早在 T+1 日开盘尝试成交。
6. 成交受停牌、涨跌停、整手、现金和 T+1 卖出规则约束。

严禁默认开启 Backtrader 的 cheat-on-close 或 cheat-on-open。任何改变都必须成为显式、版本化、在结果页可见的执行政策。

### 7.2 数据区间

- `backtest_start` 到 `backtest_end` 是正式计分区间。
- 实际读取开始日由 `ScreeningDataPlanner` 与策略指标共同计算：
  `warmup_start = min(rule_required_start, indicator_required_start)`。
- 250 个交易日可以作为默认安全上限提示，但实现应计算真实需求，不能无条件硬编码。
- warm-up 数据只用于形成指标和规则上下文，不计入收益。

### 7.3 point-in-time 规则门禁

规则按历史可用性分类：

- `PRICE_VOLUME_PIT_READY`：仅依赖历史 OHLCV 和当时交易日历，可进入第一批正式回测。
- `FUNDAMENTAL_PIT_READY`：只有数据库已保存带真实发布日期的历史序列后才允许。
- `UNIVERSE_STATE_PIT_READY`：依赖 ST、上市、退市、停牌状态，必须有当日状态快照。
- `PIT_UNSUPPORTED`：正式回测直接拒绝，不能静默改用当前值。

可以提供带醒目标记的实验模式，但实验结果必须携带 `lookahead_risk=true` 或 `survivorship_bias_risk=true`，且不能与正式结果混在一起。

## 8. 八年数据库扩展方案

### 8.1 配置升级

不要简单把 `retention_days: 360` 改成 2920。建议升级为兼容旧配置的 v2：

```json
{
  "version": 2,
  "history": {
    "target_years": 8,
    "coverage_policy": "latest_completed_trading_day"
  }
}
```

旧 v1 配置仍能读取，但启动时产生清晰迁移提示。八年是目标覆盖长度，真实 `coverage_start` 和 `coverage_end` 必须落库。

### 8.2 覆盖范围与数据版本

建议新增或等价实现：

- `dataset_versions`：不可变数据 generation、来源、adjustment、创建时间和状态。
- `dataset_coverage`：各数据类型的最早/最晚交易日、完整性和缺口。
- `backfill_runs_v2`：目标起止日、数据类型、adjustment、状态和错误。
- `backfill_chunks_v2`：run ID、代码范围、起止日、校验摘要和状态。

P5A 不强制表名，但必须满足：

1. 延长历史时能识别“缺失前缀”和“缺失尾部”。
2. checkpoint 身份包含起止范围，旧一年分片不能冒充八年分片。
3. 同一范围重复同步保持幂等，不重复访问 Provider。
4. 回测运行绑定一个稳定 generation；运行期间数据变化必须失败或使用不可变快照。
5. 数据集记录 source、generated_at、timezone 和 adjustment。

### 8.3 补齐流程

```text
计算最新已完成交易日
        ↓
读取各数据类型 coverage
        ↓
缺失前缀？──是→ 向前 backfill（串行受控 Provider）
        ↓
缺失尾部？──是→ 现有增量同步
        ↓
完整性校验与 generation 提交
        ↓
只有完整 generation 可供正式回测
```

Provider 请求继续串行并遵守速率限制。P5A 的“股票并行”只用于本地离线计算，不能扩散到 Baostock 拉取。

### 8.4 历史股票池和基本面

- 每个评估日必须能得到当日可交易/已上市/未退市/ST/停牌状态，或得到明确的“不支持”结论。
- 历史基本面必须保存真实 `report_date` 与 `published_on`，查询条件为 `published_on <= T`。
- 若 Baostock 无法完整提供所需历史状态，Provider 接口保持不变并记录数据能力缺口；不能在业务层补造事实或擅自切换数据源。
- P5A 可先让纯价格成交量模板形成完整纵向切片，但数据库契约必须为以后 point-in-time 基本面保留正确边界。

### 8.5 索引与迁移原则

- 迁移前先用八年样本跑 `EXPLAIN QUERY PLAN` 和基准，不能凭感觉新增大量索引。
- 候选索引 `(code, adjustment, trading_day)` 是否优于现有主键，必须由实际查询计划证明。
- 数据迁移必须可中断、可恢复、可校验，不得破坏现有一年数据。
- 迁移前后记录行数、每个代码的日期边界、重复键、缺口和数据库大小。

## 9. 历史筛选的并行设计

### 9.1 核心结论

并行维度固定为股票轴 Y；时间轴 T 在每个工作进程内严格顺序推进：

```text
Coordinator
  ├── shard A: codes 000001... → 一次读取完整区间 → T1,T2,...,Tn 顺序筛选
  ├── shard B: codes ...       → 一次读取完整区间 → T1,T2,...,Tn 顺序筛选
  ├── shard C: codes ...       → 一次读取完整区间 → T1,T2,...,Tn 顺序筛选
  └── shard D: codes ...       → 一次读取完整区间 → T1,T2,...,Tn 顺序筛选
                                    ↓
                        按交易日合并 EligibilityTimeline
                                    ↓
                      Backtrader 单时间线顺序运行组合
```

### 9.2 为什么不能并行 T 轴

- Backtrader 的现金、持仓、待成交订单、手续费、T+1 可卖数量和指标状态都依赖前一时点。
- T2 的组合状态无法在不知道 T1 成交结果时独立计算。
- 即使历史筛选规则本身某些日期可独立计算，拆分 T 轴也会重复 warm-up、重复读取和增加合并复杂度。

因此 Backtrader 单次组合运行不得拆成多个日期段并行。可以并行的是不同的独立参数实验，但那属于后续优化，而且必须受到全局资源配额限制。

### 9.3 HistoricalScreeningExecutor

建议新增接口：

```python
class HistoricalScreeningExecutor(Protocol):
    def execute(
        self,
        plan: PicklableScreeningPlan,
        request: HistoricalScreeningRequest,
        *,
        max_workers: int,
        progress_callback: HistoricalProgressCallback | None = None,
    ) -> HistoricalScreeningResult: ...
```

`HistoricalScreeningRequest` 至少包含代码范围、warm-up 起点、计分起止日、评估交易日、dataset generation、adjustment、股票池政策和审计等级。

执行要求：

1. 只有一层进程池，禁止嵌套现有读取线程池或再次创建进程池。
2. 每个 worker 创建自己的只读 SQLite Reader。
3. 每个代码分片一次读取完整所需区间，再按代码组织有序序列。
4. worker 内沿评估日顺序移动窗口，复用已加载数据。
5. 第一版以现有 `RuleEngine` 逐日结果为正确性参考。
6. 后续可增加 `HistoricalRuleEvaluatorProtocol.evaluate_series()` 做滚动/向量优化，但结果必须逐日等价。
7. worker 不返回全部原始 K 线，只返回压缩后的日期-代码资格结果和必要审计信息。
8. coordinator 以确定性顺序合并；任一 shard 失败则整个信号 run 失败，禁止交付部分成功结果。
9. 运行前后校验 dataset generation；检测到数据被同步进程改变则明确失败。

### 9.4 分片与内存

P4 的 `batch_size=500` 不能直接沿用为八年默认值。八年时单 shard 可能接近百万根日线，多进程会放大 DataFrame 和规则上下文内存。

实现代理必须用代表性八年数据测试：

- worker 数：1、2、4；
- shard size：50、100、250、500；
- 纯读取、规则计算、合并和序列化的独立耗时；
- 峰值 RSS、数据库 page cache 影响和重复运行方差。

默认值由基准决定。鉴于 P4 中 SQLite 并行纯读取更慢，`max_workers=4` 只能作为候选值，不能写成永远最快的结论。

### 9.5 输出等级

- `compact`：默认，只存每日通过代码和数量，供回测消费。
- `audit`：额外存规则级 actual value、threshold 和 reason，用于抽样解释或问题复现。

五年 × 5,000 股票可能产生数百万条规则结果，因此正式回测默认不得无条件生成全量 audit 对象。

## 10. Backtrader 适配方案

### 10.1 边界

新增 `BacktraderBacktestEngine` 作为适配器，实现项目自己的 `BacktestEngine` Protocol。上层只认识 Domain 对象，不认识 `bt.Cerebro`、`bt.Strategy`、feed 或 analyzer。

```python
class BacktestEngine(Protocol):
    def run(
        self,
        spec: ResearchStrategySpec,
        eligibility: EligibilityTimeline,
        market_data: BacktestMarketData,
    ) -> BacktestResult: ...
```

### 10.2 单一通用 Strategy

建议使用一个受控的 `StockManagerPortfolioStrategy`：

- 从 `EligibilityTimeline` 读取 T 日筛选资格；
- 调用注册表中的 entry/exit/rebalance/allocation/ranking policies；
- 使用 Backtrader 指标实现均线等择时；
- 形成目标仓位并通过受控 broker/execution adapter 下单；
- 将订单和成交事件转换为项目 Domain 事件。

不要把每个筛选模板动态生成成 Python Strategy 子类，也不要运行用户上传代码。

### 10.3 数据 feed 范围

Backtrader 单组合运行需要同时看到所有可能持有标的，但不应默认加载八年全市场 5,000 个 feed。采用两阶段流程：

1. 先生成历史 `EligibilityTimeline`。
2. 计算评估期内“曾入选代码并包含必要持有缓冲”的并集。
3. 只为该并集加载 Backtrader feed。

如果并集仍接近全市场，PoC 必须测量启动时间和内存；未通过门槛时，不能通过隐藏截断改变策略，应回到架构决策处理数据 feed 方案。

### 10.4 A 股执行模型

Backtrader 默认 broker 不会自动完整模拟 A 股制度。P5A 需把以下能力放在显式 `AShareExecutionModel`/broker policy 中：

- 100 股整手和余股处理；
- T+1 可卖数量；
- 停牌无成交；
- 涨停不可买、跌停不可卖及价格边界；
- 佣金最低收费、印花税、过户费及生效日期；
- 滑点与订单未成交/过期；
- 现金不足和部分成交政策。

若某项在 P5A 首发暂不支持，运行校验和结果页必须同时显示，不得沿用 Backtrader 默认行为后称其为 A 股真实撮合。

### 10.5 结果归一化

Backtrader analyzer 输出只在适配层解析，保存为稳定项目结构：

- 初始/结束资金、总收益、年化收益；
- 日净值、现金、持仓市值和暴露；
- 最大回撤与回撤区间；
- 波动率、Sharpe 所用无风险利率与频率；
- 成交次数、胜负、换手率和费用；
- 订单、成交、拒单、未成交原因；
- 每日持仓快照或可重建持仓事件；
- 数据、模板、政策和引擎版本来源。

任何指标缺少足够数据时返回明确的 unavailable 原因，禁止用 0 冒充。

## 11. 信号缓存、任务与结果存储

### 11.1 信号缓存键

历史资格信号缓存至少由以下字段决定：

- dataset ID + immutable generation；
- adjustment；
- 股票池政策；
- 评估起止日和 schedule；
- 模板 ID + revision + plan fingerprint；
- 规则实现版本；
- 交易日历 fingerprint。

手续费、初始资金、均线择时或持仓期限变化不应使纯筛选资格缓存失效；筛选模板、规则语义、数据 generation 或复权变化必须失效。

### 11.2 建议存储实体

表名可由实现按现有命名规范调整，但职责必须分开：

- `research_strategy_specs`
- `historical_screening_runs`
- `eligibility_days`
- `eligibility_members`
- `backtest_runs`
- `backtest_daily_results`
- `backtest_orders`
- `backtest_trades`
- `backtest_positions` 或等价可重建事件表
- `backtest_events`

禁止持久化 Backtrader pickle、Cerebro 或 Strategy 实例。

### 11.3 异步任务状态

```text
QUEUED
  → VALIDATING
  → BUILDING_SIGNALS
  → RUNNING_BACKTEST
  → NORMALIZING
  → SUCCEEDED
```

异常分支为 `FAILED`，用户取消为 `CANCEL_REQUESTED → CANCELLED`，进程重启时未完成任务标为 `INTERRUPTED`，不得错误恢复为成功。

建议全局同时只运行一个重型 P5A job，内部 worker 数由资源配额控制。禁止“多个回测任务进程池 × 每个任务股票进程池”的嵌套并行。

## 12. API 方案

在现有本地 API 中新增研究命名空间：

- `POST /api/research/backtests`：校验并提交任务，返回 `run_id`。
- `GET /api/research/backtests/{run_id}`：状态、阶段、进度、警告和摘要。
- `POST /api/research/backtests/{run_id}/cancel`：请求安全取消。
- `GET /api/research/backtests/{run_id}/equity`：净值与回撤序列。
- `GET /api/research/backtests/{run_id}/orders`：分页订单与成交。
- `GET /api/research/backtests/{run_id}/positions`：分页或按日查询持仓。
- `GET /api/research/backtests/{run_id}/provenance`：数据和配置来源。

提交请求必须引用已保存的模板 ID + revision。服务端重新读取模板并校验 fingerprint，禁止信任浏览器传入的隐藏规则副本。

错误结构至少包含稳定错误码、用户可读说明、字段位置和可否重试。典型错误包括：

- `INSUFFICIENT_DATA_COVERAGE`
- `UNRESOLVED_ADJUSTMENT_POLICY`
- `PIT_DATA_UNAVAILABLE`
- `UNSUPPORTED_RULE_FOR_HISTORICAL_RUN`
- `DATASET_CHANGED_DURING_RUN`
- `CANDIDATE_RANKING_REQUIRED`
- `BACKTEST_RESOURCE_LIMIT`
- `BACKTRADER_ENGINE_FAILURE`

## 13. 性能目标与基准门禁

P5A 不先拍脑袋承诺秒级五年回测。先建立可重复基准，再锁定门槛。

### 13.1 必测基准

1. 约 5,000 股票 × 5 年计分区间 + warm-up 的纯读取。
2. 同一区间逐日历史筛选，worker=1/2/4。
3. compact 与 audit 输出差异。
4. eligibility 并集为 100、500、1,000、5,000 只时的 Backtrader 启动、运行和峰值内存。
5. 缓存命中后的重复回测。
6. 同一输入重复三次，确认结果 hash 一致。

### 13.2 首轮建议验收线

以下是实施阶段需要通过基准确认的建议线，不是现状承诺：

- 结果与单进程参考实现逐日、逐代码完全一致。
- 4 worker 必须相对 1 worker 有真实收益，否则默认回退到测得更优的 worker 数。
- 峰值 RSS 不超过测试机器可用内存的 70%，且不得触发 swap 作为常态。
- 历史数据每个 shard 只读取一次，不得按交易日重复发起 1,250 次全市场查询。
- 缓存命中时不得重新执行规则引擎。
- 进度更新开销不超过总运行时间的 5%。
- Backtrader 结果在相同环境、数据 generation 和参数下确定性一致。

性能报告必须保存机器、Python、SQLite、Backtrader 版本、代码 commit、数据库 hash/大小和完整参数。

## 14. 分阶段任务包

所有任务默认由 DeepSeek V4 Flash 实现、自测、自审和技术验收。每个任务独立提交，前置任务未通过不得越级宣称完成。

### P5A-0：ADR、依赖 PoC 与基准框架 —— 已完成（2026-09-02 验收）

**目标**：在改业务代码前验证 Backtrader 与当前 Python/依赖环境兼容，并冻结架构决策。

**验收结果**：backtrader 1.9.78.123 在 Python 3.14.7 安装与运行验证通过（离线 PoC `scripts/p5a_poc_backtrader.py`，同输入重复运行结果哈希一致，3 feed + SMA 策略 + 4 analyzer，零警告）；新增 `ADR_P5A_BACKTEST_ENGINE.md`（选型、GPLv3、适配器边界、不做时间轴并行）；`ADR_P5A_SEED_DISTRIBUTION.md` 决策定稿（统一 qfq + 对策 a）；§1.2 五项待决策全部确认；基准 JSON 输出格式（schema_version 1）确立；既有筛选语义零改动，`pytest` 266 项基线保持全绿。

**工作**：

- 新增 ADR：选择 Backtrader、GPLv3 影响、适配器边界和不做时间轴并行的原因。
- 在隔离分支验证安装、最小 Cerebro、多个 data feed、analyzer 和重复运行。
- 验证当前 Python 3.14 环境；若不兼容，提出受控运行时方案，不得在主依赖中强行锁入不可运行版本。
- 建立五年/八年基准脚本、固定 fixtures 和 JSON 输出格式。
- 决定复权、成交时点和 A 股约束等级，或把未决项做成阻断门禁。

**验收**：PoC 离线运行；同输入重复结果一致；ADR 和许可说明通过审查；未修改既有筛选语义。

### P5A-1：八年覆盖与向前补齐 —— 已完成（2026-09-02 验收）

**目标**：让本地数据库可证明地覆盖目标八年，而不是只修改保留天数。

**验收结果**：配置 v2（SyncConfig.history）与 v1 兼容读取；新增 dataset_versions/dataset_coverage/backfill_runs_v2/backfill_chunks_v2（checkpoint 身份绑定目标范围与拉取区间，旧一年分片不能冒充八年分片）；前缀/尾部规划器（plan_coverage/trading_day_lookback）；v2 回补幂等（同范围 SUCCESS 零 Provider 调用）、断点恢复只拉缺失缺口、失败记录 FAILED 并抛 SyncFailedError；generation 仅在所有数据类型 COMPLETE 时提交；完整性校验（scripts/verify_db_integrity.py，只读）；Web 启动按 history 有无路由 v2/v1。pytest 298 通过（新增 32 项），v1 行为完全不变。实现文档：development/implementation/P5A_1_HISTORY_V2.md。

**工作**：

- 配置 v2 与 v1 兼容读取。
- coverage、generation、backfill run/chunk v2 迁移。
- 缺失前缀与缺失尾部分别规划。
- 幂等、持久化锁、进程锁、冷却、失败重试和启动检查。
- 数据完整性和迁移前后校验报告。

**验收**：断点恢复不重复拉取；已有一年数据不损坏；八年范围每种数据类型均能报告 COMPLETE/PARTIAL/UNAVAILABLE；Provider 请求保持串行。

### P5A-2：point-in-time 数据读取契约 —— 已完成（2026-09-02 验收）

**目标**：历史 T 日查询只能看到 T 日当时可知信息。

**验收结果**：新增 PointInTimeRequest/PointInTimeReaderProtocol/SQLitePointInTimeReader（只读；universe_as_of 最近快照+上市/退市边界、fundamentals 按 published_on <= T、bars/dividends 按日期边界、committed_generation、data_fingerprint）；RulePitCapability 枚举与 RuleDefinition.pit_capability 字段（默认 PIT_UNSUPPORTED），12 条内置规则显式声明能力；HistoricalCapabilityValidator 拒绝 PIT_UNSUPPORTED 规则并校验基本面/股票池数据可用性（UnsupportedRuleForHistoricalRunError/HistoricalCapabilityError）。反未来函数 fixture 证明 T+1 上市/发布/分红在 T 不可见、退市股票退市前可见；pytest 313 通过（新增 15 项）。实现文档：development/implementation/P5A_2_PIT_READER.md。

**工作**：

- 新增 historical/PIT reader protocol，不破坏当前单日 Reader。
- 历史股票池、状态、停牌与基本面发布日期查询。
- 规则历史能力标签和启动前校验器。
- 不可变 dataset generation 或等价一致性机制。

**验收**：用反未来函数 fixture 证明 T+1 发布的数据在 T 不可见；退市股票能在适用日期进入股票池；不支持的数据明确拒绝。

### P5A-3：研究策略领域模型与政策注册表 —— 已完成（2026-09-02 验收）

**目标**：建立与 Backtrader 解耦、可版本化的策略契约。

**验收结果**：新增 research 包（ResearchStrategySpec/PolicySpec/PolicyKind/EvaluationSchedule/PolicyParameterSpec、PolicyRegistry 白名单参数校验、canonical_json/plan_fingerprint/policy_fingerprint/spec_fingerprint）；内置 8 个政策与三个受控策略规格（selection_rebalance_v1/selection_sma_timing_v1/selection_fixed_holding_v1）；Domain 不导入 Backtrader（子进程断言），未知 policy ID/版本、import path 参数、越界/缺失参数运行前拒绝，相同配置 fingerprint 稳定。pytest 323 通过（新增 10 项）。实现文档：development/implementation/P5A_3_STRATEGY_SPEC.md。

**工作**：

- `ResearchStrategySpec`、policy specs、fingerprint 和验证器。
- entry/exit/rebalance/allocation/ranking/execution 注册表。
- JSON/API schema 与参数边界。
- 首批三个受控策略组合的规格。

**验收**：Domain 不导入 Backtrader；任意 import path 和未知 policy ID 被拒绝；相同配置 fingerprint 稳定。

### P5A-4：HistoricalScreeningExecutor —— 已完成（2026-09-02 验收）

**目标**：复用现有规则能力，完成股票轴并行、时间轴顺序的五年逐日信号生成。

**验收结果**：HistoricalScreeningRequest/EligibilitySnapshot/HistoricalScreeningResult 契约；worker 一次读整段（bars/fundamentals/all_universe_snapshots）后沿评估日顺序滚动；单层 ProcessPoolExecutor（无嵌套池），max_workers=1 走同一逻辑串行；确定性合并 + result_fingerprint；任一 shard 失败整体失败（HistoricalScreeningError）；generation 绑定校验（DatasetGenerationMismatchError）；每 shard 恰好一次 bars_through（计数测试断言）。并行与参考结果逐项相等、重复运行确定性一致；基准脚本 scripts/bench_p5a_historical.py 就绪（fixture 300 股 × 120 日各配置 fingerprint 一致）。pytest 331 通过（新增 8 项）。实现文档：development/implementation/P5A_4_HISTORICAL_SCREENING.md。

**工作**：

- 历史请求、结果、进度与错误契约。
- 单进程参考实现。
- 单层代码分片进程池；每 shard 一次读整段。
- compact/audit 模式和确定性合并。
- worker、shard、内存基准。

**验收**：与逐日调用现有 RuleEngine 的参考结果完全相等；无嵌套池；worker 失败整体失败；数据变化可检测；五年运行不按日期重复全市场读取。

### P5A-5：历史信号缓存和运行存储 —— 已完成（2026-09-02 验收）

**目标**：让筛选资格可复用，并为异步任务提供可恢复状态。

**验收结果**：完整缓存键（dataset/generation/adjustment/universe/窗口/schedule/模板 id+revision/plan fingerprint/规则实现版本/交易日历 fingerprint；策略费用与资金参数不进入键）；运行状态机 QUEUED→VALIDATING→BUILDING_SIGNALS→SUCCEEDED 及 FAILED/CANCEL_REQUESTED→CANCELLED/INTERRUPTED(重启恢复+可重新入队)；eligibility_days/members 落库、分页、按 cache_key 命中查询、keep=N 清理级联删除。pytest 340 通过（新增 9 项）。实现文档：development/implementation/P5A_5_RUN_STORAGE.md。

**工作**：

- historical screening run、eligibility day/member 存储。
- 完整 cache key 和失效规则。
- 状态机、进度、取消与进程重启后的 INTERRUPTED 处理。
- 结果分页和清理策略。

**验收**：同一键缓存命中；数据/模板/规则版本变化必失效；策略费用变化不重算纯资格；部分结果不标成功。

### P5A-6：Backtrader 适配器与通用组合策略 —— 已完成（2026-09-02 验收）

**目标**：把资格时间线送入 Backtrader，形成第一个端到端研究回测。

**验收结果**：BacktestEngine Protocol 与 BacktraderBacktestEngine 适配器（唯一导入 bt 的模块）；StockManagerPortfolioStrategy 桥接六类政策（entry/exit/rebalance/allocation/ranking/execution）；交易日历 dummy feed + 资格并集股票 feed；T 日信号最早 T+1 开盘成交（测试断言买入成交日严格晚于信号日）；三策略规格（rebalance/sma_timing/fixed_holding）全部运行；analyzer 归一化为 BacktestMetrics（unavailable 显式不冒充 0）；重复运行确定性一致；BacktestInputError/BacktestEngineError 明确转换；contracts/research 无 backtrader 泄露（子进程断言）。pytest 349 通过（新增 9 项）。实现文档：development/implementation/P5A_6_BACKTEST_ADAPTER.md。

**工作**：

- `BacktestEngine` Protocol 和 Backtrader adapter。
- `StockManagerPortfolioStrategy` 与政策桥接。
- eligibility 代码并集 feed 加载。
- SMA/CrossOver、固定持有和资格调仓策略。
- analyzer 结果归一化。

**验收**：Services/Domain 不泄露 Backtrader 类型；T 信号不会在 T 之前成交；同输入确定性一致；没有网络访问；Backtrader 异常转换为明确业务错误。

### P5A-7：A 股执行与成交语义

**目标**：把已确认的 A 股限制做成可测试执行政策。

**工作**：

- 整手、T+1、停牌、涨跌停、费用、滑点、现金和拒单规则。
- 成交日历与证券状态接入。
- 每个执行限制的结果警告与来源。

**验收**：固定 fixtures 覆盖停牌、ST、涨停、跌停、零成交量、资金不足、最低佣金、余股和跨日可卖数量；未实现能力不能静默降级。

### P5A-8：异步 API 与统一前端工作台

**目标**：在现有 UI 中完成“模板 → 当前筛选 → 回测 → 结果”闭环。

**工作**：

- 研究 backtest API、分页结果和取消。
- 本地有界 job runner，默认单重任务。
- 模板页回测配置、进度、错误、结果和 provenance。
- 刷新后恢复运行状态。

**验收**：HTTP 请求不被五年任务长期阻塞；重复点击不会重复创建同一运行；页面不接收任意代码；错误和偏差风险用户可见。

### P5A-9：端到端验收、性能调优与文档同步

**目标**：用固定小数据和代表性大数据完成技术验收。

**工作**：

- 全套离线单元、集成、E2E、回归和性能测试。
- 参考实现对拍、订单账本核对和故障注入。
- DeepSeek V4 Flash 代码自审与最终技术验收。
- 验收通过后更新 README、API、数据库、ADR、运维和用户手册，并同步 `project_doc`。

**验收**：第 15、16、17 节全部满足；文档与代码事实一致；Git 无数据库、日志、Excel、缓存、IDE 文件和构建产物。

## 15. 测试矩阵

### 15.1 规则与时间

- 空交易日、节假日、周末和最近有效交易日回溯。
- warm-up 不足、首个可计算日和末日。
- T+1 发布基本面在 T 不可见。
- 数据乱序、重复日期、缺失 bar、NaN、None 和零成交量。
- 模板 revision 或规则版本变化导致缓存失效。
- adjustment 不一致直接失败。

### 15.2 股票池与 A 股状态

- 上市前不在股票池，退市后不在股票池，历史期间退市股票仍可出现在早期日期。
- ST 状态变更、停牌、复牌、涨停、跌停。
- 100 股整手、余股、T+1 可卖、现金不足和部分成交。
- 候选数量超过最大持仓且无 ranking 时明确拒绝。

### 15.3 并发与一致性

- worker=1/2/4 与参考结果一致。
- shard 边界、空 shard、单股票和超过 5,000 股票。
- worker 异常、取消、进程重启、数据库锁和 dataset generation 变化。
- 禁止嵌套池；实际并发数不超过配置。
- 结果顺序和 hash 在重复运行中稳定。

### 15.4 Backtrader 与账本

- T 信号最早 T+1 成交。
- 无 cheat 模式。
- 多股票同日调仓的现金和订单顺序可复现。
- 资格退出、SMA 信号、固定持有期的全部分支。
- 手工小样本逐日核对现金、持仓、费用、净值和回撤。
- analyzer 数据不足时返回 unavailable 而不是 0。

### 15.5 API 与前端

- 请求字段边界、未知 policy、未知模板 revision 和超大区间。
- 状态机合法迁移、进度、取消、失败和刷新恢复。
- 分页稳定、空结果和大结果。
- XSS/注入、任意模块路径和任意代码被拒绝。
- 免责声明、数据来源、复权和偏差警告可见。

## 16. 架构决策门禁

以下任一情况发生时，DeepSeek V4 Flash 应停止相关实现并回到 Codex/用户决策：

1. Backtrader 与目标 Python 运行时不兼容，需要改变项目运行时或依赖策略。
2. 复权、分红送转或成交时点仍未确认。
3. Baostock 无法满足历史股票池或 point-in-time 数据契约，需要改变数据源策略。
4. 需要改变现有公开 API、规则语义或分层依赖。
5. 八年迁移出现数据损坏、并发锁或不可恢复 checkpoint 风险。
6. eligibility feed 并集接近全市场，Backtrader 内存/耗时无法通过门槛，需要更换数据装载架构。
7. 股票轴多进程在八年数据上没有真实收益，需要改变执行模型。

## 17. P5A 完成定义

P5A 只有同时满足以下条件才算完成：

1. 用户可从现有筛选模板工作台发起五年回测，无独立 Backtrader UI。
2. 数据库能报告八年目标覆盖、缺口和不可变 generation；补齐可恢复且不重复拉取。
3. 历史筛选在每个有效交易日使用当时可知数据，禁止无标记未来函数和幸存者偏差。
4. 历史筛选只按股票轴并行；每 worker 内时间顺序；Backtrader 单组合沿时间顺序运行。
5. 相同信号可供多种策略复用，缓存失效规则正确。
6. 至少一个端到端策略和首批模块化政策完成离线测试。
7. A 股执行能力与未支持项都显式、可测试、可追溯。
8. 回测结果、订单、持仓和 provenance 可持久化、分页查询并在页面展示。
9. 所有新增/修改函数有完整类型标注，无吞异常、无硬编码本机路径、无业务层网络访问。
10. 单元测试断网可运行，关键边界、并发、迁移、未来函数和账本分支均覆盖。
11. DeepSeek V4 Flash 完成实现、测试、调试、代码自审和最终技术验收。
12. 验收通过后，技术文档带完整 frontmatter 并同步到 `project_doc`；Git 提交和忽略规则符合项目规范。

## 18. 文档交付清单

实现完成后至少更新或新增：

- `project_doc/development/plan/P5A_PLAN.md`
- Backtrader 选型与许可 ADR
- 历史数据与 point-in-time 口径 ADR
- 数据库 v2/迁移说明
- Research Strategy / Policy 接口文档
- 历史筛选并行与缓存说明
- 回测 API 文档
- A 股成交模型说明
- 回测用户手册与风险说明
- P5A 性能基准和最终验收报告

## 19. 推荐实施顺序

严格依次执行：

```text
P5A-0 决策与 PoC
   ↓
P5A-1 八年数据覆盖
   ↓
P5A-2 point-in-time 契约
   ↓
P5A-3 领域规格与政策注册表
   ↓
P5A-4 历史股票轴并行筛选
   ↓
P5A-5 信号缓存与运行存储
   ↓
P5A-6 Backtrader 适配器
   ↓
P5A-7 A 股执行模型
   ↓
P5A-8 API 与统一前端
   ↓
P5A-9 E2E、性能、验收和文档同步
```

最关键的实现原则可以压缩成一句话：**StockManager 负责逐日、point-in-time 地决定“哪些股票有资格”；股票分片可以并行；Backtrader 负责在唯一时间线上决定“何时买卖、买多少以及组合如何演化”。**
