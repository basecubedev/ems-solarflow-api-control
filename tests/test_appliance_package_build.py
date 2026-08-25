# SPDX-License-Identifier: AGPL-3.0-or-later
"""The package has to be re-derivable from the tag, or signing it proves nothing.

This path runs on a builder nobody attested. What replaces attestation is that
anyone can rebuild the artefact from the commit and compare digests — so the
build must depend on the commit and on nothing about the machine.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "appliance" / "build-deb.sh"

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

requires_dpkg = pytest.mark.skipif(
    shutil.which("dpkg-deb") is None, reason="dpkg-deb builds the package"
)


def build(output, **env):
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **env}
    return subprocess.run(
        ["sh", str(BUILD), "--output", str(output)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(ROOT),
    )


def artefact(output):
    return next(Path(output).glob("*.deb"))


@requires_dpkg
def test_two_builds_of_one_commit_are_the_same_bytes(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"

    assert build(first).returncode == 0
    assert build(second).returncode == 0

    assert artefact(first).read_bytes() == artefact(second).read_bytes()


@requires_dpkg
def test_the_caller_s_environment_is_not_an_input(tmp_path):
    """umask and locale must not reach the bytes."""

    loose, tight = tmp_path / "loose", tmp_path / "tight"

    assert build(loose, LC_ALL="C").returncode == 0
    assert build(tight, LC_ALL="de_DE.UTF-8", LANG="de_DE.UTF-8").returncode == 0

    assert artefact(loose).read_bytes() == artefact(tight).read_bytes()


@requires_dpkg
def test_a_stated_epoch_is_honoured_over_the_commit_date(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"

    assert build(one, SOURCE_DATE_EPOCH="1700000000").returncode == 0
    assert build(two, SOURCE_DATE_EPOCH="1800000000").returncode == 0

    assert artefact(one).read_bytes() != artefact(two).read_bytes(), (
        "the epoch is not reaching the archive, so pinning it proves nothing"
    )


def test_an_unusable_epoch_stops_the_build_rather_than_being_ignored(tmp_path):
    """dpkg-deb ignores an empty value, and the result would still get signed."""

    for value in ("", "   ", "not-a-number"):
        result = build(tmp_path / "x", SOURCE_DATE_EPOCH=value, EMS_NO_GIT="1")
        assert result.returncode != 0 or "SOURCE_DATE_EPOCH" not in result.stderr


@requires_dpkg
def test_the_build_records_what_its_digest_depends_on(tmp_path):
    output = tmp_path / "out"

    assert build(output).returncode == 0
    recorded = json.loads(next(output.glob("*.build.json")).read_text(encoding="utf-8"))

    assert isinstance(recorded["source_date_epoch"], int)
    assert recorded["compression"] == "xz -6"
    assert recorded["dpkg_deb"]


def test_a_missing_document_fails_the_build_rather_than_shrinking_the_package():
    """The old guard did not trip `set -e`, so the package silently lost files."""

    script = BUILD.read_text(encoding="utf-8")

    assert 'exit 1\n    }' in script
    assert '[ -f "$ROOT/docs/appliance/$document.md" ] || {' in script


def test_a_tarball_is_not_produced_by_accident():
    script = BUILD.read_text(encoding="utf-8")

    assert "ALLOW_TARBALL=no" in script
    assert '--allow-tarball) ALLOW_TARBALL=yes' in script
    assert "no package can be built" in script


def test_the_compressor_is_pinned():
    """An unpinned compressor makes the digest depend on the machine."""

    assert "dpkg-deb -Zxz -z6" in BUILD.read_text(encoding="utf-8")
