"""Offline orchestration for stock screening."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    ScreeningResult,
)
from stock_manager.protocols import LocalRepositoryProtocol
from stock_manager.rules import (
    RulesConfig,
    evaluate_composite,
    evaluate_dividend_3y,
    evaluate_limit_up_3m,
    evaluate_limit_up_breakout,
    evaluate_non_st,
    evaluate_pe_positive,
    evaluate_volatility_multiple,
    evaluate_volume_price_5d,
)


class DatasetUnavailableError(ValueError):
    """Raised when an exact local dataset snapshot cannot be screened."""


class StockNotFoundError(ValueError):
    """Raised when a requested code is absent from the local stock snapshot."""


class ScreeningService:
    """Read one local snapshot and evaluate the configured pure rules."""

    def __init__(
        self,
        repository: LocalRepositoryProtocol,
        rules_config: RulesConfig,
    ) -> None:
        self._repository = repository
        self._config = rules_config

    def screen(
        self,
        dataset_id: str,
        trading_day: date,
        adjustment: AdjustmentMethod,
        codes: Sequence[str] = (),
    ) -> tuple[ScreeningResult, ...]:
        normalized_dataset_id = dataset_id.strip()
        if not normalized_dataset_id:
            raise ValueError("dataset_id must not be empty")
        if adjustment is not self._config.technical_adjustment:
            raise ValueError(
                "requested adjustment does not match the rules configuration"
            )
        metadata = self._repository.get_dataset_metadata(
            normalized_dataset_id, trading_day, adjustment
        )
        if metadata is None:
            raise DatasetUnavailableError(
                f"local dataset {normalized_dataset_id!r} is unavailable for "
                f"{trading_day.isoformat()} with adjustment {adjustment.value}"
            )

        stocks_by_code = {
            stock.code: stock for stock in self._repository.get_stocks(trading_day)
        }
        selected_codes = self._select_codes(codes, stocks_by_code)
        if not selected_codes:
            return ()

        trading_days = tuple(
            self._repository.get_trading_days(date.min, trading_day)
        )
        if not trading_days or trading_days[-1] != trading_day:
            raise DatasetUnavailableError(
                f"local trading calendar does not contain {trading_day.isoformat()}"
            )
        lookback = max(
            self._config.volume_price_5d.lookback_trading_sessions,
            self._config.limit_up_breakout.signal_lookback_trading_sessions,
            self._config.limit_up_breakout.highest_lookback_trading_sessions,
            self._config.limit_up_3m.lookback_trading_sessions,
            self._config.volatility_multiple.lookback_trading_sessions,
        )
        window_days = trading_days[-lookback:]
        bars = self._repository.get_daily_bars(
            selected_codes, window_days[0], trading_day, adjustment
        )
        fundamentals = self._repository.get_fundamentals(selected_codes, trading_day)
        dividend_start = date(
            trading_day.year - self._config.dividend_3y.completed_calendar_years,
            1,
            1,
        )
        dividends = self._repository.get_dividends(
            selected_codes, dividend_start, trading_day
        )
        bars_by_code = self._group_bars(bars)
        fundamentals_by_code = {item.code: item for item in fundamentals}
        dividends_by_code = self._group_dividends(dividends)

        results: list[ScreeningResult] = []
        for code in selected_codes:
            stock = stocks_by_code[code]
            code_bars = bars_by_code.get(code, ())
            rule_results = (
                evaluate_pe_positive(
                    fundamentals_by_code.get(code),
                    self._config.pe_positive.minimum_exclusive,
                ),
                evaluate_non_st(stock),
                evaluate_dividend_3y(
                    dividends_by_code.get(code, ()),
                    trading_day,
                    self._config.dividend_3y.completed_calendar_years,
                    self._config.dividend_3y.minimum_records,
                ),
                evaluate_volume_price_5d(
                    code_bars,
                    metadata,
                    self._config.volume_price_5d.lookback_trading_sessions,
                    self._config.volume_price_5d.minimum_volume_ratio,
                    self._config.volume_price_5d.minimum_close_rise_percent,
                    adjustment,
                ),
                evaluate_limit_up_breakout(
                    code_bars,
                    metadata,
                    self._config.limit_up_breakout.signal_lookback_trading_sessions,
                    self._config.limit_up_breakout.highest_lookback_trading_sessions,
                    self._config.limit_up_breakout.limit_ratio_lower_exclusive,
                    self._config.limit_up_breakout.limit_ratio_upper_exclusive,
                    self._config.limit_up_breakout.close_below_high_amount,
                    adjustment,
                ),
                evaluate_limit_up_3m(
                    code_bars,
                    metadata,
                    self._config.limit_up_3m.lookback_trading_sessions,
                    self._config.limit_up_3m.minimum_events,
                    self._config.limit_up_3m.maximum_events,
                    self._config.limit_up_3m.limit_ratio_lower_exclusive,
                    self._config.limit_up_3m.limit_ratio_upper_exclusive,
                    adjustment,
                ),
                evaluate_volatility_multiple(
                    code_bars,
                    metadata,
                    self._config.volatility_multiple.lookback_trading_sessions,
                    self._config.volatility_multiple.maximum_multiple,
                    self._config.volatility_multiple.minimum_required_sessions,
                    adjustment,
                ),
            )
            composite = evaluate_composite(rule_results)
            results.append(
                ScreeningResult(
                    code,
                    trading_day,
                    composite.passed,
                    (*rule_results, composite),
                    metadata,
                )
            )
        return tuple(results)

    @staticmethod
    def _select_codes(
        requested: Sequence[str], stocks_by_code: dict[str, object]
    ) -> tuple[str, ...]:
        normalized = {code.strip() for code in requested if code.strip()}
        selected = tuple(sorted(normalized or stocks_by_code.keys()))
        missing = tuple(code for code in selected if code not in stocks_by_code)
        if missing:
            raise StockNotFoundError(
                f"stock code(s) absent from local snapshot: {', '.join(missing)}"
            )
        return selected

    @staticmethod
    def _group_bars(items: Sequence[DailyBar]) -> dict[str, tuple[DailyBar, ...]]:
        grouped: dict[str, list[DailyBar]] = defaultdict(list)
        for item in items:
            grouped[item.code].append(item)
        return {
            code: tuple(sorted(values, key=lambda item: item.trading_day))
            for code, values in grouped.items()
        }

    @staticmethod
    def _group_dividends(
        items: Sequence[DividendRecord],
    ) -> dict[str, tuple[DividendRecord, ...]]:
        grouped: dict[str, list[DividendRecord]] = defaultdict(list)
        for item in items:
            grouped[item.code].append(item)
        return {code: tuple(values) for code, values in grouped.items()}
