# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the badges are claims, and a claim can go stale silently.

The README opens with nine badges. Five of them assert something checkable
about this repository, and all five can drift without anything failing: a
workflow file is renamed and its badge starts rendering "no status"; the CI
matrix gains a Python version the badge does not mention; the test count is
written once and never again; the interface gains a language the badge and
the sentence under it do not name.

The test count had drifted furthest. It read ``12300+`` against 11029 collected
tests -- a badge overstating what it counts is the one direction that matters,
because the number is there to be believed by someone who is not going to run
the suite.

What the count means here is fixed deliberately narrowly: what ``pytest
--collect-only`` reports, and nothing else. The browser suites add roughly
another seven hundred across five Playwright configurations, but counting them
needs ``node_modules`` and per-config environment variables, which would make
this test fail for reasons that have nothing to do with a badge. Excluding them
means the badge understates the total, which is the safe direction to be wrong
in.

The number is floored to a hundred rather than tracked exactly, so ordinary work
does not have to touch the README. It moves when a hundred tests do.
"""

import functools
import math
import re
import subprocess
import sys

import pytest
import yaml

from pathlib import Path
from urllib.parse import unquote

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "simulated-regression-tests.yml"

COUNT_BADGE = re.compile(r"automated%20tests-(\d+)%2B-")
COLLECTED = re.compile(r"(\d+) tests collected")
WORKFLOW_BADGE = re.compile(r"actions/workflows/([a-z0-9._-]+\.yml)/badge\.svg")
PYTHON_BADGE = re.compile(r"badge/python-([0-9.%A-Za-z]+)-")
LANGUAGE_BADGE = re.compile(
    r"\[!\[[^\]]*\]\(https://img\.shields\.io/badge/interface-([^)-]+)-[0-9a-f]{6}\)\]\(([^)]+)\)"
)
SHARED_PREFS = ROOT / "frontend" / "shared" / "state" / "prefs.svelte.js"
LANGUAGES = re.compile(r"export const LANGUAGES = Object\.freeze\(\[([^\]]*)\]\)")
ENDONYMS = {"en": "English", "de": "Deutsch", "nl": "Nederlands", "fr": "Français"}


def readme():
    return README.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def collected_tests():
    """What this repository's own suite reports, in a subprocess.

    ``--collect-only`` imports every module but runs nothing, so this is the
    same number a person gets by asking, which is the point: a badge that is
    checked against a hand-maintained constant has only moved the staleness.

    Cached: two tests below ask, and collecting eleven thousand tests twice to
    answer one question about a README is not worth four seconds.
    """

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
    )
    match = COLLECTED.search(result.stdout)
    assert match, (
        "pytest did not report a collected count; the output format changed:\n"
        + result.stdout[-2000:]
    )
    return int(match.group(1))


def test_the_test_count_badge_is_the_floored_truth():
    claimed = COUNT_BADGE.search(readme())
    assert claimed, "the automated-tests badge is gone or its shape changed"

    actual = collected_tests()
    expected = math.floor(actual / 100) * 100

    assert int(claimed.group(1)) == expected, (
        f"the badge claims {claimed.group(1)}+ and {actual} tests are collected; "
        f"set it to {expected}. Round down to the hundred -- a badge that "
        "overstates is believed by people who will not run the suite"
    )


def test_the_badge_never_claims_more_than_exists():
    """Stated separately from the equality above because it is the property that
    matters. If the rounding rule is ever revisited, this one still holds."""

    claimed = int(COUNT_BADGE.search(readme()).group(1))

    assert claimed <= collected_tests(), (
        "the badge claims more automated tests than the suite collects"
    )


def test_every_workflow_badge_names_a_workflow_that_exists():
    """A badge for a renamed file renders "no status" rather than failing, so
    nothing else would notice. Workflow *display names* were renamed once
    already; the files were left alone precisely because these URLs embed them."""

    named = WORKFLOW_BADGE.findall(readme())
    assert named, "no workflow badges were found; the README shape changed"

    missing = [name for name in named if not (ROOT / ".github" / "workflows" / name).exists()]
    assert missing == [], (
        f"{missing} are named by a README badge and do not exist. A badge URL "
        "embeds the filename, so renaming a workflow file breaks it silently"
    )


def test_the_python_badge_names_the_versions_ci_actually_runs():
    badge = PYTHON_BADGE.search(readme())
    assert badge, "the Python badge is gone or its shape changed"

    claimed = {part.strip() for part in badge.group(1).replace("%20", " ").split("%7C")}
    matrix = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    supported = {
        str(version)
        for version in matrix["jobs"]["python-tests"]["strategy"]["matrix"]["python-version"]
    }

    assert claimed == supported, (
        f"the badge says {sorted(claimed)} and CI runs {sorted(supported)}"
    )


def test_the_dependabot_badge_points_at_the_configuration_it_advertises():
    assert "(.github/dependabot.yml)" in readme(), (
        "the Dependabot badge no longer links to its configuration"
    )
    assert (ROOT / ".github" / "dependabot.yml").exists(), (
        "the README advertises Dependabot and the configuration is gone"
    )


def interface_languages():
    """The languages the three Modern consoles offer, in the order they offer them.

    The README names each by its own name (``ENDONYMS``), as a reader looks for it.
    """

    match = LANGUAGES.search(SHARED_PREFS.read_text(encoding="utf-8"))
    assert match, "LANGUAGES is gone from the shared prefs module or its shape changed"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_language_badge_and_sentence_name_every_interface_language():
    """A new language without a README line would leave the badge understating,
    and one removed would leave it promising what is gone; both are named here."""

    codes = interface_languages()
    assert set(codes) == set(ENDONYMS), (
        f"the interface offers {codes}; name every language in ENDONYMS here, "
        "then in the README's interface badge and the sentence under the intro"
    )
    names = [ENDONYMS[code] for code in codes]

    badge = LANGUAGE_BADGE.search(readme())
    assert badge, "the interface-language badge is gone or its shape changed"
    assert unquote(badge.group(1)).split(" · ") == names, (
        f"the badge names {unquote(badge.group(1))}; the interface offers {names}"
    )
    assert (ROOT / badge.group(2)).exists(), (
        f"the language badge links to {badge.group(2)}, which does not exist"
    )

    sentence = f"**{', '.join(names[:-1])} and {names[-1]}**"
    assert sentence in " ".join(readme().split()), f"the README no longer says {sentence}"
