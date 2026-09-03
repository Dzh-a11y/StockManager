"""Data-page reference controls use the shared runner without market coupling."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import AdjustmentMethod, VerificationStatus
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.web.app import WebApp, WebConfig
from test_capm_sync_pipeline import END, START, make_service
from test_web_api import SYSTEM_TEMPLATES, STATIC_ROOT, REPO, _post, _get, _get_query


def app_for(tmp_path: Path) -> WebApp:
    return WebApp(WebConfig(database_path=tmp_path / "db.sqlite3",
        system_template_root=SYSTEM_TEMPLATES, user_template_root=tmp_path / "templates",
        static_root=STATIC_ROOT, sync_config_path=REPO / "config/sync.json",
        lock_directory=tmp_path / "locks"))


def test_capm_button_launches_shared_runner_with_own_dataset_and_never_stock_adjustment(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    with mock.patch("subprocess.Popen", return_value=mock.Mock(pid=123)) as spawn:
        status, response = _post(app, "/api/sync/capm-reference", {})
        assert status == 200, response
        argv = spawn.call_args.args[0]
        assert "run_backfill_v2.py" in " ".join(argv)
        assert argv[argv.index("--dataset")+1] == "capm"
        assert argv[argv.index("--adjustment")+1] == "unadjusted"
        assert "--end" not in argv  # Shanghai calendar/cutoff belongs to service.
        assert _post(app, "/api/sync/capm-reference", {})[0] == 409
        assert spawn.call_count == 1
    assert _get(app, "/api/sync/progress")[1]["dataset_id"] == "market"
    assert _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "capm"})[1]["runner"]["status"] == "STARTING"
    assert _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "market"})[1]["runner"] is None


def test_preplan_failure_and_stopped_runner_are_visible_not_generic_500(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    repo = SQLiteRepository(tmp_path / "db.sqlite3")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    repo.set_sync_runner("capm", "FAILED", 123, now, "fixture calendar outage")
    status, result = _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "capm"})
    assert status == 200
    assert result["runner"]["message"] == "fixture calendar outage"
    repo.set_sync_runner("capm", "RUNNING", 123, now - timedelta(minutes=1), "pending")
    assert _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "capm"})[1]["runner"]["status"] == "INTERRUPTED"
    assert _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "wrong"})[0] == 400
    assert _post(app, "/api/sync/capm-reference", {"start": "2026-09-02", "end": "2026-09-01"})[0] == 400


def test_two_unique_data_buttons_and_separate_progress_channels() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'\s+id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    gate = html.split('id="gate-view"')[1].split('id="workbench-view"')[0]
    assert 'id="bootstrap-start"' in gate
    assert 'id="capm-reference-sync"' in gate
    assert "股票筛选池回补和同步" in gate
    assert "CAPM 回补和同步" in gate
    assert "与股票池共用限速通道" not in html
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert "/api/sync/pipeline/progress?dataset_id=capm" in script
    assert "capm-progress-fill" in script
    assert "缺口已披露，未伪造数据" not in script
    assert "历史不完整" in script


def test_runner_launch_failure_preserves_actionable_error(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    with mock.patch("subprocess.Popen", side_effect=OSError("fixture permission denied")):
        status, response = _post(app, "/api/sync/capm-reference", {})
    assert status == 400
    assert "fixture permission denied" in response["error"]["message"]
    assert _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "capm"})[1]["runner"]["status"] == "FAILED"


@pytest.mark.parametrize("verification_status,invalid_count,duplicate_count,is_warning", [
    ("ACCEPTED_WITH_GAPS", 0, 0, True),
    ("ACCEPTED_WITH_GAPS", 1, 0, False),
    ("ACCEPTED_WITH_GAPS", 0, 1, False),
    ("INCOMPLETE", 0, 0, False),
    ("INCOMPLETE", 1, 0, False),
])
def test_capm_progress_distinguishes_accepted_source_gaps_from_errors(
    tmp_path: Path, verification_status: str, invalid_count: int, duplicate_count: int, is_warning: bool,
) -> None:
    service, repository, _, _ = make_service(tmp_path)
    run = service.sync_capm_reference_data(START, END)
    assert run.candidate is not None
    original = next(v for v in repository.list_coverage_verifications(run.candidate.candidate_generation_id)
                    if v.data_type == "index_daily_bars")
    missing = tuple(f"2026-08-{day:02}" for day in (21, 24, 25, 26, 27, 28))
    verification = replace(original, expected_count=8, actual_count=2, distinct_count=2,
        invalid_count=invalid_count, duplicate_count=duplicate_count,
        coverage_ratio=Decimal("0.25"), missing_items=missing,
        status=VerificationStatus(verification_status), details_json=json.dumps({
            "codes": ["fixture.price"], "index_id": "fixture.price",
            "provider_code": "sh.000940", "index_name": "财富大盘",
            "unavailable_ranges": [[missing[0], missing[-1]]],
        }))
    app = app_for(tmp_path)
    with mock.patch.object(app._services.repository, "list_coverage_verifications", return_value=(verification,)):
        status, payload = _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "capm"})
    assert status == 200
    selected = payload["warnings"] if is_warning else payload["errors"]
    other = payload["errors"] if is_warning else payload["warnings"]
    assert not other
    assert len(selected) == 1
    assert "财富大盘" in selected[0] and "sh.000940" in selected[0]
    assert "6" in selected[0] and "前 5" in selected[0]
    assert missing[-1] not in selected[0]  # Preview is labelled, not presented as a full list.
    if is_warning:
        detail = payload["gap_details"][0]
        assert detail["missing_count"] == 6
        assert detail["missing_dates"] == list(missing)
        assert detail["provider_code"] == "sh.000940"
        assert detail["coverage_ratio"] == "0.25"


@pytest.mark.parametrize("dataset_id", ["market", "capm"])
def test_progress_never_accepts_gaps_for_stock_dataset_or_wrong_adjustment(tmp_path: Path, dataset_id: str) -> None:
    service, repository, _, _ = make_service(tmp_path)
    run = service.sync_capm_reference_data(START, END)
    assert run.candidate is not None
    plan = repository.get_sync_plan(run.plan_id)
    assert plan is not None
    wrong_plan = replace(plan, dataset_id=dataset_id, adjustment=AdjustmentMethod.QFQ)
    original = next(v for v in repository.list_coverage_verifications(run.candidate.candidate_generation_id)
                    if v.data_type == "index_daily_bars")
    verification = replace(original, status=VerificationStatus("ACCEPTED_WITH_GAPS"),
        missing_items=("2026-09-01",), details_json='{"codes":["fixture.price"]}')
    app = app_for(tmp_path)
    repo = app._services.repository
    with (mock.patch.object(repo, "list_sync_plans", return_value=(wrong_plan,)),
          mock.patch.object(repo, "get_sync_plan", return_value=wrong_plan),
          mock.patch.object(repo, "list_coverage_verifications", return_value=(verification,))):
        status, payload = _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": dataset_id})
    assert status == 200
    assert payload["warnings"] == [] and payload["gap_details"] == []
    assert "fixture.price" in payload["errors"][0]


def test_source_gap_publication_is_exposed_as_warning_with_partial_coverage(tmp_path: Path) -> None:
    service, _, provider, _ = make_service(tmp_path)
    provider.missing_day = END
    assert service.sync_capm_reference_data(START, END).published
    app = app_for(tmp_path)
    status, progress = _get_query(app, "/api/sync/pipeline/progress", {"dataset_id": "capm"})
    assert status == 200 and progress["status"] == "SUCCEEDED"
    assert progress["errors"] == []
    assert len(progress["warnings"]) == 1
    assert "沪深300指数" in progress["warnings"][0]
    assert "sh.000300" in progress["warnings"][0]
    assert progress["gap_details"][0]["missing_dates"] == [END.isoformat()]
    coverage_status, coverage = _get(app, "/api/sync/capm/status")
    assert coverage_status == 200 and coverage["coverage_status"] == "PARTIAL"
    assert coverage["gap_index_count"] == 1 and coverage["missing_count"] == 1
    assert coverage["indexes"][0]["missing_dates"] == [END.isoformat()]


def test_capm_warning_state_and_complete_gap_details_render_offline() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the offline browser test")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
function element() {
  const classes = new Set();
  return {textContent: '', hidden: false, disabled: false, style: {}, children: [],
    classList: {add: (name) => classes.add(name), contains: (name) => classes.has(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); }},
    replaceChildren(...items) { this.children = items; },
    append(...items) { this.children.push(...items); }};
}
let progress = {status: 'SUCCEEDED', plan_id: 'p1', generation: {generation: 'g1'},
  completed_tasks: 3, total_tasks: 3, progress: 1, errors: [], warnings: ['财富大盘 sh.000940：本次来源返回缺少 6 个交易日（前 5 个示例）']};
let coverage = {generation: 'g1', coverage_status: 'PARTIAL', index_count: 1,
  gap_index_count: 1, missing_count: 6, bar_count: 2, rate_count: 1, indexes: [
    {name: '财富大盘', provider_code: 'sh.000940', index_id: 'fixture.price', return_version: 'price',
     coverage_start: '2026-08-20', coverage_end: '2026-08-31', bar_count: 2,
     expected_count: 8, missing_count: 6, coverage_ratio: '0.25', coverage_status: 'PARTIAL',
     missing_dates: ['2026-08-21', '2026-08-24', '2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28'],
     unavailable_ranges: [['2026-08-21', '2026-08-28']]}]};
const context = vm.createContext({document: {querySelector(selector) {
  if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector);
}, createElement: element, addEventListener() {}}, window: {addEventListener() {}},
fetch: async (url) => ({ok: true, status: 200, text: async () => JSON.stringify(
  url.endsWith('/capm/status') ? coverage : progress)}), console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
function allText(item) { return item.textContent + item.children.map(allText).join(' '); }
(async () => {
  await vm.runInContext('pollCapmProgress()', context);
  const status = nodes.get('#capm-reference-status');
  assert.match(status.textContent, /已发布/);
  assert.match(status.textContent, /历史不完整/);
  assert.doesNotMatch(status.textContent, /缺口已披露，未伪造数据/);
  assert.equal(status.classList.contains('sync-error'), false);
  assert.equal(status.classList.contains('sync-warning'), true);
  const warning = nodes.get('#capm-reference-warnings');
  assert.equal(warning.hidden, false);
  assert.match(warning.textContent, /财富大盘.*sh.000940/);
  const detail = allText(nodes.get('#capm-index-coverage'));
  assert.match(detail, /sh.000940/);
  assert.match(detail, /2026-08-28/);
  assert.match(detail, /6.*交易日/);
  assert.equal(nodes.get('#capm-reference-sync').disabled, false);
  progress = {...progress, status: 'FAILED', errors: ['sh.000940：非法值 1'], warnings: []};
  await vm.runInContext('pollCapmProgress()', context);
  assert.match(status.textContent, /同步失败.*显式重试/);
  assert.equal(status.classList.contains('sync-error'), true);
  assert.equal(status.classList.contains('sync-warning'), false);
  progress = {...progress, status: 'SUCCEEDED', generation: {generation: 'g2'}, errors: []};
  coverage = {...coverage, generation: 'g2', coverage_status: 'COMPLETE', gap_index_count: 0,
    missing_count: 0, indexes: []};
  await vm.runInContext('pollCapmProgress()', context);
  assert.equal(status.classList.contains('sync-warning'), false);
  assert.doesNotMatch(status.textContent, /历史不完整/);
  assert.equal(warning.hidden, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, str(STATIC_ROOT / "app.js")],
        capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
