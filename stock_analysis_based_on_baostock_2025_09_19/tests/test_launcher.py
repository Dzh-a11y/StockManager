"""Offline tests for the cross-platform launcher helpers."""

import importlib.util
import socket
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = ROOT / "scripts" / "launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("stockmanager_launcher", LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_venv_python_is_platform_aware(launcher, monkeypatch) -> None:
    monkeypatch.setattr(launcher.os, "name", "nt")
    assert launcher.venv_python() == launcher.VENV_DIR / "Scripts" / "python.exe"

    monkeypatch.setattr(launcher.os, "name", "posix")
    assert launcher.venv_python() == launcher.VENV_DIR / "bin" / "python"


def test_server_command_uses_venv_and_project_root(launcher) -> None:
    cmd = launcher.server_command()
    assert cmd[0].startswith(str(launcher.VENV_DIR))
    assert cmd[1:4] == ["-m", "stock_manager.cli", "web"]
    # 路径类参数都相对项目根,且真实存在
    path_args = [
        "data/market.sqlite3", "config/rule_templates", "data/user-templates",
        "src/stock_manager/web/static", "config/sync.json", "data/locks",
    ]
    for rel in path_args:
        assert (launcher.ROOT / rel).exists()


def test_server_running_is_false_on_closed_port(launcher, monkeypatch) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    # point the probe at a recently-closed port
    monkeypatch.setattr(launcher, "URL", f"http://127.0.0.1:{port}")
    assert launcher.server_running() is False
