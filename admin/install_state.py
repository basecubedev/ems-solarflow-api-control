# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detect Admin install state and route setup vs. maintenance.

Admin exposes only two user flows: "set up a new system" and "manage my
existing system". This service inspects the real EMS install root (via the
shared ``install_context`` / ``ems.paths`` resolvers, never a second set of
path rules) and recommends the safest flow, so an existing or legacy install is
never sent down the fresh-setup path silently.

It also owns the one-way migration of a legacy root ``./config.json`` to the
standard ``./config/config.json`` layout. Migration is copy-only: it validates
the source is a JSON object, backs up before writing, writes atomically, and
never deletes ``data/`` or the legacy source, and never overwrites an existing
standard config without explicit confirmation.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from admin.install_context import detect_install_context
from ems import paths

STATE_NONE = "none"
STATE_LEGACY_ROOT_CONFIG = "legacy_root_config"
STATE_STANDARD_CONFIG_ONLY = "standard_config_only"
STATE_COMPOSE_ONLY = "compose_only"
STATE_STANDARD_INSTALL = "standard_install"
STATE_ADMIN_PREPARED_INSTALL = "admin_prepared_install"
STATE_PARTIAL_INSTALL = "partial_install"

PATH_SETUP_NEW = "setup_new"
PATH_MANAGE_EXISTING = "manage_existing"

ADMIN_MARKER_RELATIVE = ("admin", "state", ".admin-deployment.json")


@dataclass(frozen=True)
class InstallState:
    state: str
    recommended_path: str
    paths: dict
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    config_layout_state: str = paths.LAYOUT_NONE
    legacy_migration_available: bool = False
    setup_requires_confirmation: bool = False

    def as_dict(self):
        return {
            "state": self.state,
            "recommended_path": self.recommended_path,
            "paths": dict(self.paths),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "config_layout_state": self.config_layout_state,
            "legacy_migration_available": self.legacy_migration_available,
            "setup_requires_confirmation": self.setup_requires_confirmation,
        }


def _resolved_base_dir(base_dir=None):
    """Resolve the real install context and its root once.

    ``detect_install_context`` already applies ``EMS_INSTALL_DIR`` when
    ``base_dir`` is not given, so every ``ems.paths`` lookup below must key off
    the resolved ``install_root`` — never the raw ``base_dir`` — or a container
    Admin server would inspect ``/app`` instead of the mounted install.
    """

    context = detect_install_context(base_dir=base_dir)
    return context, str(context.install_root)


def _admin_marker_path(data_dir):
    return Path(data_dir).joinpath(*ADMIN_MARKER_RELATIVE)


