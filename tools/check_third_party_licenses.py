#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify ``THIRD_PARTY_LICENSES.md`` against the repository's own manifests.

Direct declarations only: every entry in the requirements files and in
``package.json``, every optional platform package in ``package-lock.json``,
every GitHub Action a workflow uses, and every static asset that does not carry
the project license header must have a row in the inventory. Every prebuilt
binary the final stage of a Dockerfile places under a system binary tree has a
row that names its ``third_party/`` license files, and that same stage copies
them beside it. A binary a ``RUN`` step compiles, writes from a heredoc or
places with a command not listed in ``stage_contents`` is not seen and needs
its row by hand. Transitive
resolution needs a package manager and is deliberately out of scope — the
runtime transitive table is maintained by hand and only checked for shape.

Run it directly (``python tools/check_third_party_licenses.py``) or through
``tests/test_third_party_licenses.py``; both use ``collect_problems``.
"""

import argparse
import json
import re
import shlex
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
WORKFLOW_DIR = ".github/workflows"

PYTHON_RUNTIME_MANIFESTS = ("requirements.txt", "deploy/admin/requirements.txt")
PYTHON_DEV_MANIFESTS = ("requirements-dev.txt", "requirements-docs.txt")

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

THIRD_PARTY_DIR = "third_party"
DOCKERFILES = ("Dockerfile", "deploy/admin/Dockerfile")
NOTICE_DESTINATION = "/usr/share/doc"

STATIC_DIRS = ("dashboard/static", "admin/static", "appliance/static")
# REUSE-IgnoreStart
PROJECT_LICENSE_HEADER = "SPDX-License-Identifier: AGPL-3.0-or-later"
# REUSE-IgnoreEnd

_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)
_WORKFLOW_FILES = ("*.yml", "*.yaml")
_FENCE = re.compile(r"^```")
_NOTICE_FILE = re.compile(rf"^{THIRD_PARTY_DIR}/([\w.-]+)/([\w.-]+)$")
_NOTICE_SOURCE = re.compile(rf"^{THIRD_PARTY_DIR}/([\w.-]+)/$")
_CONTINUATION = re.compile(r"\\[ \t]*\n")
_INSTRUCTION = re.compile(r"^\s*([A-Za-z]+)\s+(.*)$")
_COMMAND_SEPARATOR = re.compile(r"&&|\|\||;|\|")
BINARY_PREFIXES = ("/usr/", "/opt/", "/bin/", "/sbin/", "/lib/")
NOT_BINARIES = re.compile(r"^/usr/share/|/(?:site|dist)-packages(?:/|$)")
SHELL_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "("}
SHELLS = {"sh", "bash", "dash"}
NO_UPSTREAM_NOTICE = "no NOTICE upstream"
_OUTPUT_OPTION = {
    "curl": ("o", "--output"),
    "wget": ("O", "--output-document"),
    "tar": ("C", "--directory"),
    "unzip": ("d", "--directory"),
}
_OPTION_ARGUMENT = {
    "install": {"-m", "-o", "-g", "--mode", "--owner", "--group", "-S", "--suffix"},
    "cp": {"-S", "--suffix"},
    "mv": {"-S", "--suffix"},
}


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
            f"{PYTHON_DEV!r} lists {stale!r}, which no development requirements file declares"
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


def instructions(text):
    """A Dockerfile's instructions as ``(KEYWORD, arguments)``, continuations joined."""

    lines = [line for line in text.splitlines(keepends=True) if not line.lstrip().startswith("#")]
    found = []
    for line in _CONTINUATION.sub(" ", "".join(lines)).splitlines():
        match = _INSTRUCTION.match(line)
        if match:
            found.append((match.group(1).upper(), match.group(2).strip()))
    return found


def final_stage(text):
    """The instructions after the last ``FROM``: what the image ships."""

    found = instructions(text)
    starts = [index for index, (keyword, _) in enumerate(found) if keyword == "FROM"]
    return found[starts[-1] + 1:] if starts else found


