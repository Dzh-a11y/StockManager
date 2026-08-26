"""PyInstaller 打包入口:让 StockManager 以单个 exe 运行。

双击 ``StockManager.exe`` 后:
  1. 在 exe 旁边准备 data/(SQLite、日志、锁)与可编辑 config/(sync.json、系统模板);
  2. 以本地路径启动 Web 服务(打包的静态资源从 PyInstaller 解包目录读取);
  3. 服务就绪后自动打开浏览器。

数据全部存放在 exe 所在目录,因此 exe 可以被复制到任意文件夹运行。
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8000
URL = f"http://127.0.0.1:{PORT}"


def app_dir() -> Path:
    """exe 所在目录(数据存放处);开发模式 = 项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """打包资源目录(PyInstaller 解包处);开发模式 = 项目根。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(app_dir())))
    return Path(__file__).resolve().parent.parent


def _copy_if_missing(src: Path, dst: Path) -> None:
    if dst.exists() or not src.exists():
        return
    if src.resolve() == dst.resolve():
        return  # 开发模式:资源与目标同目录,无需复制
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def prepare(app_dir_path: Path, resource_dir_path: Path) -> None:
    """首次运行:创建数据目录/空库,并把可编辑配置复制到 exe 旁边。"""
    data = app_dir_path / "data"
    (data / "locks").mkdir(parents=True, exist_ok=True)
    (data / "user-templates").mkdir(exist_ok=True)

    if not (data / "market.sqlite3").exists():
        # Web 启动要求数据库文件存在;这里建空库(backfill 会自动填充)
        from stock_manager.storage.sqlite_repo import SQLiteRepository

        SQLiteRepository(data / "market.sqlite3")

    _copy_if_missing(
        resource_dir_path / "config" / "sync.json",
        app_dir_path / "config" / "sync.json",
    )
    _copy_if_missing(
        resource_dir_path / "config" / "rule_templates",
        app_dir_path / "config" / "rule_templates",
    )


def _wait_until_running_and_open(timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(URL)
                    return
        except Exception:
            time.sleep(0.5)


def main() -> int:
    root = app_dir()
    prepare(root, resource_dir())

    from stock_manager.web.config import WebConfig
    from stock_manager.web.httpd import run_web

    config = WebConfig(
        database_path=root / "data" / "market.sqlite3",
        system_template_root=root / "config" / "rule_templates",
        user_template_root=root / "data" / "user-templates",
        static_root=resource_dir() / "src" / "stock_manager" / "web" / "static",
        sync_config_path=root / "config" / "sync.json",
        lock_directory=root / "data" / "locks",
        host="127.0.0.1",
        port=PORT,
    )
    config.validate()
    threading.Thread(target=_wait_until_running_and_open, daemon=True).start()
    run_web(config)  # serve_forever,阻塞直到进程被终止
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
