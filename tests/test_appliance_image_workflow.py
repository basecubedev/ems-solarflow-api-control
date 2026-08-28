# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the CI image build stays a build, and stays complete.

Two failures this guards against, both silent. A board added to
``rpi_image_gen.HARDWARE_PROFILES`` that the workflow cannot be asked to build
would leave a release short one artefact with every job green. And a workflow
that grew a signing step would be claiming something a hosted runner may not
claim: release policy approves exactly one builder image, and this is not it.

Publishing is a different question from signing, and this file used to answer
both with one ban. A build nobody can download is not more trustworthy for being
unreachable -- ``docs/user/appliance/install.md`` sends an operator to the
Releases page for these exact file names, and an Actions artefact expires in
thirty days and needs a GitHub account with access to this repository. So the
line moved to where it belongs: the build is published, plainly labelled as
unsigned, and nothing here signs, reads a secret, or lets a build whose gates
failed reach a download page.

Where the release lands is itself load-bearing rather than cosmetic, which is
why it is asserted here. ``packaging/appliance/config/appliance.conf`` points
every flashed appliance at ``/releases/latest/download/manager-packages.json``,
and GitHub resolves that alias to the newest release that is neither draft nor
prerelease. An unsigned image build that took the alias would move it off a
working index the day a signed release exists, and the fleet would read that as
a network fault.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance import rpi_image_gen  # noqa: E402

pytestmark = [pytest.mark.contract, pytest.mark.appliance]

WORKFLOW = ROOT / ".github" / "workflows" / "appliance-image.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def text():
    return WORKFLOW.read_text(encoding="utf-8")


def steps(workflow, job):
    return workflow["jobs"][job]["steps"]


def run_blocks(workflow, job):
    return "\n".join(step.get("run", "") for step in steps(workflow, job))


def test_every_board_this_project_builds_can_be_asked_for(workflow):
    """The one table decides the board list, here as everywhere else."""

    options = workflow["on"]["workflow_dispatch"]["inputs"]["profile"]["options"]

    assert set(options) == {"all", *rpi_image_gen.HARDWARE_PROFILES}


def test_the_default_builds_every_board(workflow):
    """A release is three images; the default may not quietly be one."""

    assert workflow["on"]["workflow_dispatch"]["inputs"]["profile"]["default"] == "all"


def test_the_board_list_is_derived_and_not_typed_a_second_time(workflow):
    """The matrix comes from the plan job, which reads HARDWARE_PROFILES."""

    assert "HARDWARE_PROFILES" in run_blocks(workflow, "plan")
    assert workflow["jobs"]["image"]["strategy"]["matrix"]["profile"] == (
        "${{ fromJSON(needs.plan.outputs.profiles) }}"
    )


def test_the_build_runs_through_the_gate_runner(workflow):
    """The bare build script answers less: source authority, image inspection
    and the source bundle are gates, and their verdict is one verdict."""

    blocks = run_blocks(workflow, "image")

    assert "scripts/appliance-release-gates.sh" in blocks
    assert "--mode builder" in blocks


def test_a_failed_gate_fails_the_job(workflow):
    """A pipeline hides the gate runner's exit status behind tee."""

    blocks = run_blocks(workflow, "image")

    assert 'status="${PIPESTATUS[0]}"' in blocks
    assert 'exit "${status}"' in blocks
    for job in workflow["jobs"].values():
        assert not job.get("continue-on-error")
        for step in job.get("steps", []):
            assert not step.get("continue-on-error")


def test_the_pinned_generator_is_fetched_rather_than_trusted(workflow):
    blocks = run_blocks(workflow, "image")

    assert "scripts/appliance-fetch-rpi-image-gen.sh" in blocks
    assert "scripts/appliance-check-rpi-image-gen.sh" in blocks


