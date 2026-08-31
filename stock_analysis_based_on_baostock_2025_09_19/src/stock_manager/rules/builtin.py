"""Trusted adapters that expose the pure built-in rules through one contract."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from stock_manager.domain import RuleResult
from stock_manager.rules.annual_min_close_price import evaluate_annual_min_close_price
from stock_manager.rules.annual_min_volume import evaluate_annual_min_volume
from stock_manager.rules.base import (
    ParameterDefinition,
    ParameterType,
    RuleContext,
    RuleDataRequirement,
    RuleDefinition,
    RulePitCapability,
    ScreeningRule,
    WindowUnit,
)
from stock_manager.rules.config import (
    LimitUpBreakoutConfig,
    LimitUpConfig,
    PePositiveConfig,
    VolatilityConfig,
    VolumePriceConfig,
)
from stock_manager.rules.consecutive_up_days import evaluate_consecutive_up_days
from stock_manager.rules.limit_up_3m import evaluate_limit_up_3m
from stock_manager.rules.n_day_close_above import evaluate_n_day_close_above
from stock_manager.rules.limit_up_breakout import evaluate_limit_up_breakout
from stock_manager.rules.non_st import evaluate_non_st
from stock_manager.rules.pe_positive import evaluate_pe_positive
from stock_manager.rules.price_range_ratio import evaluate_price_range_ratio
from stock_manager.rules.registry import RuleRegistry
from stock_manager.rules.volatility_multiple import evaluate_volatility_multiple
from stock_manager.rules.volume_price_5d import evaluate_volume_price_5d
from stock_manager.rules.volume_sum_extreme import evaluate_volume_sum_extreme


@dataclass(frozen=True, slots=True)
class NoParameters:
    pass


@dataclass(frozen=True, slots=True)
class AnnualMinClosePriceParameters:
    lookback_calendar_days: int
    minimum_required_trading_sessions: int
    exclude_zero_close: bool


@dataclass(frozen=True, slots=True)
class AnnualMinVolumeParameters:
    lookback_calendar_days: int
    minimum_required_trading_sessions: int
    exclude_zero_volume: bool


@dataclass(frozen=True, slots=True)
class ConsecutiveUpDaysParameters:
    lookback_trading_sessions: int
    required_consecutive_days: int


@dataclass(frozen=True, slots=True)
class NDayCloseAboveParameters:
    lookback_trading_sessions: int
    minimum_close: Decimal


@dataclass(frozen=True, slots=True)
class VolumeSumExtremeParameters:
    lookback_trading_sessions: int
    target_days: int
    reference_days: int
    mode: str
    minimum_required_trading_sessions: int


@dataclass(frozen=True, slots=True)
class PriceRangeRatioParameters:
    lookback_trading_sessions: int
    minimum_ratio: Decimal
    maximum_ratio: Decimal


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
    description: str,
) -> ParameterDefinition:
    return ParameterDefinition(
        parameter_id,
        value_type,
        True,
        default,
        None,
        None,
        label,
        description,
    )


class PePositiveRule:
    definition = RuleDefinition(
        "pe_positive",
        "PE 下限",
        "PE TTM 必须存在并严格大于下限；用于排除亏损或微利股票",
        (
            _parameter(
                "minimum_exclusive",
                ParameterType.DECIMAL,
                "0",
                "PE 严格下限",
                "PE(TTM) 必须严格大于该值才通过。例如 0 表示只接受盈利股票；设负值可放宽到微亏股票。",
            ),
        ),
        pit_capability=RulePitCapability.FUNDAMENTAL_PIT_READY,
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
    definition = RuleDefinition(
        "non_st",
        "排除 ST",
        "股票不得标记为 ST",
        (),
        pit_capability=RulePitCapability.UNIVERSE_STATE_PIT_READY,
    )

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


class VolumePriceRule:
    definition = RuleDefinition(
        "volume_price_5d",
        "量价信号",
        "相邻交易日同时满足量比和收盘涨幅",
        (
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                5,
                "观察交易日数",
                "在最近多少个交易日里寻找量价信号；窗口越大，越容易命中历史上任一天的放量上涨。",
            ),
            _parameter(
                "minimum_volume_ratio",
                ParameterType.DECIMAL,
                "4",
                "最低量比",
                "信号日成交量 ÷ 前一日成交量的最小倍数，用于捕捉放量；4 表示成交量至少放大到前一天的 4 倍。",
            ),
            _parameter(
                "minimum_close_rise_percent",
                ParameterType.DECIMAL,
                "7",
                "最低收盘涨幅百分比",
                "信号日收盘价相对前一日收盘价的最小涨幅（百分比）；7 表示当天至少上涨 7%。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
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
            _parameter(
                "signal_lookback_trading_sessions",
                ParameterType.INTEGER,
                5,
                "信号观察交易日数",
                "在最近多少个交易日内搜索涨停炸板/假阴线形态；窗口越大命中越多。",
            ),
            _parameter(
                "highest_lookback_trading_sessions",
                ParameterType.INTEGER,
                90,
                "历史最高价观察交易日数",
                "取最近多少个交易日内的最高价作为突破参考；越大越严格。",
            ),
            _parameter(
                "limit_ratio_lower_exclusive",
                ParameterType.DECIMAL,
                "1.08",
                "涨停比例开区间下限",
                "单日涨幅达到该比例（如 1.08 = 8%）即视为涨停；开区间，恰好等于不算。",
            ),
            _parameter(
                "limit_ratio_upper_exclusive",
                ParameterType.DECIMAL,
                "1.12",
                "涨停比例开区间上限",
                "涨幅超过该比例（如 1.12 = 12%）不再视为涨停；用于排除 20% 涨跌幅的板块。",
            ),
            _parameter(
                "close_below_high_amount",
                ParameterType.DECIMAL,
                "0.03",
                "收盘低于最高价金额",
                "炸板判定：涨停日收盘价较当日最高价回落超过该金额（元）即视为炸板。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
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
        "涨幅次数",
        "统计窗口内涨幅事件次数",
        (
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                90,
                "观察交易日数",
                "统计涨幅次数的时间窗口长度（交易日数）。",
            ),
            _parameter(
                "minimum_events",
                ParameterType.INTEGER,
                1,
                "最少涨幅次数",
                "窗口内涨幅事件数下限；少于该值判定失败。",
            ),
            _parameter(
                "maximum_events",
                ParameterType.INTEGER,
                3,
                "最多涨幅次数",
                "窗口内涨幅事件数上限；超过该值判定失败，用于避开连续暴涨的股票。",
            ),
            _parameter(
                "limit_ratio_lower_exclusive",
                ParameterType.DECIMAL,
                "1.08",
                "涨幅比例开区间下限",
                "单日涨幅达到该比例即计入一次涨幅；开区间，恰好等于不算。",
            ),
            _parameter(
                "limit_ratio_upper_exclusive",
                ParameterType.DECIMAL,
                "1.12",
                "涨幅比例开区间上限",
                "涨幅超过该比例不计入涨幅；用于排除 20% 涨跌幅的板块。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
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
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                180,
                "观察交易日数",
                "计算波动倍数的观察窗口长度（交易日数）。",
            ),
            _parameter(
                "maximum_multiple",
                ParameterType.DECIMAL,
                "2",
                "最大波动倍数",
                "窗口内最高价 ÷ 最低价的上限倍数；超过该倍数判定失败（波动过于剧烈）。",
            ),
            _parameter(
                "minimum_required_sessions",
                ParameterType.INTEGER,
                5,
                "最少有效交易日数",
                "窗口内至少要有多少个有数据的交易日；不足（如次新股）直接判定失败。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
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
            _parameter(
                "lookback_calendar_days",
                ParameterType.INTEGER,
                365,
                "自然日窗口",
                "以自然日计算的回看窗口长度；目标日必须是该窗口内成交量最低的一天。",
            ),
            _parameter(
                "minimum_required_trading_sessions",
                ParameterType.INTEGER,
                120,
                "最少有效交易日数",
                "窗口内至少要有多少个交易日的数据（排除停牌后）；不足则判定失败，避免次新股误判。",
            ),
            _parameter(
                "exclude_zero_volume",
                ParameterType.BOOLEAN,
                True,
                "排除零成交量",
                "统计最低成交量时是否忽略零成交量（停牌）的交易日；关闭后停牌日也会参与比较。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
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


class AnnualMinClosePriceRule:
    definition = RuleDefinition(
        "annual_min_close_price",
        "年度最低收盘价",
        "目标日是否为自然日窗口最低收盘价",
        (
            _parameter(
                "lookback_calendar_days",
                ParameterType.INTEGER,
                365,
                "自然日窗口",
                "以自然日计算的回看窗口长度；目标日必须是该窗口内收盘价最低的一天。",
            ),
            _parameter(
                "minimum_required_trading_sessions",
                ParameterType.INTEGER,
                120,
                "最少有效交易日数",
                "窗口内至少要有多少个交易日的数据；不足则判定失败，避免次新股误判。",
            ),
            _parameter(
                "exclude_zero_close",
                ParameterType.BOOLEAN,
                True,
                "排除零收盘价",
                "统计最低收盘价时是否忽略零收盘价（停牌）的交易日；关闭后零价日也会参与比较。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
    )

    def parse_parameters(self, raw: object) -> AnnualMinClosePriceParameters:
        names = {
            "lookback_calendar_days",
            "minimum_required_trading_sessions",
            "exclude_zero_close",
        }
        values = _mapping(raw, names, self.definition.rule_id)
        return AnnualMinClosePriceParameters(
            _integer(values, "lookback_calendar_days"),
            _integer(values, "minimum_required_trading_sessions"),
            _boolean(values, "exclude_zero_close"),
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, AnnualMinClosePriceParameters):
            raise TypeError("parameters must be AnnualMinClosePriceParameters")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.CALENDAR_DAYS,
            history_length=parameters.lookback_calendar_days,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, AnnualMinClosePriceParameters):
            raise TypeError("parameters must be AnnualMinClosePriceParameters")
        return evaluate_annual_min_close_price(
            context.daily_bars,
            context.metadata,
            context.trading_day,
            parameters.lookback_calendar_days,
            parameters.minimum_required_trading_sessions,
            parameters.exclude_zero_close,
            context.adjustment,
        )


class ConsecutiveUpDaysRule:
    definition = RuleDefinition(
        "consecutive_up_days",
        "连阳",
        "最近N个交易日窗口内出现至少K个连续上涨交易日（K连阳）",
        (
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                60,
                "搜索窗口交易日数",
                "在最近多少个交易日窗口内搜索连阳段；窗口越大越容易命中历史上任一段连续上涨。",
            ),
            _parameter(
                "required_consecutive_days",
                ParameterType.INTEGER,
                5,
                "连阳天数",
                "窗口内至少要有多少个连续交易日每个交易日的收盘价都高于前一交易日收盘价；5 表示五连阳。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
    )

    def parse_parameters(self, raw: object) -> ConsecutiveUpDaysParameters:
        values = _mapping(
            raw,
            {"lookback_trading_sessions", "required_consecutive_days"},
            self.definition.rule_id,
        )
        lookback = _integer(values, "lookback_trading_sessions")
        required = _integer(values, "required_consecutive_days")
        if required > lookback:
            raise ValueError(
                "required_consecutive_days must not exceed lookback_trading_sessions"
            )
        return ConsecutiveUpDaysParameters(lookback, required)

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, ConsecutiveUpDaysParameters):
            raise TypeError("parameters must be ConsecutiveUpDaysParameters")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions + 1,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, ConsecutiveUpDaysParameters):
            raise TypeError("parameters must be ConsecutiveUpDaysParameters")
        return evaluate_consecutive_up_days(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.required_consecutive_days,
            context.adjustment,
        )


class NDayCloseAboveRule:
    definition = RuleDefinition(
        "n_day_close_above",
        "N日收盘价下限",
        "最近N个交易日每天的收盘价都严格高于设定值",
        (
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                5,
                "观察交易日数",
                "检查最近多少个交易日的收盘价；3 表示最近 3 个交易日的收盘价都必须高于设定值。",
            ),
            _parameter(
                "minimum_close",
                ParameterType.DECIMAL,
                "10",
                "最低收盘价",
                "窗口内每一天的收盘价都必须严格高于该值（元）；恰好等于判定失败。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
    )

    def parse_parameters(self, raw: object) -> NDayCloseAboveParameters:
        values = _mapping(
            raw,
            {"lookback_trading_sessions", "minimum_close"},
            self.definition.rule_id,
        )
        threshold = _decimal(values, "minimum_close")
        if threshold <= 0:
            raise ValueError("minimum_close must be positive")
        return NDayCloseAboveParameters(
            _integer(values, "lookback_trading_sessions"), threshold
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, NDayCloseAboveParameters):
            raise TypeError("parameters must be NDayCloseAboveParameters")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, NDayCloseAboveParameters):
            raise TypeError("parameters must be NDayCloseAboveParameters")
        return evaluate_n_day_close_above(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.minimum_close,
            context.adjustment,
        )


class VolumeSumExtremeRule:
    definition = RuleDefinition(
        "volume_sum_extreme",
        "连续量能极值",
        "最近连续N天的成交量之和是所有连续M天成交量之和中的最低值或最高值",
        (
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                60,
                "回看交易日数",
                "在最近多少个交易日内生成所有连续参考窗口。",
            ),
            _parameter(
                "target_days",
                ParameterType.INTEGER,
                2,
                "最近连续天数",
                "计算最近连续多少个交易日的成交量之和作为目标值。",
            ),
            _parameter(
                "reference_days",
                ParameterType.INTEGER,
                2,
                "参考连续天数",
                "用所有连续多少个交易日的成交量之和作为比较集合。",
            ),
            _parameter(
                "mode",
                ParameterType.TEXT,
                "min",
                "比较模式",
                "min 表示目标值必须是所有参考和中的最低值；max 表示必须是最高值。",
            ),
            _parameter(
                "minimum_required_trading_sessions",
                ParameterType.INTEGER,
                10,
                "最少有效交易日数",
                "回看窗口内至少要有多少个有效交易日；不足直接判定失败。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
    )

    def parse_parameters(self, raw: object) -> VolumeSumExtremeParameters:
        values = _mapping(
            raw,
            {
                "lookback_trading_sessions",
                "target_days",
                "reference_days",
                "mode",
                "minimum_required_trading_sessions",
            },
            self.definition.rule_id,
        )
        lookback = _integer(values, "lookback_trading_sessions")
        target = _integer(values, "target_days")
        reference = _integer(values, "reference_days")
        minimum_required = _integer(
            values, "minimum_required_trading_sessions"
        )
        mode = values["mode"]
        if not isinstance(mode, str) or mode not in ("min", "max"):
            raise ValueError("mode must be 'min' or 'max'")
        if lookback < max(target, reference):
            raise ValueError(
                "lookback_trading_sessions must be at least max(target_days, reference_days)"
            )
        return VolumeSumExtremeParameters(
            lookback,
            target,
            reference,
            mode,
            minimum_required,
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, VolumeSumExtremeParameters):
            raise TypeError("parameters must be VolumeSumExtremeParameters")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, VolumeSumExtremeParameters):
            raise TypeError("parameters must be VolumeSumExtremeParameters")
        return evaluate_volume_sum_extreme(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.target_days,
            parameters.reference_days,
            parameters.mode,
            parameters.minimum_required_trading_sessions,
            context.adjustment,
        )


class PriceRangeRatioRule:
    definition = RuleDefinition(
        "price_range_ratio",
        "N日高低点倍率",
        "最近N个交易日内最高价相对最低价的倍数落在指定区间内",
        (
            _parameter(
                "lookback_trading_sessions",
                ParameterType.INTEGER,
                20,
                "观察交易日数",
                "在最近多少个交易日内计算最高价与最低价。",
            ),
            _parameter(
                "minimum_ratio",
                ParameterType.DECIMAL,
                "1.3",
                "倍率下限",
                "最高价 ÷ 最低价的下限，包含等于。",
            ),
            _parameter(
                "maximum_ratio",
                ParameterType.DECIMAL,
                "1.4",
                "倍率上限",
                "最高价 ÷ 最低价的上限，包含等于。",
            ),
        ),
        pit_capability=RulePitCapability.PRICE_VOLUME_PIT_READY,
    )

    def parse_parameters(self, raw: object) -> PriceRangeRatioParameters:
        values = _mapping(
            raw,
            {"lookback_trading_sessions", "minimum_ratio", "maximum_ratio"},
            self.definition.rule_id,
        )
        minimum_ratio = _decimal(values, "minimum_ratio")
        maximum_ratio = _decimal(values, "maximum_ratio")
        if minimum_ratio <= 0:
            raise ValueError("minimum_ratio must be positive")
        if maximum_ratio < minimum_ratio:
            raise ValueError(
                "maximum_ratio must be greater than or equal to minimum_ratio"
            )
        return PriceRangeRatioParameters(
            _integer(values, "lookback_trading_sessions"),
            minimum_ratio,
            maximum_ratio,
        )

    def data_requirement(self, parameters: object) -> RuleDataRequirement:
        if not isinstance(parameters, PriceRangeRatioParameters):
            raise TypeError("parameters must be PriceRangeRatioParameters")
        return RuleDataRequirement(
            market_history_unit=WindowUnit.TRADING_SESSIONS,
            history_length=parameters.lookback_trading_sessions,
        )

    def evaluate(self, context: RuleContext, parameters: object) -> RuleResult:
        if not isinstance(parameters, PriceRangeRatioParameters):
            raise TypeError("parameters must be PriceRangeRatioParameters")
        return evaluate_price_range_ratio(
            context.daily_bars,
            context.metadata,
            parameters.lookback_trading_sessions,
            parameters.minimum_ratio,
            parameters.maximum_ratio,
            context.adjustment,
        )


def build_default_registry() -> RuleRegistry:
    rules: tuple[ScreeningRule, ...] = (
        PePositiveRule(),
        NonStRule(),
        VolumePriceRule(),
        LimitUpBreakoutRule(),
        LimitUpCountRule(),
        VolatilityRule(),
        AnnualMinVolumeRule(),
        AnnualMinClosePriceRule(),
        ConsecutiveUpDaysRule(),
        NDayCloseAboveRule(),
        VolumeSumExtremeRule(),
        PriceRangeRatioRule(),
    )
    return RuleRegistry(rules)
