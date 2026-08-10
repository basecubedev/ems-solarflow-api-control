# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-boot growth against real growpart, resize2fs and a real GPT.

The unit tier substitutes the partitioning tools, so two things stay unproven
there: whether the geometry this project reads out of sysfs and the GPT agrees
with what growpart does to a real table, and whether the kernel has re-read that
table by the time resize2fs runs. Both are what the geometry fix is about, and
both need a real block device.

The medium is an image file this test creates, on a loop device inside a
disposable privileged container. No host storage is reachable from it: the
repository is mounted read-only and the container is discarded.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.system_build]

ROOT = Path(__file__).resolve().parents[1]
DRIVER = "scripts/appliance-test-grow-persistent.sh"

# Pinned so the tier is the same Debian the appliance and the builder are.
GUEST_IMAGE = "debian:trixie-slim"

GUEST = f"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends \
    util-linux fdisk e2fsprogs cloud-guest-utils python3 mount >/dev/null 2>&1
mkdir -p /work/repo
cp -a /src/. /work/repo/
sh /work/repo/{DRIVER} --work /work/scratch
"""

requires_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="a real Docker daemon runs this tier"
)


@pytest.fixture(scope="module")
def real_growth():
    """One container run; every case below reads its report."""

    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if probe.returncode != 0:
        pytest.skip(f"no reachable Docker daemon: {probe.stderr.strip()[:120]}")

    result = subprocess.run(
        [
            "docker", "run", "--rm", "--privileged",
            "-v", f"{ROOT}:/src:ro",
            GUEST_IMAGE, "sh", "-c", GUEST,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    if "RESULT:" not in result.stdout:
        pytest.skip(
            "the disposable container could not run the tier: "
            + (result.stderr or result.stdout).strip()[-300:]
        )
    return result


def case(report, name):
    for line in report.stdout.splitlines():
        if name in line:
            return line.strip().split()[-1] if line.strip().endswith(("PASS", "FAIL")) else line
    return ""


def assert_case(report, name):
    for line in report.stdout.splitlines():
        if name in line and line.rstrip().endswith("PASS"):
            return
    pytest.fail(f"{name!r} did not pass:\n{report.stdout}")


@requires_docker
def test_the_real_tier_passes_every_case(real_growth):
    assert "RESULT: PASS" in real_growth.stdout, real_growth.stdout + real_growth.stderr
    assert real_growth.returncode == 0


@requires_docker
def test_real_tools_grow_a_freshly_imaged_medium(real_growth):
    assert_case(real_growth, "real growpart and resize2fs grow the medium")
    assert_case(real_growth, "the kernel re-read the grown partition table")
    assert_case(real_growth, "resize2fs grew the real filesystem")
    assert_case(real_growth, "the filesystem fills the grown partition")


@requires_docker
def test_an_already_grown_medium_is_recognised_from_real_geometry(real_growth):
    """The regression: a grown last partition is not read as short."""

    assert_case(real_growth, "an already grown medium is recognised, not repartitioned")
    assert_case(real_growth, "the recorded tail is alignment, not the occupied prefix")
    assert_case(real_growth, "the partition was not touched again")


@requires_docker
def test_a_medium_imaged_at_full_size_needs_no_growth(real_growth):
    assert_case(real_growth, "a medium imaged at full size needs no growth")


@requires_docker
def test_a_failing_growth_tool_leaves_no_marker(real_growth):
    assert_case(real_growth, "a failed growpart is reported")
    assert_case(real_growth, "a failed resize2fs is reported")
    assert_case(real_growth, "NOCHANGE on an ungrown medium is a refusal, not a pass")


@requires_docker
def test_a_power_cut_at_either_durable_boundary_recovers(real_growth):
    assert_case(real_growth, "a half-grown medium is never marked")
    assert_case(real_growth, "the retry completes with real resize2fs")
    assert_case(real_growth, "only now is the marker written")
    assert_case(real_growth, "the boot after the cut completes without repartitioning")


@requires_docker
def test_the_marker_is_durable_and_never_left_staged(real_growth):
    assert_case(real_growth, "a marker that cannot be staged fails the run")
    assert_case(real_growth, "and nothing is left staged")
    assert_case(real_growth, "and survives a remount, so it reached the medium")
