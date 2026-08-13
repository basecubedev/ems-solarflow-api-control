# SPDX-License-Identifier: AGPL-3.0-or-later
"""Why the A/B appliance image has no Raspberry Pi 3 profile.

A Pi 3B+ is an arm64 board, so "does it run 64-bit Linux" is not the question.
The question is whether it can boot *this* image, and three independent facts
say it cannot. Each one is checked here against the pinned upstream bytes or
against this project's own code, so the decision cannot decay into a stale
paragraph in an ADR while the build quietly grows a profile for it:

1. ``image-rota`` owns the A/B partition table, and its own metadata refuses the
   ``pi3`` device class.
2. The layout puts nothing but ``autoboot.txt`` on the first partition. A Pi 4
   or Pi 5 loads its bootloader from EEPROM and does not care; a Pi 3 boot ROM
   has to find a second-stage bootloader there.
3. The layout is GPT, and the Pi 3 boot ROM reads only an MBR.

A board this project ships no image for must also stay un-updatable rather than
be guessed at, so the fail-closed half of the decision is checked too.

See ``docs/appliance/adr/raspberry-pi-3-ab-support.md``.
"""

from pathlib import Path

import pytest
import yaml

from appliance import build_authority, rpi_image_gen
from tests.helpers import upstream_rpi_image_gen as upstream

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "packaging" / "appliance" / "image" / "profiles"

DEVICE_CLASS_VAR = "IGconf_device_class"

# The device-tree ``compatible`` a Pi 3B+ and a Pi 3B actually present.
PI3_COMPATIBLES = (
    "raspberrypi,3-model-b-plus\x00brcm,bcm2837\x00",
    "raspberrypi,3-model-b\x00brcm,bcm2837\x00",
)


def image_rota_device_class_rule():
    """The rule a build enforces on ``IGconf_device_class``."""

    rules = upstream.var_requires_rules(upstream.read(upstream.IMAGE_ROTA))
    assert DEVICE_CLASS_VAR in rules, "image-rota no longer constrains the device class"
    return rules[DEVICE_CLASS_VAR]


# --- 1. upstream defines the board, and refuses it for this layout ------------


def test_upstream_defines_a_pi3_board_layer():
    """The refusal is image-rota's, not a missing board layer.

    Stated the other way round: adding a ``rpi3-ab`` profile would not fail for
    want of a device layer, which is exactly why the refusal has to be pinned.
    """

    layer = upstream.read(upstream.PI3_DEVICE_LAYER)
    assert upstream.layer_field(layer, "Name") == "rpi3"
    assert "keywords:pi3" in layer


def test_image_rota_refuses_the_pi3_device_class():
    rule = image_rota_device_class_rule()
    assert not upstream.rule_accepts(rule, "pi3"), (
        f"image-rota rule {rule!r} now accepts pi3 — the A/B layout may have "
        "gained Raspberry Pi 3 support upstream and this decision needs revisiting"
    )


def test_image_rota_accepts_every_device_class_the_project_builds():
    """The control: the same rule, evaluated the same way, passes rpi4 and rpi5."""

    rule = image_rota_device_class_rule()
    for profile in rpi_image_gen.HARDWARE_PROFILES.values():
        assert upstream.rule_accepts(rule, profile.device_class), profile.name


# --- 2. and 3. what the first partition is, and what a Pi 3 ROM needs ---------


def genimage_block(name):
    text = upstream.read("image/gpt/ab_userdata/genimage.cfg.in.ext4")
    return text.split(f"image {name} {{", 1)[1].split("\n}", 1)[0]


def test_the_first_partition_carries_no_second_stage_bootloader():
    """``bootconfig`` is the partition a Pi 3 boot ROM would have to boot from.

    It holds the selector and nothing else. Pi 4 and Pi 5 read their bootloader
    from EEPROM, so for them that is complete; a Pi 3 would find no
    ``bootcode.bin`` and stop before any of this project's code runs.
    """

    bootconfig = genimage_block("bootconfig.vfat")
    files = {
        line.split('"', 2)[1]
        for line in bootconfig.splitlines()
        if line.strip().startswith("file ")
    }
    assert files == {"autoboot.txt"}
    assert "bootcode.bin" not in bootconfig


def test_the_partition_table_is_gpt():
    """A Pi 3 boot ROM reads an MBR. The A/B layout is GPT."""

    assert 'partition-table-type = "gpt"' in genimage_block("<IMAGE_NAME>.<IMAGE_SUFFIX>")


# --- the project ships no Raspberry Pi 3 artefact ----------------------------


def test_the_project_ships_no_pi3_build_profile():
    declared = {path.stem for path in PROFILE_DIR.glob("*.yaml")}
    assert not {name for name in declared if "rpi3" in name}
    assert "rpi3" not in rpi_image_gen.HARDWARE_PROFILES
    assert "rpi3" not in build_authority.PROFILES


def test_the_build_entry_points_refuse_a_pi3_profile_by_name():
    """Refused at the identifier, before a generator or an output directory.

    All three release entry points — image build, update build and the release
    gate — validate through here first, so a ``--profile rpi3`` stops with
    ``build_identifier_invalid`` rather than producing a partial artefact tree
    for a board that cannot boot it.
    """

    with pytest.raises(build_authority.BuildAuthorityError) as refusal:
        build_authority.validate_profile("rpi3")
    assert refusal.value.code == build_authority.INVALID_IDENTIFIER
    for supported in ("rpi4", "rpi5"):
        assert build_authority.validate_profile(supported) == supported


def test_no_profile_selects_the_pi3_device_layer():
    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert (payload.get("device") or {}).get("layer") != "rpi3", path.name


# --- fail closed: an unsupported board is not a guessed one ------------------


def test_a_raspberry_pi_3_resolves_to_no_board_class():
    for compatible in PI3_COMPATIBLES:
        assert rpi_image_gen.board_class(compatible) == rpi_image_gen.BOARD_UNKNOWN


def test_a_raspberry_pi_3_is_not_offered_an_os_update():
    """The image that would be written is built for another SoC.

    Guessing a class here would flash a Pi 4 or Pi 5 kernel and firmware onto a
    Pi 3, which is recoverable only by re-imaging the medium.
    """

    for compatible in PI3_COMPATIBLES:
        board = rpi_image_gen.board_class(compatible)
        assert not rpi_image_gen.board_is_installable(board)
