# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which local images the product may remove, and which it must never touch."""

import pytest

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO
from admin.image_retention import (
    DEFAULT_KEEP,
    MANAGED_REPOSITORIES,
    is_managed,
    plan_removals,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.unit,
]


def _image(repository, digest, created):
    return {"repository": repository, "digest": digest, "created": created}


def _series(repository, count, *, start=1):
    """``count`` images, oldest first, created one day apart."""

    return [
        _image(repository, f"sha256:{repository[-3:]}{index:03d}", f"2026-01-{index:02d}T00:00:00Z")
        for index in range(start, start + count)
    ]


def test_only_the_two_product_repositories_are_ever_managed():
    """The allowlist is the whole of what may be removed.

    A host running this Admin also runs InfluxDB, and a non-appliance host runs
    whatever else its owner put there. Removing a foreign image would destroy
    somebody else's product to reclaim space for ours.
    """

    assert MANAGED_REPOSITORIES == (ADMIN_IMAGE_REPO, EMS_IMAGE_REPO)
    assert is_managed(ADMIN_IMAGE_REPO)
    assert is_managed(EMS_IMAGE_REPO)
    for foreign in ("influxdb", "ghcr.io/someone/ems-solarflow-admin", "postgres", "", None):
        assert not is_managed(foreign)


def test_foreign_images_are_never_planned_for_removal():
    images = [
        *_series("influxdb", 20),
        *_series("postgres", 20),
        *_series(ADMIN_IMAGE_REPO, 8),
    ]

    removals = plan_removals(images, keep=5)

    assert removals, "the managed repository should still yield candidates"
    foreign = [d for d in removals if d.startswith("sha256:xdb") or d.startswith("sha256:res")]
    assert foreign == []


def test_a_lookalike_repository_is_not_ours():
    """Only the exact repository counts; a similar name is somebody else's."""

    images = _series("ghcr.io/attacker/ems-solarflow-admin", 30)

    assert plan_removals(images, keep=1) == []


def test_the_newest_five_of_each_repository_are_kept():
    images = [*_series(ADMIN_IMAGE_REPO, 8), *_series(EMS_IMAGE_REPO, 8)]

    removals = plan_removals(images, keep=5)

    # 8 per repository, 5 kept -> the 3 oldest of each are removable.
    assert len(removals) == 6
    kept_admin = [d for d in removals if ADMIN_IMAGE_REPO[-3:] in d]
    assert len(kept_admin) == 3


def test_an_image_without_a_readable_digest_is_left_alone():
    """Removal names a digest; an image that cannot state one is not touched."""

    images = [*_series(ADMIN_IMAGE_REPO, 6), _image(ADMIN_IMAGE_REPO, "", "2026-01-01T00:00:00Z")]

    removals = plan_removals(images, keep=1)

    assert "" not in removals


def test_a_protected_digest_is_never_removed_however_old_it_is():
    """The rollback target is the build that ran before, not the newest one.

    A downgrade makes the previous build an old one, so ordering alone would
    evict exactly the image the way back depends on.
    """

    images = _series(ADMIN_IMAGE_REPO, 10)
    oldest = images[0]["digest"]

    removals = plan_removals(images, protected_digests=[oldest], keep=2)

    assert oldest not in removals


def test_an_image_is_protected_by_any_of_its_identities():
    """A repo digest and a local image ID are not the same string everywhere.

    Under the containerd snapshotter they coincide; under the classic overlay2
    store they do not. Protection is recorded as the repo digest while removal
    names the local ID, so matching only one of them would delete a rollback
    target on exactly the hosts where the two differ.
    """

    images = [
        *_series(ADMIN_IMAGE_REPO, 9),
        {
            "repository": ADMIN_IMAGE_REPO,
            "digest": "sha256:localid",
            "aliases": ["sha256:repodigest"],
            "created": "2026-01-01T00:00:00Z",
        },
    ]

    removals = plan_removals(images, protected_digests=["sha256:repodigest"], keep=5)

    assert "sha256:localid" not in removals


def test_a_protected_image_occupies_one_of_the_kept_slots():
    """Protection is not extra headroom.

    Otherwise a pinned old build would silently raise the real budget above the
    number the operator configured.
    """

    images = _series(ADMIN_IMAGE_REPO, 10)
    oldest = images[0]["digest"]

    removals = plan_removals(images, protected_digests=[oldest], keep=5)

    # 10 images, budget 5, one of which the protected old image consumes.
    assert len(removals) == 5
    assert oldest not in removals


def test_the_oldest_removable_image_is_offered_first():
    """A partial run should give up the least valuable images first."""

    images = _series(ADMIN_IMAGE_REPO, 9)

    removals = plan_removals(images, keep=5)

    assert removals == [image["digest"] for image in images[:4]]


def test_one_digest_under_several_tags_counts_once():
    """``latest`` and ``v0.8.8`` on one image are one version, not two."""

    shared = "sha256:aaa"
    images = [
        {"repository": ADMIN_IMAGE_REPO, "digest": shared, "created": "2026-01-09T00:00:00Z"},
        {"repository": ADMIN_IMAGE_REPO, "digest": shared, "created": "2026-01-09T00:00:00Z"},
        *_series(ADMIN_IMAGE_REPO, 5),
    ]

    removals = plan_removals(images, keep=5)

    assert removals.count(shared) == 0
    assert len(removals) == 1


def test_nothing_is_removed_while_the_budget_is_not_exhausted():
    images = _series(ADMIN_IMAGE_REPO, DEFAULT_KEEP)

    assert plan_removals(images) == []


def test_malformed_entries_do_not_break_the_plan():
    images = [None, "nonsense", {}, *_series(ADMIN_IMAGE_REPO, 7)]

    removals = plan_removals(images, keep=5)

    assert len(removals) == 2
