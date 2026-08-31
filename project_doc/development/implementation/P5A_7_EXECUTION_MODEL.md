---
date: 2026-09-02
purpose: 记录 StockManager P5A-7 A 股执行与成交语义(整手、T+1、停牌、涨跌停、费用、滑点、现金与拒单)的实现与离线验收结果。
project: StockManager
status: active
---

# P5A-7 A 股执行与成交语义

## 阶段定位

P5A-7 把已确认的 A 股完整约束组合(决策 3)做成可测试执行政策:100 股整手与余股、T+1 卖出限制、停牌不可成交、涨跌停不可买/卖、佣金最低收费、印花税、过户费、滑点、现金不足与部分成交政策;每个执行限制产生显式结果警告与来源,未实现能力不静默降级。

## 新增/修改模块

```text
src/stock_manager/backtest/execution.py        # 新增:ExecutionParameters/ExecutionDecision/
                                               #  decide_buy/decide_sell/费用/涨跌停/整手纯函数
src/stock_manager/backtest/backtrader_engine.py# AShareCommission + 策略接入执行模型,
                                               #  prev_close 维护、逐日 equity_curve、限制警告
src/stock_manager/backtest/contracts.py        # BacktestMarketData +stocks(IS_ST 判定)
tests/test_execution_model.py                  # 纯函数执行模型(10 项)
tests/test_backtest_adapter.py                 # +涨停/停牌端到端警告(2 项)
```

## 执行模型语义(纯函数,可单测)

- 整手:买入股数向下取 100 股整手;余股不足一手拒绝;partial-fill 政策下按现金可担的整手缩减。
- T+1:卖出要求 bought_day < today(同日买入不可卖;实际成交总在 T+1,跨日可卖由 held_since 记录成交日判定)。
- 停牌:无 bar 或 volume=0 / is_trading=False 视为停牌,买入与卖出均拒绝。
- 涨跌停:涨停价 = preclose x (1 + ratio) 四舍五入到分;ST 5%、主板 10%;收盘触及涨停不可买、跌停不可卖。
- 费用:佣金(rate 与最低 5 元取大)+ 卖出印花税 + 双边过户费(十万分之一),全部 Decimal 精确计算。
- 滑点:slippage_rate 调整买入预算价(参数化,默认 0)。
- 现金:买入总成本(含费用)超过现金拒单;partial-fill 政策下按可担整手成交。
- 每次拒绝产生 "execution: <reason>" 警告,来源可追溯。

## 适配器接入

- AShareCommission(bt.CommInfoBase):佣金最低收费、卖出印花税、过户费,broker 实际扣费。
- 策略 next():先按退出政策计算退出候选,再经 decide_sell(T+1/停牌/跌停)过滤;买入经 decide_buy(整手/涨停/停牌/现金/费用)过滤;每个被拒决策写入 warnings。
- prev_close 由策略逐日维护(前收盘),用于涨跌停判定;IS_ST 状态由调用方经 BacktestMarketData.stocks 传入。
- 逐日 equity_curve 归一化:next() 记录 (day, equity, cash, holdings_value)。
- 时间语义不变:T 日收盘决策,市价单 T+1 开盘成交,cheat 模式全部关闭;涨跌停判定使用 T 日收盘价(一字板近似,文档注明)。

## 离线测试验收

python3 -m pytest:**361 passed**(P5A-7 新增 12 项;无回归)。覆盖(固定 fixtures):整手取整与余股、资金不足拒单、部分成交政策(可担整手缩减、不足一手仍拒)、涨停不可买、跌停不可卖、停牌/零成交量不可成交(含端到端警告断言)、T+1 同日卖出拒绝与次日放行、ST 5% 与主板 10% 边界、最低佣金与印花税/过户费、零值费用、缺失 bar 拒绝。

## 说明与后续

- 涨跌停判定基于 T 日收盘价(一字板近似);T+1 开盘精确触碰判断需逐 bar 撮合钩子,当前为文档化近似,不冒充真实撮合。
- 停牌日由 bar 缺失/零量推导;Baostock 停牌日无行,Backtrader 数据对齐使停牌期订单在复牌开盘成交(符合 A 股语义)。
- P5A-8 将把结果 warnings、费用与执行限制展示到统一工作台。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。

