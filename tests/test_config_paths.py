# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

from ems import paths


def test_explicit_config_path_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_CONFIG_FILE", str(tmp_path / "env.json"))

    assert paths.resolve_config_path("custom.json", base_dir=tmp_path) == Path(
        "custom.json"
    )


def test_config_environment_override_wins(tmp_path, monkeypatch):
    env_path = tmp_path / "env.json"
    monkeypatch.setenv("EMS_CONFIG_FILE", str(env_path))

    assert paths.resolve_config_path(base_dir=tmp_path) == env_path


def test_canonical_config_wins_over_legacy(tmp_path):
    canonical = tmp_path / "config" / "config.json"
    canonical.parent.mkdir()
    canonical.touch()
    (tmp_path / "config.json").touch()

    assert paths.resolve_config_path(base_dir=tmp_path) == canonical


def test_legacy_config_is_supported(tmp_path):
    legacy = tmp_path / "config.json"
    legacy.touch()

    assert paths.resolve_config_path(base_dir=tmp_path) == legacy


def test_missing_config_returns_canonical_target(tmp_path):
    assert paths.resolve_config_path(base_dir=tmp_path) == (
        tmp_path / "config" / "config.json"
    )


def test_explicit_template_path_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_TEMPLATE_FILE", str(tmp_path / "env-template.json"))

    assert paths.resolve_template_path(
        "custom-template.json", base_dir=tmp_path
    ) == Path("custom-template.json")


def test_template_environment_override_wins(tmp_path, monkeypatch):
    env_path = tmp_path / "env-template.json"
    monkeypatch.setenv("EMS_TEMPLATE_FILE", str(env_path))

    assert paths.resolve_template_path(base_dir=tmp_path) == env_path


def test_canonical_template_wins_over_legacy(tmp_path):
    canonical = tmp_path / "config" / "config.template.json"
    canonical.parent.mkdir()
    canonical.touch()
    (tmp_path / "config.template.json").touch()

    assert paths.resolve_template_path(base_dir=tmp_path) == canonical


def test_legacy_template_is_supported(tmp_path):
    legacy = tmp_path / "config.template.json"
    legacy.touch()

    assert paths.resolve_template_path(base_dir=tmp_path) == legacy


def test_docker_image_template_is_supported(tmp_path, monkeypatch):
    docker_template = tmp_path / "app" / "config.template.json"
    docker_template.parent.mkdir()
    docker_template.touch()
    monkeypatch.setattr(paths, "DOCKER_TEMPLATE_PATH", docker_template)

    assert paths.resolve_template_path(base_dir=tmp_path / "install") == (
        docker_template
    )


def test_missing_template_returns_canonical_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        paths,
        "DOCKER_TEMPLATE_PATH",
        tmp_path / "missing-docker-template.json",
    )

    assert paths.resolve_template_path(base_dir=tmp_path) == (
        tmp_path / "config" / "config.template.json"
    )


def test_data_and_compose_defaults_follow_base_dir(tmp_path):
    assert paths.resolve_data_dir(base_dir=tmp_path) == tmp_path / "data"
    assert paths.resolve_compose_path(base_dir=tmp_path) == (
        tmp_path / "docker-compose.yml"
    )
