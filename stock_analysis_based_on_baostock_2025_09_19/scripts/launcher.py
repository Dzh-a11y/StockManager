"""Cross-platform launcher for the StockManager local Web workbench.

A desktop double-click entry (macOS ``StockManager.command``, Windows
``StockManager.bat``) runs ``launcher.py start``. The whole flow is idempotent:
if the server is already up it simply opens the browser, so re-clicking the
desktop icon never errors or double-starts.

Pure standard library; no new dependencies. ``--db``/log/pid paths are kept
inside ``data/`` and are git-ignored.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
PID_FILE = ROOT / "data" / "server.pid"
LOG_FILE = ROOT / "data" / "server.log"
HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"

SERVER_ARGS = [
    "--db", "data/market.sqlite3",
    "--system-templates", "config/rule_templates",
    "--user-templates", "data/user-templates",
    "--static", "src/stock_manager/web/static",
    "--sync-config", "config/sync.json",
    "--lock-dir", "data/locks",
    "--host", HOST,
    "--port", str(PORT),
]


def venv_python() -> Path:
    """Return the virtualenv's Python interpreter path (platform-aware)."""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def server_command() -> list[str]:
    """The exact command used to launch the Web server (relative to ``ROOT``)."""
    return [str(venv_python()), "-m", "stock_manager.cli", "web", *SERVER_ARGS]


def server_running() -> bool:
    """Probe ``/health`` to decide whether the server is already up."""
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def _open_browser() -> None:
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _ensure_environment() -> bool:
    """Create the virtualenv / install deps if missing. Return True when ready."""
    if not VENV_DIR.exists():
        print("未找到虚拟环境,正在创建 .venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=False)
    python = venv_python()
    if not python.exists():
        print("虚拟环境创建失败,请手动执行: python3 -m venv .venv")
        return False
    import_check = subprocess.run(
        [str(python), "-c", "import stock_manager"], cwd=ROOT
    )
    if import_check.returncode != 0:
        print("正在安装依赖(首次可能需要一两分钟)...")
        result = subprocess.run(
            [str(python), "-m", "pip", "install", "-e", ".[dev]"], cwd=ROOT
        )
        if result.returncode != 0:
            print("依赖安装失败,请检查网络后重试,或手动执行: "
                  f"\"{python}\" -m pip install -e '.[dev]'")
            return False
    return True


def _spawn_server(log_handle) -> int:
    """Launch the server detached from this process and return its PID."""
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    proc = subprocess.Popen(
        server_command(),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        creationflags=creationflags,
        close_fds=True,
    )
    return proc.pid


def _wait_until(timeout: float, predicate) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def start() -> int:
    """Ensure the server is running, then open the browser. Idempotent."""
    if server_running():
        print(f"服务已在运行:{URL}")
        _open_browser()
        return 0
    if not _ensure_environment():
        return 1
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("ab") as log_handle:
        pid = _spawn_server(log_handle)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{pid}\n", encoding="utf-8")
    print(f"服务已启动 (pid {pid}),等待就绪...")
    if _wait_until(20.0, server_running):
        print(f"就绪:{URL}")
        print(f"日志:{LOG_FILE}")
        _open_browser()
        return 0
    print(f"启动失败,请查看日志:{LOG_FILE}")
    return 1


def stop() -> int:
    """Stop a launcher-owned server. Refuses to kill non-launcher instances."""
    if not server_running():
        print("服务未在运行")
        _remove_pid()
        return 0
    pid = _read_pid()
    if pid is None:
        print("服务在运行但没有 PID 文件(可能非本启动器启动)。"
              "可使用页面「停止服务」按钮,或 lsof -nP -i :8000 手动结束。")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    if _wait_until(12.0, lambda: not server_running()):
        print("服务已停止")
        _remove_pid()
        return 0
    print("服务未响应,可强制结束或稍后重试")
    return 1


def status() -> int:
    if server_running():
        pid = _read_pid()
        print(f"运行中 {URL} (pid {pid if pid else '未知'})")
        print(f"日志:{LOG_FILE}")
    else:
        print("未运行")
    return 0


def restart() -> int:
    stop()
    return start()


def _print_usage() -> None:
    print("StockManager 启动器")
    print("  用法: python scripts/launcher.py [start|stop|restart|status]")
    print("  默认: start(启动并打开浏览器)")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else "start"
    if command in ("start",):
        return start()
    if command == "stop":
        return stop()
    if command == "restart":
        return restart()
    if command == "status":
        return status()
    _print_usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
