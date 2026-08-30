---
date: 2026-08-27
purpose: 说明 P2 参数化可扩展规则后端的架构、模板契约、规则扩展方式与验收结果。
project: StockManager
status: active
---

# P2 参数化可扩展规则后端

## 交付结论

P2 在保留 P1 `ScreeningService`、版本 1 `config/rules.json` 和 `screen` CLI 契约的同时，增加了规则显式注册、版本 2 策略模板、计划驱动的数据加载、通用规则执行和安全模板持久化。新增 `screen-template` 命令只读取本地 SQLite，不调用 Provider。

当前架构、实现与审查代理为 Codex。Qwen 按任务包起草过本文初稿；该初稿因混淆 `SKIPPED` 与样本不足、错误描述 `non_st` 等问题被 Codex 驳回，本文为 Codex 对照真实源码和测试修正后的验收版本。

StockManager 是 A 股研究型筛选平台，禁止自动交易；筛选结果仅供研究参考，不构成投资建议。本阶段没有实现 Web、HTTP API 或 HTML 前端，也不代表生产部署已经完成。

## 执行链路

```text
版本 2 模板
  -> parse_template
  -> TemplateCompiler
  -> ScreeningPlan
  -> ParameterizedScreeningService
  -> ScreeningDataPlanner
  -> LocalRepositoryProtocol（只读本地 SQLite）
  -> RuleContext
  -> RuleEngine
  -> CompositionEngine
  -> ParameterizedScreeningResult
```

外部数据边界保持不变：Baostock 等 Provider 只能由 `DataSyncService` 调用。`RuleContext` 只包含预取的领域数据，不包含 Provider、Repository、数据库连接、HTTP 请求或系统时间。

## 规则契约

`stock_manager.rules.base` 定义：

- `ParameterType`：`integer`、`decimal`、`boolean` 和 `text`。
- `WindowUnit`：`calendar_days` 与 `trading_sessions`。
- `ParameterDefinition`：前端可发现的参数 ID、类型、默认值、边界和说明。
- `RuleDefinition`：稳定 `rule_id`、名称、说明和参数列表。
- `RuleDataRequirement`：股票身份、基本面、分红年度及行情窗口需求。
- `RuleContext`：股票、目标交易日、显式复权、数据集元数据、行情、基本面和分红。
- `ScreeningRule`：规则适配器必须实现参数解析、数据需求计算和求值。

`RuleRegistry` 只接受后端显式注册的可信规则，拒绝重复 ID 和未知 ID。模板只引用 `rule_id`，不能提供 Python 导入路径，也不能触发动态模块加载。

`build_default_registry()` 注册八条规则：

- `pe_positive`
- `non_st`
- `dividend_3y`
- `volume_price_5d`
- `limit_up_breakout`
- `limit_up_3m`
- `volatility_multiple`
- `annual_min_volume`

前七条适配器复用 P1 已验收的纯函数，P2 不重写其业务公式。

## 三态执行结果

`RuleStatus` 包含 `PASSED`、`FAILED` 和 `SKIPPED`：

- 启用规则执行后，根据原 `RuleResult.passed` 得到 `PASSED` 或 `FAILED`。
- 只有模板中禁用的规则产生 `SKIPPED`，且其 `RuleExecutionResult.result` 为 `None`。
- 样本不足是规则已经执行后的业务失败，仍为 `FAILED`，不能标记成 `SKIPPED`。

原 `RuleResult` 字段保持不变。`ParameterizedScreeningResult` 另外记录股票名称、逐规则三态结果、模板 ID 和模板 revision，避免破坏 P1 调用方。

## 模板版本 2

默认模板位于 `config/rule_templates/system-default.json`。顶层只允许：

```json
{
  "metadata": {},
  "rules": {},
  "composition": {}
}
```

元数据明确记录 `schema_version`、`template_id`、`revision`、名称、说明、`Asia/Shanghai` 和技术复权。每条规则统一使用 `enabled` 与 `parameters`。组合由顶层 `all`/`any` 和命名规则组组成。

编译器执行以下门禁：

- schema 必须为版本 2，未知字段和错误类型立即失败。
- 时区必须为 `Asia/Shanghai`，复权必须是受支持枚举。
- 规则必须已注册，参数必须严格匹配该规则的强类型契约。
- 至少启用一条规则。
- 每条启用规则必须在组合中恰好出现一次。
- 禁用规则不得进入组合表达式。

默认组合为：基本面组全部满足；信号组任一满足；风险组全部满足。`annual_min_volume` 与 `annual_min_close_price` 位于信号组，与量价信号、炸板或假阴线信号构成 `any`。

## 数据需求规划

每个已配置规则根据强类型参数声明数据需求。`ScreeningDataPlanner` 合并全部启用规则后，计算统一的行情起点、是否读取基本面以及分红起点。

自然日窗口和交易日窗口保持不同语义：`annual_min_volume` 与 `annual_min_close_price` 使用自然日；量价、涨停和波动规则使用交易日条目。服务批量读取本地 Repository 后按股票分组，再构造不可变 `RuleContext`，规则本身不访问存储。

