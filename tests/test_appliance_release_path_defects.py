# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two defects the release path carried, and what each one cost.

Both were found by adversarially reviewing a CI pipeline that would drive these
scripts, before it was written. Both apply to either image variant.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]


def text(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_every_equals_form_option_advances_the_argument_loop():
    """`--variant=ab` spun on the same argv element forever.

    A `--flag=value` branch that does not shift re-reads the same word on the
    next pass, so the script never reaches its work and never exits. It is the
    only `=`-form option the builder takes, so it is the only one that could
    carry the defect — and it did, for both variants.
    """

    script = text("appliance-builder-vm.sh")

    for line in script.splitlines():
        entry = line.strip()
        if entry.startswith("--") and "=*)" in entry and entry.endswith(";;"):
            assert "shift" in entry, f"this branch cannot terminate: {entry}"


def test_the_gate_path_hands_the_builder_environment_to_the_build():
    """Without it the authority records an empty environment.

    That is refused at signing by require_environment, so the gate passes, the
    build reports completed, and the release fails hours later on an artefact
    that can never be signed. Committed evidence shows exactly that shape.
    """

    gates = text("appliance-release-gates.sh")
    vm = text("appliance-builder-vm.sh")

    assert "--builder-environment)" in gates
    assert "$BUILDER_ENVIRONMENT_ARG" in gates
    assert "--builder-environment /build/builder-environment.json" in vm


def test_the_shape_that_defect_produced_is_still_in_the_recorded_evidence():
    """The regression's own fossil, kept as the reason the check exists."""

    import json

    recorded = json.loads(
        (ROOT / "reports/appliance/2026-08-11-head/build-authority-rpi5-1.json").read_text(
            encoding="utf-8"
        )
    )

    assert recorded["completed"] is True
    assert recorded["builder_environment"]["base_image_lock_id"] == ""
    assert recorded["builder_environment"]["base_image_sha512"] == ""
