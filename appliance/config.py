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

DEFAULT_TIMEZONE = "UTC"

DEFAULT_WEB_ADDRESS = "0.0.0.0"
# Not 8080: docker-compose.yml publishes the EMS dashboard there, and the
# appliance exists to host that deployment rather than displace it.
DEFAULT_WEB_PORT = 8088
DEFAULT_ADMIN_PORT = 8090
DEFAULT_ADMIN_CONTAINER = "ems-solarflow-admin"
# Kept equal to admin/container_names.py by tests/test_appliance_project_identity.py;
# the appliance runs outside every container and cannot import it.
DEFAULT_EMS_CONTAINER = "ems-solarflow-api-control"
DEFAULT_INFLUX_CONTAINER = "ems-influxdb"
DEFAULT_ADMIN_SERVICE = "ems-solarflow-admin"
DEFAULT_EMS_SERVICE = "ems"
DEFAULT_INFLUX_SERVICE = "influxdb"
# The Admin console's own unauthenticated status endpoint. It is the only fixed
# loopback route Admin answers without a session, so it is what a known-good
# check asks; a path Admin does not serve would 404 and fail every healthy
# appliance.
DEFAULT_ADMIN_HEALTH_PATH = "/api/admin/auth/status"
DEFAULT_WEB_USER = "ems-appliance-web"
DEFAULT_SOCKET_GROUP = "ems-appliance"
DEFAULT_BACKUP_USER = "ems-backup"
# Owner of the hosted deployment and the uid its containers run as. The
# compose files this project generates bind host paths in at the same path,
# so the identity has to be a real non-root account on the host.
DEFAULT_DEPLOYMENT_USER = "ems-deploy"
DEFAULT_ADMIN_REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-admin"
DEFAULT_IMAGE_SOURCE = "https://github.com/basecubedev/ems-solarflow-api-control"
DEFAULT_SESSION_TIMEOUT = 1800
DEFAULT_SESSION_ABSOLUTE_MAX = 43200
DEFAULT_HEALTH_TIMEOUT = 120
DEFAULT_WIFI_REVERT_TIMEOUT = 90
DEFAULT_MIN_FREE_MB = 1024
# The keyring every signed artifact this appliance installs is verified
# against. Root-owned, shipped with the package, and never reachable from
# a request.
DEFAULT_RELEASE_KEYRING = "/etc/ems-appliance-manager/release-keyring.gpg"
# The signer this appliance accepts, on top of the keyring. A keyring can hold
# more keys than a release is allowed to be signed with, so "gpg said good" is
# not the same answer as "signed by us". The release side has refused to
# finalize without a fingerprint since it was written; the device had no way to
# be told one, which left artifact_trust's fingerprint gate unarmed on every
# appliance while the security model described it as armed.
DEFAULT_RELEASE_FINGERPRINTS = ()
# Where signed Appliance Manager packages are fetched from. Empty means the
# manager can only be updated by hand, with dpkg, over SSH or at the console.
DEFAULT_MANAGER_INDEX_URL = ""


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
    timezone: str = DEFAULT_TIMEZONE
    web_address: str = DEFAULT_WEB_ADDRESS
    web_port: int = DEFAULT_WEB_PORT
    web_user: str = DEFAULT_WEB_USER
    socket_group: str = DEFAULT_SOCKET_GROUP
    backup_user: str = DEFAULT_BACKUP_USER
    deployment_user: str = DEFAULT_DEPLOYMENT_USER
    ssh_key_accounts: tuple = (DEFAULT_BACKUP_USER,)
    admin_container: str = DEFAULT_ADMIN_CONTAINER
    ems_container: str = DEFAULT_EMS_CONTAINER
    influx_container: str = DEFAULT_INFLUX_CONTAINER
    admin_service: str = DEFAULT_ADMIN_SERVICE
    ems_service: str = DEFAULT_EMS_SERVICE
    influx_service: str = DEFAULT_INFLUX_SERVICE
    admin_port: int = DEFAULT_ADMIN_PORT
    admin_health_path: str = DEFAULT_ADMIN_HEALTH_PATH
    session_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT
    session_absolute_max_seconds: int = DEFAULT_SESSION_ABSOLUTE_MAX
    health_timeout_seconds: int = DEFAULT_HEALTH_TIMEOUT
    wifi_revert_timeout_seconds: int = DEFAULT_WIFI_REVERT_TIMEOUT
    minimum_free_megabytes: int = DEFAULT_MIN_FREE_MB
    supported_architectures: tuple = ("arm64",)
    automatic_security_updates: bool = False
    release_index_url: str = ""
    release_keyring: str = DEFAULT_RELEASE_KEYRING
    release_fingerprints: tuple = DEFAULT_RELEASE_FINGERPRINTS
    manager_index_url: str = DEFAULT_MANAGER_INDEX_URL
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


