# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
import contextlib
import json
import logging
import os
import copy
import shutil
import hashlib
import tempfile
import threading
import time

from ems import config as cfg
from ems.paths import RUNTIME_STATE_PATH_WITHOUT_SETTING
from ems.file_io import (
    LOCK_TIMEOUT_SECONDS,
    STARTUP_LOCK_TIMEOUT_SECONDS,
    FileLockTimeout,
    atomic_write_bytes,
    exclusive_file_lock,
    file_signature,
    remove_stale_temp_files,
)
from ems.paths import install_root_for_config, resolve_schedules_path
from ems.runtime_devices import clean_identity as _clean_identity
from ems.runtime_devices import config_device_keys, config_entry_named, device_key, device_name, device_seed, owned_runtime_device, parked_values_for, runtime_device_for, serial_candidates, stored_devices
from ems.logging_utils import log_event
from ems.retired_defaults import (
    DEFAULTS_REVISION,
    RUNTIME_REVISION_KEY,
    refresh_retired_defaults,
    stored_revision,
)


def _sanitized_device(state):
    """Copy a loaded device entry, dropping the legacy offgrid_socket key."""

    if not isinstance(state, dict):
        return state
    entry = dict(state)
    entry.pop("offgrid_socket", None)
    return entry


PARKED_DEVICES_KEY = "parked_devices"


def parked_key(name, identity):
    """The key v0.10.8 parked a pruned device's operating values under: its serial, else its name."""

    return f"identity:{identity}" if identity is not None else f"name:{name}"


def _unpark(parked, name, identity, configured=None):
    """The values v0.10.8 parked for a device that has no runtime entry, dropped from ``parked``.

    Found under its name instead of its serial, they belonged to another
    serial (a replaced inverter): they fill only what the device's config
    entry (``configured``) does not hold, so values typed for the new device win.
    """

    if parked_key(name, identity) in parked:
        return parked.pop(parked_key(name, identity))
    if identity is not None and parked_key(name, None) in parked:
        values = parked.pop(parked_key(name, None))
        entry = configured(name) if configured is not None else None
        return {key: value for key, value in values.items() if not (isinstance(entry, dict) and key in entry)}
    return {}


def _saved_device_keys(config_path):
    """config_device_keys of the devices ``config.json`` at ``config_path`` names now; empty where it cannot be read.

    An Admin save that adds a device writes the device's runtime entry before
    the config, holding the runtime-state lock until the config is written; a
    start that read the config before that save keeps the entry by this.
    """

    if not config_path:
        return set()
    try:
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return set()
    devices = data.get("devices") if isinstance(data, dict) else None
    return config_device_keys(devices) if isinstance(devices, list) else set()


def reconcile_runtime_devices(loaded_devices, default_devices, *, prune, parked=None, keep=None, configured=None):
    """Reconcile loaded runtime device state against the configured devices.

    ``default_devices`` holds an entry for every device the EMS can control,
    switched on or off (ems.runtime_devices.stored_devices). Devices are
    matched to their entry by stable identity (serial), falling back to the
    device name for entries written before an identity was stamped. A matched
    entry keeps its operator settings under the (possibly new) config name, so
    renaming a device in config never loses its runtime settings. With
    ``prune``, an unmatched entry is dropped only when no config entry names
    it any more (``keep``, ems.runtime_devices.config_device_keys): a device
    removed from ``config.json``. One read over MQTT without control keeps its
    entry until it controls again.

    ``parked``, the operating values v0.10.8 kept for pruned devices, fills a
    device that gets a new entry here, once; what is left of it belongs to
    devices the config no longer names.

    Returns ``(devices, changes)`` where ``changes`` carries ``added``,
    ``renamed``, ``pruned``, ``rekeyed`` and ``restored`` lists for auditing. When no
    configured devices are known (empty/unreadable config) reconciliation is a
    fail-closed no-op: the loaded devices are kept verbatim and never pruned.
    """

    from ems.runtime_devices import names_a_runtime_entry

    if not isinstance(loaded_devices, dict):
        loaded_devices = {}

    changes = {"added": [], "renamed": [], "pruned": [], "rekeyed": [], "restored": []}

    if not default_devices:
        return {name: _sanitized_device(state) for name, state in loaded_devices.items()}, changes

    merged = {}
    consumed = set()

    for cfg_name, device_defaults in default_devices.items():
        ident = None
        if isinstance(device_defaults, dict):
            ident = _clean_identity(device_defaults.get("identity"))

        match_name = runtime_device_for(loaded_devices, cfg_name, ident)
        if match_name is not None and match_name not in consumed:
            operator_state = dict(loaded_devices[match_name])
            prior_identity = _clean_identity(operator_state.pop("identity", None))
            entry = {**device_defaults, **operator_state}
            if ident is not None:
                entry["identity"] = ident
            else:
                entry.pop("identity", None)
            entry.pop("offgrid_socket", None)
            merged[cfg_name] = entry
            consumed.add(match_name)
            if match_name != cfg_name:
                changes["renamed"].append(
                    {"from": match_name, "to": cfg_name, "identity": ident}
                )
            elif ident is not None and prior_identity != ident:
                changes["rekeyed"].append({"name": cfg_name, "identity": ident})
        else:
            entry = dict(device_defaults) if isinstance(device_defaults, dict) else {}
            entry.pop("offgrid_socket", None)
            merged[cfg_name] = entry
            changes["added"].append({"name": cfg_name, "identity": ident})

    for name, state in loaded_devices.items():
        if name in consumed:
            continue
        if prune and (keep is None or name in merged or not names_a_runtime_entry(keep, name, state)):
            ident = _clean_identity(state.get("identity")) if isinstance(state, dict) else None
            changes["pruned"].append({"name": name, "identity": ident})
        elif name not in merged:
            merged[name] = _sanitized_device(state)

    if parked:
        for added in changes["added"]:
            restored = _unpark(parked, added["name"], added["identity"], configured)
            if restored:
                merged[added["name"]].update(restored)
                changes["restored"].append({**added, "keys": sorted(restored)})
    return merged, changes


