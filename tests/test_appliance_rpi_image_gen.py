# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether a checkout is the rpi-image-gen release this appliance is defined by.

``image-rota`` owns the partition table, the slot labels and the shared-slot
mount mechanism, so a generator that differs produces an image whose layout
nothing at runtime agrees with. That has to be a refusal with a stable reason
code, never a fallback to the layout this project used to generate itself.
"""

import json
from pathlib import Path

import pytest

from appliance import rpi_image_gen
from appliance.rpi_image_gen import FAIL, PASS

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging" / "appliance" / "image" / "rpi-image-gen.lock"

SHARED_GENERATOR = (
    "image/gpt/ab_userdata/device/rootfs-overlay/usr/lib/systemd/"
    "system-generators/slot-shared-generator"
)
PERSIST_GENERATOR = (
    "image/gpt/ab_userdata/device/rootfs-overlay/usr/lib/systemd/"
    "system-generators/slot-perst-generator"
)


@pytest.fixture
def lock():
    return rpi_image_gen.read_lock(LOCK)


def write(root, relative, text, *, mode=0o644):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def checkout(tmp_path, lock, **overrides):
    """A checkout that satisfies the pinned contract, before any override."""

    root = tmp_path / "rpi-image-gen"
    write(root, lock.executable, "#!/bin/bash\n", mode=0o755)
    write(root, "LICENSE", "upstream\n")
    write(root, "depends", "all:bash\nbuild:mmdebstrap\n")
    write(
        root,
        lock.image_layer_path,
        "# METABEGIN\n"
        f"# X-Env-Layer-Name: {overrides.get('layer_name', lock.image_layer)}\n"
        f"# X-Env-Layer-Version: {overrides.get('layer_version', lock.image_layer_version)}\n"
        "# METAEND\n",
    )
    write(
        root,
        "image/gpt/ab_userdata/post-image.sh",
        "upd=${1}/update\n"
        "ln -sf ../system.sparse ${upd}/system\n"
        "ln -sf ../boot.sparse ${upd}/boot\n"
        "tar -I zstd -h -cf ${1}/update.tar.zst -- *\n",
    )
    write(root, "image/gpt/ab_userdata/genimage.cfg.in.ext4", "partition boot_a {}\n")
    write(
        root,
        SHARED_GENERATOR,
        f"CONF_DIR={lock.shared_slot_conf_dir}\nshared_path={lock.shared_root}$path\n",
        mode=0o755,
    )
    write(root, PERSIST_GENERATOR, "#!/bin/sh\n", mode=0o755)
    write(root, "layer/rpi/device/slot-mapper/bin/rpi-slot-label", "#!/bin/sh\n", mode=0o755)
    write(root, "config/trixie-minbase-ab.yaml", "image:\n  layer: image-rota\n")
    if overrides.get("revision", lock.commit):
        write(root, ".git/HEAD", f"{overrides.get('revision', lock.commit)}\n")
    return root


def satisfied(_binary):
    return "/usr/bin/stub"


def probe(root, lock, **kwargs):
    kwargs.setdefault("which", satisfied)
    return rpi_image_gen.probe_checkout(root, lock, **kwargs)


def failed(report):
    return {finding.check for finding in report.findings if finding.result == FAIL}


# --- the pinned lock ---------------------------------------------------------


def test_the_lock_pins_an_exact_upstream_revision(lock):
    assert lock.repository == "https://github.com/raspberrypi/rpi-image-gen"
    assert lock.release == "v2.7.0"
    assert len(lock.commit) == 40
    assert lock.image_layer == "image-rota"
    assert lock.shared_slot_mechanism == "slot-shared"


def test_the_lock_records_the_real_update_artifact(lock):
    assert lock.update_archive == "update.tar.zst"
    assert lock.update_members == ("boot", "system")


def test_the_lock_records_the_upstream_mountpoints(lock):
    assert lock.persistent_mountpoint == "/persistent"
    assert lock.shared_root == "/persistent/shared"
    assert lock.bootconfig_mountpoint == "/bootfs"
    assert lock.machine_id_source == "/persistent/common/etc/machine-id"


def test_an_unreadable_lock_is_an_error(tmp_path):
    with pytest.raises(rpi_image_gen.ImageGenError) as excinfo:
        rpi_image_gen.read_lock(tmp_path / "absent.lock")

    assert excinfo.value.code == "lock_unreadable"


def test_an_incomplete_lock_is_an_error(tmp_path):
    path = tmp_path / "partial.lock"
    path.write_text(json.dumps({"repository": "x"}), encoding="utf-8")

    with pytest.raises(rpi_image_gen.ImageGenError) as excinfo:
        rpi_image_gen.read_lock(path)

    assert excinfo.value.code == "lock_invalid"


# --- a compliant checkout ----------------------------------------------------


def test_a_pinned_checkout_is_compatible_and_buildable(tmp_path, lock):
    report = probe(checkout(tmp_path, lock), lock)

    assert report.compatible
    assert report.buildable
    assert report.reason == ""
    assert not failed(report)


def test_a_checkout_missing_build_dependencies_is_compatible_but_not_buildable(tmp_path, lock):
    report = probe(
        checkout(tmp_path, lock), lock, which=lambda binary: None, package_query=lambda _p: True
    )

    assert report.compatible
    assert not report.buildable
    assert report.reason == rpi_image_gen.REASON_DEPENDENCIES
    assert "mmdebstrap" in report.missing_dependencies


# --- refusals ----------------------------------------------------------------


def test_a_missing_checkout_is_unavailable(tmp_path, lock):
    report = probe(tmp_path / "absent", lock)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_UNAVAILABLE


def test_a_build_sh_checkout_is_refused(tmp_path, lock):
    """The interface this project used to assume is not an upstream release."""

    root = checkout(tmp_path, lock)
    write(root, "build.sh", "#!/bin/sh\n", mode=0o755)

    report = probe(root, lock)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_INCOMPATIBLE
    assert "refused:build.sh" in failed(report)


def test_a_checkout_without_the_executable_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock)
    (root / lock.executable).unlink()

    report = probe(root, lock)

    assert not report.compatible
    assert "executable" in failed(report)


def test_a_non_executable_generator_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock)
    (root / lock.executable).chmod(0o644)

    report = probe(root, lock)

    assert "executable" in failed(report)


def test_a_checkout_without_image_rota_is_refused(tmp_path, lock):
    report = probe(checkout(tmp_path, lock, layer_name="image-base"), lock)

    assert not report.compatible
    assert "image_layer" in failed(report)


def test_a_different_image_rota_version_is_refused(tmp_path, lock):
    """A layout change upstream changes what this appliance is."""

    report = probe(checkout(tmp_path, lock, layer_version="6.0.0"), lock)

    assert not report.compatible
    assert "image_layer_version" in failed(report)


def test_a_checkout_without_the_shared_slot_generator_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock)
    (root / SHARED_GENERATOR).unlink()

    report = probe(root, lock)

    assert not report.compatible
    assert "shared_slot" in failed(report)


def test_a_shared_slot_generator_with_another_contract_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock)
    write(root, SHARED_GENERATOR, "CONF_DIR=/etc/somewhere-else\n", mode=0o755)

    report = probe(root, lock)

    assert "shared_slot" in failed(report)


def test_a_checkout_that_packs_other_update_members_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock)
    write(
        root,
        "image/gpt/ab_userdata/post-image.sh",
        "ln -sf ../system.sparse ${upd}/rootfs\ntar -cf ${1}/update.tar.zst -- *\n",
    )

    report = probe(root, lock)

    assert "update_artifact" in failed(report)


def test_a_missing_required_path_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock)
    (root / "layer/rpi/device/slot-mapper/bin/rpi-slot-label").unlink()

    report = probe(root, lock)

    assert "path:layer/rpi/device/slot-mapper/bin/rpi-slot-label" in failed(report)


# --- source identity ---------------------------------------------------------


def identity(report):
    return next(finding for finding in report.findings if finding.check == "source_identity")


def test_a_tree_with_no_provable_identity_is_refused(tmp_path, lock):
    """A tarball extraction has no .git, and unknown is not a NOT RUN."""

    report = probe(checkout(tmp_path, lock, revision=""), lock)

    assert identity(report).result == FAIL
    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED
    assert report.source_identity == rpi_image_gen.SOURCE_UNVERIFIED


def test_a_checkout_at_the_pinned_revision_passes(tmp_path, lock):
    report = probe(checkout(tmp_path, lock), lock)

    assert identity(report).result == PASS
    assert report.source_identity == rpi_image_gen.SOURCE_GIT


def test_a_checkout_at_another_revision_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock, revision="")
    write(root, ".git/HEAD", "ref: refs/heads/master\n")
    write(root, ".git/refs/heads/master", "0" * 40 + "\n")

    report = probe(root, lock)

    assert not report.compatible
    assert "source_identity" in failed(report)
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED


def test_a_tarball_tree_recorded_by_the_fetch_script_is_accepted(tmp_path, lock):
    root = checkout(tmp_path, lock, revision="")
    write(
        root,
        rpi_image_gen.SOURCE_IDENTITY_NAME,
        json.dumps(
            {
                "form": "tarball",
                "release": lock.release,
                "commit": lock.commit,
                "url": lock.tarball["url"],
                "sha256": lock.tarball["sha256"],
                "top_level_directory": lock.tarball["top_level_directory"],
            }
        ),
    )

    report = probe(root, lock)

    assert identity(report).result == PASS
    assert report.source_identity == rpi_image_gen.SOURCE_TARBALL
    assert report.compatible


def test_a_tarball_record_naming_another_digest_is_refused(tmp_path, lock):
    root = checkout(tmp_path, lock, revision="")
    write(
        root,
        rpi_image_gen.SOURCE_IDENTITY_NAME,
        json.dumps(
            {
                "form": "tarball",
                "release": lock.release,
                "commit": lock.commit,
                "url": lock.tarball["url"],
                "sha256": "sha256:" + "0" * 64,
                "top_level_directory": lock.tarball["top_level_directory"],
            }
        ),
    )

    report = probe(root, lock)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED


# --- host dependencies -------------------------------------------------------


def test_package_only_entries_are_probed_through_the_package_database(tmp_path, lock):
    root = checkout(tmp_path, lock)
    write(root, "depends", "all:bash\nbuild::python3-jsonschema\nbuild::dctrl-tools\n")

    report = probe(root, lock, package_query=lambda package: package != "dctrl-tools")

    assert report.dependencies.missing_packages == ("dctrl-tools",)
    assert "python3-jsonschema" in report.dependencies.resolved
    assert not report.buildable


def test_package_only_entries_with_no_package_database_are_not_assumed_present(tmp_path, lock):
    root = checkout(tmp_path, lock)
    write(root, "depends", "all:bash\nbootstrap::python3-debian\n")

    report = probe(root, lock, package_query=None)

    assert report.dependencies.unverified_packages == ("python3-debian",)
    assert not report.buildable
    assert report.reason == rpi_image_gen.REASON_DEPENDENCIES


# --- layer metadata ----------------------------------------------------------


def test_layer_metadata_reads_the_upstream_header_block():
    name, version = rpi_image_gen.layer_metadata(
        "# METABEGIN\n# X-Env-Layer-Name: image-rota\n# X-Env-Layer-Version: 5.5.1\n# METAEND\n"
    )

    assert (name, version) == ("image-rota", "5.5.1")


def test_layer_metadata_of_a_file_without_a_header_is_empty():
    assert rpi_image_gen.layer_metadata("mmdebstrap:\n  packages: []\n") == ("", "")


# --- the build host ----------------------------------------------------------


def test_a_native_arm64_host_needs_no_emulation():
    host = rpi_image_gen.build_host_state(machine="aarch64")

    assert host.missing_binfmt == ()
    assert host.unsupported_architecture == ""
    assert host.buildable


def test_an_amd64_host_without_the_handler_reports_the_binfmt_class(tmp_path):
    host = rpi_image_gen.build_host_state(
        machine="x86_64", binfmt_path=tmp_path / "absent"
    )

    assert host.missing_binfmt
    assert "qemu-aarch64" in host.missing_binfmt[0]
    assert host.unsupported_architecture == ""
    assert not host.buildable


def test_an_amd64_host_with_the_handler_can_cross_build(tmp_path):
    handler = tmp_path / "qemu-aarch64"
    handler.write_text("enabled\n")
    host = rpi_image_gen.build_host_state(machine="x86_64", binfmt_path=handler)

    assert host.buildable


def test_an_architecture_that_can_neither_build_nor_emulate_is_named(tmp_path):
    host = rpi_image_gen.build_host_state(
        machine="riscv64", binfmt_path=tmp_path / "absent"
    )

    assert host.unsupported_architecture
    assert "riscv64" in host.unsupported_architecture


def test_missing_binaries_packages_and_binfmt_stay_separate(tmp_path, lock):
    report = probe(
        tmp_path and checkout(tmp_path, lock),
        lock,
        which=lambda binary: None,
        package_query=lambda package: False,
    )
    host = rpi_image_gen.build_host_state(
        report.dependencies, machine="x86_64", binfmt_path=tmp_path / "absent"
    )

    assert host.missing_binaries
    assert host.missing_binfmt
    assert set(host.missing_binaries) & set(host.missing_binfmt) == set()
