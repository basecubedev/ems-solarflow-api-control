# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the release catalogue follows the GitHub Release.

The catalogue every installation reads for its upgrade list is built from the
repository's GitHub Releases, not from its tags. A tag run therefore cannot
put its own release into the file unless the Release object already exists,
and on 2026-09-16 it did not: v0.8.6 was tagged, its images were published,
the catalogue was rebuilt without it, and the Release was created twenty
minutes later with nothing left to notice. The run was green, because its
verification only read back the file it had just written.

Two things are pinned against that. The catalogue job runs on its own when a
Release is published, edited or deleted, so the order of tag and Release no
longer matters; and a run is reported done only once the file lists the
release it was started for, so a missing Release is a red job with a hint
instead of a green one with a stale file.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CATALOGUE_WORKFLOW = WORKFLOWS / "release-catalogue.yml"
PUBLISH_WORKFLOW = WORKFLOWS / "docker-publish.yml"

RELEASE_EVENTS = {"published", "edited", "deleted", "prereleased", "released", "unpublished"}


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document):
    """``on:`` is the YAML 1.1 boolean ``True`` unless it was quoted."""

    return document.get("on") or document.get(True) or {}


def _jobs():
    return _load(CATALOGUE_WORKFLOW)["jobs"]


def _publish_job():
    return _jobs()["publish-release-catalogue"]


def _verify_job():
    return _jobs()["verify-release-catalogue"]


def _step(job, name):
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def test_the_workflow_and_its_jobs_carry_the_product_name():
    assert _load(CATALOGUE_WORKFLOW)["name"] == "EMS release catalogue"
    assert _publish_job()["name"] == "EMS release catalogue publish"
    assert _verify_job()["name"] == "EMS release catalogue verify"


def test_the_catalogue_is_rebuilt_whenever_a_release_changes():
    """A Release created after the tag run, edited into another channel or
    deleted must reach the file without anyone pushing something."""

    triggers = _triggers(_load(CATALOGUE_WORKFLOW))

    assert set(triggers["release"]["types"]) >= RELEASE_EVENTS
    assert triggers["workflow_call"]["inputs"]["expect_tag"]["type"] == "string"
    assert "workflow_dispatch" in triggers


def test_the_tag_run_hands_the_catalogue_to_the_same_job():
    """One job, two callers: the steps exist once, in the shared workflow."""

    job = _load(PUBLISH_WORKFLOW)["jobs"]["publish-release-catalogue"]

    assert job["uses"] == "./.github/workflows/release-catalogue.yml"
    assert job["needs"] == ["publish-ghcr"]
    assert "steps" not in job
    assert "release_catalogue.py" not in PUBLISH_WORKFLOW.read_text(encoding="utf-8")


def test_the_catalogue_jobs_kept_their_steps():
    publish = " ".join(str(step.get("run", "")) for step in _publish_job()["steps"])
    assert "scripts/release_catalogue.py build --output release-catalogue.json" in publish
    envs = [step.get("env", {}) for step in _publish_job()["steps"]]
    assert any(env.get("GITHUB_TOKEN") == "${{ secrets.GITHUB_TOKEN }}" for env in envs)
    assert any(env.get("CATALOGUE_BRANCH") == "development-build-catalogue" for env in envs)

    verify = " ".join(str(step.get("run", "")) for step in _verify_job()["steps"])
    assert "scripts/release_catalogue.py verify" in verify
    assert "development-build-catalogue/release-catalogue.json" in verify


