from pathlib import Path
p = Path("tests/test_historical_runs.py")
text = p.read_text(encoding="utf-8")

old = '''def _run(run_id: str = "run-1", status: HistoricalRunStatus = HistoricalRunStatus.QUEUED) -> HistoricalScreeningRun:'''
new = '''def _run(
    run_id: str = "run-1",
    status: HistoricalRunStatus = HistoricalRunStatus.QUEUED,
    started_at: datetime = NOW,
) -> HistoricalScreeningRun:'''
assert old in text
text = text.replace(old, new, 1)

old2 = '''        status=status,
        progress_completed=0,
        progress_total=10,
        started_at=NOW,
        finished_at=None,
        error_message=None,
    )'''
new2 = '''        status=status,
        progress_completed=0,
        progress_total=10,
        started_at=started_at,
        finished_at=None,
        error_message=None,
    )'''
assert old2 in text
text = text.replace(old2, new2, 1)

old3 = '''    store, repo = _store(tmp_path)
    store._clock.advance(5)  # run-2 更晚,保证排序稳定
    store.create(_run())'''
new3 = '''    store, repo = _store(tmp_path)
    store.create(_run())'''
assert old3 in text
text = text.replace(old3, new3, 1)

old4 = '''    store.create(_run(run_id="run-2"))
    store.start_validating("run-2")
    store.start_building("run-2")
    store.succeed("run-2", 1, 1)'''
new4 = '''    store.create(_run(run_id="run-2", started_at=NOW + timedelta(minutes=5)))
    store.start_validating("run-2")
    store.start_building("run-2")
    store.succeed("run-2", 1, 1)'''
assert old4 in text
text = text.replace(old4, new4, 1)

old5 = '''    store.create(_run())
    store.start_building("run-1")"""
new5 = '''    store.create(_run())
    store.start_validating("run-1")"""

# interrupted 测试里的 start_building 前补 validating
old6 = '''def test_interrupted_recovery_and_requeue(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    store.start_building("run-1")'''
new6 = '''def test_interrupted_recovery_and_requeue(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.create(_run())
    store.start_validating("run-1")
    store.start_building("run-1")'''
assert old6 in text, 'interrupted block not found'
text = text.replace(old6, new6, 1)

p.write_text(text, encoding="utf-8")
print("tests fixed")
