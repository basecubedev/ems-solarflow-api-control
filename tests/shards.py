# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Split a test selection into shards of whole files, balanced by their measured time.

``pytest --shard K/N`` keeps the test files of shard ``K`` (1-based) of ``N``.
A file stays whole, so its module-scoped fixtures (a built image, a booted
container) are set up once in one runner. Files are dealt out longest first
to the shard with the least time so far, by the seconds in
``tests/shard_weights.json``; a file it does not list counts as the median.
Every shard computes the same split from the same selection, so the shards
together run each selected file exactly once.
"""

import json
import statistics
from pathlib import Path

WEIGHTS_FILE = Path(__file__).with_name("shard_weights.json")


def parse_shard(value):
    """``"K/N"`` as ``(K, N)``, or ``None`` for no sharding; anything else is an error."""

    if value in (None, ""):
        return None
    try:
        index, _, count = str(value).partition("/")
        index, count = int(index), int(count)
    except ValueError:
        raise ValueError(f"--shard takes K/N, not {value!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"--shard {value!r} names no shard of {count}")
    return index, count


def load_weights(path=WEIGHTS_FILE):
    try:
        weights = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(name): float(seconds) for name, seconds in weights.items() if isinstance(seconds, (int, float))}


def assign(files, weights, count):
    """Each file's shard (0-based), longest first onto the least loaded shard."""

    known = [weights[name] for name in files if name in weights]
    fallback = statistics.median(known) if known else 1.0
    loads = [0.0] * count
    owner = {}
    for name in sorted(set(files), key=lambda name: (-weights.get(name, fallback), name)):
        shard = min(range(count), key=lambda index: (loads[index], index))
        owner[name] = shard
        loads[shard] += weights.get(name, fallback)
    return owner


def file_of(nodeid):
    return nodeid.split("::", 1)[0]


def select(items, shard, weights):
    """The items of shard ``(K, N)`` and the ones it leaves to the others."""

    index, count = shard
    owner = assign([file_of(item.nodeid) for item in items], weights, count)
    kept = [item for item in items if owner[file_of(item.nodeid)] == index - 1]
    left = [item for item in items if owner[file_of(item.nodeid)] != index - 1]
    return kept, left
