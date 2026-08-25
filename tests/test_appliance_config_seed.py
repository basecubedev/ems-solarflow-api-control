# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator configuration has to survive the thing that used to destroy it.

``/etc/ems-appliance-manager`` is a declared shared path, so upstream's
``rpi-persistent-shared-init`` pushes the booting slot's own copy of it into
``/persistent/shared`` before the binds activate — on every boot, with
``rsync -av --checksum`` and no ``--delete``. ``--checksum`` transfers exactly
the files that differ, and an operator's edit is the difference. A packaged copy
of ``appliance.conf`` under ``/etc`` therefore reverted every setting at the next
reboot, silently, and no fixture could see it because every fixture writes one
copy and never boots twice.

The tests below boot twice.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import ab_image, config, config_seed, paths as paths_module

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


class FakePaths:
    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)

    @property
    def appliance_conf(self):
        return self.config_dir / "appliance.conf"

    @property
    def allowed_images_conf(self):
        return self.config_dir / "allowed-images.conf"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    datadir = tmp_path / "usr-share"
    datadir.mkdir()
    (datadir / "appliance.conf").write_text("timezone = UTC\nweb_port = 8088\n", encoding="utf-8")
    (datadir / "allowed-images.conf").write_text("ghcr.io/basecubedev/x\n", encoding="utf-8")
    monkeypatch.setenv(paths_module.ENV_PACKAGE_DATADIR, str(datadir))
    return FakePaths(tmp_path / "etc")


def seed(paths):
    return {item.name: item for item in config_seed.seed_config(paths)}


def test_a_fresh_appliance_is_given_every_operator_file(staged):
    results = seed(staged)

    assert {name: item.outcome for name, item in results.items()} == {
        "appliance.conf": config_seed.SEEDED,
        "allowed-images.conf": config_seed.SEEDED,
    }
    assert staged.appliance_conf.read_text(encoding="utf-8") == "timezone = UTC\nweb_port = 8088\n"
    assert staged.appliance_conf.stat().st_mode & 0o777 == config_seed.FILE_MODE


def test_an_edited_file_is_never_read_compared_or_rewritten(staged):
    seed(staged)
    staged.appliance_conf.write_text("timezone = Europe/Berlin\n", encoding="utf-8")

    for _ in range(3):
        results = seed(staged)
        assert results["appliance.conf"].outcome == config_seed.PRESENT

    assert staged.appliance_conf.read_text(encoding="utf-8") == "timezone = Europe/Berlin\n"


def test_a_missing_template_is_reported_rather_than_invented(staged, monkeypatch):
    monkeypatch.setenv(paths_module.ENV_PACKAGE_DATADIR, str(staged.config_dir / "nowhere"))

    results = seed(staged)

    assert results["appliance.conf"].outcome == config_seed.TEMPLATE_MISSING
    assert not staged.appliance_conf.exists()
    assert not any(item.ok for item in results.values())


def test_an_appliance_with_no_config_at_all_still_loads_its_defaults(staged):
    """The seeding is a convenience, never a precondition — so it can fail safe."""

    loaded = config.load_config(staged)

    assert loaded.web_port == config.DEFAULT_WEB_PORT
    assert loaded.images.repositories == (config.DEFAULT_ADMIN_REPOSITORY,)


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_an_operator_setting_survives_the_boot_that_used_to_revert_it(tmp_path, monkeypatch):
    """The regression, run against the command upstream actually executes."""

    datadir = tmp_path / "usr-share"
    datadir.mkdir()
    (datadir / "appliance.conf").write_text("timezone = UTC\n", encoding="utf-8")
    (datadir / "allowed-images.conf").write_text("ghcr.io/basecubedev/x\n", encoding="utf-8")
    monkeypatch.setenv(paths_module.ENV_PACKAGE_DATADIR, str(datadir))

    slot_root = tmp_path / "slot" / "etc" / "ems-appliance-manager"
    shared = tmp_path / "persistent" / "shared" / "etc" / "ems-appliance-manager"
    slot_root.mkdir(parents=True)
    shared.mkdir(parents=True)
    # What the image still ships into the shared path, and must keep shipping:
    # both belong to the running slot and have to track it across an update.
    (slot_root / "ab-layout.json").write_text("{}\n", encoding="utf-8")
    (slot_root / "os-release-keyring.gpg").write_bytes(b"\x99keyring")

    def boot():
        subprocess.run(
            ["rsync", "-av", "--checksum", f"{slot_root}/", f"{shared}/"],
            check=True,
            capture_output=True,
        )
        config_seed.seed_config(FakePaths(shared))

    boot()
    assert (shared / "appliance.conf").read_text(encoding="utf-8") == "timezone = UTC\n"

    (shared / "appliance.conf").write_text("timezone = Europe/Berlin\n", encoding="utf-8")
    boot()

    assert (shared / "appliance.conf").read_text(encoding="utf-8") == "timezone = Europe/Berlin\n"
    assert (shared / "ab-layout.json").exists()
    assert (shared / "os-release-keyring.gpg").read_bytes() == b"\x99keyring"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_the_old_layout_is_what_destroyed_the_setting(tmp_path):
    """Why the placement is the fix, and not the seeding.

    Seeding cannot rescue a file the package also ships into the shared path:
    the boot-time push happens first and overwrites the edit, so by the time
    the seeder looks, the file exists and is correctly left alone. Putting a
    packaged copy back under /etc reintroduces the defect in full.
    """

    slot_root = tmp_path / "slot"
    shared = tmp_path / "shared"
    slot_root.mkdir()
    shared.mkdir()
    (slot_root / "appliance.conf").write_text("timezone = UTC\n", encoding="utf-8")
    (shared / "appliance.conf").write_text("timezone = Europe/Berlin\n", encoding="utf-8")

    subprocess.run(
        ["rsync", "-av", "--checksum", f"{slot_root}/", f"{shared}/"],
        check=True,
        capture_output=True,
    )

    assert (shared / "appliance.conf").read_text(encoding="utf-8") == "timezone = UTC\n"


