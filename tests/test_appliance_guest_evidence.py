# SPDX-License-Identifier: AGPL-3.0-or-later
"""The guest evidence channel, and the failure it exists to stop.

Two real aarch64 runs of the ARM64 tier reported ``FAIL`` with no diagnosis at
all: the guest printed ``== install ==`` and then nothing until its exit
marker. Both drivers run the same tier script; the amd64 one collects it over
SSH and passed, the ARM64 one redirected it to ``/dev/ttyAMA0`` — the boot
console the kernel, systemd and ``agetty`` share.

``agetty`` calls ``vhangup()`` when it claims that console. Every descriptor
already open on it is revoked, so the tier's next write fails, and a tier under
``set -e`` dies there with no record of how far it got. The exit marker
survived only because the driver wrote it through a second, fresh open.

These cases hold the wrapper to the property that failure taught: the tier is
never given a terminal, and the record arrives whole even when the console
channel is unusable.
"""

import os
import subprocess

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

from pathlib import Path  # noqa: E402  (after pytestmark on purpose)

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "appliance-guest-evidence.sh"
ARM64_DRIVER = ROOT / "scripts" / "appliance-smoke-arm64.sh"
GUEST_SMOKE = ROOT / "scripts" / "appliance-guest-smoke.sh"

CHANNEL_NAME = "org.ems.appliance.evidence"
EXIT_PREREQUISITE = 3


def write_tier(path, body):
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def guest(tmp_path):
    """A guest-shaped sandbox: a device root, a console, and a record file."""

    devices = tmp_path / "dev"
    (devices / "virtio-ports").mkdir(parents=True)
    channel = devices / "virtio-ports" / CHANNEL_NAME
    channel.touch()
    console = tmp_path / "console"
    console.touch()
    return {
        "devices": devices,
        "channel": channel,
        "console": console,
        "log": tmp_path / "record.log",
        "tmp": tmp_path,
    }


def run_wrapper(guest, tier, *arguments, fallback=None, channel_name=CHANNEL_NAME):
    env = dict(os.environ)
    env["EMS_APPLIANCE_EVIDENCE_ROOT"] = str(guest["devices"])
    command = [str(WRAPPER)]
    if channel_name is not None:
        command += ["--channel-name", channel_name]
    command += [
        "--fallback",
        str(fallback if fallback is not None else guest["console"]),
        "--log",
        str(guest["log"]),
        "--",
        str(tier),
        *arguments,
    ]
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=120, env=env
    )


# --- the record ------------------------------------------------------------


def test_the_whole_tier_output_reaches_the_dedicated_channel(guest, tmp_path):
    tier = write_tier(
        tmp_path / "tier.sh",
        "echo first\n"
        "echo to-stderr >&2\n"
        "echo last\n"
        "exit 0\n",
    )
    result = run_wrapper(guest, tier)
    record = guest["channel"].read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "first" in record
    assert "to-stderr" in record
    assert "last" in record
    assert "APPLIANCE_SMOKE_EXIT: 0" in record


def test_the_tier_is_never_given_a_terminal(guest, tmp_path):
    # The defect was a tier writing to a shared tty. A tier that can see a
    # terminal on its stdout is one a login console can still revoke.
    tier = write_tier(
        tmp_path / "tier.sh",
        'if [ -t 1 ] || [ -t 2 ]; then echo "stdout is a terminal"; exit 7; fi\n'
        "echo no-terminal\n",
    )
    result = run_wrapper(guest, tier)

    assert result.returncode == 0, guest["channel"].read_text(encoding="utf-8")
    assert "no-terminal" in guest["channel"].read_text(encoding="utf-8")


def test_a_console_the_guest_cannot_write_to_does_not_cost_the_record(guest, tmp_path):
    # The guest-side shape of vhangup(): every write to the console fails. The
    # tier used to die on the first of them; the record must now survive whole.
    dead_console = guest["tmp"] / "dead-console"
    dead_console.touch()
    dead_console.chmod(0o000)
    tier = write_tier(
        tmp_path / "tier.sh",
        "echo before\n"
        "sleep 1\n"
        "echo after\n"
        "echo RESULT: PASS\n",
    )

    result = run_wrapper(guest, tier, fallback=dead_console)
    record = guest["channel"].read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "before" in record
    assert "after" in record
    assert "RESULT: PASS" in record
    assert "APPLIANCE_SMOKE_EXIT: 0" in record


