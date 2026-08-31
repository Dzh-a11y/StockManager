---
date: 2026-09-02
purpose: 记录 StockManager P5A-8 研究回测异步 API、有界 job runner 与统一前端工作台接入的实现与离线验收结果。
project: StockManager
status: active
---

# P5A-8 异步 API 与统一前端工作台

## 阶段定位

P5A-8 在现有工作台完成「模板 → 回测配置 → 后台任务 → 结果」闭环:研究 backtest API(提交/状态/取消/净值/订单/provenance)、本地有界 job runner(默认单任务)、模板页回测入口与结果展示、刷新后通过持久化 run 状态恢复。

## 新增/修改模块

```text
src/stock_manager/domain.py                              # +RUNNING_BACKTEST/NORMALIZING 状态
src/stock_manager/services/job_runner.py                # 新增:BoundedJobRunner(单 worker、按 key 去重、取消)
src/stock_manager/services/research_backtest_service.py # 新增:submit/execute/查询;eligibility 缓存复用
src/stock_manager/storage/sqlite_repo.py                # +backtest_runs/backtest_orders/backtest_equity
src/stock_manager/services/historical_screening_run_store.py # +start_backtest/start_normalizing
src/stock_manager/web/app.py                            # +研究命名空间 API
src/stock_manager/web/static/index.html                 # +研究回测面板
src/stock_manager/web/static/app.js                     # +提交/轮询/结果渲染
tests/test_research_api.py                              # API 契约(7 项)
```

## API(研究命名空间)

| 端点 | 语义 |
|---|---|
| POST /api/research/backtests | 提交:{template_id, template_revision, strategy_spec_id, backtest_start/end, initial_cash, max_positions} → 202 {run_id} |
| GET /api/research/backtests/{run_id} | 状态/进度/metrics(成功后)/warnings/error |
| POST /api/research/backtests/{run_id}/cancel | 请求取消 → {cancel_requested} |
| GET /api/research/backtests/{run_id}/equity | 净值序列(分页) |
| GET /api/research/backtests/{run_id}/orders | 订单与成交(分页) |
| GET /api/research/backtests/{run_id}/provenance | 引擎/政策/cheat 模式来源 |
| GET /api/research/backtests | run 列表 |

服务端重新读取模板并校验 revision(不信任浏览器副本);未知策略 ID、revision 不匹配、参数错误返回 400;未知 run 返回 404。

## 有界 Job Runner

BoundedJobRunner:单 worker 线程 + FIFO 队列(默认 max_concurrent=1,禁止「多任务进程池 × 每任务股票池」嵌套);同一 job key 在 queued/running 时重复提交抛 DuplicateJobError(防重复点击);cancel 置标志,任务在阶段边界检查并安全终止(CANCEL_REQUESTED → CANCELLED)。

## 执行流

submit:模板重读 → 编译 → plan_fingerprint → 策略规格构建与政策校验 → cache_key(generation/模板/规则版本/窗口等)→ 创建 QUEUED run → 提交 job。_execute:VALIDATING(generation 与规则能力校验)→ BUILDING_SIGNALS(缓存命中则从 eligibility_days/members 重建时间线,否则 HistoricalScreeningExecutor 生成并落库)→ RUNNING_BACKTEST(PIT 读市场数据 → BacktraderBacktestEngine)→ NORMALIZING(metrics/orders/equity 落库)→ SUCCEEDED;任何异常 FAILED(含原因);阶段边界检查取消。

## 前端

「研究回测」面板:策略下拉(三内置)、起止日期、初始资金、最大持仓、提交按钮;提交后轮询状态与进度;成功后渲染 metrics 摘要(净值/收益/回撤/Sharpe/成交/费用)+ 执行限制警告列表,可展开净值与订单明细;失败与取消可见;刷新后由 run_id 状态恢复(持久化状态机)。

## 离线测试验收

python3 -m pytest:**368 passed**(P5A-8 新增 7 项;无回归)。覆盖:提交→SUCCEEDED→equity/orders/provenance 数据、二次提交复用缓存 eligibility(不同策略规格)、未知策略 400、模板 revision 不匹配 400、取消请求、run 列表、未知 run 404;既有 Web 契约 33 项无回归。

## 说明与后续

- 默认 worker 数在 Web 构造时为 2(有界);真实八年规模运行建议按机器内存调整。
- P5A-9 将执行全套 E2E/回归/性能验收与文档最终同步。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。

