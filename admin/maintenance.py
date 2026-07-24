# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only Maintenance overview of an existing EMS installation.

Never builds, starts, stops or mutates anything. Host Docker being unavailable
must degrade to file/config/compose facts, never break the overview.
"""

import json
import os
import re

from admin.admin_update import admin_image_ref_from_env
from admin.container_names import (
    DEFAULT_INFLUX_CONTAINER,
    resolve_ems_container_name,
)
from admin.install_context import detect_install_context
from admin.install_state import (
    STATE_ADMIN_PREPARED_INSTALL,
    STATE_COMPOSE_ONLY,
    STATE_LEGACY_ROOT_CONFIG,
    STATE_NONE,
    STATE_PARTIAL_INSTALL,
    STATE_STANDARD_CONFIG_ONLY,
    STATE_STANDARD_INSTALL,
    detect_install_state,
)

_CONTAINER_NAME_RE = re.compile(
    r"^\s*container_name:\s*[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"']?\s*(?:#.*)?$",
    re.MULTILINE,
)

EMS_RUNNING_IDENTITY_UNKNOWN_WARNING = (
    "EMS is running but its image identity could not be verified; the installed "
    "release is unknown. The Compose or last-known-good release is not shown as "
    "the running one."
)
_IMAGE_RE = re.compile(
    r"^\s*image:\s*[\"']?(\S+?)[\"']?\s*(?:#.*)?$", re.MULTILINE
)

_STATE_LABELS = {
    STATE_STANDARD_INSTALL: (
        "Standard installation",
        "A standard EMS installation was found.",
    ),
    STATE_ADMIN_PREPARED_INSTALL: (
        "Admin-prepared installation",
        "An Admin-prepared EMS installation was found.",
    ),
    STATE_STANDARD_CONFIG_ONLY: (
        "Config without compose",
        "A standard config/config.json exists but docker-compose.yml is missing.",
    ),
    STATE_LEGACY_ROOT_CONFIG: (
        "Legacy root config",
        "A legacy root config.json was found outside the standard config/ layout.",
    ),
    STATE_COMPOSE_ONLY: (
        "Compose without config",
        "A docker-compose.yml exists but no config.json was found.",
    ),
    STATE_PARTIAL_INSTALL: (
        "Partial installation",
        "This looks like a partial EMS installation.",
    ),
    STATE_NONE: (
        "No installation found",
        "No EMS installation was detected in the standard layout.",
    ),
}

# States that are inspectable but not a clean, complete standard install.
_PARTIAL_STATES = frozenset(
    {
        STATE_STANDARD_CONFIG_ONLY,
        STATE_LEGACY_ROOT_CONFIG,
        STATE_COMPOSE_ONLY,
        STATE_PARTIAL_INSTALL,
    }
)

PARTIAL_INSTALL_WARNING = (
    "This looks like a partial EMS installation. Maintenance can inspect it, "
    "but repair actions are not part of this read-only overview yet."
)

_DOCKER_UNAVAILABLE_MESSAGE = (
    "Docker is not available. Container status could not be read."
)


def run_maintenance_overview(base_dir=None, docker=None, admin_image=None):
    """Assemble the read-only Maintenance overview for the current install root."""

    context = detect_install_context(base_dir=base_dir)
    install_state = detect_install_state(base_dir=base_dir)

    compose_text = _read_compose(context) if context.compose_exists else ""
    specs = _container_specs(compose_text)
    docker_info, containers = _inspect_containers(docker, specs)

    label, message = _STATE_LABELS.get(
        install_state.state,
        ("Unknown state", "The install state could not be classified."),
    )

    warnings = list(install_state.warnings)
    if install_state.state in _PARTIAL_STATES:
        warnings.append(PARTIAL_INSTALL_WARNING)

    ems_view = containers["ems"]
    if ems_view.get("running") and ems_view.get("tag") is None:
        warnings.append(EMS_RUNNING_IDENTITY_UNKNOWN_WARNING)

    admin_image = admin_image or admin_image_ref_from_env()
    return {
        "install_state": {
            "state": install_state.state,
            "label": label,
            "message": message,
            "recommended_path": install_state.recommended_path,
            "reasons": list(install_state.reasons),
        },
        "paths": {
            "config": {
                "path": str(context.config_path),
                "exists": context.config_exists,
            },
            "data": {
                "path": str(context.data_dir),
                "exists": context.data_dir_exists,
            },
            "compose": {
                "path": str(context.compose_path),
                "exists": context.compose_exists,
            },
        },
        "docker": docker_info,
        "containers": containers,
        "components": {
            "admin": {
                "image": admin_image,
                "tag": _image_tag(admin_image),
            },
            "ems": {
                "image": containers["ems"].get("image"),
                "tag": containers["ems"].get("tag"),
            },
        },
        "links": {"dashboard_url": _dashboard_url(context)},
        "warnings": warnings,
    }


def _read_compose(context):
    try:
        return context.compose_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _container_specs(compose_text):
    """Resolve the EMS/InfluxDB container name + declared image from compose.

    Names/images come from the real docker-compose.yml when present; the
    canonical container names are the fallback so a partial install with a
    missing/damaged compose file can still be probed by well-known name.
    """

    names = list(dict.fromkeys(_CONTAINER_NAME_RE.findall(compose_text)))
    images = _IMAGE_RE.findall(compose_text)
    return {
        "ems": {
            "name": resolve_ems_container_name(
                compose_text=compose_text, env=os.environ
            ),
            "declared_image": _classify_image(images, want_influx=False),
        },
        "influxdb": {
            "name": _classify_name(names, want_influx=True) or DEFAULT_INFLUX_CONTAINER,
            "declared_image": _classify_image(images, want_influx=True),
        },
    }


def _classify_name(names, want_influx):
    for name in names:
        if ("influx" in name.lower()) == want_influx:
            return name
    return None


def _classify_image(images, want_influx):
    for image in images:
        if ("influxdb" in image.lower()) == want_influx:
            return image
    return None


def _inspect_containers(docker, specs):
    docker = docker if docker is not None else _default_docker()
    probe = getattr(docker, "probe", None)
    inspect = getattr(docker, "inspect_container", None)
    inspect_image = getattr(docker, "inspect_image", None)

    state = None
    if callable(probe):
        try:
            state = probe()
        except Exception:  # host Docker state must never break the read-only view
            state = None

    available = bool(state and state.get("state") == "ready")
    docker_info = {
        "available": available,
        "state": (state or {}).get("state"),
        "error": None if available else _docker_error_message(state),
        "server_version": (state or {}).get("server_version"),
    }

    containers = {
        role: _container_status(
            available, inspect, inspect_image, spec["name"], spec["declared_image"]
        )
        for role, spec in specs.items()
    }
    return docker_info, containers


def _container_status(available, inspect, inspect_image, name, declared_image):
    if not available or not callable(inspect):
        return _container_view(False, False, name, declared_image, "unknown",
                               inspect_image)
    try:
        existing = inspect(name)
    except Exception:  # a failed inspect is reported as unknown, never a 500
        return _container_view(False, False, name, declared_image, "unknown",
                               inspect_image)
    if existing is None:
        return _container_view(False, False, name, declared_image, "missing",
                               inspect_image)
    status = str(existing.get("status") or "unknown").lower() or "unknown"
    return _container_view(
        True,
        status == "running",
        existing.get("container_name") or name,
        existing.get("image") or declared_image,
        status,
        inspect_image,
    )


def _container_view(found, running, name, image, status, inspect_image=None):
    return {
        "found": found,
        "running": running,
        "name": name,
        "image": image,
        "tag": _image_version_tag(image, inspect_image),
        "status": status,
    }


def _image_tag(image):
    """Return the version tag of an image ref, ignoring a registry host:port."""

    if not image:
        return None
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else None


def _image_version_tag(image, inspect_image=None):
    """Return a readable version for an image ref.

    A ``:tag`` ref yields its tag directly. A digest-pinned ref carries no tag,
    so the readable release is recovered from the image's OCI build labels via the
    shared helper (release_tag, then version) — the same recovery ReleaseManager
    uses, so the two never disagree — rather than shown as a bare digest.
    """

    from admin.installed_release import release_tag_from_labels

    tag = _image_tag(image)
    if tag is not None:
        return tag
    if not image or not callable(inspect_image):
        return None
    try:
        info = inspect_image(image)
    except Exception:
        return None
    labels = (info or {}).get("labels") if isinstance(info, dict) else None
    return release_tag_from_labels(labels)


def _docker_error_message(state):
    if not state:
        return _DOCKER_UNAVAILABLE_MESSAGE
    return state.get("message") or _DOCKER_UNAVAILABLE_MESSAGE


def _dashboard_url(context):
    if not context.config_exists:
        return None
    try:
        parsed = json.loads(context.config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    port, scheme = 8080, "http"
    dashboard = parsed.get("dashboard") if isinstance(parsed, dict) else None
    if isinstance(dashboard, dict):
        candidate = dashboard.get("port")
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and 1 <= candidate <= 65535
        ):
            port = candidate
        if dashboard.get("ssl_enabled") is True:
            scheme = "https"
    return f"{scheme}://localhost:{port}"


def _default_docker():
    from admin.deployment import DockerCli

    return DockerCli()
