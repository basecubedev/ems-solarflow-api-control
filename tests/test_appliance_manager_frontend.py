# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Appliance Manager section of the console is allowed to say.

Style family: Control / Energy stage. The section reuses the existing stage,
card, status-value and tone tokens; no new visual system is introduced.

Both decisions the section makes are derived from the backend payload alone:
whether an install may be started, and whether the kept package may be put
back. A deadline in flight blocks both, because a second install would replace
the package whose verdict the appliance is still waiting for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "appliance" / "static" / "app.js"
APP = APP_JS.read_text(encoding="utf-8")

node = shutil.which("node")
requires_node = pytest.mark.skipif(node is None, reason="node is required to evaluate app.js")


def extract(name):
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} is not a closed function")


def evaluate(name, payload, *, preamble=""):
    script = (
        preamble
        + extract(name)
        + f"\nconsole.log(JSON.stringify({name}("
        + json.dumps(payload)
        + ")));\n"
    )
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


DIRECTIONS = "var MANAGER_DIRECTIONS = " + APP.split("var MANAGER_DIRECTIONS = ", 1)[1].split(
    "};", 1
)[0] + "};\n"


def manager(**overrides):
    payload = {
        "installed_version": "0.1.0",
        "configured": True,
        "can_revert": False,
        "verify": {"armed": False},
        "verdict": {"verdict": "pending", "settled": False},
        "outcome": {"outcome": "pending"},
        "retention": {"current": {}, "previous": {}},
    }
    payload.update(overrides)
    return payload


# --- both decisions are named functions --------------------------------------


def test_the_action_gate_is_a_named_function():
    """A gate buried in a render call cannot be tested at all."""

    assert "function managerActions(manager)" in APP


@requires_node
def test_a_quiet_appliance_may_install_but_has_nothing_to_go_back_to():
    result = evaluate("managerActions", manager())

    assert result["canUpdate"] is True
    assert result["canRevert"] is False


@requires_node
def test_an_appliance_that_kept_a_package_may_put_it_back():
    result = evaluate("managerActions", manager(can_revert=True))

    assert result["canRevert"] is True


@requires_node
def test_an_armed_deadline_blocks_both_buttons():
    result = evaluate(
        "managerActions", manager(can_revert=True, verify={"armed": True, "expected_version": "0.2.0"})
    )

    assert result["armed"] is True
    assert result["canUpdate"] is False
    assert result["canRevert"] is False


@requires_node
def test_a_missing_payload_enables_nothing_it_cannot_prove():
    result = evaluate("managerActions", {})

    assert result["canRevert"] is False


# --- what a release is labelled with -----------------------------------------


@requires_node
def test_a_release_is_labelled_with_its_version_date_and_direction():
    label = evaluate(
        "managerLabel",
        {
            "release_id": "ems-appliance-manager-0.2.0-arm64",
            "direction": "downgrade",
            "described": {"release_version": "0.2.0", "created_at": "2026-08-26T01:00:00Z"},
        },
        preamble=DIRECTIONS,
    )

    assert label == "0.2.0 · 2026-08-26 · older"


@requires_node
def test_an_index_that_says_nothing_still_labels_the_release():
    label = evaluate(
        "managerLabel", {"release_id": "ems-appliance-manager-0.2.0-arm64"}, preamble=DIRECTIONS
    )

    assert label == "ems-appliance-manager-0.2.0-arm64"


# --- where the section appears -----------------------------------------------


def test_the_section_is_rendered_on_every_appliance_shape():
    """It is the package the console runs from, not an A/B feature."""

    body = extract("renderUpdates")

    assert body.index("renderManagerUpdates(main)") < body.index("renderAbUpdates(main, ab)")
    assert body.index("renderManagerUpdates(main)") < body.index("renderPackageUpdates(")


def test_the_browser_sends_a_release_id_and_nothing_else():
    section = APP.split("function renderManagerUpdates(", 1)[1]
    section = APP.split("function renderManagerSources(", 1)[1].split(
        "function renderManagerUpdates(", 1
    )[0]

    assert 'body: { release_id: entry.release_id }' in section
    assert "manifest_url" not in section
    assert "archive_url" not in section


def test_no_dynamic_value_reaches_innerhtml():
    section = APP.split("function managerActions(", 1)[1].split("function renderAbUpdates(", 1)[0]

    assert "innerHTML" not in section


# --- the card reports an outcome that arrives after the operation ------------


def test_the_manager_state_is_refreshed_and_not_read_once():
    """Its whole purpose is to report a verdict that lands later.

    The install finishes as an operation the moment dpkg is started; whether it
    stood or was reverted is decided minutes afterwards by the deadline. A card
    fetched once when the page first rendered would still be showing "nothing in
    flight" when the appliance had already put the previous package back.
    """

    body = extract("refresh")

    assert '"/api/manager"' in body, "the live half belongs in the periodic refresh"


def test_only_the_index_is_fetched_lazily():
    """Reading a remote index on every poll would cost a network round trip."""

    section = APP.split("function renderManagerUpdates(", 1)[1].split("\n  }", 1)[0]

    assert 'loadInto("managerSources", "/api/manager/sources")' in section
    assert 'loadInto("manager"' not in section


def test_an_unfetched_state_renders_as_unknown_rather_than_as_quiet():
    section = APP.split("function renderManagerUpdates(", 1)[1].split("\n  }", 1)[0]

    assert "Reading the manager state" in section
