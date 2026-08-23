# SPDX-License-Identifier: AGPL-3.0-or-later
"""The second image variant: one writable root, patched by apt.

Two layers exist because upstream leaves no choice -- it resolves a layer name
to its latest version only, refuses a colliding name and version, and derives
the overlay directory from the layer file's own stem, while both variants are
built from one source root. So the risk this module exists for is drift: two
files that were the same yesterday and quietly differ tomorrow.

Everything that is not about slots is therefore asserted *equal* to the A/B
layer rather than restated, which is also what makes the A/B layer's own tests
cover this one. What is left is the small set of differences that are the
variant, and each is pinned to the reason it exists.
"""

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
LAYER_DIR = ROOT / "packaging" / "appliance" / "image" / "layer"
AB = LAYER_DIR / "ems-appliance.yaml"
SINGLE = LAYER_DIR / "ems-appliance-single.yaml"
AB_OVERLAY = LAYER_DIR / "ems-appliance.rootfs-overlay"
SINGLE_OVERLAY = LAYER_DIR / "ems-appliance-single.rootfs-overlay"


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
    fields = metadata(SINGLE)

    assert fields["X-Env-Layer-Name"] == "ems-appliance-single"
    assert fields["X-Env-Layer-Requires"] == "image-rpios,docker-debian-trixie"
    assert fields["X-Env-VarPrefix"] == metadata(AB)["X-Env-VarPrefix"]


def test_the_two_layers_cannot_collide_in_upstreams_resolver():
    """Upstream keys on name plus version and refuses a duplicate."""

    ab, single = metadata(AB), metadata(SINGLE)

    assert ab["X-Env-Layer-Name"] != single["X-Env-Layer-Name"]


# --- what must never drift ---------------------------------------------------


def test_both_layers_install_exactly_the_same_packages():
    """One authority for the dependency list, which is the control file."""

    assert body(SINGLE)["mmdebstrap"]["packages"] == body(AB)["mmdebstrap"]["packages"]


@pytest.mark.parametrize("index,what", [(0, "the host-key deletion"), (1, "the dpkg install")])
def test_the_hooks_that_are_not_about_slots_are_byte_identical(index, what):
    assert hooks(SINGLE)[index] == hooks(AB)[index], what


def test_the_build_marker_differs_only_where_the_variant_does():
    """Provenance is one contract; a release gate must read either image."""

    ab_marker = hooks(AB)[2]
    single_marker = hooks(SINGLE)[2]

    for field in (
        '"architecture": "arm64"',
        '"build_id": "$IGconf_emsappliance_build_id"',
        '"release_version": "$IGconf_emsappliance_release_version"',
        '"manifest": "/etc/ems-appliance-os-packages"',
    ):
        assert field in ab_marker and field in single_marker, field

    assert '"image_layer": "image-rota"' in ab_marker
    assert '"image_layer": "image-rpios"' in single_marker


# --- what the variant is -----------------------------------------------------


def test_the_marker_names_image_rpios_because_the_runtime_reads_it():
    """Not decoration: appliance/ab_persistence.py fails closed without it.

    The persistence unit is anchored on this marker, so a single-slot image is
    asked to prove a contract it does not have. This field is the only thing
    that answers, and only a value the variant table recognises will do it.
    """

    from appliance import image_variants

    variant = image_variants.variant_of_image_layer("image-rpios")

    assert variant is not None and variant.has_ab_layout is False
    assert f'"image_layer": "{variant.image_layer}"' in hooks(SINGLE)[2]


def test_the_layer_writes_no_ab_layout_descriptor():
    """Its absence *is* the mechanism, not an omission.

    With no descriptor the runtime discovers MODE_SINGLE_SLOT, refuses every
    mutating A/B plan with a reason, and reports single_slot. There is nothing
    ``ab write-layout`` could emit that describes an image with one root.
    """

    assert any("ab write-layout" in hook for hook in hooks(AB))
    assert not any("ab write-layout" in hook for hook in hooks(SINGLE))
    # The variable it took its argument from is gone too, so nothing can
    # reintroduce a descriptor by supplying one.
    assert not any("layout_id" in hook for hook in hooks(SINGLE))
    assert "X-Env-Var-layout_id" not in text(SINGLE)


def test_the_kernel_command_line_is_made_writable_and_a_read_only_one_refused():
    """The A/B image does exactly the opposite, and for the same reason.

    apt on a read-only root is the failure this variant exists to avoid, so a
    ro that arrived from anywhere fails the build rather than shipping.
    """

    hook = hooks(SINGLE)[3]

    assert "asks for a read-only root" in hook
    assert "sed -i '1s|$| rw|'" in hook
    assert "sed -i '1s|$| ro|'" not in hook


