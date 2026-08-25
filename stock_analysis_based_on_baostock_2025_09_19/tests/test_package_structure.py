from stock_manager import __version__


def test_package_version() -> None:
    assert __version__ == "1.1.1"
