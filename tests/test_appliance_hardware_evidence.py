# SPDX-License-Identifier: AGPL-3.0-or-later
"""The helpers an operator runs on a real appliance, and what they may not do.

These four scripts run on hardware that is mid-validation, often between a
a first boot. A helper that wrote a block device, changed the boot order or
restarted a service would change the state the operator is measuring, and the
case would have to start again. So the read-only contract is asserted here
rather than left to review.

The evidence they collect is the other half: a power-cut case is an argument
about what reached the medium before the power went, and an argument with no
copy of the selector is not an argument.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HELPERS = (
    "appliance-hardware-capture-baseline.sh",
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


def test_the_rc_status_block_cannot_quietly_go_stale():
    """The block declares itself the authoritative status source and defines its
    own invalidation rule -- and nothing enforced it, so it read "Stale: False"
    while development had moved six commits past the revision it names."""

    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]
    block = (root / "docs/appliance/hardware-validation.md").read_text(encoding="utf-8")
    body = block.split("<!-- CURRENT-RC-BEGIN -->")[1].split("<!-- CURRENT-RC-END -->")[0]

    recorded = re.search(r"Release-build revision[^|]*\|\s*`([0-9a-f]{40})`", body)
    stale = re.search(r"\|\s*Stale\s*\|\s*\*\*(True|False)\*\*", body)

    assert recorded, "the block records no release-build revision"
    assert stale, "the block does not state whether it is stale"

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False, cwd=root, timeout=60,
    ).stdout.strip()
    if not head:
        pytest.skip("no git checkout to compare against")

    if recorded.group(1) != head:
        assert stale.group(1) == "True", (
            f"the block says Stale=False while naming {recorded.group(1)[:12]} "
            f"and the checkout is at {head[:12]}"
        )


def test_the_prose_above_the_block_cannot_contradict_it():
    """Two sentences, five lines apart, said opposite things about staleness.

    The table row is generated from the evidence; the prose is written by hand,
    so it is the half that drifts.
    """

    import re

    root = Path(__file__).resolve().parents[1]
    page = (root / "docs/appliance/hardware-validation.md").read_text(encoding="utf-8")
    intro, _, rest = page.partition("<!-- CURRENT-RC-BEGIN -->")
    body = rest.split("<!-- CURRENT-RC-END -->")[0]
    stale = re.search(r"\|\s*Stale\s*\|\s*\*\*(True|False)\*\*", body)

    assert stale, "the block does not state whether it is stale"
    if stale.group(1) == "True":
        assert "It is not stale here" not in intro


def _page():
    root = Path(__file__).resolve().parents[1]
    return (root / "docs/appliance/hardware-validation.md").read_text(encoding="utf-8")


def _evidence_verdict(page, case):
    """The verdict cell of one row of the current evidence table.

    A renamed row fails here rather than skipping: a guard that switches
    itself off when its row goes missing is no guard.
    """

    table = page.split("This is the current evidence table.")[1]
    rows = [line for line in table.splitlines() if line.startswith(f"| {case} |")]
    assert rows, f"the evidence table has no row {case!r}"
    return rows[0].split("|")[2].strip().strip("*")


def test_the_authoritative_block_cannot_deny_a_boot_the_evidence_table_records():
    """The one authoritative status block said no board had booted either
    image while the evidence table forty lines below recorded the boot as
    PASS, read from the running board. Scoped to the RC markers on purpose:
    the historical blocks further down carry the same NOT RUN legitimately,
    and the document forbids rewriting them.
    """

    page = _page()
    verdict = _evidence_verdict(page, "The built image boots on a Pi 3B+")
    body = page.split("<!-- CURRENT-RC-BEGIN -->")[1].split("<!-- CURRENT-RC-END -->")[0]
    rows = [line for line in body.splitlines() if line.startswith("| Physical Raspberry Pi |")]
    assert rows, "the authoritative block has no Physical Raspberry Pi row"

    if verdict == "PASS":
        assert "NOT RUN" not in rows[0], (
            "the evidence table records the boot as PASS; the authoritative block denies it"
        )


def test_a_storage_class_is_not_reported_as_not_run_while_group_1_is_proven_on_it():
    """Group 5's microSD row said NOT RUN while six of Group 1's thirteen cases
    had been run on a Pi 3B+ on microSD and were carried by evidence rows. It
    has to say which cases it covers rather than collapse them into one word."""

    import re

    page = _page()
    verdict = _evidence_verdict(page, "The built image boots on a Pi 3B+")
    group = re.split(r"^### Group 5 .*storage classes\s*$", page, maxsplit=1, flags=re.M)[1]
    group = group.split("\n## ", 1)[0]
    rows = [line for line in group.splitlines() if line.startswith("| microSD | Pi 3B+ |")]
    assert rows, "Group 5 has no microSD / Pi 3B+ row"
    status = rows[0].split("|")[3]

    if verdict == "PASS":
        assert "NOT RUN" not in status.split("PARTIAL")[0], status
        assert "PARTIAL" in status, status
        for case in ("1.1", "1.6"):
            assert case in status, case


def test_readiness_is_not_claimed_while_the_evidence_is_stale():
    """`release_not_stale` is one of the required readiness invariants, so a
    stale release cannot also be physically ready. Saying so anyway is how a
    release status survives the thing that was supposed to invalidate it."""

    import re

    root = Path(__file__).resolve().parents[1]
    body = (
        (root / "docs/appliance/hardware-validation.md")
        .read_text(encoding="utf-8")
        .split("<!-- CURRENT-RC-BEGIN -->")[1]
        .split("<!-- CURRENT-RC-END -->")[0]
    )
    stale = re.search(r"\|\s*Stale\s*\|\s*\*\*(True|False)\*\*", body)
    readiness = re.search(r"\|\s*Physical readiness\s*\|\s*\*\*([A-Z ]+)\*\*", body)

    assert stale and readiness
    if stale.group(1) == "True":
        assert readiness.group(1).strip() != "READY"
