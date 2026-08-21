# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical Appliance Manager filesystem layout.

Every path used by the agent, the web service and the CLI is resolved here, so
no request can introduce a path of its own. The environment overrides exist for
tests and for unpackaged developer runs; they are read from the process
environment only and are never derived from an HTTP request.
"""

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

DEFAULT_INSTALL_ROOT = "/opt/ems-solarflow"
DEFAULT_CONFIG_DIR = "/etc/ems-appliance-manager"
DEFAULT_STATE_DIR = "/var/lib/ems-appliance-manager"
DEFAULT_LOG_DIR = "/var/log/ems-appliance-manager"
DEFAULT_RUNTIME_DIR = "/run/ems-appliance-manager"
DEFAULT_EXPORT_ROOT = "/srv/ems-appliance-export"
DEFAULT_OS_UPDATE_DIR = "/var/lib/ems-appliance-os-update"

ENV_INSTALL_ROOT = "EMS_APPLIANCE_INSTALL_ROOT"
ENV_CONFIG_DIR = "EMS_APPLIANCE_CONFIG_DIR"
ENV_STATE_DIR = "EMS_APPLIANCE_STATE_DIR"
ENV_LOG_DIR = "EMS_APPLIANCE_LOG_DIR"
ENV_RUNTIME_DIR = "EMS_APPLIANCE_RUNTIME_DIR"
ENV_EXPORT_ROOT = "EMS_APPLIANCE_EXPORT_ROOT"
ENV_BACKUP_USER = "EMS_APPLIANCE_BACKUP_USER"
ENV_EXPORT_STATUS_FILE = "EMS_APPLIANCE_EXPORT_STATUS_FILE"
ENV_PACKAGE_LIBDIR = "EMS_APPLIANCE_LIBDIR"

# Where the package puts the shell helpers it ships. The maintainer scripts and
# the Python side must name the same directory, or a helper the appliance offers
# to run is one that is not there.
DEFAULT_PACKAGE_LIBDIR = "/usr/lib/ems-appliance-manager"

# Host paths are configured, never requested. The same character policy applies
# in Python, in the packaged shell tooling and in the generated systemd drop-in,
# so one configured root cannot mean three different things.
ALLOWED_PATH_CHARACTERS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-"
)

AGENT_SOCKET_NAME = "agent.sock"


def package_helper(name):
    """Absolute path of a shell helper this package ships.

    The directory is fixed by the package, never by a request: the override
    exists so the test suite and an unpackaged developer run can point at a
    staged tree, exactly as the maintainer scripts already do.
    """

    libdir = os.environ.get(ENV_PACKAGE_LIBDIR) or DEFAULT_PACKAGE_LIBDIR
    return Path(libdir) / name


class PathBoundaryError(Exception):
    """A resolved path left its canonical base directory."""

    code = "path_outside_boundary"


def validate_host_path(label, value):
    """Return an absolute, unambiguous host path or raise ``PathBoundaryError``."""

    text = str(value or "").strip()
    if not text.startswith("/"):
        raise PathBoundaryError(f"{label} must be an absolute path")
    if text != "/" and text.endswith("/"):
        raise PathBoundaryError(f"{label} must not end in a slash")
    if text == "/":
        raise PathBoundaryError(f"{label} must not be the filesystem root")
    unexpected = sorted(set(text) - ALLOWED_PATH_CHARACTERS)
    if unexpected:
        raise PathBoundaryError(
            f"{label} contains characters a host path may not use: {''.join(unexpected)}"
        )
    for segment in text.split("/")[1:]:
        if segment == "":
            raise PathBoundaryError(f"{label} must not contain an empty path segment")
        if segment in (".", ".."):
            raise PathBoundaryError(f"{label} must not contain a {segment!r} path segment")
    return text


def path_components(text):
    """``/a/b`` → ``('/a', '/a/b')``: every component that must be a directory."""

    prefix = ""
    components = []
    for segment in str(text).split("/")[1:]:
        prefix = f"{prefix}/{segment}"
        components.append(prefix)
    return tuple(components)


def validate_configured_root(label, value, *, lstat=None):
    """Validate a configured host root without ever following a symbolic link.

    The configured path is the identity, so it is never canonicalised: a
    symlink at the path itself, or at any component that already exists, is
    refused. Everything below the nearest existing parent is still to be
    created and is checked again as it is.
    """

    lstat = os.lstat if lstat is None else lstat
    text = validate_host_path(label, value)
    for component in path_components(text):
        try:
            entry = lstat(component)
        except FileNotFoundError:
            return text
        except OSError as exc:
            raise PathBoundaryError(
                f"{label} cannot be inspected at {component}: {exc.__class__.__name__}"
            )
        if stat_module.S_ISLNK(entry.st_mode):
            raise PathBoundaryError(
                f"{label} is reached through a symbolic link at {component}; "
                "a separate partition must be mounted at the configured path"
            )
        if not stat_module.S_ISDIR(entry.st_mode):
            raise PathBoundaryError(f"{label} passes through {component}, which is not a directory")
    return text


def path_boundary_problems(label, value, *, lstat=None):
    """The no-follow policy as a list of named problems instead of an exception."""

    try:
        validate_configured_root(label, value, lstat=lstat)
    except PathBoundaryError as exc:
        return [str(exc)]
    return []


def runtime_boundary_problems(paths, *, lstat=None):
    """Re-check every configured host path the export feature acts on.

    Installation validated these paths once. A component that became a symbolic
    link afterwards would redirect an export, a chroot or a root-owned mkdir, so
    the same policy runs again wherever the paths are used, not only when they
    are configured.
    """

    problems = list(path_boundary_problems("the EMS installation root", paths.install_root, lstat=lstat))
    problems += path_boundary_problems("the export root", paths.export_root, lstat=lstat)
    for name, source in sorted(paths.export_paths().items()):
        problems += path_boundary_problems(f"the {name} export source", source, lstat=lstat)
    for name, target in sorted(paths.export_targets().items()):
        problems += path_boundary_problems(f"the {name} export target", target, lstat=lstat)
    return problems


def validate_root_pair(install_root, export_root):
    """Two roots that overlap in either direction are one boundary, not two."""

    install_text = str(install_root)
    export_text = str(export_root)
    if install_text == export_text:
        raise PathBoundaryError(
            "the EMS installation root and the export root must not be the same directory"
        )
    if f"{export_text}/".startswith(f"{install_text}/"):
        raise PathBoundaryError(
            "the export root must not live inside the EMS installation root"
        )
    if f"{install_text}/".startswith(f"{export_text}/"):
        raise PathBoundaryError(
            "the EMS installation root must not live inside the export root; the SFTP "
            "chroot would publish the whole installation"
        )
    return True


# sshd refuses a ChrootDirectory whose path is not root-owned or is writable by
# group or others, so this is a functional requirement of the export root.
CHROOT_FORBIDDEN_MODE = stat_module.S_IWGRP | stat_module.S_IWOTH


def chroot_chain_problems(export_root, *, stat_fn=None, euid=None):
    """Which components of a chroot path sshd would refuse, if any."""

    euid = os.geteuid() if euid is None else euid
    if euid != 0:
        return []
    stat_fn = os.stat if stat_fn is None else stat_fn
    problems = []
    for component in path_components(str(export_root)):
        try:
            entry = stat_fn(component)
        except OSError:
            continue
        if entry.st_uid != 0:
            problems.append(f"{component} is not owned by root")
        if stat_module.S_IMODE(entry.st_mode) & CHROOT_FORBIDDEN_MODE:
            problems.append(f"{component} is writable by group or others")
    return problems


@dataclass(frozen=True)
class AppliancePaths:
    install_root: Path
    config_dir: Path
    state_dir: Path
    log_dir: Path
    runtime_dir: Path
    export_root: Path = Path(DEFAULT_EXPORT_ROOT)

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
    def timezone_file(self):
        """The operator's chosen zone, on a shared path both slots see.

        Separate from appliance.conf, which is a packaged conffile an admin
        edits: a value set through the web UI must not rewrite the package's
        own file, and must survive a slot switch.
        """

        return self.config_dir / "timezone"

    @property
    def appliance_conf(self):
        return self.config_dir / "appliance.conf"

    @property
    def allowed_images_conf(self):
        return self.config_dir / "allowed-images.conf"

    # --- web-owned state (writable by the unprivileged web service) -------

    @property
    def web_state_dir(self):
        return self.state_dir / "web"

    @property
    def web_auth_dir(self):
        return self.web_state_dir / "auth"

    @property
    def auth_file(self):
        return self.web_auth_dir / "auth.json"

    @property
    def web_sessions_dir(self):
        return self.web_state_dir / "sessions"

    @property
    def web_preferences_dir(self):
        return self.web_state_dir / "ui-preferences"

    @property
    def state_file(self):
        return self.web_state_dir / "state.json"

    @property
    def web_log_dir(self):
        return self.log_dir / "web"

    @property
    def appliance_log(self):
        return self.web_log_dir / "appliance.log"

    # --- agent-owned state (root only; the web service reads it through the
    # agent API, never by opening these files) ----------------------------

    @property
    def agent_state_dir(self):
        return self.state_dir / "agent"

    @property
    def operations_dir(self):
        return self.agent_state_dir / "operations"

    @property
    def known_good_dir(self):
        return self.agent_state_dir / "known-good"

    @property
    def ssh_keys_dir(self):
        return self.agent_state_dir / "ssh-keys"

    @property
    def compose_backup_dir(self):
        return self.agent_state_dir / "compose-backup"

    @property
    def package_state_dir(self):
        return self.agent_state_dir / "package-state"

    @property
    def recovery_dir(self):
        return self.agent_state_dir / "recovery"

    @property
    def support_dir(self):
        return self.agent_state_dir / "support"

    @property
    def packages_dir(self):
        return self.agent_state_dir / "packages"

    @property
    def os_update_dir(self):
        """A/B state and staged artifacts, on the shared persistent partition.

        Deliberately outside the agent state tree: both slots read it, and on an
        A/B appliance it is a mount from /persist rather than part of the root
        filesystem the update is about to replace.
        """

        return Path(DEFAULT_OS_UPDATE_DIR)

    @property
    def export_status_file(self):
        return self.agent_state_dir / "export-access.json"

    @property
    def agent_log_dir(self):
        return self.log_dir / "agent"

    @property
    def operations_log(self):
        return self.agent_log_dir / "operations.log"

    @property
    def agent_log(self):
        return self.agent_log_dir / "agent.log"

    # --- audit trail (written by the agent, never by the web service) -----

    @property
    def audit_log_dir(self):
        return self.log_dir / "audit"

    @property
    def audit_log(self):
        return self.audit_log_dir / "audit.log"

    # --- legacy shared layout, kept for migration only --------------------

    @property
    def legacy_auth_file(self):
        return self.state_dir / "auth.json"

    @property
    def legacy_operations_dir(self):
        return self.state_dir / "operations"

    @property
    def legacy_known_good_dir(self):
        return self.state_dir / "known-good"

    @property
    def legacy_ssh_keys_dir(self):
        return self.state_dir / "ssh-keys"

    @property
    def legacy_compose_backup_dir(self):
        return self.state_dir / "compose-backup"

    @property
    def legacy_packages_dir(self):
        return self.state_dir / "packages"

    @property
    def legacy_state_file(self):
        return self.state_dir / "state.json"

    @property
    def legacy_appliance_log(self):
        return self.log_dir / "appliance.log"

    @property
    def legacy_audit_log(self):
        return self.log_dir / "audit.log"

    @property
    def legacy_operations_log(self):
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

    def export_targets(self):
        """Where each exported directory appears inside the SFTP chroot."""

        return {name: self.export_root / name for name in self.export_paths()}

    def web_directories(self):
        return (
            self.web_state_dir,
            self.web_auth_dir,
            self.web_sessions_dir,
            self.web_preferences_dir,
            self.web_log_dir,
        )

    def agent_directories(self):
        return (
            self.agent_state_dir,
            self.operations_dir,
            self.known_good_dir,
            self.ssh_keys_dir,
            self.compose_backup_dir,
            self.package_state_dir,
            self.recovery_dir,
            self.support_dir,
            self.packages_dir,
            self.agent_log_dir,
            self.audit_log_dir,
        )

    def state_subdirectories(self):
        return (*self.web_directories(), *self.agent_directories())


def resolve_paths(environ=None):
    """Resolve the appliance layout from the host configuration.

    Precedence is process environment, then the root-owned ``appliance.conf``,
    then the packaged defaults. The two roots an operator may move — the EMS
    installation and the SFTP export root — are validated here, so the Python
    services, the packaged shell tooling and the generated systemd drop-in all
    take the same value from the same authority.
    """

    from appliance.config import read_host_paths

    env = os.environ if environ is None else environ
    config_dir = Path(env.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR).resolve()
    configured = read_host_paths(config_dir / "appliance.conf")

    install_root = env.get(ENV_INSTALL_ROOT) or configured.get("install_root") or DEFAULT_INSTALL_ROOT
    export_root = env.get(ENV_EXPORT_ROOT) or configured.get("export_root") or DEFAULT_EXPORT_ROOT
    install_root = validate_configured_root("the EMS installation root", install_root)
    export_root = validate_configured_root("the export root", export_root)
    validate_root_pair(install_root, export_root)

    return AppliancePaths(
        install_root=Path(install_root),
        config_dir=config_dir,
        state_dir=Path(env.get(ENV_STATE_DIR) or DEFAULT_STATE_DIR).resolve(),
        log_dir=Path(env.get(ENV_LOG_DIR) or DEFAULT_LOG_DIR).resolve(),
        runtime_dir=Path(env.get(ENV_RUNTIME_DIR) or DEFAULT_RUNTIME_DIR).resolve(),
        export_root=Path(export_root),
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


ROLE_WEB = "web"
ROLE_AGENT = "agent"

# The shared roots stay group-traversable so the web account can reach its own
# subtree; everything the agent owns is root-only, because a readable operation
# record would hand the web process confirmation tokens and known-good state.
SHARED_ROOT_MODE = 0o750
WEB_DIR_MODE = 0o750
WEB_PRIVATE_DIR_MODE = 0o700
AGENT_DIR_MODE = 0o700
AGENT_FILE_MODE = 0o600
WEB_FILE_MODE = 0o640


def directory_modes(paths, *, role="all"):
    """The ``(directory, mode)`` pairs this role is responsible for."""

    targets = [(paths.state_dir, SHARED_ROOT_MODE), (paths.log_dir, SHARED_ROOT_MODE)]
    private = (paths.web_auth_dir, paths.web_sessions_dir)
    if role in (ROLE_WEB, "all"):
        for directory in paths.web_directories():
            targets.append(
                (directory, WEB_PRIVATE_DIR_MODE if directory in private else WEB_DIR_MODE)
            )
    if role in (ROLE_AGENT, "all"):
        for directory in paths.agent_directories():
            targets.append((directory, AGENT_DIR_MODE))
    return tuple(targets)


def ensure_directories(paths, *, role="all", mode=None):
    """Create the directories this process owns.

    A packaged installation gets its layout from tmpfiles and the postinst; this
    is the developer and test path. Directories owned by the other role are left
    alone, and a permission error is not fatal — the unprivileged web service
    must never abort because agent state is not writable for it.
    """

    for directory, default in directory_modes(paths, role=role):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(default if mode is None else mode)
        except OSError:
            continue
    return paths


def own_by_root(path):
    """Give an agent-owned file to root:root.

    The agent unit runs with ``Group=ems-appliance`` so it can own the socket,
    which means every file it creates would otherwise inherit that group. The
    shared group is for the socket only.
    """

    try:
        if os.geteuid() != 0:
            return False
        os.chown(path, 0, 0)
    except (AttributeError, OSError):
        return False
    return True


def atomic_write(path, text, mode=0o640, *, owner_root=False):
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
    if owner_root:
        own_by_root(tmp)
    os.replace(tmp, target)
    return target
