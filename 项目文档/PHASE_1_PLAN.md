---
date: 2026-08-25
purpose: 明确 StockManager 第一阶段的范围、架构接口、任务顺序和验收标准。
project: StockManager
status: draft
---

当前架构、实现与审查代理为 Codex。P1-4 依据用户明确指令由 Codex 单独完成，不调用本地模型。

## 阶段目标
只保留 Python 筛选内核，清除 Windows PyQt GUI、PyInstaller 配置和构建产物；建立外部数据每日一次同步到本地 SQLite、本地筛选、重复拉取警告、隔日自动补齐的完整闭环。

**明确约束**：
- 实际删除前 P1-0 必须列出精确文件清单并再次核对。
- 用户已原则授权删除 Windows 可视化与打包相关文件，但**严禁**误删规则代码、用户未提交改动或唯一数据。
- **排除范围**：Web UI、登录、自动交易、实时行情、回测、多数据源、复杂数据库、分布式任务。

## 架构数据流

```mermaid
graph LR
    A[Baostock Provider] -->|仅由 DataSyncService 调用| B(DataSyncService)
    B -->|写入| C[LocalRepository SQLite]
    C -->|只读| D[ScreeningService]
    D -->|调用| E[Rules Engine]
    E -->|返回| F[CLI / API]
    
    style A fill:#f9f,stroke:#333,stroke-width:2px
    style C fill:#ccf,stroke:#333,stroke-width:2px
    style D fill:#cfc,stroke:#333,stroke-width:2px
```

**接口边界强调**：
- “每天一次”指同一数据集和交易日最多一次成功外部同步；同步失败后仅允许用户显式、冷却后重试。
- `Baostock -> DataSyncService -> LocalRepository(SQLite) -> ScreeningService -> Rules`
- 筛选路径**不能**反向访问 Provider。
- 自动更新定义为程序在新交易日首次启动时更新，不承诺程序关闭时后台运行。
- 最新已完成交易日必须根据交易日历和数据可用截止时间计算，截止时间配置化。

## 任务分解

### P1-0: 只读盘点与迁移计划
- **目标**：识别保留的 Python 规则，精确列出待删除的 Windows/PyQt/PyInstaller/build/dist/exe 目标，核对用户未提交改动。
- **产物**：
  - `MIGRATION_PLAN.md`：详细迁移步骤。
  - `DELETE_MANIFEST.md`：精确到文件路径的删除清单。
- **验收条件**：
  - 清单中不包含任何 `.py` 规则逻辑文件（除非确认为废弃且无引用）。
  - 清单中不包含用户本地未提交的修改文件。
  - 此阶段**不执行**任何删除操作。
- **依赖**：无。

### P1-1: 工程骨架重建与清理
- **目标**：建立新的 Python 工程结构，按批准清单删除 Windows 可视化和打包文件。
- **产物**：
  - `pyproject.toml`：项目元数据与依赖。
  - `src/stock_manager/`：核心代码包。
  - `tests/`：测试目录。
  - `README.md`：项目说明。
  - `.gitignore`：忽略构建产物。
- **验收条件**：
  - `git status` 显示已删除所有 `DELETE_MANIFEST.md` 中的文件。
  - 项目结构符合 PEP 517/518 标准。
  - 保留必要的 Python 规则参考代码。
- **依赖**：P1-0。

### P1-2: 领域模型与协议定义
- **目标**：定义核心数据结构和接口协议。
- **产物**：
  - `domain.py`：包含 `StockIdentity`, `DailyBar`, `FundamentalSnapshot`, `DividendRecord`, `DatasetMetadata`, `RuleResult`, `ScreeningResult`, `SyncRecord` 的 dataclass 定义。
  - `protocols.py`：`ProviderProtocol`, `LocalRepositoryProtocol`。
- **验收条件**：
  - 所有 dataclass 字段类型明确，包含必要的元数据（如 `synced_at`, `source`）。
  - Protocol 定义清晰，无具体实现耦合。
- **依赖**：P1-1。

### P1-3: 规则特征测试与固定
- **状态**：已完成（2026-08-25，30 个离线测试通过）。
- **目标**：为旧规则建立固定 fixtures 和特征测试，确保行为一致性。
- **产物**：
  - `tests/fixtures/`：包含典型市场数据的 JSON/CSV 文件。
  - `tests/test_rules_characterization.py`：针对每个旧规则的输入输出快照测试。
- **验收条件**：
  - 所有现有规则在给定 fixtures 下输出与旧版本一致。
  - 歧义已记录在 `AMBIGUITY_LOG.md`；按用户“先固定快照、未用规则暂不改”的决定留待 P1-4 处理。
- **依赖**：P1-2。

