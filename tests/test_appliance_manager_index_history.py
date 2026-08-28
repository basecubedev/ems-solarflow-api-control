# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one list of packages an appliance can reach, and how it could vanish.

Going back to an earlier Manager is the only recovery this project's update path
provides -- there is no second slot behind it, `dpkg` runs and what is installed
is installed. The index at the fixed `appliance-manager-index` tag is the only
place an appliance learns that earlier packages exist, so an index naming one
release takes recovery away from every card that kept no local copy.

The rebuild read the existing index with ``if gh release download … 2>/dev/null``
and treated *any* non-zero exit as "no index yet". A transient 500, a rate limit
or an expired token would therefore omit ``--previous``, build a one-entry index
and ``--clobber`` it over the fleet's whole history, with every step green.
``appliance/release_index.py`` has no floor on entry count and the builder only
warns about entries *retention* dropped, so nothing downstream would have
noticed either.

The step is executed here rather than read, with a stubbed ``gh`` and a stubbed
index builder: what is under test is the decision, not the generator, which has
its own tests.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "appliance-manager-release.yml"


def index_step():
    """The publish job's index rebuild, found by what it does."""

    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    found = [
        step.get("run", "")
        for step in jobs["publish"]["steps"]
        if "appliance-build-manager-index.py" in str(step.get("run", ""))
    ]
    assert len(found) == 1, f"expected one index rebuild step, found {len(found)}"
    return found[0]


def stub_gh(binroot, *, api_status, download_entries=None, download_ok=True):
    """A ``curl`` that reports one status, and a ``gh`` for everything else.

    The step reads the status code rather than gh's error text, so the status is
    what a test has to be able to set.
    """

    payload = json.dumps({"releases": [
        {"n": n} for n in range(download_entries or 0)
    ]})

    curl = binroot / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do case \"$prev\" in -o) out=$a;; esac; prev=$a; done\n"
        "[ -z \"$out\" ] || printf '%s' '{}' > \"$out\"\n"
        f"printf %s {api_status}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    script = f"""#!/bin/sh
case "$1 $2" in
"release download")
    [ {1 if download_ok else 0} -eq 1 ] || {{ echo "gh: download failed" >&2; exit 1; }}
    for a in "$@"; do case "$prev" in --dir) dir=$a;; esac; prev=$a; done
    printf '%s' '{payload}' > "$dir/manager-packages.json"
    exit 0
    ;;
esac
exit 0
"""
    path = binroot / "gh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def stub_builder(repo, *, entries):
    """An index builder that writes a chosen number of entries."""

    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    builder = scripts / "appliance-build-manager-index.py"
    builder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        f"json.dump({{'releases': [{{'n': n}} for n in range({entries})]}}, open(out, 'w'))\n"
        "open(out + '.args', 'w').write(' '.join(sys.argv))\n",
        encoding="utf-8",
    )
    builder.chmod(0o755)
    return builder


def run_step(tmp_path, *, api_status, download_entries=None, download_ok=True, builds=1):
    repo = tmp_path / "repo"
    repo.mkdir()
    binroot = tmp_path / "bin"
    binroot.mkdir()
    runner_temp = tmp_path / "runner"
    (runner_temp / "package").mkdir(parents=True)
    (runner_temp / "package" / "r.manifest.json").write_text("{}", encoding="utf-8")

    stub_gh(binroot, api_status=api_status, download_entries=download_entries,
            download_ok=download_ok)
    stub_builder(repo, entries=builds)

    return subprocess.run(
        ["bash", "-c", index_step()],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            **os.environ,
            "PATH": f"{binroot}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_REPOSITORY": "owner/repo",
            "GH_REPO": "owner/repo",
            "GH_TOKEN": "not-a-real-token",
            "GITHUB_SHA": "a" * 40,
            "TAG": "appliance-manager-v0.2.0",
            "RELEASE_ID": "r",
            "INDEX_TAG": "appliance-manager-index",
        },
    )


def test_the_first_release_creates_the_index(tmp_path):
    """404 is the only answer that means there is nothing to carry forward."""

    result = run_step(tmp_path, api_status=404, builds=1)

    assert result.returncode == 0, result.stderr
    assert "this run creates the first one" in result.stdout


def test_history_is_carried_forward_when_an_index_exists(tmp_path):
    """The whole point of the fixed tag: every earlier package stays reachable."""

    result = run_step(tmp_path, api_status=200, download_entries=3, builds=4)

    assert result.returncode == 0, result.stderr
    assert "carrying forward 3 entries" in result.stdout
    args = (tmp_path / "runner" / "index" / "manager-packages.json.new.args").read_text()
    assert "--previous" in args


@pytest.mark.parametrize("status", [500, 401, 403, "000"])
def test_an_uncertain_answer_refuses_to_publish(tmp_path, status):
    """A transient 500, an expired token, a rate limit or a connection that never
    answered at all used to read as "no index yet", and the next line would
    --clobber the fleet's history with one entry. 000 is curl reporting no
    response, which is the case a status code makes visible and a tool's error
    message does not."""

    result = run_step(tmp_path, api_status=status, builds=1)

    assert result.returncode == 1
    assert "refusing to publish" in result.stdout + result.stderr


def test_an_index_that_exists_but_will_not_download_refuses(tmp_path):
    """Its content is not optional once it is known to exist. The step has to
    stop before the builder runs, or the run reaches an upload holding an index
    built without the history it failed to read."""

    result = run_step(tmp_path, api_status=200, download_ok=False, builds=1)

    assert result.returncode != 0
    assert not (tmp_path / "runner" / "index" / "manager-packages.json.new.args").exists(), (
        "the index was built anyway, without the history it could not read"
    )


def test_an_index_that_lost_entries_is_never_uploaded(tmp_path):
    """The floor, and the only guard that survives a way of losing history
    nobody has thought of yet: this release adds one entry and removes none."""

    result = run_step(tmp_path, api_status=200, download_entries=3, builds=1)

    assert result.returncode == 1
    assert "names 1 releases, the one it replaces named 3" in (
        result.stdout + result.stderr
    )
