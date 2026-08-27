# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assembling a release index, for every kind of artefact this project ships.

The index format is one format: a list of places to look, each pointing at a
manifest, its detached signature and the artefact itself. What differs between
an OS image and a manager package is how an entry is described, not how the
list is built, retained or checked — so that part lives here rather than being
written a second time.

It carries history for one reason: a release that turns out to be bad is only
recoverable if the one before it is still listed.
"""

import json
from pathlib import Path

from appliance import release_fetch


class ReleaseIndexError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def load_previous(path):
    """The entries an existing index carries, or none when there is no file."""

    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = payload.get("releases")
    if not isinstance(entries, list):
        raise ReleaseIndexError(f"{path}: no release list to carry forward")
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("release_id")]


def assemble(entries, *, previous="", keep=0):
    """The index and what retention dropped from it.

    A rebuild of the same release replaces its entry rather than joining it:
    two rows under one identifier is an index that cannot be resolved.
    """

    merged = {}
    for entry in load_previous(previous):
        merged[entry["release_id"]] = entry
    minted = []
    for entry in entries:
        merged[entry["release_id"]] = entry
        minted.append(entry["release_id"])

    releases = sorted(merged.values(), key=release_fetch.sort_key, reverse=True)
    dropped = []
    if keep and len(releases) > keep:
        dropped = releases[keep:]
        releases = releases[:keep]
    # Retention sorts by version, and the release being published is not always
    # the newest one -- a patch to an older line, or a rebuild, sorts below what
    # is already listed. Dropping it here would publish an assets bundle no
    # appliance can see, which reads as a release that silently did not happen.
    published = {entry["release_id"] for entry in dropped} & set(minted)
    if published:
        raise ReleaseIndexError(
            "--keep would drop the release this run just built: " + ", ".join(sorted(published))
        )
    return {"format_version": release_fetch.INDEX_FORMAT_VERSION, "releases": releases}, dropped


def verify(index):
    """Refuse an index the appliance would silently shrink."""

    accepted = release_fetch.parse_index(index)
    if len(accepted) != len(index["releases"]):
        raise ReleaseIndexError("the appliance would drop entries from this index")
    return accepted


__all__ = ["ReleaseIndexError", "assemble", "load_previous", "verify"]