### P1-4: 纯函数规则迁移
- **状态**：已完成（2026-08-25，51 个离线测试通过）。
- **目标**：将旧组合规则迁移为纯函数，支持 JSON 阈值配置。
- **产物**：
  - `rules/` 模块：
    - `pe_positive.py`
    - `non_st.py`
    - `dividend_3y.py`
    - `volume_price_5d.py`
    - `limit_up_breakout.py` (五日炸板突破最高价或假阴线)
    - `limit_up_3m.py` (三月涨停)
    - `volatility_multiple.py` (价格波动倍数)
    - `composite.py` (显式组合)
  - `config/rules.json`：默认阈值配置。
- **验收条件**：
  - 每个规则返回 `RuleResult`，必须包含 `rule_id`、`passed`、`actual_value`、`threshold`、`reason`。
  - 规则函数无副作用，不访问网络或数据库。
  - 通过 P1-3 的特征测试。
- **依赖**：P1-3。

### P1-5: 本地数据同步与存储
- **状态**：已完成（2026-08-25，72 个离线测试通过）。
- **目标**：实现 SQLite 本地存储、Baostock 同步服务、防重复与锁机制。
- **产物**：
  - `storage/sqlite_repo.py`：实现 `LocalRepositoryProtocol`，表结构包括 `stocks`, `daily_bars`, `fundamentals`, `dividends`, `trading_days`, `sync_runs`。
  - `sync/data_sync_service.py`：
    - 仅调用 `BaostockProvider`。
    - 实现 `sync_record` 持久化。
    - 实现文件/进程锁。
    - 实现“同交易日成功一次后拒绝再次拉取并警告”逻辑。
    - 实现“失败不记成功，显式冷却重试”逻辑。
    - 实现“串行限速”逻辑。
    - 实现“新交易日首次启动自动同步，漏运行时下次启动补齐”逻辑。
  - `providers/baostock_provider.py`：封装 Baostock API。
  - `providers/fixture_provider.py`：用于离线测试的模拟 Provider。
- **验收条件**：
  - 连续触发 `sync` 多次，实际只发起一次外部同步，后续返回警告和本地数据。
  - 并发测试验证锁机制，无数据竞争。
  - 同步失败后，`sync_runs` 状态为 `FAILED`，重试需显式触发。
  - 断网环境下，`sync` 命令能正确报错且不污染本地数据。
- **依赖**：P1-2, P1-4。

### P1-6: 筛选服务与 CLI
- **目标**：实现只读筛选服务和命令行接口。
- **产物**：
  - `services/screening_service.py`：
    - 仅读取 `LocalRepository`。
    - 调用 `Rules` 引擎。
    - 返回 `ScreeningResult`。
  - `cli/main.py`：
    - `screen` 命令：支持单只和批量，输出 JSON 和摘要，**绝不访问 Baostock**。
    - `sync` 命令：触发受保护同步，显示进度和警告。
    - `status` 命令：显示本地数据最新日期、同步状态、锁状态。
- **验收条件**：
  - `screen` 命令在断网时仍能用本地数据正常执行。
  - `sync` 命令在数据已存在时显示警告。
  - CLI 输出格式稳定，便于脚本解析。
- **依赖**：P1-5。

### P1-7: 端到端验证与文档
- **目标**：完成全流程测试，同步 Obsidian 文档。
- **产物**：
  - `tests/test_e2e.py`：端到端测试用例。
  - `docs/`：全部包含 YAML frontmatter 的更新后技术文档。
  - Obsidian 笔记同步。
- **验收条件**：
  - **总验收**：
    1. 连续触发 `sync` 多次，实际只发起一次外部同步。
    2. 并发测试验证锁机制有效。
    3. `screen` 在断网时仍能用本地数据。
    4. 新交易日自动补齐逻辑验证通过。
    5. 同步失败可恢复且不伪装成功。
  - 所有测试通过，关键规则、同步保护和关键分支均有测试。
  - 文档与代码一致。
- **依赖**：P1-6。

## 风险与缓解
- **复权处理**：复权策略待用户确认；采用的复权方式记录在 `DatasetMetadata`，不写入 `DailyBar`。
- **涨停制度**：必须根据交易日、证券板块、ST 状态和当时有效制度判定，禁止写死固定比例。
- **三年分红定义**：明确是最近三个自然年度还是滚动 36 个月，需在 P1-3 中由用户确认。
- **旧代码歧义**：P1-3 中发现的歧义必须记录并等待用户决策。
- **删除清单**：P1-0 必须严格核对，防止误删。

## 测试策略
- **离线测试**：默认测试完全离线，使用 `FixtureProvider` 和预置 SQLite 数据库。
- **在线测试**：显式开启（如 `pytest -m online`），仅用于验证 Baostock 连接和同步逻辑，Baostock 无需 API Key。
- **并发测试**：使用多线程/多进程模拟并发同步请求，验证锁机制。