def test_a_failing_tier_carries_its_status_and_is_marked_failed(guest, tmp_path):
    tier = write_tier(tmp_path / "tier.sh", "echo RESULT: FAIL\nexit 4\n")
    result = run_wrapper(guest, tier)
    record = guest["channel"].read_text(encoding="utf-8")

    assert result.returncode == 4
    assert "APPLIANCE_EVIDENCE result=FAIL" in record
    assert "APPLIANCE_SMOKE_EXIT: 4" in record


def test_exactly_one_completion_marker_is_delivered(guest, tmp_path):
    # The driver refuses a record with more than one; a live mirror that also
    # carried the record would produce two and make every run unreadable.
    tier = write_tier(tmp_path / "tier.sh", "echo working\n")
    run_wrapper(guest, tier)
    record = guest["channel"].read_text(encoding="utf-8")

    assert record.count("APPLIANCE_SMOKE_EXIT:") == 1


# --- the fallback ----------------------------------------------------------


def test_without_a_dedicated_channel_the_record_goes_to_the_fallback(guest, tmp_path):
    guest["channel"].unlink()
    tier = write_tier(tmp_path / "tier.sh", "echo fallback-record\n")
    result = run_wrapper(guest, tier)

    assert result.returncode == 0
    assert "fallback-record" in guest["console"].read_text(encoding="utf-8")


def test_the_stage_heartbeat_reaches_the_console_while_the_tier_runs(guest, tmp_path):
    tier = write_tier(
        tmp_path / "tier.sh",
        "printf 'APPLIANCE_EVIDENCE stage=install\\n'\n"
        "printf 'noise nobody should mirror\\n'\n",
    )
    run_wrapper(guest, tier)
    console = guest["console"].read_text(encoding="utf-8")

    assert "stage=install" in console
    assert "noise nobody should mirror" not in console


def test_a_record_that_cannot_be_delivered_is_never_a_tier_pass(guest, tmp_path):
    guest["channel"].unlink()
    tier = write_tier(tmp_path / "tier.sh", "echo lost\n")
    result = run_wrapper(guest, tier, fallback=guest["tmp"] / "no" / "such" / "device")

    assert result.returncode == EXIT_PREREQUISITE
    assert "could not be written" in result.stderr


def test_a_missing_tier_is_a_prerequisite_failure(guest, tmp_path):
    result = run_wrapper(guest, tmp_path / "absent.sh")

    assert result.returncode == EXIT_PREREQUISITE


# --- what the ARM64 driver is now required to do ---------------------------


def driver_text():
    return ARM64_DRIVER.read_text(encoding="utf-8")


def test_the_arm64_driver_no_longer_points_the_tier_at_the_login_console():
    runcmd = [line for line in driver_text().splitlines() if "guest-smoke.sh" in line]

    assert runcmd, "the driver no longer runs the shared guest tier at all"
    for line in runcmd:
        assert "> /dev/ttyAMA0" not in line, line


def test_the_arm64_driver_runs_the_tier_through_the_evidence_wrapper():
    text = driver_text()

    assert "guest-evidence.sh" in text
    assert "--channel-name" in text
    assert "virtserialport" in text


def test_the_arm64_driver_keeps_the_record_with_the_run():
    assert "evidence.log" in driver_text()


def test_the_shared_tier_names_the_stage_it_reached():
    text = GUEST_SMOKE.read_text(encoding="utf-8")

    assert "APPLIANCE_EVIDENCE stage=" in text


def test_the_console_mirror_does_not_outlive_the_run(guest, tmp_path):
    """A follower left behind on every run is a process leak, in any guest."""

    tier = write_tier(tmp_path / "tier.sh", "echo done\n")
    run_wrapper(guest, tier)

    survivors = subprocess.run(
        ["pgrep", "-af", str(guest["log"])],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert survivors.stdout.strip() == "", survivors.stdout
