from __future__ import annotations

from importlib import import_module

import pytest

from stock_manager import __version__


def test_package_version() -> None:
    assert __version__ == "1.17.0"


@pytest.mark.parametrize(
    "module_name",
    (
        "stock_manager.capm",
        "stock_manager.read.capm",
        "stock_manager.sync.capm",
        "stock_manager.web.app",
        "stock_manager.cli.main",
    ),
)
def test_capm_and_application_modules_import(module_name: str) -> None:
    """A clean checkout must contain the modules required by both entry points."""
    assert import_module(module_name).__name__ == module_name