def merge_runtime_defaults(data, defaults):
    """Merge runtime data over defaults while preserving unknown keys.

    Non-destructive: orphaned device entries (present on disk but not in the
    configured devices) are kept. Authoritative pruning of removed devices
    happens only in :meth:`RuntimeState.load_or_create`. A device the defaults
    add gets the values v0.10.8 parked for it here too, so a writer that saves
    the merge (emsctl before the EMS restarts) cannot leave it at its seed.
    """

    if not isinstance(data, dict):
        data = {}

    merged = dict(data)

    system = merged.get("system")
    if not isinstance(system, dict):
        system = {}

    merged["system"] = {
        **defaults.get("system", {}),
        **system
    }

    for section_name, default_section in defaults.items():
        if section_name in ("system", "devices") or not isinstance(default_section, dict):
            continue
        section = merged.get(section_name)
        if not isinstance(section, dict):
            section = {}

        merged[section_name] = {
            **default_section,
            **section
        }

    devices = merged.get("devices")
    if not isinstance(devices, dict):
        devices = {}

    parked = merged.get(PARKED_DEVICES_KEY)
    parked = copy.deepcopy(parked) if isinstance(parked, dict) else None
    merged_devices, _changes = reconcile_runtime_devices(
        devices, defaults.get("devices", {}), prune=False, parked=parked
    )
    merged["devices"] = merged_devices
    if parked is not None:
        if parked:
            merged[PARKED_DEVICES_KEY] = parked
        else:
            merged.pop(PARKED_DEVICES_KEY, None)

    return merged


RESET_REQUEST_SUFFIX = ".reset-requested"
SETUP_BACKUP_SUFFIX = ".before-setup"
LAST_GOOD_SUFFIX = ".last-good"
CONTROL_OFF_START_KEY = "started_control_off"
UNREADABLE_SUFFIX = ".unreadable"


def runtime_reset_request_path(path):
    """Where a fresh setup leaves its request beside the runtime state at ``path``."""

    return f"{path}{RESET_REQUEST_SUFFIX}"


def config_digest(payload):
    """The SHA-256 a request names a config's bytes by."""

    return hashlib.sha256(payload).hexdigest()


def request_runtime_reset(path, config_sha256):
    """Ask the next EMS start to build the runtime state at ``path`` afresh.

    Only a request: an EMS still running on the replaced config would write a
    state set aside now straight back from memory, so the EMS that starts on
    the new config does the reset (:func:`start_runtime_state`). The request
    names the config it was left for by its SHA-256, so it holds only while
    that very config is installed.
    """

    marker = runtime_reset_request_path(path)
    parent = os.path.dirname(marker) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f"{os.path.basename(marker)}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"reason": "fresh_setup", "config_sha256": config_sha256}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, marker)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def pending_runtime_reset(path):
    """The config SHA-256 a pending request beside ``path`` names, or ``None``."""

    try:
        with open(runtime_reset_request_path(path), encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError):
        return None
    digest = request.get("config_sha256") if isinstance(request, dict) else None
    return digest if isinstance(digest, str) and digest else None


def rebind_runtime_reset(path, from_sha256, to_sha256):
    """Move a pending request from the config it names to the edit replacing it.

    An edit of the set-up config keeps the setup's reset; a request naming any
    other config is left as it is, void.
    """

    if from_sha256 and pending_runtime_reset(path) == from_sha256:
        request_runtime_reset(path, to_sha256)
        return True
    return False


def runtime_state_path_under(install_root, config):
    """The runtime-state file the EMS reads for ``config`` under ``install_root``."""

    system = config.get("system") if isinstance(config, dict) else None
    system = system if isinstance(system, dict) else {}
    relative = system.get("runtime_state_path") or RUNTIME_STATE_PATH_WITHOUT_SETTING
    if os.path.isabs(relative):
        return relative
    return os.path.join(str(install_root), relative)


def runtime_state_path_for_config(config_path, config):
    """The runtime-state file the EMS reads for ``config`` installed at ``config_path``."""

    return runtime_state_path_under(install_root_for_config(config_path), config)