def test_the_package_ships_no_operator_file_into_a_shared_path():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")

    for name in ab_image.OPERATOR_CONFIG:
        assert f'config/{name}" "$STAGE/usr/share/ems-appliance-manager/' in build
        assert f'config/{name}" "$STAGE/etc/ems-appliance-manager/' not in build
    conffiles = (PACKAGING / "debian" / "conffiles").read_text(encoding="utf-8").split()
    for name in ab_image.OPERATOR_CONFIG:
        assert f"/etc/ems-appliance-manager/{name}" not in conffiles


def test_every_image_variant_starts_the_seeding_unit():
    unit = "ems-appliance-config-seed.service"

    assert (PACKAGING / "systemd" / unit).is_file()
    for layer in ("ems-appliance.yaml", "ems-appliance-single.yaml"):
        assert unit in (PACKAGING / "image" / "layer" / layer).read_text(encoding="utf-8")
    for units in (ab_image.ROOT_UNITS, ab_image.SINGLE_SLOT_UNITS):
        assert unit in units.values()


def test_the_seeding_unit_cannot_run_before_the_shared_binds_are_proven():
    """Ordering alone would seed into the read-only root the switch discards."""

    unit = (PACKAGING / "systemd" / "ems-appliance-config-seed.service").read_text(encoding="utf-8")

    assert "Requires=ems-appliance-persistence.service" in unit
    assert "After=local-fs.target ems-appliance-persistence.service" in unit
    assert "RequiresMountsFor=/etc/ems-appliance-manager" in unit
    assert "Before=ems-appliance-agent.service ems-appliance-web.service" in unit


# --- the file generated from the seeded one ----------------------------------


def test_the_derived_environment_file_is_not_baked_into_an_ab_slot():
    """It is generated from appliance.conf and stored beside it.

    So the same boot-time push applies: a copy in the slot root would revert a
    changed install or export root at every reboot, exactly as the packaged
    conffiles did. The single-slot root has no shared bind and keeps its own.
    """

    layer = (PACKAGING / "image" / "layer" / "ems-appliance.yaml").read_text(encoding="utf-8")

    assert 'rm -f "$1/etc/ems-appliance-manager/host-paths.env"' in layer
    assert ab_image.DERIVED_ON_SHARED == ("host-paths.env",)


def test_the_seeding_unit_regenerates_it_only_when_it_stopped_agreeing():
    """Cheap enough for every boot, which is what lets it run on every boot."""

    unit = (PACKAGING / "systemd" / "ems-appliance-config-seed.service").read_text(encoding="utf-8")

    assert "host-config --apply --if-drifted" in unit
    assert unit.index("seed-config") < unit.index("host-config"), "the source is seeded first"


def test_a_missing_environment_file_counts_as_drifted(staged, monkeypatch):
    from appliance import cli
    from appliance.host_config import host_paths_file

    monkeypatch.setenv("EMS_APPLIANCE_CONFIG_DIR", str(staged.config_dir))
    paths = cli.resolve_paths()
    loaded = config.load_config(paths)

    assert not host_paths_file(paths).exists()
    assert cli._host_paths_drifted(paths, loaded)


def test_an_environment_file_that_agrees_is_left_alone(staged, monkeypatch):
    from appliance import cli
    from appliance.host_config import environment_values, host_paths_file

    monkeypatch.setenv("EMS_APPLIANCE_CONFIG_DIR", str(staged.config_dir))
    paths = cli.resolve_paths()
    loaded = config.load_config(paths)
    target = host_paths_file(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(f"{key}={value}\n" for key, value in environment_values(paths, loaded).items()),
        encoding="utf-8",
    )

    assert not cli._host_paths_drifted(paths, loaded)


def test_a_changed_root_is_seen_as_drift(staged, monkeypatch):
    from appliance import cli
    from appliance.host_config import environment_values, host_paths_file

    monkeypatch.setenv("EMS_APPLIANCE_CONFIG_DIR", str(staged.config_dir))
    paths = cli.resolve_paths()
    loaded = config.load_config(paths)
    target = host_paths_file(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    values = dict(environment_values(paths, loaded))
    key = sorted(values)[0]
    values[key] = "/somewhere/else"
    target.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
    )

    assert cli._host_paths_drifted(paths, loaded)
