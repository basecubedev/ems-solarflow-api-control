# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A script that prints its own header as usage prints the whole comment block.

Twenty scripts answer ``--help`` with the comment block at their top. They read
it with one ``awk`` expression that stops at the first line that is not a
comment and skips the SPDX lines, instead of a fixed ``sed`` line range: a
range drifted silently every time a line moved above or inside the block,
printed ``set -eu`` or the license line, and cut six of them off mid-sentence.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.appliance,
]

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = "NR == 1 { next } /^#/ { if ($0 !~ /SPDX-/) print; next } { exit }"
USAGE = f"awk '{PROGRAM}' \"$0\""
_FIXED_RANGE = re.compile(r"""sed -n ['"]\d+,\d+p['"] "?\$\{?0""")


def shell_scripts():
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    scripts = []
    for relative in tracked.decode().split("\0"):
        path = ROOT / relative
        if not relative or path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            first = handle.readline()
        if relative.endswith(".sh") or first.startswith(b"#!") and b"sh" in first:
            scripts.append(relative)
    return scripts


def test_no_script_prints_its_header_by_a_fixed_line_range():
    offenders = [
        relative
        for relative in shell_scripts()
        if _FIXED_RANGE.search((ROOT / relative).read_text(encoding="utf-8", errors="replace"))
    ]
    assert offenders == []


def test_every_script_that_prints_its_header_prints_the_whole_comment_block():
    users = [relative for relative in shell_scripts() if USAGE in (ROOT / relative).read_text(encoding="utf-8")]
    assert len(users) == 20
    for relative in users:
        text = subprocess.run(["awk", PROGRAM, str(ROOT / relative)], capture_output=True, text=True, check=True).stdout
        lines = text.splitlines()
        assert lines, relative
        assert all(line.startswith("#") and "SPDX-" not in line for line in lines), relative
        source = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        following = source[source.index(lines[-1]) + 1]
        assert not following.startswith("#"), relative
