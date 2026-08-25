---
date: 2026-08-25
purpose: 基于只读盘点结果制定 StockManager 核心逻辑迁移至新架构的生效计划
project: StockManager
status: active
---

# StockManager 迁移计划 (MIGRATION_PLAN)

## 规范路径

- 项目根目录：`/Users/douzihao/Documents/StockManager`。
- 代码仓库：`/Users/douzihao/Documents/StockManager/stock_analysis_based_on_baostock_2025_09_19`。
- 项目文档：`/Users/douzihao/Documents/StockManager/Stockmanager/项目文档`。
- 本文中的仓库相对路径均以代码仓库为基准。

## 1. 现状摘要
当前代码仓库 `/Users/douzihao/Documents/StockManager/stock_analysis_based_on_baostock_2025_09_19` 处于 P1-0 只读盘点阶段。
- **代码状态**：9 个业务 Python 文件均通过 AST 语法解析，无语法错误。
- **架构问题**：
    - `screener.py` 为混合文件（574行），前300行包含核心数据访问与筛选逻辑，后部分为 PyQt GUI，耦合严重。
    - `condition/Basic_Technique_check.py` 存在 `check_price_volatility` 函数重复定义（164行与379行）。
    - 数据访问层直接调用 Baostock，缺乏抽象，存在异常吞没、返回元组长度不一致、固定涨停判断等风险。
    - 存在 Windows 绝对路径硬编码。
- **非代码资产**：存在大量 PyInstaller 构建产物、日志文件、Python 缓存文件及 macOS 系统文件（.DS_Store）。
- **Git 状态**：部分 IDE 配置文件已修改，两个 PyInstaller 产物已处于 Git 删除状态，多个文件未跟踪。

## 2. 保留 / 迁移 / 延期删除矩阵

| 模块/文件 | 当前状态 | 处置策略 | 理由 |
| :--- | :--- | :--- | :--- |
| `condition/Technique_condition.py` | 核心规则 | **保留/迁移** | 技术规则主要来源，必须保留用于特征测试。 |
| `condition/Basic_Technique_check.py` | 核心规则 | **保留/迁移** | 技术规则主要来源，需修复重复定义后迁移。 |
| `condition/Basic_condition.py` | 核心规则 | **保留/迁移** | 含 PE、ST、分红规则，需解耦 Baostock 直接调用。 |
| `screener.py` (1-299行) | 核心流程 | **迁移** | 包含 Baostock 访问、基本面、历史数据、组合筛选核心逻辑。 |
| `screener.py` (300行+) | GUI | **淘汰** | PyQt GUI 部分，新架构不保留 GUI。 |
| `main/GetStock_Lite.py` | 重复/手工 | **延期删除** | 行为参考，特征测试后再决定删除。 |
| `Test/Test_technique_condition_basic_condition.py` | 测试/参考 | **延期删除** | 行为参考，特征测试后再决定删除。 |
| `record_close/record_close.py` | 数据处理 | **延期删除** | Excel/前收盘价处理，迁移参考，确认是否纳入新内核。 |
| `record_close/record_close_APP.py` | 数据处理 | **延期删除** | argparse Python 程序，迁移参考，确认是否纳入新内核。 |
| `record_close/build/*` | 构建物 | **候选删除** | PyInstaller 构建产物，无业务价值。 |
| `record_close/dist/*` | 构建物 | **候选删除** | PyInstaller 分发产物，无业务价值。 |
| `*.log` | 日志 | **候选删除** | 历史运行日志，无业务价值。 |
| `__pycache__/*` | 缓存 | **候选删除** | Python 字节码缓存，可再生。 |
| `*.spec` | 配置 | **候选删除** | PyInstaller 打包配置，新架构不使用 PyInstaller。 |
| `.DS_Store` | 系统文件 | **候选删除** | macOS 系统文件，不应纳入版本控制。 |
| `.venv` | 环境 | **保留** | 未跟踪本地环境，由用户自行管理。 |
| `.idea` | IDE配置 | **保留** | 含用户未提交改动，本阶段不操作，未来需单独确认停止跟踪。 |

## 3. 模块到新架构映射

新架构目标：`BaostockProvider -> DataSyncService -> SQLite LocalRepository -> ScreeningService -> Rules`

