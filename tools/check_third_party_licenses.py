#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify ``THIRD_PARTY_LICENSES.md`` against the repository's own manifests.

Direct declarations only: every entry in the requirements files and in
``package.json``, every optional platform package in ``package-lock.json``, and
every static asset that does not carry the project license header must have a
row in the inventory. Transitive resolution needs a package manager and is
deliberately out of scope — the runtime transitive table is maintained by hand
and only checked for shape.

Run it directly (``python tools/check_third_party_licenses.py``) or through
``tests/test_third_party_licenses.py``; both use ``collect_problems``.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "THIRD_PARTY_LICENSES.md"

REQUIRED_COLUMNS = (
    "Component",
    "Version",
    "License (SPDX)",
    "Used for",
    "Runtime",
    "Distributed",
    "Upstream",
)
FLAGS = ("✅", "❌")

RUNTIME_DIRECT = "Python Runtime Dependencies (Direct)"
RUNTIME_TRANSITIVE = "Python Runtime Dependencies (Transitive)"
DISTRIBUTED_BINARIES = "Distributed Third-Party Binaries"
PYTHON_DEV = "Python Development Dependencies"
NODE_DEV = "Node Development Dependencies"
BROWSER_RUNTIMES = "Browser Runtimes"
EXTERNAL_TOOLS = "External Developer Tools"
GITHUB_ACTIONS = "GitHub Actions"
VENDORED = "Vendored Components"
BASE_IMAGES = "Container Base Images"
OPTIONAL_PLATFORM = "Optional Platform Dependencies"
APPLIANCE_PACKAGES = "Appliance Package Dependencies"

# Sections whose tables describe components and therefore share one column
# contract. "Generated Assets" is a different shape and is checked separately.
COMPONENT_SECTIONS = (
    RUNTIME_DIRECT,
    RUNTIME_TRANSITIVE,
    DISTRIBUTED_BINARIES,
    PYTHON_DEV,
    NODE_DEV,
    BROWSER_RUNTIMES,
    EXTERNAL_TOOLS,
    GITHUB_ACTIONS,
    VENDORED,
    BASE_IMAGES,
    OPTIONAL_PLATFORM,
    APPLIANCE_PACKAGES,
)

APPLIANCE_CONTROL = "packaging/appliance/debian/control"

PYTHON_RUNTIME_MANIFESTS = ("requirements.txt", "deploy/admin/requirements.txt")
PYTHON_DEV_MANIFESTS = ("requirements-dev.txt",)

# Distribution expectations that follow from what a section *is*. The runtime
# transitive table intentionally has none: a conditional dependency is listed
# there as not installed.
EXPECTED_FLAGS = {
    RUNTIME_DIRECT: ("✅", "✅"),
    PYTHON_DEV: ("❌", "❌"),
    NODE_DEV: ("❌", "❌"),
    BROWSER_RUNTIMES: ("❌", "❌"),
    GITHUB_ACTIONS: ("❌", "❌"),
    OPTIONAL_PLATFORM: ("❌", "❌"),
}

STATIC_DIRS = ("dashboard/static", "admin/static", "appliance/static")
PROJECT_LICENSE_HEADER = "SPDX-License-Identifier: AGPL-3.0-or-later"

