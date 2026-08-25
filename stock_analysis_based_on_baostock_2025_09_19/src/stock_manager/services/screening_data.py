"""Plan the minimum local data window required by a screening plan."""

from dataclasses import dataclass
from datetime import date, timedelta

from stock_manager.rules.base import WindowUnit
from stock_manager.templates.models import ScreeningPlan


@dataclass(frozen=True, slots=True)
class ScreeningDataPlan:
    market_start: date
    needs_fundamental: bool
    dividend_start: date | None


class ScreeningDataPlanner:
    def plan(
        self,
        screening_plan: ScreeningPlan,
        target_day: date,
        trading_days: tuple[date, ...],
    ) -> ScreeningDataPlan:
        if not trading_days or trading_days[-1] != target_day:
            raise ValueError("trading calendar must end on target_day")
        starts = [target_day]
        needs_fundamental = False
        dividend_years = 0
        for configured in screening_plan.enabled_rules:
            requirement = configured.data_requirement
            needs_fundamental = needs_fundamental or requirement.needs_fundamental
            dividend_years = max(
                dividend_years, requirement.dividend_calendar_years or 0
            )
            if requirement.market_history_unit is WindowUnit.CALENDAR_DAYS:
                assert requirement.history_length is not None
                starts.append(target_day - timedelta(days=requirement.history_length))
            elif requirement.market_history_unit is WindowUnit.TRADING_SESSIONS:
                assert requirement.history_length is not None
                index = max(0, len(trading_days) - requirement.history_length)
                starts.append(trading_days[index])
        dividend_start = (
            date(target_day.year - dividend_years, 1, 1)
            if dividend_years
            else None
        )
        return ScreeningDataPlan(min(starts), needs_fundamental, dividend_start)
