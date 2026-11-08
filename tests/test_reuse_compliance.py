# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every file in the repository names its copyright holder and license.

The repository follows the REUSE specification: a file the project writes
carries two SPDX lines, ``REUSE.toml`` declares the files that cannot carry a
comment or whose exact bytes are the point (fixtures, signed release evidence)
and every third-party file, and ``LICENSES/`` holds the texts. ``reuse lint``
is the check. The negative cases run against a minimal project in ``tmp_path``
with the repository's own ``REUSE.toml``, so they prove that its annotations
cover no new source file in the areas where code lives. The Modern bundles'
annotations are tied to the ``licenses.json`` the build writes beside them.
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]

ROOT = Path(__file__).resolve().parents[1]
PROJECT_LICENSE = "AGPL-3.0-or-later.txt"
HEADER = "".join(Path(__file__).read_text(encoding="utf-8").splitlines(keepends=True)[:2])
BUNDLES = {
    "dashboard": "dashboard/static/modern/assets/modern.js",
    "admin": "admin/static/modern/assets/vendor.js",
    "appliance": "appliance/static/modern/assets/appliance.js",
}
NEW_SOURCE_FILES = (
    "ems/added.py",
    "admin/added.py",
    "appliance/added.py",
    "frontend/shared/ui/Added.svelte",
    "tests/e2e/added.spec.ts",
    "scripts/added.sh",
    "deploy/admin/added.sh",
    ".github/workflows/added.yml",
    "packaging/appliance/debian/preinst",
    "packaging/appliance/config/added.conf",
)


def lint(root):
    return subprocess.run(
        [sys.executable, "-m", "reuse", "--root", str(root), "lint", "--lines"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def annotations():
    return tomllib.loads((ROOT / "REUSE.toml").read_text(encoding="utf-8"))["annotations"]


def _pattern(glob):
    parts = re.split(r"(\*\*/|\*\*|\*)", glob)
    wildcard = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*"}
    return re.compile("".join(wildcard.get(part, re.escape(part)) for part in parts))


def annotation_for(path):
    """The annotation REUSE applies to ``path``: the last one that matches."""

    found = None
    for annotation in annotations():
        paths = annotation["path"] if isinstance(annotation["path"], list) else [annotation["path"]]
        if any(_pattern(glob).fullmatch(path) for glob in paths):
            found = annotation
    return found


_COPYRIGHT_LINE = re.compile(r"\s*Copyright(?:\s*\([cC]\)|\s*\u00a9)?\s+(?![a-z])(.+)")
_TEMPLATE = re.compile(r"\[yyyy\]|<year>|\[name of copyright owner\]", re.IGNORECASE)


def holders(text):
    for line in text.splitlines():
        match = _COPYRIGHT_LINE.match(line)
        if match and not _TEMPLATE.search(line):
            holder = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", match.group(1))
            holder = re.sub(r"\s*<[^>]*>", "", holder)
            yield re.sub(r"\s*\([^)]*\)\s*$", "", holder).strip()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "LICENSES").mkdir(parents=True)
    shutil.copy(ROOT / "LICENSES" / PROJECT_LICENSE, root / "LICENSES" / PROJECT_LICENSE)
    shutil.copy(ROOT / "REUSE.toml", root / "REUSE.toml")
    return root


def test_every_file_names_its_copyright_holder_and_license():
    result = lint(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_license_text_reuse_reads_is_the_project_license():
    assert (ROOT / "LICENSES" / PROJECT_LICENSE).read_bytes() == (ROOT / "LICENSE").read_bytes()


@pytest.mark.parametrize("relative", NEW_SOURCE_FILES)
def test_a_source_file_without_the_two_lines_is_rejected(project, relative):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("added = True\n", encoding="utf-8")
    result = lint(project)
    assert result.returncode != 0
    assert relative in result.stdout


def test_a_source_file_with_the_two_lines_passes(project):
    path = project / "ems" / "added.py"
    path.parent.mkdir()
    path.write_text(HEADER + '"""A module with its header."""\n', encoding="utf-8")
    result = lint(project)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("app", sorted(BUNDLES))
def test_each_bundle_declares_every_package_its_licenses_json_lists(app):
    listed = json.loads((ROOT / app / "static" / "modern" / "licenses.json").read_text(encoding="utf-8"))
    assert listed
    for path in (BUNDLES[app], f"{app}/static/modern/licenses.json"):
        annotation = annotation_for(path)
        assert annotation is not None and annotation.get("precedence") == "override", path
        declared = annotation["SPDX-FileCopyrightText"]
        declared = [declared] if isinstance(declared, str) else declared
        expression = set(re.findall(r"[A-Za-z0-9.+-]+", annotation["SPDX-License-Identifier"])) - {"AND", "OR", "WITH"}
        for package in listed:
            assert package["identifier"] in expression, (path, package["name"])
            found = list(holders(package["text"]))
            stock_text = _TEMPLATE.search(package["text"]) is not None
            assert found or stock_text, (path, package["name"], "its license text names no holder to compare")
            for holder in found:
                assert holder in declared, (path, package["name"], holder)
