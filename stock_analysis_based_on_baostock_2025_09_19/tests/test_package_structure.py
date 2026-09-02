from __future__ import annotations

from stock_manager import __version__


def test_package_version() -> None:
    assert __version__ == "1.13.9"
