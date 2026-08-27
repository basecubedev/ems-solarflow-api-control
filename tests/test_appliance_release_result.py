# SPDX-License-Identifier: AGPL-3.0-or-later
"""One summary that cannot disagree with the reports it summarises.

The committed evidence was written by hand and drifted: a summary claiming
"79 pass, 0 not run" sat beside an image report recording 79 pass, 2 fail and
1 not run, and a bundle object count belonged to an older revision. Nobody
noticed, because nothing compared them.

Every number here is read out of the report that produced it, and readiness is
derived from the evidence rather than asserted beside it.
"""

import hashlib
import re
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.system_build, pytest.mark.appliance]

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

    assert payload["media"]["supported_media_label"] == "16 GB"
    assert payload["media"]["minimum_media_bytes"] > 14_000_000_000


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


# --- a kit manifest has to belong to this release ---------------------------


def test_a_kit_manifest_from_another_run_is_not_evidence_for_this_one(tmp_path):
    """--no-kit skips building the kit but the finalizer passed the path
    anyway, so a manifest left in the directory by an earlier run was read as
    this release's hardware readiness."""

    dist = tmp_path / "dist"
    write_reports(dist)
    stale = dist / "kit" / "kit-manifest.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        json.dumps(
            {
                "kit_version": 1,
                "physical_ready": True,
                "development_kit": False,
                "source_binding": {"bundle_sha256": "sha256:" + "a" * 64},
            }
        ),
        encoding="utf-8",
    )

    _result, payload = generate(dist, tmp_path, "--kit-manifest", str(stale))

    assert payload["hardware_kit"]["physical_ready"] is False
    assert "another release" in payload["hardware_kit"]["detail"]


def test_a_kit_manifest_bound_to_this_release_is_evidence(tmp_path):
    dist = tmp_path / "dist"
    write_reports(dist)
    bundle = tmp_path / "source-bundle.tar"
    bundle.write_bytes(b"a source bundle")
    digest = "sha256:" + hashlib.sha256(bundle.read_bytes()).hexdigest()
    manifest = dist / "kit" / "kit-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "kit_version": 1,
                "physical_ready": True,
                "development_kit": False,
                "source_binding": {"bundle_sha256": digest},
            }
        ),
        encoding="utf-8",
    )

    _result, payload = generate(
        dist, tmp_path, "--kit-manifest", str(manifest), "--source-bundle", str(bundle)
    )

    assert payload["hardware_kit"]["physical_ready"] is True


def test_the_finalizer_passes_no_kit_manifest_when_it_built_no_kit():
    """Belt and braces: the path is not handed over at all in --no-kit runs."""

    finalize = (ROOT / "scripts/appliance-finalize-rpi-release.sh").read_text(encoding="utf-8")
    unconditional = re.search(r'^\s*--kit-manifest "\$KIT/kit-manifest\.json" \\\\?$', finalize, re.M)

    assert unconditional is None, "the kit manifest path is passed unconditionally"


def test_a_run_without_a_kit_can_still_reach_a_verdict():
    """--no-kit asked for the kit invariant anyway, so it could never produce a
    ready result -- not a stricter one, an unreachable one."""

    from appliance import release_trust

    invariants = {name: True for name in release_trust.READINESS_INVARIANTS}
    verdict = release_trust.readiness(
        invariants, required=release_trust.READINESS_INVARIANTS
    )

    assert verdict.ready
    assert "hardware_kit_verified" not in verdict.to_dict()["invariants"]


def test_a_run_with_a_kit_still_has_to_prove_it():
    from appliance import release_trust

    invariants = {name: True for name in release_trust.READINESS_INVARIANTS}
    verdict = release_trust.readiness(
        invariants, required=release_trust.KIT_READINESS_INVARIANTS
    )

    assert not verdict.ready
    assert "hardware_kit_verified" in verdict.unmet


def test_the_result_records_which_invariant_set_was_applied():
    """"No kit was built" must never read as "the kit was verified"."""

    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "appliance_release_result.py"
    ).read_text(encoding="utf-8")

    assert 'result["hardware_kit_required"]' in source
    assert "release_trust.KIT_READINESS_INVARIANTS" in source
    assert "release_trust.READINESS_INVARIANTS" in source
