"""Built-in policy definitions and first three strategy specs (P5A-3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stock_manager.domain import AdjustmentMethod
from stock_manager.research.models import (
    EvaluationSchedule,
    PolicyKind,
    PolicyParameterSpec,
    PolicyParameterType,
    PolicySpec,
    ResearchStrategySpec,
)
from stock_manager.research.policies import PolicyDefinition, PolicyRegistry


def _parameter(
    parameter_id: str,
    value_type: PolicyParameterType,
    default: object,
    label: str,
    description: str,
    *,
    required: bool = False,
    minimum: object | None = None,
    maximum: object | None = None,
) -> PolicyParameterSpec:
    return PolicyParameterSpec(
        parameter_id,
        value_type,
        required,
        default,
        minimum,
        maximum,
        label,
        description,
    )


def build_default_policy_registry() -> PolicyRegistry:
    """Register the P5A policy set used by the first three strategy slices."""
    return PolicyRegistry(
        (
            PolicyDefinition(
                "eligibility_enter_v1",
                PolicyKind.ENTRY,
                1,
                "资格名单出现即允许进入(服从执行约束)",
                (),
            ),
            PolicyDefinition(
                "pullback_entry_v1",
                PolicyKind.ENTRY,
                1,
                "筛选通过后不立即买入,等价格自近 N 日高点回落 drawdown_ratio 后再买入(回调入场)",
                (
                    _parameter(
                        "lookback_trading_days",
                        PolicyParameterType.INTEGER,
                        20,
                        "高点回看交易日数",
                        "计算近 N 日最高价的窗口",
                        minimum=2,
                        maximum=250,
                    ),
                    _parameter(
                        "drawdown_ratio",
                        PolicyParameterType.DECIMAL,
                        "0.05",
                        "回落阈值",
                        "收盘价低于近 N 日最高价的比例(如 0.05 表示回落 5%)才买入",
                        minimum="0",
                        maximum="1",
                    ),
                ),
            ),
            PolicyDefinition(
                "sma_below_v1",
                PolicyKind.ENTRY,
                1,
                "收盘价低于 N 日均线时买入(均线买入,均值回归)",
                (
                    _parameter(
                        "sma_period",
                        PolicyParameterType.INTEGER,
                        20,
                        "均线周期",
                        "低于该周期简单移动平均时才买入",
                        minimum=2,
                        maximum=250,
                    ),
                ),
            ),
            PolicyDefinition(
                "eligibility_exit_v1",
                PolicyKind.EXIT,
                1,
                "资格失效即退出(服从执行约束)",
                (),
            ),
            PolicyDefinition(
                "sma_timing_v1",
                PolicyKind.EXIT,
                1,
                "收盘价跌破 N 日均线时退出(Backtrader SMA 指标)",
                (
                    _parameter(
                        "sma_period",
                        PolicyParameterType.INTEGER,
                        20,
                        "均线周期",
                        "退出均线的交易日周期",
                        minimum=2,
                        maximum=250,
                    ),
                ),
            ),
            PolicyDefinition(
                "fixed_holding_v1",
                PolicyKind.EXIT,
                1,
                "持有 N 个交易日后退出(首次进入日起算)",
                (
                    _parameter(
                        "holding_trading_days",
                        PolicyParameterType.INTEGER,
                        20,
                        "持有交易日数",
                        "进入后持有 N 个交易日再退出",
                        minimum=1,
                        maximum=1000,
                    ),
                ),
            ),
            PolicyDefinition(
                "take_profit_partial_v1",
                PolicyKind.EXIT,
                1,
                "上涨到 take_profit_ratio 后卖出一定比例(止盈减仓)",
                (
                    _parameter(
                        "take_profit_ratio",
                        PolicyParameterType.DECIMAL,
                        "0.10",
                        "止盈涨幅",
                        "相对持仓成本的涨幅达到该比例才触发减仓(如 0.10 表示 +10%)",
                        minimum="0",
                        maximum="5",
                    ),
                    _parameter(
                        "partial_ratio",
                        PolicyParameterType.DECIMAL,
                        "0.50",
                        "减仓比例",
                        "止盈时卖出现有持仓的比例(0~1)",
                        minimum="0.01",
                        maximum="1",
                    ),
                ),
            ),
            PolicyDefinition(
                "sma_above_v1",
                PolicyKind.EXIT,
                1,
                "收盘价高于 N 日均线时卖出(均线卖出,均值回归)",
                (
                    _parameter(
                        "sma_period",
                        PolicyParameterType.INTEGER,
                        20,
                        "均线周期",
                        "高于该周期简单移动平均时才卖出",
                        minimum=2,
                        maximum=250,
                    ),
                ),
            ),
            PolicyDefinition(
                "daily_v1",
                PolicyKind.REBALANCE,
                1,
                "每个交易日收盘后重算目标组合",
                (),
            ),
            PolicyDefinition(
                "equal_weight_v1",
                PolicyKind.ALLOCATION,
                1,
                "等权分配:每只目标股资金 = 净资产 / 目标持仓数",
                (
                    _parameter(
                        "max_positions",
                        PolicyParameterType.INTEGER,
                        20,
                        "最大持仓数",
                        "同一时刻最多持有的股票数",
                        required=True,
                        minimum=1,
                        maximum=500,
                    ),
                    _parameter(
                        "cash_reserve_ratio",
                        PolicyParameterType.DECIMAL,
                        "0",
                        "现金保留比例",
                        "保留净资产的比例(0~1)",
                        minimum="0",
                        maximum="1",
                    ),
                ),
            ),
            PolicyDefinition(
                "add_position_on_dip_v1",
                PolicyKind.ALLOCATION,
                1,
                "筛选后等权买入,持有中若价格回调 add_drawdown_ratio 且有现金则按 add_fraction 补仓(最多 max_additions 次)",
                (
                    _parameter(
                        "max_positions",
                        PolicyParameterType.INTEGER,
                        20,
                        "最大持仓数",
                        "同一时刻最多持有的股票数(入场等权用)",
                        required=True,
                        minimum=1,
                        maximum=500,
                    ),
                    _parameter(
                        "add_drawdown_ratio",
                        PolicyParameterType.DECIMAL,
                        "0.10",
                        "补仓回撤",
                        "相对持仓成本回撤达到该比例才触发补仓(如 0.10 表示 -10%)",
                        minimum="0",
                        maximum="1",
                    ),
                    _parameter(
                        "add_fraction",
                        PolicyParameterType.DECIMAL,
                        "0.50",
                        "补仓比例",
                        "补仓资金 = 当前持仓市值 x 该比例",
                        minimum="0.01",
                        maximum="5",
                    ),
                    _parameter(
                        "max_additions",
                        PolicyParameterType.INTEGER,
                        2,
                        "最大补仓次数",
                        "最多触发补仓的次数",
                        minimum=1,
                        maximum=10,
                    ),
                ),
            ),
            PolicyDefinition(
                "turnover_20d_desc_v1",
                PolicyKind.RANKING,
                1,
                "近 20 个交易日平均成交额降序,同额按代码升序(已确认决策 4)",
                (
                    _parameter(
                        "lookback_trading_days",
                        PolicyParameterType.INTEGER,
                        20,
                        "成交额回看交易日数",
                        "平均成交额的计算窗口",
                        minimum=1,
                        maximum=250,
                    ),
                ),
            ),
            PolicyDefinition(
                "ashare_execution_v1",
                PolicyKind.EXECUTION,
                1,
                "A 股执行模型:T+1 开盘成交、整手、停牌/涨跌停不可成交、费用(参数由 P5A-7 固化)",
                (
                    _parameter(
                        "commission_rate",
                        PolicyParameterType.DECIMAL,
                        "0.0003",
                        "佣金费率",
                        "双边佣金费率(最低 5 元)",
                        minimum="0",
                    ),
                    _parameter(
                        "stamp_duty_rate",
                        PolicyParameterType.DECIMAL,
                        "0.0005",
                        "印花税率",
                        "卖出印花税税率",
                        minimum="0",
                    ),
                    _parameter(
                        "transfer_fee_rate",
                        PolicyParameterType.DECIMAL,
                        "0.00001",
                        "过户费率",
                        "双边过户费(十万分之一)",
                        minimum="0",
                    ),
                    _parameter(
                        "min_commission",
                        PolicyParameterType.DECIMAL,
                        "5",
                        "最低佣金",
                        "单笔佣金下限(元)",
                        minimum="0",
                    ),
                    _parameter(
                        "lot_size",
                        PolicyParameterType.INTEGER,
                        100,
                        "整手股数",
                        "买入按 100 股整手",
                        minimum=1,
                        maximum=10000,
                    ),
                ),
            ),
        )
    )


def builtin_strategy_specs(
    *,
    template_id: str,
    template_revision: int,
    plan_fingerprint: str,
    adjustment: AdjustmentMethod = AdjustmentMethod.QFQ,
    backtest_start: date,
    backtest_end: date,
    initial_cash: Decimal = Decimal("1000000"),
    max_positions: int = 20,
) -> dict[str, ResearchStrategySpec]:
    """First three controlled strategy slices (P5A_PLAN 6.3)."""
    common = dict(
        screening_template_id=template_id,
        screening_template_revision=template_revision,
        screening_plan_fingerprint=plan_fingerprint,
        adjustment=adjustment,
        evaluation_schedule=EvaluationSchedule.DAILY,
        allocation_policy=PolicySpec(
            "equal_weight_v1", 1, {"max_positions": max_positions}
        ),
        ranking_policy=PolicySpec("turnover_20d_desc_v1", 1, {}),
        execution_policy=PolicySpec("ashare_execution_v1", 1, {}),
        initial_cash=initial_cash,
        backtest_start=backtest_start,
        backtest_end=backtest_end,
    )
    return {
        "selection_rebalance_v1": ResearchStrategySpec(
            strategy_spec_id="selection_rebalance_v1",
            entry_policy=PolicySpec("eligibility_enter_v1", 1, {}),
            exit_policy=PolicySpec("eligibility_exit_v1", 1, {}),
            rebalance_policy=PolicySpec("daily_v1", 1, {}),
            **common,
        ),
        "selection_sma_timing_v1": ResearchStrategySpec(
            strategy_spec_id="selection_sma_timing_v1",
            entry_policy=PolicySpec("eligibility_enter_v1", 1, {}),
            exit_policy=PolicySpec("sma_timing_v1", 1, {"sma_period": 20}),
            rebalance_policy=PolicySpec("daily_v1", 1, {}),
            **common,
        ),
        "selection_fixed_holding_v1": ResearchStrategySpec(
            strategy_spec_id="selection_fixed_holding_v1",
            entry_policy=PolicySpec("eligibility_enter_v1", 1, {}),
            exit_policy=PolicySpec("fixed_holding_v1", 1, {"holding_trading_days": 20}),
            rebalance_policy=PolicySpec("daily_v1", 1, {}),
            **common,
        ),
    }
