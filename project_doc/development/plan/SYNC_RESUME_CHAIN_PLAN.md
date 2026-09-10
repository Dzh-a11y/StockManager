---
date: 2026-09-05
purpose: 定义 P5 pipeline 增量/回补同步“在途窗口冻结 + 缺口追平”链式断点续传（方案 A）的需求、改动、边界与验收标准。
project: StockManager
status: active
---

# SYNC_RESUME_CHAIN_PLAN：增量同步在途窗口冻结与追平（方案 A）

## 0. 背景与问题（用户描述，2026-09-05 核对代码确认）

用户反馈：增量同步中途网络掉线时，数据不会进入数据库（正式可见表）；重新同步时会再次“从 0 开始”——即以 20 只为一批次，中断那一批次的进度“没有地方保存”。

以当前 Web「增量同步」入口（按钮 → `code/scripts/run_backfill_v2.py` → `DataSyncService.startup_sync(..., force_pipeline=True)`）为基准核对代码后，现状精确表述为：

1. **已完成批次有持久化**：P5 pipeline 中一个任务（20 只 × 区间）成功后，staging 行 + `ingest_batches` 摘要 + 任务 SUCCESS 在同一事务落库（`code/src/stock_manager/sync/staging.py::write_batch`）。同窗口重试（当天、冷却过后）会续传：`run_backfill_v2.py` 与 `sync/pipeline.py::recover_interrupted/retry` 只重跑 PENDING/INTERRUPTED/FAILED，SUCCESS 任务零重拉。
2. **但批次有效性绑定在 plan/candidate 上**：`plan_id` 是目标窗口的确定性指纹（`sync/pipeline.py::_planner_fingerprint_plan_id`、`sync/planner.py::PlanInput`），`candidate_id` 每次新建，`batch_id` 由 `candidate_generation_id + data_type + partition_key + codes + range` 生成（`sync/staging.py::batch_id`）。未发布前数据只在 staging，正式表与 active generation 指针不动。
3. **“从 0 重拉”的真因是窗口前移而非缺少断点**：中断当天未发布成功 → coverage 指针不变；下次启动时 `startup_sync`（`code/src/stock_manager/sync/data_sync_service.py:1489`）**不检查在途未完成计划**，直接按“当前 coverage_end → 最新已完成交易日”重新开窗。`target_end` 一前移，`plan_id` 即变化 → 生成全新 plan/candidate → 旧 staging 批次全部作废、整窗重拉。缺口多日合并进一个计划（`sync/planner.py::_window_tasks`：daily_bars 每批 20 只 × 整个缺口区间）时浪费最大。
4. **体感放大点**：失败任务带 300s 冷却（`code/config/sync.json` `retry_cooldown_seconds`），冷却内重试直接报错；数据已是最新时 `startup_sync` 静默返回 `None`，`run_backfill_v2.py` 会把它误判为失败（`successful = published or plan_status == SUCCEEDED` 恒为 False）。

参考数据（capm 数据集）的入口 `sync_capm_reference_data`（`data_sync_service.py:175-228`）**已实现方案 A 语义**：先续传未完成计划，发布成功后再规划追平到请求终点，并有跨日恢复测试 `tests/test_capm_sync_pipeline.py::test_resuming_yesterday_also_catches_up_to_requested_new_target`。市场数据入口缺的正是这一层。

## 1. 已确认决策（2026-09-05，用户逐项确认）

