# SPDX-FileCopyrightText: 2026 basecubedev
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
import importlib.util
import json
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
    PYTHON_DEV_MANIFESTS,
    PYTHON_RUNTIME_MANIFESTS,
    RUNTIME_DIRECT,
    VENDORED,
    collect_problems,
    component_keys,
    fix_versions,
    python_version_cell,
    workflow_action_versions,
    normalize_python_name,
    DOCKERFILES,
    dockerfile_binaries,
    final_stage,
    parse_inventory,
    read_package_json,
    read_python_manifest,
    vendored_static_files,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]

_SPEC = importlib.util.spec_from_file_location("binary_notices", Path(__file__).resolve().parents[1] / "scripts" / "binary_notices.py")
binary_notices = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(binary_notices)

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "THIRD_PARTY_LICENSES.md"
CHECKER = ROOT / "tools" / "check_third_party_licenses.py"
UPLOT_LICENSE = ROOT / "dashboard" / "static" / "uPlot.LICENSE"

COPIED_FILES = (
    "THIRD_PARTY_LICENSES.md",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-docs.txt",
    "deploy/admin/requirements.txt",
    "package.json",
    "package-lock.json",
    "Dockerfile",
    "deploy/admin/Dockerfile",
)
COPIED_TREES = ("dashboard/static", "admin/static", ".github/workflows", "third_party")

# Image references that belong to this project rather than a third party.
OWN_IMAGE_PREFIXES = ("ghcr.io/basecubedev/", "ems-solarflow-")

_FROM = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
_IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.MULTILINE)
_HASH_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|$", re.MULTILINE)


@pytest.fixture
def repo_copy(tmp_path):
    """A minimal copy of the inputs the checker reads, its Version cells already rewritten.

    A Dependabot commit carries a stale cell until its refresh commit arrives.
    That cell fails the tests of the committed inventory; a test of a copy sees
    only the change it makes itself.
    """

    root = tmp_path / "repo"
    for relative in COPIED_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    for relative in COPIED_TREES:
        shutil.copytree(ROOT / relative, root / relative)
    fix_versions(root)
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
            "requirements-docs.txt",
            "deploy/admin/requirements.txt",
        )
        for name in read_python_manifest(ROOT / manifest)
    }
    assert declared, "no Python dependency was parsed at all"
    assert declared <= documented, sorted(declared - documented)


def test_the_checker_reads_every_requirements_file():
    """A requirements file the checker does not know about declares
    dependencies nothing compares with the inventory."""

    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*requirements*.txt"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert listed
    assert set(listed) == set(PYTHON_RUNTIME_MANIFESTS) | set(PYTHON_DEV_MANIFESTS)


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


def test_every_vendored_file_matches_its_recorded_hash():
    recorded = dict(_HASH_ROW.findall(INVENTORY.read_text(encoding="utf-8")))
    notices = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "third_party").rglob("*")
        if path.is_file() and path.name != "THIRD_PARTY_NOTICES"
    }
    assert set(recorded) == {"dashboard/static/uPlot.iife.min.js", "dashboard/static/uPlot.min.css"} | notices
    for relative, digest in recorded.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == digest, relative


# --- the notices of shipped binaries -----------------------------------------