def carry_runtime_reset(config_path, config, replaced_digest, written_digest):
    """Move a pending request from the config replaced to the edit that replaces it.

    Every edit of an installed config goes through this, so a setup's reset
    survives it; a restore never does, and a request naming the config it
    replaced is then dropped at the next start.
    """

    return rebind_runtime_reset(
        runtime_state_path_for_config(config_path, config), replaced_digest, written_digest
    )


def cancel_runtime_reset(path):
    """Remove a pending request beside the runtime state at ``path``, if any."""

    try:
        os.remove(runtime_reset_request_path(path))
    except FileNotFoundError:
        pass


def _setup_backup_path(path):
    return _stamped_path(path, SETUP_BACKUP_SUFFIX)


def _stamped_path(path, suffix):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    candidate = f"{path}{suffix}-{stamp}"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{path}{suffix}-{stamp}-{counter}"
        counter += 1
    return candidate


def last_good_runtime_state_path(path):
    """The copy of the runtime state its last save or start-up read left beside it."""

    return f"{path}{LAST_GOOD_SUFFIX}"


def read_last_good_runtime_state(path, *, log=True):
    """The last good copy of the runtime state at ``path``, or ``None`` where there is no usable one; ``log`` names an unusable one."""

    copy_path = last_good_runtime_state_path(path)
    try:
        return _read_usable(copy_path)[0]
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        if log:
            log_event(logging.WARNING, "runtime_state_last_good_unusable", path=copy_path, error=e)
        return None


def _released_control_off_start(state):
    """Drop the control-off start's mark from ``state`` once control in it reads on; whether it did.

    Switched on by any means -- the dashboard, a schedule, a hand edit, a
    restored backup -- the state is the one that runs.
    """

    if not isinstance(state, dict) or not state.get(CONTROL_OFF_START_KEY):
        return False
    system = state.get("system") if isinstance(state.get("system"), dict) else {}
    if not cfg.safe_bool(system.get("enabled"), False):
        return False
    del state[CONTROL_OFF_START_KEY]
    return True


def _read_usable(path):
    """The runtime state at ``path`` and its signature; ValueError where it is no JSON object."""

    loaded, signature = RuntimeState.read_file(path)
    if not isinstance(loaded, dict):
        raise ValueError("runtime state is not a JSON object")
    return loaded, signature


def runtime_reset_pending(path, config_path):
    """Whether a fresh setup's request beside ``path`` still names the config at ``config_path``.

    The next EMS start then sets the runtime state aside and builds it from
    that config, so a value written into the runtime state before it is lost.
    """

    wanted = pending_runtime_reset(path)
    return wanted is not None and wanted == _config_file_digest(config_path)


def _config_file_digest(path):
    try:
        with open(path, "rb") as handle:
            return config_digest(handle.read())
    except OSError:
        return None


PLACEHOLDER_SEEDED_KEY = "seeded_from_placeholders"


def start_runtime_state(path, defaults, config_path, defaults_revision=None, placeholder_start=False):
    """The runtime state a starting EMS works with.

    A fresh setup's request is settled first. While the config at
    ``config_path`` is still the one it names, the existing file is kept as
    ``<path>.before-setup-<UTC time>`` and set aside, so the state is built
    from that config, and the schedule rules beside it are set aside the same
    way; a request naming any other config -- a restored backup,
    a write that failed -- is dropped. Only an EMS start calls this; anything
    else that opens the file leaves the request for it. Both files are set
    aside under their locks, so an emsctl edit in flight cannot write the old
    state back over the reset; a lock held past thirty seconds is logged as
    runtime_state_lock_busy and the reset goes on without it.
    """

    for stale_path in (path, last_good_runtime_state_path(path), str(resolve_schedules_path(path))):
        removed = remove_stale_temp_files(stale_path)
        if removed:
            log_event(logging.INFO, "stale_temp_files_removed", path=stale_path, files=",".join(removed))
    state = RuntimeState(path, defaults, defaults_revision)
    state.config_path = config_path
    with state._file_lock(strict=False):
        reset = _settle_runtime_reset(path, config_path)
        state.load_or_create(fresh=reset)
        _settle_placeholder_seed(state, not (state.created or state.started_control_off), placeholder_start)
    return state


def _settle_placeholder_seed(state, existed, placeholder_start):
    """A runtime state first written from a config that still held template placeholders is no operator's.

    Such a start forces control off and dry-run (ems.config.apply_template_placeholder_safety),
    so the file it creates would keep control off once the operator has
    configured the installation and restarted, and the switch-over would then
    drop the values they set. It is marked, and the first start without
    placeholders builds it afresh from the config.
    """

    if placeholder_start:
        if not existed:
            state.data[PLACEHOLDER_SEEDED_KEY] = True
            _save_seed(state)
        return
    if state.data.get(PLACEHOLDER_SEEDED_KEY):
        state.data = state._fresh_data()
        _save_seed(state)
        log_event(logging.INFO, "runtime_state_reseeded", path=state.path, reason="first start without template placeholders")


def _save_seed(state):
    """Save the placeholder seed's change, unless an unreadable file was left in place; a failed write keeps it in memory."""

    if state.left_unreadable:
        return
    try:
        state.save_atomic()
    except OSError as e:
        log_event(logging.WARNING, "runtime_state_save_failed", path=state.path, error=e)


