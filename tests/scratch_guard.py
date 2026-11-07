# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refuse a broad test run that cannot fit its own scratch data.

A full run of this suite writes tens of GB of temporary data — appliance ``.deb``
packages, disk images, EFI vars, database snapshots. A passing test's directory
is removed as soon as it ends (``tmp_path_retention_policy`` in ``pytest.ini``),
so a run only holds a fraction of that at a time; what it holds still fills a
small enough temporary filesystem part-way through, and the failure is not a
test failure: pytest dies with ``INTERNALERROR``, and until the filesystem is
cleaned every later command fails for reasons that have nothing to do with the
code.

The check is deliberately proportionate. A handful of tests needs no room worth
naming, so only a broad selection is refused, and only when the space is not
there. It states the fix rather than the symptom.
"""

import os

# Measured on the maintainer's host (apparent size, sampled every 10 s): a full
# non-Docker run (18k tests) peaked below 0.3 GB once passing tests gave their
# directories back. The large images the appliance tests write are sparse, so
# what failing tests keep is small too; the floor leaves room for it and stays
# below the 12.9 GB a GitHub runner had free.
REQUIRED_FREE_BYTES = 4 * 1024**3

# The floor from when pytest kept every test's directory to the end of the run,
# for a run that asks for that back (``-o tmp_path_retention_policy=all``).
KEEP_ALL_REQUIRED_FREE_BYTES = 20 * 1024**3

RETURNING_RETENTION_POLICY = "failed"

# Below this, the selection is a targeted tier (see docs/developer/testing.md)
# and its scratch is negligible.
BROAD_SELECTION_ITEMS = 2000

OVERRIDE_ENV = "EMS_ALLOW_SMALL_SCRATCH"


def scratch_shortfall(free_bytes, item_count, temp_dir, environ=None, retention_policy=RETURNING_RETENTION_POLICY):
    """Return the refusal message for this run, or ``None`` to let it proceed."""

    environ = os.environ if environ is None else environ
    if environ.get(OVERRIDE_ENV):
        return None
    if item_count < BROAD_SELECTION_ITEMS:
        return None
    required = REQUIRED_FREE_BYTES if retention_policy == RETURNING_RETENTION_POLICY else KEEP_ALL_REQUIRED_FREE_BYTES
    if free_bytes >= required:
        return None
    return (
        f"Only {free_bytes / 1024**3:.1f} GB free in {temp_dir}, and this "
        f"selection of {item_count} tests needs about "
        f"{required / 1024**3:.0f} GB of scratch. A run that fills "
        "the temporary filesystem does not fail as a test failure: it dies with "
        "an internal error and leaves the host unusable until it is cleaned.\n"
        "Point the scratch somewhere with room:\n"
        "    export TMPDIR=/path/with/room\n"
        f"Set {OVERRIDE_ENV}=1 to run anyway."
    )
