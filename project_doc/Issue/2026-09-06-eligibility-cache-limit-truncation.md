---
date: 2026-09-06
purpose: 记录回测资格历史筛选缓存命中路径截断资格日为前 100 天（默认 limit）导致结果错误的代码缺陷及其修复
project: StockManager
status: closed
---

# Issue：回测资格缓存命中路径截断资格日（list_eligibility_days 默认 limit=100）

- 提出日期：2026-09-06
- 提出场景：运行端（bt_annret 回测搜索，Alpha_Mining_V0/2026_09_05/2026_09_01/bt_annret，隔离快照 market_snapshot_20260905.sqlite3）
- 客户/研究意图：同筛选模板 + 同策略的回测结果必须确定可复现，缓存命中不得改变结果。
- 能力缺口/缺陷描述：`stock_manager/services/research_backtest_service.py` `_build_eligibility` 缓存命中分支调用 `self._repository.list_eligibility_days(cached.run_id)` 未传 `limit`；`stock_manager/storage/sqlite_repo.py::list_eligibility_days` 签名默认 `limit=100`。评价窗口 127 个交易日时缓存命中路径只读回前 100 天资格（末 27 天无资格→强制清仓），而全新自算路径（`HistoricalScreeningExecutor` → `_StaticTimeline`）完整 127 天。实测：同模板 r1-c07-trend-floor 自算 run members=30026、年化 −0.0124、34 笔；缓存命中 run members=0（数据存于被命中 run）、年化 −0.0422、58 笔；MDD 相同(0.1254)但净值/交易不同。三次提交探针：自算、命中、自算交替出现（命中失败因被缓存 run 自身 members=0 → 又自算），自算两次结果完全一致（可复现），命中那次不同（缺陷结果）。
- 影响：凡缓存命中路径产生的结果都不可信（等价于用前 100/127 天资格回测）；组合搜索/对比中同模板跨轮复用会静默得到错误对比。
- 修复建议（未实现，code/ 只读）：缓存命中分支显式传足够 limit（如 `limit=max_days`）或 repo 层 `list_eligibility_days(run_id, limit=None)` 表示全量；建议补回归测试：同 cache_key 第二次提交与首次自算指标一致。
- 研究侧规避（本次运行已采用，未改 code/）：每次 submit 前清空快照上的 `eligibility_members/eligibility_days/historical_screening_runs`（`.alpha_search_runs/bt_round_driver.py::clear_eligibility_cache`），强制每次自算，保证确定性。

## 修复（2026-09-06，已合入 code/）

采用「repo 层 `limit=None` 表示全量」修法（用户确认，较 `max_days` 方案更轻）：

- `sqlite_repo.py::list_eligibility_days`：`limit` 改为 `int | None`，默认 `None`=全量（SQL `LIMIT -1`，与 `prune_historical_runs` 同约定）；显式 `limit` 仍分页；`limit<=0` 拒绝。
- `research_backtest_service.py::_build_eligibility` 缓存命中分支：显式 `list_eligibility_days(cached.run_id, limit=None)`，读完整资格日。
- 回归测试（红→绿验证通过）：
  - `tests/test_historical_runs.py::test_list_eligibility_days_returns_more_than_one_hundred_by_default`：127 日窗口默认全量返回 127；显式 `limit=100` 截断 100；`offset=100, limit=None` 返回 27。
  - `tests/test_historical_runs.py::test_backtest_cache_hit_reads_full_eligibility_window`：127 日资格 + 同 cache_key 复用，缓存命中路径返回 127 snapshots（缺陷态默认 100 时该测试失败=红，修复态通过=绿）。
- 验证：`pytest tests/` 737 通过（含上述新增 2 项）。
