#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check an A/B update artifact the way the appliance will, before shipping it.
#
#   scripts/appliance-inspect-rpi-ab-update.sh [--json] [--keyring FILE]
#          [--trusted-fingerprint FPR]... [--require-signature] <manifest.json>
#
# The artifact is fed through the same parser, signature check and member
# allowlist the runtime uses, so an artifact that would be refused on an
# appliance is refused here instead of in the field. Nothing is written to a
# block device and nothing is extracted outside a temporary directory.
#
# The signature is verified cryptographically, not counted. "manifest.json.asc
# exists" is a statement about a filename: it is true of a signature made with
# any key, over any bytes, including bytes that are no longer the ones in front
# of the inspector. With --keyring the detached signature is checked against
# that keyring, the signing key's fingerprint is reported, and
# --trusted-fingerprint restricts which key a release may be signed with.
#
# --require-signature is production mode: an unsigned artifact, a signature
# that does not verify, and a signature from an untrusted key are all failures.
# Without it an unsigned artifact reports NOT RUN, which is a rehearsal and not
# a release.
#
# Exit status: 0 every check passed, 1 a check failed, 2 the command line is
# wrong, 3 the host cannot inspect.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FORMAT=text
MANIFEST=""
KEYRING=${EMS_APPLIANCE_OS_KEYRING:-}
FINGERPRINTS=${EMS_APPLIANCE_OS_TRUSTED_FINGERPRINTS:-}
REQUIRE_SIGNATURE=no

usage() {
    sed -n '3,25p' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        --keyring) KEYRING=${2:?--keyring needs a file}; shift 2 ;;
        --keyring=*) KEYRING=${1#*=}; shift ;;
        --trusted-fingerprint)
            FINGERPRINTS="$FINGERPRINTS ${2:?--trusted-fingerprint needs a fingerprint}"
            shift 2 ;;
        --trusted-fingerprint=*) FINGERPRINTS="$FINGERPRINTS ${1#*=}"; shift ;;
        --require-signature) REQUIRE_SIGNATURE=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
        *) [ -z "$MANIFEST" ] || { echo "one manifest at a time" >&2; exit 2; }
           MANIFEST=$1; shift ;;
    esac
done

