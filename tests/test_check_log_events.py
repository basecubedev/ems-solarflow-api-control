# SPDX-License-Identifier: AGPL-3.0-or-later
import subprocess
import sys
from pathlib import Path

from scripts.check_log_events import extract_events


ROOT = Path(__file__).resolve().parents[1]
CHECK_LOG_EVENTS = ROOT / "scripts/check_log_events.py"


def run_checker(log_file, *args):
    return subprocess.run(
        [sys.executable, str(CHECK_LOG_EVENTS), str(log_file), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_extract_events_finds_structured_event_names_only():
    text = (
        "event=startup device=WR1\n"
        "prefix_event=ignored\n"
        "level=info event=replay_frame source=test\n"
        "event=ems-stopped\n"
        "event=namespace:detail\n"
    )

    assert extract_events(text) == {
        "startup",
        "replay_frame",
        "ems-stopped",
        "namespace:detail",
    }


def test_check_log_events_accepts_required_and_absent_forbidden_events(tmp_path):
    log_file = tmp_path / "ems.log"
    log_file.write_text(
        "2026-06-03 | INFO | event=startup\n"
        "2026-06-03 | INFO | event=replay_stopped reason=max_cycles\n"
    )

    result = run_checker(
        log_file,
        "--require",
        "startup",
        "--require",
        "replay_stopped",
        "--forbid",
        "startup_abort",
    )

    assert result.returncode == 0, result.stderr
    assert "OK required event found: startup" in result.stdout
    assert "OK forbidden event absent: startup_abort" in result.stdout


def test_check_log_events_reports_missing_and_forbidden_events(tmp_path):
    log_file = tmp_path / "ems.log"
    log_file.write_text("event=startup\n")

    result = run_checker(
        log_file,
        "--require",
        "ems_stopped",
        "--forbid",
        "startup",
    )

    assert result.returncode == 1
    assert "Missing required events:" in result.stderr
    assert "ems_stopped" in result.stderr
    assert "Forbidden events found:" in result.stderr
    assert "startup" in result.stderr
    assert "Observed events: startup" in result.stderr


def test_check_log_events_missing_file_returns_read_error(tmp_path):
    result = run_checker(tmp_path / "missing.log", "--require", "startup")

    assert result.returncode == 2
    assert "ERROR: cannot read log file" in result.stderr
