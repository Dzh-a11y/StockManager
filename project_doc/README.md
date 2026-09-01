---
date: 2026-09-01
purpose: StockManager 项目文档中心索引：说明 project_doc 的目录组织与各文档归属。
project: StockManager
status: active
---

# StockManager 项目文档中心

本目录（`project_doc`）是 StockManager 的 Obsidian Vault 与默认项目知识源，存放全部项目资料、设计、需求、决策和开发记录。文档按「开发手册」与「使用手册」两类组织，目录名均为英文：

```text
project_doc/
├── README.md                # 本文档（索引）
├── AGENTS.md                # 项目开发规定（与仓库根 AGENTS.md 同步）
├── development/             # 开发手册
│   ├── plan/                #   计划手册：阶段计划、迁移计划、删除清单
│   ├── architecture/        #   架构手册与架构图（同属一个文件夹）
│   └── implementation/      #   实现手册：各阶段设计、验收与接口明细
└── usage/                   # 使用手册：安装、启动、规则与操作说明
```

## development/plan（计划手册）

阶段开发计划与迁移/清理计划：

- `PHASE_1_PLAN.md` ~ `PHASE_6_PLAN.md`：P1 至 P6 阶段计划。
- `P5A_PLAN.md`：P5A 历史筛选与 Backtrader 一体化回测方案（含阶段状态与验收记录）。
- `MIGRATION_PLAN.md`：核心逻辑迁移至新架构的生效计划。
- `DELETE_MANIFEST.md`：可安全删除的非业务文件候选清单（P1-1 执行依据）。

## development/architecture（架构手册与架构图）

总体架构决策、ADR 与架构图（HTML/JSON 图表与手册同属一个文件夹）：

- `ADR_OVERVIEW.md`：汇总 P1 至 P3 全部已接受架构决策的总 ADR。
- `ADR_P2_RULE_EXECUTION.md`、`ADR_P3_LOCAL_WEB_UI.md`、`ADR_P4_READ_LAYER.md`：分阶段架构决策记录。
- `ADR_P5A_BACKTEST_ENGINE.md`（Backtrader 选型与 GPLv3）、`ADR_P5A_SEED_DISTRIBUTION.md`（八年数据种子,accepted）：P5A 架构决策。
- `PARAMETERIZED_SCREENING_REARCHITECTURE.md`：参数化筛选分片并发流水线架构方案。
- `ARCHITECTURE_CLEANUP.md`：旧架构移出仓库后的精简范围与验收记录。
- `stockmanager-v1.4.3-*.{html,json}`：总体架构、数据同步工作流、锁校验与筛选规则时序图。

## development/implementation（实现手册）

各阶段的设计、契约、验收与接口明细：

- `P1_1_EXECUTION_REPORT.md`：P1-1 工程骨架重建与清理执行记录。
- `P1_2_DOMAIN_PROTOCOLS.md` ~ `P1_7_E2E_ACCEPTANCE.md`：P1 各任务实现与验收文档。
- `P2_PARAMETERIZED_RULES.md`、`P3_RULE_EXTENSION.md`：P2/P3 实现文档。
- `P3_WEB_ACCEPTANCE.md`、`P3_WEB_API.md`：Web 验收与 Web API 接口文档。
- `P4_READ_LAYER.md`：P4 可替换数据库只读访问、并发读取与分片筛选基础设施实现手册。
- `P5A_1_HISTORY_V2.md` ~ `P5A_9_ACCEPTANCE.md`：P5A 各阶段实现手册（八年覆盖、PIT 契约、策略规格、历史筛选、运行存储、回测适配器、执行模型、研究 API、最终验收）。
- `AMBIGUITY_LOG.md`：P1-3 旧规则行为歧义清单。

## usage（使用手册）

- `README.md`：用户使用手册，说明功能、内置筛选规则、回测策略（P5A）以及 macOS / Windows 下本地 Web 工作台的安装与启动方式。

## 元数据要求

本 Vault 内所有技术文档必须包含 YAML frontmatter（`date`、`purpose`、`project`、`status`），修改后须保持与代码事实一致；具体要求见 `AGENTS.md` 第 10 节。
