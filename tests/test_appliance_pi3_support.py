# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which appliance image a Raspberry Pi 3 gets, and which one it does not.

A Pi 3B+ is an arm64 board, so "does it run 64-bit Linux" is not the question.
The question is which of this project's two image shapes it can boot, and the
answer differs between them for reasons that belong to the layout rather than
to the board.

The A/B image it cannot boot. Three independent facts say so, each checked here
against the pinned upstream bytes so the decision cannot decay into a stale
paragraph while the build quietly grows a profile for it:

1. ``image-rota`` owns the A/B partition table, and its own metadata refuses the
   ``pi3`` device class.
2. The layout puts nothing but ``autoboot.txt`` on the first partition. A Pi 4
   or Pi 5 loads its bootloader from EEPROM and does not care; a Pi 3 boot ROM
   has to find a second-stage bootloader there.
3. The layout is GPT, and the Pi 3 boot ROM reads only an MBR.

The single-slot image has none of those three properties, and the same three
checks are run against it to prove that rather than assume it: ``image-rpios``
constrains no device class at all, its boot partition is the whole firmware
directory, and its table is an MBR.

So the board is recognised and gets one image, and the fail-closed half still
holds: there is no A/B artefact for it, and nothing may offer one.

See ``docs/appliance/adr/raspberry-pi-3-ab-support.md``.
"""

from pathlib import Path

import pytest
import yaml

from appliance import build_authority, image_variants, rpi_image_gen
from tests.helpers import upstream_rpi_image_gen as upstream

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

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


def test_image_rota_accepts_every_device_class_that_builds_an_ab_image():
    """The control: the same rule, evaluated the same way, passes rpi4 and rpi5.

    Restricted to the profiles that claim an A/B image, which is what makes the
    refusal above a statement about pi3 rather than about the rule.
    """

    rule = image_rota_device_class_rule()
    for profile in rpi_image_gen.HARDWARE_PROFILES.values():
        if profile.builds(image_variants.VARIANT_AB):
            assert upstream.rule_accepts(rule, profile.device_class), profile.name


# --- 2. and 3. what the first partition is, and what a Pi 3 ROM needs ---------


def genimage_block(name, path="image/gpt/ab_userdata/genimage.cfg.in.ext4"):
    text = upstream.read(path)
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


# --- the same three questions, asked of the single-slot layout ---------------


def test_image_rpios_constrains_no_device_class_at_all():
    """Finding 1 is image-rota's rule, not a property of the pi3 class."""

    rules = upstream.var_requires_rules(upstream.read(upstream.IMAGE_RPIOS))

    assert DEVICE_CLASS_VAR not in rules


def test_the_single_slot_boot_partition_is_the_whole_firmware_directory():
    """Finding 2 does not apply: this partition is where bootcode.bin lives.

    The A/B layout builds its first partition from a file list holding only
    autoboot.txt. This one mounts /boot/firmware into it, which is the
    directory a Pi 3 boot ROM has to find a second-stage bootloader in.
    """

    boot = genimage_block("boot.vfat", upstream.IMAGE_RPIOS_GENIMAGE)

    assert 'mountpoint = "/boot/firmware"' in boot
    assert "file " not in boot


def test_the_single_slot_partition_table_is_an_mbr():
    """Finding 3 does not apply: the Pi 3 boot ROM reads exactly this."""

    table = genimage_block("<IMAGE_NAME>.<IMAGE_SUFFIX>", upstream.IMAGE_RPIOS_GENIMAGE)

    assert 'partition-table-type = "mbr"' in table
    assert "partition-type = 0xC" in table, "the boot partition has to be FAT to the ROM"


def test_the_pi3_and_pi4_device_layers_want_the_same_kernel():
    """Not an assumption: both require upstream's generic 64-bit device base.

    It is why one image shape can serve both, and why a Pi 5 -- which requires
    rpi-linux-2712 instead -- cannot be treated the same way.
    """

    pi3 = upstream.layer_field(upstream.read(upstream.PI3_DEVICE_LAYER), "Requires")
    pi4 = upstream.layer_field(upstream.read("device/pi4/device.yaml"), "Requires")

    assert pi3 == pi4 == "rpi-generic64"


# --- the project ships one Raspberry Pi 3 artefact, and only one -------------


def test_the_project_ships_a_single_slot_pi3_profile_and_no_ab_one():
    declared = {path.stem for path in PROFILE_DIR.glob("*.yaml")}

    assert "rpi3-single" in declared
    assert "rpi3-ab" not in declared
    assert rpi_image_gen.HARDWARE_PROFILES["rpi3"].variants == (
        image_variants.VARIANT_SINGLE,
    )


def test_the_build_entry_points_refuse_a_pi3_ab_build():
    """Refused at the identifier, before a generator or an output directory.

    All three release entry points — image build, update build and the release
    gate — validate through here first, so an A/B build for a board that cannot
    boot one stops with ``build_identifier_invalid`` rather than producing a
    partial artefact tree.
    """

    assert build_authority.validate_profile("rpi3") == "rpi3"

    with pytest.raises(build_authority.BuildAuthorityError) as refusal:
        build_authority.validate_profile_variant("rpi3", image_variants.VARIANT_AB)
    assert refusal.value.code == build_authority.INVALID_IDENTIFIER

    assert (
        build_authority.validate_profile_variant("rpi3", image_variants.VARIANT_SINGLE)
        == image_variants.VARIANT_SINGLE
    )
    for supported in ("rpi4", "rpi5"):
        for variant in image_variants.VARIANTS:
            assert build_authority.validate_profile_variant(supported, variant) == variant


def test_only_the_single_slot_profile_selects_the_pi3_device_layer():
    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (payload.get("device") or {}).get("layer") == "rpi3":
            assert path.stem == "rpi3-single", path.name


def test_the_two_profile_lists_name_the_same_hardware():
    """One list of boards this project builds for, checked against the other."""

    assert build_authority.PROFILES == tuple(sorted(rpi_image_gen.HARDWARE_PROFILES))


# --- fail closed: no A/B artefact is offered for a board that has none -------


def test_a_raspberry_pi_3_is_recognised_as_the_board_it_is():
    """It has an image now, so an operator is told what their appliance is."""

    for compatible in PI3_COMPATIBLES:
        assert rpi_image_gen.board_class(compatible) == "pi3"


def test_a_raspberry_pi_3_is_still_not_offered_an_os_update():
    """The A/B image that would be written is built for another boot chain.

    A single-slot appliance has no update archive at all -- it is patched by
    apt -- so this stays false for a Pi 4 on the single variant too. What
    changed is the reason, not the answer.
    """

    for compatible in PI3_COMPATIBLES:
        board = rpi_image_gen.board_class(compatible)
        assert not rpi_image_gen.board_is_installable(board)
        assert not rpi_image_gen.board_is_installable(
            board, variant=image_variants.VARIANT_SINGLE
        )
    assert rpi_image_gen.board_is_installable("pi4")
    assert not rpi_image_gen.board_is_installable("pi4", variant=image_variants.VARIANT_SINGLE)


def test_a_pi3_is_told_it_has_no_ab_image_rather_than_no_profile():
    """Two different facts, and the older message was about to become wrong."""

    assert rpi_image_gen.INSTALLABLE_BOARD_CLASSES == ("pi4", "pi5")
