# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the appliance image layer has to contain.

The layer is where the appliance stops being a Raspberry Pi OS and starts being
this product: the package installed, the units enabled, the root made writable,
and the host keys the build chroot generated deleted again. Every one of those
is a thing a packaging mistake removes silently, so each is pinned to the reason
it exists rather than to the file it happens to live in.
"""

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
LAYER_DIR = ROOT / "packaging" / "appliance" / "image" / "layer"
LAYER = LAYER_DIR / "ems-appliance.yaml"
OVERLAY = LAYER_DIR / "ems-appliance.rootfs-overlay"


def text(path):
    return path.read_text(encoding="utf-8")


def metadata(path):
    head = text(path).split("# METAEND", 1)[0]
    fields = {}
    for line in head.splitlines():
        if not line.startswith("# X-Env-"):
            continue
        key, _, value = line[2:].partition(":")
        fields[key.strip()] = value.strip()
    return fields


def body(path):
    return yaml.safe_load(text(path).split("# METAEND\n---\n", 1)[1])


def hooks(path):
    return body(path)["mmdebstrap"]["customize-hooks"]


def overlay_entries(directory):
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() or path.is_symlink()
    )


# --- what makes it a different layer at all ----------------------------------


def test_the_layer_has_its_own_name_and_is_built_on_image_rpios():
    fields = metadata(LAYER)

    assert fields["X-Env-Layer-Name"] == "ems-appliance"
    assert fields["X-Env-Layer-Requires"] == "image-rpios,docker-debian-trixie"
    assert fields["X-Env-VarPrefix"] == metadata(LAYER)["X-Env-VarPrefix"]


# --- what the variant is -----------------------------------------------------


def test_the_marker_names_image_rpios_because_the_runtime_reads_it():
    """Not decoration: the first-boot growth gate fails closed without it.

    The growth unit is anchored on this marker, so an appliance image is
    asked to prove a contract it does not have. This field is the only thing
    that answers, and only a value the variant table recognises will do it.
    """

    from appliance import image_shape

    assert image_shape.image_layer_matches("image-rpios")
    assert f'"image_layer": "{image_shape.IMAGE.image_layer}"' in hooks(LAYER)[2]


def test_the_kernel_command_line_is_made_writable_and_a_read_only_one_refused():
    """apt on a read-only root is the failure this guards against, so a `ro`
    that arrived from anywhere fails the build rather than shipping."""

    hook = hooks(LAYER)[3]

    assert "asks for a read-only root" in hook
    assert "sed -i '1s|$| rw|'" in hook
    assert "sed -i '1s|$| ro|'" not in hook


def test_only_the_units_a_single_slot_host_can_run_are_enabled():
    enabled = hooks(LAYER)[-1]

    assert "ems-appliance-agent.service" in enabled
    assert "ems-appliance-web.service" in enabled
    # The medium an owner flashed is whatever they had; the image's root is
    # whatever the build declared. This is what claims the difference.
    assert "ems-appliance-grow-root.service" in enabled
    for unit in (
        "ems-appliance-host-identity.service",
        "ems-appliance-persistence.service",
        "ems-appliance-ab-health.service",
        "ems-appliance-slot-bootstrap.service",
        "ems-appliance-grow-persistent.service",
    ):
        assert unit not in enabled, unit


# --- the overlay -------------------------------------------------------------


# --- the configuration that selects it ---------------------------------------

IMAGE = ROOT / "packaging" / "appliance" / "image"
SHARED = IMAGE / "shared" / "ems-appliance.yaml"


def test_the_shared_configuration_selects_image_rpios_and_declares_no_table():
    """image-rpios owns the two partitions; only their sizes are ours."""

    shared = text(SHARED)

    assert "layer: image-rpios" in shared
    assert "trixie-minbase.yaml" in shared
    assert "app: ems-appliance" in shared
    for forbidden in ("partuuid:", "sfdisk", "partitions:", "type_guid"):
        assert forbidden not in shared.lower()


def test_the_partition_sizes_are_numbers_a_release_can_name():
    """Upstream's defaults are percentages of something this project has not
    verified the meaning of, and a release's evidence has to state a size."""

    config = yaml.safe_load(text(SHARED))["image"]

    for key in ("boot_part_size", "root_part_size"):
        assert not str(config[key]).endswith("%"), key
    assert config["rootfs_type"] == "ext4"


@pytest.mark.parametrize("board", ["rpi3", "rpi4", "rpi5"])
def test_each_profile_adds_only_its_device_layer(board):
    profile = text(IMAGE / "profiles" / f"{board}.yaml")

    assert f"layer: {board}" in profile
    assert "../shared/ems-appliance.yaml" in profile
    for forbidden in ("partuuid:", "sfdisk", "partitions:", "type_guid"):
        assert forbidden not in profile.lower()


# --- how it is built ---------------------------------------------------------

SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "appliance-build-rpi-image.sh"


# --- what a release of it has to pass ----------------------------------------

GATES = SCRIPTS / "appliance-release-gates.sh"


# --- claiming the rest of the medium -----------------------------------------

PACKAGING = ROOT / "packaging" / "appliance"
GROW_ROOT = PACKAGING / "bin" / "grow-root.sh"
GROW_ROOT_UNIT = PACKAGING / "systemd" / "ems-appliance-grow-root.service"


def test_the_growth_helper_and_its_unit_are_shipped_by_the_package():
    build = text(PACKAGING / "build-deb.sh")

    assert "systemd/ems-appliance-grow-root.service" in build
    assert "bin/grow-root.sh" in build
    assert GROW_ROOT.stat().st_mode & 0o111


def test_the_growth_unit_runs_once_and_only_on_an_imaged_medium():
    """A .deb installation on somebody else's Raspberry Pi OS has no marker,
    and the appliance promises never to repartition storage it did not write."""

    unit = text(GROW_ROOT_UNIT)

    assert "ConditionPathExists=/etc/ems-appliance-os-build" in unit
    assert "ConditionPathExists=!/var/lib/ems-appliance-manager/.root-grown" in unit
    assert "ExecStart=/usr/lib/ems-appliance-manager/grow-root.sh" in unit


def test_the_growth_unit_is_ordered_before_anything_that_fills_the_root():
    """Docker and the appliance write to the filesystem being resized."""

    unit = text(GROW_ROOT_UNIT)
    before = [line for line in unit.splitlines() if line.startswith("Before=")]

    assert before, "the growth must be ordered before its writers"
    joined = " ".join(before)
    for unit_name in ("ems-appliance-agent.service", "docker.service"):
        assert unit_name in joined, unit_name


FINALIZER = SCRIPTS / "appliance-finalize-rpi-release.sh"


