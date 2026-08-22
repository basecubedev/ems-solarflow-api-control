# SPDX-License-Identifier: AGPL-3.0-or-later
"""The boot selector parser and serializer.

``autoboot.txt`` is the whole safety argument of A/B: it is the one file that
decides which slot boots normally and which one boots once. So the parser
understands exactly the form this project generates and refuses everything else.
A selector nobody can parse exactly is a selector nobody can prove is safe, and
a generic configuration editor would be a second, weaker authority over the same
file.
"""

import os

import pytest

from appliance.ab_boot import (
    Selector,
    SelectorError,
    parse_selector,
    read_selector,
    render_selector,
    request_trial_reboot,
    write_selector,
)
from appliance.commands import RecordingRunner

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

COMMITTED = """# ems-appliance boot selector. Generated; do not edit.
[all]
tryboot_a_b=1
boot_partition=2

[tryboot]
boot_partition=3
"""


# --- parsing -----------------------------------------------------------------


def test_the_generated_form_parses_to_its_semantic_state():
    selector = parse_selector(COMMITTED)

    assert selector.default_partition == 2
    assert selector.tryboot_partition == 3
    assert selector.tryboot_a_b is True


def test_a_selector_round_trips_through_render_and_parse():
    selector = Selector(default_partition=3, tryboot_partition=2)

    assert parse_selector(render_selector(selector)) == selector


@pytest.mark.parametrize(
    "text, code",
    [
        ("[all]\ntryboot_a_b=1\nboot_partition=2\n", "selector_sections_missing"),
        ("[tryboot]\nboot_partition=3\n", "selector_sections_missing"),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=2\n[all]\nboot_partition=3\n",
            "selector_section_duplicate",
        ),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=2\nboot_partition=3\n[tryboot]\nboot_partition=3\n",
            "selector_directive_duplicate",
        ),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=2\nuart_2ndstage=1\n[tryboot]\nboot_partition=3\n",
            "selector_directive_unknown",
        ),
        (
            "[pi4]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\nboot_partition=3\n",
            "selector_section_unsupported",
        ),
        (
            "[all]\nboot_partition=2\n[tryboot]\nboot_partition=3\n",
            "selector_not_ab",
        ),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\nboot_partition=2\n",
            "selector_slots_identical",
        ),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=zwei\n[tryboot]\nboot_partition=3\n",
            "selector_partition_invalid",
        ),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=0\n[tryboot]\nboot_partition=3\n",
            "selector_partition_invalid",
        ),
        (
            "boot_partition=2\n[all]\ntryboot_a_b=1\n[tryboot]\nboot_partition=3\n",
            "selector_line_unsupported",
        ),
        (
            "[all]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\ntryboot_a_b=1\nboot_partition=3\n",
            "selector_directive_unknown",
        ),
    ],
)
def test_a_selector_this_project_did_not_generate_is_refused(text, code):
    with pytest.raises(SelectorError) as caught:
        parse_selector(text)

    assert caught.value.code == code


def test_comments_and_blank_lines_are_ignored():
    text = "# a comment\n\n[all]\n\ntryboot_a_b=1\nboot_partition=2\n\n[tryboot]\nboot_partition=3\n"

    assert parse_selector(text).default_partition == 2


# --- writing -----------------------------------------------------------------


def test_writing_the_selector_replaces_it_atomically(tmp_path):
    target = tmp_path / "autoboot.txt"
    target.write_text(COMMITTED, encoding="utf-8")

    written = write_selector(target, Selector(default_partition=3, tryboot_partition=2))

    assert written.default_partition == 3
    assert read_selector(target) == written
    assert sorted(item.name for item in tmp_path.iterdir()) == ["autoboot.txt"]


def test_a_write_that_could_not_read_back_is_a_failure(tmp_path, monkeypatch):
    target = tmp_path / "autoboot.txt"
    target.write_text(COMMITTED, encoding="utf-8")

    def replace_with_the_old_content(source, destination):
        os.unlink(source)

    monkeypatch.setattr(os, "replace", replace_with_the_old_content)

    with pytest.raises(SelectorError) as caught:
        write_selector(target, Selector(default_partition=3, tryboot_partition=2))

    assert caught.value.code == "selector_readback_mismatch"
    assert read_selector(target).default_partition == 2


def test_a_failed_write_leaves_no_staged_file_behind(tmp_path):
    target = tmp_path / "missing" / "autoboot.txt"

    with pytest.raises(SelectorError) as caught:
        write_selector(target, Selector(default_partition=2, tryboot_partition=3))

    assert caught.value.code == "selector_write_failed"


def test_an_unreadable_selector_is_an_error_not_a_default(tmp_path):
    with pytest.raises(SelectorError) as caught:
        read_selector(tmp_path / "absent.txt")

    assert caught.value.code == "selector_unreadable"


def test_the_trial_reboot_passes_its_argument_as_an_option():
    """systemd 253 removed the positional argument to ``systemctl reboot``.

    The appliance image is Debian 13 (systemd 257), so the positional form is
    rejected before the firmware ever sees the request and every A/B update
    stalls armed but not rebooted.
    """

    runner = RecordingRunner(default="")

    request_trial_reboot(runner)

    tool, args, _ = runner.calls[-1]
    assert tool == "systemctl"
    assert args == ("reboot", "--reboot-argument=0 tryboot")
