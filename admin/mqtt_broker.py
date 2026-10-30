# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in MQTT broker: its settings, its files and its two containers.

The Admin owns the broker. ``config/mqtt/broker.json`` holds the switch, the
port, the anonymous switch and the accounts; their passwords live encrypted in
the credential store. Everything Mosquitto and the mapping container read is
rendered from those into ``<install>/mqtt/`` and started as the compose project
``ems-mqtt``, beside the EMS and independent of it.

What the regulation reads is protected in every mode: the mapping's outputs
under ``ems-solarflow/``, the Zendure trees and the topic of an MQTT grid meter
on this broker. Anonymous clients and accounts without the matching option can
neither read nor write there; Mosquitto checks ``deny`` before any grant.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field, replace

from admin.deployment import DockerCompose, DockerError
from ems.config import grid_meter_mqtt_settings
from ems.file_io import atomic_write_bytes
from ems.zendure_mqtt.builtin_broker import (
    BUILTIN_PROFILE_REF,
    broker_settings_path,
    builtin_profile_configured,
    builtin_profile_users,
    mapping_status_path,
    mappings_path,
)
from mqtt_mapping.config import load_mappings_file

SETTINGS_SCHEMA_VERSION = 1
MOSQUITTO_IMAGE = "eclipse-mosquitto:2.1.2-alpine"
COMPOSE_PROJECT = "ems-mqtt"
BROKER_SERVICE = "ems-mqtt-broker"
MAPPING_SERVICE = "ems-mqtt-mapping"
WORKSPACE_DIRNAME = "mqtt"
BROKER_FILES_DIRNAME = "broker"
MAPPING_FILES_DIRNAME = "mapping"
CONTAINER_PORT = 1883
DEFAULT_PORT = 1883
DEFAULT_ACCOUNT = "devices"
INTERNAL_ACCOUNTS = ("ems", "ems-mapping", "ems-admin")
EMS_ACCOUNT, MAPPING_ACCOUNT, ADMIN_ACCOUNT = INTERNAL_ACCOUNTS
OUTPUT_TREE = "ems-solarflow/#"
ZENDURE_TREES = ("iot/#", "Zendure/#", "/#")
EMS_WRITE_TOPIC = "iot/+/+/properties/write"
RESERVED_PREFIX_ROOTS = ("ems-solarflow", "iot", "Zendure")
PASSWORD_ITERATIONS = 101
PASSWORD_SALT_BYTES = 12
START_TIMEOUT_SECONDS = 20.0

ISSUE_CODES = (
    "mqtt_broker_settings_invalid",
    "mqtt_broker_port_invalid",
    "mqtt_broker_port_in_use",
    "mqtt_broker_no_access",
    "mqtt_broker_account_name_invalid",
    "mqtt_broker_account_duplicate",
    "mqtt_broker_account_reserved",
    "mqtt_broker_account_unknown",
    "mqtt_broker_account_password_missing",
    "mqtt_broker_account_password_invalid",
    "mqtt_broker_topic_prefix_invalid",
    "mqtt_broker_grid_meter_topic_unprotectable",
    "mqtt_broker_profile_name_reserved",
)

_ACCOUNT_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_PREFIX = re.compile(r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+){0,7}")
_ACL_TOPIC = re.compile(r"[^\s+#\x00-\x1f\x7f]+")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


@dataclass(frozen=True)
class Account:
    name: str
    topic_prefix: str | None = None
    bridge: bool = False
    zendure: bool = False

    @property
    def credentials_ref(self) -> str:
        return credentials_ref_for(self.name)


@dataclass(frozen=True)
class BrokerSettings:
    enabled: bool = False
    port: int = DEFAULT_PORT
    anonymous: bool = False
    accounts: tuple[Account, ...] = ()
    grid_meter_account: str | None = None

    def account(self, name) -> Account | None:
        return next((item for item in self.accounts if item.name == name), None)


