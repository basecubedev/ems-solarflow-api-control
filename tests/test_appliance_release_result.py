# SPDX-License-Identifier: AGPL-3.0-or-later
"""One summary that cannot disagree with the reports it summarises.

The committed evidence was written by hand and drifted: a summary claiming
"79 pass, 0 not run" sat beside an image report recording 79 pass, 2 fail and
1 not run, and a bundle object count belonged to an older revision. Nobody
noticed, because nothing compared them.

Every number here is read out of the report that produced it, and readiness is
derived from the evidence rather than asserted beside it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.system_build]

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/appliance_release_result.py"


def write_reports(dist, *, profile="rpi5", image=(79, 0, 0), skipped=()):
    reports = dist / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    passed, failed, not_run = image
    (reports / f"image-inspection-{profile}.json").write_text(
        json.dumps(
            {
                "result": "fail" if failed else ("not_run" if skipped else "pass"),
                "counts": {"pass": passed, "fail": failed, "not_run": not_run},
                "mandatory_not_run": list(skipped),
            }
        )
    )
    for name in ("update-inspection", "sparse-crosscheck"):
        (reports / f"{name}-{profile}.json").write_text(
            json.dumps({"result": "pass", "counts": {"pass": 18, "fail": 0, "not_run": 0}})
        )
    return reports


def generate(dist, tmp_path, *args):
    output = tmp_path / "release-result.json"
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--dist", str(dist), "--output", str(output),
         "--profile", "rpi5", *args],
        capture_output=True, text=True, check=False, timeout=300,
    )
    payload = json.loads(output.read_text()) if output.is_file() else {}
    return result, payload


def test_the_counts_come_from_the_report_rather_than_from_a_claim(tmp_path):
    dist = tmp_path / "dist"
    write_reports(dist, image=(79, 2, 1), skipped=["gpt_independent_oracle"])

    _result, payload = generate(dist, tmp_path)

    inspection = payload["profiles"]["rpi5"]["image_inspection"]
    assert inspection["pass"] == 79
    assert inspection["fail"] == 2
    assert inspection["not_run"] == 1
    assert inspection["mandatory_not_run"] == ["gpt_independent_oracle"]


def test_a_report_that_failed_is_never_summarised_as_ready(tmp_path):
    dist = tmp_path / "dist"
    write_reports(dist, image=(79, 2, 1))

    result, payload = generate(dist, tmp_path)

    assert payload["physical_ready"] is False
    assert result.returncode == 1


def test_a_missing_report_is_not_run_rather_than_absent(tmp_path):
    dist = tmp_path / "dist"
    (dist / "reports").mkdir(parents=True)

    _result, payload = generate(dist, tmp_path)

    assert payload["profiles"]["rpi5"]["image_inspection"]["result"] == "not_run"
    assert payload["physical_ready"] is False


def test_a_release_gate_that_did_not_pass_blocks_readiness(tmp_path):
    dist = tmp_path / "dist"
    write_reports(dist)
    gate = tmp_path / "release-gate-report.txt"
    gate.write_text("inspect-image-rpi5 FAIL\nRESULT: FAIL (1 gate(s))\n")

    _result, payload = generate(dist, tmp_path, "--gate-report", str(gate))

    assert payload["release_gate"]["result"] == "fail"
    assert payload["physical_ready"] is False


def test_every_report_is_bound_by_its_own_digest(tmp_path):
    dist = tmp_path / "dist"
    reports = write_reports(dist)

    _result, payload = generate(dist, tmp_path)

    recorded = payload["profiles"]["rpi5"]["image_inspection"]["sha256"]
    assert recorded.startswith("sha256:")
    (reports / "image-inspection-rpi5.json").write_text(json.dumps({"result": "pass"}))
    _again, second = generate(dist, tmp_path)
    assert second["profiles"]["rpi5"]["image_inspection"]["sha256"] != recorded


def test_the_media_policy_travels_with_the_result(tmp_path):
    dist = tmp_path / "dist"
    write_reports(dist)

    _result, payload = generate(dist, tmp_path)

    assert payload["media"]["supported_media_label"] == "32 GB"
    assert payload["media"]["minimum_media_bytes"] > 16_000_000_000


def test_the_markdown_is_generated_from_the_same_numbers(tmp_path):
    dist = tmp_path / "dist"
    write_reports(dist, image=(79, 2, 1))
    summary = tmp_path / "summary.md"

    generate(dist, tmp_path, "--markdown", str(summary))

    text = summary.read_text()
    assert "79/2/1" in text
    assert "physical ready: **false**" in text.lower()


def test_a_run_that_proved_nothing_is_never_physically_ready(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()

    result, payload = generate(dist, tmp_path)

    assert payload["physical_ready"] is False
    assert payload["physical_tested"] is False
    assert result.returncode == 1
