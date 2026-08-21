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
import shutil
from pathlib import Path

import pytest
import yaml

from appliance import rpi_image_gen
from tests.helpers import upstream_rpi_image_gen as upstream

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

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


def test_upstream_names_its_device_layers_rpi4_and_rpi5():
    assert upstream_device_layers() == {"pi4": "rpi4", "pi5": "rpi5"}


def test_upstream_ab_config_selects_the_rpi5_device_layer():
    payload = yaml.safe_load(upstream.read(upstream.UPSTREAM_AB_CONFIG))
    assert payload["device"]["layer"] == "rpi5"
    assert payload["image"]["layer"] == "image-rota"


def test_image_rota_accepts_only_the_pi4_and_pi5_device_classes(lock):
    text = upstream.read(upstream.IMAGE_ROTA)
    assert upstream.layer_field(text, "Name") == lock.image_layer
    assert upstream.layer_field(text, "Version") == lock.image_layer_version
    assert "regex:^(cm4|pi4|cm5|pi5)$" in text


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


def test_the_update_members_are_android_sparse_images():
    post_image = upstream.read("image/gpt/ab_userdata/post-image.sh")
    genimage = upstream.read("image/gpt/ab_userdata/genimage.cfg.in.ext4")
    assert "../system.sparse" in post_image and "../boot.sparse" in post_image
    assert "update.tar.zst" in post_image
    for name in ("boot.sparse", "system.sparse"):
        block = genimage.split(f"image {name} {{", 1)[1].split("}", 1)[0]
        assert "android-sparse" in block


# --- the project's build configuration --------------------------------------


def test_the_project_declares_one_profile_per_supported_board():
    names = {path.stem for path in PROFILE_DIR.glob("*.yaml")} if PROFILE_DIR.is_dir() else set()
    assert names == {"rpi4-ab", "rpi5-ab"}


def test_every_project_device_layer_resolves_upstream():
    """A device layer upstream does not define is a build that cannot start."""

    available = set(upstream_device_layers().values())
    for path in project_image_configs():
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        declared = (payload.get("device") or {}).get("layer")
        assert declared in available, f"{path.name} selects device layer {declared!r}"


def test_every_project_profile_is_a_device_class_image_rota_accepts():
    for path in project_image_configs():
        profile = rpi_image_gen.read_profile(path)
        assert profile.device_class in ROTA_DEVICE_CLASSES, path.name


def test_the_project_layer_requires_layers_upstream_defines():
    """``X-Env-Layer-Requires`` names layers, and every one must exist."""

    upstream_names = set(upstream_device_layers().values())
    upstream_names.add(upstream.layer_field(upstream.read(upstream.IMAGE_ROTA), "Name"))
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


def test_a_source_tree_without_provable_identity_is_not_compatible(tmp_path, lock):
    """A tarball extraction has no ``.git``; unknown identity is not a pass."""

    root = tmp_path / "rpi-image-gen"
    shutil.copytree(upstream.FIXTURE, root)
    (root / lock.executable).write_text("#!/bin/bash\n", encoding="utf-8")
    (root / lock.executable).chmod(0o755)
    (root / "LICENSE").write_text("upstream\n", encoding="utf-8")
    (root / "layer/rpi/device/slot-mapper/bin").mkdir(parents=True, exist_ok=True)
    (root / "layer/rpi/device/slot-mapper/bin/rpi-slot-label").write_text("#!/bin/sh\n")

    report = rpi_image_gen.probe_checkout(root, lock, which=lambda tool: f"/usr/bin/{tool}")
    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED


def test_the_lock_pins_a_verifiable_tarball(lock):
    tarball = lock.tarball
    assert tarball["sha256"].startswith("sha256:")
    assert len(tarball["sha256"]) == len("sha256:") + 64
    assert lock.release in tarball["url"]
    assert tarball["top_level_directory"] == "rpi-image-gen-2.7.0"


