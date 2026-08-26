"""Offline tests for the PyInstaller exe entry point (dev-mode helpers)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRY_PATH = ROOT / "scripts" / "exe_entry.py"


@pytest.fixture(scope="module")
def exe_entry():
    spec = importlib.util.spec_from_file_location("stockmanager_exe_entry", ENTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_prepare_creates_data_dir_and_empty_database(tmp_path, exe_entry) -> None:
    app_dir = tmp_path / "app"
    exe_entry.prepare(app_dir, app_dir)

    assert (app_dir / "data" / "locks").is_dir()
    assert (app_dir / "data" / "user-templates").is_dir()
    assert (app_dir / "data" / "market.sqlite3").is_file()


def test_prepare_copies_missing_config_from_resources(tmp_path, exe_entry) -> None:
    resource = tmp_path / "resource"
    (resource / "config").mkdir(parents=True)
    (resource / "config" / "sync.json").write_text("{}", encoding="utf-8")
    (resource / "config" / "rule_templates").mkdir()
    (resource / "config" / "rule_templates" / "x.json").write_text("{}", encoding="utf-8")

    app_dir = tmp_path / "app"
    exe_entry.prepare(app_dir, resource)

    assert (app_dir / "config" / "sync.json").read_text(encoding="utf-8") == "{}"
    assert (app_dir / "config" / "rule_templates" / "x.json").exists()
    # 幂等:再跑一次不覆盖已有文件
    (app_dir / "config" / "sync.json").write_text('{"edited":true}', encoding="utf-8")
    exe_entry.prepare(app_dir, resource)
    assert (app_dir / "config" / "sync.json").read_text(encoding="utf-8") == '{"edited":true}'