| 旧模块/逻辑 | 新架构组件 | 映射说明 |
| :--- | :--- | :--- |
| `screener.py` (Baostock访问部分) | `BaostockProvider` | 封装 Baostock API 调用，统一异常处理，消除直接逐股票访问风险。 |
| `screener.py` (数据获取/同步) | `DataSyncService` | 负责从 Provider 获取数据并同步至本地存储。 |
| `record_close/record_close.py`（Excel/前收盘价） | **延期决定** | 当前不映射到 Repository；待需求明确后决定迁移或淘汰。 |
| `condition/Basic_condition.py` | `Rules` | 提取 PE、ST、分红规则，移除直接 Baostock 调用，改为依赖注入数据。 |
| `condition/Technique_condition.py` | `Rules` | 提取技术规则，作为纯函数或规则对象。 |
| `condition/Basic_Technique_check.py` | `Rules` | 提取技术检查逻辑，修复重复定义，作为纯函数或规则对象。 |
| `screener.py` (组合筛选逻辑) | `ScreeningService` | 协调 Rules 和 LocalRepository，执行筛选流程。 |
| `screener.py` (PyQt GUI) | **淘汰** | 新系统只保留 Python 核心，不保留 GUI。 |
| `record_close/record_close_APP.py` | **淘汰/待定** | 若 Excel 逻辑不纳入新内核，则此文件及关联逻辑淘汰。 |

## 4. 迁移顺序

1. **特征测试先行**
   - 不修改旧源码。
   - 固定输入 fixtures，记录现有行为（包括重复函数、返回值不一致、异常吞没等）。
   - 对歧义行为提交用户决定，形成决策记录。

2. **建立新工程骨架**
   - 创建新目录结构。
   - 定义 Domain 模型与 Protocol 接口。

3. **迁移 Rules 纯函数**
   - 将筛选逻辑重构为无副作用的纯函数。
   - 确保输入输出明确，便于单元测试。

4. **建立数据层**
   - 实现 `BaostockProvider` 数据获取接口。
   - 实现 `DataSyncService` 同步逻辑。
   - 实现 SQLite `LocalRepository`。
   - 添加每日一次同步保护机制，防止重复拉取。

5. **建立服务层与 CLI**
   - 实现 `ScreeningService`，组合 Rules 与 Repository。
   - 提供命令行接口（CLI）用于执行筛选任务。

6. **验收与淘汰**
   - 完成功能验收测试。
   - 删除旧文件与 GUI 组件。
   - 更新文档，标记迁移完成。

## 5. 延期决定

- **record_close Excel 逻辑**：
  - 当前阶段不直接映射进 Repository。
  - 标记为“延期决定”，待后续需求明确后再评估是否纳入核心数据流。

## 6. P1-1 进入条件

1. **文档完成**
   - `MIGRATION_PLAN.md` 与 `DELETE_MANIFEST.md` 已完成并审核通过。

2. **架构复核**
   - 架构与审查代理已完成复核清单，无阻塞性问题。

3. **用户授权**
   - 用户原则授权已记录。
   - 执行前仍需核对精确路径，确保无误。

4. **Git 状态保护**
   - 当前 `git status` 已记录。
   - `.idea` 目录下的用户改动受保护，不得删除或覆盖。

5. **删除范围限制**
   - P1-1 仅删除安全生成物（如缓存、临时文件）。
   - 不删除任何 `.py` 源文件。
   - `screener.py` 在核心迁移完成前受保护，不得删除。

6. **无代码仓库变更**
   - 本阶段不要求 Rules、Provider、Service 已实现。
   - 本阶段无代码仓库变更，仅进行文档与状态确认。

## 7. 本阶段无文件变更

-   本阶段为 P1-0 只读盘点，**不执行任何文件删除、移动或提交操作**。
-   所有删除操作均为候选状态，需待 P1-1 阶段经架构审查代理复核后方可执行。
-   `.venv` 和 `.idea` 目录不在本阶段操作范围内。
-   两个已处于 Git 删除状态的文件（`record_close/build/record_close/record_close.pkg` 和 `record_close/dist/record_close_APP.exe`）保持不操作。
