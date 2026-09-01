---
date: 2026-09-01
purpose: 定义 P5 DataSync 重构的数据库物理 schema、批次/分区 manifest、active generation 指针、迁移成本基准、磁盘峰值与回滚策略。
project: StockManager
status: accepted
---

# ADR：P5 DataSync 数据库 schema 与 generation 发布模型

## 状态

**已接受（accepted）**：真实库基准（2026-09-01 实测）与 P5-RD-1 实现验收完成。`SQLiteRepository` 已集成 `storage/migrations.py` 版本化幂等迁移（`user_version` 0→1：数据表加 `batch_id` 列与索引、新增 8 张书签表、4 张 staging 镜像表）；领域契约、Planner、Worker、StagingWriter、Verifier、Committer、ReadinessGate、种子/迁移工具与 CLI 子命令全部离线测试通过（版本 1.12.0，新增 151 项测试）。

## 背景

现有数据库（`data/market.sqlite3`，2026-09-01 实测约 850 MB）存在三类问题，P5_RECONSTRUCT_DATASYNC 计划 §1.2 已逐项记录：

1. **半成品可见**：`daily_bars`、`fundamentals`、`stocks` 在每个 chunk 完成后直接写正式表，整体失败时筛选仍可能读到半成品。
2. **coverage 证据不足**：`actual_coverage()` 只按 `MIN/MAX` 边界判断，不能证明目标区间内每个交易日、每个应有股票和每种数据类型都完整。
3. **generation 未绑定实际行**：`dataset_versions` 只保存 generation 元数据，行情行没有 generation/批次归属；Reader 读取的仍是共享正式表。

本 ADR 固化 P5 目标架构的数据库落地形态：不可变批次（batch）+ generation 分区清单（manifest）+ active generation 指针，保证未发布 candidate 对筛选/回测不可见、发布为原子事务、旧 generation 在构建期间保持可读。

## 真实库基准（2026-09-01 实测，Apple Silicon / Python 3.14 / SQLite 3）

### 数据规模

| 表 | 行数 | 说明 |
|---|---|---|
| `daily_bars` | 5,162,485 | 八年覆盖 2018-07-12 ~ 2026-08-31（qfq），日均约 2,600 只 |
| `stocks` | 20,850 | 按 `(code, as_of)` 快照 |
| `fundamentals` | 9,892 | PARTIAL（仅 2026-08-27 ~ 08-31） |
| `dividends` | 0 | UNAVAILABLE |
| `trading_days` | 2,003 | 交易日历 |
| `dataset_versions` | 0 | 尚无 generation 发布（八年 backfill 未提交） |
| `backfill_runs_v2` / `backfill_chunks_v2` | 3 / 117 | v2 检查点 |

### 读性能（只读连接）

| 查询 | 耗时 |
|---|---|
| 全范围逐日 `COUNT(DISTINCT code)`（2,003 日） | 0.983 s |
| 全范围 `DISTINCT trading_day` | 0.617 s |
| 单代码八年读取（PK 命中） | 0.002 s |
| 单日分区全市场行数 | 0.109 s |
| 最新股票池快照 | 0.003 s |
| `PRAGMA integrity_check` | 1.771 s |

### 三种迁移方案成本（抽样 1,672,460 行 = 2025 年至今 qfq）

| 方案 | 操作 | 抽样耗时 | 全库（5.16M 行）估算 | 磁盘峰值 |
|---|---|---|---|---|
| **A. 新增 `batch_id` 列**（`ALTER TABLE ADD COLUMN`，元数据级） | ALTER | 0.000 s | 近 0 | 无翻倍 |
| | 建索引 `(batch_id, trading_day)` | 0.444 s | ~1.4 s | 索引体积 |
| | 一次性回填 `UPDATE ... SET batch_id='legacy'` | 7.698 s | ~24 s | 无翻倍（WAL/临时） |
| **B. 影子 v3 表**（`CREATE TABLE ... AS SELECT` 复制） | 建表复制 | 0.395 s | ~1.2 s | **约 2 倍（~1.7 GB）** |
| **C. 纯 manifest 无列绑定** | 只写元数据表 | — | 近 0 | 无翻倍 |

结论：**采用方案 A**。`ALTER TABLE ADD COLUMN` 在 SQLite 是纯元数据操作、瞬间完成；一次性回填为常驻行打上 `batch_id` 归属，之后每行可追溯到不可变批次；不需要影子表复制，不产生 2 倍磁盘峰值，也无需停机复制八年数据。方案 C 无法满足「generation 绑定实际行」的目标（计划 §1.2 问题 3），弃用。方案 B 作为回滚备份的兜底手段保留在迁移流程中（见「回滚策略」）。

### 发布成本

单日分区从 staging 复制到正式表（`INSERT ... SELECT`，约 2,600 行）实测 0.056 s。`GenerationCommitter` 的发布只写 manifest 与 active 指针（不复制数据），短事务目标成立；staging 侧按分区批写入，不用超大事务锁库。

## 决策

### 1. 数据表增加 `batch_id` 列（不可变批次归属）

