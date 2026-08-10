# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether a release signature was verified, or merely counted.

The update inspector reported ``detached_signature: pass`` when a file called
``<manifest>.json.asc`` existed beside the manifest. That is a statement about
a filename. It is equally true of a signature made with any key in the world,
over any bytes, including bytes that are no longer the ones in front of the
inspector — so a strict gate could report a signed release while holding a
manifest nobody had signed.

Verification now runs gpg against a named keyring, reads the fingerprint out of
gpg's status output, and compares it against an explicit trust policy. A
keyring can hold more keys than a release may be signed with, and "gpg said
good" does not say which key it was good for.

The signing key used here is generated per test, lives in a temporary
GNUPGHOME, and never leaves it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import commands, os_releases
from tests.test_appliance_ab_artifact_format import build_upstream_archive

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INSPECTOR = SCRIPTS / "appliance-inspect-rpi-ab-update.sh"

requires_gpg = pytest.mark.skipif(
    shutil.which("gpg") is None, reason="gpg is required to sign and verify a release manifest"
)
requires_zstd = pytest.mark.skipif(
    shutil.which("zstd") is None or shutil.which("tar") is None,
    reason="zstd and tar are required to build an update artefact",
)


class SigningKey:
    def __init__(self, home, uid):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)
        self.env = dict(os.environ, GNUPGHOME=str(self.home))
        self._gpg(
            "--passphrase", "", "--quick-generate-key", uid, "ed25519", "sign", "never"
        )
        listed = self._gpg("--with-colons", "--list-keys").stdout
        self.fingerprint = next(
            line.split(":")[9] for line in listed.splitlines() if line.startswith("fpr:")
        )

    def _gpg(self, *args):
        return subprocess.run(
            ["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )

    def keyring(self, path):
        exported = self.home / "public.asc"
        self._gpg("--export", "--armor", "--output", str(exported), self.fingerprint)
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--no-default-keyring",
                "--keyring",
                str(path),
                "--import",
                str(exported),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(path)

    def sign(self, target):
        signature = Path(f"{target}.asc")
        self._gpg(
            "--local-user",
            self.fingerprint,
            "--detach-sign",
            "--armor",
            "--output",
            str(signature),
            str(target),
        )
        return signature


@pytest.fixture
def release(tmp_path):
    """A described artefact, its manifest, and a key that can sign it."""

    archive = build_upstream_archive(tmp_path / "work")
    output = tmp_path / "out"
    output.mkdir()
    shutil.copy(archive, output / "update.tar.zst")
    subprocess.run(
        [
            "sh",
            str(SCRIPTS / "appliance-build-rpi-ab-update.sh"),
            "--output",
            str(output),
            "--update",
            str(output / "update.tar.zst"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    manifest = next(output.glob("*.manifest.json"))
    key = SigningKey(tmp_path / "gnupg", "EMS Release Test <release@ems.invalid>")
    return manifest, key


def inspect(manifest, *args):
    result = subprocess.run(
        ["sh", str(INSPECTOR), "--json", *args, str(manifest)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    checks = {finding["check"]: finding["result"] for finding in payload.get("findings", [])}
    return result.returncode, checks, payload


@requires_gpg
@requires_zstd
def test_a_manifest_signed_by_the_trusted_key_passes(release, tmp_path):
    manifest, key = release
    keyring = key.keyring(tmp_path / "trusted.gpg")
    key.sign(manifest)

    code, checks, _ = inspect(
        manifest,
        "--keyring",
        str(keyring),
        "--trusted-fingerprint",
        key.fingerprint,
        "--require-signature",
    )

    assert code == 0
    assert checks["detached_signature"] == "pass"
    assert checks["signature_valid"] == "pass"
    assert checks["signature_key_trusted"] == "pass"


@requires_gpg
@requires_zstd
def test_a_signature_from_a_key_the_keyring_does_not_hold_fails(release, tmp_path):
    manifest, key = release
    stranger = SigningKey(tmp_path / "stranger", "Someone Else <else@ems.invalid>")
    keyring = key.keyring(tmp_path / "trusted.gpg")
    stranger.sign(manifest)

    code, checks, _ = inspect(manifest, "--keyring", str(keyring), "--require-signature")

    assert code == 1
    assert checks["signature_valid"] == "fail"


@requires_gpg
@requires_zstd
def test_a_signature_from_an_untrusted_key_in_the_keyring_fails(release, tmp_path):
    manifest, key = release
    stranger = SigningKey(tmp_path / "stranger", "Someone Else <else@ems.invalid>")
    keyring = key.keyring(tmp_path / "trusted.gpg")
    stranger.keyring(keyring)
    stranger.sign(manifest)

    code, checks, payload = inspect(
        manifest,
        "--keyring",
        str(keyring),
        "--trusted-fingerprint",
        key.fingerprint,
        "--require-signature",
    )

    assert code == 1
    assert checks["signature_valid"] == "fail"
    detail = next(
        item["detail"] for item in payload["findings"] if item["check"] == "signature_valid"
    )
    assert "release_signature_untrusted" in detail


@requires_gpg
@requires_zstd
def test_a_manifest_edited_after_signing_fails(release, tmp_path):
    manifest, key = release
    keyring = key.keyring(tmp_path / "trusted.gpg")
    key.sign(manifest)
    payload = json.loads(manifest.read_text())
    payload["release_version"] = "9.9.9"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    code, checks, _ = inspect(
        manifest, "--keyring", str(keyring), "--trusted-fingerprint", key.fingerprint
    )

    assert code == 1
    assert checks["signature_valid"] == "fail"


@requires_gpg
@requires_zstd
def test_a_tampered_signature_fails(release, tmp_path):
    manifest, key = release
    keyring = key.keyring(tmp_path / "trusted.gpg")
    signature = key.sign(manifest)
    text = signature.read_text()
    signature.write_text(text.replace("A", "B", 1))

    code, checks, _ = inspect(manifest, "--keyring", str(keyring))

    assert code == 1
    assert checks["signature_valid"] == "fail"


@requires_gpg
@requires_zstd
def test_an_unsigned_artefact_is_a_rehearsal_and_not_a_release(release, tmp_path):
    manifest, key = release
    keyring = key.keyring(tmp_path / "trusted.gpg")

    rehearsal, checks, _ = inspect(manifest, "--keyring", str(keyring))
    production, strict_checks, _ = inspect(
        manifest, "--keyring", str(keyring), "--require-signature"
    )

    assert rehearsal == 0
    assert checks["detached_signature"] == "not_run"
    assert checks["signature_valid"] == "not_run"
    assert production == 1
    assert strict_checks["detached_signature"] == "fail"


@requires_gpg
@requires_zstd
def test_production_mode_refuses_a_signature_with_no_trust_policy(release, tmp_path):
    manifest, key = release
    keyring = key.keyring(tmp_path / "trusted.gpg")
    key.sign(manifest)

    code, checks, _ = inspect(manifest, "--keyring", str(keyring), "--require-signature")

    assert code == 1
    assert checks["signature_valid"] == "pass"
    assert checks["signature_key_trusted"] == "fail"


@requires_gpg
@requires_zstd
def test_production_mode_refuses_a_signature_nobody_could_verify(release):
    manifest, key = release
    key.sign(manifest)

    code, checks, _ = inspect(manifest, "--require-signature")

    assert code == 1
    assert checks["detached_signature"] == "pass"
    assert checks["signature_valid"] == "fail"


@requires_gpg
def test_the_verifier_reads_the_signing_key_out_of_gpgs_status_output(tmp_path):
    key = SigningKey(tmp_path / "gnupg", "EMS Release Test <release@ems.invalid>")
    keyring = key.keyring(tmp_path / "trusted.gpg")
    target = tmp_path / "manifest.json"
    target.write_text('{"release":"x"}')
    signature = key.sign(target)

    verifier = os_releases.SignatureVerifier(commands.CommandRunner(), keyring=str(keyring))

    assert verifier.verify(target, signature) is True
    assert verifier.fingerprints_of(target, signature) == (key.fingerprint,)


def test_the_status_parser_ignores_everything_that_is_not_a_valid_signature():
    output = "\n".join(
        [
            "[GNUPG:] NEWSIG",
            "[GNUPG:] GOODSIG 480FAA4AAE458FC7 EMS Release Test",
            "[GNUPG:] VALIDSIG " + " ".join(["AA" * 20] + ["x"] * 8) + " " + "BB" * 20,
            "gpg: Good signature",
        ]
    )

    assert os_releases.valid_signature_fingerprints(output) == ("BB" * 20,)
    assert os_releases.valid_signature_fingerprints("") == ()
    assert os_releases.valid_signature_fingerprints("gpg: BAD signature") == ()
