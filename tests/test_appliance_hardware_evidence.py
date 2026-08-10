# SPDX-License-Identifier: AGPL-3.0-or-later
"""The helpers an operator runs on a real appliance, and what they may not do.

These four scripts run on hardware that is mid-validation, often between a
tryboot and a commit. A helper that wrote a block device, moved the selector or
restarted a service would change the state the operator is measuring, and the
case would have to start again. So the read-only contract is asserted here
rather than left to review.

The evidence they collect is the other half: a power-cut case is an argument
about what reached the medium before the power went, and an argument with no
copy of the selector is not an argument.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HELPERS = (
    "appliance-hardware-capture-baseline.sh",
    "appliance-hardware-verify-slot.sh",
    "appliance-hardware-verify-persistence.sh",
    "appliance-hardware-collect-evidence.sh",
)

# Anything that changes the appliance rather than reporting on it.
FORBIDDEN = (
    "mkfs",
    "sgdisk",
    "parted",
    "losetup",
    "dd if=",
    "shutdown",
    "systemctl start",
    "systemctl stop",
    "systemctl restart",
    "ssh-keygen -t",
    "ab commit",
    "ab rollback",
    "ab stage",
)


def source(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", HELPERS)
def test_a_helper_never_changes_what_it_is_measuring(name):
    text = source(name)
    for forbidden in FORBIDDEN:
        assert forbidden not in text, f"{name} runs {forbidden!r}"


@pytest.mark.parametrize("name", HELPERS)
def test_a_helper_never_reads_a_private_key(name):
    """Fingerprints are evidence; the secret behind them is not."""

    text = source(name)

    assert "ssh_host_ed25519_key\n" not in text
    for line in text.splitlines():
        if "ssh-keygen" in line:
            assert "-lf" in line, line


def test_the_baseline_records_the_selector_it_is_arguing_about():
    """A power-cut case with no copy of autoboot.txt proves nothing.

    The runbook tells the operator to keep a raw copy of the selector after
    every power-cut case, because a torn write to that one file is the whole
    failure mode. The helper that exists to collect the evidence did not
    collect it.
    """

    assert "autoboot.txt" in source("appliance-hardware-capture-baseline.sh")


def test_the_selector_is_found_rather_than_assumed():
    """The mountpoint differs between an appliance and a plain Pi OS image."""

    text = source("appliance-hardware-capture-baseline.sh")
    selector = text[text.index("autoboot.txt") - 400 : text.index("autoboot.txt") + 400]

    assert "/bootfs" in selector
    assert "for " in selector


@pytest.mark.parametrize("name", HELPERS)
def test_a_helper_hashes_what_it_collected(name):
    """Evidence that cannot be checked later is a claim, not evidence."""

    text = source(name)
    if name == "appliance-hardware-capture-baseline.sh":
        assert "sha256sum" in text
    else:
        assert "SHA256SUMS" in text or "capture-baseline" in text or "--json" in text


KIT = SCRIPTS / "appliance_hardware_kit.py"


def test_the_kit_does_not_read_a_disk_image_as_text():
    """Every OpenSSH binary carries the string the scan looks for.

    The kit scans its own output for private key blocks, which is right. It
    scanned the 17 GiB raw images too, and ``ssh-keygen``'s string table
    contains ``-----BEGIN OPENSSH PRIVATE KEY-----`` as a literal — so the kit
    refused to assemble for both boards and deleted itself. A check that can
    never pass protects nothing. That an image ships no host key is proven by
    the image content inspection, where it can be told from a string constant.
    """

    text = KIT.read_text(encoding="utf-8")

    assert "OPAQUE_SUFFIXES" in text
    assert '".img"' in text


def test_the_kit_still_refuses_a_real_key_beside_the_artefacts():
    """Scoping the scan must not turn it off."""

    text = KIT.read_text(encoding="utf-8")

    assert "BEGIN (OPENSSH" in text
    assert "private_key_in_kit" in text