def _words(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _file_command_destinations(command, arguments):
    if command == "install" and any(word in ("-d", "--directory") for word in arguments):
        return []
    positional, target, skip = [], None, False
    for index, word in enumerate(arguments):
        if skip:
            skip = False
        elif word in _OPTION_ARGUMENT[command]:
            skip = True
        elif word == "-t" and index + 1 < len(arguments):
            target, skip = arguments[index + 1], True
        elif word.startswith("--target-directory="):
            target = word.partition("=")[2]
        elif not word.startswith("-") or word == "-":
            positional.append(word)
    if target:
        return [target]
    return positional[-1:] if len(positional) > 1 else []


def _output_destinations(command, arguments):
    short, long = _OUTPUT_OPTION[command]
    found = []
    for index, word in enumerate(arguments):
        following = arguments[index + 1] if index + 1 < len(arguments) else None
        if word == long and following:
            found.append(following)
        elif word.startswith(long + "="):
            found.append(word.partition("=")[2])
        elif word.startswith("-") and not word.startswith("--") and short in word[1:]:
            rest = word[1:].partition(short)[2]
            if rest:
                found.append(rest)
            elif following:
                found.append(following)
    return found


def _extracting(arguments):
    clusters = [word for word in arguments if word.startswith("-") and not word.startswith("--")]
    old_style = arguments[:1] if arguments and not arguments[0].startswith("-") else []
    return any(word in ("--extract", "--get") for word in arguments) or any("x" in word for word in clusters + old_style)


def command_destinations(words):
    """Where one shell command puts files, for the commands that place binaries.

    Shell keywords and ``case`` patterns in front of a command are skipped, and
    ``sh -c`` is read as the script it runs.
    """

    words = list(words)
    if words and words[0] == "case" and "in" in words:
        after = words[words.index("in") + 1:]
        words = next((after[index + 1:] for index, word in enumerate(after) if word.endswith(")")), [])
    while words:
        first = words[0].lstrip("(")
        if not first or first in SHELL_WORDS or first.endswith(")") or "=" in first and not first.startswith("-"):
            words = words[1:]
        else:
            words[0] = first
            break
    if not words:
        return []
    command, arguments = words[0], words[1:]
    if command in SHELLS and "-c" in arguments[:-1]:
        script = arguments[arguments.index("-c") + 1]
        return [path for part in _COMMAND_SEPARATOR.split(script) for path in command_destinations(_words(part))]
    if command in _OPTION_ARGUMENT:
        return _file_command_destinations(command, arguments)
    if command == "tar" and not _extracting(arguments):
        return []
    if command in _OUTPUT_OPTION:
        return _output_destinations(command, arguments)
    return []


def _instruction_words(arguments):
    """Flags and paths of a ``COPY``, ``ADD`` or ``RUN``, in shell or exec (JSON) form."""

    words = _words(arguments)
    flags = [word for word in words if word.startswith("--")]
    rest = arguments
    for flag in flags:
        rest = rest.replace(flag, "", 1)
    rest = rest.strip()
    if rest.startswith("["):
        try:
            parsed = json.loads(rest)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(word, str) for word in parsed):
            return flags, parsed, True
    return flags, [word for word in words if not word.startswith("--")], False


def stage_contents(text):
    """The binary paths a Dockerfile's final stage installs and the notices it copies.

    ``COPY --from`` and ``ADD`` place upstream files; a ``RUN`` step does through
    ``install``, ``cp``, ``mv``, ``curl -o``, ``wget -O``, ``tar -x -C`` and
    ``unzip -d``, also behind shell keywords, in ``case`` branches and in
    ``sh -c``. Destinations under the system binary trees count, except
    ``/usr/share`` and Python package directories.
    """

    installed, copied = set(), set()
    for keyword, arguments in final_stage(text):
        flags, paths, exec_form = _instruction_words(arguments)
        if keyword == "COPY" and not any(flag.startswith("--from=") for flag in flags):
            source = _NOTICE_SOURCE.match(paths[0]) if len(paths) == 2 else None
            if source and paths[1] == f"{NOTICE_DESTINATION}/{source.group(1)}/":
                copied.add(source.group(1))
        elif keyword in ("COPY", "ADD") and len(paths) >= 2:
            installed.add(paths[-1])
        elif keyword == "RUN" and exec_form:
            installed.update(command_destinations(paths))
        elif keyword == "RUN":
            for command in _COMMAND_SEPARATOR.split(arguments):
                installed.update(command_destinations(_words(command)))
    cleaned = {path.rstrip(")]};,") for path in installed}
    return {path for path in cleaned if path.startswith(BINARY_PREFIXES) and not NOT_BINARIES.search(path)}, copied


def dockerfile_binaries(root):
    """Per Dockerfile, the binaries its final stage installs and the notices it copies."""

    found = {}
    for relative in DOCKERFILES:
        path = Path(root) / relative
        if path.is_file():
            found[relative] = stage_contents(path.read_text(encoding="utf-8"))
    return found