def test_only_the_units_a_single_slot_host_can_run_are_enabled():
    enabled = hooks(SINGLE)[-1]

    assert "ems-appliance-agent.service" in enabled
    assert "ems-appliance-web.service" in enabled
    for unit in (
        "ems-appliance-host-identity.service",
        "ems-appliance-persistence.service",
        "ems-appliance-ab-health.service",
        "ems-appliance-slot-bootstrap.service",
        "ems-appliance-grow-persistent.service",
    ):
        assert unit not in enabled, unit


# --- the overlay -------------------------------------------------------------


def test_the_overlay_carries_only_what_is_not_about_slots():
    assert overlay_entries(SINGLE_OVERLAY) == [
        "etc/docker/daemon.json",
        "etc/systemd/journald.conf.d/50-ems-appliance.conf",
        "etc/systemd/system.conf.d/50-ems-appliance-watchdog.conf",
    ]


@pytest.mark.parametrize("relative", [
    "etc/docker/daemon.json",
    "etc/systemd/journald.conf.d/50-ems-appliance.conf",
    "etc/systemd/system.conf.d/50-ems-appliance-watchdog.conf",
])
def test_every_carried_overlay_file_is_byte_identical(relative):
    assert (SINGLE_OVERLAY / relative).read_bytes() == (AB_OVERLAY / relative).read_bytes()


@pytest.mark.parametrize("absent,why", [
    ("etc/rpi-image-gen", "a slot-shared declaration with no generator to read it"),
    (
        "etc/systemd/system/local-fs.target.wants",
        "bind mounts nothing generates without image-rota",
    ),
    ("etc/systemd/system/NetworkManager.service.d", "a guard for a mechanism that is absent"),
    ("etc/ssh/sshd_config.d", "host keys on a partition that does not exist"),
    ("etc/systemd/system/ssh.service.d", "a dependency on a unit that never runs"),
])
def test_the_overlay_declares_nothing_this_image_cannot_honour(absent, why):
    assert (AB_OVERLAY / absent).exists(), "the A/B overlay is what this is a subset of"
    assert not (SINGLE_OVERLAY / absent).exists(), why


# --- the configuration that selects it ---------------------------------------

IMAGE = ROOT / "packaging" / "appliance" / "image"
SHARED = IMAGE / "shared" / "ems-appliance-single.yaml"


def test_the_shared_configuration_selects_image_rpios_and_declares_no_table():
    """image-rpios owns the two partitions; only their sizes are ours."""

    shared = text(SHARED)

    assert "layer: image-rpios" in shared
    assert "trixie-minbase.yaml" in shared
    assert "app: ems-appliance-single" in shared
    for forbidden in ("partuuid:", "sfdisk", "partitions:", "type_guid"):
        assert forbidden not in shared.lower()


def test_the_partition_sizes_are_numbers_a_release_can_name():
    """Upstream's defaults are percentages of something this project has not
    verified the meaning of, and a release's evidence has to state a size."""

    config = yaml.safe_load(text(SHARED))["image"]

    for key in ("boot_part_size", "root_part_size"):
        assert not str(config[key]).endswith("%"), key
    assert config["rootfs_type"] == "ext4"


@pytest.mark.parametrize("board", ["rpi4", "rpi5"])
def test_each_single_slot_profile_adds_only_its_device_layer(board):
    profile = text(IMAGE / "profiles" / f"{board}-single.yaml")

    assert f"layer: {board}" in profile
    assert "../shared/ems-appliance-single.yaml" in profile
    for forbidden in ("partuuid:", "sfdisk", "partitions:", "type_guid"):
        assert forbidden not in profile.lower()


@pytest.mark.parametrize("board", ["rpi4", "rpi5"])
def test_a_single_slot_artefact_can_never_be_confused_with_an_ab_one(board):
    from appliance import rpi_image_gen

    profiles = rpi_image_gen.profiles()
    single = profiles[f"{board}-single"]
    paired = profiles[f"{board}-ab"]

    assert single.variant.slug == "single"
    assert single.artifact_basename("0.1.0") != paired.artifact_basename("0.1.0")
    assert single.artifact_basename("0.1.0").endswith(f"{board}-arm64-single")


# --- how it is built ---------------------------------------------------------

SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "appliance-build-rpi-ab-image.sh"
SINGLE_BUILDER = SCRIPTS / "appliance-build-rpi-single-image.sh"


