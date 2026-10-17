# SPDX-License-Identifier: AGPL-3.0-or-later
"""The films on YouTube are listed once and named where their guides are.

``docs/user/videos.md`` is the one list of the project's films. Every film a
page links is on it, every film on it is named at the top of the guide the
list pairs it with, and the README routes to the list and the channel. The
documentation website is named in the README and the documentation map with
the address the site is built for.
"""

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
README = ROOT / "README.md"
VIDEOS = DOCS / "user" / "videos.md"
CHANNEL = "https://www.youtube.com/channel/UCk7i1JfCVML8hqlvzenCJBg"

YOUTUBE = re.compile(r"https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})")
ROW = re.compile(
    r"^\| \[(?P<title>[^\]]+)\]\(https://www\.youtube\.com/watch\?v=(?P<id>[A-Za-z0-9_-]{11})\) "
    r"\| \[[^\]]+\]\((?P<guide>[^)]+)\) \|$",
    re.M,
)


def read(path):
    return path.read_text(encoding="utf-8")


def films():
    rows = ROW.findall(read(VIDEOS))
    assert rows, "docs/user/videos.md lists no film in the expected row shape"
    return rows


def test_the_videos_page_lists_every_film_any_page_links():
    listed = {film_id for _title, film_id, _guide in films()}
    stray = []
    for page in [README] + sorted(DOCS.rglob("*.md")):
        for film_id in YOUTUBE.findall(read(page)):
            if film_id not in listed:
                stray.append(f"{page.relative_to(ROOT)}: {film_id}")

    assert stray == []


def test_every_listed_film_is_named_at_the_top_of_its_guide():
    """A reader of the guide finds its film before the first section."""

    unnamed = []
    for title, film_id, guide in films():
        page = (VIDEOS.parent / guide).resolve()
        assert page.is_file(), guide
        top = read(page).split("\n## ", 1)[0]
        if f"watch?v={film_id}" not in top:
            unnamed.append(f"{guide}: {title}")

    assert unnamed == []


def test_each_film_is_listed_once():
    ids = [film_id for _title, film_id, _guide in films()]

    assert len(ids) == len(set(ids))


def test_the_readme_routes_to_the_channel_and_the_list():
    text = read(README)

    assert CHANNEL in text
    assert "docs/user/videos.md" in text
    assert CHANNEL in read(VIDEOS)


def test_the_readme_and_the_map_name_the_website():
    with (ROOT / "zensical.toml").open("rb") as handle:
        site_url = tomllib.load(handle)["project"]["site_url"]

    assert f"<{site_url}>" in read(README)
    assert f"<{site_url}>" in read(DOCS / "README.md")
