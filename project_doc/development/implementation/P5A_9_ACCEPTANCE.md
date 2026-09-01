---
date: 2026-09-02
purpose: 记录 StockManager P5A-9 端到端验收、性能基准、代码自审与文档同步的最终结果。
project: StockManager
status: active
---

# P5A-9 端到端验收、性能调优与文档同步

## 阶段定位

P5A-9 完成 P5A 全套验收:端到端测试(固定小数据)、账本核对、故障注入、性能基准、代码自审、测试矩阵与完成定义逐项核对、文档同步与版本发布(1.9.0)。

## 端到端验收(新增 6 项)

| 测试 | 验证内容 |
|---|---|
| test_e2e_full_loop_via_api | HTTP 层全链路:模板 → 历史信号 → 回测 → 归一化结果(metrics/warnings/provenance,cheat_modes=none) |
| test_ledger_consistency_manual_sample | 逐净值点账本核对:cash + holdings_value == equity |
| test_fault_injection_missing_database | 数据库缺失:提交期 400 或运行期 FAILED,绝不部分成功 |
| test_fault_injection_no_stock_pool | 历史股票池缺失:PIT 校验明确拒绝(non_st 数据警告) |
| test_cancellation_fault_injection | 取消请求:阶段边界安全终止(CANCELLED 或竞态内 SUCCEEDED) |
| test_no_generated_artifacts_in_repo | 仓库无数据库/日志/缓存/构建产物(git status 干净) |

## 性能基准(2026-09-02,真实库一年样本)

scripts/bench_p5a_historical.py --real-db(当前 data/market.sqlite3 一年数据,5200+ 股票):

- worker 1/2/4 × shard 100/250/500 的逐日历史筛选;各配置 result_fingerprint 一致(确定性)。
- 详细 JSON 与原始输出存档:scripts/bench_results/2026-09-02_p5a_historical_real1y.json / .out。
- 八年规模(约 2080 交易日)的收敛结论需八年数据就绪后以同一脚本重跑(命令与格式已固定);本阶段不伪造加速结论。

## 代码自审

- 无 except:pass / 吞异常模式;无硬编码本机绝对路径(全部受控相对路径)。
- backtrader 仅存在于 src/stock_manager/backtest/backtrader_engine.py(适配器边界,子进程断言测试锁定)。
- 进程池仅两处单层使用(screening_shard_executor / historical_screening_executor),无嵌套池。
- 测试全部离线(FixtureProvider/固定 fixture),断网可运行。
- pyproject 移除对不存在 README.md 的 readme 引用(文档唯一存放位置为 project_doc,AGENTS 10.2)。

## 测试矩阵核对(P5A_PLAN 15)

- 规则与时间:空日历/乱序/重复/NaN/None/零量、warm-up、T+1 发布不可见(P5A-2/4 测试)。
- 股票池与 A 股状态:上市前/退市后边界、ST/停牌/涨跌停/整手/余股/T+1/现金不足/部分成交(P5A-2/7)。
- 并发与一致性:worker 1/2/4 与参考一致、shard 边界、失败整体失败、generation 变化检测、确定性 hash(P5A-4)。
- Backtrader 与账本:T 信号最早 T+1 成交、无 cheat、多股票同日调仓、三种退出分支、手工账本核对、unavailable 不冒充 0(P5A-6/7/9)。
- API 与前端:字段边界、未知策略/模板 revision、状态机迁移、取消、刷新恢复、注入拒绝、警告可见(P5A-8/9)。

## 架构门禁回顾(P5A_PLAN 16)

1. Backtrader × Python 3.14 兼容 ✓(P5A-0 PoC,无需受控运行时)。
2. 复权/成交时点已确认 ✓(qfq 统一 + 对策 a;T+1 开盘)。
3. Baostock 契约保持;历史股票池/基本面 PIT 边界按能力显式报告,不补造事实 ✓。
4. 公共 API/规则语义/分层未改变(兼容 v1,全部新功能增量)✓。
5. 八年迁移:幂等/断点恢复/校验报告,现有一年数据未动 ✓。
6. eligibility 并集 feed 加载与内存:一年样本基准已跑;八年样本待数据就绪后按同一门禁复核(脚本就绪)✓。
7. 股票轴并行在一年样本上有效;八年收敛基准脚本就绪 ✓。

## 完成定义核对(P5A_PLAN 17)

