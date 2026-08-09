# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host configuration for the Appliance Manager.

``appliance.conf`` and ``allowed-images.conf`` live under ``/etc`` and are only
writable by root. They are the authority for which container images, containers
and host accounts the appliance may touch; the browser can never widen any of
these sets.
"""

import configparser
from dataclasses import dataclass, field
from pathlib import Path

SECTION = "appliance"

DEFAULT_WEB_ADDRESS = "0.0.0.0"
DEFAULT_WEB_PORT = 8080
DEFAULT_ADMIN_PORT = 8090
DEFAULT_ADMIN_CONTAINER = "ems-solarflow-admin"
DEFAULT_EMS_CONTAINER = "ems-solarflow"
DEFAULT_INFLUX_CONTAINER = "ems-influxdb"
DEFAULT_ADMIN_SERVICE = "ems-solarflow-admin"
DEFAULT_WEB_USER = "ems-appliance-web"
DEFAULT_SOCKET_GROUP = "ems-appliance"
DEFAULT_BACKUP_USER = "ems-backup"
DEFAULT_ADMIN_REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-admin"
DEFAULT_IMAGE_SOURCE = "https://github.com/basecubedev/ems-solarflow-api-control"
DEFAULT_SESSION_TIMEOUT = 1800
DEFAULT_SESSION_ABSOLUTE_MAX = 43200
DEFAULT_HEALTH_TIMEOUT = 120
DEFAULT_WIFI_REVERT_TIMEOUT = 90
DEFAULT_MIN_FREE_MB = 1024


class ConfigError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AllowedImages:
    repositories: tuple = (DEFAULT_ADMIN_REPOSITORY,)
    expected_source: str = DEFAULT_IMAGE_SOURCE
    legacy_exempt_tags: tuple = ()
    allow_prerelease: bool = False

    @property
    def admin_repository(self):
        return self.repositories[0]


@dataclass(frozen=True)
class ApplianceConfig:
    web_address: str = DEFAULT_WEB_ADDRESS
    web_port: int = DEFAULT_WEB_PORT
    web_user: str = DEFAULT_WEB_USER
    socket_group: str = DEFAULT_SOCKET_GROUP
    backup_user: str = DEFAULT_BACKUP_USER
    ssh_key_accounts: tuple = (DEFAULT_BACKUP_USER,)
    admin_container: str = DEFAULT_ADMIN_CONTAINER
    ems_container: str = DEFAULT_EMS_CONTAINER
    influx_container: str = DEFAULT_INFLUX_CONTAINER
    admin_service: str = DEFAULT_ADMIN_SERVICE
    admin_port: int = DEFAULT_ADMIN_PORT
    admin_health_path: str = "/api/health"
    session_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT
    session_absolute_max_seconds: int = DEFAULT_SESSION_ABSOLUTE_MAX
    health_timeout_seconds: int = DEFAULT_HEALTH_TIMEOUT
    wifi_revert_timeout_seconds: int = DEFAULT_WIFI_REVERT_TIMEOUT
    minimum_free_megabytes: int = DEFAULT_MIN_FREE_MB
    supported_architectures: tuple = ("arm64",)
    automatic_security_updates: bool = False
    release_index_url: str = ""
    images: AllowedImages = field(default_factory=AllowedImages)

    @property
    def managed_containers(self):
        return (self.admin_container, self.ems_container, self.influx_container)

    @property
    def admin_health_url(self):
        return f"http://127.0.0.1:{self.admin_port}{self.admin_health_path}"


def _read_ini(path):
    parser = configparser.ConfigParser()
    text = Path(path).read_text(encoding="utf-8")
    parser.read_string(text)
    if not parser.has_section(SECTION):
        raise ConfigError("config_section_missing", f"{path} has no [{SECTION}] section")
    return {key: value.strip() for key, value in parser.items(SECTION)}


def _as_int(values, key, default):
    raw = values.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("config_value_invalid", f"{key} must be an integer")


def _as_bool(values, key, default):
    raw = values.get(key)
    if raw in (None, ""):
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError("config_value_invalid", f"{key} must be a boolean")


def _as_tuple(values, key, default):
    raw = values.get(key)
    if raw in (None, ""):
        return tuple(default)
    items = [item.strip() for item in raw.replace("\n", ",").split(",")]
    return tuple(item for item in items if item)


def load_allowed_images(path):
    """Parse ``allowed-images.conf``: one repository per line plus directives."""

    repositories = []
    expected_source = DEFAULT_IMAGE_SOURCE
    legacy = []
    allow_prerelease = False

    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return AllowedImages()

    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if "=" in entry:
            key, _, value = entry.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key == "expected_source":
                expected_source = value
            elif key == "legacy_exempt_tags":
                legacy.extend(item.strip() for item in value.split(",") if item.strip())
            elif key == "allow_prerelease":
                allow_prerelease = value.lower() in ("1", "true", "yes", "on")
            else:
                raise ConfigError("config_value_invalid", f"unknown image directive {key!r}")
            continue
        repositories.append(entry)

    if not repositories:
        repositories = [DEFAULT_ADMIN_REPOSITORY]

    return AllowedImages(
        repositories=tuple(repositories),
        expected_source=expected_source,
        legacy_exempt_tags=tuple(legacy),
        allow_prerelease=allow_prerelease,
    )


def load_config(paths):
    """Load ``appliance.conf``; missing files fall back to packaged defaults."""

    images = load_allowed_images(paths.allowed_images_conf)
    try:
        values = _read_ini(paths.appliance_conf)
    except FileNotFoundError:
        return ApplianceConfig(images=images)

    return ApplianceConfig(
        web_address=values.get("web_address") or DEFAULT_WEB_ADDRESS,
        web_port=_as_int(values, "web_port", DEFAULT_WEB_PORT),
        web_user=values.get("web_user") or DEFAULT_WEB_USER,
        socket_group=values.get("socket_group") or DEFAULT_SOCKET_GROUP,
        backup_user=values.get("backup_user") or DEFAULT_BACKUP_USER,
        ssh_key_accounts=_as_tuple(
            values, "ssh_key_accounts", (values.get("backup_user") or DEFAULT_BACKUP_USER,)
        ),
        admin_container=values.get("admin_container") or DEFAULT_ADMIN_CONTAINER,
        ems_container=values.get("ems_container") or DEFAULT_EMS_CONTAINER,
        influx_container=values.get("influx_container") or DEFAULT_INFLUX_CONTAINER,
        admin_service=values.get("admin_service") or DEFAULT_ADMIN_SERVICE,
        admin_port=_as_int(values, "admin_port", DEFAULT_ADMIN_PORT),
        admin_health_path=values.get("admin_health_path") or "/api/health",
        session_timeout_seconds=_as_int(values, "session_timeout_seconds", DEFAULT_SESSION_TIMEOUT),
        session_absolute_max_seconds=_as_int(
            values, "session_absolute_max_seconds", DEFAULT_SESSION_ABSOLUTE_MAX
        ),
        health_timeout_seconds=_as_int(values, "health_timeout_seconds", DEFAULT_HEALTH_TIMEOUT),
        wifi_revert_timeout_seconds=_as_int(
            values, "wifi_revert_timeout_seconds", DEFAULT_WIFI_REVERT_TIMEOUT
        ),
        minimum_free_megabytes=_as_int(values, "minimum_free_megabytes", DEFAULT_MIN_FREE_MB),
        supported_architectures=_as_tuple(values, "supported_architectures", ("arm64",)),
        automatic_security_updates=_as_bool(values, "automatic_security_updates", False),
        release_index_url=values.get("release_index_url") or "",
        images=images,
    )