def test_a_recorded_tarball_identity_is_accepted(tmp_path, lock):
    root = tmp_path / "rpi-image-gen"
    shutil.copytree(upstream.FIXTURE, root)
    (root / lock.executable).write_text("#!/bin/bash\n", encoding="utf-8")
    (root / lock.executable).chmod(0o755)
    (root / "LICENSE").write_text("upstream\n", encoding="utf-8")
    (root / "layer/rpi/device/slot-mapper/bin").mkdir(parents=True, exist_ok=True)
    (root / "layer/rpi/device/slot-mapper/bin/rpi-slot-label").write_text("#!/bin/sh\n")
    rpi_image_gen.write_source_identity(
        root,
        form=rpi_image_gen.SOURCE_TARBALL,
        release=lock.release,
        commit=lock.commit,
        url=lock.tarball["url"],
        sha256=lock.tarball["sha256"],
        top_level_directory=lock.tarball["top_level_directory"],
    )

    report = rpi_image_gen.probe_checkout(root, lock, which=lambda tool: f"/usr/bin/{tool}")
    assert report.source_identity == "tarball"
    assert report.compatible
    assert report.tree_digest.startswith("sha256:")


# --- the slot-shared generator upstream actually ships ----------------------


@pytest.fixture
def generator_runner():
    if not upstream.namespaces_available():
        pytest.skip("a private mount namespace is required to run the upstream generator")
    return upstream.run_slot_shared_generator


def test_the_pinned_generator_leaves_all_but_one_mount_unactivated(
    generator_runner, tmp_path
):
    """Upstream links only the last generated mount into ``local-fs.target``.

    Its ``ln -sf`` sits outside both loops, so ``unit_name`` still holds the last
    path of the last configuration file. Six declared paths produce six mount
    units and one activation link; the five that are never activated fall back to
    the read-only root, silently, until the next slot switch discards everything
    written to them.
    """

    from appliance import ab_persistence

    result = generator_runner(
        {ab_persistence.SLOT_SHARED_CONF_NAME: ab_persistence.slot_shared_conf()},
        tmp_path / "generated",
    )
    expected = set(ab_persistence.shared_mount_units())

    assert set(result["units"]) == expected
    assert len(result["wants"]) == 1


def test_one_configuration_file_per_path_does_not_change_activation(
    generator_runner, tmp_path
):
    """Splitting the declaration is not a fix; the link is outside both loops."""

    from appliance import ab_persistence

    files = {
        f"{50 + index}-ems.conf": f"Version=1\nPath={shared.target}\n"
        for index, shared in enumerate(ab_persistence.SHARED_PATHS)
    }
    result = generator_runner(files, tmp_path / "generated")

    assert len(result["units"]) == len(ab_persistence.SHARED_PATHS)
    assert len(result["wants"]) == 1


def test_the_image_activates_every_mount_the_generator_leaves_behind(
    generator_runner, tmp_path
):
    """Generator output plus the links the image ships covers every path."""

    from appliance import ab_persistence

    result = generator_runner(
        {ab_persistence.SLOT_SHARED_CONF_NAME: ab_persistence.slot_shared_conf()},
        tmp_path / "generated",
    )
    shipped = {
        path.name
        for path in (
            IMAGE_DIR
            / "layer"
            / "ems-appliance.rootfs-overlay"
            / "etc/systemd/system/local-fs.target.wants"
        ).iterdir()
    }
    expected = set(ab_persistence.shared_mount_units())

    assert expected - (set(result["wants"]) | shipped) == set()


def test_the_escaped_unit_names_match_systemd_escape(generator_runner, tmp_path):
    """The project computes them without systemd; systemd has the last word."""

    from appliance import ab_persistence

    for shared in ab_persistence.SHARED_PATHS:
        assert ab_persistence.mount_unit_name(shared.target) == upstream.escaped_mount_unit(
            shared.target
        )


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
        assert data["image"]["layer"] == "image-rota", path.name
        assert data["layer"]["app"] == "ems-appliance", path.name
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


def test_the_project_layer_loads_and_its_dependencies_resolve(upstream_tooling):
    """The project layer, loaded by upstream beside upstream's own."""

    source, (_config_loader, layer_manager) = upstream_tooling
    manager = upstream.layer_index(
        source, layer_manager, extra_paths=[IMAGE_DIR / "layer"]
    )

    assert manager.resolve_layer_name("ems-appliance")
    satisfied, problems = manager.check_dependencies("ems-appliance")
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
