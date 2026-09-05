---
date: 2026-09-05
purpose: 记录 P5 DataSync 重构（P5-RD-0..10）的实现结果、组件接口、测试与验收证据，作为实现手册与验收记录。
project: StockManager
status: active
---

# P5 DataSync 重构实现手册

本手册记录 `development/plan/P5_RECONSTRUCT_DATASYNC.md` 计划落地后的实现事实：新增模块、接口契约、迁移、测试与验收。代码仓库为代码事实来源，本文档只描述已验收的真实代码。

## 1. 交付范围

| 任务包 | 交付 | 测试 |
|---|---|---|
| P5-RD-0 | 真实库基准、数据库 ADR（`ADR_P5_DATASYNC_DATABASE.md`，accepted） | 基准脚本见 ADR 第 2 节 |
| P5-RD-1 | 领域契约、版本化迁移骨架、Repository Protocol | `tests/test_p5_rd1_contracts.py`（46） |
| P5-RD-2 | 确定性 SyncPlanner | `tests/test_p5_rd2_planner.py`（22） |
| P5-RD-3 | SerialFetchWorker 与 Provider 安全边界 | `tests/test_p5_rd3_worker.py`（12） |
| P5-RD-4 | StagingWriter、批次与 checkpoint | `tests/test_p5_rd4_staging.py`（14） |
| P5-RD-5 | CoverageVerifier 与 VerificationReport | `tests/test_p5_rd5_verifier.py`（11） |
| P5-RD-6 | GenerationCommitter 原子发布、ReadinessGate | `tests/test_p5_rd6_committer.py`（18） |
| P5-RD-7 | 种子 SHA-256、外部 manifest、WAL checkpoint、跨平台迁移 | `tests/test_p5_rd7_seed.py`（18） |
| P5-RD-8 | CLI 同步控制子命令 | `tests/test_p5_rd8_cli.py`（9） |
| P5-RD-9 | 旧库 LEGACY_IMPORT 迁移 | `tests/test_p5_rd9_legacy.py`（9） |
| P5-RD-10 | 端到端流水线验收、版本与文档同步 | `tests/test_p5_rd10_e2e.py`（2） |

P5-RD-1..10 当前合计 **161 项**离线测试；2026-09-02 稳定性修复验收时全量 **594 passed**。版本 **1.12.0 → 1.13.0**（MINOR，包含 generation manifest 完整性与同步稳定性增强）。

## 2. 新增模块与导入路径

| 模块 | 职责 |
|---|---|
| `stock_manager.sync.planner` | `PlanInput`、`PlannedOutput`、`SyncPlanner`、`PlanRejectedError`；BOOTSTRAP/INCREMENTAL/REPAIR/LEGACY_IMPORT 四模式确定性规划，`plan_fingerprint` 覆盖全部输入 |
| `stock_manager.sync.worker` | `SerialFetchWorker`、`FetchTaskProvider`、`ProviderFetchError`、`ConcurrentProviderAccessError`；单通道串行执行，显式 adjustment |
| `stock_manager.sync.staging` | `StagingWriter`、`CandidateNotWritableError`、`StagingWriteError`；幂等批次写入 `*_staging` 表，`write_revision` 递增 |
| `stock_manager.sync.verifier` | `CoverageVerifier`、`VerificationOutcome`、`VerificationError`；逐分区读取 staging 实际行，输出 `VerificationReport` + 证据记录 |
| `stock_manager.sync.committer` | `GenerationCommitter`、`ReadinessGate`、`PublishError`、`ReadGateError`；事务内发布（publish-copy + manifest + active 指针）与读取门禁 |
| `stock_manager.sync.seed` | `SeedManifest`、`SeedPackageVerifier`、`TransferPreparer`、`stream_sha256`；种子校验、跨平台迁移 manifest |
| `stock_manager.sync.legacy` | `LegacyImporter`、`LegacyImportError`；旧共享表 → staging candidate，失败保留旧库 |
| `stock_manager.storage.migrations` | `migrate_database`、`schema_version`、`CURRENT_SCHEMA_VERSION`；版本化幂等迁移 |

领域契约（P5-RD-1）追加在 `stock_manager.domain`：`SyncPlan`、`SyncTask`、`CandidateGeneration`、`IngestBatch`、`CoverageVerification`、`GenerationPartition`、`PublishedGeneration`、`ActiveGeneration`、`VerificationIssue`、`VerificationReport`、`ReadinessResult` 及配套枚举（`SyncPlanMode`、`SyncSource`、`SyncPlanStatus`、`SyncTaskStatus`、`CandidateGenerationStatus`、`VerificationStatus`、`IssueType`、`Repairability`、`ReadinessStatus`）。

Repository 侧：`stock_manager.protocols.DataSyncAdminRepositoryProtocol`（书签表 CRUD 契约），由 `SQLiteRepository` 实现；`SQLiteRepository.__init__` 集成 `migrate_database`。

## 3. 数据库迁移（P5-RD-1）

