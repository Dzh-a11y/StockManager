---
date: 2026-09-02
purpose: 合并规划 StockManager 的数据源可替换性与数据类型扩展能力，并明确该计划延期且不进入当前工作序列。
project: StockManager
status: deferred
---

# PE_DATAEXTENSION：数据源与数据类型扩展计划

## 1. 状态与执行边界

本计划统一承载两项未来工作：

1. **数据源可替换性**：解除正式运行入口、同步计划和 Provider 身份对 Baostock 的硬绑定，使未来数据源可通过明确适配与配置接入。
2. **数据类型扩展性**：把新增分红、公司行动、财务报表等数据类型所需的规划、抓取、标准化、staging、验证、发布和读取契约收敛为可注册的完整纵向切片。

本计划当前状态为 **deferred**，不在当前工作序列中，不占用 P5-A 回测验收或 P5-B CAPM 的开发顺序。当前工作顺序保持：

```text
P5-A 回测验收 → P5-B CAPM 开发与验收
```

`PE_DATAEXTENSION` 没有默认启动日期。只有在上述工作完成后，且用户再次明确授权启动时，才能进入架构决策、实现或真实数据源接入阶段。

本文件只记录未来计划，不授权当前执行以下操作：

- 不修改正在运行的 DataSync 流水线、数据库 schema、Provider 或配置。
- 不启动、停止或重试任何 Baostock 同步任务。
- 不切换数据源，不选择第二数据源，不下载新增数据类型。
- 不修改 `AGENTS.md` 或现有 skills；这些属于本计划激活后的治理任务。
- 不把占位表、Protocol 声明或测试 fake 视为已经支持的数据源/数据类型。

## 2. 立项原因

当前 P5 DataSync 已形成 Planner、SerialFetchWorker、StagingWriter、CoverageVerifier、GenerationCommitter 和 ReadinessGate 主链路，但扩展边界仍不完整。

### 2.1 数据源替换现状

- `DataSyncService` 可以接收 Provider 对象，测试也能注入 fixture；这是可替换性的基础，但不是完整的运行时替换能力。
- Web、CLI 和独立 runner 的正式入口仍直接构造 `BaostockProvider`。
- 在线同步计划把来源固化为 `SyncSource.BAOSTOCK`；来源类别与具体 Provider 身份没有分离。
- 同步配置没有 Provider registry、启用列表、能力声明或逐数据集路由。
- 一个同步服务实例只绑定一个 Provider，不能明确表达不同数据集使用不同来源。
- 股票代码、复权映射、登录、分页、限速和错误码等 Provider 特定语义尚未全部限制在适配器边界。

### 2.2 数据类型扩展现状

- `dividends` 已存在 Domain、SQLite、staging、Verifier 和 Committer 结构，但正式 `ProviderProtocol` 与 `BaostockProvider` 没有完成对应抓取能力。
- `SerialFetchWorker` 已包含 `fetch_dividends` 分支，形成“下游已接线、正式 Provider 未实现”的半成品状态。
- 默认 Bootstrap 仅同步 `stocks`、`daily_bars`、`fundamentals`，本地 `dividends` 仍为空。
- Planner、Worker、Staging、Verifier、Committer 内存在分散的 `data_type` 条件分支；新增数据类型需要同时修改多个模块。
- 当前 `DividendRecord` 不能完整表达公告日与生效日等 PIT 语义，不足以直接支持无前视偏差的历史研究。

### 2.3 数据缺口输入需求：退市股与历史每日股票池（2026-09-02）

回测验收排查（见 P5 实现手册 §12.7 与 §11.4）发现的历史数据缺口，作为本计划的**输入需求**记录；本计划维持 `deferred`，不因本条目提前启动，也不在当前主链路散落实现。

