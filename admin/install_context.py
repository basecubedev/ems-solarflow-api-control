# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detect the real EMS installation/config context for the Admin Server.

Admin is orchestration/UI only; the EMS core remains the source of truth for
where ``config.json``, the template, ``data/`` and ``docker-compose.yml`` live.
This helper reuses the central ``ems.paths`` resolver so Admin never grows a
second, divergent set of path rules.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from ems import paths

SOURCE_CANONICAL = "canonical"
SOURCE_LEGACY = "legacy"
SOURCE_ENV = "env"
SOURCE_DOCKER = "docker"
SOURCE_MISSING = "missing"


@dataclass(frozen=True)
class AdminInstallContext:
    config_path: Path
    config_exists: bool
    config_source: str
    template_path: Path
    template_exists: bool
    template_source: str
    data_dir: Path
    data_dir_exists: bool
    compose_path: Path
    compose_exists: bool
    config_layout_state: str

    @property
    def install_root(self):
        """The standard EMS install root that owns config/, data/, and compose."""

        return self.compose_path.parent

    def as_dict(self):
        return {
            "config_path": str(self.config_path),
            "config_exists": self.config_exists,
            "config_source": self.config_source,
            "template_path": str(self.template_path),
            "template_exists": self.template_exists,
            "template_source": self.template_source,
            "data_dir": str(self.data_dir),
            "data_dir_exists": self.data_dir_exists,
            "compose_path": str(self.compose_path),
            "compose_exists": self.compose_exists,
            "install_root": str(self.install_root),
            "config_layout_state": self.config_layout_state,
        }


def detect_install_context(base_dir=None):
    """Resolve the real EMS install layout via ``ems.paths``.

    ``base_dir`` overrides the resolver's project root (used by tests). When it
    is not given, ``EMS_INSTALL_DIR`` supplies the root: in the Admin container
    ``ems.paths.BASE_DIR`` points at ``/app`` (only the path resolver is copied
    in), so the real EMS installation must be mounted and pointed at explicitly.
    With neither set, the central resolver picks the canonical Docker-first
    path, falling back to the legacy repo path only when it is the one that
    exists.
    """

    if base_dir is None:
        base_dir = os.environ.get("EMS_INSTALL_DIR") or None

    config_path = paths.resolve_config_path(base_dir=base_dir)
    config_exists = config_path.exists()
    template_path = paths.resolve_template_path(base_dir=base_dir)
    template_exists = template_path.exists()
    data_dir = paths.resolve_data_dir(base_dir=base_dir)
    compose_path = paths.resolve_compose_path(base_dir=base_dir)
    return AdminInstallContext(
        config_path=config_path,
        config_exists=config_exists,
        config_source=_config_source(config_path, config_exists),
        template_path=template_path,
        template_exists=template_exists,
        template_source=_template_source(template_path, template_exists),
        data_dir=data_dir,
        data_dir_exists=data_dir.is_dir(),
        compose_path=compose_path,
        compose_exists=compose_path.is_file(),
        config_layout_state=paths.detect_config_layout_state(base_dir=base_dir),
    )


def _config_source(path, exists):
    env_value = os.environ.get("EMS_CONFIG_FILE")
    if env_value and Path(env_value) == path:
        return SOURCE_ENV
    if not exists:
        return SOURCE_MISSING
    return SOURCE_CANONICAL if path.parent.name == "config" else SOURCE_LEGACY


def _template_source(path, exists):
    env_value = os.environ.get("EMS_TEMPLATE_FILE")
    if env_value and Path(env_value) == path:
        return SOURCE_ENV
    if path == paths.DOCKER_TEMPLATE_PATH:
        return SOURCE_DOCKER
    if not exists:
        return SOURCE_MISSING
    return SOURCE_CANONICAL if path.parent.name == "config" else SOURCE_LEGACY
