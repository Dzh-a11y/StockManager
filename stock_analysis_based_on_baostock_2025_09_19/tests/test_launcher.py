"""Offline tests for the cross-platform launcher helpers."""

from __future__ import annotations

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
    # 仓库内资源必须存在;data/ 目录是运行时创建的 gitignored 目录,CI 干净检出里不存在,
    # 所以这里只断言仓库内的资源,不要求 data/ 存在。
    for rel in (
        "config/sync.json",
        "config/rule_templates",
        "src/stock_manager/web/static",
    ):
        assert (launcher.ROOT / rel).exists()


def test_server_running_is_false_on_closed_port(launcher, monkeypatch) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    # point the probe at a recently-closed port
    monkeypatch.setattr(launcher, "URL", f"http://127.0.0.1:{port}")
    assert launcher.server_running() is False


def test_ensure_environment_rejects_python_below_311(launcher, monkeypatch) -> None:
    class _OldVersion:
        major, minor = 3, 9

        def __lt__(self, other):  # drives "sys.version_info < (3, 11)"
            return (self.major, self.minor) < (other[0], other[1])

    monkeypatch.setattr(launcher.sys, "version_info", _OldVersion())
    assert launcher._ensure_environment() is False


def test_desktop_shortcut_is_noop_off_windows(launcher, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *args, **kwargs: calls.append(args)
    )
    launcher._ensure_desktop_shortcut()
    assert calls == []


def test_desktop_shortcut_builds_powershell_command_on_windows(
    launcher, monkeypatch, tmp_path
) -> None:
    calls: list = []
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: (calls.append(args), type("R", (), {"returncode": 0})())[1],
    )

    launcher._ensure_desktop_shortcut()

    assert calls
    argv = calls[0][0]
    assert argv[0] == "powershell"
    command = " ".join(argv)
    assert "StockManager.lnk" in command
    assert "StockManager.bat" in command
