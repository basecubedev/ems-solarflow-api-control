#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch the newest stable Appliance Manager package an index names.

    scripts/appliance-fetch-manager-package.py --index URL --into DIR
        [--keyring FILE] [--version V]

What an image bakes in should be the package an operator is offered, not a
second build of the same source that only happens to match. This is the fetch
that makes those the same bytes.

The index is a suggestion and is never trusted: what may be used is decided by
the detached signature over the manifest, verified with the same program and
the same keyring the appliance itself uses. A candidate release never wins --
an image that quietly baked one in would ship it to every card.

Exit status: 0 a verified package was written, 1 something failed verification,
2 the command line is wrong, 3 the index names no stable release yet. Three is
separate on purpose: before the first Manager release there is nothing to fetch
and that is not an error, it is a build that has to fall back to its own source.
"""

import argparse
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance import artifact_trust, manager_releases, release_fetch  # noqa: E402
from appliance.config import _as_tuple, _read_ini  # noqa: E402
from appliance.version import (  # noqa: E402
    is_readable,
    is_stable,
    newest_stable as newest_stable_entry,
)

DEFAULT_KEYRING = ROOT / "packaging" / "appliance" / "config" / "release-keyring.gpg"
SHIPPED_CONFIG = ROOT / "packaging" / "appliance" / "config" / "appliance.conf"


def shipped_fingerprints():
    """The release signers the appliance this image becomes will accept."""

    return _as_tuple(_read_ini(SHIPPED_CONFIG), "release_fingerprints", ())


class GpgvRunner:
    """The two methods artifact_trust.SignatureVerifier asks of a runner."""

    def available(self, tool):
        return shutil.which(tool) is not None

    def run(self, tool, args, timeout=None):
        done = subprocess.run(
            [tool, *args], capture_output=True, text=True, check=False, timeout=timeout
        )
        return SimpleNamespace(ok=done.returncode == 0, stdout=done.stdout, stderr=done.stderr)
# An index is a list of names, and a manifest describes one package. Neither is
# large, and neither may decide how much of this host's memory it occupies.
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024


def fetch(url, *, limit, label):
    request = urllib.request.Request(url, headers={"User-Agent": "ems-appliance-build"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise SystemExit(f"{label} is larger than this build will read")
    return payload


def described_version(entry):
    return entry["described"].get("release_version", "")


def newest_stable(index, *, wanted):
    """The newest stable candidate the index names, or None.

    ``parse_index`` has already refused anything malformed and sorted what
    survived, but its order is over the index's own claims. Choosing by version
    here is the same claim read again -- which is fine, because nothing is
    trusted until the signature over the manifest is checked. What this decides
    is only which manifest to go and fetch.
    """

    candidates = release_fetch.parse_index(index)
    if wanted:
        candidates = [
            entry for entry in candidates
            if release_fetch.claim_matches(described_version(entry), wanted)
        ]
    return newest_stable_entry(candidates, version_of=described_version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, help="the package index url")
    parser.add_argument("--into", required=True, help="a directory to write the package into")
    parser.add_argument("--keyring", default=str(DEFAULT_KEYRING))
    parser.add_argument(
        "--fingerprint",
        action="append",
        default=None,
        help="a release signer to accept (default: release_fingerprints of the shipped appliance.conf)",
    )
    parser.add_argument("--version", default="", help="take this version instead of the newest")
    args = parser.parse_args(argv)

    if not args.index.startswith("https://"):
        raise SystemExit("--index must be https: an unverified transport is not a source")
    keyring = Path(args.keyring)
    if not keyring.is_file():
        raise SystemExit(f"{keyring} is not a keyring; there is nothing to verify against")

    # Not published and not reachable are different answers, and only the first
    # is a fallback. Before the first Manager release the index is genuinely not
    # there and a build that then makes its own package is right. A 503, a
    # timeout, a DNS failure or a TLS error are not that -- and urllib raises all
    # of them as OSError, so catching the base class turned every one of them
    # into "nothing published yet" and a silently unsigned package in the image.
    try:
        document = fetch(args.index, limit=MAX_DOCUMENT_BYTES, label="the index")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise SystemExit(f"the index at {args.index} could not be read: HTTP {exc.code}")
        print(f"no index is published at {args.index}", file=sys.stderr)
        return 3
    except OSError as exc:
        raise SystemExit(f"the index at {args.index} could not be reached: {exc}")
    try:
        index = json.loads(document)
    except ValueError as exc:
        raise SystemExit(f"the index at {args.index} is not readable json: {exc}")

    try:
        chosen = newest_stable(index, wanted=args.version.strip().lstrip("vV"))
    except release_fetch.FetchError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}")
    if chosen is None:
        print("the index names no stable release", file=sys.stderr)
        return 3

    into = Path(args.into)
    into.mkdir(parents=True, exist_ok=True)
    release_id = chosen["release_id"]
    manifest_path = into / f"{release_id}.manifest.json"
    signature_path = into / f"{release_id}.manifest.json.asc"
    manifest_path.write_bytes(
        fetch(chosen["manifest_url"], limit=MAX_DOCUMENT_BYTES, label="the manifest")
    )
    signature_path.write_bytes(
        fetch(chosen["signature_url"], limit=MAX_DOCUMENT_BYTES, label="the signature")
    )

    # The appliance's own verifier: bare gpgv accepts revoked, expired and foreign keys.
    fingerprints = tuple(args.fingerprint) if args.fingerprint else shipped_fingerprints()
    verifier = artifact_trust.SignatureVerifier(
        GpgvRunner(), keyring=str(keyring.resolve()), fingerprints=fingerprints
    )
    try:
        verifier.verify(manifest_path, signature_path)
    except artifact_trust.ReleaseError as exc:
        raise SystemExit(
            f"{release_id}: the signature is not one this project trusts ({exc})"
        )

    try:
        manifest = manager_releases.parse_manifest(
            json.loads(manifest_path.read_text("utf-8")), release_id=release_id
        )
    except manager_releases.ManagerReleaseError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}")

    # Which entry to fetch was decided on the index's own claim, because that is
    # all there is to go on before a signature has been checked. Now there is a
    # signed answer, and the two have to agree: an index that overstates a
    # version would otherwise have this build install an older Manager while
    # reporting the newer one, with every signature valid.
    if not release_fetch.claim_matches(described_version(chosen), manifest.version):
        raise SystemExit(
            f"the index calls {release_id} version {described_version(chosen)}, "
            f"but its signed manifest says {manifest.version}"
        )
    if not is_stable(manifest.version):
        reason = "a candidate" if is_readable(manifest.version) else "not a readable version"
        raise SystemExit(f"{manifest.version} is {reason}; no image bakes one in")

    package_path = into / Path(manifest.artifact_name).name
    package_path.write_bytes(
        fetch(chosen["archive_url"], limit=MAX_PACKAGE_BYTES, label="the package")
    )
    digest = artifact_trust.file_digest(package_path)
    if digest != manifest.artifact_digest:
        raise SystemExit(
            f"{package_path.name} hashes to {digest}, "
            f"but the signed manifest names {manifest.artifact_digest}"
        )

    print(f"{package_path}", flush=True)
    print(
        f"{manifest.version}: signature accepted by {keyring.name}, digest matches",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
