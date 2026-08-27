---
date: 2026-08-25
purpose: 说明 ScreeningService 的本地编排边界及 screen、sync、status、smoke 命令契约。
project: StockManager
status: active
---

# P1-6 筛选服务与 CLI

## 安全边界

StockManager 是 A 股研究型筛选平台，禁止自动交易，所有筛选结果仅供研究参考，不构成投资建议。

`screen`、`status` 和 `ScreeningService` 只能读取本地 SQLite。`sync` 与显式联网诊断命令 `smoke` 会构建 `BaostockProvider`，且 Provider 调用仍全部委托给 `DataSyncService`。筛选路径不持有 Provider，断网时仍可筛选已经成功同步的数据。

## ScreeningService 契约

精确导入路径：`stock_manager.services.screening_service.ScreeningService`。

```python
ScreeningService(
    repository: LocalRepositoryProtocol,
    rules_config: RulesConfig,
) -> None

screen(
    dataset_id: str,
    trading_day: date,
    adjustment: AdjustmentMethod,
    codes: Sequence[str] = (),
) -> tuple[ScreeningResult, ...]
```

服务首先校验非空数据集 ID、请求复权与规则配置一致、目标日精确元数据存在，以及目标日在本地交易日历中。随后批量读取股票、行情、基本面和分红数据，并按代码排序输出。指定代码不存在时抛出 `StockNotFoundError`；目标数据集或交易日历不可用时抛出 `DatasetUnavailableError`；复权不一致时抛出 `ValueError`。

每只股票依次包含以下规则结果：`pe_positive`、`non_st`、`dividend_3y`、`volume_price_5d`、`limit_up_breakout`、`limit_up_3m`、`volatility_multiple`、`composite`。`ScreeningResult` 包含 `code`、`trading_day`、`passed`、`rule_results`、`metadata`；每个 `RuleResult` 包含 `rule_id`、`passed`、`actual_value`、`threshold`、`reason`。元数据明确记录数据集、交易日、来源、同步时间和复权方式。

## CLI 契约

入口为 `stock_manager.cli.main.main`，安装后的命令名为 `stock-manager`。

```text
stock-manager screen --db PATH --rules PATH [--dataset market]
  --date YYYY-MM-DD --adjustment {unadjusted,qfq,hfq}
  [--code CODE]... [--format {json,summary}]

stock-manager sync --db PATH --config PATH --lock-dir PATH [--dataset market]
  --date YYYY-MM-DD --adjustment {unadjusted,qfq,hfq} [--retry]

stock-manager status --db PATH --lock-dir PATH [--dataset market]
  --adjustment {unadjusted,qfq,hfq}

stock-manager smoke --config PATH --code CODE --date YYYY-MM-DD
  --adjustment {unadjusted,qfq,hfq}
```

- `screen`：只读本地数据；`--code` 可重复，不提供时筛选本地股票全集。
- `sync`：调用 `DataSyncService.sync(...)`；重复成功同步会显示“数据已存在，跳过拉取”，不产生新的 Provider 请求；失败后的重试必须显式指定 `--retry`，并遵守冷却时间。
- `status`：只读最新同步记录、指定复权下的最新元数据，以及对应持久化文件锁当前是否被占用。
- `smoke`：显式联网诊断，在临时 SQLite 和锁目录中对单只股票调用所有 Provider 接口；不写入正式数据库，输出结构化 JSON。它不是默认离线测试的一部分。

JSON 输出中，日期和时间使用 ISO 格式，`Decimal` 使用字符串，枚举使用其值。`summary` 首行输出筛选总数、通过数、失败数，后续逐代码输出 `PASS` 或 `FAIL`。已知业务、配置、Provider、SQLite 和文件错误会以 JSON 写到 stderr，并返回退出码 1。

复权参数必须显式提供，不设业务默认值。