- **现状与成因**：`fetch_stocks` 用 `query_all_stock(day=最新交易日)` 建池，股票池 =「最新一天在市」的 5,214 只；本地 8 年日线仅覆盖该池（`stocks` 无退市记录、`daily_bars` 无池外代码）。2025 年及更早退市的股票从未进池、从未下载 → 历史回测/筛选永远够不到它们。
- **影响**：以当前池回测存在**幸存者偏差**（退市股退市前的较差表现被系统性排除，收益偏乐观）；`non_st`（`UNIVERSE_STATE_PIT_READY`）等规则的历史回测只能给出「当前可投池」近似；`pe_positive` 同受 fundamentals 单快照限制（P5 §11.4）。本缺口不因 §12.7 的股票池向后重建（按 `listed_on` 恢复当前池的存在性）而消失——向后重建只能让「至今仍在市的股票」在历史上正确出现。
- **数据源可行性**：Baostock `query_all_stock(day)` 支持任意历史日（返回该日在市的股票，含后来退市者）；`query_stock_basic` 返回全 A 股（含已退市，带 `ipoDate`/`outDate`）；退市代码在其上市窗口内可返回日线。即消除幸存者偏差在数据源层面**可行**。
- **激活后方案要点（拟 PE-9，见第 5 节）**：①按窗口内交易日（或分段）用 `query_all_stock(day)` 构建历史每日股票池，每日快照入 stocks（`as_of=day`）；②为曾上市但已退市的代码补拉其在市窗口的 bar 及可获得的 ST/基本面；③`is_st` 等 PIT 字段取自对应日快照，不再用最近快照近似，并保留 §11.4 的边界诚实声明；④退市股 bar 与池纳入 coverage、generation、Readiness 与 provenance。
- **未决项（激活后先验证再冻结契约）**：历史退市股逐日 ST 序列、基本面历史与复权数据的实际可获得性与一致性，以及数据量（约每交易日 5,000+ 行 × 窗口天数）对抓取与存储规模的影响。

因此，未来目标不是“默认下载某个 Provider 的所有接口”，而是：

> 系统可以完整接入经过批准的数据源和数据类型，用户按研究需求选择实际同步范围；任何能力必须完成端到端契约后才能声明为 supported。

## 3. 目标架构

### 3.1 分离来源类别与 Provider 身份

同步计划不得再用 `BAOSTOCK` 同时表达“在线来源”和“具体供应方”。目标契约至少包含：

- `origin_kind`：`ONLINE`、`SEED`、`LEGACY`。
- `provider_id`：稳定的 Provider 标识，例如 `baostock`。
- `provider_adapter_version`：适配器契约版本。
- `capability_digest`：本次计划实际使用的能力集合摘要。
- `dataset_routes`：每个数据类型选用的 Provider。

上述字段必须进入 plan fingerprint、task、ingest batch provenance、coverage evidence 和 generation manifest。来源变化必须生成不同计划身份。

### 3.2 Provider Registry 与 Factory

正式入口统一通过 Provider Registry 创建适配器，Web、CLI、runner 不得直接实例化具体 Provider。Registry 负责：

- 解析启用的 `provider_id`。
- 创建 Provider adapter。
- 返回显式 capability 声明。
- 校验配置版本与必需参数。
- 禁止未知 Provider 静默回退到 Baostock。

认证信息不得写入计划、日志、数据库 manifest 或 Git 配置；具体密钥载入方式必须在接入真实第二数据源时另立安全决策。

### 3.3 可组合 Provider 能力

Provider 不应被迫一次实现所有数据类型。目标能力可按职责拆分，例如：

- `TradingCalendarCapability`
- `SecurityMasterCapability`
- `DailyBarCapability`
- `FundamentalCapability`
- `CorporateActionCapability`

Planner 在落计划前求取“数据需求与 Provider 能力”的交集。缺少能力时返回结构化拒绝原因，禁止等到 Worker 执行时才因方法不存在而失败。

### 3.4 Dataset Registry 与完整纵向切片