def test_the_catalogue_is_written_from_the_default_branch():
    """A Release edited on an old tag must not check out that tag's tree."""

    checkouts = [
        step
        for job in _jobs().values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert len(checkouts) == 2
    for checkout in checkouts:
        assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"


def test_catalogue_rebuilds_never_run_side_by_side():
    assert _publish_job()["concurrency"] == "release-catalogue"


def test_waiting_for_a_release_never_holds_the_publish_group():
    """The verify may wait minutes -- for the CDN, or for a Release that is
    not there yet. In the job that holds the group it would hold back the
    very rebuild that Release triggers, so it is a job of its own, fed the
    newest tag as an output instead of sharing the file."""

    verify = _verify_job()

    assert verify["needs"] == ["publish-release-catalogue"]
    assert "concurrency" not in verify
    assert _publish_job()["outputs"]["newest_tag"] == "${{ steps.build.outputs.newest_tag }}"
    build = _step(_publish_job(), "Build release catalogue")
    assert build["id"] == "build"
    assert 'echo "newest_tag=${newest}" >> "$GITHUB_OUTPUT"' in build["run"]


def test_the_verify_step_is_told_what_the_run_was_started_for():
    """The expected tag comes through ``env``, never interpolated into the
    script, so a tag name is data to the shell and not code."""

    step = _step(_verify_job(), "Verify published release catalogue")

    assert step["env"]["EXPECT_TAG"] == "${{ inputs.expect_tag }}"
    assert step["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert step["env"]["EVENT_ACTION"] == "${{ github.event.action }}"
    assert step["env"]["RELEASE_TAG"] == "${{ github.event.release.tag_name }}"
    assert step["env"]["RELEASE_DRAFT"] == "${{ github.event.release.draft }}"
    assert step["env"]["NEWEST_TAG"] == "${{ needs.publish-release-catalogue.outputs.newest_tag }}"
    assert "${{" not in step["run"]


# --- the verify script, run against a stub ---------------------------------

STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
[[ "$*" == *" verify "*"--expect-tag $STUB_LISTED" ]]
"""


def _run_verify(tmp_path, *, listed, newest="v0.8.5", **env):
    """Run the verify step's script with ``python3`` replaced by a stub whose
    catalogue lists exactly one tag; ``newest`` is what the publish job wrote."""

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to run the workflow script")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    environment = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_LOG": str(log),
        "STUB_LISTED": listed,
        "NEWEST_TAG": newest,
        "GITHUB_REPOSITORY": "example/repo",
        "EXPECT_TAG": "",
        "EVENT_NAME": "push",
        "EVENT_ACTION": "",
        "RELEASE_TAG": "",
        "RELEASE_DRAFT": "false",
    }
    environment.update(env)
    result = subprocess.run(
        [bash, "-c", _step(_verify_job(), "Verify published release catalogue")["run"]],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    verify_calls = [line for line in log.read_text(encoding="utf-8").splitlines() if " verify " in line]
    return result, verify_calls


def test_a_tag_run_expects_the_tag_it_published(tmp_path):
    result, calls = _run_verify(tmp_path, listed="v0.8.6", EXPECT_TAG="v0.8.6")

    assert result.returncode == 0, result.stderr
    assert calls and calls[0].endswith("--expect-tag v0.8.6")


def test_a_tag_run_without_its_release_is_red_and_says_why(tmp_path):
    result, calls = _run_verify(tmp_path, listed="v0.8.5", EXPECT_TAG="v0.8.6")

    assert result.returncode == 1
    assert calls and calls[0].endswith("--expect-tag v0.8.6")
    assert "GitHub Release" in result.stdout
    assert "v0.8.6" in result.stdout


def test_a_published_release_expects_itself(tmp_path):
    result, calls = _run_verify(
        tmp_path, listed="v0.8.6", EVENT_NAME="release", EVENT_ACTION="published", RELEASE_TAG="v0.8.6"
    )

    assert result.returncode == 0, result.stderr
    assert calls and calls[0].endswith("--expect-tag v0.8.6")


@pytest.mark.parametrize("action", ["deleted", "unpublished"])
def test_a_removed_release_falls_back_to_the_newest_entry_written(tmp_path, action):
    """The removed tag cannot be expected; the round trip on what was written
    is what is left to check."""

    result, calls = _run_verify(
        tmp_path, listed="v0.8.5", newest="v0.8.5", EVENT_NAME="release", EVENT_ACTION=action, RELEASE_TAG="v0.8.6"
    )

    assert result.returncode == 0, result.stderr
    assert calls and calls[0].endswith("--expect-tag v0.8.5")


def test_an_edited_draft_is_not_expected_in_the_file(tmp_path):
    """``edited`` fires for drafts too, and the catalogue skips drafts on
    purpose; expecting the draft's tag would turn a correct file red."""

    result, calls = _run_verify(
        tmp_path,
        listed="v0.8.5",
        newest="v0.8.5",
        EVENT_NAME="release",
        EVENT_ACTION="edited",
        RELEASE_TAG="v0.9.0",
        RELEASE_DRAFT="true",
    )

    assert result.returncode == 0, result.stderr
    assert calls and calls[0].endswith("--expect-tag v0.8.5")


def test_a_branch_run_expects_the_newest_entry_it_wrote(tmp_path):
    result, calls = _run_verify(tmp_path, listed="v0.8.5", newest="v0.8.5")

    assert result.returncode == 0, result.stderr
    assert calls and calls[0].endswith("--expect-tag v0.8.5")