[ -n "$MANIFEST" ] || { usage >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || {
    echo "appliance-inspect-rpi-ab-update: python3 is not installed" >&2
    echo "RESULT: NOT RUN (required_tool_missing)" >&2
    exit 3
}
[ -f "$MANIFEST" ] || {
    echo "appliance-inspect-rpi-ab-update: $MANIFEST does not exist" >&2
    echo "RESULT: NOT RUN (artifact_unavailable)" >&2
    exit 3
}

EMS_KEYRING="$KEYRING" EMS_TRUSTED_FINGERPRINTS="$FINGERPRINTS" \
EMS_REQUIRE_SIGNATURE="$REQUIRE_SIGNATURE" \
PYTHONPATH="$ROOT" python3 - "$MANIFEST" "$FORMAT" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

from appliance import ab_persistence, commands, os_artifacts, os_releases, rpi_image_gen, sparse

manifest_path, output_format = sys.argv[1:3]
lock = rpi_image_gen.read_lock()
findings = []


def record(check, ok, detail=""):
    findings.append({"check": check, "result": "pass" if ok else "fail", "detail": detail})


def filesystem_of(path):
    """The filesystem an expanded image holds, by magic. Nothing is mounted."""

    import struct

    with open(path, "rb") as handle:
        head = handle.read(0x440)
    if len(head) > 0x43A and struct.unpack_from("<H", head, 0x438)[0] == 0xEF53:
        return "ext4"
    if len(head) > 512 and head[510:512] == b"\x55\xaa" and head[0:1] in (b"\xeb", b"\xe9"):
        return "vfat"
    return ""


payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
try:
    release = os_releases.parse_manifest(payload)
    record("manifest_parses", True, release.release_id)
except os_releases.ReleaseError as exc:
    record("manifest_parses", False, exc.message)
    print(json.dumps({"result": "fail", "findings": findings}, indent=2))
    sys.exit(1)

record(
    "members_are_upstreams",
    set(release.members) == set(lock.update_members),
    ", ".join(sorted(release.members)),
)
record(
    "image_layer",
    str(payload.get("image_layer") or "") == lock.image_layer,
    str(payload.get("image_layer") or "none"),
)
record(
    "persistent_schema",
    release.persistent_schema_version == ab_persistence.PERSISTENT_SCHEMA_VERSION,
    str(release.persistent_schema_version),
)
record("hardware", bool(release.compatible_hardware), ", ".join(release.compatible_hardware))

archive = Path(manifest_path).parent / release.archive_name
if not archive.is_file():
    record("archive_present", False, str(archive))
else:
    record("archive_present", True, str(archive))
    observed = os_releases.file_digest(archive)
    record(
        "archive_digest",
        observed == release.archive_digest,
        f"{observed} (manifest {release.archive_digest})",
    )
    record("archive_size", archive.stat().st_size == release.archive_size, str(archive.stat().st_size))
    # Beside the archive, not in TMPDIR: the system member expands to 4 GiB and
    # /tmp is a RAM-sized tmpfs on any systemd default. The directory holding
    # the archive is the one place known to have room for it.
    with tempfile.TemporaryDirectory(dir=str(archive.parent)) as staging:
        try:
            staged = os_artifacts.extract(archive, Path(staging) / "members", release)
            record("members_extract_and_verify", True, ", ".join(sorted(staged.members)))
        except (os_artifacts.ArtifactError, os_releases.ReleaseError) as exc:
            record("members_extract_and_verify", False, exc.message)
            staged = None
        # A verified member is a container, not a filesystem. Expanding it here
        # is the only way to tell that the manifest describes what a partition
        # would end up holding.
        for name in sorted(release.members) if staged else ():
            member = release.member(name)
            source = staged.path(name)
            if member.encoding != sparse.ENCODING_ANDROID_SPARSE:
                record(f"member_encoding:{name}", True, member.encoding or "raw")
                continue
            record(f"member_is_sparse:{name}", sparse.is_sparse(source), member.encoding)
            try:
                report = sparse.expand(
                    source,
                    Path(staging) / f"{name}.img",
                    expected_size=member.expanded_size,
                    expected_digest=member.expanded_digest,
                )
            except sparse.SparseError as exc:
                record(f"member_expands:{name}", False, exc.message)
                continue
            record(
                f"member_expands:{name}",
                True,
                f"{report.bytes_written} bytes, {report.digest}",
            )
            observed = filesystem_of(Path(staging) / f"{name}.img")
            record(
                f"member_filesystem:{name}",
                observed == member.filesystem,
                f"{observed or 'unrecognised'} (manifest {member.filesystem})",
            )

def record_state(check, state, detail=""):
    findings.append({"check": check, "result": state, "detail": detail})


# "manifest.json.asc exists" is a statement about a filename. What a release
# needs is that these exact bytes were signed by a key this project trusts.
signature = Path(manifest_path).with_suffix(".json.asc")
keyring = os.environ.get("EMS_KEYRING") or ""
trusted = tuple(
    item for item in (os.environ.get("EMS_TRUSTED_FINGERPRINTS") or "").split() if item
)
required = os.environ.get("EMS_REQUIRE_SIGNATURE") == "yes"

if not signature.is_file():
    record_state(
        "detached_signature",
        "fail" if required else "not_run",
        f"{signature} is missing; unsigned artifacts are refused in production",
    )
    record_state(
        "signature_valid",
        "fail" if required else "not_run",
        "there is no signature to verify",
    )
else:
    record_state("detached_signature", "pass", str(signature))
    if not keyring:
        record_state(
            "signature_valid",
            "fail" if required else "not_run",
            "no --keyring was given, so the signature was not verified",
        )
    else:
        verifier = os_releases.SignatureVerifier(
            commands.CommandRunner(), keyring=keyring, fingerprints=trusted
        )
        if not verifier.available:
            record_state(
                "signature_valid",
                "fail" if required else "not_run",
                "gpg is not installed, so the signature could not be verified",
            )
        else:
            try:
                verifier.verify(manifest_path, signature)
            except os_releases.ReleaseError as exc:
                record_state("signature_valid", "fail", f"{exc.code}: {exc.message}")
            else:
                observed = verifier.fingerprints_of(manifest_path, signature)
                record_state(
                    "signature_valid",
                    "pass",
                    f"signed by {', '.join(observed) or 'a key in the keyring'}",
                )
                if trusted:
                    record_state("signature_key_trusted", "pass", ", ".join(trusted))
                else:
                    record_state(
                        "signature_key_trusted",
                        "fail" if required else "not_run",
                        "no --trusted-fingerprint policy was given, so any key in the "
                        "keyring would have been accepted",
                    )

counts = {"pass": 0, "fail": 0, "not_run": 0}
for finding in findings:
    counts[finding["result"]] = counts.get(finding["result"], 0) + 1
summary = {
    "result": "fail" if counts["fail"] else "pass",
    "counts": counts,
    "findings": findings,
}

if output_format == "json":
    print(json.dumps(summary, indent=2, sort_keys=True))
else:
    for finding in findings:
        print(f"{finding['result'].upper():8} {finding['check']:28} {finding['detail']}")
    print()
    print(f"RESULT: {summary['result'].upper()}")

sys.exit(1 if counts["fail"] else 0)
PY
