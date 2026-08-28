# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every command this appliance tells an operator to run has to exist.

``artifact_trust`` answered ``state_schemas_unrecorded`` -- the block that stops
both an update and a revert -- with "run 'ems-appliance state-schema' and try
again". There is no such subcommand, and there never was. Worse, the action it
described could not have worked: ``persistent_state.reconcile`` skips
``write_stamp`` whenever the outcome is ``STATE_UNREADABLE``, so nothing in this
codebase rewrites a stamp it cannot read.

An operator meeting that message is already stuck -- the console is what the
update was going to fix -- and the instruction sent them looking for a command
instead of at the file. So the class is pinned rather than the instance: any
``ems-appliance <verb>`` this project prints or documents must be a subcommand
the parser actually registers.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]

# An invocation, not a mention: at the start of a line, after `sudo`, or inside
# backticks. A path table reading "/usr/bin/ems-appliance    host CLI" and a file
# header reading "# ems-appliance backup home marker" are prose, and matching
# them would make the check something to be silenced rather than read.
MENTION = re.compile(r"(?:^|`|sudo )ems-appliance ([a-z][a-z0-9-]{2,})", re.M)


def registered():
    from appliance.cli import build_parser

    parser = build_parser()
    names = set()
    for action in parser._actions:
        for name in getattr(action, "choices", None) or ():
            names.add(name)
    assert names, "no subcommands were found; the parser shape changed"
    return names


def sources():
    for path in sorted((ROOT / "appliance").glob("*.py")):
        yield path
    for directory in ("docs/appliance", "docs/user/appliance"):
        for path in sorted((ROOT / directory).rglob("*.md")):
            yield path


def test_no_advice_names_a_subcommand_that_does_not_exist():
    known = registered()
    unknown = {}
    for path in sources():
        for verb in MENTION.findall(path.read_text(encoding="utf-8")):
            if verb in known:
                continue
            unknown.setdefault(verb, []).append(str(path.relative_to(ROOT)))

    assert unknown == {}, f"advice naming subcommands that do not exist: {unknown}"


def test_the_unreadable_state_record_is_answered_with_something_that_works():
    """A manager never rewrites a stamp it cannot read, so the repair is the
    file and not a command.

    The second assertion is what makes the first one mean anything: if
    reconcile ever learns to rewrite an unreadable record, this message becomes
    the wrong advice again and should be revisited.
    """

    from appliance import artifact_trust, persistent_state

    source = Path(artifact_trust.__file__).read_text(encoding="utf-8")
    message = source.split('"code": "state_schemas_unrecorded"', 1)[1].split("]", 1)[0]

    assert persistent_state.STAMP_NAME in message, (
        "the message does not name the record, which is the only thing to act on"
    )

    reconcile = Path(persistent_state.__file__).read_text(encoding="utf-8")
    skips = reconcile.split("def reconcile(", 1)[1].split("return verdict, stamp", 1)[0]
    assert "STATE_UNREADABLE" in skips, (
        "an unreadable record is now rewritten somewhere; the advice above assumes it is not"
    )
