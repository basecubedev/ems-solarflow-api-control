# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical Appliance Manager filesystem layout.

Every path used by the agent, the web service and the CLI is resolved here, so
no request can introduce a path of its own. The environment overrides exist for
tests and for unpackaged developer runs; they are read from the process
environment only and are never derived from an HTTP request.
"""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_INSTALL_ROOT = "/opt/ems-solarflow"
DEFAULT_CONFIG_DIR = "/etc/ems-appliance-manager"
DEFAULT_STATE_DIR = "/var/lib/ems-appliance-manager"
DEFAULT_LOG_DIR = "/var/log/ems-appliance-manager"
DEFAULT_RUNTIME_DIR = "/run/ems-appliance-manager"

ENV_INSTALL_ROOT = "EMS_APPLIANCE_INSTALL_ROOT"
ENV_CONFIG_DIR = "EMS_APPLIANCE_CONFIG_DIR"
ENV_STATE_DIR = "EMS_APPLIANCE_STATE_DIR"
ENV_LOG_DIR = "EMS_APPLIANCE_LOG_DIR"
ENV_RUNTIME_DIR = "EMS_APPLIANCE_RUNTIME_DIR"

AGENT_SOCKET_NAME = "agent.sock"


class PathBoundaryError(Exception):
    """A resolved path left its canonical base directory."""

    code = "path_outside_boundary"


@dataclass(frozen=True)
class AppliancePaths:
    install_root: Path
    config_dir: Path
    state_dir: Path
    log_dir: Path
    runtime_dir: Path

    @property
    def compose_file(self):
        return self.install_root / "docker-compose.yml"

    @property
    def ems_config_dir(self):
        return self.install_root / "config"

    @property
    def ems_config_file(self):
        return self.ems_config_dir / "config.json"

    @property
    def ems_data_dir(self):
        return self.install_root / "data"

    @property
    def ems_backups_dir(self):
        return self.install_root / "backups"

    @property
    def admin_dir(self):
        return self.install_root / "admin"

    @property
    def admin_env_file(self):
        return self.admin_dir / "environment"

    @property
    def admin_bootstrap_state(self):
        return self.admin_dir / "bootstrap-state.json"

    @property
    def appliance_conf(self):
        return self.config_dir / "appliance.conf"

    @property
    def allowed_images_conf(self):
        return self.config_dir / "allowed-images.conf"

    @property
    def state_file(self):
        return self.state_dir / "state.json"

    @property
    def auth_file(self):
        return self.state_dir / "auth.json"

    @property
    def operations_dir(self):
        return self.state_dir / "operations"

    @property
    def known_good_dir(self):
        return self.state_dir / "known-good"

    @property
    def ssh_keys_dir(self):
        return self.state_dir / "ssh-keys"

    @property
    def compose_backup_dir(self):
        return self.state_dir / "compose-backup"

    @property
    def appliance_log(self):
        return self.log_dir / "appliance.log"

    @property
    def audit_log(self):
        return self.log_dir / "audit.log"

    @property
    def operations_log(self):
        return self.log_dir / "operations.log"

    @property
    def agent_socket(self):
        return self.runtime_dir / AGENT_SOCKET_NAME

    def export_paths(self):
        """Host directories the backup-access account may read."""

        return {
            "config": self.ems_config_dir,
            "backups": self.ems_backups_dir,
            "data": self.ems_data_dir,
        }

    def state_subdirectories(self):
        return (
            self.operations_dir,
            self.known_good_dir,
            self.ssh_keys_dir,
            self.compose_backup_dir,
        )


def resolve_paths(environ=None):
    """Resolve the appliance layout, honouring the documented env overrides."""

    env = os.environ if environ is None else environ
    return AppliancePaths(
        install_root=Path(env.get(ENV_INSTALL_ROOT) or DEFAULT_INSTALL_ROOT).resolve(),
        config_dir=Path(env.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR).resolve(),
        state_dir=Path(env.get(ENV_STATE_DIR) or DEFAULT_STATE_DIR).resolve(),
        log_dir=Path(env.get(ENV_LOG_DIR) or DEFAULT_LOG_DIR).resolve(),
        runtime_dir=Path(env.get(ENV_RUNTIME_DIR) or DEFAULT_RUNTIME_DIR).resolve(),
    )


def ensure_within(base, candidate):
    """Return ``candidate`` resolved inside ``base`` or raise ``PathBoundaryError``.

    Symlinks are resolved before the comparison, so a symlinked state entry can
    never redirect a write outside the appliance layout.
    """

    base_path = Path(base).resolve()
    resolved = Path(candidate)
    if not resolved.is_absolute():
        resolved = base_path / resolved
    resolved = resolved.resolve()
    if resolved != base_path and base_path not in resolved.parents:
        raise PathBoundaryError(f"{resolved} is outside {base_path}")
    return resolved


def ensure_directories(paths, mode=0o750):
    """Create the appliance state and log directories when missing."""

    for directory in (paths.state_dir, paths.log_dir, *paths.state_subdirectories()):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(mode)
        except OSError:
            pass
    return paths


def atomic_write(path, text, mode=0o640):
    """Write ``text`` to ``path`` atomically with an explicit file mode."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, target)
    return target
