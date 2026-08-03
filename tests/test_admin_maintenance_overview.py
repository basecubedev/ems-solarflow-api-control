# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only Maintenance overview tests (no hardware/network/Docker required)."""

import json

import pytest

from admin.container_names import DEFAULT_EMS_CONTAINER, DEFAULT_INFLUX_CONTAINER
from admin.maintenance import (
    PARTIAL_INSTALL_WARNING,
    run_maintenance_overview,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


COMPOSE_TEXT = """
services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1
    container_name: ems-solarflow-api-control
  influxdb:
    image: influxdb:2.7
    container_name: ems-influxdb
"""


class FakeDocker:
    """Records every Docker call so tests can assert no mutation happens."""

    def __init__(self, ready=True, containers=None, raise_on_probe=False):
        self._ready = ready
        self._containers = containers or {}
        self._raise_on_probe = raise_on_probe
        self.calls = []

    def probe(self):
        self.calls.append(("probe",))
        if self._raise_on_probe:
            raise RuntimeError("docker exploded")
        if not self._ready:
            return {
                "state": "socket_missing",
                "code": "docker_socket_not_mounted",
                "message": "The Docker socket is not mounted.",
                "server_version": None,
            }
        return {
            "state": "ready",
            "code": None,
            "message": "Docker is available.",
            "server_version": "24.0.7",
        }

    def inspect_container(self, name):
        self.calls.append(("inspect_container", name))
        return self._containers.get(name)


def _standard_install(base_dir):
    (base_dir / "config").mkdir()
    (base_dir / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"port": 8080}}), encoding="utf-8"
    )
    (base_dir / "data").mkdir()
    (base_dir / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")


def test_standard_install_reports_all_paths_present(tmp_path):
    _standard_install(tmp_path)
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["install_state"]["state"] == "standard_install"
    assert overview["install_state"]["label"] == "Standard installation"
    assert overview["paths"]["config"]["exists"] is True
    assert overview["paths"]["data"]["exists"] is True
    assert overview["paths"]["compose"]["exists"] is True
    assert overview["warnings"] == []


def test_missing_config_is_not_a_standard_install(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["paths"]["config"]["exists"] is False
    assert overview["install_state"]["state"] == "compose_only"
    assert PARTIAL_INSTALL_WARNING in overview["warnings"]


def test_missing_data_directory_is_reported(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["paths"]["data"]["exists"] is False


def test_missing_compose_file_is_reported_as_partial(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data").mkdir()
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["paths"]["compose"]["exists"] is False
    assert overview["install_state"]["state"] == "standard_config_only"
    assert PARTIAL_INSTALL_WARNING in overview["warnings"]


def test_docker_unavailable_still_returns_paths_and_state(tmp_path):
    _standard_install(tmp_path)
    overview = run_maintenance_overview(
        base_dir=str(tmp_path), docker=FakeDocker(ready=False)
    )

    assert overview["docker"]["available"] is False
    assert overview["docker"]["error"]
    assert overview["paths"]["config"]["exists"] is True
    assert overview["install_state"]["state"] == "standard_install"
    # Containers degrade to unknown, never an error.
    assert overview["containers"]["ems"]["status"] == "unknown"
    assert overview["containers"]["influxdb"]["status"] == "unknown"


def test_docker_probe_exception_degrades_gracefully(tmp_path):
    _standard_install(tmp_path)
    overview = run_maintenance_overview(
        base_dir=str(tmp_path), docker=FakeDocker(raise_on_probe=True)
    )

    assert overview["docker"]["available"] is False
    assert overview["containers"]["ems"]["status"] == "unknown"


def test_running_ems_container_is_reported(tmp_path):
    _standard_install(tmp_path)
    docker = FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
                "status": "running",
            },
            "ems-influxdb": {
                "container_name": "ems-influxdb",
                "image": "influxdb:2.7",
                "status": "running",
            },
        }
    )
    overview = run_maintenance_overview(
        base_dir=str(tmp_path),
        docker=docker,
        admin_image="ghcr.io/basecubedev/ems-solarflow-admin:v0.9.0",
    )

    ems = overview["containers"]["ems"]
    assert ems["found"] is True
    assert ems["running"] is True
    assert ems["name"] == "ems-solarflow-api-control"
    assert ems["image"] == "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1"
    assert ems["tag"] == "v0.6.1"
    assert overview["containers"]["influxdb"]["running"] is True
    assert overview["containers"]["influxdb"]["tag"] == "2.7"
    assert overview["components"] == {
        "admin": {
            "image": "ghcr.io/basecubedev/ems-solarflow-admin:v0.9.0",
            "tag": "v0.9.0",
        },
        "ems": {
            "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
            "tag": "v0.6.1",
        },
    }


def test_digest_pinned_running_ems_reports_readable_version_from_labels(tmp_path):
    # After a digest-pinned Guided Upgrade the running EMS image ref carries no
    # tag; the overview recovers a readable release from the image's OCI labels
    # rather than showing a bare digest.
    _standard_install(tmp_path)
    digest_ref = "ghcr.io/basecubedev/ems-solarflow-api-control@sha256:" + "a" * 64

    class LabelDocker(FakeDocker):
        def inspect_image(self, ref):
            self.calls.append(("inspect_image", ref))
            if ref == digest_ref:
                return {
                    "image_ref": ref,
                    "digest": "sha256:" + "a" * 64,
                    "labels": {
                        "de.basecubedev.ems.release_tag": "v0.8.0",
                        "org.opencontainers.image.version": "v0.8.0",
                    },
                }
            return None

    docker = LabelDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": digest_ref,
                "status": "running",
            },
        }
    )
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=docker)

    ems = overview["containers"]["ems"]
    assert ems["image"] == digest_ref
    # The digest is never shown as the version; the readable release tag is.
    assert ems["tag"] == "v0.8.0"
    assert overview["components"]["ems"]["tag"] == "v0.8.0"


