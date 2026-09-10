# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refuse a broad test run that cannot fit its own scratch data.

A full run of this suite writes tens of GB of temporary data — appliance ``.deb``
packages, disk images, EFI vars, database snapshots. On a host whose temporary
directory is a small tmpfs that fills it part-way through, and the failure is
not a test failure: pytest dies with ``INTERNALERROR`` after half an hour, and
until the filesystem is cleaned every later command fails for reasons that have
nothing to do with the code.

The check is deliberately proportionate. A handful of tests needs no room worth
naming, so only a broad selection is refused, and only when the space is not
there. It states the fix rather than the symptom.
"""

import os

# Measured on the maintainer's host: a full non-Docker run (11k tests) overflowed
# a 12 GB tmpfs. The floor is that measurement rounded up, not a guess at what
# any one test needs.
REQUIRED_FREE_BYTES = 20 * 1024**3

# Below this, the selection is a targeted tier (see docs/developer/testing.md)
# and its scratch is negligible.
BROAD_SELECTION_ITEMS = 2000

OVERRIDE_ENV = "EMS_ALLOW_SMALL_SCRATCH"


def scratch_shortfall(free_bytes, item_count, temp_dir, environ=None):
    """Return the refusal message for this run, or ``None`` to let it proceed."""

    environ = os.environ if environ is None else environ
    if environ.get(OVERRIDE_ENV):
        return None
    if item_count < BROAD_SELECTION_ITEMS:
        return None
    if free_bytes >= REQUIRED_FREE_BYTES:
        return None
    return (
        f"Only {free_bytes / 1024**3:.1f} GB free in {temp_dir}, and this "
        f"selection of {item_count} tests needs about "
        f"{REQUIRED_FREE_BYTES / 1024**3:.0f} GB of scratch. A run that fills "
        "the temporary filesystem does not fail as a test failure: it dies with "
        "an internal error and leaves the host unusable until it is cleaned.\n"
        "Point the scratch somewhere with room:\n"
        "    export TMPDIR=/path/with/room\n"
        f"Set {OVERRIDE_ENV}=1 to run anyway."
    )