| 编号 | 决策 | 内容 |
|---|---|---|
| D1 | 实施范围 | 只改 **market 数据集 `startup_sync` 的 pipeline 入口**（Web 增量/在线 Bootstrap、`run_backfill_v2.py`、watchdog、CLI pipeline 分支共用）。capm 数据集已有链式实现，不动；legacy v2 fallback（`pipeline_default=False` 的旧路径 `backfill_on_startup_v2`）本次不改 |
| D2 | 链上失败语义 | **失败即停**，保留 FAILED 状态，显式重试 / 下次启动续传；不得跳过失败缺口（符合 AGENTS.md 第 11.4 节“失败不记成功/失败日回补刷新/显式重试+冷却”） |
| D3 | 陈旧失败计划 | 在途计划窗口若**已被更宽的已发布数据完全覆盖**（`target_end <= 当前 coverage_end`），判定为已被取代，**跳过不续**，正常规划追平（避免陈旧计划整窗重拉并发布重复 superseding generation） |
| D4 | 顺手修复 | 数据已是最新时，`startup_sync` 返回携带“数据已存在，跳过拉取”的可辨识结果（而非静默 `None`），`run_backfill_v2.py` 判定为成功并提示；watchdog 冷却对齐另议（不做入本期） |
| D5 | 下一步 | 定稿计划并入 Vault → 开短功能分支实现 + 离线测试；真实网络验证由用户在本机执行 |

## 2. 目标与典型场景

**目标**：市场数据 P5 pipeline 同步在中断/掉线后，已完成批次的进度不因目标窗口前移而作废；重开先完成在途计划的原窗口（窗口冻结），发布成功后再补齐到最新已完成交易日（追平）。

典型流程：

1. coverage 到 09-01；用户在 09-02 点增量，规划窗口（09-01..09-02），抓到第 300/523 批掉线 → 计划 FAILED、当天未发布。
2. 用户 09-04 才再点增量（目标 = 09-04）：
   - **现状**：按（09-01..09-04）重开窗 → 旧窗已抓批次作废 → 全窗重拉。
   - **改后**：先续（09-01..09-02）在途计划（SUCCESS 批次零重拉）→ 发布成功 → 再规划并执行（09-02..09-04）追平 → 最终 coverage 到 09-04。

## 3. 范围

### 本次必做

- `startup_sync` pipeline 分支改为链式：续在途 →（成功后）追平尾差；窗口冻结。
- 已最新场景：返回带 warning 的跳过结果；`run_backfill_v2.py` 相应判定成功。
- 离线测试与全量回归；版本号 MINOR 递增；文档同步。

### 明确不做（可延期）

- legacy v2 fallback（`backfill_on_startup_v2` / `backfill_history_v2`）的跨 run 窗口复用。
- capm 数据集实现改动（其入口已具备链式语义）。
- 跨 plan/candidate 的批次签名全局复用（原“方案 C”），需要另立 ADR 与数据契约变更。
- 批内逐码粒度断点（一次上游请求覆盖整批 20 只，批内位置不是可靠续传单元；拆小会放大请求数、违反速率约束）。
- watchdog 重启间隔与冷却对齐、Web 冷却倒计时文案（原“方案 D”其余项）。
- 修改 staging/committer/verifier/planner/pipeline.execute 的内部语义与表结构。

## 4. 现状机制与根因（代码依据）

见第 0 节 1～4。相关位置：

- 规划/执行入口：`code/src/stock_manager/sync/data_sync_service.py::startup_sync`（1489-1610）、`sync_capm_reference_data`（175-228，样板）。
- 恢复原语：`code/src/stock_manager/sync/pipeline.py::recover_interrupted`（366）、`retry`（263，含冷却）、`execute`（177）。
- 计划指纹/批次归属：`sync/pipeline.py::_planner_fingerprint_plan_id`（437）、`sync/staging.py::batch_id`（250）、`sync/planner.py::PlanInput.fingerprint`。
- 窗口任务粒度：`sync/planner.py::_window_tasks`（340）。
- 计划列表/覆盖率：`code/src/stock_manager/storage/sqlite_repo.py::list_sync_plans`（1745，按 `created_at DESC`）、`actual_coverage`（1152，读已发布共享表 MIN/MAX）。
- 状态枚举：`code/src/stock_manager/domain.py`：`SyncPlanMode{BOOTSTRAP, INCREMENTAL, REPAIR, LEGACY_IMPORT}`（545）、`SyncPlanStatus{PLANNED, RUNNING, SUCCEEDED, FAILED}`（562）、`CandidateGenerationStatus`（582，含 PUBLISHED/NEEDS_REPAIR/VERIFICATION_FAILED/FAILED）。

## 5. 改动设计

### 5.1 `DataSyncService.startup_sync`（pipeline 分支，镜像 capm 入口）

