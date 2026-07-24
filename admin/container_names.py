# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single shared precedence for the productive EMS container name."""

import os
import re

DEFAULT_EMS_CONTAINER = "ems-solarflow-api-control"
DEFAULT_INFLUX_CONTAINER = "ems-influxdb"

_CONTAINER_NAME_RE = re.compile(
    r"^\s*container_name:\s*[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"']?\s*(?:#.*)?$",
    re.MULTILINE,
)


def ems_container_name_from_compose(compose_text):
    for name in _CONTAINER_NAME_RE.findall(compose_text or ""):
        if "influx" not in name.lower():
            return name
    return None


def resolve_ems_container_name(*, explicit=None, compose_text=None, env=None):
    explicit = explicit.strip() if isinstance(explicit, str) else ""
    if explicit:
        return explicit
    source = env if env is not None else os.environ
    configured = str(source.get("EMS_CONTAINER_NAME", "") or "").strip()
    if configured:
        return configured
    from_compose = ems_container_name_from_compose(compose_text)
    if from_compose:
        return from_compose
    return DEFAULT_EMS_CONTAINER