每个支持的数据类型必须由统一 Dataset Definition 声明：

- 数据类型 ID 与 schema 版本。
- Domain 模型与标准化字段。
- 时间语义、PIT 字段和分区键。
- 复权或公司行动语义。
- Provider capability 要求。
- 规划与抓取策略。
- staging 与正式存储映射。
- CoverageVerifier 规则。
- Generation 发布与 Readiness 要求。
- seed、迁移、保留和清理策略。

只有以下链路全部实现并通过契约测试，数据类型才可标记为 `SUPPORTED`：

```text
Plan → Fetch → Normalize → Stage → Verify → Commit → Readiness → Local Read
```

只有表结构或占位接口的能力必须标记为 `SCAFFOLDED`；未接入的能力标记为 `UNAVAILABLE`。

### 3.5 逐数据集路由与禁止隐式混源

未来允许不同数据集使用不同 Provider，但选择必须显式、确定且可追溯。例如日线与分红可以来自不同适配器，但不得在同一逻辑分区内静默拼接来源。

如未来确需 fallback、交叉校验或多源合并，必须另立 ADR 定义：

- 主来源与候补来源。
- 冲突解决规则。
- 字段优先级与时间口径。
- 去重键和一致性阈值。
- generation provenance 表达方式。

在该 ADR 被接受前，默认策略为每个数据集/逻辑分区只使用一个明确 Provider。

## 4. 分红与公司行动首个扩展示例

分红是 Dataset Registry 的首个候选验收数据类型，但不在当前阶段执行。正式接入前至少需要决定：

- 公告日、登记日、除权日、派息日等时间字段的标准模型。
- 现金分红、送股、转增等事件类型的表达方式。
- 数据在研究日期 `T` 的 PIT 可见条件。
- qfq/hfq 与公司行动发生后的重拉、失效和 REPAIR 规则。
- “某日没有分红事件”与“数据源缺失”的可验证区分。
- Provider 修订历史数据时的 revision 和 generation 处理方式。

在这些语义冻结前，不得仅凭 `ex_date` 和现金金额将分红用于正式历史回测。

## 5. 计划任务包（激活后执行）

### PE-0：重新基准与激活门禁

- 确认 P5-A 回测验收与 P5-B CAPM 已完成。
- 重新读取届时代码、数据库 schema、当前 Provider 与文档事实。
- 确认不存在运行中的 DataSync runner，生成数据库备份与完整性报告。
- 由用户明确批准本计划从 `deferred` 改为 `active`。

### PE-1：治理规则与 ADR

- 更新 `AGENTS.md`：新增 Provider 替换、Dataset 完整纵向切片、PIT 和多源 provenance 红线。
- 创建 Provider/Dataset Registry ADR。
- 明确启动 Web 是否允许自动联网，以及真实 Provider 操作的授权边界。
- 更新或创建 DataSync 操作与数据接入 skills。

### PE-2：来源身份和配置契约

- 分离 `origin_kind` 与 `provider_id`。
- 定义版本化 Provider 配置与 dataset routing。
- 将 Provider identity/capability digest 纳入计划与 manifest。
- 提供旧 Baostock 计划和数据库记录的兼容迁移。

### PE-3：Provider Registry 与能力协议

- 正式入口全部改由 Registry/Factory 创建 Provider。
- 引入可组合 capability protocols。
- 建立统一 Provider 合约测试套件。
- 保持 Baostock 行为不变，通过兼容性测试证明零业务回归。

### PE-4：Dataset Registry

- 收敛 Planner、Worker、Staging、Verifier、Committer 的数据类型分派。
- 将现有 stocks、daily bars、fundamentals 注册为完整数据集定义。
- 对半接线或未实现的数据类型给出明确能力状态。
- 保持 P5 generation 与 ReadinessGate 发布语义不变。

### PE-5：分红/公司行动纵向切片

