# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage and check the documentation website.

The Markdown under ``docs/`` is written for GitHub's file view, where a link may
point anywhere in the repository: to source files, tests, the root README or a
directory listing. A website built from ``docs/`` alone has none of those, so
``stage`` copies ``docs/`` to the staging directory Zensical builds from and
points every link that leaves it, or names a directory without an index page, at
GitHub. The pages themselves stay the single source; nothing under ``docs/`` is
changed. ``check`` then proves that every local link and anchor of the built
site resolves.

    python3 scripts/docs_site.py stage
    zensical build --clean --strict
    python3 scripts/docs_site.py check

See docs/developer/docs-site.md.
"""

import argparse
import functools
import html.parser
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIG = ROOT / "zensical.toml"
INDEX_NAMES = ("index.md", "README.md")
STAGE_MARKER = ".docs-site-stage"

LINK = re.compile(
    r"(?P<image>!?)\[(?P<text>(?:[^\[\]]|!\[[^\]]*\]\([^)]*\))*)\]\((?P<space>\s*)"
    r"(?P<target><[^>\n]*>|[^)\s]+)(?P<suffix>(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\))"
)
REFERENCE = re.compile(r"^(?P<prefix> {0,3}\[(?!\^)[^\]]+\]:[ \t]*)(?P<target><[^>\n]*>|\S+)(?P<suffix>.*)$", re.S)
INLINE_CODE = re.compile(r"`[^`]*`")
FENCE = re.compile(r"^(```|~~~)")
EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", re.I)
LINK_ATTRIBUTES = ("href", "src", "poster")
HTML_LINK = re.compile(rf"(?P<prefix>\b(?P<attribute>{'|'.join(LINK_ATTRIBUTES)})=\")(?P<target>[^\"]+)(?P<suffix>\")")


@functools.cache
def project():
    with CONFIG.open("rb") as handle:
        return tomllib.load(handle)["project"]


def stage_dir():
    return ROOT / project()["docs_dir"]


def site_dir():
    return ROOT / project()["site_dir"]


def branch():
    """The branch the edit links name, so source links point at the same tree."""

    return project()["edit_uri"].split("/")[1]


def github_url(path, kind):
    relative = path.relative_to(ROOT).as_posix()
    return f"{project()['repo_url']}/{kind}/{branch()}/{quote(relative)}"


def docs_files():
    """Every file under docs/ that git tracks or would track.

    A gitignored local note under docs/ never reaches the website."""

    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "docs"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"listing docs/ needs git and a checkout of this repository: {error}") from error
    return sorted({ROOT / name for name in listed.split("\0") if name and (ROOT / name).is_file()})


def _relative(path, page):
    return quote(os.path.relpath(path, page.parent).replace(os.sep, "/"))


def rewrite_target(target, page, image=False):
    """The link target as the website needs it, or ``target`` unchanged."""

    if target.startswith("<") and target.endswith(">"):
        return f"<{rewrite_target(target[1:-1], page, image)}>"
    if EXTERNAL.match(target):
        return target
    path_part, hash_mark, fragment = target.partition("#")
    if not path_part:
        return target
    resolved = (page.parent / unquote(path_part)).resolve()
    if not resolved.is_relative_to(ROOT):
        return target
    inside_docs = resolved.is_relative_to(DOCS)
    if resolved.is_dir():
        index = next((name for name in INDEX_NAMES if (resolved / name).is_file()), None)
        if inside_docs and index is not None:
            return _relative(resolved / index, page) + hash_mark + fragment
        return github_url(resolved, "tree") + hash_mark + fragment
    if not resolved.is_file():
        return target
    if inside_docs:
        return _relative(resolved, page) + hash_mark + fragment
    return github_url(resolved, "raw" if image else "blob") + hash_mark + fragment


def _code_spans(line):
    return [span.span() for span in INLINE_CODE.finditer(line)]


def _rewrite_inline(text, page, code=()):
    def rewrite(match):
        if any(start <= match.start() < end for start, end in code):
            return match.group(0)
        image = match.group("image")
        target = rewrite_target(match.group("target"), page, image=bool(image))
        nested = _rewrite_inline(match.group("text"), page)
        return f"{image}[{nested}]({match.group('space')}{target}{match.group('suffix')}"

    return LINK.sub(rewrite, text)


def _rewrite_html(line, page):
    code = _code_spans(line)

    def rewrite(match):
        if any(start <= match.start() < end for start, end in code):
            return match.group(0)
        image = match.group("attribute") != "href"
        return match.group("prefix") + rewrite_target(match.group("target"), page, image) + match.group("suffix")

    return HTML_LINK.sub(rewrite, line)


def rewrite_line(line, page):
    """``line`` with every link outside inline code rewritten for the website:
    inline links and images, reference definitions, and raw HTML links."""

    reference = REFERENCE.match(line)
    if reference:
        return reference.group("prefix") + rewrite_target(reference.group("target"), page) + reference.group("suffix")
    return _rewrite_html(_rewrite_inline(line, page, _code_spans(line)), page)


def rewrite_page(text, page):
    """``text`` with every link outside code rewritten for the website."""

    lines = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if FENCE.match(line.lstrip()):
            fenced = not fenced
        elif not fenced:
            line = rewrite_line(line, page)
        lines.append(line)
    return "".join(lines)


def stage(destination=None):
    """Copy docs/ to ``destination`` with its links rewritten for the website.

    Only a directory an earlier ``stage`` created is replaced; anything else in
    the way is refused rather than deleted."""

    destination = Path(destination or stage_dir())
    if destination.exists():
        if not (destination / STAGE_MARKER).is_file():
            raise SystemExit(f"{destination} exists and was not created by this script; refusing to replace it")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    (destination / STAGE_MARKER).write_text("staged by scripts/docs_site.py\n", encoding="utf-8")
    for source in docs_files():
        target = destination / source.relative_to(DOCS)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".md":
            target.write_text(rewrite_page(source.read_text(encoding="utf-8"), source), encoding="utf-8")
        else:
            shutil.copy2(source, target)
    return destination


class _PageLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.anchors = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if not value:
                continue
            if name in LINK_ATTRIBUTES:
                self.links.append(value)
            elif name == "id" or (name == "name" and tag == "a"):
                self.anchors.add(value)


def _parse(page):
    parser = _PageLinks()
    parser.feed(page.read_text(encoding="utf-8"))
    return parser


def check(site=None):
    """Every local link of the built site that names no existing file or anchor."""

    site = Path(site or site_dir()).resolve()
    if not site.is_dir():
        raise SystemExit(f"{site} does not exist; build the site first")
    base = urlsplit(project()["site_url"]).path
    pages = {page.resolve(): _parse(page) for page in site.rglob("*.html")}
    problems = []
    for page, parsed in sorted(pages.items()):
        name = page.relative_to(site).as_posix()
        for link in parsed.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            if not url.path:
                target = page
            elif url.path.startswith("/"):
                if not url.path.startswith(base):
                    problems.append(f"{name}: {link} is outside the site path {base}")
                    continue
                target = site / unquote(url.path[len(base):])
            else:
                target = page.parent / unquote(url.path)
            target = target.resolve()
            if target.is_dir():
                target = target / "index.html"
            if not target.is_relative_to(site) or not target.is_file():
                problems.append(f"{name}: {link} names no file of the site")
            elif url.fragment and target in pages and unquote(url.fragment) not in pages[target].anchors:
                problems.append(f"{name}: {link} names no anchor of {target.relative_to(site).as_posix()}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stage", help="copy docs/ into the staging directory with links fixed for the website")
    commands.add_parser("check", help="verify that every local link and anchor of the built site resolves")
    args = parser.parse_args(argv)
    if args.command == "stage":
        print(stage())
        return 0
    problems = check()
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