- `PRAGMA user_version` 作为 schema 版本；v0 = 旧 schema，v1 = P5 初版，v2 = 多批次 manifest，**v3 = 当前**。
- v1 迁移：`daily_bars`/`stocks`/`fundamentals`/`dividends` 增加 `batch_id TEXT` 列 + `(batch_id, <时间列>)` 索引；`dataset_versions` 增加 `manifest_sha256`/`parent_generation`；新建 `sync_plans`、`sync_tasks`、`candidate_generations`、`ingest_batches`、`generation_partitions`、`coverage_verifications`、`active_generations`、`seed_imports` 与 `*_staging` 镜像表。
- v2 迁移：`generation_partitions` 主键扩展为 `(generation, data_type, partition_key, batch_id)`，同一区间的多个股票批次不再互相覆盖。
- v3 迁移：删除已经取消的 Provider 每日请求额度与持久化熔断表；不改动行情、staging、generation 或 coverage 数据。
- 迁移幂等、逐版本事务提交；`user_version` 高于代码支持版本时拒绝打开。
- 真实库副本（850 MB）验证：0→1 迁移、重复执行幂等、线上库未被修改。

## 4. 同步流水线语义

1. **规划**：`SyncPlanner` 基于交易日历、股票池与目标窗口生成确定性 plan/task；REPAIR 只消费 `VerificationReport` 中 `repairability=REFETCH` 的条目，`MANUAL`/`REBUILD`/不可信输入抛 `PlanRejectedError`，绝不生成普通重拉任务。
2. **抓取**：`SerialFetchWorker` 单通道串行执行，Provider 失败抛明确 `ProviderFetchError`（不吞异常）；并发访问被 `ConcurrentProviderAccessError` 拒绝。
3. **隔离写入**：`StagingWriter` 把 staging 行、`ingest_batches` checkpoint 与 `write_revision+1` 放在同一 `BEGIN IMMEDIATE` 事务；事务内重读 candidate 状态，拒绝向已失败/已发布 candidate 写入。
4. **验证**：`CoverageVerifier` 对每个 task/batch 独立生成证据，按上市/退市生命周期与交易日逐日计算日线覆盖率（不足 95% → INCOMPLETE + MISSING），并检测重复、无效值和 PIT 违规。
5. **发布**：`GenerationCommitter` 单事务内 recheck candidate/验证状态（revision/manifest 一致）→ publish-copy staging 行到正式表 → 固化 `generation_partitions` → 写 `dataset_versions` → 切换 `active_generations`。任一步失败整体回滚，active 指针不变。
6. **读取门禁**：`ReadinessGate` 只返回 `READY(generation)` / `NO_GENERATION` / `OUT_OF_RANGE` / `MISSING_DATA_TYPE` / `INCOMPLETE`；非 READY 带明确原因，禁止隐式联网。

## 5. 种子与跨平台迁移（P5-RD-7）

- 权威 SHA-256 只放外部 sidecar manifest（写回库内会自引用）；`stream_sha256` 分块流式计算并报告进度。
- `SeedPackageVerifier`：文件名/SHA-256/`PRAGMA integrity_check`/当前 `user_version=3` 逐项校验，任一失败抛 `SeedVerificationError`（REJECTED）。
- `TransferPreparer.prepare`：`wal_checkpoint(TRUNCATE)` + integrity + 生成 `*.transfer.json`；`verify_transfer` 重算 SHA 比对并检查 schema 与绝对路径。
- 实测：单日分区 publish-copy 0.056 s；ALTER ADD COLUMN 元数据级瞬间完成。

## 6. CLI 子命令（P5-RD-8）

```text
stock-manager sync-plan --db ... --mode BOOTSTRAP|INCREMENTAL|LEGACY_IMPORT \
    --start ... --end ... --adjustment ... [--data-types ...]
stock-manager sync-import-legacy --db ... --adjustment ... --date ...
stock-manager sync-verify --db ... --candidate ... --adjustment ... --start ... --end ...
stock-manager db-prepare-transfer --db ... --out <dir>
stock-manager db-verify-transfer --db ... --manifest <file>
```

所有子命令只调用 Service/Repository 层，不直接操作 Provider 或散落 SQL；异常统一 JSON `{"error": ..., "message": ...}`。

## 7. 验收证据与红线核对

| 计划红线 | 证据 |
|---|---|
| 未发布 candidate 对筛选/回测不可见 | RD4 `test_staging_invisible_to_published_read`、RD10 E2E |
| generation 绑定实际分区、发布原子 | RD6 `test_publish_*`（revision 竞态、状态竞态、故障注入）、RD10 |
| coverage 逐日/逐股票池/逐类型证据、95% 边界 | RD5 95% 边界、MISSING/DUPLICATE/INVALID/PIT 用例 |
| 失败不伪装成功 | RD5 `test_sqlite_failure_raises_verification_error`、RD10 发布失败保留旧 active |
| 断网只读已发布 generation | RD10 E2E 全离线流水线 + 正式表读取 |
| 旧库迁移可回滚、不删数据 | RD9 `test_verification_failure_keeps_legacy`、import 不修改旧表 |
| SHA-256/integrity/schema/provenance | RD7 全部用例 |
| Provider 串行、显式 adjustment | RD3 全部用例 |
| 无吞异常、类型标注完整 | 全量测试 + 代码审查 |

## 8. 与计划的差异说明

- **发布模型**：计划 §6.1 的「不可变批次」在本实现中落地为「staging 隔离 + 发布时事务内 publish-copy 到正式表」——正式表保存当前 active generation 的行，manifest 与 active 指针不可变；REPAIR 发布会原子替换被修复分区并 supersede 旧 generation（见 ADR「结果」）。这是真实库 5.16M 行、避免影子表 2 倍磁盘峰值的实现选择，已写入 ADR。
- **第 17 章第 8 条**：Mac→Windows 物理迁移人工验收需 Windows 实机，2026-09-01 用户确认不纳入完成定义；离线测试与迁移工具已交付，Windows 端实机验收由用户执行。