_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^```")


class Section:
    def __init__(self, title):
        self.title = title
        self.lines = []
        self.tables = []


def normalize_python_name(name):
    """PEP 503 normalization so ``PyYAML`` and ``pyyaml`` are one key."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_inventory(text):
    """Split the inventory into sections, each with its Markdown tables."""

    sections = {}
    current = Section("")
    pending = None
    open_table = None
    in_fence = False

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING.match(line)
        if heading:
            pending, open_table = None, None
            current = Section(heading.group(2))
            sections[current.title] = current
            continue

        current.lines.append(line)
        stripped = line.strip()

        if not stripped.startswith("|"):
            pending, open_table = None, None
            continue
        if _SEPARATOR.match(stripped) and pending is not None:
            open_table = {"columns": pending, "rows": []}
            current.tables.append(open_table)
            pending = None
            continue
        if open_table is not None:
            cells = _split_row(line)
            if len(cells) == len(open_table["columns"]):
                open_table["rows"].append(dict(zip(open_table["columns"], cells)))
            else:
                open_table["rows"].append({"__malformed__": stripped})
            continue
        pending = _split_row(line)

    return sections


def component_tables(section):
    """Tables that describe components; other shapes (hashes) are ignored."""

    return [table for table in section.tables if "Component" in table["columns"]]


def component_keys(section):
    """Component cells of every component row in a section, backticks stripped."""

    keys = []
    for table in component_tables(section):
        for row in table["rows"]:
            cell = row.get("Component")
            if cell is None:
                continue
            span = _CODE_SPAN.search(cell)
            keys.append(span.group(1) if span else cell)
    return keys


def read_python_manifest(path):
    names = []
    if not path.is_file():
        return names
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def read_package_json(path):
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    declared = {}
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in (data.get(field) or {}).items():
            declared[name] = (field, spec)
    return declared


def read_package_lock(path):
    """Return ``(all_names, optional_names)`` from a lockfile."""

    if not path.is_file():
        return set(), set()
    data = json.loads(path.read_text(encoding="utf-8"))
    names, optional = set(), set()
    for location, entry in (data.get("packages") or {}).items():
        if not location:
            continue
        name = location.split("node_modules/")[-1]
        names.add(name)
        if entry.get("optional") or entry.get("os"):
            optional.add(name)
    for name in (data.get("dependencies") or {}):
        names.add(name)
    return names, optional


def vendored_static_files(root):
    """Static assets that do not carry the project license header."""

    found = []
    for relative in STATIC_DIRS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            head = path.read_bytes()[:400].decode("utf-8", errors="replace")
            if PROJECT_LICENSE_HEADER in head:
                continue
            found.append(path.relative_to(root).as_posix())
    return found


def _check_structure(sections, problems):
    for title in COMPONENT_SECTIONS:
        section = sections.get(title)
        if section is None:
            problems.append(f"missing inventory section: {title!r}")
            continue
        tables = component_tables(section)
        if not tables:
            problems.append(f"section {title!r} has no component table")
            continue
        for table in tables:
            missing = [name for name in REQUIRED_COLUMNS if name not in table["columns"]]
            if missing:
                problems.append(f"section {title!r} table is missing columns: {missing}")
            for row in table["rows"]:
                if "__malformed__" in row:
                    problems.append(
                        f"section {title!r} has a row with the wrong cell count: "
                        f"{row['__malformed__']}"
                    )
                    continue
                for name in REQUIRED_COLUMNS:
                    if name in table["columns"] and not row.get(name):
                        problems.append(
                            f"section {title!r} row {row.get('Component', '?')} "
                            f"has an empty {name!r} cell"
                        )

        keys = component_keys(section)
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            problems.append(f"section {title!r} lists duplicate components: {duplicates}")

        expected = EXPECTED_FLAGS.get(title)
        for table in tables:
            for row in table["rows"]:
                if "__malformed__" in row:
                    continue
                component = row.get("Component", "?")
                for column in ("Runtime", "Distributed"):
                    value = row.get(column)
                    if value not in FLAGS:
                        problems.append(
                            f"section {title!r} row {component} has a non-flag "
                            f"{column!r} cell: {value!r}"
                        )
                if expected is None:
                    continue
                if (row.get("Runtime"), row.get("Distributed")) != expected:
                    problems.append(
                        f"section {title!r} row {component} must be "
                        f"Runtime={expected[0]} / Distributed={expected[1]}"
                    )


def _check_python(root, sections, problems):
    runtime = sections.get(RUNTIME_DIRECT)
    dev = sections.get(PYTHON_DEV)
    if runtime is None or dev is None:
        return

    documented_runtime = {normalize_python_name(key) for key in component_keys(runtime)}
    documented_dev = {normalize_python_name(key) for key in component_keys(dev)}

    declared_runtime = set()
    for manifest in PYTHON_RUNTIME_MANIFESTS:
        for name in read_python_manifest(root / manifest):
            declared_runtime.add(normalize_python_name(name))
            if normalize_python_name(name) not in documented_runtime:
                problems.append(
                    f"{manifest} declares {name!r}, which is not in the "
                    f"{RUNTIME_DIRECT!r} section"
                )

    declared_dev = set()
    for manifest in PYTHON_DEV_MANIFESTS:
        for name in read_python_manifest(root / manifest):
            declared_dev.add(normalize_python_name(name))
            if normalize_python_name(name) not in documented_dev:
                problems.append(
                    f"{manifest} declares {name!r}, which is not in the "
                    f"{PYTHON_DEV!r} section"
                )

    for stale in sorted(documented_runtime - declared_runtime):
        problems.append(
            f"{RUNTIME_DIRECT!r} lists {stale!r}, which no requirements file declares"
        )
    for stale in sorted(documented_dev - declared_dev):
        problems.append(
            f"{PYTHON_DEV!r} lists {stale!r}, which requirements-dev.txt does not declare"
        )

    overlap = sorted(documented_runtime & documented_dev)
    if overlap:
        problems.append(f"packages documented as both runtime and development: {overlap}")


def _check_node(root, sections, problems):
    node = sections.get(NODE_DEV)
    optional_section = sections.get(OPTIONAL_PLATFORM)
    if node is None or optional_section is None:
        return

    documented_node = set(component_keys(node))
    documented_optional = set(component_keys(optional_section))
    declared = read_package_json(root / "package.json")
    locked, optional = read_package_lock(root / "package-lock.json")

    for name, (field, _spec) in sorted(declared.items()):
        if name not in documented_node:
            problems.append(
                f"package.json {field} declares {name!r}, which is not in the "
                f"{NODE_DEV!r} section"
            )

    for stale in sorted(documented_node - set(declared) - locked):
        problems.append(
            f"{NODE_DEV!r} lists {stale!r}, which neither package.json nor "
            "package-lock.json contains"
        )

    for name in sorted(optional):
        if name not in documented_optional:
            problems.append(
                f"package-lock.json marks {name!r} optional/platform-specific, "
                f"which is not in the {OPTIONAL_PLATFORM!r} section"
            )

    for stale in sorted(documented_optional - optional):
        problems.append(
            f"{OPTIONAL_PLATFORM!r} lists {stale!r}, which package-lock.json "
            "does not mark optional"
        )


def _check_vendored(root, sections, problems):
    section = sections.get(VENDORED)
    if section is None:
        return
    body = "\n".join(section.lines)
    for path in vendored_static_files(root):
        if path not in body:
            problems.append(
                f"static asset {path!r} carries no project license header and is "
                f"not documented in the {VENDORED!r} section"
            )


def _appliance_dependencies(root):
    """The Debian packages the appliance declares, from the control file itself.

    Repeating the list here is how it drifts: cloud-guest-utils and e2fsprogs
    were added to the package and reached neither the inventory nor the guest
    the image tier builds.
    """

    control = Path(root) / APPLIANCE_CONTROL
    if not control.is_file():
        return []
    field = re.search(r"^Depends:(.*?)(?=^\S)", control.read_text(encoding="utf-8"),
                      re.MULTILINE | re.DOTALL)
    if not field:
        return []
    names = []
    for entry in field.group(1).replace("\n", " ").split(","):
        parts = entry.strip().split()
        if parts:
            names.append(parts[0])
    return names


def _check_appliance(root, sections, problems):
    section = sections.get(APPLIANCE_PACKAGES)
    if section is None:
        problems.append(f"missing section: {APPLIANCE_PACKAGES}")
        return
    documented = set(component_keys(section))
    for name in _appliance_dependencies(root):
        if name not in documented:
            problems.append(
                f"{APPLIANCE_PACKAGES}: {name} is declared in {APPLIANCE_CONTROL} "
                "but not documented"
            )


def collect_problems(root=ROOT):
    """Return every inventory problem as a human-readable line."""

    root = Path(root)
    inventory = root / INVENTORY
    if not inventory.is_file():
        return [f"missing {INVENTORY}"]

    sections = parse_inventory(inventory.read_text(encoding="utf-8"))
    problems = []
    _check_structure(sections, problems)
    _check_python(root, sections, problems)
    _check_node(root, sections, problems)
    _check_vendored(root, sections, problems)
    _check_appliance(root, sections, problems)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"Verify {INVENTORY} against the manifests.")
    parser.add_argument("--root", default=str(ROOT), help="repository root to check")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    problems = collect_problems(Path(args.root))
    if problems:
        print(f"{INVENTORY} is out of date:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{INVENTORY}: every direct dependency, optional package and vendored asset is documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
