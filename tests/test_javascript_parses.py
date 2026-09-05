# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every tracked JavaScript file parses.

CI compiles all of the Python (`compileall`) and parses exactly one JavaScript
file (`node --check admin/static/admin.js`). Everything else written in the
language -- two more frontends, and the Playwright drivers and probes the
performance work added -- was never parsed by anything. A syntax error in a
benchmark driver ships and is found by whoever next runs a benchmark; a syntax
error in a frontend is caught only where a test happens to `require` it.

This closes that: it is the JavaScript half of `compileall`, and it is cheap
enough to be a contract test rather than a CI step, which means it runs in the
same group as the code it protects.

Parsing is not linting. It catches the class of mistake that makes a file
unloadable, and nothing else.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]

# Third-party bundles are shipped as-is and are not this project's to fix; they
# are also large enough to make the check noticeably slower.
VENDORED = {
    "dashboard/static/uPlot.iife.min.js",
}


def tracked_javascript():
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.js", "*.mjs"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    paths = [p for p in listing.split("\0") if p]
    return sorted(p for p in paths if p not in VENDORED)


@pytest.mark.parametrize("path", tracked_javascript())
def test_the_file_parses(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to parse JavaScript")
    result = subprocess.run(
        [node, "--check", path], cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{path} does not parse:\n{result.stderr}"


def test_the_listing_is_not_silently_empty():
    """A `git ls-files` that returns nothing would make every case above vanish
    and the suite would still be green."""

    paths = tracked_javascript()
    assert len(paths) > 20, paths
    assert "dashboard/static/app.js" in paths
    assert "scripts/dashboard_profile/profile_driver.mjs" in paths