## 9. 集成收尾（SyncPipeline 与门面、CLI、Web）

### 9.1 SyncPipeline 编排器

`stock_manager.sync.pipeline`（`SyncPipeline`、`PipelineRun`、`PipelineError`、`RetryCooldownError`）：

- `plan(...)`：调用 `SyncPlanner` 生成并持久化 plan/tasks/candidate。
- `execute(plan_id)`：串行执行 PENDING/INTERRUPTED 任务 → worker 取数 → staging 写批次 → 验证 → 全部 COMPLETE 时发布；已 SUCCEEDED 计划重复执行零 Provider 调用（幂等跳过）。
- `retry(plan_id)`：显式重试 FAILED/INTERRUPTED/NEEDS_REPAIR/VERIFICATION_FAILED，受 `not_before` 冷却约束，冷却未到抛 `RetryCooldownError`；失败状态不会被普通 `execute()` 或崩溃恢复偷偷解锁。
- `mark_interrupted()`：进程重启后把 RUNNING 任务标 INTERRUPTED。
- 验证失败闭环：`MISSING/INVALID`（REFETCH）→ candidate `NEEDS_REPAIR`，可由 REPAIR 计划修复后重跑；验证器自身异常 → `VERIFICATION_FAILED`。

### 9.2 DataSyncService 门面

`DataSyncService.build_pipeline()` / `run_pipeline_plan(...)` / `run_pipeline_execute(plan_id)`：

- 复用同一 provider 实例（串行限速/socket 超时/重登录保护继承），构造 planner/worker/staging/verifier/committer/gate/legacy。
- `run_pipeline_execute` 在 `_provider_process_lock + persistent_file_lock(_provider_file_lock)` 内执行，保持「Baostock 只能由 DataSyncService 边界调用」与并发保护红线。
- 旧同步路径（`sync` / `backfill_history_v2` 等）保留兼容。

### 9.5 默认入口切换（灰度开关）

新增配置项 `policy.pipeline_default: bool`（默认 `false`，`SyncConfig.pipeline_default`，`config/sync.json` 未启用时行为与旧版完全一致）：

- `DataSyncService.startup_sync(...)`：Web 启动自动回补的默认入口。`pipeline_default=true` 时通过 SyncPipeline 规划并执行（首次启动 BOOTSTRAP，已有 active generation 则 INCREMENTAL）；`false` 时回退 `backfill_on_startup_v2`（八年）或 `backfill_on_startup`（一年），现状不变。
- CLI `sync`：`pipeline_default=true` 时走 pipeline（BOOTSTRAP 单日计划 + 执行）；`false` 时走旧 `service.sync(...)`。
- 切换开关不修改 `config/sync.json` 线上配置：是否启用由用户显式设置，实网验收由用户执行。

新增测试：`tests/test_p5_facade.py::TestPipelineDefaultSwitch`（配置解析、启动路由到 pipeline、未启用回退旧路径）。全量 **561 passed**（版本保持 1.12.0）。

### 9.3 CLI 与 Web

CLI 新增（`tests/test_p5_cli_pipeline.py`）：

```text
stock-manager sync-start --db ... --config ... --lock-dir ... \
    --mode BOOTSTRAP|INCREMENTAL|LEGACY_IMPORT --start ... --end ... --adjustment ...
stock-manager sync-retry --db ... --config ... --lock-dir ... --plan-id ...
stock-manager sync-status --db ... [--plan-id ...]
```

Web `/api/sync/status` 新增 `p5_plans`（plan/task_counts/candidate_status）与 `active_generation` 字段（`tests/test_web_api.py::test_sync_status_exposes_p5_plan_state`）。

### 9.6 首次启动门禁页（P5 §5.1，前端）

`index.html` 重构为两个视图：

- **`#gate-view`（前置初始化门禁页）**：默认显示。提供「在线 Bootstrap / 导入种子 / 增量同步」来源选择、复权方式、种子路径、启动按钮与同步/回补进度条；数据未就绪时工作台不可见。
- **`#workbench-view`（筛选工作台）**：仅当 `readiness.status == READY` 时显示。

后端 `POST /api/sync/bootstrap` 支持 `source`：`online`（在线 Bootstrap，经 `startup_sync` 走 SyncPipeline）、`seed`（外部 manifest 校验 → `LegacyImporter` 导入）、`incremental`（已有 active generation 时补齐尾部）。门禁判定依据 ReadinessGate：无 active generation → `NO_GENERATION` → 门禁页；部分数据未发布 → 同样门禁页并显示回补进度。

> 1.13.1（2026-09-02）起：有已激活 generation 时，数据页可点选本地库后手动进入工作台（不再强制 `READY`）；仅当完全没有已激活代时，才停留在本页描述的首次启动流程。放行规则与数据页新增能力详见 §12。

### 9.7 八年回补进度改为按完整入库股票数

原 `_v2_window_coverage` 按「每年最大覆盖数做基准」的逐日判定会在只拉一批时立即 100%。现改为**按完整入库股票数**：

- `SQLiteRepository.daily_bar_code_counts()`：`{code: 窗口内 distinct 交易日数}`。
- 一只股票在窗口内 bar 天数 ≥ 窗口交易日数的 95% 才算「完整入库」；`progress = 完整入库数 / 当前股票池规模`。
- 前端文案改为「总进度 X%（已完整入库 A/B 只股票）」。
- 真实部分库验证：20/5,214 只完整 → 0.38%，不再假 100%。
- 测试：`tests/test_backfill_v2_progress.py` 更新为股票语义；`tests/test_web_api.py::TestStockBasedProgress` 覆盖长窗口部分入库场景。

