---
date: 2026-08-25
purpose: 记录 P1-4 纯函数规则接口、配置语义及相对旧规则的明确变化。
project: StockManager
status: active
---

# P1-4 纯函数规则迁移

## 架构结果

新规则位于 `stock_manager.rules`，只接收领域对象、阈值和数据集元数据，返回 `RuleResult`。规则不访问网络、数据库、文件、系统时间或日志，不修改输入数据。旧 `condition/` 代码在 P1-3 行为固定和 P1-4 至 P1-7 验收完成后已移至仓库外项目归档；新筛选内核不依赖它。

技术规则必须接收 `DatasetMetadata` 和预期 `AdjustmentMethod`。复权不匹配会抛出明确的 `ValueError`，不会被转换成普通筛选失败。行情会按交易日排序，只统计 `is_trading=True` 的有效交易条目；重复交易日、混合股票代码和晚于数据集元数据的行情均被拒绝。

## 规则接口

- `evaluate_pe_positive`：PE TTM 必须存在并严格大于配置下限。
- `evaluate_non_st`：使用标准化 `StockIdentity.is_st`。
- `evaluate_dividend_3y`：统计最近三个已完成自然年度内的分红记录，不联网。
- `evaluate_volume_price_5d`：最近五个交易条目中，同一相邻日对同时满足量比和收盘涨幅。
- `evaluate_limit_up_breakout`：最近交易窗口内满足配置化涨停区间及炸板最高价条件，或者出现假阴线。
- `evaluate_limit_up_3m`：最近配置数量的交易条目内，涨停事件数位于闭区间。
- `evaluate_volatility_multiple`：最高价/最低价不超过配置上限，并要求最小样本数。
- `evaluate_composite`：基本面规则全部通过；量价或炸板/假阴线至少一项通过；三月涨停与波动规则全部通过。

## JSON 配置

`config/rules.json` 保存版本、`Asia/Shanghai`、技术数据复权和全部阈值。数值比例使用十进制字符串，避免二进制浮点误差。`load_rules_config` 将 JSON 严格转换为不可变配置对象；错误版本、时区、复权枚举或字段类型会显式失败。

默认技术复权为 `qfq`，来源是 P1-3 已固定的旧技术入口 `adjustflag=2`，并非系统级隐式默认。调用方仍必须把数据集实际复权方式传入规则。

## 对 P1-3 歧义的处理

- PE 缺失不再自动通过，而是明确失败。
- ST 规则统一返回 `RuleResult`，不再返回二元组。
- 三年分红定义为最近三个已完成自然年度，至少一条记录。
- 五日和三月窗口按有效交易条目计数，不依赖运行机器时间。
- 三月涨停要求至少一次，最多三次；零次不再通过，三次允许通过。
- “三倍”名称不再保留；波动规则名称与默认上限 2 一致，等于上限时通过。
- 涨停比例区间和固定 `0.03` 回落价差暂时保留为显式配置，后续可在不改接口的情况下按板块、ST 和制度版本扩展。
- 异常不再被吞掉并伪装成普通筛选失败。

P1-3 测试继续验证旧行为；P1-4 测试验证新契约。两者用途不同，不能互相覆盖。

## 验收

执行：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONPYCACHEPREFIX=/tmp/stockmanager-p1-4-pycache .venv/bin/python -m compileall -q src tests
```

结果：`51 passed`，字节码编译通过。测试完全离线。