执行顺序调整为：

1. **续在途计划（窗口冻结）**：
   - 取 `plans = repository.list_sync_plans(dataset_id, adjustment)`（新→旧）。
   - 选第一个满足全部条件的计划为 `pending`：
     - `status is not SyncPlanStatus.SUCCEEDED`；
     - `mode in (SyncPlanMode.BOOTSTRAP, SyncPlanMode.INCREMENTAL)`（REPAIR/LEGACY_IMPORT 不自动续，D3 附带规则）；
     - 无“已发布窗口完全覆盖该计划”（`coverage_end is None` 或 `pending.target_end > coverage_end`；覆盖即跳过，D3）。
   - `pending is None` → 跳到第 2 步。
   - 否则在 provider 锁内：
     - `pipeline.recover_interrupted(pending.plan_id)`（清理上一实例残留 RUNNING）；
     - `pending.status is FAILED` → `retry_failed=False` 抛 `RetryRequiredError`（显式重试语义）；`True` → `pipeline.retry(pending.plan_id)`（冷却约束内置，冷却未到抛 `RetryCooldownError`）；
     - 否则 `pipeline.execute(pending.plan_id)`；
   - 结果未发布（`published=False` 且计划未 SUCCEEDED）→ **立即返回该 PipelineRun**（D2：失败即停，不规划追平，不吞异常；下次启动该计划仍在 → 再次被续）。
   - 发布成功 → 刷新 plans/coverage 后继续第 2 步追平。

2. **追平尾差**：
   - `coverage_end = repository.actual_coverage(adjustment, "daily_bars")[1]`；`target = 最新已完成交易日`。
   - `coverage_end >= target` → 返回“已最新”跳过结果（见 5.2），零 Provider 调用。
   - 无 active generation → 保留现状：股票池前置（缺失时在 provider 锁内 `fetch_stocks(target)` 并落库）→ `pipeline.plan(BOOTSTRAP, 八年窗口 … target)` → provider 锁内 `execute`。
   - 有 active generation → `pipeline.plan(INCREMENTAL, coverage_end..target, ...)` → provider 锁内 `execute`（一次；失败即返回，下次启动时它成为在途计划被续）。

单次启动最多执行两段（续在途 + 一段追平）；追平段再失败留给显式重试 / 下次启动，配合 watchdog 循环收敛。

### 5.2 已最新跳过结果与 runner 判定（D4）

- `startup_sync`：优先取“最新 SUCCEEDED 计划或其 candidate 已 PUBLISHED 的计划”，执行 `pipeline.execute(plan_id)` 得到既有跳过/修复语义（`pipeline.py` 对 SUCCEEDED 计划返回 “plan already succeeded; skipping”；对 candidate 已 PUBLISHED 的计划修复计划状态）；不存在任何可用计划时返回 `None`。
- `run_backfill_v2.py`：`run is None` 或结果为“已存在/已成功跳过”时打印提示、将 `sync_runners` 置 `SUCCEEDED` 并返回 0，不再误报 FAILED；同时该分支不发起任何 Provider 调用。
- 服务层测试以“连续第三次对同一目标启动 → Provider 零调用 + 跳过结果”为验收。

### 5.3 边界与异常处理规则

- E1 同窗中断当天重试：与现状一致（plan() 幂等 + retry 冷却），行为回归既有测试。
- E2 中断后跨日重开（核心修复）：旧窗在途计划被续并发布，再追平；旧窗 SUCCESS 批次零重拉，仅在途批次重拉，新日只新增。
- E3 陈旧失败计划（窗口已被更宽已发布覆盖）：跳过不续（D3）。
- E4 追平段中途失败：返回未发布 PipelineRun，计划/任务 FAILED 状态明确，下次启动继续。
- E5 已是最新重入：跳过提示 + 零 Provider 调用 + runner 判成功（D4）。
- E6 mode 过滤：REPAIR/LEGACY_IMPORT 计划不进入自动续传链。
- E7 首启 BOOTSTRAP 中断跨日重开：续旧 BOOTSTRAP 窗口发布后，再 INCREMENTAL 追平新缺口。
- E8 冷却约束：FAILED 在途计划在冷却内续传抛 `RetryCooldownError`，不静默自动重试。
- 红线不变：幂等（成功即锁定）、失败不记成功、显式重试 + 冷却、原子发布、防重复拉取、串行受控速率、Provider 单一入口。

