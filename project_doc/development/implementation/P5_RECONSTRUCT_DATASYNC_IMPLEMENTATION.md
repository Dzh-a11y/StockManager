---
date: 2026-09-01
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
| P5-RD-1 | 领域契约、版本化迁移骨架、Repository Protocol | `tests/test_p5_rd1_contracts.py`（44） |
| P5-RD-2 | 确定性 SyncPlanner | `tests/test_p5_rd2_planner.py`（21） |
| P5-RD-3 | SerialFetchWorker 与 Provider 安全边界 | `tests/test_p5_rd3_worker.py`（11） |
| P5-RD-4 | StagingWriter、批次与 checkpoint | `tests/test_p5_rd4_staging.py`（11） |
| P5-RD-5 | CoverageVerifier 与 VerificationReport | `tests/test_p5_rd5_verifier.py`（11） |
| P5-RD-6 | GenerationCommitter 原子发布、ReadinessGate | `tests/test_p5_rd6_committer.py`（16） |
| P5-RD-7 | 种子 SHA-256、外部 manifest、WAL checkpoint、跨平台迁移 | `tests/test_p5_rd7_seed.py`（18） |
| P5-RD-8 | CLI 同步控制子命令 | `tests/test_p5_rd8_cli.py`（9） |
| P5-RD-9 | 旧库 LEGACY_IMPORT 迁移 | `tests/test_p5_rd9_legacy.py`（8） |
| P5-RD-10 | 端到端流水线验收、版本与文档同步 | `tests/test_p5_rd10_e2e.py`（2） |

合计新增 **151 项**离线测试，全量 **544 passed**（基线 393 零回归）。版本 **1.11.3 → 1.12.0**（MINOR，新增功能）。

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

- `PRAGMA user_version` 作为 schema 版本；v0 = 旧 schema，v1 = 当前。
- v1 迁移：`daily_bars`/`stocks`/`fundamentals`/`dividends` 增加 `batch_id TEXT` 列 + `(batch_id, <时间列>)` 索引；`dataset_versions` 增加 `manifest_sha256`/`parent_generation`；新建 `sync_plans`、`sync_tasks`、`candidate_generations`、`ingest_batches`、`generation_partitions`、`coverage_verifications`、`active_generations`、`seed_imports` 与 `*_staging` 镜像表。
- 迁移幂等、逐版本事务提交；`user_version` 高于代码支持版本时拒绝打开。
- 真实库副本（850 MB）验证：0→1 迁移、重复执行幂等、线上库未被修改。

## 4. 同步流水线语义

1. **规划**：`SyncPlanner` 基于交易日历、股票池与目标窗口生成确定性 plan/task；REPAIR 只消费 `VerificationReport` 中 `repairability=REFETCH` 的条目，`MANUAL`/`REBUILD`/不可信输入抛 `PlanRejectedError`，绝不生成普通重拉任务。
2. **抓取**：`SerialFetchWorker` 单通道串行执行，Provider 失败抛明确 `ProviderFetchError`（不吞异常）；并发访问被 `ConcurrentProviderAccessError` 拒绝。
3. **隔离写入**：`StagingWriter` 把行写入 `*_staging`（键含 `batch_id`），每次成功写入 `write_revision+1`；candidate 数据对 published 读取完全不可见。
4. **验证**：`CoverageVerifier` 读取 staging 实际行，逐日计算日线去重覆盖率（<95% → INCOMPLETE + MISSING）、检测重复/无效/PIT 违规，输出机器可消费报告。
5. **发布**：`GenerationCommitter` 单事务内 recheck candidate/验证状态（revision/manifest 一致）→ publish-copy staging 行到正式表 → 固化 `generation_partitions` → 写 `dataset_versions` → 切换 `active_generations`。任一步失败整体回滚，active 指针不变。
6. **读取门禁**：`ReadinessGate` 只返回 `READY(generation)` / `NO_GENERATION` / `OUT_OF_RANGE` / `MISSING_DATA_TYPE` / `INCOMPLETE`；非 READY 带明确原因，禁止隐式联网。

## 5. 种子与跨平台迁移（P5-RD-7）

- 权威 SHA-256 只放外部 sidecar manifest（写回库内会自引用）；`stream_sha256` 分块流式计算并报告进度。
- `SeedPackageVerifier`：文件名/SHA-256/`PRAGMA integrity_check`/`user_version` 逐项校验，任一失败抛 `SeedVerificationError`（REJECTED）。
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
- `retry(plan_id)`：显式重试 FAILED/INTERRUPTED 任务，受 `not_before` 冷却与 `max_attempts` 上限约束，冷却未到抛 `RetryCooldownError`。
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

### 9.4 本轮测试

新增 13 项：`tests/test_p5_pipeline.py`（7）、`tests/test_p5_facade.py`（3）、`tests/test_p5_cli_pipeline.py`（2）、`tests/test_web_api.py`（1）。全量 **557 passed**（版本保持 1.12.0）。

## 10. 文档同步

- 计划：`development/plan/P5_RECONSTRUCT_DATASYNC.md`（draft，实现完成后由验收更新状态）。
- 架构：`development/architecture/ADR_P5_DATASYNC_DATABASE.md`（accepted）。
- 本手册：`development/implementation/P5_RECONSTRUCT_DATASYNC_IMPLEMENTATION.md`。
- 所有文档含 `date`/`purpose`/`project`/`status` frontmatter，存放于 `project_doc`。

## 免责声明

所有筛选与回测结果仅供研究参考，不构成任何投资建议。项目禁止实现自动交易功能。
