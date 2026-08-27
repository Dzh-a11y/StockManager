---
date: 2026-08-25
purpose: 索引 StockManager P3 本地 Web 可视化与规则自动扩展的正式执行计划。
project: StockManager
status: draft
---

# StockManager P3 计划索引

P3 正式任务包已写入代码仓库根目录的 `PHASE_3_PLAN.md`。该任务包按 P3-0 至 P3-9 拆分本地 HTTP 接口、动态规则目录、模板 API、离线筛选 API、元数据驱动策略编辑器、模板工作流、结果可视化、新规则扩展性验收以及最终文档验收。

P3 的核心门禁如下：

- 页面不得硬编码内置规则 ID；规则表单必须由 `RuleRegistry.definitions()` 经规则目录 API 自动生成。
- Web、API、筛选服务和规则只读本地 SQLite，不得访问 Provider 或触发自动同步。
- 模板校验必须继续以 `parse_template` 和 `TemplateCompiler` 为后端权威入口。
- 系统模板只读，用户模板更新和删除必须进行 revision 并发检查。
- P3-8 必须用测试规则证明：后端显式注册后，API 自动发现、前端自动生成控件、模板可编译且筛选结果可展示。
- 新规则需要当前 `RuleContext` 不支持的数据域时，必须先通过 ADR 扩展领域、本地 Repository 和同步边界，禁止规则自行查库或联网。

StockManager 是 A 股研究型筛选平台，禁止自动交易；所有筛选结果仅供研究参考，不构成任何投资建议。
