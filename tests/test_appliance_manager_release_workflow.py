# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the one workflow in this repository that holds a key.

An image built on a hosted runner is refused at signing time by design. The
Manager package is not: ``build-deb.sh`` is reproducible from
``SOURCE_DATE_EPOCH`` and a pinned compressor, so two builds of one commit are
the same bytes and an unattested builder is no objection to it. That asymmetry
is why this workflow may sign and ``appliance-image.yml`` may not, and why the
rules here are about key custody rather than about builder trust.

Three of them are load-bearing. No job may hold both the signing key and
permission to write to the repository. A signature the shipped keyring would
refuse must fail on a runner rather than on every appliance that fetched it.
And the index tag must stay exactly what ``appliance.conf`` names, because a
flashed appliance never gets that value corrected -- ``ems-appliance-config-seed``
creates a missing file and leaves an existing one unread, and it is not a dpkg
conffile either.
"""

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "appliance-manager-release.yml"
SHIPPED_CONFIG = ROOT / "packaging" / "appliance" / "config" / "appliance.conf"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps(workflow, job):
    return workflow["jobs"][job]["steps"]


def run_blocks(workflow, job):
    return "\n".join(step.get("run", "") for step in steps(workflow, job))


def environments(workflow, job):
    return "\n".join(yaml.dump(step.get("env") or {}) for step in steps(workflow, job))


# --- key custody --------------------------------------------------------------


def test_no_job_holds_both_the_key_and_permission_to_write(workflow):
    """The reason this is three jobs and not one.

    An approval gate that released the key into a job which could also write to
    the repository would make the gate the only thing between a compromised step
    and both.
    """

    for name, job in workflow["jobs"].items():
        body = yaml.dump(job)
        holds_key = "secrets." in body
        may_write = (job.get("permissions") or {}).get("contents") == "write"

        assert not (holds_key and may_write), name


def test_only_the_publishing_job_may_write(workflow):
    writers = {
        name for name, job in workflow["jobs"].items()
        if (job.get("permissions") or {}).get("contents") == "write"
    }

    assert writers == {"publish"}
    assert workflow["permissions"] == {"contents": "read"}


def test_the_key_is_released_by_a_human_and_by_nothing_else(workflow):
    """A GitHub Environment is what makes required reviewers possible. Without
    one the secret is readable by any run of this workflow."""

    assert workflow["jobs"]["sign"].get("environment") == "appliance-manager-signing"
    for name, job in workflow["jobs"].items():
        if name != "sign":
            assert "secrets." not in yaml.dump(job), f"{name} reads a secret"


def test_the_key_never_reaches_a_filesystem(workflow):
    """A key written to a runner's disk is a key in a snapshot nobody can
    revoke. It arrives on a pipe, and the keyring it builds is destroyed
    unconditionally -- including when the signature failed."""

    blocks = run_blocks(workflow, "sign")

    assert "base64 -d | gpg" in blocks
    assert "rm -rf" in blocks and "gnupg" in blocks
    teardown = [
        step for step in steps(workflow, "sign")
        if "rm -rf" in str(step.get("run", ""))
    ]

    assert teardown, "nothing destroys the key material"
    for step in teardown:
        assert "always()" in str(step.get("if", "")), step.get("name")


def test_the_named_subkey_is_the_one_that_signs(workflow):
    """Measured, not assumed.

    Without a trailing ``!`` gpg reads a fingerprint as naming a *key* and signs
    with whichever of that key's signing subkeys it prefers -- the newest, not
    the one named. Invisible while there is one subkey; silently the wrong key
    the moment a rotation adds a second, which is when nobody is looking. The
    verification step downstream would catch it, but would report it as the
    fleet refusing the signature rather than as gpg having chosen another key.
    """

    blocks = run_blocks(workflow, "sign")

    assert '--local-user "${FINGERPRINT}!"' in blocks


def test_a_missing_secret_says_what_to_create(workflow):
    """Nothing else in this repository reads a secret, so the first run will be
    the first time anyone finds out whether the environment was set up."""

    blocks = run_blocks(workflow, "sign")

    assert "APPLIANCE_MANAGER_SIGNING_KEY" in blocks
    assert "holds no key" in blocks


# --- the signature the fleet has to accept ------------------------------------


def test_the_signature_is_checked_against_the_keyring_the_fleet_ships(workflow):
    """The whole point of verifying here.

    ``packaging/appliance/config/release-keyring.gpg`` is what the package
    installs at ``/etc/ems-appliance-manager/release-keyring.gpg``, and ``gpgv``
    is the program the appliance runs. Checking with anything else would prove
    that *some* key signed it, which is not the question.
    """

    blocks = run_blocks(workflow, "sign")

    assert "gpgv --keyring" in blocks
    assert "packaging/appliance/config/release-keyring.gpg" in blocks


def test_the_verification_happens_before_anything_is_published(workflow):
    """Ordering, not presence: verifying after the upload would publish a
    package the fleet refuses and then complain about it."""

    names = [str(step.get("name", "")) for step in steps(workflow, "sign")]
    publishing = run_blocks(workflow, "publish")

    assert any("Refuse" in name for name in names)
    assert "gpgv" not in publishing, "the check belongs to the job that signed"


# --- what an appliance will be able to reach ----------------------------------


def index_tag(workflow):
    match = re.search(r"INDEX_TAG:\s*(\S+)", environments(workflow, "publish"))

    assert match, "the publish job names no index tag"
    return match.group(1)


def test_the_index_tag_is_the_one_the_fleet_was_flashed_with(workflow):
    """The coupling that cannot drift.

    Every appliance reads the URL in its own /etc, written once at flash time
    and never revised: ems-appliance-config-seed creates a missing file and
    leaves an existing one unread, and appliance.conf is not a dpkg conffile.
    Renaming this tag does not migrate a fleet, it strands one.
    """

    configured = re.search(
        r"^manager_index_url\s*=\s*(\S+)$",
        SHIPPED_CONFIG.read_text(encoding="utf-8"),
        re.M,
    ).group(1)

    assert f"/releases/download/{index_tag(workflow)}/manager-packages.json" in configured


def test_the_index_carries_history_rather_than_only_the_newest(workflow):
    """Going back to an earlier package is the entire recovery this path
    provides -- there is no A/B slot behind it. An index naming only the newest
    package takes that away from every appliance that kept no local copy."""

    blocks = run_blocks(workflow, "publish")
    commands = [
        line for line in blocks.splitlines() if not line.strip().startswith("#")
    ]

    assert "--previous" in blocks
    for line in commands:
        assert "--keep" not in line, (
            f"a retention cap here silently removes a way back: {line.strip()}"
        )


def test_asset_urls_are_pinned_to_the_release_that_carries_them(workflow):
    """The index stores absolute URLs, so an entry written today has to still
    resolve years later. ``/releases/latest`` moves; a tag does not."""

    blocks = run_blocks(workflow, "publish")

    assert "/releases/download/${TAG}" in blocks
    assert "/releases/latest" not in blocks


def test_a_published_version_is_never_rewritten(workflow):
    """An index entry names a digest. Replacing the bytes behind a version that
    an appliance may already have installed makes that entry a lie."""

    assert "already exists" in run_blocks(workflow, "publish")


def test_gh_is_told_which_repository_to_act_on(workflow):
    """gh resolves the repository from --repo, GH_REPO or the git remotes of the
    working directory, and never from GITHUB_REPOSITORY."""

    for step in steps(workflow, "publish"):
        if "gh " not in str(step.get("run", "")):
            continue
        environment = step.get("env") or {}

        assert "GH_REPO" in environment or "--repo" in step["run"], step.get("name")


# --- what gets signed ---------------------------------------------------------


def test_the_package_is_built_with_the_version_the_tag_names(workflow):
    """The tag is the version and the source records none, so the one thing
    that can still go wrong is building the package without passing it through:
    the release would be named after the tag while the artefact inside carried a
    development version, and the index would point at an entry nothing can
    satisfy."""

    blocks = run_blocks(workflow, "package")

    assert "version_from_tag" in blocks
    assert "--version" in blocks, (
        "build-deb.sh is invoked without a version, so it would name itself a "
        "development build while the release is named after the tag"
    )
    assert "APPLIANCE_VERSION" not in blocks, (
        "the workflow reads a version literal again; the tag is the only version"
    )


def test_the_document_that_gets_signed_does_not_depend_on_the_runner_locale(workflow):
    """build-deb.sh records dpkg-deb's own --version string in the build record,
    the manifest quotes it, and the manifest is what gets signed. A German
    runner would otherwise sign a different document for identical bytes."""

    assert "LC_ALL=C packaging/appliance/build-deb.sh" in run_blocks(workflow, "package")


def test_the_manifest_is_built_from_the_package_rather_than_typed(workflow):
    blocks = run_blocks(workflow, "package")

    assert "scripts/appliance-build-manager-manifest.py" in blocks
    assert "--revision" in blocks


def test_a_candidate_is_refused_here_rather_than_five_steps_later(workflow):
    """This chain publishes releases and nothing else, and it should say so
    where the version is chosen.

    A prerelease is spelled with a tilde, because that is the only form on which
    dpkg and ``version_key`` agree about order, and
    ``artifact_trust.RELEASE_ID`` admits no tilde. So a candidate has no
    publishable release id -- which without this check surfaces inside the
    manifest generator as ``invalid_release_id``, a message about the wrong
    thing entirely.
    """

    blocks = run_blocks(workflow, "package")

    assert "is_stable" in blocks
    assert "RELEASE_ID admits no" in blocks


def test_the_release_id_grammar_really_is_what_forbids_it():
    """The premise of the check above, run rather than remembered. If the
    grammar ever admits a tilde, the refusal is over-strict and should go."""

    from appliance.artifact_trust import RELEASE_ID

    assert RELEASE_ID.match("ems-appliance-manager-0.1.0-arm64")
    assert not RELEASE_ID.match("ems-appliance-manager-0.1.0~rc1-arm64")