def test_the_single_slot_entry_point_is_not_a_second_implementation():
    """Everything a release is signed on lives in one script.

    Two copies of the source proofs, the ambiguous-artefact refusal and the NOT
    RUN discipline is how the security-relevant half of one of them comes to
    differ from the other without anyone noticing.
    """

    script = text(SINGLE_BUILDER)

    assert "--variant single" in script
    assert "appliance-build-rpi-ab-image.sh" in script
    for owned_by_the_builder in ("assert_buildable", "sha256sum", "build_authority"):
        assert owned_by_the_builder not in script, owned_by_the_builder


def test_the_builder_refuses_a_variant_it_does_not_know():
    """Refused as a wrong command line, not resolved into a missing path."""

    script = text(BUILDER)

    assert "unknown variant: $VARIANT" in script
    assert "profiles/${PROFILE}-${VARIANT}.yaml" in script


def test_the_slot_mount_gate_runs_only_where_there_are_slots():
    """Running it here would prove something about an absent mechanism."""

    script = text(BUILDER)
    guarded = script.split('if [ "$VARIANT" = ab ]; then', 1)

    assert len(guarded) == 2
    assert "appliance-verify-slot-mounts.sh" in guarded[1].split("fi", 1)[0]


def test_no_update_archive_is_collected_for_an_image_that_cannot_install_one():
    script = text(BUILDER)
    block = script.split("UPDATE=\"\"", 1)[1].split("echo", 1)[0]

    assert 'if [ "$VARIANT" = ab ]; then' in block
    assert "update.tar.zst" in block


def test_the_build_metadata_names_the_variant_and_its_layer():
    script = text(BUILDER)

    assert '"image_variant": "$VARIANT"' in script
    assert '"image_layer": "$IMAGE_LAYER"' in script
    assert '"image_layer": "image-rota"' not in script


def test_the_builder_vm_builds_one_variant_per_run():
    """Two artefacts with two authorities; a run has to say which it is."""

    script = text(SCRIPTS / "appliance-builder-vm.sh")

    assert "unknown variant: $VARIANT" in script
    assert "${BUILD_ID}-${profile}-${VARIANT}" in script


@pytest.mark.parametrize("branch,marker", [
    ("build", "build_args=\"--profile $profile --variant $VARIANT\""),
    ("gate", "gate_args=\"--variant $VARIANT\""),
])
def test_both_of_the_builder_vms_branches_pass_the_variant_on(branch, marker):
    """The VM either builds or runs the gates, and both have to be told.

    The gate branch did not pass it on at first, so asking for a single-slot
    release gate would have measured the A/B gate list instead -- silently,
    because every gate in that list exists and most of them would have run.
    """

    assert marker in text(SCRIPTS / "appliance-builder-vm.sh"), branch


# --- what a release of it has to pass ----------------------------------------

GATES = SCRIPTS / "appliance-release-gates.sh"


def test_the_gate_runner_builds_one_variant_per_run():
    """The gate list itself differs between them, so one verdict cannot cover
    both -- it would be a verdict about neither."""

    script = text(GATES)

    assert "--variant is ab or single" in script
    assert '--variant "$VARIANT"' in script
    assert "arm64-${VARIANT}" in script


@pytest.mark.parametrize("gate", [
    "slot-mounts",
    "inspect-update-*",
    "describe-*",
    "sign-*",
    "verify-signature-*",
    "crosscheck-*",
])
def test_no_gate_about_an_absent_mechanism_is_required_of_a_single_slot_release(gate):
    """A gate this image does not have cannot be required of it.

    Stated in the script rather than left to follow from how those gates happen
    to be reported today: if that reporting ever became NOT RUN, a release
    would start failing on the absence of an archive it never produces.
    """

    script = text(GATES)
    guard = script.split("applies_to_variant() {", 1)[1].split("}", 1)[0]

    assert gate in guard


def test_an_absent_gate_is_named_rather_than_silently_dropped():
    """A gate list that quietly gets shorter is one nobody can tell apart from
    a gate list that was passed."""

    script = text(GATES)

    assert "NOT APPLICABLE (this image has one root and no binds)" in script
    assert "NOT APPLICABLE (this image has no update archive)" in script


def test_the_inspector_is_told_the_variant_rather_than_sniffing_it():
    """Deciding it from the file would judge an image by the contract it
    already satisfies rather than the one it was built to."""

    inspector = text(SCRIPTS / "appliance-inspect-rpi-ab-image.sh")

    assert "unknown variant: $VARIANT" in inspector
    assert 'variant=variant' in inspector
    assert 'if variant == "ab":' in inspector