### 9.8 上市/退市窗口（P5 §7.3）新上市股票在八年窗口内的交易日数远小于窗口总天数，按全窗口 95% 判定会被永久误标不完整。修正：

- **Provider**：`BaostockProvider.fetch_stock_basics()` 分页调用 `query_stock_basic`（每页 2000，全市场约 3 页），解析 `ipoDate`/`outDate` 填充 `StockIdentity.listed_on`/`delisted_on`；`fetch_stocks()` 在同一 session 内合并（`session=False` 复用外层会话，不额外 login）。旧客户端无 `query_stock_basic` 时发出可见告警并保守降级为未知上市日期。
- **进度判定**：`_v2_window_coverage` 中每只股票的期望交易日 = 窗口 ∩ [listed_on, delisted_on]；上市日期未知时退化为全窗口（保守）。窗口外上市/退市的股票不纳入分母。
- 测试：`tests/test_baostock_provider.py`（basics 解析、合并、缺失降级）；`tests/test_web_api.py::TestListingWindowProgress`（中途上市股票只看上市以来）。
- 真实库现状：当前 `stocks.listed_on` 为 NULL（旧同步未填）；下次重拉 stocks 后自动带上市日期，进度口径自动收紧。

### 9.9 批量粒度 Planner（回补/增量统一 20 只 × 区间）

新 `SyncPlanner` 的任务粒度由「每股 × 每交易日」改为**批量粒度**（与旧 v2 同级，用户 2026-09-02 确认回补与增量同步统一采用）：

- `PlanInput` 增加 `batch_size`（默认 20，进入 `plan_fingerprint`）；`planner_version` 升至 `p5-rd2-2`。
- `_window_tasks` 重写：
  - `stocks`：每交易日 1 任务（全市场快照，快照语义）。
  - `daily_bars`：代码按 `batch_size` 分批，每批 1 任务覆盖**整个目标区间**（`range_start..range_end`，一次串行请求，同 v2）。
  - `fundamentals`：代码分批，`as_of = target_end`。
- 任务量对比：八年全量 5,214 只 → daily_bars ≈ 261 任务（÷20），而非旧粒度约 1,040 万；增量（1 个交易日）≈ 1 + 261 + 261 ≈ 523 次请求（非 5,000 次/股）。
- 配套适配：
  - `CoverageVerifier._verify_daily_bars` 按区间判定：每只代码在该区间内的 distinct 交易日 ≥ 95% 视为完整；无效值检查保留；重叠批次按代码去重（行级重复由 staging 主键结构性防止）。
  - `ReadinessGate` 分区键支持 `YYYY-MM-DD..YYYY-MM-DD` 范围，覆盖边界取范围终点。
  - `verifier._partition_date` / `committer._partition_date` 支持范围键。
- 请求路径不变：`SerialFetchWorker → BaostockProvider`（串行、限速、超时、重登录），业务层不直连 SDK。
- 测试：`tests/test_p5_rd2_planner.py`（批量任务、batch_size 分块）、`tests/test_p5_rd5_verifier.py`（区间完整性、重叠去重、无效值）、`tests/test_p5_rd10_e2e.py`（批量流水线）、`tests/test_p5_rd6_committer.py`（范围分区键门禁）。全量 **572 passed**。

### 9.4 本轮测试

新增 13 项：`tests/test_p5_pipeline.py`（7）、`tests/test_p5_facade.py`（3）、`tests/test_p5_cli_pipeline.py`（2）、`tests/test_web_api.py`（1）。全量 **557 passed**（版本保持 1.12.0）。

## 10. 文档同步

- 计划：`development/plan/P5_RECONSTRUCT_DATASYNC.md`（draft，实现完成后由验收更新状态）。
- 架构：`development/architecture/ADR_P5_DATASYNC_DATABASE.md`（accepted）。
- 本手册：`development/implementation/P5_RECONSTRUCT_DATASYNC_IMPLEMENTATION.md`。
- 所有文档含 `date`/`purpose`/`project`/`status` frontmatter，存放于 `project_doc`。

## 11. 2026-09-02 稳定性修复与真实库恢复

### 11.1 事故根因

真实工作库出现「批次已经下载，但当前 Web 数据库没有 ingest checkpoint」与 runner 重复登录。确认根因为：

1. Web 启动 `run_backfill_v2.py` 时只传配置文件，没有传 `WebConfig.database_path`、`lock_directory` 和用户选择的 adjustment；自定义数据库场景下 runner 会写默认 `data/market.sqlite3`。
2. 旧 `StagingWriter` 分两个事务提交 staging 行与 `ingest_batches`，第二阶段失败会留下孤儿 staging。
3. runner 默认外层自动尝试 15 次；失败 candidate 未通过显式 retry 正确解锁，造成重复登录与无效重试。
4. 多个股票批次共用相同 `partition_key`，旧 verifier 去重证据且旧 `generation_partitions` 主键覆盖同区间的其他 batch。

### 11.2 已修复契约

