# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: only the EMS release family may look like a version.

``admin/releases.py`` offers the operator every non-draft release of this
repository as an EMS system build, and decides which ones by
``VERSION_PATTERN.fullmatch(tag)`` -- there is no product marker on a release,
so the tag's *shape* is the only discriminator there is. A tag from the
appliance side that parsed as a version would put an OS image in the console's
list of installable EMS builds, and choosing it means pulling container images
that were never published.

The appliance therefore tags outside that shape: ``appliance-manager-v<x>``,
``appliance-image-v<x>``, ``appliance-image-ci-<n>``, ``appliance-manager-index``.
The ``appliance-`` prefix is what does the work, not the ``v`` -- the pattern is
anchored at both ends, so anything before the digits fails it at the first
character.

That is a convention, and a convention holds until someone adds a workflow. So
the patterns are read back out of the workflows and instantiated with values
chosen to break them, rather than being trusted to keep their shape.

The right long-term fix is a product marker on the release or a tag-to-product
allowlist in ``releases.py``, since a shape test cannot tell an appliance tag
from a third product nobody has written yet. Until then this is the guard.
"""

import pytest
import yaml

from pathlib import Path

from admin.releases import VERSION_PATTERN

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

EMS_FAMILY = "v*"

# What a ``*`` is asked to stand for. Every one of these is a string somebody
# could plausibly tag, and each is chosen to make the pattern match if the
# prefix in front of it were ever dropped.
SUBSTITUTIONS = ("0.1.0", "1.2.3", "1.0.0-rc1", "0.0.1+build.5", "", "v2.0.0")


def triggers(document):
    return document.get("on") or document.get(True) or {}


def tag_patterns():
    found = []
    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        push = triggers(document).get("push") or {}
        for pattern in push.get("tags") or ():
            found.append((path.name, pattern))
    assert found, "no workflow triggers on a tag; the trigger shape changed"
    return found


def instantiate(pattern):
    if "*" not in pattern:
        return [pattern]
    return [pattern.replace("*", value) for value in SUBSTITUTIONS]


@pytest.mark.parametrize("workflow,pattern", tag_patterns())
def test_only_the_ems_family_may_parse_as_a_version(workflow, pattern):
    if pattern == EMS_FAMILY:
        assert VERSION_PATTERN.fullmatch("v1.2.3"), (
            "the EMS release family stopped parsing as a version, which is the "
            "one thing admin/releases.py needs it to do"
        )
        return

    parses = [tag for tag in instantiate(pattern) if VERSION_PATTERN.fullmatch(tag)]
    assert parses == [], (
        f"{workflow} triggers on {pattern!r}, which can produce {parses} -- "
        "admin/releases.py would offer that release as an installable EMS system "
        "build whose container images do not exist"
    )


def test_the_image_workflow_publishes_under_the_appliance_prefix():
    """Both forms of the tag it creates, not just the one a schedule takes."""

    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    steps = document["jobs"]["publish"]["steps"]
    declared = [
        step["env"]["TAG"] for step in steps if "TAG" in (step.get("env") or {})
    ]

    assert declared, "the publish job no longer declares the tag it creates"
    for expression in declared:
        assert "appliance-image-ci-" in expression, (
            "the fallback tag lost the appliance prefix that keeps it out of the "
            "EMS system-build list"
        )
        assert "github.ref_name" in expression, (
            "a tag push no longer publishes under its own name"
        )


def test_a_tag_that_misnames_its_version_is_refused_before_the_build():
    """Three emulated boards take about three hours, and a published tag can
    never be reused -- so the check has to sit in the first job, not the last."""

    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    plan = document["jobs"]["plan"]["steps"]
    guards = [
        step for step in plan if "APPLIANCE_VERSION" in step.get("run", "")
    ]

    assert guards, (
        "the plan job no longer compares the tag against appliance/version.py, so "
        "a mistyped tag is only caught after the images are built"
    )
    assert all(step.get("if") == "github.ref_type == 'tag'" for step in guards), (
        "the version guard runs on more than tag pushes, where there is no tag to "
        "compare and it would fail every scheduled build"
    )


def test_both_release_titles_open_with_the_words_the_install_guide_names():
    """``docs/user/appliance/install.md`` sends an operator to the Releases page
    and tells them to open the newest entry *whose title starts with "Appliance
    image"*, because that page lists EMS releases too and those carry no image.

    A tagged build and a weekly one are titled differently. Both are on that
    page, so both have to answer to the same instruction.
    """

    guide = (ROOT / "docs" / "user" / "appliance" / "install.md").read_text(encoding="utf-8")
    assert 'title starts with "Appliance image"' in guide, (
        "the install guide no longer identifies a release by its title; this test "
        "is pinning a contract that moved"
    )

    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    scripts = " ".join(
        step.get("run", "") for step in document["jobs"]["publish"]["steps"]
    )
    titles = [
        line.split("=", 1)[1].strip().strip('"')
        for line in scripts.splitlines()
        if line.strip().startswith("title=")
    ]

    assert len(titles) == 2, (
        f"expected a title for a tagged build and one for a weekly build, found {titles}"
    )
    for title in titles:
        assert title.startswith("Appliance image "), (
            f"{title!r} does not open with the words the install guide names"
        )
        assert "${APPLIANCE_VERSION}" in title, (
            f"{title!r} does not say which appliance version it carries"
        )
