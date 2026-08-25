"""External and deterministic market-data providers."""

from stock_manager.providers.baostock_provider import BaostockProvider
from stock_manager.providers.fixture_provider import FixtureProvider

__all__ = ["BaostockProvider", "FixtureProvider"]
