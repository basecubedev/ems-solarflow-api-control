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

from pathlib import Path

import pytest

from appliance import image_inspect, config, config_seed, paths as paths_module

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


def test_the_package_ships_no_operator_file_into_a_shared_path():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")

    for name in image_inspect.OPERATOR_CONFIG:
        assert f'config/{name}" "$STAGE/usr/share/ems-appliance-manager/' in build
        assert f'config/{name}" "$STAGE/etc/ems-appliance-manager/' not in build
    conffiles = (PACKAGING / "debian" / "conffiles").read_text(encoding="utf-8").split()
    for name in image_inspect.OPERATOR_CONFIG:
        assert f"/etc/ems-appliance-manager/{name}" not in conffiles


def test_the_image_starts_the_seeding_unit():
    unit = "ems-appliance-config-seed.service"

    assert (PACKAGING / "systemd" / unit).is_file()
    layer = PACKAGING / "image" / "layer" / "ems-appliance.yaml"
    assert unit in layer.read_text(encoding="utf-8")
    assert unit in image_inspect.REQUIRED_UNITS.values()


# --- the file generated from the seeded one ----------------------------------


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
