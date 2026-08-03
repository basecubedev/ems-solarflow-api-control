# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local Markdown link contract.

Scans the project's Markdown documentation and proves that every *local* link
resolves: relative file targets exist, and same-file / local-file heading
anchors point at a real GitHub-style heading slug. External links
(http/https/mailto/tel), links inside code fences or inline code, and
intentional ``<placeholder>`` targets are ignored. Line anchors on non-Markdown
targets (e.g. ``server.py#L42``) only require the file to exist. No network
requests are made.
"""
import re
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
_EXTERNAL = ("http://", "https://", "mailto:", "tel:")


def _markdown_files():
    return [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))


def _strip_code(text):
    return _INLINE_CODE.sub("", _FENCE.sub("", text))


def _heading_slug(heading):
    heading = re.sub(r"`", "", heading)
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # link -> its text
    heading = heading.strip().lower()
    heading = re.sub(r"[^\w\s-]", "", heading)
    return heading.replace(" ", "-")


def _heading_anchors(path):
    # GitHub disambiguates repeated slugs with -1, -2, ... suffixes.
    anchors, counts = set(), {}
    body = _FENCE.sub("", path.read_text(encoding="utf-8"))
    for match in _HEADING.finditer(body):
        base = _heading_slug(match.group(2))
        seen = counts.get(base, 0)
        anchors.add(base if seen == 0 else f"{base}-{seen}")
        counts[base] = seen + 1
    return anchors


def _local_targets(text):
    for raw in _LINK.findall(text) + _IMAGE.findall(text):
        target = raw.strip()
        if not target or target.startswith(_EXTERNAL):
            continue
        if "<" in target or ">" in target or "YOUR_" in target:
            continue  # intentional template / placeholder, not a real path
        yield target


def test_local_markdown_file_links_resolve():
    broken = []
    for doc in _markdown_files():
        text = _strip_code(doc.read_text(encoding="utf-8"))
        for target in _local_targets(text):
            if target.startswith("#"):
                continue  # same-file anchor, checked separately
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if not (doc.parent / path_part).resolve().exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert broken == [], "broken local file links:\n" + "\n".join(broken)


def test_local_markdown_heading_anchors_resolve():
    broken = []
    for doc in _markdown_files():
        text = _strip_code(doc.read_text(encoding="utf-8"))
        for target in _local_targets(text):
            path_part, _, frag = target.partition("#")
            if not frag:
                continue
            if not path_part:
                resolved = doc  # same-file anchor
            else:
                resolved = (doc.parent / path_part).resolve()
                # Non-Markdown targets (e.g. source #Lnn anchors) or a missing
                # file: file existence is the file-links test's job.
                if resolved.suffix != ".md" or not resolved.exists():
                    continue
            if frag not in _heading_anchors(resolved):
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert broken == [], "broken local heading anchors:\n" + "\n".join(broken)