- Web runner 参数固定包含 `--db`、`--lock-dir`、`--adjustment`；runner 锁也使用同一个配置锁目录。
- runner 默认一次显式尝试；单请求瞬时失败仍由 Provider 有界重试。FAILED task/candidate 只能由显式 retry 解锁，并继续受 cooldown 约束。
- staging 行、批次摘要、ingest checkpoint、revision 在同一事务提交；LegacyImporter 使用同一原子契约。
- `CoverageVerifier` 不再按 `(data_type, partition_key)` 丢弃批次；每个 task 产生唯一证据。`GenerationCommitter` 要求每个 candidate batch 恰有一条唯一 COMPLETE 证据，并继承 parent generation 的 manifest。
- Baostock session 对已连接 socket 设置超时；日线与基本面查询均可在会话失效后重登录；错误码 `10001011` 只让当前请求立即失败且不自动重试，不记录跨进程/跨重启熔断状态。
- 不设置本地每日请求次数软上限或硬上限，也不维护 Provider 请求计数账本；串行访问、请求间隔、socket 超时和单次请求内的有界重试仍保留。
- 基本面查询回看最近 60 个自然日并取 `as_of` 以前最新一行，避免停牌日精确日期无行造成不必要缺失。
- 全新数据库在 Planner 前先获取并本地保存股票池；已有 generation 的增量从本地 `daily_bars` 实际 coverage_end 后一个交易日开始。

### 11.3 真实库处置与验收证据

- 迁移前 SQLite 一致性备份：`data/market.pre-stability-v1.sqlite3.bak`（Git ignored，约 215 MB，`integrity_check=ok`，schema v1）。
- 工作库迁移到 schema v3 后：`integrity_check=ok`；已取消的两张空表完成删除。
- 从 SUCCESS task 与精确 row_count 恢复登记可信批次；最终状态为 12 个 SUCCESS checkpoint（stocks 1、daily_bars 11）。清理 3 个无成功任务证明的孤儿 daily-bar batch，共 117,865 行；清理内容仍可从上述备份恢复。
- 工作库最终保留 434,025 行可信 daily-bar staging；计划为 `PLANNED`、candidate 为可续传的 `WRITING`，任务为 12 SUCCESS / 511 PENDING。未发布 candidate 对筛选和回测仍不可见。
- 本地 Web 服务已重启并加载新代码；收尾时意外启动的 runner 已终止，残留 RUNNING 任务已安全重置为 PENDING，当前无同步锁持有。
- 正常系统权限下全量离线测试：**594 passed，5 warnings**。warning 为测试 fake client 缺少 `query_stock_basic` 的显式降级告警，以及既有幂等跳过提示；无失败。

### 11.4 当前能力边界

本轮目标是「稳定、安全、可断点续传地把 Baostock 数据落入当前本地库」。八年日线可以按 generation 验证和发布；但当前 `fundamentals` 任务仍只同步 `target_end` 的最新截面，`stocks.is_st` 也是快照字段，不构成八年逐日 PIT 历史。因此：

- `non_st` 与 `pe_positive` 可以进入筛选/回测的规则计划和请求契约；
- 它们只能在对应研究日有可信股票状态/基本面快照时执行；
- 在补齐历史 PIT 基本面与历史 ST 状态前，不得把当前快照回填到过去日期，也不得宣称八年历史回测已完整支持这两个条件。

## 12. 2026-09-02 UI：门禁放行、数据页增强与返回导航（1.13.1）

用户反馈「增量同步期间被锁在数据页无法使用已有数据做研究」，本版对 Web 门禁与数据 UI 做向后兼容增强（`tests/test_web_api.py` 共 48 passed 含本版新增断言）。

### 12.1 放行规则：有已激活库即可选择进入

- `/api/sync/status` 新增 `can_enter`：`market/qfq` 存在已激活 generation（`active_generations` 有行）时为 `true`。
- 门禁页新增「本地数据库」区：展示当前 generation 标识、激活时间、**数据截至日**（`latest_synced_trading_day`）、股票数与覆盖区间提示；说明「本地仅保留最新一代数据；未覆盖交易日筛选会被拒绝」。
- 用户点选该库（即使只有一项）后，「使用所选数据库进入筛选工作台」才启用；不再要求 `readiness.status == READY`。
- 筛选安全性不变：`POST /api/screen` 仍按请求交易日走 `ReadinessGate`（未覆盖日返回 404 与原因），不因放行而绕过。
- 首次启动（无任何已激活代）流程不变：门禁页只提供在线 Bootstrap / 导入种子 / 增量同步。

### 12.2 数据页 runner 状态与停止按钮

- `/api/instances` 的进程匹配扩展为 `stock_manager|run_backfill`，使独立回补 runner（`scripts/run_backfill_v2.py`）对 Web 可见。
- 门禁页显示「回补进程运行中 · pid N」+「停止同步」按钮，复用 `POST /api/instances/kill`（SIGTERM）。
- 停止后的状态收敛沿用既有机制：runner 死亡后 RUNNING 计划经 30 秒 staleness 重置为 `PLANNED`、RUNNING 任务标 `INTERRUPTED`；未完成任务下次显式启动续跑，SUCCESS 任务不重拉。

### 12.3 视图切换与说明文案

- 工作台顶部新增「← 数据同步」按钮（`state.uiView` 手动视图优先于自动切换），数据页与工作台可随时互切。
- 数据同步区新增说明文案：「1. 数据增量约需 4~5 小时；进度条若卡住会在 5 分钟后自动重启。」

### 12.4 改动与验收

