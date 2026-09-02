"""Regression: Windows PyInstaller spawn must not re-enter main()."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).parents[1]


ENTRY_FILES = (
    REPO / "scripts" / "exe_entry.py",
    REPO / "src" / "stock_manager" / "cli" / "main.py",
    REPO / "src" / "stock_manager" / "cli" / "__main__.py",
)


def test_frozen_entries_call_freeze_support() -> None:
    """每个可执行入口必须在 __main__ 块中调用 multiprocessing.freeze_support()。

    PyInstaller frozen 应用 + ProcessPoolExecutor(spawn) 时,缺少该调用会让每个
    worker 子进程重新执行整个 main()(多开程序/重复启动 Web 服务与回填)。
    """
    for path in ENTRY_FILES:
        source = path.read_text(encoding="utf-8")
        assert "multiprocessing.freeze_support()" in source, f"{path} 缺少 freeze_support()"
        assert "if __name__ == \"__main__\":" in source, f"{path} 缺少 __main__ 保护"


def test_exe_entry_guards_main_under_main_name() -> None:
    """exe_entry 的 main() 调用必须在 __main__ 块内,防止 spawn 子进程重入。"""
    source = (REPO / "scripts" / "exe_entry.py").read_text(encoding="utf-8")
    main_block = source.split("if __name__ == \"__main__\":")[-1]
    assert "main()" in main_block