- 冻结 PIT Domain 模型和分区语义。
- 实现 Baostock 对应能力适配。
- 完成 staging、coverage、generation、repair 和读取闭环。
- 使用离线 fixtures 覆盖空事件、修订、重复、缺失和时间边界。

### PE-6：第二 Provider 离线替换验收

- 先实现确定性 fixture Provider，不立即选择商业或联网数据源。
- 验证配置切换后 Web、CLI、runner 和 Pipeline 均不再构造 Baostock。
- 验证 capability 缺失在规划阶段失败。
- 验证不同 Provider 产生不同 plan fingerprint 与完整 provenance。

### PE-7：真实第二数据源（可选）

- 只有用户明确选定数据源并批准其许可、成本和联网范围后才执行。
- Provider 特定 SDK、认证、限流、错误码和字段映射全部限制在 adapter。
- 禁止为了接入新来源修改 Rules、Backtest 或 CAPM 业务层。

### PE-8：迁移、验收与文档

- 完成旧配置、旧计划和旧 generation 的兼容测试。
- 执行断网测试、故障注入、锁、恢复、seed 和跨平台迁移测试。
- 同步架构、实现与用户手册。
- 未完成真实 generation 发布与 Readiness 验收时不得标记计划完成。

### PE-9：退市股与历史每日股票池（输入需求，激活后评估）

承接第 2.3 节数据缺口输入需求；激活后先做**可行性验证**再冻结契约，不与 PE-5/PE-6 抢占顺序：

- 可行性验证：Baostock 对历史每日股票池（`query_all_stock(day)` 任意日）、已退市代码的 bar/ST/基本面历史、复权一致性的实际返回质量与数据量。
- 历史每日池：按研究窗口内交易日建 `as_of=day` 的 stocks 快照；明确与「当前池 + listed_on 向后重建」两条路径的读取契约关系。
- 退市股数据切片：补拉其在市窗口的日线与可获得的 PIT 字段，纳入 staging、coverage、generation 与 Readiness。
- PIT 语义：`is_st` 等取自对应日快照，标注快照近似边界；无偏八年历史回测的声明必须基于真实历史池验收后再给出。
- 规模与成本：评估窗口内每日池与退市股 bar 的抓取时间、存储增量与限速预算；若规模不可接受，给出分段/按需降级方案。

## 6. 激活后的验收标准

1. Web、CLI 和 runner 不直接 import 或构造具体 Provider。
2. 切换 Provider 只修改受控配置和适配器注册，不修改 Rules、Backtest、CAPM 或 Web 业务逻辑。
3. Provider 能力不足在 Planner 阶段明确拒绝，不产生网络请求。
4. 新数据类型必须完成 Plan 到 Local Read 的完整纵向切片。
5. 所有 batch、coverage 和 generation 可追溯 `provider_id`、适配器版本和数据 schema。
6. 不同 Provider 或路由配置生成不同 plan fingerprint。
7. 多来源不得静默混合；任何合并行为均有已接受 ADR 和测试证据。
8. 分红等 PIT 数据不得把未来公告或修订泄漏到历史研究日。
9. Baostock 现有日线同步、断点续传和 generation 发布行为无回归。
10. 全部单元与契约测试离线运行；真实联网测试必须由用户明确批准。

## 7. 与当前工作序列的关系

`PE_DATAEXTENSION` 使用 `PE` 前缀，表示独立的扩展计划，不是 P5-A、P5-B 或 P6 的子任务编号，也不作为它们的前置依赖。

当前只保留本计划文档作为未来入口。回测验收和 P5-B CAPM 期间发现的数据缺口，应记录为输入需求，但不得借此提前启动本计划或在当前主链路中散落实现扩展接口。

## 8. 免责声明

StockManager 的数据、筛选、回测与 CAPM 结果仅供研究参考，不构成任何投资建议。本计划不包含自动交易、实盘下单或券商连接能力。