请求复权、模板复权和数据集元数据复权必须一致。目标日必须存在精确数据集元数据和本地交易日历；数据缺失时明确失败，不会偷偷同步。

## 年度最低交易量

真实入口为 `stock_manager.rules.annual_min_volume.evaluate_annual_min_volume`。

语义如下：

- 窗口包含两端：`[target_day - lookback_calendar_days, target_day]`。
- 默认参数为 365 个自然日、至少 120 个有效交易日、排除零成交量。
- 只统计 `is_trading=True` 的行情；排零开启时，零量目标日视为缺失或被排除。
- 目标日成交量等于窗口最小成交量即通过，并列最低允许通过。
- 样本不足返回结构化 `FAILED`，不会把局部样本表述为年度结论。
- `actual_value` 报告 `target_volume`、`minimum_volume`、`minimum_dates` 和 `valid_session_count`。
- 重复交易日、混合股票代码、目标日与元数据不一致和复权不匹配均显式抛出异常。

## 年度最低收盘价

`annual_min_close_price`（真实入口 `stock_manager.rules.annual_min_close_price.evaluate_annual_min_close_price`）是 `annual_min_volume` 的镜像规则，仅把比较字段由成交量改为收盘价，窗口与参数语义逐一对应：

- 窗口包含两端：`[target_day - lookback_calendar_days, target_day]`。
- 默认参数为 365 个自然日、至少 120 个有效交易日、排除零收盘价。
- 只统计 `is_trading=True` 的行情；排零开启时，零价目标日视为缺失或被排除。
- 目标日收盘价等于窗口最小收盘价即通过，并列最低允许通过。
- 样本不足返回结构化 `FAILED`，不会把局部样本表述为年度结论。
- `actual_value` 报告 `target_close`、`minimum_close`、`minimum_dates` 和 `valid_session_count`。
- 重复交易日、混合股票代码、目标日与元数据不一致和复权不匹配均显式抛出异常。

## 模板持久化

`JsonTemplateRepository` 区分系统模板目录和用户模板目录：

- 系统模板只读。
- 模板 ID 仅允许小写字母、数字和连字符，最大 64 个字符。
- 目录或模板文件为符号链接时拒绝操作。
- 保存采用同目录临时文件、`fsync` 和 `os.replace` 原子替换。
- 保存前由 `TemplateCompiler` 完整验证。

`TemplateService` 提供列表、读取、编译、创建、更新和删除。新模板必须从 revision 1 开始；更新和删除要求调用方提供 `expected_revision`，过期 revision 会抛出 `TemplateRevisionConflictError`，更新成功后 revision 加一。

## CLI

P1 命令保持可用。P2 新增：

```text
stock-manager screen-template --db PATH --template PATH [--dataset market]
  --date YYYY-MM-DD --adjustment {unadjusted,qfq,hfq}
  [--code CODE]... [--format {json,summary}]
```

命令严格加载版本 2 模板、编译执行计划并读取已有 SQLite。JSON 输出包含 `rule_executions`、模板 ID 和 revision；`summary` 保持筛选数量及逐股票 PASS/FAIL 格式。

## 新增规则步骤

新增可信后端规则时：

1. 定义不可变强类型参数对象。
2. 提供完整 `RuleDefinition` 参数描述。
3. 严格解析 JSON 参数，拒绝未知字段和错误类型。
4. 声明 `RuleDataRequirement`，不在规则中查询数据库。
5. 使用 `RuleContext` 编写或适配纯计算规则。
6. 在 `build_default_registry()` 显式注册。
7. 在模板中配置开关、参数和唯一组合位置。
8. 添加离线单元测试、执行引擎测试和端到端测试。

新增规则不需要修改 `ParameterizedScreeningService` 或 `CompositionEngine`。如果规则需要当前领域模型和 Repository 不支持的新数据类型，则必须先进行架构决策并扩展本地数据边界，不能绕过 Repository 直接访问 Provider。

## 验收

验收命令：

```text
PYTHONPYCACHEPREFIX=/private/tmp/stockmanager-p2-pycache .venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONPYCACHEPREFIX=/private/tmp/stockmanager-p2-compile .venv/bin/python -m compileall -q src tests
```

结果为 97 个离线测试通过，字节码编译通过。覆盖注册冲突、模板严格校验、组合边界、三态结果、自然日和交易日需求、年度最低量边界、参数化本地筛选、CLI 模板筛选、模板 CRUD、路径安全、原子保存前校验和 revision 冲突。

## 已知边界

- P2 保留版本 1 与版本 2 两条显式入口，没有自动改写用户的版本 1 配置。
- 本阶段模板 Repository 是本地 JSON 文件实现，不包含账户和远程共享。
- 没有运行时第三方插件加载功能。
- 没有实现 Web、HTTP API 或 HTML 前端。
- 本阶段验收为离线功能与契约验收，不等于生产部署、容量测试或长期运行验收。
