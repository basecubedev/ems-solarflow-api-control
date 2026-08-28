# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the check a pull request is held to sees every job.

Branch protection holds a list of check names, never "all of them". A job that
is not on that list still runs and still reports, but nothing stops a merge
while it is red -- and a job added later is not on any list, so coverage decays
by default rather than by decision.

The workflows answer that with one ``gate`` job per workflow that waits on the
rest, so a single name can be marked required and new jobs are picked up by
adding them to its ``needs``. That last step is the one a person forgets, and
forgetting it is silent in exactly the way the list was: the gate goes green
while the job it never waited for is red.

So the coupling is pinned here instead. What each assertion protects:

``needs`` names every other job
    the reason the gate exists at all.

``if: always()``
    without it a failure upstream *skips* the gate rather than running it, and
    a required check reporting "skipped" blocks the pull request permanently
    instead of reporting the failure underneath it. The pull request is then
    stuck with no red check to explain why.

no other job declares ``needs``
    the gate treats a skipped job as a pass, because ``playwright-webkit`` is
    skipped on every pull request by its own ``if`` and the two config-template
    jobs exclude each other. That is only sound while a skip can come from a
    condition and nothing else: the moment one job waits on another, a skip can
    also mean "the job before me failed", and the gate would wave it through.
"""

import pytest
import yaml

from pathlib import Path

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

GATE = "gate"
PASSING_RESULTS = ("success", "skipped")


def triggers(document):
    """``on:`` is the YAML 1.1 boolean ``True`` unless it was quoted."""

    return document.get("on") or document.get(True) or {}


def gated_workflows():
    """Every workflow a pull request waits on, which is every workflow that has
    to carry a gate."""

    found = []
    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if "pull_request" not in triggers(document):
            continue
        found.append((path, document))
    assert found, "no pull-request workflow was found; the trigger shape changed"
    return found


@pytest.mark.parametrize("path,document", gated_workflows(), ids=lambda v: getattr(v, "name", ""))
def test_every_pull_request_workflow_carries_a_gate(path, document):
    assert GATE in document["jobs"], (
        f"{path.name} runs on pull requests but has no {GATE!r} job, so its jobs "
        "can only be required one name at a time"
    )


@pytest.mark.parametrize("path,document", gated_workflows(), ids=lambda v: getattr(v, "name", ""))
def test_the_gate_waits_on_every_other_job(path, document):
    jobs = document["jobs"]
    others = {name for name in jobs if name != GATE}
    awaited = set(jobs[GATE].get("needs") or ())

    assert others - awaited == set(), (
        f"{path.name}: the gate does not wait on {sorted(others - awaited)}, so a "
        "red run of those jobs still reports a green gate"
    )
    assert awaited - others == set(), (
        f"{path.name}: the gate waits on {sorted(awaited - others)}, which is not a "
        "job in this workflow"
    )


@pytest.mark.parametrize("path,document", gated_workflows(), ids=lambda v: getattr(v, "name", ""))
def test_the_gate_runs_even_when_something_failed(path, document):
    assert document["jobs"][GATE].get("if") == "always()", (
        f"{path.name}: without if: always() the gate is skipped when a job it waits "
        "on fails, and a required check that reports skipped blocks the pull request "
        "with no red check to explain it"
    )


@pytest.mark.parametrize("path,document", gated_workflows(), ids=lambda v: getattr(v, "name", ""))
def test_a_skip_can_only_come_from_a_condition(path, document):
    """The gate counts a skipped job as a pass. That holds only while nothing in
    the workflow can be skipped for having lost its predecessor."""

    chained = {
        name: job.get("needs")
        for name, job in document["jobs"].items()
        if name != GATE and job.get("needs")
    }

    assert chained == {}, (
        f"{path.name}: {sorted(chained)} now wait on other jobs, so a skip can mean "
        "an upstream failure. The gate treats a skip as a pass and would let that "
        "through -- teach it the difference before adding the dependency"
    )


@pytest.mark.parametrize("path,document", gated_workflows(), ids=lambda v: getattr(v, "name", ""))
def test_the_gate_reads_the_results_it_waits_on(path, document):
    """A gate that lists its dependencies but never inspects them is green by
    construction, which looks exactly like a gate that works."""

    steps = document["jobs"][GATE]["steps"]
    scripts = " ".join(step.get("run", "") for step in steps)
    passed_in = " ".join(
        str(value)
        for step in steps
        for value in (step.get("env") or {}).values()
    )

    assert "toJSON(needs)" in passed_in, (
        f"{path.name}: the gate never receives the results of the jobs it waits on"
    )
    for result in PASSING_RESULTS:
        assert result in scripts, (
            f"{path.name}: the gate does not name {result!r}, so it cannot be "
            "distinguishing outcomes the way the workflow comment claims"
        )
