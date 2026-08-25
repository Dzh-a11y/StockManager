---
date: 2026-08-25
purpose: 记录 P2 规则三态、模板版本、兼容入口和年度最低量组合归属的架构决策。
project: StockManager
status: active
---

# ADR：P2 参数化规则执行契约

## 状态

已接受并实现。

## 背景

P1 的 `RuleResult.passed` 是布尔值，`ScreeningService` 逐条调用固定规则并执行固定组合。P2 需要规则开关、模板版本、通用组合和可扩展数据需求，同时必须保护现有 CLI 和领域调用方。

## 决策

### 三态采用兼容包装

保留 P1 `RuleResult` 的全部字段和布尔 `passed`。P2 新增 `RuleExecutionResult`，使用 `RuleStatus.PASSED`、`FAILED`、`SKIPPED` 包装规则执行。

只有模板禁用规则是 `SKIPPED`；启用规则的样本不足仍然是结构化 `FAILED`。这样既不伪造禁用规则为通过，也不破坏 P1 构造函数和 JSON 契约。

### 版本 1 与版本 2 使用显式入口

P1 的 `screen --rules config/rules.json` 继续使用版本 1 配置。P2 增加 `screen-template --template TEMPLATE.json`，只接受严格版本 2 模板。系统不自动覆盖或改写用户的版本 1 文件。

### 模板执行前编译

模板必须先通过 `TemplateCompiler` 转换为不可变 `ScreeningPlan`。未知字段、规则、参数、重复组合引用、启用规则遗漏和全禁用配置在读取市场数据前失败。

### 规则注册是显式可信注册

模板只引用稳定 `rule_id`。后端使用 `build_default_registry()` 显式注册规则，不扫描目录，不从模板导入模块，也不支持运行时上传 Python 代码。

### 年度最低量属于信号组

默认模板把 `annual_min_volume` 放在 `signal` 的 `any` 组，与 `volume_price_5d`、`limit_up_breakout` 任一通过即可。用户模板可以通过受校验的组合配置调整规则，但每条启用规则必须恰好出现一次。

### 模板 revision 使用乐观并发契约

用户模板从 revision 1 开始。更新和删除必须携带 `expected_revision`；不匹配时抛出 `TemplateRevisionConflictError`。保存采用同目录临时文件、刷新落盘和原子替换。

## 结果

- P1 调用方无需接受可空 `passed`。
- P2 可以明确报告禁用规则并保持组合引擎通用。
- 版本迁移可审计，不会静默改变已有筛选配置。
- 新规则通过注册、参数、数据需求、纯函数和模板组合接入，不修改筛选主循环。
- Web 和 HTML 可以在后续阶段消费规则定义与模板契约，但不属于本 ADR 的已实现范围。
