---
date: 2026-08-25
purpose: 记录旧 StockManager 架构移出仓库后的精简范围、恢复位置和验收结果。
project: StockManager
status: active
---

# 旧架构清理记录

## 结果

完成 P1-1 至 P1-7 迁移和验收后，运行时仓库已精简为 `src/stock_manager/`、`tests/`、`config/` 和技术文档。新内核不存在对旧模块的导入或运行时依赖。

以下旧架构内容已移出代码仓库：

- `screener.py`：Baostock、筛选流程和 PyQt GUI 混合入口。
- `condition/`：旧基本面与技术规则。
- `main/`：旧手工股票获取入口。
- `Test/`：旧测试脚本和历史日志目录。
- `record_close/`：旧 Excel、前收盘价和 PyInstaller 架构。
- `build/`、`logs/`：旧构建与运行输出目录。
- `tests/test_rules_characterization.py`：直接导入旧 `condition/` 的迁移期特征测试。

## 可恢复归档

上述文件未永久删除，统一移动至项目根目录下的 `legacy_stockmanager_2025_archive`。该目录位于代码仓库外，不参与新内核包发现、测试收集或运行时导入。

固定行为 fixture `tests/fixtures/legacy_rules_market_data.json`、`AMBIGUITY_LOG.md` 和 P1-3/P1-4 技术文档继续保留，确保迁移依据没有丢失。

## 验收

- 对新仓库执行旧路径引用扫描，仅技术文档保留历史说明，无运行时反向依赖。
- `compileall` 通过。
- 精简后的现行测试套件：`65 passed`。
- `.idea` 用户改动与 `.venv` 均未操作。
- 未创建 Git commit。