def credentials_ref_for(name) -> str:
    return f"mqtt-broker-{str(name).lower()}"


def _issue(code, message, path=""):
    return {"code": code, "path": path, "message": message}


def default_settings() -> BrokerSettings:
    return BrokerSettings(accounts=(Account(DEFAULT_ACCOUNT),))


def settings_document(settings: BrokerSettings) -> dict:
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "enabled": settings.enabled,
        "port": settings.port,
        "anonymous": settings.anonymous,
        "accounts": [
            {
                "name": account.name,
                "credentials_ref": account.credentials_ref,
                "topic_prefix": account.topic_prefix,
                "bridge": account.bridge,
                "zendure": account.zendure,
            }
            for account in settings.accounts
        ],
        "grid_meter_account": settings.grid_meter_account,
    }


def valid_account_name(name) -> bool:
    return isinstance(name, str) and _ACCOUNT_NAME.fullmatch(name) is not None


def topic_prefix_problem(prefix) -> str | None:
    if prefix is None:
        return None
    if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None:
        return "A prefix is one to eight topic levels of letters, digits and _ . : -."
    if prefix.split("/", 1)[0] in RESERVED_PREFIX_ROOTS:
        return "This prefix belongs to a protected tree."
    return None


def parse_settings(document):
    """``(BrokerSettings, [])`` for a valid document, or ``(None, issues)``."""

    if not isinstance(document, dict) or document.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        return None, [_issue("mqtt_broker_settings_invalid", "broker.json has an unknown form.")]
    issues = []
    enabled = document.get("enabled", False)
    anonymous = document.get("anonymous", False)
    if not isinstance(enabled, bool) or not isinstance(anonymous, bool):
        issues.append(_issue("mqtt_broker_settings_invalid", "enabled and anonymous are true or false."))
    port = document.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        issues.append(_issue("mqtt_broker_port_invalid", "The port is a number from 1 to 65535.", "port"))
    raw_accounts = document.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raw_accounts = []
        issues.append(_issue("mqtt_broker_settings_invalid", "accounts must be a list.", "accounts"))
    accounts = []
    seen = set()
    for index, raw in enumerate(raw_accounts):
        path = f"accounts[{index}]"
        if not isinstance(raw, dict):
            issues.append(_issue("mqtt_broker_settings_invalid", "An account is a JSON object.", path))
            continue
        account, problems = _parse_account(raw, path, seen)
        issues.extend(problems)
        if account is not None:
            seen.add(account.name.lower())
            accounts.append(account)
    grid_meter_account = document.get("grid_meter_account")
    if grid_meter_account is not None and grid_meter_account not in {item.name for item in accounts}:
        issues.append(
            _issue("mqtt_broker_account_unknown", "The grid meter account does not exist.", "grid_meter_account")
        )
    if not issues and not anonymous and not accounts:
        issues.append(_issue("mqtt_broker_no_access", "Without anonymous access the broker needs an account."))
    if issues:
        return None, issues
    return BrokerSettings(enabled, port, anonymous, tuple(accounts), grid_meter_account), []


def _parse_account(raw, path, seen):
    name = raw.get("name")
    if not valid_account_name(name):
        return None, [_issue("mqtt_broker_account_name_invalid", "A name is 1 to 32 of A-Z a-z 0-9 _ -.", f"{path}.name")]
    if name.lower() in INTERNAL_ACCOUNTS:
        return None, [_issue("mqtt_broker_account_reserved", f"{name} is used by the system.", f"{path}.name")]
    if name.lower() in seen:
        return None, [_issue("mqtt_broker_account_duplicate", f"{name} exists twice.", f"{path}.name")]
    prefix = raw.get("topic_prefix")
    problem = topic_prefix_problem(prefix)
    if problem:
        return None, [_issue("mqtt_broker_topic_prefix_invalid", problem, f"{path}.topic_prefix")]
    bridge, zendure = raw.get("bridge", False), raw.get("zendure", False)
    if not isinstance(bridge, bool) or not isinstance(zendure, bool):
        return None, [_issue("mqtt_broker_settings_invalid", "bridge and zendure are true or false.", path)]
    return Account(name, prefix, bridge, zendure), []


