# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the CI image build stays a build, and stays complete.

Two failures this guards against, both silent. A board added to
``rpi_image_gen.HARDWARE_PROFILES`` that the workflow cannot be asked to build
would leave a release short one artefact with every job green. And a workflow
that grew a signing step, a publish step or a write permission would be claiming
something a hosted runner may not claim: release policy approves exactly one
builder image, and this is not it.
"""

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
    which check failed."""

    passed = [
        line.strip()
        for line in run_blocks(workflow, "image").splitlines()
        if line.strip().startswith("--base-image-sha512")
    ]

    assert passed == [], passed


def test_nothing_here_signs_or_publishes(text):
    """An unsigned build is the honest output of an unapproved builder."""

    for forbidden in ("gpg ", "--detach-sign", "gh release", "secrets."):
        assert forbidden not in text, forbidden


def test_no_job_asks_for_more_than_read_access(workflow):
    for name, job in workflow["jobs"].items():
        permissions = job.get("permissions")

        assert permissions == {"contents": "read"}, f"{name}: {permissions}"


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
