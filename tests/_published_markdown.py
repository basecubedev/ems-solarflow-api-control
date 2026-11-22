# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Markdown files a docs test reads: what git would commit, never an ignored note."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = ("README.md", "docs/*.md")


def published_markdown(*pathspecs, root=ROOT):
    """Tracked or new Markdown under ``pathspecs``, sorted; git-ignored files left out.

    Notes kept beside the docs and ignored by git (``docs/develop/``) are nobody's
    published documentation. Reading them failed a link test on every developer
    checkout that kept one with a dangling link, and could let a screenshot count
    as used by a note CI never sees.
    """

    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *(pathspecs or PUBLISHED)],
        cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return sorted({root / name for name in listed.split("\0") if name and (root / name).is_file()})
