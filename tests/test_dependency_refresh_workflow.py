# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for ``.github/workflows/dependency-refresh.yml``.

The workflow pushes what a Dependabot update makes onto the Dependabot branch.
Three lists have to agree for that to be complete: the directories the Vite
configs build into, the directories the browser job compares after its own
build, and the paths the push job accepts. The push job holds a deploy key, so
the tests also hold it to running no action and none of the updated packages'
code, to rewriting the inventory itself, and to staging nothing from the build
but plain regular files in those directories.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-refresh.yml"
BROWSER_WORKFLOW = ROOT / ".github" / "workflows" / "playwright-e2e.yml"
INVENTORY = "THIRD_PARTY_LICENSES.md"
_OUT_DIR = re.compile(r'outDir:\s*resolve\(ROOT,\s*"([^"]+)"\)')
_SECRET = re.compile(r"secrets\.([A-Z0-9_]+)")
_STAGE_STEP = "Stage only the build output"


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(job, name):
    return next(entry for entry in job["steps"] if entry.get("name") == name)


def build_directories():
    found = set()
    for config in sorted((ROOT / "frontend").glob("*/vite.config.js")):
        found.update(_OUT_DIR.findall(config.read_text(encoding="utf-8")))
    return found


def test_the_vite_configs_name_the_three_modern_builds():
    assert build_directories() == {"dashboard/static/modern", "admin/static/modern", "appliance/static/modern"}


def test_the_build_job_collects_every_build_directory():
    run = step(workflow()["jobs"]["build"], "Collect the build output")["run"]
    added = next(line for line in run.splitlines() if "git add" in line).split(" -- ", 1)[1].split()

    assert sorted(added) == sorted(build_directories())


def test_the_push_job_accepts_exactly_the_build_directories():
    run = step(workflow()["jobs"]["push"], _STAGE_STEP)["run"]
    case = next(line for line in run.splitlines() if "static/modern/*" in line)
    accepted = [pattern.strip().removesuffix("/*") for pattern in case.strip().rstrip(") ;").split("|")]

    assert sorted(accepted) == sorted(build_directories())


def test_the_push_job_writes_the_inventory_itself_with_the_isolated_checker():
    run = step(workflow()["jobs"]["push"], "Rewrite the inventory's Version cells")["run"]

    assert "python3 -I tools/check_third_party_licenses.py --fix-versions" in run
    assert "git add -- THIRD_PARTY_LICENSES.md" in run


def test_the_browser_job_compares_every_build_directory():
    steps = yaml.safe_load(BROWSER_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["playwright-dashboard"]["steps"]
    run = step({"steps": steps}, "Check the Modern builds")["run"]
    compared = next(line for line in run.splitlines() if "git diff --exit-code" in line).split(" -- ", 1)[1].split()

    assert "npm run build:modern" in run
    assert sorted(compared) == sorted(build_directories())


def test_build_modern_runs_every_modern_build_once():
    scripts = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]
    builds = sorted(name for name in scripts if re.fullmatch(r"build:[a-z]+-modern", name))

    assert builds == ["build:admin-modern", "build:appliance-modern", "build:dashboard-modern"]
    assert sorted(re.findall(r"npm run (\S+)", scripts["build:modern"])) == builds
    assert "npm run build:modern" in step(workflow()["jobs"]["build"], "Build the Modern front ends")["run"]


def test_the_build_job_holds_no_secret_and_no_write_permission():
    job = workflow()["jobs"]["build"]

    assert job["permissions"] == {"contents": "read"}
    assert not _SECRET.search(json.dumps(job))
    assert step(job, "Check out the Dependabot commit")["with"]["persist-credentials"] is False


def test_the_push_job_runs_no_action_and_none_of_the_updated_code():
    job = workflow()["jobs"]["push"]
    runs = "\n".join(entry.get("run", "") for entry in job["steps"])
    interpreters = re.findall(r"\b(?:npm|npx|node|pip|python3?)\b.*", runs)

    assert job["permissions"] == {"actions": "read", "contents": "read"}
    assert [entry["name"] for entry in job["steps"] if "uses" in entry] == []
    assert interpreters == ["python3 -I tools/check_third_party_licenses.py --fix-versions; then"]


def test_only_a_dependabot_pull_request_starts_the_refresh():
    document = workflow()
    build = document["jobs"]["build"]

    assert document["permissions"] == {}
    assert "${{ github.actor }}" in document["concurrency"]["group"]
    assert "github.actor == 'dependabot[bot]'" in build["if"]
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in build["if"]
    assert "head.repo.full_name == github.repository" in build["if"]
    assert document["jobs"]["push"]["needs"] == "build"


