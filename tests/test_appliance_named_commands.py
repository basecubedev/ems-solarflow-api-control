# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ems-appliance command a script or page tells an operator to run exists.

Those messages are what an operator meets when apt refuses a removal or an
account is locked; one named `backup-access migrate-ownership`, which the CLI
does not accept, and another `shell-access --enable`.
"""

import re
import shlex
from pathlib import Path

import pytest

from appliance.cli import build_parser

pytestmark = [pytest.mark.contract, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
QUOTED = re.compile(r"'ems-appliance ([a-z][a-z -]*)'")


def named_commands():
    found = set()
    for base in ("packaging", "docs/appliance", "docs/user/appliance"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix not in {".deb", ".gpg", ".png", ".hash"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                found.update(match.group(1).strip() for match in QUOTED.finditer(text))
    return sorted(found)


@pytest.mark.parametrize("command", named_commands())
def test_a_named_command_is_one_the_cli_accepts(command):
    try:
        build_parser().parse_args(shlex.split(command))
    except SystemExit as exc:
        pytest.fail(f"'ems-appliance {command}' is not a command the CLI accepts ({exc})")


def test_the_scan_finds_the_commands_it_guards():
    assert "backup-account migrate-ownership" in named_commands()
