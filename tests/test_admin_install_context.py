# SPDX-License-Identifier: AGPL-3.0-or-later
from admin.install_context import (
    SOURCE_CANONICAL,
    SOURCE_DOCKER,
    SOURCE_ENV,
    SOURCE_LEGACY,
    SOURCE_MISSING,
    detect_install_context,
)
from ems import paths


def test_canonical_config_is_preferred(tmp_path):
    canonical = tmp_path / "config" / "config.json"
    canonical.parent.mkdir()
    canonical.touch()
    (tmp_path / "config.json").touch()

    context = detect_install_context(base_dir=tmp_path)
    assert context.config_path == canonical
    assert context.config_exists is True
    assert context.config_source == SOURCE_CANONICAL


def test_legacy_config_is_detected(tmp_path):
    legacy = tmp_path / "config.json"
    legacy.touch()

    context = detect_install_context(base_dir=tmp_path)
    assert context.config_path == legacy
    assert context.config_exists is True
    assert context.config_source == SOURCE_LEGACY


def test_missing_config_targets_canonical_docker_first_path(tmp_path):
    context = detect_install_context(base_dir=tmp_path)
    assert context.config_path == tmp_path / "config" / "config.json"
    assert context.config_exists is False
    assert context.config_source == SOURCE_MISSING


def test_config_environment_override_is_labeled_env(tmp_path, monkeypatch):
    env_path = tmp_path / "elsewhere" / "config.json"
    monkeypatch.setenv("EMS_CONFIG_FILE", str(env_path))

    context = detect_install_context(base_dir=tmp_path)
    assert context.config_path == env_path
    assert context.config_source == SOURCE_ENV


def test_install_dir_env_supplies_base_dir(tmp_path, monkeypatch):
    install = tmp_path / "srv" / "ems"
    (install / "config").mkdir(parents=True)
    (install / "config" / "config.json").touch()
    (install / "data").mkdir()
    (install / "docker-compose.yml").touch()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(install))

    context = detect_install_context()
    assert context.config_path == install / "config" / "config.json"
    assert context.config_exists is True
    assert context.config_source == SOURCE_CANONICAL
    assert context.data_dir == install / "data"
    assert context.data_dir_exists is True
    assert context.compose_path == install / "docker-compose.yml"
    assert context.compose_exists is True


def test_docker_fallback_does_not_resolve_to_app_when_install_dir_set(
    tmp_path, monkeypatch
):
    # In the Admin container ems.paths.BASE_DIR points at /app (only the resolver
    # is copied in). With EMS_INSTALL_DIR set, resolution must use the mounted
    # install root instead of the /app image fallback.
    monkeypatch.setattr(paths, "BASE_DIR", "/app")
    install = tmp_path / "srv" / "ems"
    (install / "config").mkdir(parents=True)
    (install / "config" / "config.json").touch()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(install))

    context = detect_install_context()
    assert context.config_path == install / "config" / "config.json"
    assert context.data_dir == install / "data"
    assert context.compose_path == install / "docker-compose.yml"
    assert "/app" not in str(context.config_path)


def test_explicit_base_dir_wins_over_install_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path / "ignored"))
    context = detect_install_context(base_dir=tmp_path)
    assert context.config_path == tmp_path / "config" / "config.json"


def test_data_dir_resolves_to_real_ems_data_dir(tmp_path):
    context = detect_install_context(base_dir=tmp_path)
    assert context.data_dir == tmp_path / "data"
    assert context.data_dir_exists is False

    (tmp_path / "data").mkdir()
    assert detect_install_context(base_dir=tmp_path).data_dir_exists is True


def test_compose_resolves_to_real_install_compose(tmp_path):
    context = detect_install_context(base_dir=tmp_path)
    assert context.compose_path == tmp_path / "docker-compose.yml"
    assert context.compose_exists is False

    (tmp_path / "docker-compose.yml").touch()
    assert detect_install_context(base_dir=tmp_path).compose_exists is True


def test_docker_image_template_is_labeled_docker(tmp_path, monkeypatch):
    docker_template = tmp_path / "app" / "config.template.json"
    docker_template.parent.mkdir()
    docker_template.touch()
    monkeypatch.setattr(paths, "DOCKER_TEMPLATE_PATH", docker_template)

    context = detect_install_context(base_dir=tmp_path / "install")
    assert context.template_path == docker_template
    assert context.template_source == SOURCE_DOCKER


def test_as_dict_stringifies_paths(tmp_path):
    payload = detect_install_context(base_dir=tmp_path).as_dict()
    assert payload["config_path"] == str(tmp_path / "config" / "config.json")
    assert payload["data_dir"] == str(tmp_path / "data")
    assert payload["compose_path"] == str(tmp_path / "docker-compose.yml")
    assert payload["install_root"] == str(tmp_path)
    assert isinstance(payload["config_exists"], bool)


def test_install_root_is_the_standard_layout_root(tmp_path):
    context = detect_install_context(base_dir=tmp_path)
    assert context.install_root == tmp_path
    assert context.config_path.parent.parent == context.install_root
    assert context.data_dir.parent == context.install_root


def test_legacy_root_only_layout_state_is_surfaced(tmp_path):
    (tmp_path / "config.json").write_text("{}")

    context = detect_install_context(base_dir=tmp_path)
    assert context.config_source == SOURCE_LEGACY
    assert context.config_layout_state == "legacy_root_only"
    assert context.as_dict()["config_layout_state"] == "legacy_root_only"


def test_standard_only_layout_state_is_surfaced(tmp_path):
    standard = tmp_path / "config" / "config.json"
    standard.parent.mkdir()
    standard.write_text("{}")

    context = detect_install_context(base_dir=tmp_path)
    assert context.config_layout_state == "standard_only"


def test_both_configs_present_prefers_standard_but_reports_difference(tmp_path):
    standard = tmp_path / "config" / "config.json"
    standard.parent.mkdir()
    standard.write_text('{"a": 1}')
    (tmp_path / "config.json").write_text('{"a": 2}')

    context = detect_install_context(base_dir=tmp_path)
    assert context.config_source == SOURCE_CANONICAL
    assert context.config_layout_state == "both_different"
