---
date: 2026-09-07
purpose: StockManager 项目文档中心索引：提供当前架构、API、数据计算能力入口，并区分历史阶段文档。
project: StockManager
status: active
code_version: 1.17.2
verified_on: 2026-09-06
---

# StockManager 项目文档中心

## 当前源码阅读入口（1.17.2，2026-09-06 核验）

本次按源码提交 `9322845` 整理以下入口；版本适用范围不代表远端发布或全量测试验收。读文档先核对 `date`、适用版本和正文历史时点，详见 [AGENTS 日期与版本规定](AGENTS.md#15-项目文档的日期与版本核对)。

| 需要的信息 | 当前维护入口 | 内容 |
| --- | --- | --- |
| 项目与启动 | [根 README](../README.md)、[使用手册](usage/README.md) | 功能概览、目录、平台启动、页面操作 |
| 当前架构 | [CURRENT_ARCHITECTURE.md](development/architecture/CURRENT_ARCHITECTURE.md) | 模块职责、四条调用链、数据边界、源码/测试导航及差异清单 |
| 可用 API | [P3_WEB_API.md](development/implementation/P3_WEB_API.md) | 完整 HTTP 路由、参数/响应、分页、副作用、15 个 CLI 命令和示例 |
| 可计算的数据 | [DATA_CAPABILITIES.md](usage/DATA_CAPABILITIES.md) | 已存字段、12 条规则及默认参数、13 个政策、CAPM、回测指标和未实现项 |
| 本轮整理与验证 | [维护计划第 3 节](development/plan/PB_DOCANDMAINTANENCE.md#3-当前文档整理2026-09-06) | 已授权范围、交付、实际检查与后续待统一项 |

这些入口描述当前实现；下方计划、ADR 和阶段验收保留各自历史时点。特别是旧文件名中含 `current` 的架构图和旧手册的“当前/已通过”，不能单凭名称或修改日期视为1.17.2的实现及验收证据。本轮未重绘旧架构图，也未批量重写所有历史计划。

本目录（`project_doc`）是 StockManager 的 Obsidian Vault 与默认项目知识源，存放项目资料、设计、需求、决策和开发记录。文档按「开发手册」与「使用手册」两类组织，目录名均为英文：

```text
project_doc/
├── README.md                # 本文档（索引）
├── AGENTS.md                # 项目开发规定（与仓库根 AGENTS.md 同步）
├── Issue/                   # Alpha 研究（stock-alpha-research 输入端）指标/能力缺口登记
├── development/             # 开发手册
│   ├── plan/                #   计划手册：阶段计划、迁移计划、删除清单
│   ├── architecture/        #   架构手册与架构图（同属一个文件夹）
│   └── implementation/      #   实现手册：各阶段设计、验收与接口明细
└── usage/                   # 使用文档：安装、启动、规则、操作与研究建模说明
```

## 开始讨论或开发前

先阅读本索引，再按任务查阅相关计划、ADR 和使用手册。协作遵循 [AGENTS.md](AGENTS.md) 第 7 节：用户提出需求，Agent 按主题提出大量具体问题并持续追问，不设每轮问题数量上限，逐步形成尽量精确、可执行、可验收的项目计划（plan），确认后实施；职责以当前提示词为准，不绑定模型。已确认授权的工作和范围明确的小修改直接执行。

项目功能与启动说明见 [使用手册](usage/README.md)；计划中的目标与已验收的功能应分别记录。

代码目录为项目根下的 `code/`，Git 根目录仍为 `StockManager/`。2026-09-02 完成目录改名；历史架构图绑定旧 Git 提交，其证据路径保留当时目录名，不作为当前启动或配置路径。

## 当前工作方向（2026-09-03 用户确认）

项目总体架构建设在当前范围内收口，现有模块范围作为后续维护基线；接下来聚焦模块内部打磨和 UI 小幅修整。默认不再扩展模块数量或启动新一轮总体架构建设。

用户进一步提出，项目文档的修订和管理是本阶段重点方向，服务于人和各类模型对代码的理解与维护。文档工作应围绕当前事实、模块导航、代码与测试依据及持续更新展开，具体建议与首批待核对项见 [PB_DOCANDMAINTANENCE 计划第 2 节](development/plan/PB_DOCANDMAINTANENCE.md#2-文档修订与管理重点2026-09-03)。

当前维护范围与待办处理见 [PB_DOCANDMAINTANENCE 计划第 1 节](development/plan/PB_DOCANDMAINTANENCE.md#1-架构收口后的维护方向2026-09-03用户确认)，架构边界见 [总 ADR](development/architecture/ADR_OVERVIEW.md)。本次确认工作方向，不代替各模块已有或尚待完成的技术验收。

### 按任务阅读

- 了解怎么使用：先读 [使用手册](usage/README.md)。
- 理解或维护模块：从下方实现手册目录选取对应模块，结合相关 ADR、实际代码和测试核对；阶段编号用于定位，不代表阅读时必须从 P1 顺序通读。
- 查找需求与取舍：读取对应计划和 ADR，区分最新明确决定、历史讨论与待决建议；出现代码与文档冲突时按 [项目规定](AGENTS.md) 处理，不自行选一方覆盖另一方。
- 接手具体任务：先明确任务范围，再读取该模块的入口、依赖、契约和验证记录。模型职责以当前任务为准，旧文档中的固定模型分工不作为本次授权。

后续修订的目标是让读者按需找到“模块做什么、为什么这样设计、从哪里改、如何验证”，并能定位到真实源码和测试。历史计划与验收记录保留其时点含义；文档修改日期不能替代代码核验日期或当前版本的测试证据。

## development/plan（计划手册）

阶段开发计划与迁移/清理计划：

- [PB_DOCANDMAINTANENCE.md](development/plan/PB_DOCANDMAINTANENCE.md)（active）：当前项目文档与维护计划，包含文档修订管理、模块打磨、UI 小修及相关交付与验收建议。
- [SYNC_RESUME_CHAIN_PLAN.md](development/plan/SYNC_RESUME_CHAIN_PLAN.md)（active，2026-09-05）：市场数据 P5 pipeline 增量/回补同步“在途窗口冻结 + 缺口追平”链式断点续传（方案 A）的需求、改动、边界与验收标准。

- `PHASE_1_PLAN.md` ~ `PHASE_5_PLAN.md`：P1 至 P5 阶段计划与历史设计记录；当前维护方向见上文。
- `P5A_PLAN.md`：P5A 历史筛选与 Backtrader 一体化回测方案（含阶段状态与验收记录）。
- `P5B_PLAN.md`（active）：P5B 指数数据与 CAPM 独立计划，保存当前确认的 Baostock 范围、央行存款基准利率、独立同步和来源可用数据验收口径；保留早期讨论及变更记录。
- [ALPHA_SEARCH_20260901_RUN.md](development/plan/ALPHA_SEARCH_20260901_RUN.md)：截至 2026-09-01 的稳健 Alpha 模板搜索；40 个候选、完整观测、邻档检查与冠军原生模板，未使用回测系统。
- `P5_RECONSTRUCT_DATASYNC.md`：DataSync 重构计划，定义确定性规划、串行抓取、staging、完整性验证、generation 原子发布、种子 SHA-256 与跨平台迁移。
- `MIGRATION_PLAN.md`：核心逻辑迁移至新架构的生效计划。
- `DELETE_MANIFEST.md`：可安全删除的非业务文件候选清单（P1-1 执行依据）。

延期且不属于当前工作序列的独立计划：

- `PHASE_6_PLAN.md`（deferred）：主力行为统计追踪与高维研究扩展，按本次架构收口决定暂缓；保留原设想供未来讨论，仅在用户重新明确授权时激活。
- `PE_DATAEXTENSION.md`（deferred）：合并规划数据源可替换性与数据类型扩展；待 P5-A 回测验收和 P5-B CAPM 完成后，仅在用户再次明确授权时激活。

## development/architecture（架构手册与架构图）

总体架构决策、ADR 与架构图（HTML/JSON 图表与手册同属一个文件夹）：

- [CURRENT_ARCHITECTURE.md](development/architecture/CURRENT_ARCHITECTURE.md)：当前源码架构阅读入口（1.17.2）。
- `ADR_OVERVIEW.md`：总 ADR，保留 P1–P5 阶段决策和2026-09-03架构收口决定；不是当前实现的完整审计报告。
- `ADR_P2_RULE_EXECUTION.md`、`ADR_P3_LOCAL_WEB_UI.md`、`ADR_P4_READ_LAYER.md`：分阶段架构决策记录。
- `ADR_P5A_BACKTEST_ENGINE.md`（Backtrader 选型与 GPLv3）、`ADR_P5A_SEED_DISTRIBUTION.md`（八年数据种子,accepted）：P5A 架构决策。
- `ADR_P5_DATASYNC_DATABASE.md`（accepted）：P5 DataSync 重构的数据库 schema、批次/分区 manifest、active generation 指针与真实库迁移基准。
- `PARAMETERIZED_SCREENING_REARCHITECTURE.md`：参数化筛选分片并发流水线架构方案。
- `ARCHITECTURE_CLEANUP.md`：旧架构移出仓库后的精简范围与验收记录。
- `stockmanager-v1.4.3-*.{html,json}`：总体架构、数据同步工作流、锁校验与筛选规则时序图。

## development/implementation（实现手册）

各阶段的设计、契约、验收与接口明细：

- `P1_1_EXECUTION_REPORT.md`：P1-1 工程骨架重建与清理执行记录。
- `P1_2_DOMAIN_PROTOCOLS.md` ~ `P1_7_E2E_ACCEPTANCE.md`：P1 各任务实现与验收文档。
- `P2_PARAMETERIZED_RULES.md`、`P3_RULE_EXTENSION.md`：P2/P3 实现文档。
- `P3_WEB_ACCEPTANCE.md`：P3 历史 Web 验收；[P3_WEB_API.md](development/implementation/P3_WEB_API.md) 沿用文件名维护当前完整 API。
- `P4_READ_LAYER.md`：P4 可替换数据库只读访问、并发读取与分片筛选基础设施实现手册。
- `P5A_1_HISTORY_V2.md` ~ `P5A_9_ACCEPTANCE.md`：P5A 各阶段实现手册（八年覆盖、PIT 契约、策略规格、历史筛选、运行存储、回测适配器、执行模型、研究 API、最终验收）。
- `P5_RECONSTRUCT_DATASYNC_IMPLEMENTATION.md`：P5 DataSync 重构实现手册（Planner/Worker/Staging/Verifier/Committer/Gate、种子与迁移工具、CLI、验收记录）。
- [CAPM数据处理](development/implementation/CAPM数据处理.md)（active）：P5B 指数与利率数据、前复权简单收益、CAPM 回归及线性年化处理手册；包含来源缺口验收、独立个股研究 UI、可切换参数、实际覆盖、接口和离线测试证据。
- `AMBIGUITY_LOG.md`：P1-3 旧规则行为歧义清单。

## usage（使用文档）

- [DATA_CAPABILITIES.md](usage/DATA_CAPABILITIES.md)：当前已存数据、计算公式、输出含义、访问入口和能力缺口（1.17.2）。
- `README.md`：用户使用手册，说明功能、内置筛选规则、回测策略（P5A）以及 macOS / Windows 下本地 Web 工作台的安装与启动方式。
- [STOCK_ALPHA_RESEARCH.md](usage/STOCK_ALPHA_RESEARCH.md)：Alpha 研究与模板搜索技能（`stock-alpha-research`，2026-09-05 由 `stock-template-alpha-search` 更名）的输入端五问/运行端筛选+回测/输出端 Alpha_Mining_V0 三段式说明、可用指标与辅助脚本验证记录。
- [Alpha Mining的优化建模.md](usage/Alpha%20Mining的优化建模.md)：记录 Alpha Mining 的候选目标函数、MDD/CVaR 硬约束、优化算法分层与待确认参数。

## 元数据要求

本 Vault 内所有技术文档必须包含 YAML frontmatter（`date`、`purpose`、`project`、`status`），修改后须保持与代码事实一致；具体要求见 `AGENTS.md` 第 10 节。