- 后端：`src/stock_manager/web/app.py`（`can_enter`、instances 匹配）。
- 前端：`src/stock_manager/web/static/index.html`、`static/app.js`（视图切换、版本选择、runner 行、文案）。
- 版本：1.13.0 → 1.13.1（PATCH，新 UI 内容；三处版本号同步）。
- 测试：`test_sync_status_exposes_p5_plan_state` 增 `can_enter is True`；`test_app_starts_on_first_run_without_database` 增 `can_enter is False`；新增 `test_instances_endpoint_lists_backfill_runner`（mock `subprocess.run` 输出含 `run_backfill_v2.py`）。

### 12.5 三栏横向布局与数据库条（1.13.2）

用户反馈主 UI 竖排不顺。工作台改为三栏网格，并新增顶部「数据库条」：

- **顶部数据库条（全宽）**：`← 数据同步` 按钮 + `#sync-status-title`（数据状态：最近同步/覆盖/N 只）+ `#wb-active-gen`（当前库 generation）。
- **左栏**：运行筛选（数据集/交易日/复权/worker/代码 → 运行筛选）+ 回测基础数据（窗口/初始资金/最大持仓 → 运行回测）。
- **中栏**：筛选模版（模板选择/校验/保存/另存/删除 + 规则卡片 + 组合逻辑）+ 策略模版（回测六类政策）。
- **右栏**：筛选结果（`#result-body`）+ 回测结果（`#bt-result`）。
- **底部全宽「数据同步与维护」**：同步进度条、覆盖示意图、bootstrap 面板、停止服务、实例列表。
- 样式：`styles.css` 新增 `#workbench-view` 网格（300px / 1fr / 400px，≤1180px 两栏、≤860px 单栏）、`.db-bar`、`.wb-col`；`#gate-view` 与 `#workbench-view` 各占满网格全宽。所有元素 id 不变，`app.js` 无需改动。
- 版本：1.13.1 → 1.13.2（新 UI 内容，PATCH）。
- 1.13.4：数据 UI 与工作台「数据同步与维护」面板的初始化方式默认选为「增量同步」（非 Bootstrap/回补）。
- 1.13.5：主 UI 底部新增「进程管理」，区分**主进程**（本服务 web）与**下载进程**（回补/增量 runner）；runner 状态文案按实际计划模式显示（增量/回补），不再一概称「回补进程」。

### 12.6 年度覆盖按当年股票池计算（1.13.3）

「数据状态」的年度覆盖条原以最新一年的股票总数（5,214×0.95）判定全部历史年完整性，导致 2018~2022 等早年（当时市场股票更少）被误标「未完全同步」。修正：`_sync_status` 的 `year_bands` 按**该年实际股票池**判定——以截至该日在当已出现过的 distinct 股票数峰值（年内累计）作为该日参考，某天完整 ⇔ 当天 bar 股票数 ≥ 该日参考×0.95；当年新股上市只增不减，避免把市场增长误判为数据缺失。版本 1.13.2 → 1.13.3（修复 PATCH）。

### 12.7 历史回测股票池向后重建（1.13.6）

**现象**：回测报「historical run blocked by missing data: rule non_st requires historical stock universe snapshots, but no stocks coverage exists」。

**根因**：`non_st` 是 `UNIVERSE_STATE_PIT_READY` 规则。历史回测的 `universe_as_of(day)` 与执行器 `_universe_for(day, snapshots)` 均按契约取 `as_of <= day` 的快照；而 `stocks` 表只有 2026-09-01 单一快照，于是任何早于该日的回测起点（如 5 年窗口 2021-09）都无快照 → 返回空 → 守卫判「无股票池覆盖」→ 拦截。这与接线复杂度无关，是"历史股票池快照不足"的数据契约问题，P5 §11.4 早已声明该边界。

**修复**：当无 `as_of <= day` 快照时，回退到**最早快照**，用 `listed_on / delisted_on` **向后重建**该日股票池存在性（`SQLitePointInTimeReader.universe_as_of` 与 `historical_screening_executor._universe_for` 两处一致）。存在性 PIT 正确（上市/退市日为事实）；ST 仍取快照值，属 P5A-2 §约定「最近快照近似」——不回填历史逐日 ST、不宣称八年完整支持 ST。真实库验证：`universe_as_of(2021-09-01)` 由 0 → **4,268 只**。新增回归测试 `test_universe_reconstructed_before_earliest_snapshot` 与 `test_universe_for_reconstructs_before_earliest_snapshot`。全量离线测试 **597 passed**。版本 1.13.5 → 1.13.6（修复 PATCH）。

### 12.8 Web 启动清理孤儿回测任务（1.13.7）

**现象**：服务重启后，上一进程提交的回测仍显示 `QUEUED(0/4)`/`BUILDING_SIGNALS` 且永不推进——进程内 `BoundedJobRunner` 随进程消亡，遗留任务成孤儿堵住 UI 队列。

**根因**：`HistoricalScreeningRunStore.recover_interrupted`（重启后把 QUEUED/进行中任务标记 INTERRUPTED）只被测试调用，Web 启动未接线。

**修复**：`ResearchBacktestService` 新增公开方法 `recover_interrupted_runs()`，Web `WebApp.__init__` 构造回测服务后调用一次。新增测试 `test_web_startup_marks_orphaned_backtest_runs_interrupted`。全量离线测试 **598 passed**。版本 1.13.6 → 1.13.7（修复 PATCH）。

### 12.9 回测并发数 UI 可配置（1.13.8）

回测的 eligibility worker 原在 Web 装配写死 `max_workers=2`，与「运行筛选」的 Max-Workers 可调不一致。本版对齐：

