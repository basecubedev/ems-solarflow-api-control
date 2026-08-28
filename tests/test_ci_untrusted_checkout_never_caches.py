# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: a job that checks out a chosen revision writes no cache.

``docker-feature-publish.yml`` exists to build a branch somebody names. Every
job after ``resolve-source`` checks out that revision, so every one of them runs
code the default branch has not accepted.

Those jobs also used ``actions/setup-python`` and ``actions/setup-node`` with
``cache: pip`` / ``cache: npm``. A GitHub Actions cache is scoped to the *ref of
the run*, not to what the run checked out -- and this workflow is dispatched from
``main``, so anything it saved landed in the default branch's cache. Its key is
derived from the lockfile hashes, which the release workflow hashes identically,
so `docker-publish.yml` would restore on main whatever a feature build had left
behind. Code from an unmerged branch could therefore decide what a signed release
was built against, without ever being merged.

The buildx layer caches are a different case and stay: they carry explicit
``*_FEATURE_CACHE_SCOPE`` / ``*_RELEASE_CACHE_SCOPE`` names, so a feature build
and a release build never read each other's entries. What is left is a feature
build able to affect the next feature build, which is the same trust boundary it
already sits inside -- the workflow's whole purpose is to build that code.

CodeQL reports four of these; it follows ``needs.resolve-source.outputs.sha``
into a job but not the same revision laundered through
``publish-feature-ghcr.outputs.revision``, which three further jobs check out.
Repairing only the alert list would have left three jobs of identical shape, so
the property is asserted here for every job rather than for the reported ones.
"""

import pytest
import yaml

from pathlib import Path

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"

# What a job would have to say to cache a dependency tree. The buildx inputs
# (`cache-from`, `cache-to`) are deliberately not here -- see the module
# docstring.
CACHE_KEYS = ("cache", "cache-dependency-path")


def jobs():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def checkout_ref(job):
    for step in job.get("steps", ()):
        if "checkout" in str(step.get("uses", "")):
            return str((step.get("with") or {}).get("ref", ""))
    return ""


def caching_steps(job):
    return [
        step
        for step in job.get("steps", ())
        if any(key in (step.get("with") or {}) for key in CACHE_KEYS)
    ]


def test_the_workflow_still_builds_a_revision_somebody_chose():
    """The premise. If this stops being true the rule below is about nothing,
    and its absence would look like the property holding."""

    refs = {name: checkout_ref(job) for name, job in jobs().items()}

    direct = {name for name, ref in refs.items() if "resolve-source.outputs.sha" in ref}
    laundered = {
        name for name, ref in refs.items()
        if "publish-feature-ghcr.outputs.revision" in ref
    }

    # Both families, named separately: the second is the one CodeQL does not
    # follow, and counting them together let a mutation that removed every
    # direct checkout still satisfy a threshold.
    assert direct, "no job checks out the revision resolve-source resolved"
    assert laundered, (
        "no job checks out that revision through publish-feature-ghcr; if that "
        "path is gone the docstring's claim about CodeQL's blind spot is stale"
    )


def test_no_job_that_checks_out_a_chosen_revision_declares_a_cache():
    offenders = {
        name: [step.get("name") or step.get("uses") for step in caching_steps(job)]
        for name, job in jobs().items()
        if "needs." in checkout_ref(job) and caching_steps(job)
    }

    assert offenders == {}, (
        f"{offenders} cache a dependency tree while building a chosen revision. "
        "The cache is scoped to the ref of the run -- main, on a dispatch -- so "
        "the release workflow would restore it under the same lockfile-derived key"
    )


def test_the_release_workflow_is_the_one_that_would_have_restored_it():
    """Why the rule is worth having rather than theoretical: the two workflows
    hash the same files, so they would have agreed on the key."""

    release = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
    )
    cached = [
        step
        for job in release["jobs"].values()
        for step in job.get("steps", ())
        if (step.get("with") or {}).get("cache")
    ]

    assert cached, (
        "the release workflow no longer caches, which removes the reason this "
        "contract exists -- reread it before deleting it"
    )


def test_the_buildx_layer_caches_keep_their_separate_scopes():
    """The mitigation that was already there and must not be undone while
    tidying: a feature build and a release build never share a scope."""

    text = WORKFLOW.read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    assert "FEATURE_CACHE_SCOPE" in text, "the feature buildx cache lost its scope"
    assert "RELEASE_CACHE_SCOPE" not in text, (
        "the feature workflow names a release cache scope, which is the crossing "
        "the separate scopes exist to prevent"
    )
    assert "FEATURE_CACHE_SCOPE" not in release, (
        "the release workflow names a feature cache scope"
    )