def _check_distributed_binaries(root, sections, problems):
    section = sections.get(DISTRIBUTED_BINARIES)
    if section is None:
        return
    images = dockerfile_binaries(root)
    claimed = set()
    named = set()
    for table in component_tables(section):
        for row in table["rows"]:
            if "__malformed__" in row:
                continue
            component = row.get("Component", "?")
            license_expression = row.get("License (SPDX)", "")
            image = next(iter(_CODE_SPAN.findall(row.get("Image", ""))), None)
            if image not in images:
                problems.append(f"{DISTRIBUTED_BINARIES!r} row {component} names no Dockerfile of {list(DOCKERFILES)}")
                continue
            installed, copied = images[image]
            for path in _CODE_SPAN.findall(row.get("Installed as", "")):
                claimed.add((image, path))
                if path not in installed:
                    problems.append(f"{image} does not install {path}, which row {component} names")
            notices = [match.groups() for match in map(_NOTICE_FILE.match, _CODE_SPAN.findall(row.get("Notices", ""))) if match]
            directories = {directory for directory, _ in notices}
            if len(directories) != 1:
                problems.append(
                    f"{DISTRIBUTED_BINARIES!r} row {component} must name files of one {THIRD_PARTY_DIR}/ directory"
                )
                continue
            name = directories.pop()
            named.add(name)
            files = {file for _, file in notices}
            cell = row.get("Notices", "")
            apache = "Apache-2.0" in license_expression and NO_UPSTREAM_NOTICE not in cell
            required = {"LICENSE", "NOTICE"} if apache else {"LICENSE"}
            for file in sorted(required - files):
                problems.append(f"row {component} names no {THIRD_PARTY_DIR}/{name}/{file}")
            directory = root / THIRD_PARTY_DIR / name
            present = {path.name for path in directory.iterdir() if path.is_file()} if directory.is_dir() else set()
            for file in sorted(files - present):
                problems.append(f"{THIRD_PARTY_DIR}/{name}/{file}, named by {component}, does not exist")
            for file in sorted(present - files):
                problems.append(f"{THIRD_PARTY_DIR}/{name}/{file} is not named by row {component}")
            if name not in copied:
                problems.append(
                    f"the final stage of {image} does not copy {THIRD_PARTY_DIR}/{name}/ to {NOTICE_DESTINATION}/{name}/"
                )
    for image, (installed, _) in sorted(images.items()):
        for path in sorted(installed):
            if (image, path) not in claimed:
                problems.append(f"{image} installs {path}, which no row of {DISTRIBUTED_BINARIES!r} names")
    directory = root / THIRD_PARTY_DIR
    if directory.is_dir():
        for name in sorted(path.name for path in directory.iterdir() if path.is_dir()):
            if name not in named:
                problems.append(f"{THIRD_PARTY_DIR}/{name}/ is named by no row of {DISTRIBUTED_BINARIES!r}")


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


def workflow_actions(root):
    """The third-party actions the workflows use, as ``owner/repository``.

    A local ``./`` workflow is project code and a ``docker://`` step names an
    image, so neither is an action; an action in a subdirectory, such as
    ``github/codeql-action/init``, is its repository."""

    workflows = Path(root) / WORKFLOW_DIR
    actions = set()
    for pattern in _WORKFLOW_FILES:
        for path in sorted(workflows.glob(pattern)):
            for reference in _USES.findall(path.read_text(encoding="utf-8")):
                if reference.startswith(("./", "docker://")):
                    continue
                actions.add("/".join(reference.partition("@")[0].split("/")[:2]))
    return actions


def _check_github_actions(root, sections, problems):
    section = sections.get(GITHUB_ACTIONS)
    if section is None or not (Path(root) / WORKFLOW_DIR).is_dir():
        return
    documented = set(component_keys(section))
    used = workflow_actions(root)
    for action in sorted(used - documented):
        problems.append(f"{WORKFLOW_DIR} uses {action!r}, which is not in the {GITHUB_ACTIONS!r} section")
    for stale in sorted(documented - used):
        problems.append(f"{GITHUB_ACTIONS!r} lists {stale!r}, which no workflow uses")


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
    _check_distributed_binaries(root, sections, problems)
    _check_appliance(root, sections, problems)
    _check_github_actions(root, sections, problems)
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
        print(
            f"{INVENTORY}: every direct dependency, optional package, GitHub Action, "
            "vendored asset and shipped binary's notices are documented."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
