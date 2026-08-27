# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signature verification, against a real gpg.

``SignatureVerifier`` is what stands between this appliance and an artifact
somebody else built. It is exercised here against gpgv itself rather than a
stub: what matters is not that the code calls a verifier, but that gpg's own
status output is read the way gpg means it -- an expired key, a revoked key and
a good signature all produce a ``GOODSIG`` line, and only one of them is a
signature this appliance may act on.

The end-to-end chain over a real artifact is in test_appliance_release_trust.py,
which signs a release attestation and verifies it against a trust policy.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import commands, artifact_trust

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

requires_gpg = pytest.mark.skipif(
    shutil.which("gpg") is None, reason="gpg is required to sign and verify a release manifest"
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






















@requires_gpg
def test_the_verifier_reads_the_signing_key_out_of_gpgs_status_output(tmp_path):
    key = SigningKey(tmp_path / "gnupg", "EMS Release Test <release@ems.invalid>")
    keyring = key.keyring(tmp_path / "trusted.gpg")
    target = tmp_path / "manifest.json"
    target.write_text('{"release":"x"}')
    signature = key.sign(target)

    verifier = artifact_trust.SignatureVerifier(commands.CommandRunner(), keyring=str(keyring))

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

    assert artifact_trust.valid_signature_fingerprints(output) == ("BB" * 20,)
    assert artifact_trust.valid_signature_fingerprints("") == ()
    assert artifact_trust.valid_signature_fingerprints("gpg: BAD signature") == ()


requires_gpgv = pytest.mark.skipif(
    shutil.which("gpgv") is None, reason="gpgv verifies a release manifest on the appliance"
)


@requires_gpg
@requires_gpgv
def test_verification_works_with_only_the_binary_the_image_ships(tmp_path):
    """The A/B image installs ``gpgv`` and no full ``gpg``.

    A dev host has both, so a verifier that speaks gpg's option syntax passes
    every test here and still refuses every signed update on real hardware.
    """

    key = SigningKey(tmp_path / "gnupg", "Appliance Release <release@example.invalid>")
    keyring = key.keyring(tmp_path / "trusted.gpg")
    manifest = tmp_path / "release.json"
    manifest.write_text('{"release_version": "1.5.0"}\n')
    signature = key.sign(manifest)

    runner = commands.CommandRunner(executables={"gpgv": (shutil.which("gpgv"),)})
    verifier = artifact_trust.SignatureVerifier(
        runner, keyring=keyring, fingerprints=(key.fingerprint,)
    )

    assert verifier.available
    assert verifier.verify(manifest, signature)
    assert verifier.fingerprints_of(manifest, signature) == (key.fingerprint,)


# --- a key that is no longer usable is not a valid signature ------------------


class StatusRunner:
    """A gpgv whose status output is scripted; exit status stays 0.

    That combination is the point: gpgv reports EXPKEYSIG or REVKEYSIG *and*
    VALIDSIG, and still exits 0, so a verifier that reads only the exit status
    and VALIDSIG accepts a signature made with a key that was revoked.
    """

    def __init__(self, status):
        self.status = status

    def available(self, _name):
        return True

    def run(self, _executable, _args, **_kwargs):
        return commands.CommandResult(
            tool="gpgv", args=(), returncode=0, stdout=self.status, stderr=""
        )


def status_lines(marker):
    return "\n".join(
        [
            "[GNUPG:] NEWSIG",
            f"[GNUPG:] {marker} 480FAA4AAE458FC7 EMS Release Test",
            "[GNUPG:] VALIDSIG " + " ".join(["AA" * 20] + ["x"] * 8) + " " + "BB" * 20,
        ]
    )


@pytest.mark.parametrize(
    "marker,code",
    [
        ("EXPKEYSIG", "release_signature_key_expired"),
        ("REVKEYSIG", "release_signature_key_revoked"),
        ("EXPSIG", "release_signature_expired"),
    ],
)
def test_a_signature_from_an_unusable_key_is_refused(tmp_path, marker, code):
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"keyring")
    manifest = tmp_path / "release.json"
    manifest.write_text("{}\n")
    signature = tmp_path / "release.json.asc"
    signature.write_text("signature\n")

    verifier = artifact_trust.SignatureVerifier(
        StatusRunner(status_lines(marker)), keyring=str(keyring)
    )

    with pytest.raises(artifact_trust.ReleaseError) as error:
        verifier.verify(manifest, signature)

    assert error.value.code == code


def test_an_ordinary_good_signature_is_still_accepted(tmp_path):
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"keyring")
    manifest = tmp_path / "release.json"
    manifest.write_text("{}\n")
    signature = tmp_path / "release.json.asc"
    signature.write_text("signature\n")

    verifier = artifact_trust.SignatureVerifier(
        StatusRunner(status_lines("GOODSIG")), keyring=str(keyring)
    )

    assert verifier.verify(manifest, signature) is True


def test_the_fingerprint_query_refuses_an_unusable_key_too(tmp_path):
    """A caller that only asks who signed must not be handed a revoked key."""

    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"keyring")
    manifest = tmp_path / "release.json"
    manifest.write_text("{}\n")
    signature = tmp_path / "release.json.asc"
    signature.write_text("signature\n")

    verifier = artifact_trust.SignatureVerifier(
        StatusRunner(status_lines("REVKEYSIG")), keyring=str(keyring)
    )

    assert verifier.fingerprints_of(manifest, signature) == ()
