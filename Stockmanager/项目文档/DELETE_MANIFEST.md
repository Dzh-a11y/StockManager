---
date: 2026-08-25
purpose: 列出 StockManager 项目中可安全删除的非业务文件候选清单
project: StockManager
status: active
---

# StockManager 删除清单 (DELETE_MANIFEST)

**状态说明**：以下所有条目均为**候选**，P1-1 执行前需架构审查代理复核。本清单不包含任何删除命令，避免误执行。

**路径基准**：本清单中所有相对路径均相对于代码仓库 `/Users/douzihao/Documents/StockManager/stock_analysis_based_on_baostock_2025_09_19`；项目根目录为 `/Users/douzihao/Documents/StockManager`。

## 1. PyInstaller 构建物

**原因**：PyInstaller 生成的构建中间文件和分发文件，无业务价值，新架构不使用 PyInstaller。
**可恢复性**：可通过重新运行 PyInstaller 构建过程恢复（但新架构已淘汰 PyInstaller，故无需恢复）。
**执行前检查**：确认新架构已完全替代 PyInstaller 打包流程，且无其他依赖这些文件的脚本。

| 路径 | 大小/文件数 | 备注 |
| :--- | :--- | :--- |
| `build/screener/localpycs/pyimod01_archive.pyc` | - | PyInstaller 构建缓存 |
| `build/screener/localpycs/pyimod02_importers.pyc` | - | PyInstaller 构建缓存 |
| `build/screener/localpycs/pyimod03_ctypes.pyc` | - | PyInstaller 构建缓存 |
| `build/screener/localpycs/pyimod04_pywin32.pyc` | - | PyInstaller 构建缓存 |
| `build/screener/localpycs/struct.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close/Analysis-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close/EXE-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close/PKG-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close/PYZ-00.pyz` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close/PYZ-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close/base_library.zip` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close/localpycs/pyimod01_archive.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close/localpycs/pyimod02_importers.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close/localpycs/pyimod03_ctypes.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close/localpycs/pyimod04_pywin32.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close/localpycs/struct.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close/warn-record_close.txt` | - | PyInstaller 构建警告日志 |
| `record_close/build/record_close/xref-record_close.html` | - | PyInstaller 构建交叉引用 |
| `record_close/build/record_close_APP/Analysis-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/EXE-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/PKG-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/PYZ-00.pyz` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/PYZ-00.toc` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/base_library.zip` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/localpycs/pyimod01_archive.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close_APP/localpycs/pyimod02_importers.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close_APP/localpycs/pyimod03_ctypes.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close_APP/localpycs/pyimod04_pywin32.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close_APP/localpycs/struct.pyc` | - | PyInstaller 构建缓存 |
| `record_close/build/record_close_APP/record_close_APP.pkg` | - | PyInstaller 构建中间文件 |
| `record_close/build/record_close_APP/warn-record_close_APP.txt` | - | PyInstaller 构建警告日志 |
| `record_close/build/record_close_APP/xref-record_close_APP.html` | - | PyInstaller 构建交叉引用 |
| `record_close/dist/logs/stock_screening_20250919_081939.log` | - | 构建目录中的日志 |
| `record_close/dist/logs/stock_screening_20250919_081944.log` | - | 构建目录中的日志 |
| `record_close/dist/logs/stock_screening_20250919_084151.log` | - | 构建目录中的日志 |
| `record_close/dist/logs/stock_screening_20250920_020338.log` | - | 构建目录中的日志 |

**总览**：
- `build` 目录：约 56K / 5 文件
- `record_close/build` 目录：约 56M / 27 文件
- `record_close/dist` 目录：当前约 12K / 4 文件

## 2. 日志文件

**原因**：历史运行日志，无业务价值，占用存储空间。
**可恢复性**：不可恢复，但日志通常用于调试，新架构应实现结构化日志记录。
**执行前检查**：确认无未解决的 bug 需要参考这些日志，且日志内容已归档或不再需要。

| 路径 | 备注 |
| :--- | :--- |
| `Test/logs/stock_screening_20250919_050007.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050130.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050152.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050206.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050309.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050409.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050458.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050611.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050718.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_050801.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_051217.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_051306.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_051326.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_051639.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_051921.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_052205.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_052408.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_052442.log` | 测试日志 |
| `Test/logs/stock_screening_20250919_052509.log` | 测试日志 |
| `logs/stock_screening_20250920_081451.log` | 主日志 |
| `logs/stock_screening_20250920_145154.log` | 主日志 |
| `main/logs/stock_screening_20250920_080139.log` | 主日志 |
| `main/logs/stock_screening_20250920_080157.log` | 主日志 |
| `main/logs/stock_screening_20250920_080238.log` | 主日志 |
| `record_close/logs/stock_screening_20250919_080318.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_080326.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_080340.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082206.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082249.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082358.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082429.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082533.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082742.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_082904.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_083018.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_083321.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_083501.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250919_083612.log` | record_close 日志 |
| `record_close/logs/stock_screening_20250920_081903.log` | record_close 日志 |

**总览**：约 1.45M / 39 文件

## 3. Python 缓存文件

**原因**：Python 字节码缓存，可再生，无业务价值。
**可恢复性**：可通过重新运行 Python 脚本自动重新生成。
**执行前检查**：无特殊检查，删除后 Python 会自动重新生成。

| 路径 | 备注 |
| :--- | :--- |
| `condition/__pycache__/Basic_Technique_check.cpython-313.pyc` | Python 字节码缓存 |
| `condition/__pycache__/Basic_condition.cpython-313.pyc` | Python 字节码缓存 |
| `condition/__pycache__/Technique_condition.cpython-313.pyc` | Python 字节码缓存 |
| `condition/__pycache__/__init__.cpython-313.pyc` | Python 字节码缓存 |
| `main/__pycache__/Basic_Technique_check.cpython-313.pyc` | Python 字节码缓存 |
| `main/__pycache__/Basic_condition.cpython-313.pyc` | Python 字节码缓存 |
| `main/__pycache__/Technique_condition.cpython-313.pyc` | Python 字节码缓存 |

**总览**：7 个文件

## 4. PyInstaller spec 文件

**原因**：PyInstaller 打包配置文件，用于定义打包行为。新架构已明确淘汰 PyInstaller 打包流程，且核心逻辑将重构为模块化 Python 包，不再需要此类静态打包配置。
**可恢复性**：高。若未来需要恢复打包能力，必须另行作出架构决策并根据届时的项目结构重新生成配置。
**执行前检查**：
1. 确认新架构中没有任何脚本或文档引用这两个 spec 文件。
2. 确认团队已达成共识，不再使用 PyInstaller 作为主要分发方式。
3. 确认删除只影响打包配置，不影响 `record_close` 中待评估的 Python 逻辑。

| 路径 | 备注 |
| :--- | :--- |
| `record_close/record_close.spec` | PyInstaller 打包配置 |
| `record_close/record_close_APP.spec` | PyInstaller 打包配置 |

## 5. macOS 系统文件

**原因**：macOS Finder 自动生成的目录元数据文件，用于存储图标位置、视图设置等本地信息。这些文件与代码逻辑无关，且因用户环境不同而频繁变动，纳入版本控制会导致无意义的 diff 噪音。
**可恢复性**：高。删除后，macOS Finder 会在下次访问目录时自动重新生成。不影响任何代码功能。
**执行前检查**：
1. 确认 `.gitignore` 中已包含 `.DS_Store` 规则，防止未来再次被跟踪。
2. 确认没有脚本依赖这些文件中的元数据（极不可能，但需确认）。

| 路径 | 备注 |
| :--- | :--- |
| `.DS_Store` | macOS 系统元数据 |
| `main/.DS_Store` | macOS 系统元数据 |
| `record_close/.DS_Store` | macOS 系统元数据 |

## 6. 已处于 Git 删除状态，本阶段保持不操作

**说明**：以下文件在当前工作树中已处于删除状态（`git status` 显示为 `deleted`），但尚未提交；盘点无法推断删除原因。根据 P1-0 只读盘点原则，**本阶段不执行任何 `git add`、`git commit` 或 `git restore` 操作**。这些文件的状态由用户在后续阶段自行决定是提交删除还是恢复。

| 路径 | 当前 Git 状态 | 备注 |
| :--- | :--- | :--- |
| `record_close/build/record_close/record_close.pkg` | `deleted` | PyInstaller 构建产物，当前工作树中已缺失 |
| `record_close/dist/record_close_APP.exe` | `deleted` | PyInstaller 分发产物，当前工作树中已缺失 |

**注意**：
- 不执行 `git rm` 或 `git add`。
- 不执行 `git checkout -- <file>` 或 `git restore <file>`。
- 保持工作树现状，等待用户在 P1-1 或后续阶段明确指示。

## 7. 明确排除与受保护项

以下文件**严禁删除**，无论其是否包含问题代码、重复定义或硬编码路径。它们是业务逻辑的核心载体，必须在迁移过程中保留并重构。

### 7.1 业务 Python 文件（核心逻辑）

| 路径 | 保护理由 |
| :--- | :--- |
| `screener.py` | 包含核心筛选流程和数据访问逻辑，虽耦合 GUI，但前 300 行为关键业务代码 |
| `condition/Basic_condition.py` | 基本面规则核心，含 PE、ST、分红判断 |
| `condition/Technique_condition.py` | 技术面规则核心 |
| `condition/Basic_Technique_check.py` | 技术检查逻辑，虽有重复定义，但需修复后保留 |
| `condition/__init__.py` | 包初始化文件，可能包含导出逻辑 |
| `main/GetStock_Lite.py` | 轻量级股票获取逻辑，行为参考 |
| `Test/Test_technique_condition_basic_condition.py` | 测试用例，行为参考，用于特征测试 |
| `record_close/record_close.py` | Excel/前收盘价处理逻辑，待决定归属 |
| `record_close/record_close_APP.py` | argparse 程序入口，待决定归属 |

### 7.2 IDE 配置文件（含未提交修改）

以下文件存在用户未提交的修改，**不得列入删除清单**，也不得执行 `git checkout` 或 `git restore` 覆盖用户改动。

| 路径 | 状态 | 保护理由 |
| :--- | :--- | :--- |
| `.idea/misc.xml` | `modified` | 用户已修改，含项目配置 |
| `.idea/stock_analysis_based_on_baostock_2025_09_19.iml` | `modified` | 用户已修改，含模块配置 |
| `.idea/workspace.xml` | `modified` | 用户已修改，含工作区状态 |
| `.idea/modules.xml` | `untracked` | 未跟踪文件，先不处理，避免干扰用户 IDE 环境 |

### 7.3 虚拟环境

| 路径 | 状态 | 保护理由 |
| :--- | :--- | :--- |
| `.venv` | `untracked` | 本地 Python 虚拟环境，由用户自行管理，不纳入版本控制，也不删除 |

## 8. P1-1 执行前复核清单

在执行任何删除操作前，必须逐项完成以下复核，确保无误删、无遗漏、无副作用：

- [ ] **无 .py 文件删除**：确认删除清单中不包含任何 `.py` 文件。所有业务 Python 文件均在“明确排除与受保护项”中列出。
- [ ] **无 .idea/.venv 操作**：确认删除清单中不包含 `.idea` 目录下的任何文件，也不包含 `.venv` 目录。
- [ ] **用户改动保留**：确认 `.idea/misc.xml`、`.idea/stock_analysis_based_on_baostock_2025_09_19.iml`、`.idea/workspace.xml` 的未提交修改未被覆盖或重置。
- [ ] **日志不含唯一诊断证据**：确认待删除的日志文件中不包含任何未解决的 bug 诊断信息、关键错误堆栈或唯一的历史运行数据。如有，需先归档或提取关键信息。
- [ ] **Git 状态核对**：删除后，立即运行 `git status`，确认：
  - 所有待删除文件显示为 `deleted`。
  - 无意外文件被删除。
  - 无意外文件被添加。
  - `.idea` 和 `.venv` 状态未变。
- [ ] **候选状态确认**：确认所有删除操作均为“候选”状态，未执行 `git commit`。删除操作仅在本地工作树生效，可随时通过 `git restore` 恢复。
- [ ] **无删除命令执行**：确认本清单中不包含任何 `rm`、`git rm`、`git add` 等执行命令。所有操作均由用户在 P1-1 阶段手动执行或经架构审查代理批准后执行。

**状态**：候选（Draft）  
**执行权限**：需架构审查代理复核批准后方可执行  
**恢复方式**：`git restore <path>` 或从 Git 历史中恢复