def test_the_push_never_overwrites_a_newer_dependabot_commit():
    push = step(workflow()["jobs"]["push"], "Commit and push onto the Dependabot branch")["run"]
    command = next(line for line in push.splitlines() if "git push" in line)

    assert "--force" not in command and '"+' not in command and " +" not in command
    assert "dependabot/*)" in push


def test_the_secret_the_workflow_reads_is_the_one_the_documentation_names():
    push = workflow()["jobs"]["push"]
    secrets = set(_SECRET.findall(WORKFLOW.read_text(encoding="utf-8")))
    readers = [entry["name"] for entry in push["steps"] if _SECRET.search(json.dumps(entry))]
    docs = (ROOT / "docs" / "developer" / "ci-release.md").read_text(encoding="utf-8")

    assert secrets == {"DEPENDENCY_REFRESH_KEY"}
    assert readers == ["Check out the Dependabot commit with the deploy key"]
    assert "`DEPENDENCY_REFRESH_KEY`" in docs


# --- the push job's path guard, run against real patches ---------------------


def git(cwd, *arguments):
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def checkout(tmp_path):
    """A repository with a build file, the inventory and a file the refresh must never touch."""

    origin = tmp_path / "origin"
    (origin / "admin" / "static" / "modern").mkdir(parents=True)
    (origin / "admin" / "static" / "modern" / "admin.js").write_text("old\n", encoding="utf-8")
    (origin / INVENTORY).write_text("inventory\n", encoding="utf-8")
    (origin / "README.md").write_text("readme\n", encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main", str(origin))
    git(origin, "add", "--all")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
    git(tmp_path, "clone", "-q", str(origin), "work")
    git(tmp_path, "clone", "-q", str(origin), "target")
    return tmp_path


def make_patch(root, change):
    work = root / "work"
    change(work)
    git(work, "add", "--all")
    patch = root / "dependency-refresh.patch"
    patch.write_text(git(work, "diff", "--cached", "--binary", "--full-index"), encoding="utf-8")
    return patch


def stage(root, patch):
    run = step(workflow()["jobs"]["push"], _STAGE_STEP)["run"]
    return subprocess.run(
        ["bash", "-c", run],
        cwd=root / "target",
        env={"PATCH": str(patch), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_guard_stages_a_changed_and_a_new_build_file(checkout):
    def change(work):
        (work / "admin" / "static" / "modern" / "admin.js").write_text("new\n", encoding="utf-8")
        (work / "admin" / "static" / "modern" / "chunk.js").write_text("chunk\n", encoding="utf-8")

    result = stage(checkout, make_patch(checkout, change))

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(checkout / "target", "diff", "--cached", "--name-only").split() == [
        "admin/static/modern/admin.js",
        "admin/static/modern/chunk.js",
    ]


def test_the_guard_stages_nothing_when_the_build_changed_nothing(checkout):
    result = stage(checkout, checkout / "absent.patch")

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(checkout / "target", "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize(
    "outside",
    [INVENTORY, "README.md"],
)
def test_the_guard_refuses_a_file_outside_the_build_output(checkout, outside):
    def change(work):
        (work / "admin" / "static" / "modern" / "admin.js").write_text("new\n", encoding="utf-8")
        (work / outside).write_text("changed by a package\n", encoding="utf-8")

    result = stage(checkout, make_patch(checkout, change))

    assert result.returncode != 0
    assert outside in result.stdout


@pytest.mark.parametrize("name", [".gitattributes", "assets/.hidden.js", "odd name.js"])
def test_the_guard_refuses_a_name_no_build_writes(checkout, name):
    def change(work):
        target = work / "admin" / "static" / "modern" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("planted\n", encoding="utf-8")

    result = stage(checkout, make_patch(checkout, change))

    assert result.returncode != 0
    assert "no plain build file name" in result.stdout


@pytest.mark.parametrize("mode", ["symlink", "executable"])
def test_the_guard_refuses_anything_but_a_regular_file(checkout, mode):
    def change(work):
        target = work / "admin" / "static" / "modern" / "planted"
        if mode == "symlink":
            target.symlink_to("../../../README.md")
        else:
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)

    result = stage(checkout, make_patch(checkout, change))

    assert result.returncode != 0
    assert "file mode" in result.stdout