def _settle_runtime_reset(path, config_path):
    """Settle a fresh setup's request; whether the state is to be built afresh from the config."""

    marker = runtime_reset_request_path(path)
    reset = False
    if os.path.exists(marker):
        wanted = pending_runtime_reset(path)
        if wanted is not None and wanted == _config_file_digest(config_path):
            reset = True
            backup = _setup_backup_path(path)
            moved = os.path.exists(path)
            if moved:
                os.replace(path, backup)
            try:
                os.remove(last_good_runtime_state_path(path))
            except FileNotFoundError:
                pass
            except OSError as e:
                log_event(logging.WARNING, "runtime_state_last_good_failed", path=path, error=e)
            schedules = str(resolve_schedules_path(path))
            schedules_backup = None
            with contextlib.ExitStack() as stack:
                try:
                    stack.enter_context(exclusive_file_lock(f"{schedules}.lock", STARTUP_LOCK_TIMEOUT_SECONDS))
                except FileLockTimeout:
                    log_event(logging.WARNING, "runtime_state_lock_busy", path=schedules)
                if os.path.exists(schedules):
                    schedules_backup = _setup_backup_path(schedules)
                    os.replace(schedules, schedules_backup)
            log_event(
                logging.INFO,
                "runtime_state_reset_by_setup",
                path=path,
                backup=backup if moved else None,
                schedules_backup=schedules_backup,
            )
        else:
            log_event(
                logging.INFO,
                "runtime_reset_request_dropped",
                path=path,
                reason="config_replaced",
            )
        os.remove(marker)
    return reset


