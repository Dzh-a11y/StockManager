"""P5A-1 backfill watchdog: restart the v2 backfill runner on failure.

The runner itself (run_backfill_v2.py) retries transient network errors
in-process with a bounded attempt count. This watchdog is the process-level
layer: if the whole runner process exits non-zero (crash, OOM kill, signal,
or exhausted in-process retries), it waits a cooldown and relaunches. The v2
backfill is idempotent and resumes from completed chunks, so every restart
makes progress instead of redoing completed work.

The watchdog writes its own log to data/backfill_logs/backfill_watchdog.log
(absolute path, so no shell redirect is needed). Run from the workspace root:

    cd stock_analysis_based_on_baostock_2025_09_19
    nohup .venv/bin/python -u scripts/run_backfill_watchdog.py \
        [--restart-cooldown 30] [--max-restarts 0] &

Exit codes:
    0 = backfill finished (window covered or completed);
    1 = gave up after --max-restarts restarts.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
PID_FILE = ROOT / "data" / "backfill_logs" / "backfill_watchdog.pid"
LOG_FILE = ROOT / "data" / "backfill_logs" / "backfill_watchdog.log"

LOG_HANDLE = None


def _redirect_log() -> None:
    """Append stdout/stderr (watchdog and runner) to the watchdog's own log."""
    global LOG_HANDLE
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_HANDLE = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 - held for process lifetime
    sys.stdout = LOG_HANDLE
    sys.stderr = LOG_HANDLE


def _write_pid() -> None:
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(__import__("os").getpid()), encoding="utf-8")
    except OSError:
        pass


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="restart the v2 backfill runner whenever it fails"
    )
    parser.add_argument(
        "--restart-cooldown",
        type=float,
        default=30.0,
        help="seconds to wait between restarts (default 30)",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=0,
        help="max restarts before giving up; 0 = restart until success (default 0)",
    )
    parser.add_argument(
        "--runner",
        type=Path,
        default=ROOT / "scripts" / "run_backfill_v2.py",
        help="runner script to relaunch (default scripts/run_backfill_v2.py)",
    )
    args, runner_args = parser.parse_known_args()

    cooldown = max(args.restart_cooldown, 0.0)
    max_restarts = max(args.max_restarts, 0)
    runner = args.runner.resolve()
    restarts = 0
    attempt = 0
    _redirect_log()
    _write_pid()
    try:
        while True:
            attempt += 1
            print(
                f"[watchdog] launching backfill runner #{attempt} (restarts={restarts})",
                flush=True,
            )
            result = subprocess.run(
                [str(VENV_PYTHON), "-u", str(runner), *runner_args],
                cwd=str(ROOT),
                stdout=LOG_HANDLE,
                stderr=LOG_HANDLE,
            )
            if result.returncode == 0:
                print("[watchdog] backfill finished (window covered or completed)", flush=True)
                return 0
            restarts += 1
            print(
                f"[watchdog] runner exited rc={result.returncode} (failure #{restarts}); "
                f"restarting in {cooldown:g}s ...",
                flush=True,
            )
            if max_restarts and restarts > max_restarts:
                print(f"[watchdog] gave up after {restarts} restarts", flush=True)
                return 1
            time.sleep(cooldown)
    finally:
        _remove_pid()


if __name__ == "__main__":
    sys.exit(main())
