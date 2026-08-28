# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the development build runs in the cache scope it belongs to.

``docker-feature-publish.yml`` exists to build a branch somebody names, and its
gates install that branch's requirements and run its tests. That is untrusted
code by construction -- the whole point of the workflow is to run code the
default branch has not accepted yet.

An Actions cache is scoped to the **ref of the run**, not to what the run checked
out, and a dispatched run can write its own scope. The workflow used to take the
branch as a dispatch *input*, which made those two independent choices: whoever
started the run picked a ref to run on and a ref to build, and nothing checked
that they matched. Started from ``main`` -- what the run dialog offers first --
every gate executed unmerged code while holding a cache token for the default
branch's scope. The publish procedure did tell operators to select the feature
branch and then name it again as the input, so the safe pairing was the
documented one; it was never the enforced one, and the alert is about what the
workflow permits.

Untrusted code holding that token can write any key it likes. The
``*_CACHE_SCOPE`` names separate a feature build from a release build by
convention, and convention does not bind code that is choosing what to write.
``docker-publish.yml`` restores from that same scope under lockfile-derived keys,
so an unmerged branch could decide what a signed release was built against.

The fix is the trigger, not the caching: the workflow is dispatched *from* the
branch it builds, so the run's ref is that branch and the two choices collapse
into one. A feature branch's scope can read the default branch's cache and write
only its own, which is the isolation GitHub already provides and the input was
stepping around.

The dependency-cache prohibition this module used to carry (no ``cache: pip`` /
``cache: npm`` in a job checking out a chosen revision) is deliberately gone
rather than lost. It existed because those jobs wrote pip and npm entries into
the default branch's scope under keys the release workflow hashes identically.
A run on a feature branch writes its own scope, so the same steps are now
harmless, and forbidding them would be forbidding a thing that is no longer true.

CodeQL reports this as ``actions/cache-poisoning/poisonable-step``, once per
gate step that executes repository code. It reported ten of them and not the
publish job's identical steps, because that job holds ``packages: write`` and
the query excludes privileged jobs -- so the alert list was never the boundary
to fix to. The property is asserted here for every job instead.
"""

import pytest
import yaml

from pathlib import Path

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"
RELEASE = ROOT / ".github" / "workflows" / "docker-publish.yml"

# What a step would have to say to name a revision of its own.
CHECKOUT_REF_KEYS = ("ref", "repository")


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def jobs():
    return workflow()["jobs"]


def checkout_steps(job):
    return [step for step in job.get("steps", ()) if "checkout" in str(step.get("uses", ""))]


def test_the_workflow_still_runs_code_from_the_branch_it_builds():
    """The premise. If the gates stopped executing the branch's own code there
    would be nothing to isolate, and their absence would look like the property
    below holding."""

    scripts = " ".join(
        str(step.get("run", ""))
        for job in jobs().values()
        for step in job.get("steps", ())
    )

    assert "pip install -r requirements.txt" in scripts
    assert "pytest" in scripts
    assert "npm ci" in scripts


def test_the_dispatch_cannot_be_handed_a_revision():
    """The single control. Any input naming a branch, tag or sha puts that code
    back into a run whose ref -- and cache scope -- is wherever the dispatch was
    started from."""

    triggers = workflow()
    triggers = triggers[True] if True in triggers else triggers["on"]

    assert list(triggers) == ["workflow_dispatch"], (
        f"{sorted(triggers)}: a second trigger needs its own answer to which "
        "ref the run lands on"
    )
    assert not triggers["workflow_dispatch"], (
        f"the dispatch declares {triggers['workflow_dispatch']}; a revision "
        "input is what put untrusted code in the default branch's cache scope"
    )


def test_no_job_checks_out_anything_but_the_run_it_belongs_to():
    offenders = {
        name: {key: step[key] for key in step if key in CHECKOUT_REF_KEYS}
        for name, job in jobs().items()
        for step in [(s.get("with") or {}) for s in checkout_steps(job)]
        if any(key in step for key in CHECKOUT_REF_KEYS)
    }

    assert offenders == {}, (
        f"{offenders} name a revision. A checkout with no ref lands on the "
        "commit GitHub pinned the run to, which is the commit whose branch owns "
        "the cache scope this run can write"
    )


def test_no_run_step_re_points_the_tree_after_the_checkout():
    """A checkout with no ref settles where the tree starts, not where it stays.

    ``git fetch`` plus ``git checkout``, or ``gh pr checkout``, would put another
    revision under the same job before the gates run it -- and the contract above
    reads ``actions/checkout`` inputs, so it would not notice. CodeQL models the
    same two shapes (``GitSHACheckout``, ``GhSHACheckout``); this is that half of
    the property. ``git rev-parse``, ``git describe`` and ``git tag --points-at``
    only read, and stay allowed.
    """

    moves = {}
    for name, job in jobs().items():
        for step in job.get("steps", ()):
            script = str(step.get("run", ""))
            for command in ("git fetch", "git pull", "git checkout", "git switch",
                            "git reset", "gh pr checkout"):
                if command in script:
                    moves.setdefault(name, []).append(command)

    assert moves == {}, (
        f"{moves} re-point the working tree. The job would then build something "
        "other than the revision whose branch owns this run's cache scope"
    )


def test_no_build_takes_its_context_from_somewhere_else():
    """``docker/build-push-action`` accepts a remote git URL as ``context``,
    which is a checkout the contract above cannot see either."""

    remote = {
        name: (step.get("with") or {})["context"]
        for name, job in jobs().items()
        for step in job.get("steps", ())
        if "build-push-action" in str(step.get("uses", ""))
        and not str((step.get("with") or {}).get("context", ".")).startswith(".")
    }

    assert remote == {}, f"{remote} build a context that is not this checkout"


def test_every_job_checks_out_something():
    """Guards the test above against passing because the checkouts went away:
    with no ``actions/checkout`` at all there is no ``ref`` to find."""

    without = [name for name, job in jobs().items() if not checkout_steps(job)]

    assert without == [], f"{without} run without checking out the source"


def test_the_release_workflow_is_the_one_that_would_have_restored_it():
    """Why this is worth a contract rather than a comment: the two workflows
    hash the same files, so they agree on the key, and the release build is the
    reader that a poisoned entry was aiming for."""

    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    cached = [
        step
        for job in release["jobs"].values()
        for step in job.get("steps", ())
        if (step.get("with") or {}).get("cache")
        or "type=gha" in str((step.get("with") or {}).get("cache-from", ""))
    ]

    assert cached, (
        "the release workflow no longer restores a cache, which removes the "
        "reason this contract exists -- reread it before deleting it"
    )


def test_the_buildx_layer_caches_keep_their_separate_scopes():
    """The convention that was already there and must not be undone while
    tidying. It never bound untrusted code -- see the module docstring -- but it
    does keep an honest feature build from evicting release layers."""

    text = WORKFLOW.read_text(encoding="utf-8")
    release = RELEASE.read_text(encoding="utf-8")

    assert "FEATURE_CACHE_SCOPE" in text, "the feature buildx cache lost its scope"
    assert "RELEASE_CACHE_SCOPE" not in text, (
        "the feature workflow names a release cache scope, which is the crossing "
        "the separate scopes exist to prevent"
    )
    assert "FEATURE_CACHE_SCOPE" not in release, (
        "the release workflow names a feature cache scope"
    )
