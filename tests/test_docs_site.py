# SPDX-License-Identifier: AGPL-3.0-or-later
"""The documentation website: link staging, link check, navigation and workflow.

``scripts/docs_site.py`` turns links written for GitHub's file view into links
the website can serve, and checks the built site. The navigation in
``zensical.toml`` and the workflow that publishes the site are paired with the
pages and the script by the tests at the end. See docs/developer/docs-site.md.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
WORKFLOWS = ROOT / ".github" / "workflows"
CHECK_WORKFLOW = WORKFLOWS / "docs-site.yml"
PUBLISH_WORKFLOW = WORKFLOWS / "docs-site-publish.yml"

_spec = importlib.util.spec_from_file_location("docs_site", ROOT / "scripts" / "docs_site.py")
docs_site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(docs_site)

REPOSITORY = docs_site.project()["repo_url"]
BRANCH = docs_site.branch()

# Pages under docs/ the navigation leaves out on purpose. Redirect stubs, whose
# heading is "# Moved", are recognised from their content and need no entry.
NOT_IN_NAV = {
    "assets/media/admin/README.md": "how the Admin media were captured, beside the files",
    "assets/screenshots/admin/README.md": "how the Admin screenshots were captured, beside the files",
    "assets/screenshots/appliance/README.md": "how the appliance screenshots were captured, beside the files",
    "assets/screenshots/dashboard/README.md": "how the dashboard screenshots were captured, beside the files",
    "assets/videos/admin/README.md": "how the Admin videos were rendered, beside the files",
    "demo/README.md": "how the install demo animation is generated, beside its script",
}


def blob(path):
    return f"{REPOSITORY}/blob/{BRANCH}/{path}"


# --- rewrite_target ------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "target", "expected"),
    [
        ("technical/system-build-pairing.md", "../../admin/system_build.py", blob("admin/system_build.py")),
        ("technical/system-build-pairing.md", "../../admin/system_build.py#L10", blob("admin/system_build.py") + "#L10"),
        ("README.md", "../README.md", blob("README.md")),
        ("README.md", "../tests/", f"{REPOSITORY}/tree/{BRANCH}/tests"),
        ("README.md", "technical/", f"{REPOSITORY}/tree/{BRANCH}/docs/technical"),
        ("README.md", "user/admin/", "user/admin/index.md"),
        ("README.md", "user/admin#guides", "user/admin/index.md#guides"),
        ("README.md", "developer/design-notes/", "developer/design-notes/README.md"),
        ("README.md", "../docs/user/faq.md", "user/faq.md"),
        ("user/faq.md", "../../docs/user/admin/#guides", "admin/index.md#guides"),
        ("README.md", "<../README.md>", f"<{blob('README.md')}>"),
    ],
)
def test_a_link_the_site_cannot_serve_is_rewritten(page, target, expected):
    assert docs_site.rewrite_target(target, DOCS / page) == expected


def test_an_image_outside_docs_uses_the_raw_file():
    assert docs_site.rewrite_target("../README.md", DOCS / "README.md", image=True) == (
        f"{REPOSITORY}/raw/{BRANCH}/README.md"
    )


@pytest.mark.parametrize(
    "target",
    [
        "user/faq.md",
        "user/faq.md#admin",
        "#operating-models",
        "https://example.org/page.md",
        "mailto:someone@example.org",
        "/absolute/path.md",
        "missing.md",
        "../missing.py",
        "../../",
    ],
)
def test_a_link_the_site_can_serve_or_nobody_can_is_left_alone(target):
    assert docs_site.rewrite_target(target, DOCS / "README.md") == target


# --- rewrite_page --------------------------------------------------------------


def test_every_link_form_outside_code_is_rewritten():
    readme = blob("README.md")
    raw = f"{REPOSITORY}/raw/{BRANCH}/README.md"
    text = (
        "See [the README](../README.md \"Root\") and ![logo](../README.md).\n"
        "Inline `[x](../README.md)` stays, [`README.md`](../README.md) does not.\n"
        "```markdown\n"
        "[x](../README.md)\n"
        "```\n"
        "[single](../README.md 'Root'), [paren](../README.md (Root)) and [angle](<../README.md>).\n"
        "[![badge](../README.md)](../README.md)\n"
        "[root]: ../README.md \"Root\"\n"
        "[^note]: ../README.md is a footnote\n"
        '<a href="../README.md">raw</a> <img src="../README.md"> `<a href="../README.md">`\n'
        '<video poster="../README.md"></video>\n'
    )

    lines = docs_site.rewrite_page(text, DOCS / "README.md").splitlines()

    assert lines == [
        f"See [the README]({readme} \"Root\") and ![logo]({raw}).",
        f"Inline `[x](../README.md)` stays, [`README.md`]({readme}) does not.",
        "```markdown",
        "[x](../README.md)",
        "```",
        f"[single]({readme} 'Root'), [paren]({readme} (Root)) and [angle](<{readme}>).",
        f"[![badge]({raw})]({readme})",
        f"[root]: {readme} \"Root\"",
        "[^note]: ../README.md is a footnote",
        f'<a href="{readme}">raw</a> <img src="{raw}"> `<a href="../README.md">`',
        f'<video poster="{raw}"></video>',
    ]


# --- stage ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    return docs_site.stage(tmp_path_factory.mktemp("docs-site") / "stage")


# Deliberately not the script's own patterns: any target in a staged page that
# looks like a link, in Markdown or in raw HTML, outside fenced code.
_FENCED = re.compile(r"^ {0,3}(```|~~~).*?^ {0,3}\1", re.M | re.S)
_ANY_TARGET = re.compile(
    r"\]\(\s*<?([^)>\s]+)"
    r"|^ {0,3}\[[^\]^][^\]]*\]:\s*<?([^>\s]+)"
    r"|\b(?:href|src|poster)=\"([^\"]+)\"",
    re.M,
)


def _relative_targets(page):
    text = _FENCED.sub("", page.read_text(encoding="utf-8"))
    text = re.sub(r"`[^`\n]*`", "", text)
    for match in _ANY_TARGET.finditer(text):
        target = next(group for group in match.groups() if group)
        if not re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", target, re.I):
            yield target


def test_every_relative_link_of_the_stage_stays_inside_it(staged):
    escaping = []
    checked = 0
    for page in staged.rglob("*.md"):
        for target in _relative_targets(page):
            checked += 1
            resolved = (page.parent / target.partition("#")[0]).resolve()
            if not resolved.is_relative_to(staged.resolve()) or not resolved.is_file():
                escaping.append(f"{page.relative_to(staged)}: {target}")

    assert checked > 500, "the pattern no longer finds the links of the stage"
    assert escaping == []


def test_the_stage_holds_exactly_the_docs_files(staged):
    stage_files = {
        path.relative_to(staged) for path in staged.rglob("*") if path.is_file() and path.name != docs_site.STAGE_MARKER
    }

    assert stage_files == {path.relative_to(DOCS) for path in docs_site.docs_files()}


def test_staging_again_replaces_its_own_stage(tmp_path):
    destination = docs_site.stage(tmp_path / "stage")
    (destination / "stale.md").write_text("left over\n", encoding="utf-8")

    docs_site.stage(destination)

    assert not (destination / "stale.md").exists()
    assert (destination / "README.md").is_file()


def test_staging_refuses_a_directory_it_did_not_create(tmp_path):
    (tmp_path / "keep.txt").write_text("not ours\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        docs_site.stage(tmp_path)

    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "not ours\n"


def test_a_gitignored_file_under_docs_is_not_staged(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/docs/develop/\n", encoding="utf-8")
    (tmp_path / "docs" / "develop").mkdir(parents=True)
    (tmp_path / "docs" / "page.md").write_text("# Page\n", encoding="utf-8")
    (tmp_path / "docs" / "develop" / "note.md").write_text("# Local note\n", encoding="utf-8")
    monkeypatch.setattr(docs_site, "ROOT", tmp_path)

    assert docs_site.docs_files() == [tmp_path / "docs" / "page.md"]


# --- check ---------------------------------------------------------------------


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")


def test_check_reports_every_link_that_names_no_file_or_anchor(tmp_path):
    base = docs_site.project()["site_url"].split("://", 1)[1].partition("/")[2]
    site = tmp_path / "site"
    _write(site / "page.html", '<meta name="description"><h2 id="there">There</h2><a name="old"></a>')
    _write(site / "sub" / "index.html", "")
    _write(site / "assets" / "clip.mp4", "")
    _write(
        site / "index.html",
        '<a href="page.html">ok</a>'
        '<a href="page.html#there">ok</a>'
        '<a href="page.html#old">ok</a>'
        '<a href="sub/">ok</a>'
        f'<a href="/{base}page.html">ok</a>'
        '<a href="https://example.org/missing.html">external</a>'
        '<video poster="assets/clip.mp4"><source src="assets/clip.mp4"></video>'
        '<a href="page.html#missing">bad anchor</a>'
        '<a href="page.html#description">a meta name is no anchor</a>'
        '<a href="#nowhere">bad same-page anchor</a>'
        '<a href="missing.html">bad file</a>'
        '<img src="assets/missing.png">'
        '<video poster="assets/missing.png"></video>'
        '<a href="/elsewhere/page.html">outside the site path</a>'
        '<a href="../outside.html">outside the site</a>',
    )
    (tmp_path / "outside.html").write_text("", encoding="utf-8")

    problems = docs_site.check(site)

    assert [problem.split(": ", 1)[1] for problem in problems] == [
        "page.html#missing names no anchor of page.html",
        "page.html#description names no anchor of page.html",
        "#nowhere names no anchor of index.html",
        "missing.html names no file of the site",
        "assets/missing.png names no file of the site",
        "assets/missing.png names no file of the site",
        f"/elsewhere/page.html is outside the site path /{base}",
        "../outside.html names no file of the site",
    ]


def test_check_needs_a_built_site(tmp_path):
    with pytest.raises(SystemExit):
        docs_site.check(tmp_path / "missing")


# --- navigation ----------------------------------------------------------------


def _nav_pages(entries):
    for entry in entries:
        if isinstance(entry, str):
            yield entry
            continue
        for value in entry.values():
            yield from _nav_pages(value) if isinstance(value, list) else [value]


def _docs_pages():
    return {path.relative_to(DOCS).as_posix() for path in docs_site.docs_files() if path.suffix == ".md"}


def _is_redirect_stub(page):
    return (DOCS / page).read_text(encoding="utf-8").startswith("# Moved\n")


def test_every_navigation_entry_is_a_page():
    nav = list(_nav_pages(docs_site.project()["nav"]))

    assert sorted(set(nav) - _docs_pages()) == []
    assert len(nav) == len(set(nav)), "a page is listed twice in the navigation"


def test_every_page_is_in_the_navigation_or_a_named_exception():
    nav = set(_nav_pages(docs_site.project()["nav"]))
    left_out = {page for page in _docs_pages() - nav if not _is_redirect_stub(page)}

    assert left_out == set(NOT_IN_NAV)


def test_every_named_exception_is_a_page_left_out():
    nav = set(_nav_pages(docs_site.project()["nav"]))

    assert set(NOT_IN_NAV) <= _docs_pages()
    assert set(NOT_IN_NAV) & nav == set()


# --- the workflows and the ignore lists -----------------------------------------


def _workflow(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(path):
    document = _workflow(path)
    return document.get("on", document.get(True))


def _build_steps(path):
    return _workflow(path)["jobs"]["build"]["steps"]


def _commands(path):
    return [step["run"] for step in _build_steps(path) if "run" in step]


def test_every_pull_request_and_main_build_the_site():
    assert set(_triggers(CHECK_WORKFLOW)) == {"pull_request"}
    assert set(_triggers(PUBLISH_WORKFLOW)) == {"push", "workflow_dispatch"}
    assert _triggers(PUBLISH_WORKFLOW)["push"] == {"branches": [BRANCH]}


def test_main_builds_exactly_what_pull_requests_check():
    """A difference here, a Python version say, would break the build on main
    only, where no pull request runs it first."""

    publish = [step for step in _build_steps(PUBLISH_WORKFLOW) if "upload-pages-artifact" not in step.get("uses", "")]

    assert publish == _build_steps(CHECK_WORKFLOW)


def test_both_workflows_build_the_way_the_documentation_says():
    commands = [
        "python -m pip install -r requirements-docs.txt",
        "python3 scripts/docs_site.py stage",
        "zensical build --clean --strict",
        "python3 scripts/docs_site.py check",
    ]

    assert _commands(CHECK_WORKFLOW) == commands
    assert _commands(PUBLISH_WORKFLOW) == commands
    guide = (DOCS / "developer" / "docs-site.md").read_text(encoding="utf-8")
    for command in commands[1:]:
        assert command in guide


def test_only_main_publishes_the_site_it_built():
    jobs = _workflow(PUBLISH_WORKFLOW)["jobs"]
    upload = next(step for step in jobs["build"]["steps"] if "upload-pages-artifact" in step.get("uses", ""))

    assert upload["with"]["path"] == docs_site.project()["site_dir"]
    assert jobs["deploy"]["needs"] == "build"
    assert jobs["deploy"]["if"] == f"github.ref == 'refs/heads/{BRANCH}'"
    assert not any("pages" in str(step.get("uses", "")) for step in _workflow(CHECK_WORKFLOW)["jobs"]["build"]["steps"])


@pytest.mark.parametrize("path", [CHECK_WORKFLOW, PUBLISH_WORKFLOW], ids=lambda path: path.name)
def test_the_workflows_carry_the_repo_prefix(path):
    document = _workflow(path)

    assert document["name"].startswith("Repo ")
    assert all(job["name"].startswith("Repo ") for job in document["jobs"].values())


@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
@pytest.mark.parametrize("key", ["docs_dir", "site_dir"])
def test_the_stage_and_the_site_are_ignored(key, ignore_file):
    patterns = (ROOT / ignore_file).read_text(encoding="utf-8").splitlines()
    top = Path(docs_site.project()[key]).parts[0]

    assert {f"/{top}/", f"{top}/"} & set(patterns)


def test_the_docker_context_leaves_out_the_zensical_cache():
    """Zensical ignores its cache for git itself, inside it; Docker reads no such file."""

    assert ".cache/" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
