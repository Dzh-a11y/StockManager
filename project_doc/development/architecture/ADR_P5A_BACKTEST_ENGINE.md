---
date: 2026-09-01
purpose: 记录 StockManager P5A 采用 Backtrader 作为回测引擎的决策、GPLv3 许可影响、适配器边界与不做时间轴并行的原因。
project: StockManager
status: active
---

# ADR：P5A 回测引擎选型（Backtrader）、许可影响与适配器边界

## 状态

已接受（accepted）：用户于 2026-09-01 确认采用 Backtrader（P5A_PLAN.md 第 1.1 节第 1 项）；P5A-0 依赖 PoC 已于 2026-09-01 在 Python 3.14.7 上验证通过。

## 背景

P5A（development/plan/P5A_PLAN.md）要求把现有筛选工作台扩展为「历史信号生成 → 组合回测 → 结果展示」研究闭环。候选引擎为 Backtrader 与 bt，用户已确认采用 **Backtrader**。本 ADR 记录：

1. 选型理由与版本事实；
2. GPLv3 许可影响与本项目义务边界；
3. 适配器边界（Backtrader 只出现在适配层）；
4. 不做时间轴并行的原因（组合状态依赖前一时点）。

## 决策

### 1. 引擎：Backtrader 1.9.78.123

- 版本事实（2026-09-01 实测，Apple M5 Pro / macOS 26.6.2 / Python 3.14.7 / pandas 2.3.3 / numpy 2.5.2 / SQLite 3.50.4）：
  - pip install backtrader 安装 1.9.78.123（py2.py3-none-any wheel）成功；
  - import backtrader 成功，**Python 3.14 兼容性验证通过**，无需受控运行时方案（P5A_PLAN 第 16 节门禁 1 不触发）；
  - 离线 PoC（scripts/p5a_poc_backtrader.py，3 只合成股票 × 520 交易日、SMA 交叉策略、Returns/DrawDown/SharpeRatio/TradeAnalyzer 四个 analyzer）运行成功，零警告；
  - 同输入重复运行两次最终净值与结果哈希完全一致（d1aa5647d571bd98），**确定性验证通过**；单次运行约 0.05 秒、峰值 RSS 约 97 MB（3 feed 小样本）。
- 选型理由：Backtrader 提供 Cerebro/Strategy/Broker/订单 API/指标/analyzer 的成熟实现，社区与文档完善；bt 与 pandas<3 的兼容性风险更高。Backtrader 不提供符合本项目语义的完整 A 股筛选策略，业务策略由 StockManager 适配层定义（见下）。

### 2. GPLv3 许可影响

- Backtrader 以 **GPLv3** 许可发布。本项目仓库当前无 LICENSE 文件，且定位为**个人研究型 A 股筛选平台**（禁止自动交易，结果不构成投资建议）。
- 义务判断（以本项目实际情况为准）：
  - **仅个人使用、不对外分发**：源码随个人仓库保管，不构成向第三方分发，GPLv3 的分发义务不触发；本项目继续以源码形态自用，不引入闭源分发渠道。
  - **若未来对外分发**：任何包含或链接 Backtrader 的发行物必须遵守 GPLv3（附许可文本、提供对应源码、以兼容许可发布）；在决策对外分发方式前，不得把含 Backtrader 的程序以闭源二进制分发。
  - 许可文本随 Backtrader 包保留（wheel 内 LICENSE），不额外复制进仓库。
- 结论：当前「个人使用 + 源码自持」满足许可要求；对外分发策略改变时必须重新评估并回到用户决策，不得静默改变分发方式。

### 3. 适配器边界（Backtrader 只出现在适配层）

- 新增 BacktraderBacktestEngine 作为**适配器**，实现项目自己的 BacktestEngine Protocol；上层（Services/Domain/API）只认识 Domain 对象（ResearchStrategySpec、EligibilityTimeline、BacktestResult），不认识 bt.Cerebro、bt.Strategy、feed 或 analyzer。
- Services 与 Domain 不得导入 Backtrader 类型；Rules 保持纯函数；Backtrader 严禁持有 Provider；不持久化 Backtrader pickle/Cerebro/Strategy 实例。
- 使用**单一受控通用策略** StockManagerPortfolioStrategy（从资格时间线读取逐日资格、调用注册表中的 entry/exit/rebalance/allocation/ranking/execution policies、使用 Backtrader 指标实现均线等择时、把订单与成交转换为项目 Domain 事件），禁止为每个筛选模板动态生成 Python Strategy 子类，禁止运行用户上传代码（P5A_PLAN 第 10.2 节）。
- 校验：tests/ 中将增加「Services/Domain 无 Backtrader 导入」的静态回归测试。

### 4. 不做时间轴并行的原因

- 组合状态（现金、持仓、待成交订单、手续费、T+1 可卖数量、指标状态）**依赖前一时点**：T+1 的持仓与可卖数量由 T 日成交结果决定，T2 无法在不知道 T1 结果时独立计算；Backtrader 单次组合运行沿唯一时间线顺序推进。
- 历史筛选（生成资格信号）只按**股票轴**分片并行：每个 worker 一次读入整段历史，在 worker 内沿评估日 T 顺序滚动计算（P5A_PLAN 第 9.1/9.3 节）。
- 即使筛选规则本身某些日期可独立计算，拆分 T 轴也会重复 warm-up、重复读取、增加合并复杂度与一致性风险；结论：**Backtrader 组合运行不得拆段并行**。可并行的只有相互独立的参数实验（后续优化，受全局资源配额限制），不属于 P5A 范围。

## 依赖变更记录

- 2026-09-01：backtrader==1.9.78.123 装入项目 .venv（用户确认）。pyproject.toml 的正式依赖声明在 P5A-6（适配器实现）时随版本递增一并处理，不在 P5A-0 修改主依赖。

## 关联决策记录

- development/plan/P5A_PLAN.md：回测引擎决策（第 1.1 节）、目标架构（第 5 节）、时间语义（第 7 节）、适配方案（第 10 节）、性能门禁（第 13 节）。
- development/architecture/ADR_P5A_SEED_DISTRIBUTION.md：八年数据种子的 qfq 口径与除权断层对策（对策 a：除权后重拉受影响股票）。

## 结果（PoC 证据）

- 2026-09-01 scripts/p5a_poc_backtrader.py 输出：scripts/bench_results/2026-09-01_p5a_poc.json（schema_version 1）。
- Python 3.14 兼容、多 feed、analyzer、重复运行确定性、JSON 报告格式全部验证通过；既有筛选语义零改动（pytest 基线保持全绿）。