def password_problem(password) -> str | None:
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return f"A password has {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters."
    if any(ord(char) < 32 or char == ":" for char in password):
        return "A password holds no control characters and no colon."
    return None


def generate_password() -> str:
    return secrets.token_urlsafe(18)


def hash_password(password, *, salt=None, iterations=PASSWORD_ITERATIONS) -> str:
    """Mosquitto's ``$7$`` form: PBKDF2-SHA512 with a random salt."""

    salt = secrets.token_bytes(PASSWORD_SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations)
    encode = base64.b64encode
    return f"$7${iterations}${encode(salt).decode()}${encode(digest).decode()}"


def grid_meter_topic(config) -> str | None:
    """The topic an MQTT grid meter reads on the built-in broker, or ``None``."""

    grid_meter = config.get("grid_meter") if isinstance(config, dict) else None
    settings = grid_meter_mqtt_settings(grid_meter)
    if settings.get("broker_ref") != BUILTIN_PROFILE_REF:
        return None
    topic = settings.get("topic")
    return topic.strip() if isinstance(topic, str) and topic.strip() else None


def render_mosquitto_conf(settings: BrokerSettings) -> str:
    return "\n".join(
        [
            "per_listener_settings false",
            f"listener {CONTAINER_PORT}",
            f"allow_anonymous {'true' if settings.anonymous else 'false'}",
            "password_file /mosquitto/config/passwd",
            "acl_file /mosquitto/config/acl",
            "persistence true",
            "persistence_location /mosquitto/data/",
            "message_size_limit 65536",
            "max_connections 64",
            "log_dest stdout",
            "",
        ]
    )


def _denials(account, grid_topic, grid_meter_account):
    denied = []
    if account is None or not account.bridge:
        denied.append(OUTPUT_TREE)
    if account is None or not account.zendure:
        denied.extend(ZENDURE_TREES)
    if grid_topic and (account is None or account.name != grid_meter_account):
        denied.append(grid_topic)
    return [f"topic deny {topic}" for topic in denied]


def render_acl(settings: BrokerSettings, *, grid_topic=None) -> str:
    """The ACL: anonymous clients first, then every account, then the internal ones."""

    if grid_topic is not None and _ACL_TOPIC.fullmatch(grid_topic) is None:
        raise ValueError("mqtt_broker_grid_meter_topic_unprotectable")
    lines = []
    if settings.anonymous:
        lines += ["topic readwrite #", *_denials(None, grid_topic, settings.grid_meter_account), ""]
    for account in settings.accounts:
        lines.append(f"user {account.name}")
        if account.topic_prefix:
            lines.append(f"topic readwrite {account.topic_prefix}/#")
        else:
            lines.append("topic readwrite #")
        if account.bridge:
            lines.append(f"topic readwrite {OUTPUT_TREE}")
        if account.zendure:
            lines += [f"topic readwrite {tree}" for tree in ZENDURE_TREES]
        if grid_topic and account.name == settings.grid_meter_account:
            lines.append(f"topic readwrite {grid_topic}")
        lines += _denials(account, grid_topic, settings.grid_meter_account)
        lines.append("")
    lines += [f"user {EMS_ACCOUNT}", f"topic read {OUTPUT_TREE}"]
    lines += [f"topic read {tree}" for tree in ZENDURE_TREES]
    if grid_topic:
        lines.append(f"topic read {grid_topic}")
    lines += [f"topic write {EMS_WRITE_TOPIC}", ""]
    lines += [f"user {MAPPING_ACCOUNT}", "topic read #", f"topic write {OUTPUT_TREE}", ""]
    lines += [f"user {ADMIN_ACCOUNT}", "topic read #", "topic read $SYS/#", ""]
    return "\n".join(lines)


