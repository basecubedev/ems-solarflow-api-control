# SPDX-License-Identifier: AGPL-3.0-or-later
"""The project's build configuration against the real pinned upstream contract.

Everything here is checked against bytes that came out of rpi-image-gen v2.7.0,
not against a fixture this project invented. That distinction is the point: a
layer name, a device class or a dependency entry the project believes in but
upstream does not is a build that fails after dependency installation, on a
build host, hours in — and no amount of self-consistent project fixtures would
have caught it.

When ``EMS_RPI_IMAGE_GEN`` names a real pinned checkout the same assertions run
against it, and the fixture is proven byte-identical to that tree.
"""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from appliance import rpi_image_gen
from tests.helpers import upstream_rpi_image_gen as upstream

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = ROOT / "packaging" / "appliance" / "image"
LOCK = IMAGE_DIR / "rpi-image-gen.lock"
PROFILE_DIR = IMAGE_DIR / "profiles"
LAYER_FILE = IMAGE_DIR / "layer" / "ems-appliance.yaml"

# image-rota refuses anything else: its metadata carries
# X-Env-VarRequires-Valid: ...,regex:^(cm4|pi4|cm5|pi5)$
ROTA_DEVICE_CLASSES = frozenset({"cm4", "pi4", "cm5", "pi5"})


@pytest.fixture
def lock():
    """The shipped lock without its tree pin.

    Tests that build a synthetic tree cannot satisfy the digest of the real one.
    The pin is exercised against that tree by
    test_the_pinned_tree_hash_matches_the_pinned_tree.
    """

    from dataclasses import replace

    return replace(rpi_image_gen.read_lock(LOCK), tree_sha256="")


def upstream_device_layers():
    """Every device layer name the pinned release exposes, by directory."""

    return {
        Path(relative).parts[1]: upstream.layer_field(upstream.read(relative), "Name")
        for relative in upstream.DEVICE_LAYERS
    }


def project_image_configs():
    """Every rpi-image-gen config this project builds from."""

    found = sorted(PROFILE_DIR.glob("*.yaml")) if PROFILE_DIR.is_dir() else []
    return found or sorted((IMAGE_DIR / "config").glob("*.yaml"))


# --- the fixture is the pinned release --------------------------------------


def test_the_contract_fixture_matches_its_recorded_digests():
    for relative, digest in upstream.fixture_files().items():
        blob = (upstream.FIXTURE / relative).read_bytes()
        assert "sha256:" + hashlib.sha256(blob).hexdigest() == digest, relative


def test_the_contract_fixture_pins_the_same_revision_as_the_lock(lock):
    payload = upstream.manifest()
    assert payload["commit"] == lock.commit
    assert payload["release"] == lock.release
    assert payload["repository"] == lock.repository


def test_the_contract_fixture_is_the_real_pinned_tree():
    source = upstream.real_source_tree()
    if source is None:
        pytest.skip(f"{upstream.SOURCE_ENV} does not name a pinned rpi-image-gen tree")
    for relative in upstream.fixture_files():
        if relative == "source-manifest.json":
            continue
        assert (source / relative).read_bytes() == (
            upstream.FIXTURE / relative
        ).read_bytes(), relative


# --- what upstream actually exposes -----------------------------------------


def test_upstream_names_its_device_layers_after_the_boards():
    assert upstream_device_layers() == {"pi3": "rpi3", "pi4": "rpi4", "pi5": "rpi5"}


def test_the_single_slot_root_is_writable_and_named_by_slot():
    """What makes apt possible, read out of upstream's own bytes.

    The A/B image mounts its root read-only, which is why it cannot be patched
    in place. image-rpios writes the opposite fstab, and points the kernel at
    the same by-slot name its udev rules create.
    """

    setup = upstream.read(upstream.IMAGE_RPIOS_SETUP)
    rules = upstream.read(upstream.IMAGE_RPIOS_SLOT_RULES)

    assert "/dev/disk/by-slot/system  /  ext4 rw," in setup
    assert "root=/dev/disk/by-slot/system" in setup
    assert 'ENV{ID_FS_LABEL}=="ROOT", SYMLINK+="disk/by-slot/system"' in rules


def test_upstream_single_slot_config_selects_image_rpios():
    payload = yaml.safe_load(upstream.read(upstream.UPSTREAM_SINGLE_CONFIG))
    assert payload["image"]["layer"] == "image-rpios"


def test_upstream_exposes_the_trixie_docker_layer_and_its_capability():
    trixie = upstream.read("layer/app-container/docker/engine-trixie.yaml")
    assert upstream.layer_field(trixie, "Name") == "docker-debian-trixie"
    assert "docker" in upstream.layer_list(trixie, "Provides")


