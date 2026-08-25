---
date: 2026-08-25
purpose: 记录 P1-7 离线端到端与真实 Baostock 联网冒烟验收结果。
project: StockManager
status: active
---

# P1-7 端到端验收

## 验收结论

新内核已经通过离线端到端验收和真实 Baostock 联网冒烟验收：数据只能经 `DataSyncService` 从可替换 Provider 进入本地 SQLite，筛选服务随后只读本地数据。该结论不表示生产部署已经完成。

StockManager 是 A 股研究型筛选平台，禁止自动交易，筛选结果仅供研究参考，不构成投资建议。

## 已验证场景

- 应用启动检查交易日历，并按 `Asia/Shanghai` 和配置化截止时间补齐缺失交易日。
- 同一数据集和交易日同步成功后，再次请求发出用户可见警告并直接返回本地元数据，不再调用 Provider。
- 进程锁和持久化文件锁共同阻止同一数据集、交易日的并发重复拉取。
- Provider 或负载校验失败会持久化 `FAILED`，不会留下部分市场快照。
- 失败记录必须显式重试，并受冷却时间限制。
- 使用 `FixtureProvider` 完成“启动补齐 → 重复同步跳过 → Provider 故障注入 → 本地筛选仍成功”的完整链路；测试全程未联网。
- CLI 的筛选 JSON、状态 JSON、summary 与错误出口保持稳定、可解析。
- 每个筛选数据集都携带来源、同步时间和明确复权方式；技术规则使用配置中指定的复权方式，不假定默认值。
- 真实 Baostock 登录、退出、交易日历、股票全集、前复权日线、基本面和分红返回均已完成单股票契约冒烟。

## 真实 Baostock 冒烟结果

联网冒烟只通过 `DataSyncService` 调用 Provider，并使用临时 SQLite 和临时锁目录，不写入正式市场数据库。

`DataSyncService.sync_trading_calendar(...)` 对 2026-08-18 至 2026-08-25 的在线交易日历同步成功，返回 6 个交易日。随后使用以下有界接口验证全部 Provider 返回类型：

```python
smoke_test_provider(
    code: str,
    trading_day: date,
    adjustment: AdjustmentMethod,
) -> ProviderSmokeOutcome
```

2025-09-19、`sh.600000`、`qfq` 的真实结果为：交易日 1、股票全集 5663、日线 1、基本面 1、分红 4；Baostock 五次会话均成功登录并正常退出。

2026-08-25 的首次全接口尝试被股票全集校验拒绝，因为该日期返回的在线股票集合不含目标代码；改用已知存在历史行情的 2025-09-19 后通过。这一行为表明冒烟测试会显式暴露不同线上接口的数据覆盖差异，不会把部分返回误报为成功。

## 验收命令与结果

```text
PYTHONPYCACHEPREFIX=/private/tmp/stockmanager-pycache .venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q -p no:cacheprovider
```

清理旧架构前的完整迁移验收结果为 `82 passed`。移出直接执行旧源码的特征测试并增加 `smoke` CLI 回归测试后，当前精简仓库的编译检查通过，现行新内核测试套件为 `65 passed`。耗时会随本机负载波动，不作为验收条件。

端到端场景位于 `tests/test_e2e.py`，其余规则、Provider、存储、同步、并发、重试、服务和 CLI 分支由对应离线测试覆盖。P1-3 固定 fixture 仍在仓库中；直接导入旧 `condition/` 的特征测试已随旧架构归档。

## 剩余边界

- 真实联网仅为单股票、单交易日的契约冒烟，不等同于全市场批量同步的容量、耗时和长期稳定性测试。
- 未执行生产环境部署、长期运行或系统级调度测试；当前自动补齐只依赖应用启动检查。
- 本阶段未创建 Git commit。
