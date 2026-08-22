# SPDX-License-Identifier: AGPL-3.0-or-later
"""The screenshot index has to describe the screenshots that exist.

Three things claim to agree: the capture spec that writes the images, the
directory README that says what each one shows and where it is used, and the
guides that embed them. Nothing checked that they did, so a renamed capture, a
dropped image or a row pointing at a guide that never embedded it would all read
as complete.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "assets" / "screenshots" / "appliance"
INDEX = SHOTS / "README.md"
SPEC = ROOT / "tests" / "e2e-appliance" / "capture-docs.spec.ts"

ROW = re.compile(r"^\|\s*`(appliance-[a-z0-9-]+)`\s*\|[^|]*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|", re.M)


def rows():
    return {name: (label, target) for name, label, target in ROW.findall(INDEX.read_text("utf-8"))}


def images():
    return {path.stem for path in SHOTS.glob("*.png")}


def captures():
    return set(re.findall(r'test\("(appliance-[a-z0-9-]+)"', SPEC.read_text("utf-8")))


def test_every_image_is_described():
    assert images() - set(rows()) == set()


def test_every_description_has_an_image():
    assert set(rows()) - images() == set()


def test_every_capture_is_described():
    assert captures() - set(rows()) == set()


def test_every_description_is_captured():
    """A row nobody writes is a screenshot that silently goes stale."""

    assert set(rows()) - captures() == set()


@pytest.mark.parametrize("name", sorted(rows()))
def test_the_guide_a_row_names_really_embeds_it(name):
    _, target = rows()[name]
    guide = (INDEX.parent / target).resolve()

    assert guide.is_file(), f"{name} points at {target}, which does not exist"
    assert f"{name}.png" in guide.read_text("utf-8"), f"{target} does not embed {name}.png"


def test_the_minimum_set_the_task_specified_is_complete():
    """The nine the documentation plan asked for, by name."""

    required = {
        "appliance-first-start-password",
        "appliance-login",
        "appliance-overview",
        "appliance-update-plan",
        "appliance-update-running",
        "appliance-ab-slots",
        "appliance-network-wifi",
        "appliance-backup-access",
        "appliance-recovery",
    }

    assert required - images() == set()
