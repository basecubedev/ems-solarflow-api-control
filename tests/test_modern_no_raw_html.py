# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""No front end writes text it did not make as markup.

Device names, findings, refusal messages, diagnosis and log lines and backup
details come from the server and from devices, and reach the page through
Svelte's text interpolation, which escapes them. The one way around that is
to hand a string to the browser's HTML parser. None of the front ends does,
and this keeps it so: a `{@html}` block or a write to `innerHTML` and its
relatives anywhere in their sources fails here, so a raw value cannot become
markup by accident.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    ROOT / "frontend" / "admin-modern" / "src",
    ROOT / "frontend" / "dashboard-modern" / "src",
    ROOT / "frontend" / "appliance-modern" / "src",
    ROOT / "frontend" / "shared",
)
RAW_HTML = re.compile(r"\{@html\b|\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write\b")


def _sources():
    for root in SOURCES:
        for path in sorted(root.rglob("*")):
            if path.suffix in {".svelte", ".js"} and "node_modules" not in path.parts and "vite" not in path.parts:
                yield path


def test_the_sources_are_found():
    assert sum(1 for _ in _sources()) > 100


def test_no_front_end_hands_a_string_to_the_html_parser():
    found = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if RAW_HTML.search(line)
    ]
    assert found == []
