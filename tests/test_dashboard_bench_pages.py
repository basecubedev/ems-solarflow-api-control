# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard benchmark measures pages the dashboard has, and every one of them a reader opens without signing in.

``scripts/dashboard_bench.mjs`` names its pages in a list of its own; a page
renamed or added in ``frontend/dashboard-modern/src/app/pages.js`` would
otherwise drop out of the measurement or make it open a page that is not
there.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]


def _bench_pages():
    text = (ROOT / "scripts" / "dashboard_bench.mjs").read_text(encoding="utf-8")
    listed = re.search(r"export const PAGES = \[([^\]]*)\]", text)
    assert listed, "the benchmark no longer names its pages"
    return re.findall(r'"([a-z]+)"', listed.group(1))


def _dashboard_pages():
    source = (ROOT / "frontend" / "dashboard-modern" / "src" / "app" / "pages.js").read_text(encoding="utf-8")
    text = source[source.index("export const PAGES = "): source.index("export const MOBILE_PAGES")]
    pages = {}
    for entry in re.findall(r"\{ id: \"([a-z]+)\"([^}]*)\}", text):
        pages[entry[0]] = entry[1]
    return pages


def test_the_benchmark_measures_every_page_a_reader_opens_without_signing_in():
    pages = _dashboard_pages()
    open_pages = [page for page, rest in pages.items() if "operator: true" not in rest and "hidden: true" not in rest]
    assert _bench_pages() == open_pages