对以下表执行幂等迁移，新增 `batch_id TEXT` 列：

- `daily_bars`：每行绑定写入它的批次。
- `stocks`：每个快照行绑定批次。
- `fundamentals`：每行绑定批次。
- `dividends`：每行绑定批次（当前为空表，仍保留列以统一写入契约）。

迁移步骤（P5-RD-1 实现，全部离线可测）：

1. `ALTER TABLE <table> ADD COLUMN batch_id TEXT`（幂等：先查 `PRAGMA table_info`，存在则跳过）。
2. 建索引（`daily_bars`：`(batch_id, trading_day)`；其余表按 `(batch_id, <主键首列>)`）。
3. 旧数据回填：`UPDATE <table> SET batch_id = '<legacy_batch_id>' WHERE batch_id IS NULL`，其中 `legacy_batch_id` 由 `LEGACY_IMPORT` 计划创建，禁止硬编码任意值。
4. 旧行 `batch_id IS NULL` 视为未归属：新 verifier 与 reader 一律不可见，直到回填完成。

`batch_id` 不进入主键；同一逻辑行（如 `(code, trading_day, adjustment)`）可能在不同批次出现，按 `generation_partitions` 的分区清单决定哪一批次对当前 generation 可见。**写入禁止用 `INSERT OR REPLACE` 覆盖已发布批次的行**；新数据必须写入新批次（新 `batch_id`），已发布批次保持物理不可变。

### 2. 新增书签表（bookkeeping tables）

| 表 | 主要用途 | 关键列 |
|---|---|---|
| `sync_plans` | 确定性计划与顶层状态 | `plan_id`、`plan_version`、`mode`、`source`、`dataset_id`、`adjustment`、`universe_policy`、`target_start/end`、`parent_generation`、`candidate_generation_id`、`required_data_types`、`task_count`、`plan_fingerprint`、`status` |
| `sync_tasks` | 串行任务、重试、冷却与 checkpoint | `task_id`、`plan_id`、`sequence_no`、`data_type`、`partition_key`、`codes`、`range_start/end`、`dependencies`、`status`、`attempt_count`、`not_before`、`row_count`、`error_code/message` |
| `candidate_generations` | candidate 身份、父 generation、写入修订号与生命周期 | `candidate_generation_id`、`plan_id`、`parent_generation`、`write_revision`、`status` |
| `ingest_batches` | 不可变数据批次、行数、来源与摘要 | `batch_id`、`candidate_generation_id`、`data_type`、`partition_key`、`codes`、`range_start/end`、`row_count`、`source`、`batch_sha256`、`created_at` |
| `generation_partitions` | generation 对数据类型/分区/批次的不可变映射 | `generation`、`data_type`、`partition_key`、`batch_id` |
| `coverage_verifications` | 逐类型/逐分区验证证据 | `candidate_generation_id`、`data_type`、`partition_key`、`expected/actual/distinct/duplicate/invalid_count`、`coverage_ratio`、`status`、`verified_revision`、`manifest_sha256`、`verified_at`、`details_json` |
| `active_generations` | 每个 dataset/adjustment 当前可读 generation | `dataset_id`、`adjustment`、`generation`、`activated_at` |
| `seed_imports` | 种子文件、source SHA-256、manifest、导入与验证结果 | `import_id`、`filename`、`source_sha256`、`manifest_json`、`schema_version`、`status`、`imported_at` |

`sync_plans.plan_id` 与 `plan_fingerprint` 由规范化计划输入确定性生成（计划 §4.2），相同输入必须产出相同计划身份；范围/复权/数据类型变化必须生成不同计划。

### 3. generation 分区清单与 active 指针

- `candidate_generations` 生命周期（计划 §6.3）：`PLANNED → WRITING → VERIFYING → VERIFIED → PUBLISHED`，`VERIFYING → NEEDS_REPAIR / VERIFICATION_FAILED / REJECTED`，`WRITING → FAILED`，`VERIFIED → INVALIDATED`，`PUBLISHED → SUPERSEDED`。
- `StagingWriter` 每次成功写入增加 `write_revision`；`CoverageVerifier` 保存 `verified_revision` 与 candidate manifest 摘要；提交条件（计划 §6.3）逐项核对，任一变化使验证失效。
- `GenerationCommitter` 在单个写事务内：重读 candidate/验证记录/parent generation → 确认全部必需分区 `COMPLETE` → 固化 generation manifest（`generation_partitions`）→ 写 published generation → 切换 `active_generations` 指针 → 写发布事件与成功终态。任一步失败整体回滚。
- 增量 generation 复用 parent 未变化分区：新 generation 的 `generation_partitions` 显式引用 parent 已发布分区（不复制数据行），只为新增/修复分区创建新 batch。
- 已发布 generation 永不因新 candidate 失败而失效；被替代后标记 `SUPERSEDED`，仍不可变可查。

### 4. 分区键（partition_key）