def test_the_docker_capability_alone_is_ambiguous_upstream():
    """Two layers provide ``docker``, so the capability cannot name a suite.

    This is why the project layer requires the exact Trixie layer: upstream's
    provider index is first-wins, and a build that resolved ``docker`` would get
    whichever of the two loaded first.
    """

    providers = {
        upstream.layer_field(upstream.read(relative), "Name")
        for relative in upstream.DOCKER_LAYERS
        if "docker" in upstream.layer_list(upstream.read(relative), "Provides")
    }
    assert providers == {"docker-debian-trixie", "docker-debian-bookworm"}


# --- the project's build configuration --------------------------------------


def test_the_project_declares_one_profile_per_board_and_variant_it_claims():
    """Every shape a board claims exists, and nothing beyond it is offered.

    A board that claims a shape and has no profile for it is the failure this
    pins: an owner told the appliance supports their Pi finds no image of the
    kind they were told to flash. The claim is the profile's own ``variants``,
    which is not both for every board -- a Raspberry Pi 3 cannot boot the A/B
    image, so it does not offer one.
    """

    expected = set(rpi_image_gen.HARDWARE_PROFILES)
    names = {path.stem for path in PROFILE_DIR.glob("*.yaml")} if PROFILE_DIR.is_dir() else set()

    assert names == expected


def test_every_project_device_layer_resolves_upstream():
    """A device layer upstream does not define is a build that cannot start."""

    available = set(upstream_device_layers().values())
    for path in project_image_configs():
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        declared = (payload.get("device") or {}).get("layer")
        assert declared in available, f"{path.name} selects device layer {declared!r}"


def test_the_project_layer_requires_layers_upstream_defines():
    """``X-Env-Layer-Requires`` names layers, and every one must exist."""

    upstream_names = set(upstream_device_layers().values())
    for relative in (upstream.IMAGE_RPIOS,):
        upstream_names.add(upstream.layer_field(upstream.read(relative), "Name"))
    for relative in upstream.DOCKER_LAYERS:
        upstream_names.add(upstream.layer_field(upstream.read(relative), "Name"))

    required = upstream.layer_list(LAYER_FILE.read_text(encoding="utf-8"), "Requires")
    unresolved = [name for name in required if name not in upstream_names]
    assert not unresolved, f"the project layer requires {unresolved}, which upstream has no layer for"


def test_each_profile_claims_only_the_hardware_its_device_layer_is():
    for path in project_image_configs():
        profile = rpi_image_gen.read_profile(path)
        assert profile.compatible_board_classes
        assert all(
            entry.startswith(profile.device_class) or entry == profile.device_class
            for entry in profile.compatible_board_classes
        ), f"{path.name} claims {profile.compatible_board_classes}"


# --- host dependency enumeration --------------------------------------------


def test_package_only_dependencies_are_reported_as_missing(lock):
    """``all::python3`` declares a package with no binary to probe for.

    Eleven of the pinned release's entries have that shape. Skipping them turns
    a host that cannot build into one that reports it can.
    """

    report = rpi_image_gen.probe_dependencies(
        upstream.FIXTURE, lock, which=lambda tool: None, package_query=lambda _p: False
    )
    for package in ("python3-debian", "python3-jsonschema", "debian-archive-keyring"):
        assert package in report.missing_packages, f"{package} was not reported"
    assert len(report.missing_packages) == 11


def test_binary_dependencies_are_reported_by_their_package(lock):
    report = rpi_image_gen.probe_dependencies(
        upstream.FIXTURE, lock, which=lambda tool: None, package_query=lambda _p: True
    )
    assert "coreutils" in report.missing_binaries
    assert "dosfstools" in report.missing_binaries
    assert "uidmap" in report.missing_binaries
    assert report.missing_packages == ()


# --- pinned source identity --------------------------------------------------


def test_the_lock_pins_a_verifiable_tarball(lock):
    tarball = lock.tarball
    assert tarball["sha256"].startswith("sha256:")
    assert len(tarball["sha256"]) == len("sha256:") + 64
    assert lock.release in tarball["url"]
    assert tarball["top_level_directory"] == "rpi-image-gen-2.7.0"


# --- the slot-shared generator upstream actually ships ----------------------


@pytest.fixture
def generator_runner():
    if not upstream.namespaces_available():
        pytest.skip("a private mount namespace is required to run the upstream generator")
    return upstream.run_slot_shared_generator


# --- the real upstream resolver ----------------------------------------------


@pytest.fixture
def upstream_tooling():
    source = upstream.real_source_tree()
    if source is None:
        pytest.skip(f"{upstream.SOURCE_ENV} does not name a pinned rpi-image-gen tree")
    tooling, reason = upstream.site_tooling(source)
    if tooling is None:
        pytest.skip(reason)
    return source, tooling