def render_passwd(passwords) -> str:
    """``passwords`` maps a user name to its clear password; only hashes are written."""

    return "".join(f"{name}:{hash_password(password)}\n" for name, password in sorted(passwords.items()))


def render_compose(*, port, admin_image, puid, pgid) -> str:
    """The ``ems-mqtt`` project; paths are relative to ``<install>/mqtt/``.

    Directories are mounted, never single files: the Admin replaces a file
    atomically, and a file mount would keep showing the container the old one.
    """

    return "\n".join(
        [
            f"name: {COMPOSE_PROJECT}",
            "services:",
            f"  {BROKER_SERVICE}:",
            f"    image: {MOSQUITTO_IMAGE}",
            f"    container_name: {BROKER_SERVICE}",
            f'    user: "{puid}:{pgid}"',
            f'    ports: ["{port}:{CONTAINER_PORT}"]',
            "    volumes:",
            f"      - ./{BROKER_FILES_DIRNAME}:/mosquitto/config:ro",
            "      - ./data:/mosquitto/data",
            "    restart: unless-stopped",
            f"  {MAPPING_SERVICE}:",
            f"    image: {admin_image}",
            f"    container_name: {MAPPING_SERVICE}",
            f'    user: "{puid}:{pgid}"',
            '    entrypoint: ["python", "-B", "-m", "mqtt_mapping"]',
            "    command:",
            "      - --mappings",
            "      - /app/config/mqtt/mappings.json",
            "      - --brokers",
            "      - /app/mqtt/mapping/mapping-credentials.json",
            "      - --status",
            "      - /app/data/mqtt-mapping-status.json",
            "    environment:",
            '      TZ: "${TZ:-UTC}"',
            "    volumes:",
            "      - ../config/mqtt:/app/config/mqtt:ro",
            f"      - ./{MAPPING_FILES_DIRNAME}:/app/mqtt/mapping:ro",
            "      - ../data:/app/data",
            "    healthcheck:",
            '      test: ["CMD", "python", "-B", "-m", "mqtt_mapping", "--healthcheck",'
            ' "--status", "/app/data/mqtt-mapping-status.json"]',
            "      interval: 30s",
            "      timeout: 10s",
            "      retries: 3",
            "    restart: unless-stopped",
            "",
        ]
    )


def local_profile_endpoints(config, credential_store):
    """Every enabled local MQTT profile other than the built-in one, as mapping endpoints."""

    section = config.get("zendure_mqtt") if isinstance(config, dict) else None
    brokers = section.get("brokers") if isinstance(section, dict) else None
    endpoints = {}
    for ref, profile in (brokers or {}).items():
        if ref == BUILTIN_PROFILE_REF or not isinstance(profile, dict):
            continue
        if profile.get("enabled") is False or str(profile.get("source") or "local_mqtt") != "local_mqtt":
            continue
        host, port = profile.get("host"), profile.get("port", 1883)
        if not isinstance(host, str) or not host.strip() or not isinstance(port, int) or isinstance(port, bool):
            continue
        tls = profile.get("tls") is True or profile.get("tls_mode") in ("system_ca", "insecure_no_verify")
        entry = {
            "host": host.strip(),
            "port": port,
            "tls": tls,
            "tls_insecure": tls and profile.get("tls_mode") == "insecure_no_verify",
        }
        ref_name = profile.get("credentials_ref")
        if isinstance(ref_name, str) and ref_name:
            secret = credential_store.load_mqtt_broker_secret(ref_name)
            if secret is None or not secret.username:
                continue
            entry["username"], entry["password"] = secret.username, secret.password
        endpoints[ref] = entry
    return endpoints


@dataclass
class _Result:
    ok: bool
    status: str
    message: str = ""
    issues: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def as_dict(self):
        return {"ok": self.ok, "status": self.status, "message": self.message, "issues": self.issues, **self.extra}