- 回测基础数据卡片新增「回测并发数（Worker 数，1-16）」输入（`#bt-workers`，默认 2）。
- `ResearchBacktestService.submit` 新增 `max_workers` 参数（1..16 校验，None 回落服务默认）；按 `run_id` 记录，`_build_eligibility` 建执行器时取用。
- Web `POST /api/research/backtests` 解析并校验 `max_workers` 后传入。
- 测试：`test_research_accepts_max_workers` / `test_research_max_workers_upper_bound_is_16` / `test_research_max_workers_lower_bound_is_1`。全量离线测试 **601 passed**。版本 1.13.7 → 1.13.8（PATCH，UI 内容）。
- 备注：回测与筛选仍各自独立 worker 池（不同入口的并发机制），统一并发入口另行评估。

### 12.10 回测前置提示（1.13.9）

「回测基础数据」面板「用此模板回测」按钮下方新增提示：进行回测前必须把模板里的 PE（市盈率）选项关掉。原因：本地基本面仅最新交易日一份快照（published_on 单日），开着 `pe_positive` 会让历史窗口每日无入选，回测不产生交易（实证：test 模板 260 天入选池仅 1 天非空、订单 0；去除后 316/520 天非空、产生交易）。纯 UI 文案变更，版本 1.13.8 → 1.13.9（PATCH）。

## 13. 开发分支合并验收（2026-09-02）

- 合并来源：`codex/fix-datasync-stability`，开发分支提交 `d0777cd`，包版本 1.13.9；相对原 `main`（`6148bea`）包含 13 个开发提交。
- 同时保留需求澄清与任务职责文档更新（`59f6b09`）：大量、系统追问以形成精确 plan，职责由当前提示词指定；根目录与 Vault 的 `AGENTS.md` 保持一致。
- 在文档分支合入开发内容后验证，再将相同结果快进合入本地 `main`。`project_doc/README.md` 自动合并成功，无未解决冲突。
- 从代码目录执行 `.venv/bin/python -m pytest -q`：600 passed、1 failed、5 warnings，用时 8.25 秒。唯一失败为 `tests/test_launcher.py::test_server_running_is_false_on_closed_port`，原因是沙箱禁止绑定 `127.0.0.1` 临时端口，抛出 `PermissionError`。
- 在获准的沙箱外环境仅复测上述端口测试：1 passed，用时 0.02 秒。合计 601 项均获得通过证据；未将首轮测试表述为全绿。测试使用本地 fixtures；该复测只访问本机回环地址。
- 本次仅合并与验证，不启动 Web/数据同步服务，不执行真实行情数据库迁移或重新拉取数据；未执行浏览器人工验收、实网同步或 Windows 实机验收。
- README 与 Vault 使用手册同步更新主分支版本及合并状态；原有未跟踪的架构图产物不纳入本次提交。本地合并不代表已推送远端或创建发布标签。

## 14. 增量同步断点续传链（方案 A，1.17.0，2026-09-05，功能分支 feat/sync-resume-chain）

需求与验收计划：[SYNC_RESUME_CHAIN_PLAN](../plan/SYNC_RESUME_CHAIN_PLAN.md)（active）。本记录为开发分支实现证据，**尚未合入本地 main，也未推送远端或打标签**；合入后如需可再按第 13 节补合并验收。

### 14.1 目标与改动

用户反馈增量同步网络掉线重开后“从 0 重拉”。根因核对（代码事实）：P5 pipeline 每个任务（20 只 × 区间）成功后批次是有持久化的（staging 行 + `ingest_batches` + 任务 SUCCESS 同事务），但 `plan_id/candidate_id/batch_id` 绑定目标窗口与 candidate；`startup_sync` 不检查在途未完成计划，重开时 `target_end` 前移即生成新计划，旧在途批次作废、整窗重拉。capm 数据集入口 `sync_capm_reference_data` 已有“先续在途计划、发布后再追平”的链式语义（含跨日测试），市场入口缺失该层。

实现（纯编排层，不动 staging/committer/planner/verifier/`pipeline.execute` 语义与表结构）：

- `DataSyncService.startup_sync`（pipeline 分支）改为链式：先 `_resume_pending_plan_for_startup` 续传“窗口冻结”的在途 BOOTSTRAP/INCREMENTAL 计划（`recover_interrupted` 后按状态 retry/execute，失败即停、保留 FAILED、显式重试），发布成功后再按当前 coverage 规划并执行尾差追平到最新已完成交易日。
- 陈旧已覆盖计划跳过不续：在途计划 `target_end <= 当前 coverage_end` 判为已被更宽已发布数据取代；REPAIR/LEGACY_IMPORT 模式不进入自动续传链。
- 已最新修复：published coverage 已达目标时返回可辨识的跳过结果（优先复用 SUCCEEDED/已发布 candidate 计划的 skip 语义，带 warning、零 Provider 调用），不再静默返回 `None`；`scripts/run_backfill_v2.py` 对 `None`/跳过结果判为 SUCCEEDED 并提示（原会把“已最新”误报为失败）。
- 已确认决策（2026-09-05 用户确认）：只改 market `startup_sync` pipeline 入口；链上失败即停；陈旧计划跳过；已最新误报顺手修复；capm 与 legacy v2 fallback 路径不动。

### 14.2 证据

