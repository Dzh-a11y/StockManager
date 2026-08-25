"""Trusted adapters that expose the pure built-in rules through one contract."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from stock_manager.domain import RuleResult
from stock_manager.rules.annual_min_volume import evaluate_annual_min_volume
from stock_manager.rules.base import (
    ParameterDefinition,
    ParameterType,
    RuleContext,
    RuleDataRequirement,
    RuleDefinition,
    ScreeningRule,
    WindowUnit,
)
from stock_manager.rules.config import (
    DividendConfig,
    LimitUpBreakoutConfig,
    LimitUpConfig,
    PePositiveConfig,
    VolatilityConfig,
    VolumePriceConfig,
)
from stock_manager.rules.dividend_3y import evaluate_dividend_3y
from stock_manager.rules.limit_up_3m import evaluate_limit_up_3m
from stock_manager.rules.limit_up_breakout import evaluate_limit_up_breakout
from stock_manager.rules.non_st import evaluate_non_st
from stock_manager.rules.pe_positive import evaluate_pe_positive
from stock_manager.rules.registry import RuleRegistry
from stock_manager.rules.volatility_multiple import evaluate_volatility_multiple
from stock_manager.rules.volume_price_5d import evaluate_volume_price_5d


@dataclass(frozen=True, slots=True)
class NoParameters:
    pass


@dataclass(frozen=True, slots=True)
class AnnualMinVolumeParameters:
    lookback_calendar_days: int
    minimum_required_trading_sessions: int
    exclude_zero_volume: bool


def _mapping(raw: object, expected: set[str], rule_id: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{rule_id} parameters must be an object")
    if set(raw) != expected:
        raise ValueError(f"{rule_id} parameters must contain exactly {sorted(expected)}")
    return raw


def _integer(raw: dict[str, object], name: str) -> int:
    value = raw[name]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(raw: dict[str, object], name: str) -> int:
    value = raw[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _decimal(raw: dict[str, object], name: str) -> Decimal:
    value = raw[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _boolean(raw: dict[str, object], name: str) -> bool:
    value = raw[name]
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _parameter(
    parameter_id: str,
    value_type: ParameterType,
    default: object,
    label: str,
) -> ParameterDefinition:
    return ParameterDefinition(
        parameter_id,
        value_type,
        True,
        default,
        None,
        None,
        label,
        label,
    )


class PePositiveRule:
    definition = RuleDefinition(
        "pe_positive",
        "PE 下限",
        "PE TTM 必须存在并严格大于下限",
        (_parameter("minimum_exclusive", ParameterType.DECIMAL, "0", "PE 严格下限"),),
    )

    def parse_parameters(self, raw: object) -> PePositiveConfig:
        values = _mapping(raw, {"minimum_exclusive"}, self.definition.rule_id)
        return PePositiveConfig(_decimal(values, "minimum_exclusive"))

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, PePositiveConfig):
            raise TypeError("parameters must be PePositiveConfig")
        return RuleDataRequirement(needs_fundamental=True)

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, PePositiveConfig):
            raise TypeError("parameters must be PePositiveConfig")
        return evaluate_pe_positive(context.fundamental, parameters.minimum_exclusive)


class NonStRule:
    definition = RuleDefinition("non_st", "排除 ST", "股票不得标记为 ST", ())

    def parse_parameters(self, raw: object) -> NoParameters:
        _mapping(raw, set(), self.definition.rule_id)
        return NoParameters()

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, NoParameters):
            raise TypeError("parameters must be NoParameters")
        return RuleDataRequirement()

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, NoParameters):
            raise TypeError("parameters must be NoParameters")
        return evaluate_non_st(context.stock)


class DividendRule:
    definition = RuleDefinition(
        "dividend_3y",
        "历史分红",
        "检查最近若干个已完成自然年度的分红记录",
        (
            _parameter("completed_calendar_years", ParameterType.INTEGER, 3, "自然年度数"),
            _parameter("minimum_records", ParameterType.INTEGER, 1, "最少分红记录"),
        ),
    )

    def parse_parameters(self, raw: object) -> DividendConfig:
        values = _mapping(
            raw,
            {"completed_calendar_years", "minimum_records"},
            self.definition.rule_id,
        )
        years = _integer(values, "completed_calendar_years")
        minimum = _nonnegative_integer(values, "minimum_records")
        return DividendConfig(years, minimum)

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, DividendConfig):
            raise TypeError("parameters must be DividendConfig")
        return RuleDataRequirement(
            needs_dividends=True,
            dividend_calendar_years=parameters.completed_calendar_years,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, DividendConfig):
            raise TypeError("parameters must be DividendConfig")
        return evaluate_dividend_3y(
            context.dividends,
            context.trading_day,
            parameters.completed_calendar_years,
            parameters.minimum_records,
        )


class VolumePriceRule:
    definition = RuleDefinition(
        "volume_price_5d",
        "量价信号",
        "相邻交易日同时满足量比和收盘涨幅",
        (
            _parameter("lookback_trading_sessions", ParameterType.INTEGER, 5, "观察交易日数"),
            _parameter("minimum_volume_ratio", ParameterType.DECIMAL, "4", "最低量比"),
            _parameter("minimum_close_rise_percent", ParameterType.DECIMAL, "7", "最低收盘涨幅百分比"),
        ),
    )

    def parse_parameters(self, raw: object) -> VolumePriceConfig:
        names = {
            "lookback_trading_sessions",
            "minimum_volume_ratio",
            "minimum_close_rise_percent",
        }
        values = _mapping(raw, names, self.definition.rule_id)
        return VolumePriceConfig(
            _integer(values, "lookback_trading_sessions"),
            _decimal(values, "minimum_volume_ratio"),
            _decimal(values, "minimum_close_rise_percent"),
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, VolumePriceConfig):
            raise TypeError("parameters must be VolumePriceConfig")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, VolumePriceConfig):
            raise TypeError("parameters must be VolumePriceConfig")
        return evaluate_volume_price_5d(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.minimum_volume_ratio,
            parameters.minimum_close_rise_percent,
            context.adjustment,
        )


class LimitUpBreakoutRule:
    definition = RuleDefinition(
        "limit_up_breakout",
        "炸板或假阴线",
        "检查涨停炸板或假阴线信号",
        (
            _parameter("signal_lookback_trading_sessions", ParameterType.INTEGER, 5, "信号观察交易日数"),
            _parameter("highest_lookback_trading_sessions", ParameterType.INTEGER, 90, "历史最高价观察交易日数"),
            _parameter("limit_ratio_lower_exclusive", ParameterType.DECIMAL, "1.08", "涨停比例开区间下限"),
            _parameter("limit_ratio_upper_exclusive", ParameterType.DECIMAL, "1.12", "涨停比例开区间上限"),
            _parameter("close_below_high_amount", ParameterType.DECIMAL, "0.03", "收盘低于最高价金额"),
        ),
    )

    def parse_parameters(self, raw: object) -> LimitUpBreakoutConfig:
        names = {
            "signal_lookback_trading_sessions",
            "highest_lookback_trading_sessions",
            "limit_ratio_lower_exclusive",
            "limit_ratio_upper_exclusive",
            "close_below_high_amount",
        }
        values = _mapping(raw, names, self.definition.rule_id)
        lower = _decimal(values, "limit_ratio_lower_exclusive")
        upper = _decimal(values, "limit_ratio_upper_exclusive")
        if lower >= upper:
            raise ValueError("limit ratio lower bound must be below upper bound")
        return LimitUpBreakoutConfig(
            _integer(values, "signal_lookback_trading_sessions"),
            _integer(values, "highest_lookback_trading_sessions"),
            lower,
            upper,
            _decimal(values, "close_below_high_amount"),
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, LimitUpBreakoutConfig):
            raise TypeError("parameters must be LimitUpBreakoutConfig")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=max(
                parameters.signal_lookback_trading_sessions,
                parameters.highest_lookback_trading_sessions,
            ),
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, LimitUpBreakoutConfig):
            raise TypeError("parameters must be LimitUpBreakoutConfig")
        return evaluate_limit_up_breakout(
            context.daily_bars,
            context.metadata,
            parameters.signal_lookback_trading_sessions,
            parameters.highest_lookback_trading_sessions,
            parameters.limit_ratio_lower_exclusive,
            parameters.limit_ratio_upper_exclusive,
            parameters.close_below_high_amount,
            context.adjustment,
        )


class LimitUpCountRule:
    definition = RuleDefinition(
        "limit_up_3m",
        "涨停次数",
        "统计窗口内涨停事件次数",
        (
            _parameter("lookback_trading_sessions", ParameterType.INTEGER, 90, "观察交易日数"),
            _parameter("minimum_events", ParameterType.INTEGER, 1, "最少涨停次数"),
            _parameter("maximum_events", ParameterType.INTEGER, 3, "最多涨停次数"),
            _parameter("limit_ratio_lower_exclusive", ParameterType.DECIMAL, "1.08", "涨停比例开区间下限"),
            _parameter("limit_ratio_upper_exclusive", ParameterType.DECIMAL, "1.12", "涨停比例开区间上限"),
        ),
    )

    def parse_parameters(self, raw: object) -> LimitUpConfig:
        names = {
            "lookback_trading_sessions",
            "minimum_events",
            "maximum_events",
            "limit_ratio_lower_exclusive",
            "limit_ratio_upper_exclusive",
        }
        values = _mapping(raw, names, self.definition.rule_id)
        minimum = _nonnegative_integer(values, "minimum_events")
        maximum = _nonnegative_integer(values, "maximum_events")
        lower = _decimal(values, "limit_ratio_lower_exclusive")
        upper = _decimal(values, "limit_ratio_upper_exclusive")
        if minimum > maximum:
            raise ValueError("minimum_events must not exceed maximum_events")
        if lower >= upper:
            raise ValueError("limit ratio lower bound must be below upper bound")
        return LimitUpConfig(
            _integer(values, "lookback_trading_sessions"),
            minimum,
            maximum,
            lower,
            upper,
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, LimitUpConfig):
            raise TypeError("parameters must be LimitUpConfig")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, LimitUpConfig):
            raise TypeError("parameters must be LimitUpConfig")
        return evaluate_limit_up_3m(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.minimum_events,
            parameters.maximum_events,
            parameters.limit_ratio_lower_exclusive,
            parameters.limit_ratio_upper_exclusive,
            context.adjustment,
        )


class VolatilityRule:
    definition = RuleDefinition(
        "volatility_multiple",
        "波动倍数",
        "限制窗口最高价与最低价的倍数",
        (
            _parameter("lookback_trading_sessions", ParameterType.INTEGER, 180, "观察交易日数"),
            _parameter("maximum_multiple", ParameterType.DECIMAL, "2", "最大波动倍数"),
            _parameter("minimum_required_sessions", ParameterType.INTEGER, 5, "最少有效交易日数"),
        ),
    )

    def parse_parameters(self, raw: object) -> VolatilityConfig:
        names = {
            "lookback_trading_sessions",
            "maximum_multiple",
            "minimum_required_sessions",
        }
        values = _mapping(raw, names, self.definition.rule_id)
        return VolatilityConfig(
            _integer(values, "lookback_trading_sessions"),
            _decimal(values, "maximum_multiple"),
            _integer(values, "minimum_required_sessions"),
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, VolatilityConfig):
            raise TypeError("parameters must be VolatilityConfig")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, VolatilityConfig):
            raise TypeError("parameters must be VolatilityConfig")
        return evaluate_volatility_multiple(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.maximum_multiple,
            parameters.minimum_required_sessions,
            context.adjustment,
        )


class AnnualMinVolumeRule:
    definition = RuleDefinition(
        "annual_min_volume",
        "年度最低交易量",
        "目标日是否为自然日窗口最低交易量",
        (
            _parameter("lookback_calendar_days", ParameterType.INTEGER, 365, "自然日窗口"),
            _parameter("minimum_required_trading_sessions", ParameterType.INTEGER, 120, "最少有效交易日数"),
            _parameter("exclude_zero_volume", ParameterType.BOOLEAN, True, "排除零成交量"),
        ),
    )

    def parse_parameters(self, raw: object) -> AnnualMinVolumeParameters:
        names = {
            "lookback_calendar_days",
            "minimum_required_trading_sessions",
            "exclude_zero_volume",
        }
        values = _mapping(raw, names, self.definition.rule_id)
        return AnnualMinVolumeParameters(
            _integer(values, "lookback_calendar_days"),
            _integer(values, "minimum_required_trading_sessions"),
            _boolean(values, "exclude_zero_volume"),
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, AnnualMinVolumeParameters):
            raise TypeError("parameters must be AnnualMinVolumeParameters")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.CALENDAR_DAYS,
            history_length=parameters.lookback_calendar_days,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, AnnualMinVolumeParameters):
            raise TypeError("parameters must be AnnualMinVolumeParameters")
        return evaluate_annual_min_volume(
            context.daily_bars,
            context.metadata,
            context.trading_day,
            parameters.lookback_calendar_days,
            parameters.minimum_required_trading_sessions,
            parameters.exclude_zero_volume,
            context.adjustment,
        )


def build_default_registry() -> RuleRegistry:
    rules: tuple[ScreeningRule, ...] = (
        PePositiveRule(),
        NonStRule(),
        DividendRule(),
        VolumePriceRule(),
        LimitUpBreakoutRule(),
        LimitUpCountRule(),
        VolatilityRule(),
        AnnualMinVolumeRule(),
    )
    return RuleRegistry(rules)