def test_both_images_ship_the_project_license_and_the_inventory():
    for relative in ("Dockerfile", "deploy/admin/Dockerfile"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert re.search(r"^COPY LICENSE THIRD_PARTY_LICENSES\.md \./$", text, re.MULTILINE), relative


def test_every_binary_an_image_installs_is_found_with_the_notices_beside_it():
    assert dockerfile_binaries(ROOT) == {
        "Dockerfile": ({"/usr/local/bin/influx"}, {"influx-cli"}),
        "deploy/admin/Dockerfile": (
            {"/usr/local/bin/docker", "/usr/local/lib/docker/cli-plugins/docker-compose"},
            {"docker-cli", "docker-compose"},
        ),
    }
    assert not [
        problem
        for problem in collect_problems(ROOT)
        if "has Version" not in problem and ("third_party" in problem or "Dockerfile" in problem)
    ]


def test_the_notices_are_of_the_versions_the_dockerfiles_pin():
    """A version bump in a Dockerfile fails here until the inventory records the new version.

    Recording it is the moment to refresh the third_party/ files from that
    version; their SHA-256 rows then show the new bytes in review. The bytes
    themselves are compared with upstream by nothing offline.
    """

    admin = (ROOT / "deploy" / "admin" / "Dockerfile").read_text(encoding="utf-8")
    ems = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    rows = {
        row["Component"].strip("`"): row["Version"]
        for table in sections()["Distributed Third-Party Binaries"].tables
        if "Component" in table["columns"]
        for row in table["rows"]
    }
    pins = {
        "docker/cli": re.search(r"^ARG DOCKER_CLI_VERSION=(\S+)$", admin, re.MULTILINE).group(1),
        "docker/compose": re.search(r"^ARG DOCKER_COMPOSE_VERSION=(\S+)$", admin, re.MULTILINE).group(1),
    }
    for component, version in pins.items():
        assert rows[component] == version, f"refresh third_party/ for {component} {version}"
    influx_stage = re.search(r"^FROM (influxdb:\S+) AS influxdb-cli$", ems, re.MULTILINE).group(1)
    assert f"`{influx_stage}`" in rows["influx"], f"refresh third_party/influx-cli for {influx_stage}"


def mutate(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def problems_about(repo_copy, *needles):
    return [problem for problem in collect_problems(repo_copy) if all(needle in problem for needle in needles)]


def test_a_shipped_binary_without_its_license_text_is_rejected(repo_copy):
    (repo_copy / "third_party" / "influx-cli" / "LICENSE").unlink()
    assert problems_about(repo_copy, "third_party/influx-cli/LICENSE", "does not exist")


def test_notices_the_final_stage_does_not_copy_are_rejected(repo_copy):
    mutate(repo_copy / "Dockerfile", "COPY third_party/influx-cli/ /usr/share/doc/influx-cli/\n", "")
    assert problems_about(repo_copy, "final stage of Dockerfile does not copy third_party/influx-cli/")


def test_notices_copied_only_in_a_discarded_build_stage_are_rejected(repo_copy):
    dockerfile = repo_copy / "Dockerfile"
    mutate(dockerfile, "COPY third_party/influx-cli/ /usr/share/doc/influx-cli/\n", "")
    mutate(
        dockerfile,
        "AS influxdb-cli\n",
        "AS influxdb-cli\nCOPY third_party/influx-cli/ /usr/share/doc/influx-cli/\n",
    )
    assert problems_about(repo_copy, "final stage of Dockerfile does not copy third_party/influx-cli/")


def test_notices_copied_into_the_other_image_are_rejected(repo_copy):
    line = "COPY third_party/docker-cli/ /usr/share/doc/docker-cli/\n"
    mutate(repo_copy / "deploy" / "admin" / "Dockerfile", line, "")
    mutate(repo_copy / "Dockerfile", "COPY third_party/influx-cli/", line + "COPY third_party/influx-cli/")
    assert problems_about(repo_copy, "final stage of deploy/admin/Dockerfile does not copy third_party/docker-cli/")


def test_a_binary_no_row_names_is_rejected(repo_copy):
    mutate(
        repo_copy / "Dockerfile",
        "COPY --from=influxdb-cli /usr/local/bin/influx /usr/local/bin/influx\n",
        "COPY --from=influxdb-cli /usr/local/bin/influx /usr/local/bin/influx\n"
        "COPY --from=influxdb-cli /usr/local/bin/influxd /usr/local/bin/influxd\n",
    )
    assert problems_about(repo_copy, "Dockerfile installs /usr/local/bin/influxd", "no row")


def test_an_apache_component_without_its_notice_is_rejected(repo_copy):
    (repo_copy / "third_party" / "docker-cli" / "NOTICE").unlink()
    mutate(repo_copy / "THIRD_PARTY_LICENSES.md", "`third_party/docker-cli/LICENSE`, `third_party/docker-cli/NOTICE`", "`third_party/docker-cli/LICENSE`")
    assert problems_about(repo_copy, "row `docker/cli` names no third_party/docker-cli/NOTICE")


def test_a_notice_file_no_row_names_is_rejected(repo_copy):
    (repo_copy / "third_party" / "influx-cli" / "AUTHORS").write_text("someone\n", encoding="utf-8")
    assert problems_about(repo_copy, "third_party/influx-cli/AUTHORS is not named")


def test_a_row_without_notice_files_is_rejected(repo_copy):
    mutate(
        repo_copy / "THIRD_PARTY_LICENSES.md",
        "`/usr/local/bin/influx` | `third_party/influx-cli/LICENSE`, `third_party/influx-cli/THIRD_PARTY_NOTICES` |",
        "`/usr/local/bin/influx` | none |",
    )
    assert problems_about(repo_copy, "row `influx` must name files of one third_party/ directory")


def _binary_rows():
    return [
        row
        for table in sections()["Distributed Third-Party Binaries"].tables
        if "Component" in table["columns"]
        for row in table["rows"]
    ]


def test_the_generated_notices_are_for_the_versions_the_dockerfiles_pin():
    """A pin that moves without its notices fails here, before an image build does."""

    admin = (ROOT / "deploy" / "admin" / "Dockerfile").read_text(encoding="utf-8")
    header = {
        name: binary_notices.parse_notices((ROOT / "third_party" / name / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8"))[0]
        for name in ("influx-cli", "docker-cli", "docker-compose")
    }
    for name, argument in (("docker-cli", "DOCKER_CLI_VERSION"), ("docker-compose", "DOCKER_COMPOSE_VERSION")):
        pinned = re.search(rf"^ARG {argument}=(\S+)$", admin, re.MULTILINE).group(1)
        assert header[name]["version"] == pinned, f"regenerate: scripts/binary_notices.py generate --component {name}"
    influx = next(row for row in _binary_rows() if row["Component"] == "`influx`")
    assert f"`{header['influx-cli']['identity'][:7]}`" in influx["Version"]


def test_each_image_build_checks_the_binaries_it_ships_against_their_notices():
    for row in _binary_rows():
        image = row["Image"].strip("`")
        installed = row["Installed as"].strip("`")
        name = re.search(r"third_party/([\w.-]+)/", row["Notices"]).group(1)
        stage = " ".join(
            arguments for keyword, arguments in final_stage((ROOT / image).read_text(encoding="utf-8")) if keyword == "RUN"
        )
        expected = (
            f"python3 /tmp/binary_notices.py check --component {name} --binary {installed} "
            f"--notices /usr/share/doc/{name}/THIRD_PARTY_NOTICES"
        )
        assert expected in " ".join(stage.split()), (image, name)


def test_the_checker_reads_every_dockerfile_this_repository_builds():
    built = {path for path in tracked_files() if Path(path).name.startswith("Dockerfile") and not path.startswith("tests/fixtures/")}
    assert built == set(DOCKERFILES)


@pytest.mark.parametrize(
    "line",
    (
        "RUN curl -fsSL https://example.org/tool -o /usr/local/bin/tool && \\\n    chmod 0755 /usr/local/bin/tool\n",
        "COPY --from=influxdb-cli --chmod=0755 /usr/local/bin/tool /usr/local/bin/tool\n",
        "COPY --link --from=influxdb-cli /usr/local/bin/a /usr/local/bin/tool\n",
        "ADD --chmod=0755 https://example.org/tool /usr/local/bin/tool\n",
        "RUN install -Dm755 /tmp/tool /usr/local/bin/tool\n",
        "RUN mv /tmp/tool /usr/local/bin/tool\n",
        "RUN wget -qO /usr/local/bin/tool https://example.org/tool\n",
        "RUN tar -xzf /tmp/tool.tgz -C /usr/local/bin/tool\n",
        'RUN if [ "$TARGETARCH" = arm64 ]; then curl -fsSL https://example.org/t -o /usr/local/bin/tool; fi\n',
        "RUN for name in tool; do install -m 0755 /tmp/tool /usr/local/bin/tool; done\n",
        'RUN case "$TARGETARCH" in amd64) true ;; arm64) curl -o /usr/local/bin/tool https://example.org/t ;; esac\n',
        "RUN sh -c 'curl -fsSL https://example.org/t -o /usr/local/bin/tool'\n",
        'RUN ["curl", "-fsSL", "https://example.org/t", "-o", "/usr/local/bin/tool"]\n',
        "RUN (cd /tmp && install -m 0755 tool /usr/local/bin/tool)\n",
        'COPY --from=influxdb-cli ["/usr/local/bin/influx", "/usr/local/bin/tool"]\n',
    ),
)
def test_a_binary_placed_in_any_usual_way_needs_a_row(repo_copy, line):
    mutate(repo_copy / "Dockerfile", "COPY third_party/influx-cli/", line + "COPY third_party/influx-cli/")
    assert problems_about(repo_copy, "Dockerfile installs /usr/local/bin/tool", "no row")


@pytest.mark.parametrize(
    "line",
    (
        "COPY --from=influxdb-cli /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages\n",
        "RUN cp /tmp/tool /usr/share/ems/tool\n",
        "RUN tar -czf /tmp/tool.tgz -C /usr/local/lib .\n",
        "RUN pip install --prefix=/usr/local tool && mkdir -p /usr/local/lib/tool && ln -s /a /usr/local/bin/b\n",
    ),
)
def test_what_is_not_a_prebuilt_binary_needs_no_row(repo_copy, line):
    mutate(repo_copy / "Dockerfile", "COPY third_party/influx-cli/", line + "COPY third_party/influx-cli/")
    assert not problems_about(repo_copy, "Dockerfile installs")


def test_notices_left_behind_by_a_lowercase_final_from_are_rejected(repo_copy):
    mutate(repo_copy / "Dockerfile", "ENV EMS_IN_CONTAINER=1\n", "from python:3.14-slim\nENV EMS_IN_CONTAINER=1\n")
    assert problems_about(repo_copy, "final stage of Dockerfile does not copy third_party/influx-cli/")


def test_an_apache_component_whose_upstream_has_no_notice_says_so(repo_copy):
    (repo_copy / "third_party" / "docker-cli" / "NOTICE").unlink()
    mutate(
        repo_copy / "THIRD_PARTY_LICENSES.md",
        "`third_party/docker-cli/LICENSE`, `third_party/docker-cli/NOTICE`",
        "`third_party/docker-cli/LICENSE` (no NOTICE upstream)",
    )
    assert not problems_about(repo_copy, "docker-cli")


def test_a_notice_directory_no_row_names_is_rejected(repo_copy):
    extra = repo_copy / "third_party" / "unlisted"
    extra.mkdir()
    (extra / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    assert problems_about(repo_copy, "third_party/unlisted/", "named by no row")


# --- the checker actually rejects drift --------------------------------------


def test_an_undeclared_python_runtime_dependency_is_rejected(repo_copy):
    append(repo_copy / "requirements.txt", "\nhttpx>=0.27\n")
    assert any("httpx" in problem for problem in collect_problems(repo_copy))


def test_an_undeclared_python_development_dependency_is_rejected(repo_copy):
    append(repo_copy / "requirements-dev.txt", "\nmypy\n")
    assert any("mypy" in problem for problem in collect_problems(repo_copy))


def test_an_undeclared_documentation_dependency_is_rejected(repo_copy):
    append(repo_copy / "requirements-docs.txt", "\nmkdocs-material\n")
    assert any("mkdocs-material" in problem for problem in collect_problems(repo_copy))


def test_an_undeclared_node_dependency_is_rejected(repo_copy):
    """Mutate the manifest structurally, never by matching a pinned version.

    A textual replacement that names the current version silently stops
    mutating anything the moment the dependency is bumped: the checker then has
    nothing to reject and the test fails while the inventory is in fact intact.
    """

    package = repo_copy / "package.json"
    manifest = json.loads(package.read_text(encoding="utf-8"))
    manifest["devDependencies"]["eslint"] = "^9.0.0"
    package.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assert any("eslint" in problem for problem in collect_problems(repo_copy))


def test_a_stale_inventory_entry_is_rejected(repo_copy):
    manifest = repo_copy / "requirements-dev.txt"
    manifest.write_text("pytest\nruff\n", encoding="utf-8")
    assert any("pyyaml" in problem for problem in collect_problems(repo_copy))


def test_an_undocumented_vendored_asset_is_rejected(repo_copy):
    (repo_copy / "dashboard" / "static" / "chart.min.js").write_text(
        "/*! some third-party chart bundle */", encoding="utf-8"
    )
    assert any("chart.min.js" in problem for problem in collect_problems(repo_copy))


def test_an_undocumented_optional_platform_package_is_rejected(repo_copy):
    """Anchored on the packages map itself, not on a real optional package:
    the lockfile marks none at present, and the last one left with a
    Playwright bump -- an anchor on it would have matched nothing and the
    test would have passed a lockfile it never changed."""

    lock = repo_copy / "package-lock.json"
    text = lock.read_text(encoding="utf-8")
    assert text.count('"packages": {') == 1
    lock.write_text(
        text.replace(
            '"packages": {',
            '"packages": {\n'
            '    "node_modules/other-watcher": {\n'
            '      "version": "1.0.0",\n'
            '      "dev": true,\n'
            '      "optional": true,\n'
            '      "os": ["darwin"]\n'
            "    },",
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


def test_the_copied_workflows_have_no_action_problem(repo_copy):
    assert not any("GitHub Actions" in problem or "workflows" in problem for problem in collect_problems(repo_copy))


def test_an_undocumented_github_action_is_rejected(repo_copy):
    (repo_copy / ".github" / "workflows" / "added.yaml").write_text(
        "jobs:\n  added:\n    steps:\n      - uses: example/new-action/setup@v1\n"
        "      - uses: ./.github/workflows/local.yml\n"
        "      - uses: docker://alpine:3.20\n",
        encoding="utf-8",
    )
    problems = [problem for problem in collect_problems(repo_copy) if "GitHub Actions" in problem]

    assert problems == [".github/workflows uses 'example/new-action', which is not in the 'GitHub Actions' section"]


def test_a_github_action_no_workflow_uses_is_rejected(repo_copy):
    inventory = repo_copy / "THIRD_PARTY_LICENSES.md"
    text = inventory.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `actions/checkout`"))
    inventory.write_text(text.replace(row, row + "\n" + row.replace("actions/checkout", "example/unused-action")), encoding="utf-8")

    assert any("example/unused-action" in problem for problem in collect_problems(repo_copy))


# --- the Version cells the manifests decide ----------------------------------


def row_of(root, component):
    """The inventory rows whose Component cell starts with ``component``, as cell lists."""

    lines = (root / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8").splitlines()
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines if line.startswith(f"| {component}")]


def lock_package(root, name, **changes):
    lock = root / "package-lock.json"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["packages"][f"node_modules/{name}"].update(changes)
    lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def declare_node(root, name, specifier):
    package = root / "package.json"
    manifest = json.loads(package.read_text(encoding="utf-8"))
    manifest["devDependencies"][name] = specifier
    package.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def version_problems(root):
    return [problem for problem in collect_problems(root) if "has Version" in problem]


def test_a_requirement_bump_is_rejected_until_the_rewrite_takes_it(repo_copy):
    manifest = repo_copy / "requirements-docs.txt"
    manifest.write_text(re.sub(r"zensical==\S+", "zensical==9.9.9", manifest.read_text(encoding="utf-8")), encoding="utf-8")

    assert [problem for problem in version_problems(repo_copy) if "zensical" in problem]
    assert fix_versions(repo_copy) == 1
    assert collect_problems(repo_copy) == []
    assert row_of(repo_copy, "`zensical`")[0][1] == "`==9.9.9`"


def test_a_verified_note_stays_until_the_constraint_it_was_verified_against_changes(repo_copy):
    assert row_of(repo_copy, "`pytest`")[0][1].startswith("unpinned (verified ")

    manifest = repo_copy / "requirements-dev.txt"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("pytest\n", "pytest>=99\n"), encoding="utf-8")
    fix_versions(repo_copy)

    assert row_of(repo_copy, "`pytest`")[0][1] == "`>=99`"
    assert collect_problems(repo_copy) == []


def test_a_package_two_manifests_declare_differently_names_each_constraint():
    assert python_version_cell([("requirements.txt", ">=1"), ("deploy/admin/requirements.txt", ">=1")]) == "`>=1`"
    assert python_version_cell([("requirements.txt", "")]) == "unpinned"
    assert (
        python_version_cell([("requirements.txt", ">=1"), ("deploy/admin/requirements.txt", ">=1,<2")])
        == "`>=1` in requirements.txt, `>=1,<2` in deploy/admin/requirements.txt"
    )


def test_an_npm_bump_rewrites_every_row_that_names_the_package_and_nothing_else(repo_copy):
    inventory = repo_copy / "THIRD_PARTY_LICENSES.md"
    before = inventory.read_text(encoding="utf-8").splitlines(keepends=True)
    declare_node(repo_copy, "lucide-static", "9.8.7")
    lock_package(repo_copy, "lucide-static", version="9.8.7")

    assert len([problem for problem in version_problems(repo_copy) if "lucide" in problem.lower()]) == 2
    assert fix_versions(repo_copy) == 2

    after = inventory.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = [index for index, (old, new) in enumerate(zip(before, after)) if old != new]
    assert len(before) == len(after) and len(changed) == 2
    for index in changed:
        old, new = before[index].split("|"), after[index].split("|")
        assert [cell for position, cell in enumerate(old) if position != 2] == [
            cell for position, cell in enumerate(new) if position != 2
        ]
    assert row_of(repo_copy, "`lucide-static`")[0][1] == "`9.8.7`"
    assert row_of(repo_copy, "`Lucide` (`lucide-static`)")[0][1] == "9.8.7"
    assert collect_problems(repo_copy) == []


def test_a_range_shows_the_version_the_lockfile_holds(repo_copy):
    declare_node(repo_copy, "vite", "^8.0.0")
    lock_package(repo_copy, "vite", version="8.9.1")
    fix_versions(repo_copy)

    assert row_of(repo_copy, "`vite` |")[0][1] == "`^8.0.0` (locked 8.9.1)"
    assert row_of(repo_copy, "`vite` (module-preload helper)")[0][1] == "8.9.1"
    assert collect_problems(repo_copy) == []


def test_the_browser_builds_follow_the_locked_playwright_driver(repo_copy):
    lock_package(repo_copy, "playwright-core", version="1.99.0")
    fix_versions(repo_copy)

    assert row_of(repo_copy, "`Chromium`")[0][1] == "Playwright build for 1.99.0"
    assert row_of(repo_copy, "`Firefox`")[0][1] == "Playwright build for 1.99.0, plus the host Firefox"
    assert row_of(repo_copy, "`playwright-core`")[0][1] == "1.99.0"


def test_a_font_package_bump_keeps_the_typeface_version_beside_it(repo_copy):
    declare_node(repo_copy, "@fontsource-variable/inter", "5.4.0")
    lock_package(repo_copy, "@fontsource-variable/inter", version="5.4.0")
    fix_versions(repo_copy)

    assert row_of(repo_copy, "`Inter` (`@fontsource-variable/inter`)")[0][1].endswith(" via 5.4.0")
    assert collect_problems(repo_copy) == []


def test_a_changed_npm_license_is_rejected_and_never_rewritten(repo_copy):
    lock_package(repo_copy, "svelte", license="GPL-3.0-only")
    fix_versions(repo_copy)

    assert row_of(repo_copy, "`svelte`")[0][2] == "MIT"
    assert any("svelte" in problem and "GPL-3.0-only" in problem for problem in collect_problems(repo_copy))


def test_an_action_bump_rewrites_the_major_versions_the_workflows_use(repo_copy):
    workflows = sorted((repo_copy / ".github" / "workflows").glob("*.yml"))
    for path in workflows:
        path.write_text(path.read_text(encoding="utf-8").replace("actions/setup-node@v7", "actions/setup-node@v8"), encoding="utf-8")
    fix_versions(repo_copy)
    assert row_of(repo_copy, "`actions/setup-node`")[0][1] == "v8"

    append(workflows[0], "\n      - uses: actions/setup-node@" + "0" * 40 + "  # v9.1.0\n")
    fix_versions(repo_copy)
    assert row_of(repo_copy, "`actions/setup-node`")[0][1] == "v8, v9"
    assert collect_problems(repo_copy) == []


@pytest.mark.parametrize(
    ("reference", "major"),
    [
        ("example/tool@v7", "v7"),
        ("example/tool@v7.1.2", "v7"),
        ("example/tool@2.0.1", "2"),
        ("example/tool@main", "main"),
        ("example/tool@" + "a" * 40 + "  # v8.0.1", "v8"),
        ("example/tool@" + "a" * 40 + "  # pin@v9.2.0", "v9"),
        ("example/tool/sub@" + "a" * 40 + " # tag=v10", "v10"),
    ],
)
def test_the_major_version_is_read_from_every_form_a_workflow_writes(tmp_path, reference, major):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(f"jobs:\n  a:\n    steps:\n      - uses: {reference}\n", encoding="utf-8")

    assert workflow_action_versions(tmp_path) == {"example/tool": {major}}


def test_the_rewrite_settles_in_one_pass(repo_copy):
    lock_package(repo_copy, "playwright", version="1.99.0")
    fix_versions(repo_copy)
    settled = (repo_copy / "THIRD_PARTY_LICENSES.md").read_bytes()

    assert fix_versions(repo_copy) == 0
    assert (repo_copy / "THIRD_PARTY_LICENSES.md").read_bytes() == settled


def test_the_checker_command_rewrites_versions_before_it_checks(repo_copy):
    lock_package(repo_copy, "playwright", version="1.99.0")
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(repo_copy), "--fix-versions"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "rewrote 1 Version cells" in result.stdout