class RuntimeState:
    """Persist mutable operator state outside static config.

    ``defaults_revision`` is the defaults revision the config records, and a
    new file records it. A live EMS start also lets loading move values still
    at a default that revision retired to ``defaults``, once
    (``refresh_defaults``); emsctl only records it in a file it creates.
    """

    def __init__(self, path, defaults, defaults_revision=None, *, refresh_defaults=True):
        self.path = path
        self.defaults = defaults
        self.defaults_revision = defaults_revision
        self.refresh_defaults = refresh_defaults
        self.data = self._fresh_data()
        self.load_failed = False
        self.created = False
        self.trusted = False
        self.holds_state = False
        self.left_unreadable = False
        self.started_control_off = False
        self.last_signature = None
        self.config_path = None
        self.lock = threading.RLock()
        self._held = threading.local()

    @contextlib.contextmanager
    def _file_lock(self, *, strict, timeout=None):
        """The cross-process lock, once per thread however deeply nested.

        The flock is waited for before the in-process lock is taken, so the
        control loop's readers never queue behind a writer that waits for
        another process. ``strict`` refuses a change when the lock stays taken
        (ValueError, which every runtime writer reports); otherwise the write
        waits ``timeout``, at start-up thirty seconds, and then goes on, so a
        stalled emsctl cannot keep the EMS from starting.
        """

        if getattr(self._held, "depth", 0):
            self._held.depth += 1
            try:
                with self.lock:
                    yield
            finally:
                self._held.depth -= 1
            return
        if timeout is None:
            timeout = LOCK_TIMEOUT_SECONDS if strict else STARTUP_LOCK_TIMEOUT_SECONDS
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(exclusive_file_lock(f"{self.path}.lock", timeout))
            except FileLockTimeout as exc:
                if strict:
                    raise ValueError(
                        f"runtime state {self.path} is busy: another process holds its lock"
                    ) from exc
                log_event(logging.WARNING, "runtime_state_lock_busy", path=self.path)
            self._held.depth = 1
            try:
                with self.lock:
                    yield
            finally:
                self._held.depth = 0

    def load_or_create(self, *, fresh=False):
        """Load the file, put a missing one back from the last good copy, or create it.

        Only where neither the file nor a copy exists, and this instance holds
        no state it read or ran, is the state new: a first start, or ``fresh``,
        a fresh setup's reset, whose first save also replaces a copy it could
        not remove. A file removed while an instance runs comes back as the
        state it runs (_write_back), which may be newer than the copy; at a
        start it comes back from its copy. One whose copy cannot be used, or
        that went missing while control was already off for want of one,
        starts with control off.
        """

        with self._file_lock(strict=False):
            if not os.path.exists(self.path):
                if not fresh and self.holds_state:
                    return self._write_back("missing")
                if not fresh and self._restore_last_good("missing"):
                    return self._reconcile_on_load(restored=True)
                if not fresh and self._run_on_copy("missing"):
                    return self.data
                if not fresh and (self.data.get(CONTROL_OFF_START_KEY) or os.path.lexists(last_good_runtime_state_path(self.path))):
                    return self._start_control_off(None)
                self.data = self._fresh_data()
                self.created = True
                self.trusted = True
                self.holds_state = True
                log_event(
                    logging.INFO,
                    "runtime_state_created",
                    path=self.path
                )
                self.save_atomic()
                return self.data

            return self._reconcile_on_load()

    def _reconcile_on_load(self, *, restored=False):
        """Load the file and reconcile device entries against config (EMS-owned).

        The EMS is authoritative for runtime device state: on load it migrates
        renamed devices by identity, prunes devices removed from config, and
        backfills identities. Destructive changes (rename/prune) first back up
        the file and are audit-logged. Any device-level change is persisted so
        the file converges to a clean state.
        """

        with self._file_lock(strict=False):
            return self._reconcile_locked(restored=restored)

    @staticmethod
    def read_file(path):
        """The file's JSON and its :func:`file_signature`, from one open."""

        with open(path) as f:
            signature = file_signature(os.fstat(f.fileno()))
            return json.load(f), signature

    def adopt(self, loaded, signature):
        """Take ``loaded``, read at ``signature``, as the state in memory."""

        with self.lock:
            self.data = merge_runtime_defaults(loaded, self.defaults)
            self.last_signature = signature
            self.trusted = True
            self.holds_state = True

    def _reconcile_locked(self, *, restored=False):
        if not os.path.exists(self.path):
            return self.load_or_create()

        try:
            loaded, signature = _read_usable(self.path)
        except FileNotFoundError:
            return self.load_or_create()
        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_load_error",
                path=self.path,
                error=e
            )
            return self._recover_unreadable(try_copy=not restored)

        self.load_failed = False
        self.trusted = True
        self.holds_state = True
        merged = merge_runtime_defaults(loaded, self.defaults)
        loaded_devices = loaded.get("devices") if isinstance(loaded, dict) else {}
        loaded_parked = loaded.get(PARKED_DEVICES_KEY) if isinstance(loaded, dict) else None
        parked = copy.deepcopy(loaded_parked) if isinstance(loaded_parked, dict) else {}
        default_devices = self.defaults.get("devices", {})
        reconciled_devices, changes = reconcile_runtime_devices(
            loaded_devices if isinstance(loaded_devices, dict) else {},
            default_devices,
            prune=True,
            parked=parked,
            keep=config_device_keys(cfg.ZENDURE_CONFIG) | _saved_device_keys(self.config_path),
            configured=lambda name: config_entry_named(cfg.ZENDURE_CONFIG, name),
        )
        merged["devices"] = reconciled_devices
        parked_changed = bool(default_devices) and PARKED_DEVICES_KEY in merged
        if parked_changed:
            self._keep_parked_for_unstored(reconciled_devices, parked, changes)
            merged.pop(PARKED_DEVICES_KEY)

        merged, refreshed = self._refresh_retired_defaults(merged)
        released = _released_control_off_start(merged)
        destructive = bool(changes["renamed"] or changes["pruned"])
        changed = destructive or refreshed or parked_changed or released or bool(changes["added"] or changes["rekeyed"])

        if destructive:
            self._backup_runtime_state()

        for entry in changes["renamed"]:
            log_event(
                logging.INFO,
                "runtime_device_renamed",
                path=self.path,
                device_from=entry["from"],
                device_to=entry["to"],
                identity=entry.get("identity"),
            )
        for entry in changes["pruned"]:
            log_event(
                logging.INFO,
                "runtime_device_pruned",
                path=self.path,
                device=entry["name"],
                identity=entry.get("identity"),
            )
        for entry in changes["restored"]:
            log_event(
                logging.INFO,
                "runtime_device_values_restored",
                path=self.path,
                device=entry["name"],
                identity=entry.get("identity"),
                keys=",".join(entry["keys"]),
            )
        for entry in changes["added"]:
            log_event(
                logging.INFO,
                "runtime_device_added",
                path=self.path,
                device=entry["name"],
                identity=entry.get("identity"),
            )

        self.data = merged
        self.last_signature = signature

        log_event(
            logging.INFO,
            "runtime_state_loaded",
            path=self.path
        )

        if changed:
            self.save_atomic()
        else:
            self._keep_last_good(self._serialized(loaded), loaded)

        return self.data

    def _recover_unreadable(self, *, try_copy=True):
        """The state for a file that exists but cannot be read as one.

        The file is kept as ``<path>.unreadable-<UTC time>`` for the operator,
        linked there before anything replaces it, and the last good copy takes
        its place. Without a usable copy the EMS starts on its defaults with
        control off (_start_control_off). A file that cannot be kept aside is
        left where it is at this start: the EMS runs on the copy in memory, or
        with control off without one, and its next save replaces the file.
        """

        aside = _stamped_path(self.path, UNREADABLE_SUFFIX)
        try:
            os.link(self.path, aside)
        except OSError:
            try:
                shutil.copy2(self.path, aside)
            except OSError as e:
                log_event(logging.WARNING, "runtime_state_set_aside_failed", path=self.path, error=e)
                aside = None
                self.left_unreadable = True
        if try_copy and aside is not None and self._restore_last_good("unreadable", unreadable=aside):
            return self._reconcile_locked(restored=True)
        if try_copy and self._run_on_copy("unreadable"):
            return self.data
        return self._start_control_off(aside, replace=aside is not None)

    def _run_on_copy(self, reason):
        """Run on a usable copy in memory where it cannot be put in place of the file; whether there was one.

        Nothing is written at this start (``load_failed``); the next save puts
        the state where the file is.
        """

        loaded = read_last_good_runtime_state(self.path, log=False)
        if loaded is None:
            return False
        self.data = merge_runtime_defaults(loaded, self.defaults)
        self.load_failed = True
        self.trusted = True
        self.holds_state = True
        log_event(logging.WARNING, "runtime_state_restored", path=self.path, source="last_good", reason=reason, unreadable=None)
        return True

    def _write_back(self, reason):
        """Put the state this instance holds, read or run by it, where the file went missing.

        It is no first start and no copy to fall back on is needed: the state
        that runs is known. A write that fails keeps it in memory, as a failed
        save does.
        """

        try:
            self.save_atomic()
        except OSError as e:
            log_event(logging.WARNING, "runtime_state_save_failed", path=self.path, error=e)
            return self.data
        log_event(logging.WARNING, "runtime_state_restored", path=self.path, source="memory", reason=reason)
        return self.data

    def _start_control_off(self, unreadable, *, replace=True):
        """The built-in defaults with control off, for a state neither the file nor its copy can give.

        Written to the file so the dashboard and ``emsctl status`` show what
        runs, unless ``replace`` is false (an unreadable file not kept aside).
        It carries ``CONTROL_OFF_START_KEY`` until a save or a read finds
        control on in it (_released_control_off_start): while a copy exists,
        no state before that becomes the copy, which keeps what an operator
        saved.
        """

        self.data = self._fresh_data()
        self.data.setdefault("system", {})["enabled"] = False
        self.data[CONTROL_OFF_START_KEY] = True
        self.load_failed = True
        self.trusted = True
        self.started_control_off = True
        log_event(
            logging.WARNING,
            "runtime_state_unusable",
            path=self.path,
            unreadable=unreadable,
            control="off",
        )
        if replace:
            try:
                self.save_atomic()
            except OSError as e:
                log_event(logging.WARNING, "runtime_state_save_failed", path=self.path, error=e)
        return self.data

    def _restore_last_good(self, reason, *, unreadable=None):
        """Put the last good copy in place of the file; whether it was usable."""

        loaded = read_last_good_runtime_state(self.path)
        if loaded is None:
            return False
        try:
            atomic_write_bytes(self.path, self._serialized(loaded))
        except OSError as e:
            log_event(logging.WARNING, "runtime_state_restore_failed", path=self.path, error=e)
            return False
        log_event(
            logging.WARNING,
            "runtime_state_restored",
            path=self.path,
            source="last_good",
            reason=reason,
            unreadable=unreadable,
        )
        return True

    @staticmethod
    def _serialized(data):
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _keep_last_good(self, raw, state):
        """Write ``raw``, the ``state`` just saved or read, as the last good copy; a failure only warns.

        Not a state the control-off start made (CONTROL_OFF_START_KEY, which a
        state reading control on no longer carries) while a copy exists: it is
        no operator's, and the copy may still hold what one saved. Without any
        copy there is nothing to keep, and it becomes the copy, so a file lost
        later comes back with control off.
        """

        copy_path = last_good_runtime_state_path(self.path)
        if isinstance(state, dict) and state.get(CONTROL_OFF_START_KEY) and os.path.lexists(copy_path):
            return
        try:
            atomic_write_bytes(copy_path, raw)
        except OSError as e:
            log_event(logging.WARNING, "runtime_state_last_good_failed", path=self.path, error=e)

    @staticmethod
    def _keep_parked_for_unstored(devices, parked, changes):
        """Give a device still in the config that gets no entry of its own the values v0.10.8 parked for it, as its entry.

        Read over MQTT without control, or with an ``sn`` and ``serial_number``
        that disagree, it keeps such an entry until it leaves ``config.json``
        (config_device_keys); what is left belongs to removed devices.
        """

        entries = cfg.ZENDURE_CONFIG if isinstance(cfg.ZENDURE_CONFIG, list) else []
        for entry in entries:
            name = device_name(entry)
            if not parked or not name or name in devices or (device_key(entry) is None and not serial_candidates(entry)):
                continue
            values, serial = parked_values_for(parked, entry)
            if serial is None and serial_candidates(entry):
                values = {key: value for key, value in values.items() if key not in entry}
                serial = serial_candidates(entry)[0]
            if values:
                devices[name] = {**values, **({"identity": serial} if serial else {})}
                changes["restored"].append({"name": name, "identity": serial, "keys": sorted(values)})

    def _fresh_data(self):
        data = merge_runtime_defaults({}, self.defaults)
        if self.defaults_revision is not None:
            data[RUNTIME_REVISION_KEY] = min(self.defaults_revision, DEFAULTS_REVISION)
        return data

    def _refresh_retired_defaults(self, merged):
        """``merged`` with its untouched old defaults moved to this config's.

        Only up to the config's revision, and no further than this release
        knows: a config that has not been refreshed still holds the old default
        itself, so the file keeps it too, and is refreshed once the config is.
        Returns the state and whether it changed.
        """

        if self.defaults_revision is None or not self.refresh_defaults:
            return merged, False
        target = min(self.defaults_revision, DEFAULTS_REVISION)
        stored = stored_revision(merged, RUNTIME_REVISION_KEY)
        if stored >= target:
            return merged, False
        refreshed, changes = refresh_retired_defaults(merged, self.defaults, stored, target)
        refreshed[RUNTIME_REVISION_KEY] = target
        if changes:
            self._backup_runtime_state()
        for change in changes:
            log_event(
                logging.INFO,
                "runtime_default_refreshed",
                path=self.path,
                key=change["path"],
                old_default=json.dumps(change["from"]),
                default=json.dumps(change["to"]),
            )
        return refreshed, True

    def _backup_runtime_state(self):
        """Best-effort one-step backup of the current file before pruning."""

        try:
            shutil.copy2(self.path, f"{self.path}.bak")
            log_event(
                logging.INFO,
                "runtime_state_backup_created",
                path=f"{self.path}.bak"
            )
        except OSError as e:
            log_event(
                logging.WARNING,
                "runtime_state_backup_failed",
                path=self.path,
                error=e
            )

    def load_if_changed(self, force=False):
        """Re-read the file when it changed. A missing file is put back (load_or_create), and
        that waits for the cross-process lock outside ``self.lock``, since a
        writer holding the flock takes ``self.lock`` next, and no longer than
        an edit would: the control loop calls this every cycle."""

        with self.lock:
            try:
                signature = file_signature(os.stat(self.path))
            except FileNotFoundError:
                signature = None
            if signature is not None:
                return self._load_changed(signature, force)
        with self._file_lock(strict=False, timeout=LOCK_TIMEOUT_SECONDS):
            return self.load_or_create()

    def _load_changed(self, signature, force):
        """Re-read when the file's signature changed, or always with ``force``;
        only a new signature is logged, so a writer's forced re-read of a file
        nobody else changed says nothing."""

        with self.lock:
            if not force and self.last_signature == signature:
                return self.data

            try:
                loaded, signature = _read_usable(self.path)
                new = signature != self.last_signature

                if new and self.last_signature is not None:
                    log_event(
                        logging.INFO,
                        "runtime_state_changed",
                        path=self.path
                    )

                self.data = merge_runtime_defaults(loaded, self.defaults)
                _released_control_off_start(self.data)
                self.last_signature = signature
                self.trusted = True
                self.holds_state = True

                if new:
                    log_event(
                        logging.INFO,
                        "runtime_state_loaded",
                        path=self.path
                    )

            except Exception as e:
                log_event(
                    logging.WARNING,
                    "runtime_state_load_error",
                    path=self.path,
                    error=e
                )

            return self.data

    def _refresh_from_disk(self, *, force=False):
        """Pick up another process's write before changing anything.

        A change saved at once re-reads unconditionally, because two writes
        within one tick of the filesystem's coarse clock carry the same mtime.
        ``set_device`` keeps its unsaved changes and re-reads only on a new
        mtime.
        """

        if self.path and os.path.exists(self.path):
            self.load_if_changed(force=force)

    def save_atomic(self):
        """Write the state, and as the last good copy (_keep_last_good).

        An instance that never read the file it would replace (it exists, but
        could not be read) refuses with ValueError: what it holds is no
        operator's state, and saving it would also overwrite the copy that
        is to rescue exactly that file.
        """

        with self._file_lock(strict=False):
            if not self.trusted and os.path.exists(self.path):
                raise ValueError(f"runtime state {self.path} could not be read; it is not overwritten")
            _released_control_off_start(self.data)
            raw = self._serialized(self.data)
            atomic_write_bytes(self.path, raw)
            self.last_signature = file_signature(os.stat(self.path))
            self.trusted = True
            self._keep_last_good(raw, self.data)

            log_event(
                logging.INFO,
                "runtime_state_saved",
                path=self.path
            )

    def save_unless_changed(self):
        """save_atomic, refused with OSError where the file changed since this instance read or wrote it.

        The start's move of operating values persists the state the start
        built; a runtime writer that wrote in between, such as the Admin
        creating an added device's entry, is not overwritten with it.
        """

        with self._file_lock(strict=False):
            try:
                current = file_signature(os.stat(self.path))
            except FileNotFoundError:
                current = None
            if current != self.last_signature:
                raise OSError(f"{self.path} changed since the EMS read it")
            self.save_atomic()

    @contextlib.contextmanager
    def holding_lock(self):
        """The cross-process lock, strict, for a writer that must finish another write before any other writer sees this one."""

        with self._file_lock(strict=True):
            yield

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def update_section(self, section_name, values):
        with self._file_lock(strict=True):
            self._refresh_from_disk(force=True)
            section = self.data.setdefault(section_name, {})
            if not isinstance(section, dict):
                raise ValueError(f"runtime section {section_name} must be an object")
            section.update(values)
            result = copy.deepcopy(section)
            self.save_atomic()
            return result

    def update_device(self, device_name, values):
        with self._file_lock(strict=True):
            self._refresh_from_disk(force=True)
            return self._write_device(device_name, values)

    def update_configured_device(self, name, identity, values):
        """Update the entry a device as config names it owns; returns its name and new state.

        The entry is found under the file lock, by the rule the EMS starts
        with (owned_runtime_device), so an EMS start that re-keys the entries
        between a caller's read and this write cannot turn it onto another
        device.
        """

        with self._file_lock(strict=True):
            self._refresh_from_disk(force=True)
            devices = self.data.setdefault("devices", {})
            entry_name = owned_runtime_device(devices, name, identity) if isinstance(devices, dict) else None
            if entry_name is None:
                known = ", ".join(sorted(devices)) if isinstance(devices, dict) and devices else "(none)"
                raise KeyError(f"no runtime entry for device {name}; known devices: {known}")
            return entry_name, self._write_device(entry_name, values)

    def create_configured_device(self, name, identity, values):
        """Give a device the config is about to name its runtime entry ``values``; return the entry's name, or None.

        Under the file lock, by the rule the EMS starts with: an entry the
        device owns by its serial (owned_runtime_device) takes the values in
        place; one it owns by its name alone is left untouched and answers
        None. A name an entry of another device holds is never taken over
        (ValueError): until its next start the running EMS still drives that
        device under that name, and reads and saves that entry as its own.
        """

        with self._file_lock(strict=True):
            self._refresh_from_disk(force=True)
            devices = self.data.setdefault("devices", {})
            if not isinstance(devices, dict):
                raise ValueError("runtime devices must be an object")
            entry_name = owned_runtime_device(devices, name, identity)
            if entry_name is not None and _clean_identity((devices.get(entry_name) or {}).get("identity")) is None:
                return None
            if entry_name is None:
                if name in devices:
                    raise ValueError(f"runtime entry {name} belongs to another device")
                entry_name = name
            devices[entry_name] = copy.deepcopy(values)
            self.save_atomic()
            return entry_name

    def _write_device(self, device_name, values):
        devices = self.data.setdefault("devices", {})
        if device_name not in devices:
            known = ", ".join(sorted(devices)) or "(none)"
            raise KeyError(f"unknown device {device_name}; known devices: {known}")
        device = devices[device_name]
        if not isinstance(device, dict):
            raise ValueError(f"device {device_name} runtime state must be an object")
        device.update(values)
        result = copy.deepcopy(device)
        self.save_atomic()
        return result

    def get_system(self, key, default=None):
        with self.lock:
            system = self.data.get("system", {})
            if not isinstance(system, dict):
                return default

            return system.get(key, default)

    def get_device(self, device_name, key, default=None):
        with self.lock:
            devices = self.data.get("devices", {})
            device = devices.get(device_name, {})

            if not isinstance(device, dict):
                return default

            return device.get(key, default)

    def set_device(self, device_name, key, value):
        with self.lock:
            self._refresh_from_disk()
            devices = self.data.setdefault("devices", {})
            device = devices.setdefault(device_name, {})
            previous = device.get(key)
            device[key] = value
            return previous != value