def _config_is_damaged(config_path):
    """A resolved config that exists but is not a JSON object is a damaged layout."""

    try:
        with open(config_path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return True
    return not isinstance(data, dict)


def detect_install_state(base_dir=None):
    """Classify the current install root and recommend the safest Admin path."""

    context, install_root = _resolved_base_dir(base_dir)
    standard = paths.standard_config_path(install_root)
    legacy = paths.legacy_config_path(install_root)
    standard_exists = standard.exists()
    legacy_exists = legacy.exists()
    compose_exists = context.compose_exists
    layout_state = context.config_layout_state
    marker_exists = _admin_marker_path(context.data_dir).is_file()

    reasons = []
    warnings = []

    path_info = {
        "legacy_config": str(legacy),
        "standard_config": str(standard),
        "compose": str(context.compose_path),
        "data": str(context.data_dir),
        "install_root": str(context.install_root),
    }

    if layout_state == paths.LAYOUT_BOTH_DIFFERENT:
        warnings.append(
            "Both config/config.json and a legacy root config.json exist and "
            "differ. The standard config/config.json is active; they are not "
            "merged automatically."
        )

    if standard_exists:
        if _config_is_damaged(standard):
            reasons.append(
                "The standard config/config.json exists but is not a valid JSON "
                "object and needs repair."
            )
            state = STATE_PARTIAL_INSTALL
        elif compose_exists:
            state = (
                STATE_ADMIN_PREPARED_INSTALL if marker_exists else STATE_STANDARD_INSTALL
            )
        else:
            reasons.append(
                "A standard config/config.json exists but docker-compose.yml is "
                "missing."
            )
            state = STATE_STANDARD_CONFIG_ONLY
    elif legacy_exists:
        reasons.append("A legacy root config.json was found.")
        if compose_exists:
            reasons.append(
                "docker-compose.yml is present alongside the legacy config."
            )
        state = STATE_LEGACY_ROOT_CONFIG
    elif compose_exists:
        reasons.append(
            "A docker-compose.yml exists but no config.json was found."
        )
        state = STATE_COMPOSE_ONLY
    else:
        state = STATE_NONE

    recommended_path = PATH_SETUP_NEW if state == STATE_NONE else PATH_MANAGE_EXISTING
    # Only the pure legacy state (no standard config yet) is offered a one-step
    # migration. When both configs exist and differ, the standard config is
    # already active and the divergence is surfaced as a warning for an explicit
    # maintenance action later, not an auto-prompt that would refuse to overwrite.
    legacy_migration_available = state == STATE_LEGACY_ROOT_CONFIG
    setup_requires_confirmation = state != STATE_NONE

    return InstallState(
        state=state,
        recommended_path=recommended_path,
        paths=path_info,
        reasons=reasons,
        warnings=warnings,
        config_layout_state=layout_state,
        legacy_migration_available=legacy_migration_available,
        setup_requires_confirmation=setup_requires_confirmation,
    )


def select_start_path(choice, *, base_dir=None, confirm=False):
    """Resolve a user's Admin choice into a routing decision.

    ``setup_new`` while an install already exists requires an explicit second
    confirmation before any replace/reset behavior. ``manage_existing`` routes to
    maintenance, flagging a legacy-config migration as the first step when one is
    detected.
    """

    if choice not in (PATH_SETUP_NEW, PATH_MANAGE_EXISTING):
        raise ValueError(f"unknown Admin path choice: {choice!r}")

    install_state = detect_install_state(base_dir=base_dir)

    if choice == PATH_SETUP_NEW:
        needs_confirmation = install_state.setup_requires_confirmation and not confirm
        return {
            "ok": not needs_confirmation,
            "choice": choice,
            "route": "setup",
            "state": install_state.state,
            "requires_confirmation": needs_confirmation,
            "reasons": install_state.reasons,
            "warnings": install_state.warnings,
        }

    return {
        "ok": True,
        "choice": choice,
        "route": "maintenance",
        "state": install_state.state,
        "requires_confirmation": False,
        "migrate_legacy_config": install_state.legacy_migration_available,
        "reasons": install_state.reasons,
        "warnings": install_state.warnings,
    }


class LegacyMigrationError(Exception):
    def __init__(self, reason, message, status=409):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


def migrate_legacy_root_config(
    base_dir=None,
    *,
    admin_data_dir=None,
    overwrite=False,
):
    """Migrate a legacy root ``config.json`` to the standard ``config/config.json``.

    Copy-only and non-destructive: validates the source is a JSON object, backs
    up before writing, writes atomically, keeps the legacy source and ``data/``
    intact, and refuses to overwrite an existing standard config unless
    ``overwrite`` is explicitly set.
    """

    _, install_root = _resolved_base_dir(base_dir)
    legacy = paths.legacy_config_path(install_root)
    standard = paths.standard_config_path(install_root)

    if not legacy.is_file():
        raise LegacyMigrationError(
            "legacy_config_missing",
            "No legacy root config.json was found to migrate.",
            status=404,
        )

    raw = legacy.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LegacyMigrationError(
            "invalid_legacy_config",
            f"The legacy root config.json is not valid JSON: {exc}",
            status=422,
        )
    if not isinstance(parsed, dict):
        raise LegacyMigrationError(
            "invalid_legacy_config",
            "The legacy root config.json must contain a JSON object.",
            status=422,
        )

    standard_exists = standard.exists()
    if standard_exists and not overwrite:
        if standard.read_bytes() == raw:
            return {
                "ok": True,
                "migrated": False,
                "reason": "already_migrated",
                "message": "config/config.json already matches the legacy config.",
                "source": str(legacy),
                "target": str(standard),
                "backup_path": None,
            }
        raise LegacyMigrationError(
            "target_exists",
            "config/config.json already exists and differs from the legacy "
            "config. Confirm the overwrite to replace it.",
            status=409,
        )

    backup_dir = _resolve_backup_dir(admin_data_dir, install_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backups = [_write_backup(backup_dir, f"root-config-legacy-{stamp}", raw)]
    if standard_exists:
        backups.append(
            _write_backup(
                backup_dir,
                f"standard-config-before-migrate-{stamp}",
                standard.read_bytes(),
            )
        )

    _atomic_write(standard, raw)

    return {
        "ok": True,
        "migrated": True,
        "source": str(legacy),
        "target": str(standard),
        "backup_path": str(backups[0]),
        "backups": [str(item) for item in backups],
        "migrated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _resolve_backup_dir(admin_data_dir, base_dir):
    if admin_data_dir is not None:
        root = Path(admin_data_dir)
    else:
        root = paths.resolve_data_dir(base_dir=base_dir) / "admin"
    return root / "backups" / "config"


def _write_backup(backup_dir, stem, data):
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate = backup_dir / f"{stem}.json"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / f"{stem}-{counter}.json"
        counter += 1
    _atomic_write(candidate, data)
    return candidate


def _atomic_write(path, payload):
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
