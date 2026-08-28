#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Is the shipped release identity one this project can actually sign with?

    scripts/appliance-check-release-identity.py [--keyring FILE] [--config FILE]

The fingerprint an appliance pins is frozen the moment a card is flashed.
``appliance.conf`` ships to ``/usr/share``, it is not a dpkg conffile, and
``config_seed`` reports an existing ``/etc`` copy as present without reading it,
so no update and no operation can correct it afterwards. A card flashed against
an identity nobody holds the secret half of can never install a Manager package
again -- neither an upgrade nor the downgrade that is its only recovery -- and
the repair is a root console on every unit.

That makes publishing an image the point of no return, which is why this refuses
there rather than warning in a document. Two properties, both read back from the
artefacts rather than taken on trust:

  the pinned fingerprint is the keyring's primary   -- pinning the subkey passes
                                                       review and then refuses
                                                       every update, because gpg
                                                       reports the primary in
                                                       VALIDSIG field 12
  the primary cannot sign                           -- what
                                                       appliance-new-release-identity.sh
                                                       produces, and the marker
                                                       that tells a real identity
                                                       from the hand-made
                                                       placeholder this project
                                                       started with

Exit status: 0 the identity is usable, 1 it is not, 2 the command line is wrong,
3 gpg is not installed.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEYRING = ROOT / "packaging/appliance/config/release-keyring.gpg"
DEFAULT_CONFIG = ROOT / "packaging/appliance/config/appliance.conf"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Refuse a release identity no one can sign with."
    )
    parser.add_argument("--keyring", default=str(DEFAULT_KEYRING))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    return parser.parse_args(argv)


def listing(keyring):
    result = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(keyring)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise SystemExit(f"{keyring} could not be read as a keyring: {result.stderr.strip()}")
    return result.stdout


def keys(text):
    """Every ``pub``/``sub`` with the fingerprint that follows it."""

    found = []
    pending = None
    for line in text.splitlines():
        fields = line.split(":")
        if fields[0] in ("pub", "sub"):
            pending = {"kind": fields[0], "capabilities": fields[11], "fingerprint": ""}
            found.append(pending)
        elif fields[0] == "fpr" and pending is not None and not pending["fingerprint"]:
            pending["fingerprint"] = fields[9]
    return found


def pinned(config):
    text = Path(config).read_text(encoding="utf-8")
    match = re.search(r"^release_fingerprints\s*=\s*(\S+)\s*$", text, re.M)
    return match.group(1) if match else ""


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if shutil.which("gpg") is None:
        print("release-identity: gpg is not installed", file=sys.stderr)
        return 3

    held = keys(listing(args.keyring))
    primaries = [key for key in held if key["kind"] == "pub"]
    signing = [key for key in held if key["kind"] == "sub" and "s" in key["capabilities"]]
    problems = []

    if len(primaries) != 1:
        problems.append(f"the keyring holds {len(primaries)} primary keys; it must hold one")
    if not signing:
        problems.append("the keyring holds no signing subkey, so nothing can sign a release")

    pin = pinned(args.config)
    if primaries and pin != primaries[0]["fingerprint"]:
        problems.append(
            f"{Path(args.config).name} pins {pin or 'nothing'}, "
            f"which is not the keyring's primary {primaries[0]['fingerprint']}"
        )

    # 's' in the primary's capabilities. The uppercase letters describe the whole
    # key rather than this one, so they are dropped before looking.
    if primaries and "s" in primaries[0]["capabilities"].replace("S", "").replace("C", ""):
        problems.append(
            f"the primary {primaries[0]['fingerprint']} can sign. "
            "scripts/appliance-new-release-identity.sh makes a certify-only primary, so this "
            "is the hand-made placeholder whose secret half this project does not hold. "
            "Every card flashed against it can never install a Manager package again, "
            "and appliance.conf cannot be corrected in the field"
        )

    for problem in problems:
        print(f"REJECTED  {problem}", file=sys.stderr)
    if problems:
        print("RESULT: FAIL", file=sys.stderr)
        return 1

    print(f"primary   {primaries[0]['fingerprint']}  (certify-only, pinned)")
    for key in signing:
        print(f"signing   {key['fingerprint']}")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