def build_runtime_defaults(devices, config_devices=None):
    """Build runtime defaults from current device configuration.

    One entry for each built device in ``devices``, and with
    ``config_devices``, the config's device list, one seeded from its entry
    for each device the EMS can control but does not build: switched off,
    its values wait in its own entry (ems.runtime_devices.stored_devices).
    """

    device_defaults = {}

    for dev in devices:
        entry = {
            "enabled": True,
            "max_power": cfg.safe_int(
                getattr(dev, "max_power", cfg.MAX_DEVICE_POWER),
                cfg.MAX_DEVICE_POWER,
                minimum=0
            ),
            "offgrid_socket_mode": cfg.offgrid_socket_mode_for(
                getattr(dev, "grid_off_mode", None)
            ),
            "pv_priority_factor": cfg.safe_float(
                getattr(dev, "pv_priority_factor", 1.0),
                1.0,
                minimum=0.01
            ),
            "ac_charge_enabled": bool(getattr(dev, "ac_charge_enabled", True)),
            "ac_discharge_enabled": bool(getattr(dev, "ac_discharge_enabled", True)),
        }
        identity = _clean_identity(str(getattr(dev, "sn", "") or ""))
        if identity is not None:
            entry["identity"] = identity
        device_defaults[dev.name] = entry

    if config_devices is not None:
        identities = {entry.get("identity") for entry in device_defaults.values()} - {None}
        for name, serial, config_entry in stored_devices(config_devices):
            if name in device_defaults or (serial is not None and serial in identities):
                continue
            device_defaults[name] = device_seed(config_entry, cfg.MAX_DEVICE_POWER)

    return {
        "system": {
            "enabled": cfg.SYSTEM_ENABLED,
            "max_total_power": cfg.MAX_TOTAL_POWER,
            "loop_interval": cfg.LOOP_INTERVAL,
            "min_output_limit": cfg.MIN_OUTPUT_LIMIT,
            "grid_target_discharge_w": cfg.GRID_TARGET_DISCHARGE_W,
            "grid_target_charge_w": cfg.GRID_TARGET_CHARGE_W,
        },
        "winter": {
            "enabled": cfg.winter_config_bool("enabled", False)
        },
        "ac_charge_control": {
            "enabled": cfg.ac_charge_control_enabled()
        },
        "battery_care": dict(cfg.BATTERY_CARE_CONFIG),
        "devices": device_defaults
    }