def test_overview_and_release_manager_agree_on_installed_release(tmp_path):
    # Maintenance Overview and the release catalogue must report the SAME
    # installed release for a digest-pinned running EMS container.
    from admin.releases import ReleaseManager

    digest = "sha256:" + "a" * 64
    digest_ref = "ghcr.io/basecubedev/ems-solarflow-api-control@" + digest
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"port": 8080}}), encoding="utf-8"
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  ems:\n"
        f"    image: {digest_ref}\n"
        "    container_name: ems-solarflow-api-control\n",
        encoding="utf-8",
    )
    labels = {
        "de.basecubedev.ems.release_tag": "v0.8.0",
        "org.opencontainers.image.version": "v0.8.0",
        "de.basecubedev.ems.build_id": "v0.8.0-f7265fc",
        "org.opencontainers.image.revision": "f" * 40,
    }

    class LabelDocker(FakeDocker):
        def inspect_image(self, ref):
            if ref == digest_ref:
                return {"image_ref": ref, "digest": digest, "labels": labels}
            return None

    docker = LabelDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": digest_ref,
                "status": "running",
            },
        }
    )
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=docker)
    manager = ReleaseManager(
        data_dir=tmp_path / "admin-data", project_dir=tmp_path, docker=docker
    )

    assert overview["components"]["ems"]["tag"] == "v0.8.0"
    assert manager.detect_active_release() == "v0.8.0"


def test_image_tag_parsing_ignores_registry_port():
    from admin.maintenance import _image_tag

    assert _image_tag("ghcr.io/org/ems:v0.6.1") == "v0.6.1"
    assert _image_tag("influxdb:2.7") == "2.7"
    assert _image_tag("localhost:5000/ems") is None
    assert _image_tag("ems@sha256:abc") is None
    assert _image_tag(None) is None


def test_stopped_ems_container_is_reported(tmp_path):
    _standard_install(tmp_path)
    docker = FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
                "status": "exited",
            }
        }
    )
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=docker)

    ems = overview["containers"]["ems"]
    assert ems["found"] is True
    assert ems["running"] is False
    assert ems["status"] == "exited"
    # InfluxDB was not inspected as present -> missing, not an error.
    assert overview["containers"]["influxdb"]["found"] is False
    assert overview["containers"]["influxdb"]["status"] == "missing"


def test_overview_never_issues_mutating_docker_commands(tmp_path):
    _standard_install(tmp_path)
    docker = FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": "influxdb:2.7",
                "status": "running",
            }
        }
    )
    run_maintenance_overview(base_dir=str(tmp_path), docker=docker)

    call_names = {call[0] for call in docker.calls}
    assert call_names <= {"probe", "inspect_container"}
    for mutating in ("pull", "remove_container", "stop_container", "up"):
        assert mutating not in call_names


def test_dashboard_url_uses_configured_port_and_scheme(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"dashboard": {"port": 9443, "ssl_enabled": True}}),
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["links"]["dashboard_url"] == "https://localhost:9443"


def test_dashboard_url_none_when_config_missing(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["links"]["dashboard_url"] is None


def test_missing_compose_falls_back_to_default_container_names(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data").mkdir()
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=FakeDocker())

    assert overview["containers"]["ems"]["name"] == DEFAULT_EMS_CONTAINER
    assert overview["containers"]["influxdb"]["name"] == DEFAULT_INFLUX_CONTAINER


def test_running_but_unidentified_ems_reports_unknown_release_with_warning(tmp_path):
    from admin.maintenance import EMS_RUNNING_IDENTITY_UNKNOWN_WARNING

    _standard_install(tmp_path)
    digest_ref = "ghcr.io/basecubedev/ems-solarflow-api-control@sha256:" + "a" * 64
    docker = FakeDocker(
        containers={
            "ems-solarflow-api-control": {
                "container_name": "ems-solarflow-api-control",
                "image": digest_ref,
                "status": "running",
            }
        }
    )
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=docker)

    ems = overview["containers"]["ems"]
    assert ems["running"] is True
    assert ems["tag"] is None
    assert overview["components"]["ems"]["tag"] is None
    assert EMS_RUNNING_IDENTITY_UNKNOWN_WARNING in overview["warnings"]


def test_configured_container_name_is_honored_in_overview(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_CONTAINER_NAME", "custom-ems")
    _standard_install(tmp_path)
    docker = FakeDocker(
        containers={
            "custom-ems": {
                "container_name": "custom-ems",
                "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
                "status": "running",
            }
        }
    )
    overview = run_maintenance_overview(base_dir=str(tmp_path), docker=docker)

    ems = overview["containers"]["ems"]
    assert ems["found"] is True
    assert ems["running"] is True
    assert ems["name"] == "custom-ems"
    assert ems["tag"] == "v0.6.1"