## 6. 架构与实施落点

| 文件 | 改动 |
|---|---|
| `code/src/stock_manager/sync/data_sync_service.py` | `startup_sync` pipeline 分支重写为链式；新增私有 helper（如 `_resume_pending_plan`）承载“选 pending + recover + retry/execute”；已最新分支返回跳过结果 |
| `code/scripts/run_backfill_v2.py` | `startup_sync` 返回 `None`/跳过结果时判成功并提示 |
| `code/tests/` | 新增/扩展：建议 `tests/test_p5_facade.py`（跨日续传追平、陈旧跳过、已最新零调用）或新建 `tests/test_sync_resume_chain.py`；回归既有 sync 测试 |
| 版本号 | `1.16.0 → 1.17.0`（MINOR，同步能力增强，向后兼容），同步 `code/pyproject.toml`、`code/src/stock_manager/__init__.py`、`code/tests/test_package_structure.py` |
| 文档 | 本文档 + `project_doc/README.md` 索引；实现验收记录按项目规定后续补记 |

不新增表结构、不改 staging/committer/planner/verifier/`pipeline.execute` 内部语义；capm 入口与 legacy v2 路径不动。

## 7. 测试与验收

### 7.1 新增离线测试（FixtureProvider/计数 provider，禁止联网）

- T1 跨日续传 + 追平（E2）：首次在缺口窗口中途失败（provider 第 k 次调用抛异常）→ clock 前移一天（模拟新已完成交易日）→ 再次 `startup_sync`：断言先出现旧窗 SUCCESS/SUCCEEDED 计划、再出现追平计划且 `target_end == 新目标`、最终 coverage 到新目标；Provider 调用集精确断言旧窗已 SUCCESS 的 `(codes, range)` 组合零重拉，仅新增新日请求。
- T2 陈旧失败计划跳过（E3）：手工构造窗口已被覆盖的 FAILED 计划 → `startup_sync` 不续它、正常规划追平、无整窗重拉。
- T3 已最新零调用（E5/D4）：同一目标连续第三次启动 → Provider 调用数为 0，返回跳过结果（warning 含“数据已存在/跳过”语义），runner 判定逻辑（抽成可测函数或脚本级断言）为成功。
- T4 失败即停（E2/D2）：追平段失败 → 返回未发布 PipelineRun，计划 FAILED，不规划下一段。
- T5 冷却（E8）：FAILED 在途计划在 `not_before` 前续传抛 `RetryCooldownError`；clock 越过冷却后成功。
- T6 mode 过滤（E6）：REPAIR/LEGACY_IMPORT 陈旧计划不被链续传。
- T7 首启 BOOTSTRAP 跨日（E7）：无 active generation 场景中断后再开，先续旧 BOOTSTRAP 再追平。

### 7.2 回归与验收

- `test_p5_facade.py` 现有 6 例、`test_p5_pipeline.py`、`test_p5_rd10_e2e.py`、`test_capm_sync_pipeline.py`、`test_p5_cli_pipeline.py`、`test_web_api.py` 等全量离线 `pytest` 无回归。
- 前端无改动（不改 JS）；如 runner 判定逻辑有测试覆盖按 T3 处理。
- 真实网络验证由用户本机执行（Baostock 需联网，Agent 不代跑）：断网点增量、观察续传与追平、次日验证零重拉与最终覆盖；验证记录后续补入实现手册。

## 8. 决策与待决项

- 已确认：D1～D5（见第 1 节）。
- 待用户后续决定（不阻塞本期）：方案 C（跨 plan 批次签名复用）、watchdog 冷却对齐、Web 冷却倒计时文案。
- 风险提示：陈旧 FAILED 计划不做清理（跳过不删），属“留档不续”；若用户希望自动归档历史失败计划，需另行决策。
