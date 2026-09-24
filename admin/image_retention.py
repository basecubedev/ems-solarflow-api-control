# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded local image history for the two images this product owns.

Docker keeps every image it ever pulled, and nothing removes them. On an
appliance that is a slow leak rather than an event: one upgrade adds roughly
180 MB of unique layers per image, so a monthly release cycle fills a 16 GB
card in about two years. A full card does not merely fail the next upgrade --
InfluxDB and the EMS stop being able to write, and the documented recovery is
re-flashing the card.

Two rules keep this from becoming the more expensive problem it replaces.

*Only our own images.* The allowlist below is the whole of what may ever be
removed. A host running this Admin also runs InfluxDB, and a non-appliance
host runs whatever else its owner puts there; an image that cannot be proven
to come from one of these two repositories is never a candidate, including an
untagged one whose origin is no longer readable.

*Never the way back.* Keeping the newest N is not by itself safe, because the
rollback target is the build that ran *before*, which is not the same as the
build that was *built* most recently -- a downgrade makes the previous build an
old one. So protection is explicit and passed in by the caller, never inferred
from ordering.
"""

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO

# The complete set of repositories this module may remove from. Anything else
# on the host belongs to somebody else.
MANAGED_REPOSITORIES = (ADMIN_IMAGE_REPO, EMS_IMAGE_REPO)

# Per repository, not per product: an Admin and an EMS image are pulled as a
# pair, so five keeps five switchable System Builds. Development and release
# builds share the budget -- a test appliance churns through development builds
# and would otherwise evict the releases it has to be able to return to.
DEFAULT_KEEP = 5


def is_managed(repository) -> bool:
    """Whether this repository is one of the two this product owns."""

    return str(repository or "") in MANAGED_REPOSITORIES


def plan_removals(images, *, protected_digests=(), keep=DEFAULT_KEEP):
    """Digests that may be removed, per repository, newest kept.

    ``images`` are dicts carrying ``repository``, ``digest`` and ``created``
    (an ISO-8601 string or any sortable value). Ordering is by ``created``
    descending, with the digest as a tiebreak so a run is reproducible when two
    images share a timestamp.

    An image is removable only when its repository is managed, its digest is
    readable, it is not protected, and it falls outside the newest ``keep`` of
    its repository. Returns digests in the order they may be removed, oldest
    first, so a partial run removes the least valuable images.
    """

    budget = max(int(keep), 1)
    protected = {str(d) for d in protected_digests if d}

    by_repository = {}
    for image in images or ():
        if not isinstance(image, dict):
            continue
        repository = str(image.get("repository") or "")
        digest = str(image.get("digest") or "")
        # An image whose origin or identity cannot be read is left alone: it
        # cannot be proven to be ours, and a digest is what removal names.
        if not digest or not is_managed(repository):
            continue
        by_repository.setdefault(repository, []).append((image.get("created") or "", digest))

    removable = []
    for repository in sorted(by_repository):
        entries = sorted(
            by_repository[repository], key=lambda item: (str(item[0]), item[1]), reverse=True
        )
        # Protected images are counted against the budget before anything else.
        # Counting them where they happen to fall in the ordering would let a
        # pinned *old* build raise the real budget above the configured number,
        # because by then the newest ones have already filled it.
        protected_here = {digest for _created, digest in entries if digest in protected}
        kept = len(protected_here)
        seen = set()
        for created, digest in entries:
            if digest in seen:
                continue
            seen.add(digest)
            if digest in protected:
                continue
            if kept < budget:
                kept += 1
                continue
            removable.append((str(created), digest))

    removable.sort()
    return [digest for _created, digest in removable]
