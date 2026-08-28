# SPDX-License-Identifier: AGPL-3.0-or-later
"""The publish step that stands in for a decision nobody can take back.

The fingerprint an appliance pins is frozen the moment a card is flashed.
``appliance.conf`` ships to ``/usr/share``, it is not a dpkg conffile, and
``config_seed`` reports an existing ``/etc`` copy as present without reading it,
so no update and no operation can correct it. A card flashed against an identity
whose secret half nobody holds can never install a Manager package again --
neither an upgrade nor the downgrade that is its only recovery -- and the repair
is a root console on every unit.

The identity this project shipped up to now is the hand-made placeholder: its
primary carries ``scSC`` and can sign, where
``scripts/appliance-new-release-identity.sh`` produces a certify-only one. That
difference is the machine-readable marker between a real identity and the
placeholder, and it is what the gate reads.

Building is left alone on purpose -- physical hardware validation needs images.
Publishing is where the damage becomes permanent, so that is where it refuses.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "appliance-check-release-identity.py"
GENERATOR = ROOT / "scripts" / "appliance-new-release-identity.sh"
IMAGE_WORKFLOW = ROOT / ".github" / "workflows" / "appliance-image.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "appliance-manager-release.yml"

gpg_required = pytest.mark.skipif(
    shutil.which("gpg") is None,
    reason="the identity is read with gpg, so there is nothing to check without it",
)


def check(keyring, config):
    return subprocess.run(
        ["python3", str(CHECK), "--keyring", str(keyring), "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def config_pinning(tmp_path, fingerprint, name="appliance.conf"):
    path = tmp_path / name
    path.write_text(f"release_fingerprints = {fingerprint}\n", encoding="utf-8")
    return path


def generated_identity(tmp_path):
    """An identity made the documented way, in a sandbox copy of the two files."""

    repo = tmp_path / "repo"
    (repo / "packaging" / "appliance" / "config").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy(GENERATOR, repo / "scripts" / GENERATOR.name)
    shutil.copy(
        ROOT / "packaging/appliance/config/appliance.conf",
        repo / "packaging/appliance/config/appliance.conf",
    )
    (repo / "packaging/appliance/config/release-keyring.gpg").write_bytes(b"")

    home = tmp_path / "gnupg"
    home.mkdir()
    home.chmod(0o700)
    made = subprocess.run(
        [
            "sh", str(repo / "scripts" / GENERATOR.name),
            "--uid", "Gate Test <gate@example.invalid>",
            "--secret-out", str(tmp_path / "subkey.b64"),
        ],
        capture_output=True, text=True, check=False, timeout=300,
        env={**os.environ, "GNUPGHOME": str(home)},
    )
    assert made.returncode == 0, made.stderr
    return (
        repo / "packaging/appliance/config/release-keyring.gpg",
        repo / "packaging/appliance/config/appliance.conf",
        home,
    )


def fingerprints(keyring, kind):
    listed = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(keyring)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    found, pending = [], False
    for line in listed.splitlines():
        fields = line.split(":")
        if fields[0] in ("pub", "sub"):
            pending = fields[0] == kind
        elif fields[0] == "fpr" and pending:
            found.append(fields[9])
            pending = False
    return found


@gpg_required
def test_the_documented_way_of_making_an_identity_is_accepted(tmp_path):
    """The gate has to pass for the thing the runbook tells a maintainer to do,
    or it is an obstacle rather than a check."""

    keyring, config, _ = generated_identity(tmp_path)

    result = check(keyring, config)

    assert result.returncode == 0, result.stderr
    assert "certify-only, pinned" in result.stdout


@gpg_required
def test_a_primary_that_can_sign_is_refused_as_the_placeholder(tmp_path):
    """The marker that tells the hand-made identity from a generated one. It is
    a proxy, and the message says which action it is asking for."""

    home = tmp_path / "gnupg"
    home.mkdir()
    home.chmod(0o700)
    environment = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        ["gpg", "--batch", "--quiet", "--passphrase", "",
         "--quick-generate-key", "Placeholder <p@example.invalid>", "ed25519", "sign,cert", "never"],
        check=True, capture_output=True, timeout=120, env=environment,
    )
    primary = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys", "Placeholder"],
        capture_output=True, text=True, check=True, timeout=60, env=environment,
    ).stdout.split("fpr:::::::::", 1)[1].split(":", 1)[0]
    subprocess.run(
        ["gpg", "--batch", "--quiet", "--passphrase", "",
         "--quick-add-key", primary, "ed25519", "sign", "never"],
        check=True, capture_output=True, timeout=120, env=environment,
    )
    keyring = tmp_path / "placeholder.gpg"
    keyring.write_bytes(subprocess.run(
        ["gpg", "--batch", "--export", primary],
        capture_output=True, check=True, timeout=60, env=environment,
    ).stdout)

    result = check(keyring, config_pinning(tmp_path, primary))

    assert result.returncode == 1
    assert "can never install a Manager package again" in result.stderr
    assert "appliance-new-release-identity.sh" in result.stderr


@gpg_required
def test_pinning_the_subkey_is_refused(tmp_path):
    """It passes review and then refuses every update the appliance is offered,
    because gpg reports the primary in VALIDSIG field 12 for a subkey
    signature."""

    keyring, _, _ = generated_identity(tmp_path)
    subkey = fingerprints(keyring, "sub")[0]

    result = check(keyring, config_pinning(tmp_path, subkey))

    assert result.returncode == 1
    assert "not the keyring's primary" in result.stderr


@gpg_required
def test_a_keyring_with_nothing_to_sign_with_is_refused(tmp_path):
    """A certify-only primary and no subkey cannot sign a release either."""

    home = tmp_path / "gnupg"
    home.mkdir()
    home.chmod(0o700)
    environment = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        ["gpg", "--batch", "--quiet", "--passphrase", "",
         "--quick-generate-key", "Certify Only <c@example.invalid>", "ed25519", "cert", "never"],
        check=True, capture_output=True, timeout=120, env=environment,
    )
    primary = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys", "Certify Only"],
        capture_output=True, text=True, check=True, timeout=60, env=environment,
    ).stdout.split("fpr:::::::::", 1)[1].split(":", 1)[0]
    keyring = tmp_path / "nosub.gpg"
    keyring.write_bytes(subprocess.run(
        ["gpg", "--batch", "--export", primary],
        capture_output=True, check=True, timeout=60, env=environment,
    ).stdout)

    result = check(keyring, config_pinning(tmp_path, primary))

    assert result.returncode == 1
    assert "no signing subkey" in result.stderr


def test_publishing_an_image_is_gated_on_it():
    """Building is not. Hardware validation needs images, and an image that is
    never distributed strands nobody."""

    jobs = yaml.safe_load(IMAGE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    publish = [step.get("run", "") or str(step.get("uses", "")) for step in jobs["publish"]["steps"]]
    gate = [i for i, step in enumerate(publish) if "appliance-check-release-identity.py" in step]
    creates = [i for i, step in enumerate(publish) if "gh release create" in step]

    assert gate, "an image can be published against an identity nobody can sign with"
    assert creates and gate[0] < creates[0], "the release is created before the identity is checked"
    assert not any(
        "appliance-check-release-identity.py" in str(step.get("run", ""))
        for job, body in jobs.items() if job != "publish"
        for step in body["steps"]
    ), "building an image must stay possible; only publishing is gated"


def test_a_release_is_refused_before_a_reviewer_is_asked_for_the_key():
    """gpgv would catch it three jobs later and report it as the fleet refusing
    the signature, which is a message about the wrong thing -- and by then a
    person has already released the key."""

    jobs = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    package = [step.get("run", "") for step in jobs["package"]["steps"]]

    assert any("appliance-check-release-identity.py" in str(step) for step in package)
    assert "environment" not in jobs["package"], "the package job must hold no key"
