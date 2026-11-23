# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The docs tests read the Markdown git would commit, and only that."""

import subprocess

import pytest

from tests._published_markdown import ROOT, published_markdown

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]


def test_tracked_and_new_markdown_is_read_and_an_ignored_note_is_not(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/docs/develop/\n", encoding="utf-8")
    for name in ("README.md", "docs/user/tracked.md", "docs/new.md", "docs/develop/note.md", "docs/gone.md", "docs/user/notes.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("# Heading\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", "docs/user/tracked.md", "docs/gone.md"], cwd=tmp_path, check=True)
    (tmp_path / "docs" / "gone.md").unlink()

    def read(*pathspecs):
        return [path.relative_to(tmp_path).as_posix() for path in published_markdown(*pathspecs, root=tmp_path)]

    assert read() == ["README.md", "docs/new.md", "docs/user/tracked.md"]
    assert read("docs/user/*.md") == ["docs/user/tracked.md"]


def test_this_repository_lists_its_readme_and_docs():
    found = published_markdown()

    assert ROOT / "README.md" in found
    assert ROOT / "docs" / "README.md" in found
