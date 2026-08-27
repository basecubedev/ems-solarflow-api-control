# SPDX-License-Identifier: AGPL-3.0-or-later
"""Appliance Manager package version, and how versions compare.

This is the version of the host package, deliberately independent from the EMS
Admin container version it manages.

The comparator lives here because two callers need the same answer and used to
compute it separately: the OS release gates and the container release index.
Both discarded everything after the first hyphen, which made ``0.1.0-rc1`` and
``0.1.0`` compare equal — so a candidate could not be told from its own final
release, in either direction, by either caller.
"""

import re

APPLIANCE_VERSION = "0.1.0"

PACKAGE_NAME = "ems-appliance-manager"
SUPPORTED_ARCHITECTURES = ("arm64",)
# The boards this package runs on. Not the same list as the boards this project
# builds an image for: the package installs on Raspberry Pi OS anywhere.
SUPPORTED_PI_MODELS = ("Raspberry Pi 3", "Raspberry Pi 4", "Raspberry Pi 5")

_TRAILING_NUMBER = re.compile(r"^([^0-9]+)([0-9]+)$")


def version_key(text):
    """A sortable key where a prerelease ranks below the release it precedes.

    Numeric identifiers compare numerically and rank below alphanumeric ones,
    which is what makes ``1.0.0-rc1`` order before ``1.0.0-rc.beta`` and both
    before ``1.0.0``. Anything unparseable degrades to zero rather than raising:
    these keys gate installs, and a refusal has to come from a rule, not from a
    manifest that happened to spell its version oddly.
    """

    raw = str(text or "").strip().lstrip("vV")
    # Debian spells a pre-release with a tilde, and sorts it below the release:
    # `0.1.0~rc1 < 0.1.0`, while `0.1.0-rc1` is a *revision* and sorts above.
    # Splitting only on the hyphen made the tilde form invisible here -- it
    # parsed as the release itself and compared equal, which is the same defect
    # this function was written to fix, in the spelling the packaging uses.
    marker = min((raw.find(c) for c in "~-" if c in raw), default=-1)
    core, prerelease = (raw, "") if marker < 0 else (raw[:marker], raw[marker + 1:])
    parts = [int(chunk) if chunk.isdigit() else 0 for chunk in core.split(".")]
    while len(parts) < 3:
        parts.append(0)
    release = tuple(parts[:3])

    if not prerelease:
        # A release outranks every prerelease that carries the same core.
        return (*release, 1, ())

    identifiers = []
    for chunk in prerelease.replace("+", ".").split("."):
        if chunk.isdigit():
            identifiers.append((0, "", int(chunk)))
            continue
        # A trailing number counts as a number. Strict semver compares "rc10"
        # and "rc2" as text and puts rc10 first; this project writes rc1, rc2,
        # rc10 without a separator, and ordering the tenth candidate before the
        # second is wrong in the direction that matters -- a release gate would
        # read the newest candidate as the oldest.
        match = _TRAILING_NUMBER.match(chunk)
        if match:
            identifiers.append((1, match.group(1), int(match.group(2))))
        else:
            identifiers.append((1, chunk, 0))
    return (*release, 0, tuple(identifiers))