def _failure(status, message, issues=()):
    return _Result(False, status, message, list(issues))


class BuiltinBroker:
    """Settings, accounts and containers of the built-in broker for one installation.

    Every Docker call goes through the injected :class:`DockerCompose`; the port
    probes and the clock are injected too, so tests never touch a daemon.
    """

    _lock = threading.Lock()

    def __init__(
        self,
        *,
        install_root,
        config_dir,
        data_dir,
        credential_store,
        compose=None,
        admin_image=None,
        runtime_identity=None,
        port_free=None,
        port_answers=None,
        monotonic=time.monotonic,
        sleep=time.sleep,
        password_factory=generate_password,
        config_loader=None,
    ):
        self.install_root = str(install_root)
        self.config_dir = str(config_dir)
        self.data_dir = str(data_dir)
        self.workspace = os.path.join(self.install_root, WORKSPACE_DIRNAME)
        self.store = credential_store
        self.compose = compose or DockerCompose()
        self.admin_image = admin_image
        self.runtime_identity = runtime_identity
        self._port_free = port_free or _port_free
        self._port_answers = port_answers or _port_answers
        self._monotonic = monotonic
        self._sleep = sleep
        self._password_factory = password_factory
        self._config_loader = config_loader or self._read_ems_config

    # --- reading ----------------------------------------------------------

    @property
    def settings_path(self):
        return broker_settings_path(self.config_dir)

    def _read_ems_config(self):
        try:
            with open(os.path.join(self.config_dir, "config.json"), encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError):
            return {}
        return config if isinstance(config, dict) else {}

    def load(self):
        """``(settings, issues, exists)``; no file is the default settings, switched off."""

        try:
            with open(self.settings_path, encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return default_settings(), [], False
        except (OSError, ValueError):
            return None, [_issue("mqtt_broker_settings_invalid", "broker.json cannot be read.")], True
        settings, issues = parse_settings(document)
        return settings, issues, True

    def _save(self, settings):
        raw = json.dumps(settings_document(settings), indent=2).encode("utf-8") + b"\n"
        atomic_write_bytes(self.settings_path, raw)

    def password(self, name) -> str | None:
        secret = self.store.load_mqtt_broker_secret(credentials_ref_for(name))
        return secret.password if secret is not None and secret.password else None

    def _store_password(self, name, password):
        self.store.save_mqtt_broker_secret(credentials_ref_for(name), name, password)

    def container_states(self) -> dict:
        if not os.path.isfile(os.path.join(self.workspace, "compose.yml")):
            return {}
        try:
            rows = self.compose.ps(self.workspace)
        except DockerError:
            return {}
        return {row.get("service"): row.get("state") for row in rows if row.get("service")}

    def mappings(self):
        mappings, _issues = load_mappings_file(mappings_path(self.config_dir))
        return mappings

    def view(self) -> dict:
        """What the Admin shows: settings, saved passwords, containers, users; no secret."""

        settings, issues, exists = self.load()
        states = self.container_states()
        config = self._config_loader()
        if not exists and builtin_profile_configured(config):
            issues = [
                *issues,
                _issue(
                    "mqtt_broker_profile_name_reserved",
                    f"The profile {BUILTIN_PROFILE_REF} is reserved for the built-in broker; rename it.",
                ),
            ]
        accounts = []
        for account in settings.accounts if settings else ():
            accounts.append(
                {
                    "name": account.name,
                    "topic_prefix": account.topic_prefix,
                    "bridge": account.bridge,
                    "zendure": account.zendure,
                    "password_saved": self.password(account.name) is not None,
                }
            )
        return {
            "configured": exists,
            "settings": settings_document(settings) if settings else None,
            "issues": issues,
            "accounts": accounts,
            "containers": {
                BROKER_SERVICE: states.get(BROKER_SERVICE),
                MAPPING_SERVICE: states.get(MAPPING_SERVICE),
            },
            "running": states.get(BROKER_SERVICE) == "running",
            "profile_ref": BUILTIN_PROFILE_REF,
            "profile_users": builtin_profile_users(config),
            "grid_meter_topic": grid_meter_topic(config),
            "mapping_status_path": mapping_status_path(self.data_dir),
        }

    # --- rendering --------------------------------------------------------

    def _ensure_internal_passwords(self):
        passwords = {}
        for name in INTERNAL_ACCOUNTS:
            password = self.password(name)
            if password is None:
                password = self._password_factory()
                self._store_password(name, password)
            passwords[name] = password
        return passwords

    def render(self, settings):
        """Write every file the two containers read; return issues instead of a partial set."""

        passwords = self._ensure_internal_passwords()
        issues = []
        for account in settings.accounts:
            password = self.password(account.name)
            if password is None:
                issues.append(
                    _issue(
                        "mqtt_broker_account_password_missing",
                        f"The account {account.name} has no password; set one.",
                        account.name,
                    )
                )
            else:
                passwords[account.name] = password
        config = self._config_loader()
        topic = grid_meter_topic(config)
        try:
            acl = render_acl(settings, grid_topic=topic)
        except ValueError:
            issues.append(
                _issue(
                    "mqtt_broker_grid_meter_topic_unprotectable",
                    "The grid meter topic cannot be protected; it must not contain spaces or wildcards.",
                )
            )
        if issues:
            return issues
        identity = self.runtime_identity or (os.getuid(), os.getgid())
        os.makedirs(os.path.join(self.workspace, "data"), exist_ok=True)
        files = {
            f"{BROKER_FILES_DIRNAME}/mosquitto.conf": (render_mosquitto_conf(settings), 0o644),
            f"{BROKER_FILES_DIRNAME}/acl": (acl, 0o600),
            f"{BROKER_FILES_DIRNAME}/passwd": (render_passwd(passwords), 0o600),
            "compose.yml": (
                render_compose(
                    port=settings.port,
                    admin_image=self.admin_image or "ghcr.io/basecubedev/ems-solarflow-admin:latest",
                    puid=identity[0],
                    pgid=identity[1],
                ),
                0o644,
            ),
            f"{MAPPING_FILES_DIRNAME}/mapping-credentials.json": (
                self._mapping_credentials(config, passwords[MAPPING_ACCOUNT]),
                0o600,
            ),
        }
        for name, (text, mode) in files.items():
            path = os.path.join(self.workspace, name)
            atomic_write_bytes(path, text.encode("utf-8"))
            os.chmod(path, mode)
        return []

    def _mapping_credentials(self, config, mapping_password):
        brokers = local_profile_endpoints(config, self.store)
        brokers[BUILTIN_PROFILE_REF] = {
            "host": BROKER_SERVICE,
            "port": CONTAINER_PORT,
            "username": MAPPING_ACCOUNT,
            "password": mapping_password,
        }
        return json.dumps({"schema_version": 1, "brokers": brokers}, indent=2, sort_keys=True) + "\n"

    def mapping_broker_refs(self):
        """The brokers a mapping may name: the built-in one and every usable local profile."""

        return {BUILTIN_PROFILE_REF, *local_profile_endpoints(self._config_loader(), self.store)}

    # --- containers -------------------------------------------------------

    def _wait_for_port(self, port):
        deadline = self._monotonic() + START_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            if self._port_answers(port):
                return True
            self._sleep(0.5)
        return self._port_answers(port)

    def _mapping_wanted(self, settings):
        mappings = self.mappings()
        if mappings is None:
            return False
        active = mappings.active_devices()
        return any(device.broker_ref != BUILTIN_PROFILE_REF or settings.enabled for device in active)

    def sync_mapping_container(self, settings=None):
        """Start or stop the mapping container to match the mappings; never removes it."""

        if settings is None:
            settings, _issues, _exists = self.load()
            if settings is None:
                return _failure("settings_invalid", "broker.json cannot be read.")
        wanted = self._mapping_wanted(settings)
        state = self.container_states().get(MAPPING_SERVICE)
        try:
            if wanted:
                issues = self.render(settings)
                if issues:
                    return _failure("render_failed", "The broker files cannot be written.", issues)
            if wanted and state != "running":
                self.compose.up(self.workspace, services=[MAPPING_SERVICE])
            elif not wanted and state == "running":
                self.compose.stop(self.workspace, [MAPPING_SERVICE])
        except DockerError as exc:
            return _failure(exc.code, exc.message)
        return _Result(True, "running" if wanted else "stopped")

    def _apply_running(self, settings, *, recreate=False):
        issues = self.render(settings)
        if issues:
            return _failure("render_failed", "The broker files cannot be written.", issues)
        if recreate:
            self.compose.up(self.workspace, services=[BROKER_SERVICE], force_recreate=True)
        else:
            self.compose.reload(self.workspace, BROKER_SERVICE)
        return _Result(True, "running")

    def enable(self):
        with self._one_at_a_time() as busy:
            if busy:
                return busy.as_dict()
            return self._enable().as_dict()

    def _enable(self):
        settings, issues, exists = self.load()
        if settings is None or issues:
            return _failure("settings_invalid", "broker.json cannot be used.", issues)
        if not exists and settings.accounts and self.password(DEFAULT_ACCOUNT) is None:
            self._store_password(DEFAULT_ACCOUNT, self._password_factory())
        candidate = replace(settings, enabled=True)
        render_issues = self.render(candidate)
        if render_issues:
            return _failure("render_failed", "The broker cannot be set up yet.", render_issues)
        running = self.container_states().get(BROKER_SERVICE) == "running"
        if not running and not self._port_free(candidate.port):
            return _failure(
                "mqtt_broker_port_in_use",
                f"Port {candidate.port} is taken by another program; choose another port.",
                [_issue("mqtt_broker_port_in_use", f"Port {candidate.port} is in use.", "port")],
            )
        try:
            self.compose.up(self.workspace, services=[BROKER_SERVICE])
        except DockerError as exc:
            return _failure(exc.code, exc.message)
        if not self._wait_for_port(candidate.port):
            try:
                self.compose.stop(self.workspace, [BROKER_SERVICE])
            except DockerError:
                pass
            return _failure("mqtt_broker_not_ready", "The broker did not start answering; it stays off.")
        self._save(candidate)
        mapping = self.sync_mapping_container(candidate)
        result = _Result(True, "running", extra={"mapping": mapping.as_dict()})
        if not exists:
            result.extra["created_account"] = DEFAULT_ACCOUNT
        return result

    def stop_impact(self, control_status=None) -> dict:
        """Who loses what when the broker stops, for the confirmation dialog."""

        config = self._config_loader()
        held = {}
        if isinstance(control_status, dict):
            for name, entry in (control_status.get("devices") or {}).items():
                if isinstance(entry, dict) and entry.get("output_limit_w") is not None:
                    held[name] = entry["output_limit_w"]
        external, zendure = [], []
        for index, item in enumerate(config.get("devices") or []):
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            mqtt = item.get("mqtt") if isinstance(item.get("mqtt"), dict) else {}
            if mqtt.get("broker_ref") != BUILTIN_PROFILE_REF:
                continue
            name = str(item.get("name") or f"device {index + 1}")
            if item.get("type") == "zendure_mqtt":
                zendure.append({"name": name, "held_output_limit_w": held.get(name)})
            else:
                external.append({"name": name})
        return {
            "external_devices": external,
            "zendure_devices": zendure,
            "grid_meter": grid_meter_topic(config) is not None,
        }

    def stop(self):
        with self._one_at_a_time() as busy:
            if busy:
                return busy.as_dict()
            return self._stop().as_dict()

    def _stop(self):
        settings, issues, exists = self.load()
        if settings is None:
            return _failure("settings_invalid", "broker.json cannot be read.", issues)
        if os.path.isfile(os.path.join(self.workspace, "compose.yml")):
            try:
                self.compose.stop(self.workspace, [BROKER_SERVICE])
            except DockerError as exc:
                return _failure(exc.code, exc.message)
        stopped = replace(settings, enabled=False)
        if exists:
            self._save(stopped)
        mapping = self.sync_mapping_container(stopped)
        return _Result(True, "stopped", extra={"mapping": mapping.as_dict()})

    # --- settings and accounts --------------------------------------------

    def update(self, changes):
        """Change port, anonymous, the grid meter account or one account's options."""

        with self._one_at_a_time() as busy:
            if busy:
                return busy.as_dict()
            return self._update(changes).as_dict()

    def _update(self, changes):
        settings, issues, exists = self.load()
        if settings is None:
            return _failure("settings_invalid", "broker.json cannot be read.", issues)
        document = settings_document(settings)
        for key in ("port", "anonymous", "grid_meter_account"):
            if key in changes:
                document[key] = changes[key]
        if "accounts" in changes:
            document["accounts"] = changes["accounts"]
        updated, problems = parse_settings(document)
        if problems:
            return _failure("invalid", "The change breaks a rule.", problems)
        known = {account.name for account in settings.accounts}
        for account in updated.accounts:
            if account.name not in known and self.password(account.name) is None:
                self._store_password(account.name, self._password_factory())
        result = self._persist(settings, updated)
        if result.ok:
            for name in known - {account.name for account in updated.accounts}:
                self.store.forget_mqtt_broker_secret(credentials_ref_for(name))
        return result

    def _persist(self, before, after):
        if after.enabled and after.port != before.port and not self._port_free(after.port):
            return _failure(
                "mqtt_broker_port_in_use",
                f"Port {after.port} is taken by another program; the broker keeps port {before.port}.",
                [_issue("mqtt_broker_port_in_use", f"Port {after.port} is in use.", "port")],
            )
        if after.enabled:
            try:
                result = self._apply_running(after, recreate=after.port != before.port)
            except DockerError as exc:
                return _failure(exc.code, exc.message)
            if not result.ok:
                return result
        self._save(after)
        return _Result(True, "saved")

    def set_password(self, name, password=None):
        """Set an account's password, or generate one; return it once in the result."""

        with self._one_at_a_time() as busy:
            if busy:
                return busy.as_dict()
            settings, issues, _exists = self.load()
            if settings is None:
                return _failure("settings_invalid", "broker.json cannot be read.", issues).as_dict()
            if settings.account(name) is None:
                return _failure("mqtt_broker_account_unknown", "There is no such account.").as_dict()
            password = self._password_factory() if password is None else password
            problem = password_problem(password)
            if problem:
                return _failure(
                    "mqtt_broker_account_password_invalid",
                    problem,
                    [_issue("mqtt_broker_account_password_invalid", problem, name)],
                ).as_dict()
            self._store_password(name, password)
            if settings.enabled:
                try:
                    result = self._apply_running(settings)
                except DockerError as exc:
                    return _failure(exc.code, exc.message).as_dict()
                if not result.ok:
                    return result.as_dict()
            return _Result(True, "saved", extra={"password": password}).as_dict()

    def reveal_password(self, name):
        settings, _issues, _exists = self.load()
        if settings is None or settings.account(name) is None:
            return None
        return self.password(name)

    class _Busy:
        def __init__(self, lock):
            self.lock = lock
            self.acquired = False

        def __enter__(self):
            self.acquired = self.lock.acquire(blocking=False)
            if self.acquired:
                return None
            return _failure("busy", "Another broker action is still running; try again in a moment.")

        def __exit__(self, *_exc):
            if self.acquired:
                self.lock.release()
            return False

    def _one_at_a_time(self):
        return self._Busy(self._lock)


def _port_free(port) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _port_answers(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False