def test_the_builder_environment_is_recorded(workflow):
    """Without it the build authority records an empty environment, and the
    artefact could never be signed for a reason nobody wrote down."""

    blocks = run_blocks(workflow, "image")

    assert "appliance-capture-builder-environment.sh" in blocks
    assert "--builder-environment" in blocks


def test_the_workflow_does_not_claim_a_base_image_it_does_not_have(workflow):
    """A hosted runner boots no approved builder image. Passing a digest for
    something that is not one would make the refusal at signing time a lie about
    which check failed.

    Asked of every job, not only the one that builds: the shortcut this forbids
    is just as available to a job that assembles a download page.
    """

    everywhere = "\n".join(run_blocks(workflow, job) for job in workflow["jobs"])
    passed = [
        line.strip()
        for line in everywhere.splitlines()
        if line.strip().startswith("--base-image-sha512")
    ]

    assert passed == [], passed


def test_nothing_here_signs_anything(text):
    """An unsigned build is the honest output of an unapproved builder.

    This half of the old ban stays absolute. Publishing moved; signing did not,
    and a signing step here would have to come with a key, which is why reading
    a repository secret is refused in the same breath. The automatic token is
    ``github.token`` and is not a secret this workflow was given.
    """

    for forbidden in ("gpg ", "--detach-sign", "minisign", "cosign", "secrets."):
        assert forbidden not in text, forbidden


def test_only_the_publish_job_may_write(workflow):
    """Least privilege, and the shape ``test_docker_feature_publish_workflow``
    already uses: every job states its permissions, and exactly one of them is
    the one that writes."""

    permissions = {name: job.get("permissions") for name, job in workflow["jobs"].items()}
    writers = {name for name, perms in permissions.items() if perms != {"contents": "read"}}

    assert writers == {"publish"}, permissions
    assert permissions["publish"] == {"contents": "write"}
    assert workflow["permissions"] == {"contents": "read"}, "the default must stay read-only"


def test_the_workflow_is_dispatch_only(workflow):
    """Three image builds per push would be hours of runner time per commit."""

    assert set(workflow["on"]) == {"workflow_dispatch"}


def test_the_uploaded_image_is_the_compressed_one(workflow):
    """The raw .img is 8.25 GiB and mostly empty. Imager and balenaEtcher write
    the .xz straight to a card."""

    uploads = [
        step for step in steps(workflow, "image")
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]

    assert uploads, "the image job uploads nothing"
    paths = "\n".join(step["with"]["path"] for step in uploads)

    assert "*.img.xz" in paths
    assert "*.build-authority.json" in paths
    for line in paths.splitlines():
        assert not line.strip().endswith("*.img"), "the raw image must not be uploaded"


def test_the_evidence_survives_a_failed_build(workflow):
    """A build that failed is exactly when its logs are worth having."""

    for step in steps(workflow, "image"):
        if str(step.get("uses", "")).startswith("actions/upload-artifact"):
            assert "always()" in str(step.get("if", "")), step.get("name")


def test_the_manager_package_is_built_and_offered_on_its_own(workflow, text):
    """A Manager fix during a hardware test is a file copy, not a re-flash."""

    assert "packaging/appliance/build-deb.sh" in run_blocks(workflow, "manager-package")
    assert "appliance-manager-deb" in text


# --- what Manager the image carries ------------------------------------------


def test_the_image_takes_the_package_an_operator_would_be_offered(workflow):
    """Otherwise the image carries a second build of the same source that only
    happens to match, and the two are compared by nothing."""

    blocks = run_blocks(workflow, "image")

    assert "scripts/appliance-fetch-manager-package.py" in blocks
    assert "EMS_APPLIANCE_MANAGER_PACKAGE=" in blocks


def test_the_image_reads_the_index_the_fleet_was_flashed_with(workflow):
    """One index, two readers. An image built against a different index than
    the one its own /etc names would ship a Manager the appliance's update page
    cannot see, let alone go back from."""

    configured = re.search(
        r"^manager_index_url\s*=\s*(\S+)$",
        (ROOT / "packaging" / "appliance" / "config" / "appliance.conf").read_text("utf-8"),
        re.M,
    ).group(1)
    path = configured.split("basecubedev/ems-solarflow-api-control", 1)[1]

    assert path in run_blocks(workflow, "image")


