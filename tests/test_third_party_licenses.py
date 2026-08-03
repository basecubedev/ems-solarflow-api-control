# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for the third-party license inventory.

``THIRD_PARTY_LICENSES.md`` is the authoritative human-readable inventory, so a
new dependency, a new vendored asset or a new base image must fail here instead
of silently shipping undocumented. The checks reuse
``tools/check_third_party_licenses.py`` rather than re-parsing the document, and
the negative cases run against a mutated copy in ``tmp_path`` so the repository
is never touched.
"""
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.check_third_party_licenses import (
    NODE_DEV,
    OPTIONAL_PLATFORM,
    PYTHON_DEV,
    RUNTIME_DIRECT,
    VENDORED,
    collect_problems,
    component_keys,
    normalize_python_name,
    parse_inventory,
    read_package_json,
    read_python_manifest,
    vendored_static_files,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "THIRD_PARTY_LICENSES.md"
CHECKER = ROOT / "tools" / "check_third_party_licenses.py"
UPLOT_LICENSE = ROOT / "dashboard" / "static" / "uPlot.LICENSE"

COPIED_FILES = (
    "THIRD_PARTY_LICENSES.md",
    "requirements.txt",
    "requirements-dev.txt",
    "deploy/admin/requirements.txt",
    "package.json",
    "package-lock.json",
)
COPIED_TREES = ("dashboard/static", "admin/static")

# Image references that belong to this project rather than a third party.
OWN_IMAGE_PREFIXES = ("ghcr.io/basecubedev/", "ems-solarflow-")

_FROM = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
_IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.MULTILINE)
_HASH_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|$", re.MULTILINE)


@pytest.fixture
def repo_copy(tmp_path):
    """A minimal copy of the inputs the checker reads."""

    root = tmp_path / "repo"
    for relative in COPIED_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    for relative in COPIED_TREES:
        shutil.copytree(ROOT / relative, root / relative)
    return root


def sections():
    return parse_inventory(INVENTORY.read_text(encoding="utf-8"))


def tracked_files():
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def append(path, text):
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# --- the inventory as committed ---------------------------------------------


def test_the_committed_inventory_has_no_problems():
    assert collect_problems(ROOT) == []


def test_the_checker_command_exits_zero():
    result = subprocess.run(
        [sys.executable, str(CHECKER)], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_direct_python_dependency_is_documented():
    documented = {
        normalize_python_name(key)
        for title in (RUNTIME_DIRECT, PYTHON_DEV)
        for key in component_keys(sections()[title])
    }
    declared = {
        normalize_python_name(name)
        for manifest in (
            "requirements.txt",
            "requirements-dev.txt",
            "deploy/admin/requirements.txt",
        )
        for name in read_python_manifest(ROOT / manifest)
    }
    assert declared, "no Python dependency was parsed at all"
    assert declared <= documented, sorted(declared - documented)


def test_every_direct_node_dependency_is_documented():
    documented = set(component_keys(sections()[NODE_DEV]))
    declared = set(read_package_json(ROOT / "package.json"))
    assert declared, "no Node dependency was parsed at all"
    assert declared <= documented, sorted(declared - documented)


def test_every_vendored_static_asset_is_documented():
    body = "\n".join(sections()[VENDORED].lines)
    assets = vendored_static_files(ROOT)
    assert assets, "the vendored-asset scan found nothing to check"
    assert [path for path in assets if path not in body] == []


def test_runtime_and_development_dependencies_stay_separated():
    runtime = {normalize_python_name(key) for key in component_keys(sections()[RUNTIME_DIRECT])}
    development = {normalize_python_name(key) for key in component_keys(sections()[PYTHON_DEV])}
    assert runtime and development
    assert runtime.isdisjoint(development)


def test_development_dependencies_are_never_distributed():
    for title in (PYTHON_DEV, NODE_DEV, OPTIONAL_PLATFORM):
        for table in sections()[title].tables:
            for row in table["rows"]:
                assert row["Runtime"] == "❌", (title, row["Component"])
                assert row["Distributed"] == "❌", (title, row["Component"])


def test_every_container_base_image_is_documented():
    documented = set(component_keys(sections()["Container Base Images"]))
    referenced = set()
    for relative in tracked_files():
        name = Path(relative).name
        if not (name.startswith("Dockerfile") or "compose" in name and name.endswith(".yml")):
            continue
        text = (ROOT / relative).read_text(encoding="utf-8")
        patterns = _FROM if name.startswith("Dockerfile") else _IMAGE
        for reference in patterns.findall(text):
            if reference.startswith(OWN_IMAGE_PREFIXES) or "$" in reference:
                continue
            referenced.add(reference)
    assert referenced, "no base image reference was found at all"
    assert referenced <= documented, sorted(referenced - documented)


# --- vendored uPlot ----------------------------------------------------------


def test_the_vendored_uplot_license_file_carries_the_upstream_notice():
    text = UPLOT_LICENSE.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Leon Sorokin" in text
    assert "WITHOUT WARRANTY OF ANY KIND" in text


def test_the_vendored_uplot_files_match_the_recorded_hashes():
    recorded = dict(_HASH_ROW.findall(INVENTORY.read_text(encoding="utf-8")))
    assert len(recorded) == 2, recorded
    for relative, digest in recorded.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == digest, relative


# --- the checker actually rejects drift --------------------------------------


def test_an_undeclared_python_runtime_dependency_is_rejected(repo_copy):
    append(repo_copy / "requirements.txt", "\nhttpx>=0.27\n")
    assert any("httpx" in problem for problem in collect_problems(repo_copy))


def test_an_undeclared_python_development_dependency_is_rejected(repo_copy):
    append(repo_copy / "requirements-dev.txt", "\nmypy\n")
    assert any("mypy" in problem for problem in collect_problems(repo_copy))


def test_an_undeclared_node_dependency_is_rejected(repo_copy):
    package = repo_copy / "package.json"
    package.write_text(
        package.read_text(encoding="utf-8").replace(
            '"@playwright/test": "^1.61.1"',
            '"@playwright/test": "^1.61.1",\n    "eslint": "^9.0.0"',
        ),
        encoding="utf-8",
    )
    assert any("eslint" in problem for problem in collect_problems(repo_copy))


def test_a_stale_inventory_entry_is_rejected(repo_copy):
    manifest = repo_copy / "requirements-dev.txt"
    manifest.write_text("pytest\nruff==0.15.22\n", encoding="utf-8")
    assert any("pyyaml" in problem for problem in collect_problems(repo_copy))


def test_an_undocumented_vendored_asset_is_rejected(repo_copy):
    (repo_copy / "dashboard" / "static" / "chart.min.js").write_text(
        "/*! some third-party chart bundle */", encoding="utf-8"
    )
    assert any("chart.min.js" in problem for problem in collect_problems(repo_copy))


def test_an_undocumented_optional_platform_package_is_rejected(repo_copy):
    lock = repo_copy / "package-lock.json"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            '"node_modules/fsevents": {',
            '"node_modules/other-watcher": {\n'
            '      "version": "1.0.0",\n'
            '      "dev": true,\n'
            '      "optional": true,\n'
            '      "os": ["darwin"]\n'
            "    },\n"
            '    "node_modules/fsevents": {',
        ),
        encoding="utf-8",
    )
    assert any("other-watcher" in problem for problem in collect_problems(repo_copy))


def test_a_duplicate_entry_is_rejected(repo_copy):
    inventory = repo_copy / "THIRD_PARTY_LICENSES.md"
    text = inventory.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `ruff`"))
    inventory.write_text(text.replace(row, row + "\n" + row), encoding="utf-8")
    problems = collect_problems(repo_copy)
    assert any("duplicate" in problem and "ruff" in problem for problem in problems)


def test_a_missing_required_column_is_rejected(repo_copy):
    inventory = repo_copy / "THIRD_PARTY_LICENSES.md"
    text = inventory.read_text(encoding="utf-8")
    inventory.write_text(
        text.replace("| License (SPDX) | Used for |", "| Used for |", 1), encoding="utf-8"
    )
    assert any("missing columns" in problem for problem in collect_problems(repo_copy))


def test_a_development_dependency_marked_distributed_is_rejected(repo_copy):
    inventory = repo_copy / "THIRD_PARTY_LICENSES.md"
    text = inventory.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `ruff`"))
    inventory.write_text(text.replace(row, row.replace("❌ | ❌", "❌ | ✅")), encoding="utf-8")
    assert any("Distributed=❌" in problem for problem in collect_problems(repo_copy))