- 代码：`code/src/stock_manager/sync/data_sync_service.py`（`startup_sync` 及新增 `_resume_pending_plan_for_startup` / `_already_latest_result`）；`code/scripts/run_backfill_v2.py`（None/跳过判成功）。
- 测试：新增 `code/tests/test_sync_resume_chain.py`（6 项，全离线 fixture）：跨日续传+追平且零重拉（SUCCESS 批次不重拉、仅掉线批与新增日拉取，fetch 调用集精确断言）、陈旧已覆盖 FAILED 计划跳过不续、已最新重入零 Provider 调用并返回跳过结果、显式重试与冷却（`RetryRequiredError`/`RetryCooldownError`，冷却后恢复）、REPAIR 计划不被自动续、首启 BOOTSTRAP 中断后先续旧窗再 INCREMENTAL 追平。
- 回归：功能分支全量离线 `.venv/bin/python -m pytest -q`：**730 passed**（无回归）。前端 JS 未改动。
- 版本：1.16.0 → 1.17.0（MINOR，同步恢复能力增强，向后兼容），已同步 `pyproject.toml`、`src/stock_manager/__init__.py`、`tests/test_package_structure.py`。
- 待办（未做，不阻塞本期）：真实 Baostock 网络验证由用户本机执行并补记；watchdog 重启与冷却对齐、Web 冷却倒计时文案（计划 §3 明确不做）；方案 C（跨 plan 批次签名复用）待单独决策。

## 15. 2026-09-05 coverage_ratio 域校验放宽与真实库手动发布（修复）

### 15.1 事故根因

market INCREMENTAL 计划 `plan-2023af33f6d93396`（窗口 2026-09-02..2026-09-04，523 个任务全部 SUCCESS、staging 完整）在发布前的**验证阶段**抛出未分类的 `ValueError: coverage_ratio must be within [0, 1]`：

- 比对对象：staged 2026-09-04 股票快照 **5,215 只**（含新上市扩容 1 只）vs 已发布池（`stocks` 最新 as_of=09-01）**5,214 只** → `coverage_ratio = 5215/5214 ≈ 1.0002 > 1`，被 `domain.CoverageVerification.__post_init__` 的 `[0, 1]` 域校验拒绝。
- 异常路径缺陷：该 `ValueError` 不是 `VerificationError`，未被 `_verify_candidate` 归类；`pipeline.execute` 兜底只把 **plan 置 FAILED**，且只在 candidate 为 PLANNED/WRITING 时置 FAILED——当时 candidate 已是 **VERIFYING**，于是永久停在 VERIFYING、`coverage_verifications` 零证据。
- 后果：`pipeline.retry()` 只认 NEEDS_REPAIR / VERIFICATION_FAILED / FAILED，四者皆非 → `nothing to retry`，每次启动 0 分钟放弃，market active generation 停留在 09-02（数据只到 09-01）。同日 CAPM 计划正常发布（走 `CapmVerifier`，股票池基准为 0），不受影响。

### 15.2 修复（代码）

`code/src/stock_manager/domain.py` `CoverageVerification.__post_init__`：coverage_ratio 合法区间由 `[0, 1]` 放宽为 **`[0, 2)`**（下界保持 0，保留低覆盖 → INCOMPLETE → NEEDS_REPAIR 修复闭环；上界 2.00 兜住股票池扩容等 staged > published 的合法场景，≥ 2.00 仍拒绝）。

### 15.3 测试

- `tests/test_p5_rd1_contracts.py`：契约拆分补充——ratio>1（1.5、1.0002）接受、ratio=2 拒绝、ratio>2（2.1）拒绝、负 ratio 拒绝。
- `tests/test_p5_rd5_verifier.py`：新增 `test_stocks_snapshot_larger_than_pool_complete`——staged 5 只 > 池 4 只时验证返回 COMPLETE 且 `coverage_ratio == 1.25`，不再抛 ValueError。
- 全量离线 `.venv/bin/python -m pytest -q`：**734 passed**，无回归。

### 15.4 真实库手动发布（2026-09-05）

已下载数据未丢失：15,642 bar 行 + 5,215 股票行 + 5,214 基本面行全部在 staging/`ingest_batches`（任务全 SUCCESS）。恢复走项目自身显式重试语义、零网络请求：

1. 先在线备份：`data/market.manual-recovery-20260905_201923.sqlite3`（同尺寸，`integrity_check=ok`）。
2. candidate `cand-2023af33f6d93396-20260905130947085941`：VERIFYING → **VERIFICATION_FAILED**（合法迁移，语义=验证崩溃未留证据）。
3. CLI `sync-retry`：rerun_verification 分支重跑验证（复用已下载 staging）→ 通过 → 原子发布 → plan SUCCEEDED。
4. 验收：`active_generations` market/qfq → `cand-2023af33f6d93396-20260905130947085941`（激活 20:22:54）；父代 `cand-28d8c00caaaf17b2-20260902005054614289` → SUPERSEDED；`daily_bars` 覆盖至 **2026-09-04**（5,214 只）、`stocks` 快照 as_of 09-04（5,215 只）；523 条验证证据落库；staging 发布后清空。

### 15.5 版本与提交

- 版本：1.17.0 → **1.17.1**（PATCH，数据校验口径修复，向后兼容），已同步 `pyproject.toml`、`src/stock_manager/__init__.py`、`tests/test_package_structure.py`。
- Git 提交遵循 Conventional Commits（`fix:`），聚焦本次数据校验修复与真实库手动恢复。
- 未推送远端、未打标签。

## 免责声明

所有筛选与回测结果仅供研究参考，不构成任何投资建议。项目禁止实现自动交易功能。