def read_host_paths(path):
    """The two movable host roots, read without building the whole config.

    ``resolve_paths`` needs these before a config object exists, so they are
    parsed on their own here instead of through ``load_config``.
    """

    try:
        values = _read_ini(path)
    except (OSError, ConfigError, configparser.Error):
        return {}
    return {
        key: values[key]
        for key in ("install_root", "export_root")
        if values.get(key)
    }


def load_allowed_images(path):
    """Parse ``allowed-images.conf``: one repository per line plus directives."""

    repositories = []
    expected_source = DEFAULT_IMAGE_SOURCE
    legacy = []
    # From the dataclass rather than a literal: a file that omits the directive
    # and a file that is not there at all must resolve to the same policy, and
    # two literals is how they come to disagree.
    allow_prerelease = AllowedImages.allow_prerelease

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
                # Through the same parser every other boolean uses: reading a
                # typo as "false" would silently re-disable release candidates
                # on a host whose operator wrote that they are allowed.
                allow_prerelease = _as_bool({key: value}, key, allow_prerelease)
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


def _package_owned_account(values, key, expected, reason):
    """A package-created account name is not configurable.

    The package creates exactly one account per role and removes it again.
    Accepting a different name here would produce a configuration the account
    lifecycle cannot honour, which is worse than refusing it.
    """

    configured = (values.get(key) or "").strip()
    if configured and configured != expected:
        raise ConfigError(
            f"{key}_unsupported",
            f"{key} must be {expected}; {reason}",
        )
    return expected


def _backup_user(values):
    return _package_owned_account(
        values,
        "backup_user",
        DEFAULT_BACKUP_USER,
        "the backup account is created and removed by this package and no other "
        "account is managed",
    )


def _deployment_user(values):
    return _package_owned_account(
        values,
        "deployment_user",
        DEFAULT_DEPLOYMENT_USER,
        "the deployment owner is created and removed by this package and no other "
        "account is managed",
    )


def _ssh_key_accounts(values):
    """Whose authorized_keys a request may ever reach.

    The backup account has no shell and is confined to a read-only SFTP view
    of the export root. Any other name here would let an authenticated browser
    deploy a key on an account that can open a session, which is the boundary
    the unprivileged web process and the allowlisted agent exist to hold.
    """

    configured = _as_tuple(values, "ssh_key_accounts", (DEFAULT_BACKUP_USER,))
    unsupported = [name for name in configured if name != DEFAULT_BACKUP_USER]
    if unsupported:
        raise ConfigError(
            "ssh_key_accounts_unsupported",
            f"ssh_key_accounts must be {DEFAULT_BACKUP_USER}; "
            f"{', '.join(unsupported)} is not an account this package manages keys for",
        )
    return (DEFAULT_BACKUP_USER,)


def _read_timezone(paths):
    """The operator's choice, which outranks the packaged default."""

    try:
        return paths.timezone_file.read_text(encoding="utf-8").strip()
    except (OSError, AttributeError):
        return ""


def load_config(paths):
    """Load ``appliance.conf``; missing files fall back to packaged defaults."""

    images = load_allowed_images(paths.allowed_images_conf)
    chosen_zone = _read_timezone(paths)
    try:
        values = _read_ini(paths.appliance_conf)
    except FileNotFoundError:
        return ApplianceConfig(images=images, timezone=chosen_zone or DEFAULT_TIMEZONE)

    _backup_user(values)
    _deployment_user(values)
    return ApplianceConfig(
        timezone=chosen_zone or values.get("timezone") or DEFAULT_TIMEZONE,
        web_address=values.get("web_address") or DEFAULT_WEB_ADDRESS,
        web_port=_as_int(values, "web_port", DEFAULT_WEB_PORT),
        web_user=values.get("web_user") or DEFAULT_WEB_USER,
        socket_group=values.get("socket_group") or DEFAULT_SOCKET_GROUP,
        backup_user=DEFAULT_BACKUP_USER,
        deployment_user=DEFAULT_DEPLOYMENT_USER,
        ssh_key_accounts=_ssh_key_accounts(values),
        admin_container=values.get("admin_container") or DEFAULT_ADMIN_CONTAINER,
        ems_container=values.get("ems_container") or DEFAULT_EMS_CONTAINER,
        influx_container=values.get("influx_container") or DEFAULT_INFLUX_CONTAINER,
        admin_service=values.get("admin_service") or DEFAULT_ADMIN_SERVICE,
        ems_service=values.get("ems_service") or DEFAULT_EMS_SERVICE,
        influx_service=values.get("influx_service") or DEFAULT_INFLUX_SERVICE,
        admin_port=_as_int(values, "admin_port", DEFAULT_ADMIN_PORT),
        admin_health_path=values.get("admin_health_path") or DEFAULT_ADMIN_HEALTH_PATH,
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
        release_keyring=values.get("release_keyring") or DEFAULT_RELEASE_KEYRING,
        release_fingerprints=tuple(
            fingerprint.replace(" ", "").upper()
            for fingerprint in _as_tuple(values, "release_fingerprints", ())
        ),
        manager_index_url=(values.get("manager_index_url") or "").strip(),
        images=images,
    )
