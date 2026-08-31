---
date: 2026-09-02
purpose: 记录 StockManager P5A-2 point-in-time 数据读取契约、规则历史能力标签与反未来函数离线验收结果。
project: StockManager
status: active
---

# P5A-2 point-in-time 数据读取契约

## 阶段定位

P5A-2 保证历史回测的每个评估日 T 只能看到 T 日当时可知的信息:历史股票池、基本面发布日、防未来函数读取、不可变 dataset generation 绑定。不改变现有单日 Reader 与参数化筛选路径。

## 已确认决策(2026-09-01)

- 成交时点:T 日收盘生成信号,最早 T+1 开盘成交(见 P5A_PLAN 第 1.1 节)——本阶段所有 as-of 边界按"通过 T 可知"实现。
- 复权统一 qfq;PIT 读取按 request.adjustment 过滤 daily_bars。

## 新增/修改模块

```text
src/stock_manager/read/historical.py              # 新增:PointInTimeRequest/
                                                  #  PointInTimeReaderProtocol/
                                                  #  SQLitePointInTimeReader(只读)
src/stock_manager/rules/base.py                   # +RulePitCapability 枚举;RuleDefinition
                                                  #  新增 pit_capability 字段(默认 PIT_UNSUPPORTED)
src/stock_manager/rules/builtin.py                # 12 条内置规则显式声明能力
src/stock_manager/rules/historical_capability.py  # 新增:能力校验器与两类业务错误
src/stock_manager/read/__init__.py                # 导出补齐

tests/test_pit_reader.py                          # 反未来函数/股票池边界/指纹(10 项)
tests/test_rule_capability.py                     # 能力标签与校验器(5 项)
```

## PIT 读取契约

`PointInTimeRequest(dataset_id, codes, start, end, adjustment)`:不可变读取意图;codes 为空表示全市场(股票池查询语义),bars/fundamentals/dividends 有 codes 时按 codes 过滤;请求窗口外查询明确失败。

`PointInTimeReaderProtocol`(数据库无关,无 SQL 泄露):

| 方法 | 语义 |
|---|---|
| universe_as_of(day) | 股票池:取 as_of <= day 的**最近快照**,过滤 listed_on <= day 且 (delisted_on 为空或 > day);当日精确上市/退市边界 |
| bars_through(day) | trading_day <= day 且 >= start 的日线(按 adjustment) |
| fundamentals_through(day) | published_on <= day 的基本面(真实发布日,防未来函数核心) |
| dividends_through(day) | ex_date <= day 的分红 |
| trading_days(start, end) | 本地交易日历 |
| committed_generation() | 最新 COMPLETE 数据集 generation(P5A-1 提交) |
| data_fingerprint() | generation + 各类型 coverage 边界的稳定摘要;数据变化必变 |

`SQLitePointInTimeReader`:只读 URI(mode=ro)+ query_only;close 幂等;关闭后使用显式失败。

## 规则历史能力标签

`RulePitCapability`:PRICE_VOLUME_PIT_READY / FUNDAMENTAL_PIT_READY / UNIVERSE_STATE_PIT_READY / PIT_UNSUPPORTED。RuleDefinition 新增 `pit_capability` 字段,**默认 PIT_UNSUPPORTED**(安全默认:能力必须显式声明,不得静默降级)。

12 条内置规则声明:

- FUNDAMENTAL_PIT_READY:pe_positive(published_on 数据可用时)。
- UNIVERSE_STATE_PIT_READY:non_st(依赖历史股票快照的 ST 状态)。
- PRICE_VOLUME_PIT_READY:其余 10 条(volume_price_5d/limit_up_breakout/annual_min_volume/annual_min_close_price/limit_up_3m/volatility_multiple/consecutive_up_days/n_day_close_above/volume_sum_extreme/price_range_ratio)。

## 历史能力校验器

`HistoricalCapabilityValidator.validate(plan, reader, *, dataset_id, adjustment)`:

- 汇总每条启用规则的能力;PIT_UNSUPPORTED → rejected_rules,整体不 ready。
- FUNDAMENTAL_PIT_READY 规则 → 校验 fundamentals 有 published_on 数据;UNIVERSE_STATE_PIT_READY 规则 → 校验 stocks 快照存在;缺失 → data_warnings。
- `require_ready` 抛 `UnsupportedRuleForHistoricalRunError`(明确列出被拒规则)或 `HistoricalCapabilityError`(缺失数据)。禁止静默改用当前值。

## 反未来函数离线验收

fixture 构造(全部离线):A(2019-01-02 上市,未退市)、B(2020-01-15 上市)、C(2019-06-03 上市,2019-12-31 退市);基本面 published_on=2020-01-16;bars 首根 2020-01-15;分红 ex_date=2020-01-20;股票快照 as_of=2019-12-01 与 2020-01-10 各一份。

验证:

- B 在 2020-01-14 不可见,2020-01-16 可见(T+1 上市)。
- C 在 2019-12-01 可见,2020-01-14 不可见(退市后消失;退市日前仍可进入股票池,消除幸存者偏差)。
- published_on=2020-01-16 的基本面在 2020-01-15 查询为空,01-16 起可见。
- bars_through(2020-01-14) 为空,01-15 起可见;分红同理。
- 窗口外查询拒绝;codes 过滤正确;关闭后使用拒绝。
- generation 绑定:未提交为 None;提交后 committed_generation 返回;fingerprint 随 generation/coverage 变化且同状态稳定。

## 离线测试验收

`python3 -m pytest`:**313 passed**(P5A-2 新增 15 项;P5A-1 298 项与基线 266 项无回归)。

## 说明与后续

- 历史 ST 状态语义:universe_as_of 使用最近快照的 is_st 值(P5A-4 执行时在文档与结果 provenance 中注明快照近似性);精确逐日 ST 序列依赖未来 Provider 能力评估,超出当前数据契约。
- 停牌日推导(volume=0 / is_trading=false)与涨跌停约束由 P5A-7 消费 bars_through 数据实现。
- 历史筛选执行器(P5A-4)将把 request 绑定 committed_generation 并在运行前后比对 data_fingerprint,检测数据被同步进程改变。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。
