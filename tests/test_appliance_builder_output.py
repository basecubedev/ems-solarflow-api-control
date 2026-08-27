# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the host actually received what the builder guest produced.

The builder copied its output back with ``scp ... || true`` and then printed
RESULT: PASS. A dropped 16 GiB image over a slirp link, a full host disk and a
complete build all produced the same verdict — and the evidence a release is
signed from is exactly the part most likely to go missing.

The guest now describes its output before anything moves, the host re-hashes
what arrived, and only a verified staging directory is published. Logs are the
one optional class: useful to read, never evidence.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/appliance_builder_output.py"
BUILDER_VM = ROOT / "scripts/appliance-builder-vm.sh"


def run(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, check=False
    )


def authority(profile="rpi5", build_id="20260809120000", completed=True):
    return json.dumps(
        {
            "schema_version": 3,
            "profile": profile,
            "build_id": build_id,
            "completed": completed,
            "builder_environment_sha256": "sha256:" + "ab" * 32,
        }
    )


def build_dist(tmp_path, *, completed=True, with_image=True):
    dist = tmp_path / "dist"
    (dist / "gates").mkdir(parents=True)
    (dist / "reports").mkdir()
    (dist / "reports/image-inspection-rpi5.json").write_text('{"result": "pass"}')
    # The build's own work root: a chroot and the image it produced.
    # None of it is an artefact and none of it may be described as one.
    (dist / "build-20260809120000/rootfs/usr/bin").mkdir(parents=True)
    (dist / "build-20260809120000/rootfs/usr/bin/sh").write_bytes(b"chroot content")
    name = "ems-solarflow-appliance-0.1.0-rpi5-arm64"
    (dist / f"{name}.build-authority.json").write_text(authority(completed=completed))
    if with_image:
        (dist / f"{name}.img").write_bytes(b"an image" * 512)
    (dist / f"{name}.build.log").write_text("noise")
    (dist / "gates" / "build-rpi5.log").write_text("gate log")
    return dist, name


def describe(dist, manifest):
    result = run("describe", "--dist", str(dist), "--output", str(manifest))
    assert result.returncode == 0, result.stderr
    return json.loads(Path(manifest).read_text())


def test_the_guest_describes_every_file_it_produced(tmp_path):
    dist, name = build_dist(tmp_path)

    manifest = describe(dist, tmp_path / "manifest.json")

    paths = {entry["path"] for entry in manifest["files"]}
    assert f"{name}.img" in paths
    assert "gates/build-rpi5.log" in paths
    assert "reports/image-inspection-rpi5.json" in paths
    assert all(entry["sha256"].startswith("sha256:") for entry in manifest["files"])


def test_the_build_work_root_is_never_described_as_an_artefact(tmp_path):
    """A chroot and a 16 GiB image live under dist too, and are not output."""

    dist, _ = build_dist(tmp_path)

    manifest = describe(dist, tmp_path / "manifest.json")

    paths = {entry["path"] for entry in manifest["files"]}
    assert not any(path.startswith("build-") for path in paths), sorted(paths)
    assert len(paths) < 20


def test_only_logs_are_optional(tmp_path):
    dist, name = build_dist(tmp_path)

    manifest = describe(dist, tmp_path / "manifest.json")

    optional = {entry["path"] for entry in manifest["files"] if not entry["required"]}
    assert optional == {f"{name}.build.log", "gates/build-rpi5.log"}


def test_a_complete_copy_passes(tmp_path):
    dist, _ = build_dist(tmp_path)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 0
    assert "RESULT: PASS" in result.stdout


def test_a_dropped_image_is_a_failure_not_a_pass(tmp_path):
    dist, name = build_dist(tmp_path)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)
    (dist / f"{name}.img").unlink()

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 1
    assert "artifact_copy_failed" in result.stdout
    assert f"missing: {name}.img" in result.stdout


def test_a_truncated_copy_is_a_failure(tmp_path):
    dist, name = build_dist(tmp_path)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)
    (dist / f"{name}.img").write_bytes(b"short")

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 1
    assert "truncated" in result.stdout


def test_a_corrupt_copy_of_the_right_size_is_a_failure(tmp_path):
    dist, name = build_dist(tmp_path)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)
    original = (dist / f"{name}.img").read_bytes()
    (dist / f"{name}.img").write_bytes(b"X" + original[1:])

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 1
    assert "corrupt" in result.stdout


def test_a_missing_log_is_not_a_failure(tmp_path):
    dist, name = build_dist(tmp_path)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)
    (dist / f"{name}.build.log").unlink()

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 0


def test_an_incomplete_build_authority_is_a_failure(tmp_path):
    dist, _ = build_dist(tmp_path, completed=False)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 1
    assert "incomplete" in result.stdout


def test_a_build_authority_with_no_image_is_a_failure(tmp_path):
    dist, _ = build_dist(tmp_path, with_image=False)
    manifest = tmp_path / "manifest.json"
    describe(dist, manifest)

    result = run("verify", "--manifest", str(manifest), "--directory", str(dist))

    assert result.returncode == 1
    assert "image file" in result.stdout


def test_the_builder_never_reports_pass_after_an_ignored_copy(tmp_path):
    text = BUILDER_VM.read_text(encoding="utf-8")

    assert "appliance_builder_output.py describe" in text
    assert "appliance_builder_output.py\" verify" in text
    assert "artifact_copy_failed" in text
    assert 'bcp "builder@127.0.0.1:/build/dist/$artefact" "$OUTPUT/" >/dev/null 2>&1 || true' \
        not in text
