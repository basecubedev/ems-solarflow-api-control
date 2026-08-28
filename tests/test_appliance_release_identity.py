# SPDX-License-Identifier: AGPL-3.0-or-later
"""Creating the identity the whole fleet's trust hangs from.

This script runs once and what it writes decides, for the life of every card
flashed afterwards, which signatures are believed. A defect in it surfaces at
the worst available moment: after an image has been distributed.

Three properties are asserted by running it rather than reading it, because each
was measured to be a real trap. The primary must be unable to sign, so that "the
primary only certifies" is true by construction and not by discipline. The
exported secret must carry the subkey and not the primary. And the pin it writes
must be the keyring's primary — gpg reports the primary for a subkey signature,
so pinning the subkey would refuse every release the appliance is ever offered.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "appliance-new-release-identity.sh"

gpg_required = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgv") is None,
    reason="the identity is made with gpg, so there is nothing to test without it",
)


def sandbox(tmp_path):
    """A throwaway copy of the two files the script writes into."""

    config = tmp_path / "repo" / "packaging" / "appliance" / "config"
    config.mkdir(parents=True)
    (tmp_path / "repo" / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "repo" / "scripts" / SCRIPT.name)
    shutil.copy(ROOT / "packaging/appliance/config/appliance.conf", config / "appliance.conf")
    (config / "release-keyring.gpg").write_bytes(b"")
    return tmp_path / "repo"


def create(repo, tmp_path, *extra):
    home = tmp_path / "gnupg"
    home.mkdir(exist_ok=True)
    home.chmod(0o700)
    return subprocess.run(
        ["sh", str(repo / "scripts" / SCRIPT.name),
         "--uid", "Test Releases <t@example.invalid>",
         "--secret-out", str(tmp_path / "subkey.b64"), *extra],
        capture_output=True, text=True, check=False, timeout=300,
        env={**os.environ, "GNUPGHOME": str(home)},
    )


def fingerprints(listing):
    """The fpr: lines only.

    A loose scan for forty hex characters also matches the hash on the uid:
    line, which sits between the primary and the subkey and makes the subkey
    look like the third entry.
    """

    return [
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    ]


def test_a_secret_may_not_be_written_where_it_could_be_committed(tmp_path):
    """The one way a signing key ends up in a repository is by being put there
    by the thing that made it."""

    repo = sandbox(tmp_path)
    # Inside the tree the script itself is running from, which is the sandbox
    # copy here and the real repository in use.
    leaked = repo / "packaging" / "leaked.b64"
    run = subprocess.run(
        ["sh", str(repo / "scripts" / SCRIPT.name),
         "--uid", "x <x@example.invalid>", "--secret-out", str(leaked)],
        capture_output=True, text=True, check=False, timeout=60,
    )

    assert run.returncode == 1
    assert "inside the repository" in run.stderr
    assert not leaked.exists()


def test_an_existing_identity_is_not_replaced_by_accident(tmp_path):
    """Replacing it strands every appliance already flashed with the old one."""

    repo = sandbox(tmp_path)
    (repo / "packaging/appliance/config/release-keyring.gpg").write_bytes(b"an identity")

    run = create(repo, tmp_path)

    assert run.returncode == 1
    assert "--force" in run.stderr


@gpg_required
def test_the_primary_it_creates_cannot_sign(tmp_path):
    """``docs/appliance/security-model.md`` says the primary only certifies. A
    certify-only primary makes that true whatever anyone later types."""

    repo = sandbox(tmp_path)
    assert create(repo, tmp_path).returncode == 0

    keyring = repo / "packaging/appliance/config/release-keyring.gpg"
    listed = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(keyring)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    capabilities = {
        line.split(":")[0]: line.split(":")[11]
        for line in listed.splitlines()
        if line.startswith(("pub:", "sub:"))
    }

    assert "s" not in capabilities["pub"].replace("S", ""), capabilities["pub"]
    assert capabilities["sub"] == "s", capabilities["sub"]


@gpg_required
def test_the_pin_it_writes_is_the_primary_and_the_variable_is_the_subkey(tmp_path):
    """The two fingerprints are different keys and fail in opposite directions:
    the appliance pins the primary because gpg reports it for a subkey
    signature; the runner is given only the subkey's secret."""

    repo = sandbox(tmp_path)
    run = create(repo, tmp_path)

    assert run.returncode == 0, run.stderr
    conf = (repo / "packaging/appliance/config/appliance.conf").read_text(encoding="utf-8")
    pinned = re.search(r"^release_fingerprints = (\S+)$", conf, re.M).group(1)
    keyring = repo / "packaging/appliance/config/release-keyring.gpg"
    held = fingerprints(subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(keyring)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout)

    assert pinned == held[0], "the pin is not the keyring's primary"
    assert f"APPLIANCE_MANAGER_SIGNING_FINGERPRINT  = {held[1]}" in run.stdout


@gpg_required
def test_what_it_exports_signs_a_release_the_shipped_keyring_accepts(tmp_path):
    """The whole point, end to end: a runner given nothing but that secret
    produces a signature the appliance would believe."""

    repo = sandbox(tmp_path)
    assert create(repo, tmp_path).returncode == 0
    subkey = fingerprints(
        subprocess.run(
            ["gpg", "--show-keys", "--with-colons",
             str(repo / "packaging/appliance/config/release-keyring.gpg")],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
    )[1]

    runner = tmp_path / "runner"
    runner.mkdir()
    runner.chmod(0o700)
    environment = {**os.environ, "GNUPGHOME": str(runner)}
    import base64

    subprocess.run(
        ["gpg", "--batch", "--quiet", "--import"],
        input=base64.b64decode((tmp_path / "subkey.b64").read_text()),
        check=True, capture_output=True, timeout=120, env=environment,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest": "x"}', encoding="utf-8")
    subprocess.run(
        ["gpg", "--batch", "--yes", "--armor", "--detach-sign",
         "--local-user", f"{subkey}!", "--output", str(manifest) + ".asc", str(manifest)],
        check=True, capture_output=True, timeout=120, env=environment,
    )

    verified = subprocess.run(
        ["gpgv", "--keyring",
         str(repo / "packaging/appliance/config/release-keyring.gpg"),
         "--status-fd", "1", str(manifest) + ".asc", str(manifest)],
        capture_output=True, text=True, check=False, timeout=120,
    )

    assert verified.returncode == 0, verified.stderr
    conf = (repo / "packaging/appliance/config/appliance.conf").read_text(encoding="utf-8")
    pinned = re.search(r"^release_fingerprints = (\S+)$", conf, re.M).group(1)
    validsig = next(
        line for line in verified.stdout.splitlines() if "VALIDSIG" in line
    ).split()

    assert validsig[11] == pinned, "the fingerprint gate on the appliance would refuse this"
