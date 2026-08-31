---
date: 2026-09-02
purpose: 记录 StockManager P5A-3 研究策略领域模型、政策注册表、fingerprint 与首批三个受控策略规格的实现与离线验收结果。
project: StockManager
status: active
---

# P5A-3 研究策略领域模型与政策注册表

## 阶段定位

P5A-3 建立与 Backtrader 解耦、可版本化的研究策略契约:ResearchStrategySpec、PolicySpec、政策注册表(白名单参数校验)、fingerprint,以及首批三个受控策略组合。Domain 不导入 Backtrader;任意 import path 与未知 policy ID 在运行前被拒绝。

## 新增模块

```text
src/stock_manager/research/__init__.py   # 公共导出
src/stock_manager/research/models.py     # PolicyKind/EvaluationSchedule/PolicyParameterSpec/
                                         #  PolicySpec/ResearchStrategySpec
src/stock_manager/research/policies.py   # PolicyDefinition/PolicyRegistry/校验错误
src/stock_manager/research/fingerprint.py# canonical_json/plan_fingerprint/policy_fingerprint/spec_fingerprint
src/stock_manager/research/builtin.py    # 内置政策定义与三个策略规格工厂

tests/test_research_models.py            # 域模型/注册表/fingerprint/无 Backtrader 泄露(10 项)
```

## 领域契约

ResearchStrategySpec(P5A_PLAN 6.1):strategy_spec_id、模板 ID + revision、plan fingerprint、adjustment、evaluation_schedule(DAILY)、六类 PolicySpec(entry/exit/rebalance/allocation/ranking/execution)、initial_cash(Decimal)、backtest_start/end。金额一律 Decimal,校验 initial_cash > 0、区间合法。

PolicySpec(policy_id, version, parameters):参数为 JSON 可序列化 Mapping(内部转 MappingProxyType 保证不可变)。禁止保存任意 Python import path——校验器按注册表参数 schema 白名单拒绝未知键、缺失必填、越界值与类型错误。

## 政策注册表

PolicyRegistry:显式注册 (policy_id, kind, version) → PolicyDefinition(含参数 schema);validate_strategy 一次性校验六个政策;未知 ID/版本/kind 组合抛 UnknownPolicyError,参数违规抛 InvalidPolicyParametersError。

内置 8 个政策(P5A-3 首批):eligibility_enter_v1 / eligibility_exit_v1 / sma_timing_v1(均线退出)/ fixed_holding_v1(持有 N 日退出)/ daily_v1(每日调仓)/ equal_weight_v1(等权+最大持仓+现金保留)/ turnover_20d_desc_v1(近 20 日均成交额降序,已确认决策 4)/ ashare_execution_v1(T+1 开盘、整手、停牌/涨跌停、佣金/印花税/过户费,参数由 P5A-7 固化)。

## 首批三个受控策略规格

builtin_strategy_specs(...) 返回:

| spec_id | entry | exit | 语义 |
|---|---|---|---|
| selection_rebalance_v1 | eligibility_enter_v1 | eligibility_exit_v1 | 资格进入、失效退出,每日调仓 |
| selection_sma_timing_v1 | eligibility_enter_v1 | sma_timing_v1(20 日) | 资格 + 收盘跌破均线退出 |
| selection_fixed_holding_v1 | eligibility_enter_v1 | fixed_holding_v1(20 日) | 资格进入后持有 20 交易日退出 |

## Fingerprint

- canonical_json:键排序 + Decimal 转十进制字符串 + dataclass/Enum 规范化,消除浮点舍入与键序影响。
- plan_fingerprint:模板 ID/revision/adjustment/规则实现版本/启用规则与参数/组合结构。
- policy_fingerprint:policy_id + version + 参数。
- spec_fingerprint:全规格(模板、plan fingerprint、区间、现金、六个政策指纹)。相同配置稳定;模板 revision、退出政策、参数变化必变。

## 离线测试验收

python3 -m pytest:**323 passed**(P5A-3 新增 10 项;P5A-2 313 项与基线无回归)。关键覆盖:research 包导入不产生 backtrader 模块(子进程断言)、未知 policy/版本/kind 拒绝、未知/缺失/越界参数拒绝、import path 白名单、三个内置规格全政策校验通过、fingerprint 稳定与敏感(模板 revision/退出政策/参数变化)、canonical_json 规范化。

## 说明与后续

- 政策语义实现(entry/exit/rebalance/allocation/ranking/execution 的具体计算)属于 P5A-6(Backtrader 适配器)与 P5A-7(执行模型)。
- 规则实现版本当前由调用方声明(rule_implementation_version 默认 builtin-v1);若未来规则语义变化,必须递增该版本以触发缓存失效。

## 免责声明

所有筛选与回测结果仅供研究参考,不构成任何投资建议。项目禁止实现自动交易功能。