1. 模板工作台发起回测 ✓(P5A-8 前端面板)。
2. 八年覆盖/缺口/generation 报告 + 可恢复回补 ✓(P5A-1)。
3. 逐日 point-in-time,无未标记未来函数 ✓(P5A-2/4)。
4. 股票轴并行、worker 内时间顺序、Backtrader 单时间线 ✓(P5A-4/6)。
5. 信号复用与缓存失效规则 ✓(P5A-5)。
6. 首批三个端到端策略完成离线测试 ✓(P5A-6)。
7. A 股执行能力与未支持项显式、可测试、可追溯 ✓(P5A-7)。
8. 结果/订单/持仓可持久化分页展示 ✓(P5A-5/8)。
9. 完整类型标注、无吞异常、无硬编码路径、无业务层网络 ✓(自审)。
10. 单元测试断网可运行,关键边界覆盖 ✓(374 passed)。
11. DeepSeek V4 Flash 完成实现、测试、调试、自审与技术验收 ✓。
12. 文档带 frontmatter 同步 project_doc;Git 提交符合规范、无生成物 ✓。

## 文档交付清单(P5A_PLAN 18)

| 文档 | 位置 |
|---|---|
| P5A_PLAN(状态 active,各阶段验收记录) | development/plan/P5A_PLAN.md |
| Backtrader 选型与许可 ADR(accepted) | development/architecture/ADR_P5A_BACKTEST_ENGINE.md |
| 八年数据种子 ADR(accepted,qfq + 对策 a) | development/architecture/ADR_P5A_SEED_DISTRIBUTION.md |
| 总 ADR 第 11 节 P5A 决策 | development/architecture/ADR_OVERVIEW.md |
| P5A-1 八年覆盖实现文档 | development/implementation/P5A_1_HISTORY_V2.md |
| P5A-2 PIT 契约实现文档 | development/implementation/P5A_2_PIT_READER.md |
| P5A-3 策略规格实现文档 | development/implementation/P5A_3_STRATEGY_SPEC.md |
| P5A-4 历史筛选实现文档 | development/implementation/P5A_4_HISTORICAL_SCREENING.md |
| P5A-5 运行存储实现文档 | development/implementation/P5A_5_RUN_STORAGE.md |
| P5A-6 适配器实现文档 | development/implementation/P5A_6_BACKTEST_ADAPTER.md |
| P5A-7 执行模型实现文档 | development/implementation/P5A_7_EXECUTION_MODEL.md |
| P5A-8 研究 API 实现文档 | development/implementation/P5A_8_RESEARCH_API.md |
| 本报告(P5A-9) | development/implementation/P5A_9_ACCEPTANCE.md |

全部文档含 date/purpose/project/status frontmatter;回测用户手册与风险说明以各实现文档「说明与后续」与免责声明覆盖,不另建重复文档。

## 版本与 Git

- 版本 1.8.0 → **1.9.0**(P5A 研究功能完整交付,三处同步:pyproject.toml / src/stock_manager/__init__.py / tests/test_package_structure.py)。
- 提交历史:P5A-0…P5A-9 每阶段独立 Conventional Commits,无数据库/日志/Excel/缓存/IDE 文件/构建产物入库。
- 全量测试:**374 passed**(P5A 累计新增 108 项,基线 266 项零回归)。

## 追加修复:Baostock 会话失效自动恢复(v1.9.1,2026-09-01)

**现象**:全市场同步进行中(如 08-31 交易日)查询报 `query_fundamentals(sh.600517) failed: 用户未登录` → 同步 FAILED。根因:Baostock 长时间会话被服务端失效后,provider 只对失效结果盲目重试,不重新登录,3 次重试后必然失败。

**修复**(src/stock_manager/providers/baostock_provider.py):

- 新增 `_is_session_expired` 检测(error_code != 0 且消息含「未登录」/「not logged」)。
- `_retry` 增加 `relogin` 回调:检测到会话失效时重新登录后继续重试,不再在死会话上耗尽。
- `_session` 暴露 relogin 回调,四个 fetch 方法全部透传;重登录失败抛 `BaostockProviderError`(不吞);重试耗尽仍失败时保留最后失败结果,由 `_rows` 抛明确业务错误。
- 回归测试 3 项(离线 fake client):会话失效一次自动恢复且 login 计数为 2、持续失效明确失败、失效检测边界。
- 实测验证:修复后 `fetch_fundamentals(('sh.600517',), 2026-08-31)` 正常返回(login success → 1 行 → logout success)。
- 版本 1.9.0 → 1.9.1;全量 pytest **377 passed**(新增 3 项)。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。
