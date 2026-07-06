# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Admin/Dashboard password resolution for the Admin Console.

There is exactly one local password for an EMS host. The EMS Dashboard and the
Admin Console share the same file, ``<install_root>/config/dashboard-auth.json``,
so a user never creates a second Admin-only password. This module only resolves
*where* that shared file lives and wraps the ``dashboard.auth`` primitives — it
never stores the password inside the Admin container.

Path resolution follows the real EMS install root (from ``EMS_INSTALL_DIR`` /
install context), honouring ``dashboard.auth_file`` from ``config/config.json``
when that config exists, and falling back to the standard location when it does
not. No running EMS and no ``config/config.json`` are required for a fresh
install.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from admin.install_context import detect_install_context
from dashboard.auth import (
    create_password_file_if_missing,
    load_auth_file,
    verify_password_file,
)

# Friendly relative hint surfaced to the UI; never an absolute host path.
SHARED_AUTH_HINT = "config/dashboard-auth.json"

# Surfaced when the shared file exists but cannot be parsed/validated. This is a
# recovery state, never first-password setup.
AUTH_FILE_INVALID_ERROR = "auth_file_invalid"
AUTH_FILE_INVALID_MESSAGE = (
    "The shared password file exists but cannot be read. "
    "Repair or remove config/dashboard-auth.json on the EMS host."
)

SOURCE_DEFAULT_MISSING_CONFIG = "default_missing_config"
SOURCE_CONFIG_DASHBOARD_AUTH_FILE = "config_dashboard_auth_file"
SOURCE_DEFAULT_CONFIG_PARSE_FAILED = "default_config_parse_failed"


@dataclass(frozen=True)
class AdminAuthPaths:
    install_root: Path
    auth_file: Path
    config_path: Path
    config_exists: bool
    source: str
    warning: str = None


def resolve_admin_auth_paths(base_dir=None):
    """Resolve the shared auth-file path from the Admin install context.

    The auth file always lives under the real EMS install root, never under the
    Admin container's ``/app``. When ``config/config.json`` exists and defines a
    non-empty ``dashboard.auth_file``, that path wins (resolved relative to the
    install root when relative). A missing or unreadable config falls back to the
    standard ``config/dashboard-auth.json`` without crashing the login page.
    """

    context = detect_install_context(base_dir=base_dir)
    install_root = Path(context.install_root)
    config_path = Path(context.config_path)
    config_exists = context.config_exists
    default_auth = install_root / "config" / "dashboard-auth.json"

    if not config_exists:
        return AdminAuthPaths(
            install_root=install_root,
            auth_file=default_auth,
            config_path=config_path,
            config_exists=False,
            source=SOURCE_DEFAULT_MISSING_CONFIG,
        )

    try:
        with open(config_path) as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return AdminAuthPaths(
            install_root=install_root,
            auth_file=default_auth,
            config_path=config_path,
            config_exists=True,
            source=SOURCE_DEFAULT_CONFIG_PARSE_FAILED,
            warning="config/config.json could not be parsed; using the default auth path.",
        )

    auth_file = None
    if isinstance(config, dict):
        dashboard = config.get("dashboard")
        if isinstance(dashboard, dict):
            configured = dashboard.get("auth_file")
            if isinstance(configured, str) and configured.strip():
                auth_file = configured.strip()

    if auth_file is None:
        return AdminAuthPaths(
            install_root=install_root,
            auth_file=default_auth,
            config_path=config_path,
            config_exists=True,
            source=SOURCE_DEFAULT_MISSING_CONFIG,
        )

    resolved = Path(auth_file)
    if not resolved.is_absolute():
        resolved = install_root / resolved
    return AdminAuthPaths(
        install_root=install_root,
        auth_file=resolved,
        config_path=config_path,
        config_exists=True,
        source=SOURCE_CONFIG_DASHBOARD_AUTH_FILE,
    )


def install_dir_available(*, base_dir=None):
    """True when the resolved install root exists and is writable.

    Guards initial password creation: in the Admin container without a real host
    mount the install root is the read-only image dir, so this returns ``False``
    and the caller refuses to create an internal auth file. With a mounted host
    install (or a test temp dir) the root is writable and creation proceeds.
    """

    paths = resolve_admin_auth_paths(base_dir=base_dir)
    root = paths.install_root
    return root.is_dir() and os.access(str(root), os.W_OK)


@dataclass(frozen=True)
class AdminAuthStatus:
    """Classification of the shared auth file: missing, valid, or malformed.

    ``configured`` is true whenever the file exists (valid or not). ``valid`` is
    true only for a readable password file. ``recovery_required`` marks an
    existing-but-unreadable file, which must never reopen first-password setup.
    """

    configured: bool
    valid: bool
    recovery_required: bool
    error: str = None
    message: str = None


def admin_auth_status(*, base_dir=None):
    """Resolve whether the shared password file is missing, valid, or malformed."""

    paths = resolve_admin_auth_paths(base_dir=base_dir)
    try:
        record = load_auth_file(str(paths.auth_file))
    except ValueError:
        # A malformed auth file must not reopen first-password setup.
        return AdminAuthStatus(
            configured=True,
            valid=False,
            recovery_required=True,
            error=AUTH_FILE_INVALID_ERROR,
            message=AUTH_FILE_INVALID_MESSAGE,
        )
    if record is None:
        return AdminAuthStatus(configured=False, valid=False, recovery_required=False)
    return AdminAuthStatus(configured=True, valid=True, recovery_required=False)


def admin_auth_configured(*, base_dir=None):
    """True when the shared password file exists and is a readable password file.

    A malformed existing file is *not* "configured" for this predicate; callers
    that must distinguish recovery from a fresh install use ``admin_auth_status``.
    """

    return admin_auth_status(base_dir=base_dir).valid


def create_shared_password_if_missing(password, *, base_dir=None):
    """Create the shared password file atomically. Raises ``FileExistsError``.

    Returns ``True`` on success. The caller resolves validation (length/confirm)
    before calling this; here we only guarantee the first-visitor-wins write.
    """

    paths = resolve_admin_auth_paths(base_dir=base_dir)
    # Initial password creation must not overwrite a password created by another
    # browser; O_EXCL turns that race into a clean FileExistsError.
    create_password_file_if_missing(str(paths.auth_file), password)
    return True


def verify_admin_password(password, *, base_dir=None):
    """Verify a login attempt against the shared password file."""

    paths = resolve_admin_auth_paths(base_dir=base_dir)
    return verify_password_file(str(paths.auth_file), password)