def test_no_published_release_is_a_fallback_and_anything_else_is_a_failure(workflow):
    """Before the first Manager release there is nothing to fetch and the build
    must go on; a package that failed verification is a different matter, and
    treating the two alike is how an unverified package reaches a card."""

    blocks = run_blocks(workflow, "image")

    assert "3) echo \"::notice::no stable Manager release is published yet" in blocks
    assert "*) echo \"::error::the published Manager package could not be verified\"" in blocks


# --- the download page -------------------------------------------------------


def test_the_finished_build_is_published_where_an_operator_was_sent(workflow):
    """docs/user/appliance/install.md already names the Releases page and these
    file names. Until this job existed the page described a download nobody
    produced."""

    assert "publish" in workflow["jobs"]
    blocks = run_blocks(workflow, "publish")

    assert "gh release create" in blocks
    assert "*.img.xz" in blocks
    assert "*.img.xz.sha256" in blocks
    assert "SHA256SUMS" in blocks


def test_gh_is_told_which_repository_to_act_on(workflow):
    """The df defect again, one command further on.

    ``gh`` resolves the repository from ``--repo``, ``GH_REPO`` or the git
    remotes of the working directory, in that order, and never from
    ``GITHUB_REPOSITORY``. The publish job does not check out, so without this
    every call dies with "not a git repository" -- at the end of a three-hour
    build, after the images have been made and downloaded again.
    """

    for step in steps(workflow, "publish"):
        if "gh release" not in str(step.get("run", "")):
            continue
        environment = step.get("env") or {}

        assert "GH_REPO" in environment or "--repo" in step["run"], step.get("name")


def test_a_failed_upload_does_not_take_the_tag_with_it(workflow):
    """Re-running failed jobs keeps ``github.run_number``, so a tag derived from
    it is taken by the wreckage of the previous attempt. A draft holds no git
    tag, so clearing one costs nothing -- and a release already published is a
    different matter and still refused."""

    blocks = run_blocks(workflow, "publish")

    assert "isDraft" in blocks
    assert "gh release delete" in blocks
    assert "already published" in blocks


def test_a_verdict_that_could_not_be_read_is_not_published_blank(workflow):
    """The notes say the verdict was read back from evidence rather than
    asserted. A command substitution's failure is discarded, so an unreadable
    log would put a blank line under that sentence -- which is the one way an
    unsigned build could be read as claiming more than it proved."""

    blocks = run_blocks(workflow, "publish")

    assert 'verdict="$(sed -n' in blocks
    assert '[ -n "${verdict}" ]' in blocks
    assert '[ -r "${gates}/release-gates.log" ]' in blocks


def test_the_release_is_a_prerelease_so_the_fleet_pointer_stays_put(workflow):
    """The load-bearing one.

    ``packaging/appliance/config/appliance.conf`` points every flashed appliance
    at ``/releases/latest/download/manager-packages.json``, and GitHub resolves
    that alias to the newest release that is neither draft nor prerelease. An
    unsigned image build published as an ordinary release takes the alias.
    Nothing breaks while no signed release exists -- and the day one does, the
    next image build moves the pointer off the working index and the Manager
    updater goes quiet across the fleet, as ``release_download_failed``, which
    an operator reads as a network fault.
    """

    blocks = run_blocks(workflow, "publish")

    assert "--prerelease" in blocks
    assert "--latest" not in blocks, "that flag would claim the alias outright"


def test_the_release_is_complete_before_it_is_reachable(workflow):
    """Created as a draft and published once, at the end. Three quarters of a
    gigabyte takes long enough that a release visible while it uploads is a
    release somebody downloads half of."""

    blocks = run_blocks(workflow, "publish")

    assert "--draft" in blocks
    assert "--draft=false" in blocks


