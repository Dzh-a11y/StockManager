---
date: 2026-09-02
purpose: 记录 StockManager P5A-6 Backtrader 适配器、通用组合策略与端到端研究回测的实现与离线验收结果。
project: StockManager
status: active
---

# P5A-6 Backtrader 适配器与通用组合策略

## 阶段定位

P5A-6 把历史资格时间线送入 Backtrader,形成第一个端到端研究回测:BacktestEngine 契约(纯 Domain)、Backtrader 适配器(唯一可导入 bt 的模块)、单一受控 StockManagerPortfolioStrategy、eligibility 并集 feed 加载、analyzer 结果归一化。

## 新增模块

```text
src/stock_manager/backtest/__init__.py            # 公共导出
src/stock_manager/backtest/contracts.py           # BacktestEngine Protocol/BacktestMarketData/
                                                   #  BacktestMetrics/BacktestResult/BacktestTrade/
                                                   #  EquityPoint/两类错误(无 bt 导入)
src/stock_manager/backtest/policies.py            # 纯决策函数:排名/等权/三种退出(无 bt)
src/stock_manager/backtest/backtrader_engine.py   # StockManagerPortfolioStrategy +
                                                   #  BacktraderBacktestEngine(唯一 bt 依赖)
tests/test_backtest_adapter.py                    # 端到端/T+1/确定性/隔离/异常(9 项)
```

## 时间语义(T+1 成交)

策略在 next()(data0 = 交易日历 dummy feed,每个交易日触发)读取当日资格时间线;订单为 T 日收盘发出的市价单,Backtrader 默认在下一 bar 开盘成交(cheat-on-close/open 全部关闭)。测试断言:资格自 DAYS[5] 出现时,所有买入成交日严格 > DAYS[5]。

## 数据 feed

- data0 为交易日历合成 feed(全市场日历,保证 next() 每个交易日触发)。
- 股票 feed 只加载**资格并集**(曾入选代码),每代码一个 PandasData;停牌缺失的交易日由 Backtrader 数据对齐语义自然处理(下一可用 bar 开盘成交)。
- 输入为 BacktestMarketData(PIT 读取的 domain 对象),适配器内部转 DataFrame,不向 Services/Domain 泄露 bt 类型。

## 策略政策桥接(P5A-6 首批)

| 政策 | 行为 |
|---|---|
| eligibility_enter_v1 | 资格名单出现即加入候选 |
| eligibility_exit_v1 | 资格失效即卖出 |
| sma_timing_v1 | 收盘 < SMA(N) 退出(bt.ind.SMA) |
| fixed_holding_v1 | 进入后持有 N 交易日退出 |
| daily_v1 | 每日收盘后重算目标组合 |
| equal_weight_v1 | 目标市值 = 净资产×(1-reserve)/max_positions |
| turnover_20d_desc_v1 | 近 N 日平均成交额降序 + 代码升序同分键(测试断言排序) |
| ashare_execution_v1 | 本阶段为市价单语义;完整 A 股执行由 P5A-7 接管 |

## 结果归一化

Returns/DrawDown/SharpeRatio/TradeAnalyzer 输出在适配层解析为 BacktestMetrics(initial/final、total/annualized return、max drawdown、sharpe、成交胜负、总费用),unavailable 指标显式列表(如数据不足的 sharpe)而非 0;订单与成交转为 BacktestTrade(filled/rejected + 原因);provenance 记录引擎版本、政策清单与 cheat_modes=none。

## 离线测试验收

python3 -m pytest:**349 passed**(P5A-6 新增 9 项;无回归)。覆盖:端到端回测(两个股票 20 交易日 + 资格时间线)、T+1 成交语义断言、资格失效卖出、重复运行 metrics/trades 完全一致、SMA 择时与固定持有策略运行、调整不匹配 BacktestInputError、contracts/research 导入不产生 backtrader 模块(子进程断言)、排名策略确定性排序。

## 说明与后续

- 本阶段执行模型为市价单 + Broker 默认费用(P5A-7 将注入完整 A 股执行:整手、T+1 可卖、停牌/涨跌停、佣金/印花税/过户费、滑点、现金不足与部分成交,并报告每个执行限制)。
- equity_curve 归一化(逐日净值序列)在 P5A-7 随执行模型一并补齐(当前返回空曲线,metrics 已归一化)。
- 并集接近全市场时的启动/内存基准属 P5A-9 性能门禁(脚本已在 P5A-0 就绪)。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。

