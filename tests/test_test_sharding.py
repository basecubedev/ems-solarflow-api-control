# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``pytest --shard K/N`` splits a selection into whole files, each run exactly once.

The Docker job runs its files in parallel runners this way (tests/shards.py);
a file split across runners would set its module fixtures up twice, and a file
no shard took would never run.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.shards import WEIGHTS_FILE, assign, load_weights, parse_shard, select

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[1]


def _items(*nodeids):
    return [SimpleNamespace(nodeid=nodeid) for nodeid in nodeids]


@pytest.mark.parametrize(("value", "shard"), [(None, None), ("", None), ("1/1", (1, 1)), ("3/4", (3, 4))])
def test_a_shard_is_read_as_k_of_n(value, shard):
    assert parse_shard(value) == shard


@pytest.mark.parametrize("value", ["0/4", "5/4", "2", "a/b", "1/0"])
def test_a_shard_that_names_none_is_refused(value):
    with pytest.raises(ValueError):
        parse_shard(value)


def test_every_file_lands_in_exactly_one_shard_and_stays_whole():
    items = _items(
        "tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one",
        "tests/test_c.py::one", "tests/test_d.py::one", "tests/test_d.py::two",
    )
    weights = {"tests/test_a.py": 50, "tests/test_b.py": 10, "tests/test_c.py": 30}

    shards = [select(items, (index, 3), weights)[0] for index in (1, 2, 3)]

    assert sorted(item.nodeid for shard in shards for item in shard) == sorted(item.nodeid for item in items)
    owners = {}
    for index, shard in enumerate(shards):
        for item in shard:
            owners.setdefault(item.nodeid.split("::")[0], set()).add(index)
    assert all(len(found) == 1 for found in owners.values())


def test_files_are_dealt_out_longest_first_onto_the_least_loaded_shard():
    weights = {"a": 8, "b": 7, "c": 6, "d": 5, "e": 4}

    owner = assign(list(weights), weights, 2)

    assert owner == {"a": 0, "b": 1, "c": 1, "d": 0, "e": 0}
    assert assign(list(reversed(list(weights))), weights, 2) == owner


def test_a_file_without_a_measured_time_counts_as_the_median():
    owner = assign(["known_long", "known_short", "known_mid", "new"], {"known_long": 9, "known_short": 1, "known_mid": 5}, 2)

    assert owner == {"known_long": 0, "known_mid": 1, "new": 1, "known_short": 0}


def test_every_file_the_weights_name_exists():
    weights = load_weights(WEIGHTS_FILE)

    assert weights
    assert [name for name in weights if not (ROOT / name).is_file()] == []


def test_the_docker_job_runs_every_shard_it_names():
    import re

    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "simulated-regression-tests.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["docker-first-e2e"]
    shards = job["strategy"]["matrix"]["shard"]
    runs = [step["run"] for step in job["steps"] if re.search(r"^\s*pytest ", step.get("run", ""), flags=re.MULTILINE)]
    counts = [re.findall(r"--shard \$\{\{ matrix\.shard \}\}/(\d+)", run) for run in runs]

    assert runs, "the job runs no pytest"
    assert all(len(found) == 1 for found in counts), "a pytest run of the job takes no shard, or more than one"
    assert {int(found[0]) for found in counts} == {len(shards)}
    assert shards == list(range(1, len(shards) + 1))
    assert job["strategy"]["fail-fast"] is False


def _passed(*args):
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}
    files = ["tests/test_ci_gate_covers_every_job.py", "tests/test_dependency_refresh_workflow.py", "tests/test_ci_base_image_prepull.py"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q", *files, *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    return sorted(re.findall(r"^PASSED (\S+)", result.stdout, flags=re.MULTILINE))


@pytest.mark.slow
def test_a_shard_runs_the_same_tests_in_one_process_and_on_workers():
    """xdist workers report their collection in the same hook that cuts the shard; the cut has to come first."""

    alone = [_passed("-p", "no:xdist", "--shard", f"{index}/2") for index in (1, 2)]
    workers = [_passed("-n", "2", "--shard", f"{index}/2") for index in (1, 2)]

    assert all(alone)
    assert workers == alone
    assert not set(alone[0]) & set(alone[1])
