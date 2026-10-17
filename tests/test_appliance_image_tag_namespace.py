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

import re

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


def test_the_image_build_is_told_which_version_the_tag_names():
    """There is nothing to compare a tag against any more -- the source records
    no version -- so what used to be a guard is now a hand-off. If the build is
    not told, it names itself a development build and the release ends up
    pointing at files whose names do not contain the tag's version.
    """

    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    scripts = " ".join(
        step.get("run", "") for step in document["jobs"]["image"]["steps"]
    )

    assert "GITHUB_REF_NAME#appliance-image-v" in scripts, (
        "the image build no longer takes its version from the tag, so a tagged "
        "build would publish files named after a development version"
    )
    assert "export VERSION" in scripts, (
        "the version is derived but never exported, so the build script never "
        "sees it"
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


# --- the fixed download link ------------------------------------------------
#
# The Releases page lists two products and its "Latest" badge names an EMS
# release that carries no image, so finding the newest image meant reading a
# mixed list and knowing which entries to skip. `appliance-image-latest` is one
# address that always answers, and everything below is what keeps it honest.

POINTER_TAG = "appliance-image-latest"
POINTER_LINK = f"/releases/tag/{POINTER_TAG}"


def publish_scripts():
    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    return " ".join(step.get("run", "") for step in document["jobs"]["publish"]["steps"])


def pointer_step():
    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    for step in document["jobs"]["publish"]["steps"]:
        if (step.get("env") or {}).get("POINTER_TAG"):
            return step
    raise AssertionError("no publish step declares POINTER_TAG")


def test_the_fixed_download_tag_keeps_the_appliance_prefix():
    """It is a release like any other to admin/releases.py, so it answers to the
    same rule as every tag on this side."""

    step = pointer_step()

    assert step["env"]["POINTER_TAG"] == POINTER_TAG
    assert not VERSION_PATTERN.fullmatch(POINTER_TAG), (
        f"{POINTER_TAG} parses as a version, so the console would offer the "
        "download signpost as an installable EMS system build"
    )


def test_the_fixed_download_tag_is_created_once_and_then_only_rewritten():
    """The whole value is that a link printed in a document stays true. Deleting
    and re-creating the release would move the tag to a new commit, and a
    release created afresh each week is one whose URL was never fixed at all.

    Same shape as `appliance-manager-index`, for the same reason.
    """

    script = pointer_step()["run"]

    assert 'gh release view "${POINTER_TAG}"' in script, (
        "the step no longer asks whether the pointer already exists, so it "
        "cannot tell creating it from rewriting it"
    )
    assert 'gh release edit "${POINTER_TAG}"' in script, (
        "an existing pointer is no longer edited in place"
    )
    assert 'gh release delete "${POINTER_TAG}"' not in script, (
        "the pointer is deleted somewhere; re-creating it moves the tag, which "
        "is the one thing a fixed address may not do"
    )
    assert "--clobber" in script, "the pointer file is not replaced in place"


def command_containing(script, needle):
    """The one shell command that holds ``needle``, continuation lines included.

    Written after a character window quietly made this file lie: asserting
    ``"--prerelease" in script[edit : edit + 400]`` passed with the flag deleted
    from the edit branch, because the window reached into the `else` branch and
    found the *create* call's flag. A test that reads a neighbouring command is
    not a weaker test, it is a test of nothing.
    """

    lines = script.splitlines()
    start = next(index for index, line in enumerate(lines) if needle in line)
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    return "\n".join(lines[start : end + 1])


def test_the_fixed_download_tag_is_a_prerelease_every_time_it_is_touched():
    """`/releases/latest` skips prereleases, and that alias belongs to EMS.

    Both branches have to say so, and the edit branch is the one that matters:
    it is the only one that runs after the first build, so a pointer created by
    hand -- or flipped in the web UI by someone who read "latest" as a promise
    rather than a badge -- would otherwise stay a full release forever, with
    every later build content to leave it that way.
    """

    script = pointer_step()["run"]

    assert "--prerelease" in command_containing(script, 'gh release create "${POINTER_TAG}"'), (
        "a newly created pointer is not marked prerelease, so it would take the "
        "Latest badge from the EMS release that owns it"
    )
    assert "--prerelease" in command_containing(script, 'gh release edit "${POINTER_TAG}"'), (
        "the edit branch does not re-assert prerelease, so a pointer that is a "
        "full release is never put back -- and that branch is the only one that "
        "runs once the pointer exists"
    )


def test_the_pointer_does_not_answer_the_install_guides_search():
    """The guide's fallback tells an operator to open the newest entry whose
    title starts with "Appliance image". The pointer holds no image, so a title
    that matched would send exactly the reader this exists for to a page with
    nothing to download."""

    script = pointer_step()["run"]
    titles = [
        line.split("=", 1)[1].strip().strip('"')
        for line in script.splitlines()
        if line.strip().startswith("pointer_title=")
    ]

    assert len(titles) == 1, f"expected one pointer title, found {titles}"
    assert not titles[0].startswith("Appliance image"), (
        f"{titles[0]!r} answers the install guide's search and has no image to give"
    )


def test_the_pointer_carries_no_image_of_its_own():
    """A copy here would cost three quarters of a gigabyte a week, lose the
    version out of the file name, and leave a window where half the assets are
    last week's while the checksums beside them are this week's."""

    script = pointer_step()["run"]
    uploads = [
        line for line in script.splitlines()
        if 'gh release upload "${POINTER_TAG}"' in line
    ]

    assert len(uploads) == 1, f"expected one upload to the pointer, found {uploads}"
    assert ".img" not in uploads[0], (
        "the pointer uploads an image; it is meant to name where the images are"
    )


def test_the_pointer_can_only_name_files_that_were_published():
    """Built by reading the release back, not from the names this job used, so a
    build that published nothing cannot advertise three files."""

    script = pointer_step()["run"]

    assert 'gh release view "${TAG}" --json tagName,url,assets' in script, (
        "the pointer no longer reads its file list off the published release"
    )
    assert "publishes no .img.xz" in script, (
        "the pointer no longer refuses to point at a release with no image"
    )


@pytest.mark.parametrize(
    "document",
    ("README.md", "docs/user/appliance/install.md"),
)
def test_the_fixed_link_is_the_one_the_reader_is_given(document):
    text = (ROOT / document).read_text(encoding="utf-8")

    assert POINTER_LINK in text, (
        f"{document} does not give the reader the fixed download link, which "
        "leaves them reading a release list that mixes two products"
    )


def test_the_pointer_step_defines_every_variable_it_reads():
    """A `run:` block is its own shell, and this step is the last one in a build
    that takes half an hour.

    Writing it, I referred to `${url}` — a name the *previous* step had set. It
    parsed, `bash -n` was happy, and every other contract here passed. Under
    `set -u` it would have failed on the final line of the run that produced
    three images, and only there.

    So the rule is stated rather than remembered: a name is either in the step's
    own `env:`, assigned by the step itself, handed over through `$GITHUB_ENV`
    earlier in this job, or provided by the runner.
    """

    document = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "appliance-image.yml").read_text(encoding="utf-8")
    )
    steps = document["jobs"]["publish"]["steps"]
    index = next(
        position
        for position, step in enumerate(steps)
        if (step.get("env") or {}).get("POINTER_TAG")
    )
    script = steps[index]["run"]

    # Everything the runner puts in the environment of every step.
    provided = {"RUNNER_TEMP", "GITHUB_STEP_SUMMARY", "GITHUB_SHA", "GITHUB_REPOSITORY",
                "GITHUB_RUN_NUMBER", "GITHUB_RUN_ID", "GITHUB_REF_NAME", "GITHUB_ENV"}
    # Handed over by an earlier step of this job, which is the one hand-off that
    # does survive between `run:` blocks.
    carried = {
        match.group(1)
        for earlier in steps[:index]
        for match in re.finditer(
            r'echo\s+"([A-Za-z_][A-Za-z0-9_]*)=[^"]*"\s*>>\s*"?\$\{?GITHUB_ENV\}?"?',
            earlier.get("run", ""),
        )
    }
    declared = set(steps[index].get("env") or {})
    assigned = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", script, re.MULTILINE))

    read = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", script))
    read |= set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", script))

    unbound = sorted(read - provided - carried - declared - assigned)

    assert unbound == [], (
        f"{unbound} are read but never set in this step. A variable from the "
        "step above does not survive into this shell, and `set -u` turns that "
        "into a failure on the last line of a thirty-minute build"
    )
