# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the package and the image build have to ship for A/B to be safe.

Two properties matter here and neither is visible from Python alone:

- the A/B units are in **both** slots, which they are by construction because
  the package installs them into the one rootfs image both slots receive;
- they are inert on a single-slot appliance, so the same .deb installs on an
  ordinary Raspberry Pi OS host without trying to mount a partition that is not
  there.

The build scripts are checked for the one property that makes them honest: a
host that cannot build reports NOT RUN rather than a pass.
"""

import json
import re
from pathlib import Path

import pytest

from appliance import ab_persistence
from appliance.ab_layout import parse_layout_manifest

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"
IMAGE = PACKAGING / "image"
SYSTEMD = PACKAGING / "systemd"
SCRIPTS = ROOT / "scripts"

AB_UNITS = ("ems-appliance-persistence.service", "ems-appliance-ab-health.service")
BUILD_SCRIPTS = (
    "appliance-build-rpi-ab-image.sh",
    "appliance-build-rpi-single-image.sh",
    "appliance-build-rpi-ab-update.sh",
    "appliance-inspect-rpi-ab-image.sh",
    "appliance-test-ab-layout.sh",
)


def read(path):
    return Path(path).read_text(encoding="utf-8")

def directives(text, key):
    """Every value of a directive; systemd accumulates repeated keys."""

    values = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            values.update(line[len(key) + 1 :].split())
    return values


# --- the units ---------------------------------------------------------------


# The layout descriptor is written only by the A/B image, so a unit conditioned
# on it cannot run anywhere else. The build marker is different: every appliance
# image writes it, including the single-slot one, so it does not make anything
# inert -- it says which image this is.
AB_LAYOUT_MARKER = "/etc/ems-appliance-manager/ab-layout.json"
OS_BUILD_MARKER = "/etc/ems-appliance-os-build"

# Conditioned on the descriptor, and therefore genuinely inert off an A/B image.
LAYOUT_GATED_UNITS = (
    "ems-appliance-ab-health.service",
    "ems-appliance-host-identity.service",
    "ems-appliance-slot-bootstrap.service",
    "ems-appliance-grow-persistent.service",
)


def conditions_of(unit):
    return [
        line.split("=", 1)[1]
        for line in read(SYSTEMD / unit).splitlines()
        if line.startswith("ConditionPathExists=")
    ]


@pytest.mark.parametrize("unit", LAYOUT_GATED_UNITS)
def test_every_layout_gated_unit_is_inert_without_an_ab_layout(unit):
    """The same package installs on a single-slot appliance and does nothing."""

    conditions = conditions_of(unit)

    assert conditions, f"{unit} would run on a single-slot appliance"
    assert AB_LAYOUT_MARKER in conditions, conditions


def test_the_persistence_unit_is_anchored_on_the_build_marker_instead():
    """Deliberately not the layout descriptor, and not inert as a result.

    The descriptor lives under /etc/ems-appliance-manager, one of the binds this
    unit exists to verify: a skipped bind would take the descriptor with it, the
    condition would read false, and the one unit that fails closed would quietly
    skip -- exactly when it is needed.

    The price is that a single-slot *image* writes that marker too, so the unit
    runs there. Nothing in the unit file can tell the two apart; the answer comes
    from appliance/ab_persistence.py, which reads the variant the marker names.
    """

    conditions = conditions_of("ems-appliance-persistence.service")

    assert conditions == [OS_BUILD_MARKER]
    assert AB_LAYOUT_MARKER not in conditions


@pytest.mark.parametrize("unit", AB_UNITS)
def test_every_ab_unit_is_shipped_by_the_package(unit):
    build = read(PACKAGING / "build-deb.sh")

    assert f"systemd/{unit}" in build


def test_persistence_is_verified_before_anything_that_writes():
    text = read(SYSTEMD / "ems-appliance-persistence.service")

    assert "RequiresMountsFor=/persistent" in text
    assert "Before=ems-appliance-agent.service ems-appliance-web.service" in text
    assert "Before=ems-appliance-slot-bootstrap.service ems-appliance-ab-health.service" in text


def test_persistence_verifies_upstreams_mounts_rather_than_making_its_own():
    """A second mount framework beside slot-shared would be a second authority."""

    text = read(SYSTEMD / "ems-appliance-persistence.service")

    assert "ExecStart=/usr/bin/ems-appliance ab verify-persistence --quiet" in text
    assert "mount-persistence" not in text
    assert "Restart=" not in text


@pytest.mark.parametrize(
    "unit", ["ems-appliance-agent.service", "ems-appliance-web.service"]
)
def test_every_writing_unit_requires_persistence_not_merely_orders_after_it(unit):
    text = read(SYSTEMD / unit)

    assert "ems-appliance-persistence.service" in directives(text, "Requires")
    assert "ems-appliance-persistence.service" in directives(text, "After")


def test_the_health_service_runs_after_everything_it_judges():
    text = read(SYSTEMD / "ems-appliance-ab-health.service")

    for unit in (
        "ems-appliance-persistence.service",
        "docker.service",
        "ems-appliance-agent.service ems-appliance-web.service",
    ):
        assert f"After={unit}" in text
    assert "Requires=ems-appliance-persistence.service" in text


def test_the_health_service_wants_but_never_requires_network_online():
    """A WAN outage is not a broken slot and must not roll back a good update."""

    text = read(SYSTEMD / "ems-appliance-ab-health.service")

    assert "Wants=network-online.target" in text
    assert "Requires=network-online.target" not in text


def test_the_health_window_is_bounded_and_never_retried():
    text = read(SYSTEMD / "ems-appliance-ab-health.service")
    timeout = int(re.search(r"TimeoutStartSec=(\d+)", text).group(1))

    assert 120 <= timeout <= 600
    assert "Restart=no" in text


def test_the_health_service_commits_only_through_the_cli(tmp_path):
    text = read(SYSTEMD / "ems-appliance-ab-health.service")

    assert "ExecStart=/usr/bin/ems-appliance ab trial-health --commit" in text


def test_the_postinst_enables_the_ab_units_and_starts_persistence_first():
    text = read(PACKAGING / "debian" / "postinst")

    assert "AB_UNITS=" in text
    for unit in AB_UNITS:
        assert unit in text
    assert "systemctl start ems-appliance-persistence.service" in text
    assert "local-fs.target.wants" in text


# --- the image configuration -------------------------------------------------


def test_rpi_image_gen_is_not_vendored():
    """The generator comes from the build host; only the config is ours."""

    entries = {item.name for item in IMAGE.iterdir()}

    assert entries >= {"README.md", "profiles", "shared", "layer", "rpi-image-gen.lock"}
    assert not (IMAGE / "rpi-image-gen").exists()


def test_the_project_declares_no_partition_layout_of_its_own():
    """image-rota owns the GPT; a second table would be a second authority."""

    assert not (IMAGE / "manifests").exists()
    assert not (IMAGE / "hooks").exists()
    for path in IMAGE.rglob("*"):
        if path.is_file() and path.suffix in (".yaml", ".yml", ".sh", ".json"):
            assert "sfdisk" not in read(path), path.name


def test_the_image_config_selects_image_rota_and_declares_no_partitions():
    shared = read(IMAGE / "shared" / "ems-appliance-ab.yaml")

    assert "layer: image-rota" in shared
    assert "include:" in shared
    assert "trixie-minbase-ab.yaml" in shared
    # Prose may name them; a declaration may not.
    for forbidden in ("partuuid:", "sfdisk", "partitions:", "type_guid"):
        assert forbidden not in shared.lower()


def test_each_hardware_profile_adds_only_its_device_layer():
    """One image per board. A shared base, and one line that differs."""

    for name, layer in (("rpi4-ab", "rpi4"), ("rpi5-ab", "rpi5")):
        profile = read(IMAGE / "profiles" / f"{name}.yaml")
        assert f"layer: {layer}" in profile
        assert "../shared/ems-appliance-ab.yaml" in profile
        for forbidden in ("partuuid:", "sfdisk", "partitions:", "type_guid"):
            assert forbidden not in profile.lower()


def test_the_project_layer_installs_the_package_and_enables_the_units():
    layer = read(IMAGE / "layer" / "ems-appliance.yaml")

    assert "X-Env-Layer-Name: ems-appliance" in layer
    assert "X-Env-Layer-Requires: image-rota,docker-debian-trixie" in layer
    assert "dpkg -i" in layer
    assert "ems-appliance-os-build" in layer
    assert "ab write-layout" in layer
    for unit in (*AB_UNITS, "ems-appliance-slot-bootstrap.service"):
        assert unit in layer


def test_the_build_marker_records_what_dpkg_answered_inside_the_chroot():
    """image-rota binds /var per slot, so the shipped root has no database.

    Without this record a release gate has nothing exact to compare the
    package name, version, architecture and installation status against, and
    the version check falls back to matching a substring.
    """

    layer = read(IMAGE / "layer" / "ems-appliance.yaml")

    assert "dpkg-query -W" in layer
    assert "${Package}" in layer and "${Version}" in layer
    assert "${Architecture}" in layer and "${Status}" in layer
    assert '"package": {' in layer
    # A build whose package is not configured must not produce an image.
    assert '"install ok installed"' in layer


def test_the_layer_declares_every_shared_path_to_upstreams_generator():
    conf = read(
        IMAGE
        / "layer"
        / "ems-appliance.rootfs-overlay"
        / "etc"
        / "rpi-image-gen"
        / "slot-shared.d"
        / "50-ems-appliance.conf"
    )

    assert conf == ab_persistence.slot_shared_conf()


def test_the_sshd_drop_in_shares_only_the_host_keys():
    drop_in = read(
        IMAGE
        / "layer"
        / "ems-appliance.rootfs-overlay"
        / "etc"
        / "ssh"
        / "sshd_config.d"
        / "50-ems-appliance-hostkeys.conf"
    )

    assert ab_persistence.SSH_HOST_KEY_DIRECTORY in drop_in
    assert "PasswordAuthentication" not in drop_in
    assert "Port " not in drop_in


def test_the_persistent_partition_is_grown_only_once_on_a_fresh_medium():
    """Repartitioning a running installation is never reachable from anywhere."""

    unit = read(SYSTEMD / "ems-appliance-grow-persistent.service")
    script = read(PACKAGING / "bin" / "grow-persistent.sh")

    assert "ConditionPathExists=!/persistent/.grown" in unit
    assert "grow-persistent.sh" in unit
    assert "growpart" in script
    assert "$MARKER" in script


def test_the_growth_script_is_the_only_thing_that_can_repartition():
    """It is a shell unit precisely so no request-handling code path can.

    The allowlist is the real gate: commands.py refuses to run anything that is
    not in it. Naming a tool is not invoking one -- verify-install has to know
    growpart exists in order to report it missing -- so what is checked here is
    that no module hands one of these to a runner.
    """

    import ast

    from appliance.commands import EXECUTABLES

    repartitioning = {"growpart", "resize2fs", "sfdisk", "parted", "sgdisk"}
    for tool in repartitioning:
        assert tool not in EXECUTABLES, tool

    for path in (ROOT / "appliance").glob("*.py"):
        tree = ast.parse(read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in repartitioning:
                raise AssertionError(f"{path.name} invokes {first.value}")


# --- no in-place conversion anywhere ----------------------------------------


@pytest.mark.parametrize(
    "tool", ["sfdisk", "parted", "sgdisk", "growpart", "resize2fs", "mkfs"]
)
def test_no_appliance_module_ever_runs_a_partitioning_tool(tool):
    for path in (ROOT / "appliance").glob("*.py"):
        source = read(path)
        for call in (f'run("{tool}', f"run('{tool}", f'resolve("{tool}'):
            assert call not in source, (path.name, call)


def test_the_image_inspector_only_reads():
    """ab_image reads a GPT out of a file; it never attaches or runs anything."""

    source = read(ROOT / "appliance" / "ab_image.py")

    assert "subprocess" not in source
    assert "os.system" not in source
    assert "losetup" not in source


def test_the_command_allowlist_has_no_partitioning_tool():
    from appliance.commands import EXECUTABLES

    for tool in ("sfdisk", "parted", "sgdisk", "growpart", "resize2fs",
                 "mkfs.ext4", "mkfs.vfat", "dd"):
        assert tool not in EXECUTABLES, tool


# --- the build scripts -------------------------------------------------------


@pytest.mark.parametrize("name", BUILD_SCRIPTS)
def test_every_build_script_is_executable(name):
    path = SCRIPTS / name

    assert path.is_file(), name
    assert path.stat().st_mode & 0o111, name


@pytest.mark.parametrize(
    "name", ["appliance-build-rpi-ab-image.sh", "appliance-build-rpi-ab-update.sh"]
)
def test_a_build_that_could_not_happen_is_never_reported_as_a_pass(name):
    text = read(SCRIPTS / name)

    assert "RESULT: NOT RUN" in text
    assert "required_tool_missing" in text


@pytest.mark.parametrize("name", [*BUILD_SCRIPTS, "appliance-build-rpi-ab-update.sh"])
def test_no_build_script_publishes_anything(name):
    text = read(SCRIPTS / name)

    for forbidden in ("docker push", "gh release", "curl -X POST", "git push", "npm publish"):
        assert forbidden not in text, forbidden


def test_the_update_builder_never_embeds_signing_material():
    text = read(SCRIPTS / "appliance-build-rpi-ab-update.sh")

    assert "--local-user" in text
    assert "--export-secret-keys" not in text
    assert "gpg --gen-key" not in text
    assert "the runtime refuses this artifact" in text


def test_the_inspector_reports_what_it_could_not_check():
    text = read(SCRIPTS / "appliance-inspect-rpi-ab-image.sh")

    assert "NOT RUN" in text
    assert "no loop device, no mount, no root" in text


def test_the_test_runner_names_the_tier_it_did_not_run():
    text = read(SCRIPTS / "appliance-test-ab-layout.sh")

    assert "loop-device tier: NOT RUN" in text
    assert "image inspection: NOT RUN" in text


# --- the derived artefacts agree with the runtime ----------------------------


def test_the_descriptor_the_image_writes_parses_as_the_runtime_layout(tmp_path):
    """The build fails rather than shipping a descriptor the runtime rejects."""

    from appliance.cli import main

    target = tmp_path / "ab-layout.json"
    assert main(["ab", "write-layout", "--output", str(target)]) == 0

    manifest = parse_layout_manifest(json.loads(read(target)))

    assert {name for name in manifest.slots} == {"A", "B"}
    assert manifest.slot("A").root.label == "system_a"
    assert manifest.slot("B").root.label == "system_b"
    assert manifest.bootconfig.label == "bootconfig"
    assert manifest.persist_mountpoint == "/persistent"
    assert manifest.selector_mountpoint == "/bootfs"


def test_the_descriptor_the_image_writes_names_no_partition_identity(tmp_path):
    from appliance.cli import main

    target = tmp_path / "ab-layout.json"
    main(["ab", "write-layout", "--output", str(target)])

    assert "partuuid" not in read(target).lower()
    assert "partition" not in json.loads(read(target))


def test_the_documented_docker_decision_matches_the_contract():
    document = read(ROOT / "docs" / "appliance" / "ab-persistence-contract.md")

    assert "/var/lib/docker" in document
    assert "slot-local" in document
    assert "/var/lib/docker" in ab_persistence.SLOT_LOCAL_PATHS


def test_every_shared_path_the_contract_declares_is_documented():
    document = read(ROOT / "docs" / "appliance" / "ab-persistence-contract.md")

    for shared in ab_persistence.SHARED_PATHS:
        assert shared.target in document, shared.name


def test_the_package_enables_and_starts_the_host_identity_unit():
    """Shipping the unit is not enough; an unenabled one never runs.

    The image layer enables it at build time, but a package installed onto an
    existing A/B host has to enable it too — otherwise sshd is pointed at host
    keys nothing ever creates.
    """

    postinst = read(ROOT / "packaging" / "appliance" / "debian" / "postinst")

    assert "ems-appliance-host-identity.service" in postinst.split("AB_UNITS=")[1].split('"')[1]
    assert "systemctl start ems-appliance-host-identity.service" in postinst
    # Before persistence verification, which is what the unit ordering says too.
    assert postinst.index("start ems-appliance-host-identity.service") < postinst.index(
        "start ems-appliance-persistence.service"
    )


# --- what the image layer has to install for the package to configure ---------


def _declared_dependencies():
    control = read(PACKAGING / "debian" / "control")
    field = re.search(r"^Depends:(.*?)(?=^\S)", control, re.MULTILINE | re.DOTALL)
    assert field, "the package must declare its runtime dependencies"
    return [
        entry.strip().split()[0]
        for entry in field.group(1).replace("\n", " ").split(",")
        if entry.strip()
    ]


def _layer_packages():
    layer = read(PACKAGING / "image" / "layer" / "ems-appliance.yaml")
    return re.findall(r"^\s+- (\S+)$", layer.split("customize-hooks")[0], re.MULTILINE)


def test_the_image_layer_installs_every_dependency_the_package_declares():
    """A dependency a base layer happens to pull in is not a dependency that is met.

    The layer installs the .deb with ``dpkg -i``, which resolves nothing: a
    declared dependency no other layer brought along leaves the package
    unconfigured and fails the whole image build. That is what happened to the
    first real rpi5 build — ``acl`` and ``gpgv`` were declared by the package
    and installed by nobody.

    So the layer names them all. The control file stays the single authority;
    this keeps the layer's list derived from it rather than from whatever the
    base happened to include on the day.
    """

    missing = [name for name in _declared_dependencies() if name not in _layer_packages()]

    assert not missing, f"the image layer does not install: {', '.join(missing)}"


def test_the_image_ships_no_host_private_keys():
    """openssh-server's postinst makes a key pair when it is installed.

    That happens inside the build chroot, so the image carries one — the same
    one in both slots, generated on the build machine, and published with every
    release. The first real rpi5 image did: /etc/ssh/ssh_host_ed25519_key in
    both system slots, commented ``root@ems-appliance-builder``.

    sshd uses the persistent pair because the image's drop-in names it, but a
    private key that ships in a public artefact is compromised whether or not
    anything reads it, and a slot whose drop-in did not apply falls back to
    exactly these. The appliance's identity is created on first boot, onto the
    persistent partition, and there is nothing for the image to carry.
    """

    layer = read(PACKAGING / "image" / "layer" / "ems-appliance.yaml")

    assert "/etc/ssh/ssh_host_" in layer, (
        "the layer must remove the host keys openssh-server generated in the chroot"
    )
    assert "rm -f" in layer


def test_the_image_installs_the_mdns_responder_every_document_relies_on():
    """``ems-solarflow.local`` is the only address the documentation gives.

    The appliance's own hostname path restarts ``avahi-daemon.service`` and its
    command allowlist carries ``avahi-resolve``, so the responder is not
    optional here: without it the first thing a new owner is told to type
    cannot resolve, and no document names an alternative.
    """

    packages = _layer_packages()

    assert "avahi-daemon" in packages
    assert "avahi-utils" in packages


def test_the_documented_schema_version_is_the_one_the_code_declares():
    """A header that drifts silently is worse than no header: the schema version
    is what binds a slot's state to the code that may read it."""

    document = read(ROOT / "docs" / "appliance" / "ab-persistence-contract.md")

    assert (
        f"Schema version: **{ab_persistence.PERSISTENT_SCHEMA_VERSION}**." in document
    ), "the persistence contract names a different schema version than the code"


def test_the_hardware_kit_counts_the_shared_paths_it_prints():
    """The operator is told how many binds to expect; a hardcoded count drifts."""

    kit = read(ROOT / "scripts" / "appliance-hardware-validation-kit.sh")

    assert "len(ab_persistence.SHARED_PATHS)" in kit
    for spelled in (" all six ", " all seven ", " all eight "):
        assert spelled not in kit, f"the kit hardcodes a count: {spelled.strip()}"