def test_the_real_upstream_loader_resolves_every_project_profile(upstream_tooling):
    """Include chain and all: profile → shared → upstream's own A/B config."""

    source, (config_loader, _layer_manager) = upstream_tooling

    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        data = upstream.load_config(source, config_loader, path)
        profile = rpi_image_gen.read_profile(path)
        assert data["device"]["layer"] == profile.device_layer, path.name
        assert data["image"]["layer"] == profile.variant.image_layer, path.name
        assert data["layer"]["app"] == profile.variant.app_layer, path.name
        # Inherited through the chain, never restated per profile.
        assert data["image"]["rootfs_type"] == "ext4", path.name


def test_the_real_upstream_layer_manager_knows_every_layer_the_project_names(
    upstream_tooling,
):
    source, (_config_loader, layer_manager) = upstream_tooling
    manager = upstream.layer_index(source, layer_manager)

    required = set(upstream.layer_list(LAYER_FILE.read_text(encoding="utf-8"), "Requires"))
    for profile in rpi_image_gen.profiles().values():
        required.add(profile.device_layer)

    unknown = sorted(name for name in required if not manager.resolve_layer_name(name))
    assert unknown == []


@pytest.mark.parametrize("layer", ["ems-appliance", "ems-appliance-single"])
def test_the_project_layer_loads_and_its_dependencies_resolve(upstream_tooling, layer):
    """Each project layer, loaded by upstream beside upstream's own.

    Not a formality. The metadata block above METAEND is parsed as DEB822, so a
    line that is neither a field nor a space-indented continuation of one fails
    the entire layer to load -- and the failure upstream reports is "Layer
    'ems-appliance-single' not found", which reads like a missing file rather
    than an unparseable one. The single-slot layer's first real build ended
    exactly there, four seconds in, on an explanatory paragraph.
    """

    source, (_config_loader, layer_manager) = upstream_tooling
    manager = upstream.layer_index(
        source, layer_manager, extra_paths=[IMAGE_DIR / "layer"]
    )

    assert manager.resolve_layer_name(layer), f"upstream cannot load {layer}"
    satisfied, problems = manager.check_dependencies(layer)
    blocking = [item for item in problems if "missing required dependency" in item]
    assert blocking == [], blocking
    assert satisfied or not blocking


def test_the_pinned_tree_passes_the_projects_own_compatibility_probe(upstream_tooling):
    source, _tooling = upstream_tooling

    report = rpi_image_gen.probe_checkout(source)

    assert report.compatible, [
        finding.to_dict() for finding in report.findings if finding.result == rpi_image_gen.FAIL
    ]
    assert report.source_identity in (rpi_image_gen.SOURCE_GIT, rpi_image_gen.SOURCE_TARBALL)


def test_the_vendored_upstream_tree_is_in_the_licence_inventory():
    """Twelve upstream files are committed verbatim; the inventory has to say so.

    The repository ships a CI gate whose whole purpose is to keep that inventory
    true, and it cannot see this tree on its own.
    """

    root = Path(__file__).resolve().parents[1]
    inventory = (root / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    manifest = json.loads(
        (root / "tests" / "fixtures" / "rpi_image_gen" / "source-manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert "rpi-image-gen" in inventory
    assert manifest["release"] in inventory
    assert (root / "tests" / "fixtures" / "rpi_image_gen" / "UPSTREAM.LICENSE").is_file()
    assert "No other third-party source is vendored" in inventory


def test_the_lock_pins_the_tree_it_verifies_against():
    """The record beside an extracted tree is written by the same fetch that
    extracted it, so it cannot be the authority for it. This file is reviewed
    and committed, which is what makes it one."""

    from appliance import rpi_image_gen

    root = Path(__file__).resolve().parents[1]
    lock = rpi_image_gen.read_lock(
        root / "packaging" / "appliance" / "image" / "rpi-image-gen.lock"
    )

    assert lock.tree_sha256.startswith("sha256:")
    assert len(lock.tree_sha256) == len("sha256:") + 64


def test_the_pinned_tree_hash_matches_the_pinned_tree():
    """The pin is only worth anything if it is the digest of the real tree."""

    from appliance import rpi_image_gen

    source = upstream.real_source_tree()
    if source is None:
        pytest.skip(f"{upstream.SOURCE_ENV} names no pinned checkout")

    root = Path(__file__).resolve().parents[1]
    lock = rpi_image_gen.read_lock(
        root / "packaging" / "appliance" / "image" / "rpi-image-gen.lock"
    )

    assert rpi_image_gen.tree_digest(source) == lock.tree_sha256


def test_a_job_actually_names_a_tree_for_the_upstream_tier():
    """These tests skip unless something points them at a real tree, so without
    a job that fetches one they were five tests nobody had ever run."""

    root = Path(__file__).resolve().parents[1]
    workflows = root / ".github" / "workflows"
    naming = [
        path.name
        for path in workflows.glob("*.yml")
        if upstream.SOURCE_ENV in path.read_text(encoding="utf-8")
    ]

    assert naming, f"no workflow sets {upstream.SOURCE_ENV}"
