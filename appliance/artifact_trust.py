# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether an artifact is one this appliance is allowed to install.

The browser may name a release. It may not name a URL, a path, a device, a
repository, a key or a checksum -- every one of those comes from the root-owned
host configuration or from a signed manifest, because a request that could
supply any of them could supply a package of its own choosing.

The trust chain is short on purpose:

    root-owned keyring
      -> detached signature over the release manifest
        -> manifest carries the package's SHA-256
          -> the file on disk is hashed again before it is installed

What is verified here is the Appliance Manager's own replacement package. The
state-schema comparison lives here too, because "can the code in this artifact
read the state already on this disk" is the same kind of question as "was this
artifact signed by a key we trust": both are answered before anything is run,
and both fail closed.
"""

import hashlib
import re
from pathlib import Path


MAX_MANIFEST_BYTES = 256 * 1024

VERIFIED_SIGNATURE = "signature"
VERIFIED_NONE = "unverified"

RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_release_id(value):
    text = str(value or "").strip()
    if not RELEASE_ID.match(text):
        raise ReleaseError("invalid_release_id", "the release id is not a valid identifier")
    return text


# --- signature authority -----------------------------------------------------


class SignatureVerifier:
    """Detached-signature verification against a root-owned keyring.

    The keyring path comes from the host configuration only. ``gpgv`` is invoked
    through the allowlisted command runner with a fixed argv; there is no shell
    and no caller-supplied option.
    """

    def __init__(self, runner, *, keyring, fingerprints=()):
        self.runner = runner
        self.keyring = str(keyring or "")
        # A trust policy, not a key list: a keyring can hold more keys than a
        # release is allowed to be signed with, and "gpg said good" does not
        # say which key it was good for.
        self.fingerprints = tuple(
            str(item).replace(" ", "").upper() for item in fingerprints if str(item).strip()
        )

    @property
    def available(self):
        return bool(self.keyring) and self.runner is not None and self.runner.available("gpgv")

    def verify(self, manifest_path, signature_path):
        if not self.keyring:
            raise ReleaseError(
                "release_keyring_missing",
                "no OS release keyring is configured; an unsigned artifact is refused",
            )
        if not Path(self.keyring).is_file():
            raise ReleaseError(
                "release_keyring_missing", f"the OS release keyring {self.keyring} does not exist"
            )
        if self.runner is None or not self.runner.available("gpgv"):
            raise ReleaseError(
                "release_verification_unavailable", "gpgv is not installed on this appliance"
            )
        result = self.runner.run(
            "gpgv",
            [
                "--status-fd",
                "1",
                "--keyring",
                self.keyring,
                str(signature_path),
                str(manifest_path),
            ],
            timeout=60,
        )
        if not result.ok:
            raise ReleaseError(
                "release_signature_invalid",
                "the release manifest signature could not be verified against the appliance keyring",
            )
        unusable = unusable_signature(result.stdout)
        if unusable is not None:
            code, reason = unusable
            raise ReleaseError(
                code, f"the release manifest signature is not usable: {reason}"
            )
        observed = valid_signature_fingerprints(result.stdout)
        if not observed:
            raise ReleaseError(
                "release_signature_invalid",
                "gpg reported no valid signature over the release manifest",
            )
        if self.fingerprints and not set(observed) & set(self.fingerprints):
            raise ReleaseError(
                "release_signature_untrusted",
                f"the release manifest was signed by {observed[0]}, which is not a trusted "
                "release key",
            )
        return True

    def fingerprints_of(self, manifest_path, signature_path):
        """The keys a usable signature was made with, or ().

        A revoked or expired key still produces VALIDSIG, so the same refusal
        `verify` applies is applied here: a caller that only asks this question
        must not be handed a fingerprint it would then treat as trusted.
        """

        if not self.available or not Path(self.keyring).is_file():
            return ()
        result = self.runner.run(
            "gpgv",
            [
                "--status-fd",
                "1",
                "--keyring",
                self.keyring,
                str(signature_path),
                str(manifest_path),
            ],
            timeout=60,
        )
        if not result.ok:
            return ()
        if unusable_signature(result.stdout) is not None:
            return ()
        return valid_signature_fingerprints(result.stdout)


# gpgv reports these *alongside* VALIDSIG and still exits 0, so a verifier that
# reads only the exit status and VALIDSIG accepts a signature made with a key
# that was revoked or has expired.
UNUSABLE_SIGNATURE_MARKERS = {
    "EXPKEYSIG": ("release_signature_key_expired", "the signing key has expired"),
    "REVKEYSIG": ("release_signature_key_revoked", "the signing key was revoked"),
    "EXPSIG": ("release_signature_expired", "the signature has expired"),
    "ERRSIG": ("release_signature_invalid", "the signature could not be checked"),
}


def unusable_signature(status_output):
    """The first reason gpg gave for not trusting an otherwise valid signature."""

    for line in (status_output or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "[GNUPG:]" and parts[1] in UNUSABLE_SIGNATURE_MARKERS:
            return UNUSABLE_SIGNATURE_MARKERS[parts[1]]
    return None


def valid_signature_fingerprints(status_output):
    """The primary-key fingerprints gpg reported as VALIDSIG."""

    fingerprints = []
    for line in (status_output or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "[GNUPG:]" and parts[1] == "VALIDSIG":
            # Field 12 of VALIDSIG is the primary key fingerprint; field 3 is
            # the fingerprint of the signing (sub)key.
            primary = parts[11] if len(parts) >= 12 else parts[2]
            fingerprints.append(primary.upper())
    return tuple(fingerprints)


def file_digest(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


# --- the release source ------------------------------------------------------


# --- compatibility -----------------------------------------------------------


def state_schema_problems(release, *, recorded):
    """Whether the code in this artifact can read the state already on the disk.

    ``recorded`` is what this appliance's own state record says it was last
    written by. It is the one schema record in this system that does not come
    out of an installed package, and therefore the only one that can answer the
    question at all: every other number is compiled into a package and compared
    against a constant compiled into that same package.

    Undecidable is refused. An appliance that cannot say what its state is
    formatted as cannot be told that some artifact is safe for it.
    """

    if recorded is None:
        return [
            {
                "code": "state_schemas_unrecorded",
                "message": (
                    "this appliance has no record of what its stored state is formatted "
                    "as, so no artifact can be proven able to read it. The record is "
                    "ems-appliance/state-schema.json on the persistent partition; a "
                    "manager that cannot read one never rewrites it, so an unreadable "
                    "record has to be removed by hand and the next reconciliation will "
                    "adopt the partition"
                ),
            }
        ]

    recorded = dict(recorded)
    if not release.state_implements:
        # Every manifest this appliance accepts declares its schemas: the reader
        # refuses a manifest without them before it ever gets here. An artifact
        # that says nothing therefore cannot be proven able to read anything,
        # and guessing on its behalf is exactly the step that would let an
        # older manager silently open state it does not understand.
        return [
            {
                "code": "artifact_state_schemas_undeclared",
                "message": (
                    "the artifact does not say what state formats its manager implements, "
                    "so it cannot be proven able to read this appliance's state"
                ),
            }
        ]


    problems = []
    for axis in sorted(recorded):
        if axis not in release.state_implements:
            problems.append(
                {
                    "code": "artifact_state_schema_undeclared",
                    "message": (
                        f"this appliance holds {axis} state; the artifact does not say "
                        "whether its manager can read that format"
                    ),
                }
            )
            continue
        implements = release.state_implements[axis]
        if recorded[axis] > implements:
            problems.append(
                {
                    "code": "artifact_state_schema_too_old",
                    "message": (
                        f"this appliance's state is written at {axis} schema {recorded[axis]}; "
                        f"the artifact's manager implements {implements} and could not read it"
                    ),
                }
            )
            continue
        floor = release.state_reads.get(axis, 1)
        if recorded[axis] < floor:
            problems.append(
                {
                    "code": "artifact_state_schema_unreadable",
                    "message": (
                        f"the artifact's manager reads {axis} schema {floor} or newer; this "
                        f"appliance's state is written at {recorded[axis]}"
                    ),
                }
            )
    # An axis the artifact declares and this partition has no state for is not a
    # problem: there is nothing of that format here to be incompatible with.
    return problems