def test_the_tag_cannot_be_read_as_a_version(workflow):
    """A cross-product trap. ``admin/releases.py`` lists every non-draft release
    of this repository as an EMS system-build target and decides eligibility by
    parsing the tag, so a semver-shaped tag here would offer operators a build
    whose container images do not exist.

    Run rather than eyeballed: the tag template is fed to the pattern that
    actually decides.
    """

    from admin.releases import VERSION_PATTERN

    templates = [
        str(step["env"]["TAG"])
        for step in steps(workflow, "publish")
        if "TAG" in (step.get("env") or {})
    ]

    assert templates, "the publish job names no tag"
    for template in templates:
        # The run number is the only part that varies, and no value of it may
        # turn the tag into something a version parser accepts.
        for number in ("1", "7", "42", "1000"):
            candidate = template.replace("${{ github.run_number }}", number)

            assert not VERSION_PATTERN.fullmatch(candidate), candidate


def test_only_a_build_whose_gates_passed_is_published(workflow):
    """The image job uploads its evidence even when the gates fail -- that is
    when the logs are worth most -- so a publish job that asked whether an
    artefact exists would publish a failed build. It asks the verdict."""

    condition = str(workflow["jobs"]["publish"]["if"])

    assert "needs.image.result == 'success'" in condition
    assert "needs['manager-package'].result == 'success'" in condition


def test_publishing_can_be_declined_without_editing_the_workflow(workflow):
    """A build made to answer a question about the builder needs no download
    page."""

    inputs = workflow["on"]["workflow_dispatch"]["inputs"]

    assert inputs["publish"]["type"] == "boolean"
    assert inputs["publish"]["default"] is True
    assert "inputs.publish" in str(workflow["jobs"]["publish"]["if"])


def test_the_publish_job_does_not_type_the_board_list_a_second_time(workflow):
    """The failure this file exists to prevent is a release short one board with
    every job green, and an asset list written by hand is exactly that."""

    environments = "\n".join(
        yaml.dump(step.get("env") or {}) for step in steps(workflow, "publish")
    )

    assert "needs.plan.outputs.profiles" in environments
    for board in rpi_image_gen.HARDWARE_PROFILES:
        assert board not in run_blocks(workflow, "publish"), f"{board} is typed by hand"


def test_the_published_image_is_the_compressed_one(workflow):
    """The same rule the Actions upload follows. The raw .img is 8.25 GiB and
    mostly empty, and it must not leave the runner by either route."""

    for line in run_blocks(workflow, "publish").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        assert "*.img " not in stripped, stripped
        assert not stripped.endswith("*.img"), stripped


def test_the_evidence_travels_with_the_download(workflow):
    """What was proven about an image is not a separate document from it. A
    release carrying the image alone invites the question a week later, when the
    run it came from has expired."""

    blocks = run_blocks(workflow, "publish")

    assert "gate-evidence-" in blocks
    assert "*.build-authority.json" in blocks
    assert "builder-environment" in blocks


def test_the_published_package_can_still_be_signed_later(workflow):
    """``scripts/appliance-build-manager-manifest.py`` refuses without the
    ``.build.json`` beside the package, so publishing the .deb alone would mean
    rebuilding it to sign it -- and a rebuild elsewhere records a different
    builder in the reproducibility block of a document about to be signed."""

    blocks = run_blocks(workflow, "publish")

    assert '"${package}"/*.build.json' in blocks


def test_the_release_says_it_is_not_a_release(workflow):
    """In the project's own words, taken from the gate runner's own verdict
    line. A download page that omitted this would be the one place a reader
    could reasonably conclude the opposite."""

    blocks = run_blocks(workflow, "publish")

    assert "not signable" in blocks
    assert "builder_environment_untrusted" in blocks
    assert "This is not a release" in blocks