| 数据类型 | 分区键 | 说明 |
|---|---|---|
| `daily_bars` | `trading_day` | 一日一个分区（增量按交易日补齐）；REPAIR 修复可按 `(trading_day, code)` 细分，`partition_key` 文本约定 `YYYY-MM-DD` 或 `YYYY-MM-DD:code` |
| `stocks` | `as_of` | 快照日一个分区 |
| `fundamentals` | `published_on` | PIT 发布日一个分区（`published_on > T` 不得进入 T 日 PIT 可见集合） |
| `dividends` | `ex_date` | 除权日一个分区 |

### 5. schema 版本机制

- 新增 `PRAGMA user_version` 作为数据库 schema 版本号（当前为 0）。P5-RD-1 起每步迁移在事务内执行并递增 `user_version`。
- `seed_imports.schema_version` 记录种子 schema 版本；种子外部 manifest 中的 `schema_version` 必须与 `PRAGMA user_version` 语义一致，禁止把计划示例值（3）当作现状。
- 启动时校验 `user_version`：过低则执行增量迁移；过高或未知则拒绝打开并给出明确错误，禁止静默降级。

## 备选方案与否决理由

| 方案 | 结论 | 理由 |
|---|---|---|
| 影子 v3 表（B） | 否决为主方案 | 磁盘峰值约 2 倍（~1.7 GB）、复制耗时与回填成本更高；仅在回滚备份时作为兜底 |
| 纯 manifest 无列绑定（C） | 否决 | 无法满足「generation 绑定实际行」；只读侧无法证明读到的行属于哪个批次 |
| 每次 generation 复制整库 | 否决 | 八年全量 ~1.7 GB 复制成本不可接受（计划 §6.1） |
| 无 schema 版本（维持现状） | 否决 | 无法区分旧库/半成品/已迁移库，种子与跨平台校验无基准 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| `daily_bars` 5.16M 行回填 UPDATE 约 24 s | 一次性成本，事务内分批执行并显示进度；回填前先备份 |
| `batch_id` 索引增大写放大 | 仅 `daily_bars` 需要该索引；写入为追加式新批次，索引局部性好 |
| 旧行 `batch_id IS NULL` 被误读 | Reader 只读 active generation 分区清单指向的批次；NULL 行不可见 |
| `INSERT OR REPLACE` 覆盖已发布批次 | 写入契约禁止；StagingWriter 只写新批次，测试覆盖重复写入场景 |
| 迁移中断产生半状态 | 全部迁移步骤幂等 + 事务内执行 + `user_version` 记录；重启续跑 |
| 发布事务过长锁库 | 发布只写 manifest/指针（短事务）；数据写入在 staging 批事务中完成 |
| 回填期间新写入竞争 | 迁移在启动早期完成；迁移与同步互斥（进程锁 + 持久化锁） |

## 回滚策略

1. 迁移前创建可恢复备份（`market.sqlite3.bak` 或 manifest + 校验和记录），并生成迁移前完整性报告（`storage/integrity.py`）。
2. 任一迁移步骤失败：事务回滚，`user_version` 不变；重启后幂等重试。
3. 需要回滚到旧架构：恢复备份文件，删除新增书签表（不删数据表），`user_version` 归零；旧 `DataSyncService` 路径保持可用（`backfill_runs_v2` 等旧表不删除）。
4. 回滚不删除用户数据；旧 coverage/generation 记录保留用于核对。
5. 灰度切换（计划 §11.2）：新 Planner/Verifier 先 shadow mode 只读旧库生成报告，不改变读取路径；确认无回归后再切换默认入口。

## 关联决策记录

- `development/plan/P5_RECONSTRUCT_DATASYNC.md`：第 6 节数据库存储与 generation 发布模型、第 7 节 CoverageVerifier、第 8 节 ReadinessGate、第 10 节跨平台迁移。
- `development/architecture/ADR_P5A_SEED_DISTRIBUTION.md`：种子 SHA-256 外部 manifest、qfq 统一口径与除权对策 a。
- `development/architecture/ADR_OVERVIEW.md`：本地优先、防重复、95% 完整性红线。
- `development/implementation/P1_5_LOCAL_SYNC_STORAGE.md`：现有同步存储与完整性语义。

## 结果（实现状态）

- 2026-09-01 完成真实库基准（见上），三种迁移方案成本对比与分区键已固化。
- P5-RD-1 起实现：`storage/migrations.py` 版本化幂等迁移（v0→v1），`SQLiteRepository` 启动时自动迁移并在真实库副本上验证通过（0→1 幂等、不触碰线上库）；新增 `DataSyncAdminRepositoryProtocol` 与 8 张书签表、4 张 staging 镜像表的 CRUD。
- 发布采用「事务内 staging→正式表 publish-copy + manifest + active 指针切换」模型：单日分区实测复制约 0.056 s，短事务成立；旧 generation 在构建期间通过 staging 隔离保持可读。
- 版本 1.12.0（MINOR 递增），全量离线测试 544 passed（新增 151 项，覆盖 P5-RD-1..10）。
- 第 17 章第 8 条（Mac→Windows 物理迁移人工验收）按用户 2026-09-01 确认不纳入完成定义，Windows 端实机验收由用户另行执行。
