# SPDX-License-Identifier: AGPL-3.0-or-later
"""One interpretation of a config change, shared by every Admin workflow.

Guided Setup builds a system and Maintenance conservatively edits one. Those
are genuinely different policies — what may be created, what may be removed,
which representation an existing config keeps. What a *field* means is not a
policy: ``grid_meter.mqtt.port`` is one catalog field, and the same answer to it
has to produce the same stored value no matter which screen asked.

So the workflows keep their policies and hand the interpretation here:

    base config + typed changes + explicit policy
        -> canonical normalized config
        -> issues
        -> deterministic mutation record

The rules this module owns, once:

``set``/``clear``/``keep``
    A change carries an intent, not just a value. An emptied field clears its
    key rather than storing ``""`` — EMS Core reads ``max_age_seconds`` as a
    number, and an empty string is not a smaller value but an unparsable one.
    Secrets invert that default: a blank credential box means "not retyped",
    so it keeps the stored secret and only an explicit ``clear`` removes it.

catalog coercion
    Types come from :mod:`ems.config_catalog`. Text is stripped, lists accept
    the comma-separated form a browser field produces, numbers keep integral
    values integral.

grid-meter normalization
    One variant switch, one incompatible-key cleanup, one nested-vs-flat
    representation decision, one credential intent.

Core owns config semantics, so this module lives in :mod:`ems` and imports
nothing from ``admin``. It is import-side-effect-free.
"""

import copy
from dataclasses import dataclass, field as dataclass_field

from ems.config import MQTT_GRID_METER_TYPES, grid_meter_mqtt_settings
from ems.config_catalog import (
    GRID_METER_KNOWN_MQTT_KEYS,
    GRID_METER_KNOWN_TOP_KEYS,
    config_field_index,
    grid_meter_variant_field_spec,
    is_secret_catalog_field,
)

# Bumped whenever the canonical interpretation of a change moves. Preview
# authority folds it in, so a preview issued under older semantics can never be
# applied by a process that would now mutate differently.
CONFIG_MUTATION_CONTRACT_VERSION = 1

SET = "set"
CLEAR = "clear"
KEEP = "keep"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

WORKFLOW_SETUP = "setup"
WORKFLOW_MAINTENANCE = "maintenance"

GRID_METER_PREFIX = "grid_meter."
# ``grid_meter.port`` is a shared HTTP key the catalog never declares as a field
# (it is contributed by the variant spec, not by a form row), yet both flows
# accept an edited port. Declaring its type here keeps it inside the one
# coercion rule instead of an ad-hoc numeric helper per flow.
_UNDECLARED_GRID_METER_FIELDS = {"port": {"type": "number"}}
# Names a broker profile rather than carrying a connection value, so the
# catalog does not list it as a meter field, but the editors do write it.
_GRID_METER_MQTT_EXTRA_KEYS = ("broker_ref",)

_MISSING = object()


@dataclass(frozen=True)
class ConfigChange:
    """One typed edit of one config path.

    ``path`` is a dotted catalog path (``winter.enabled``) for whole-config
    mutation, or a path relative to the block for a scoped mutation such as the
    grid meter (``mqtt.topic``).
    """

    path: str
    value: object = None
    operation: str = SET


@dataclass(frozen=True)
class CredentialIntent:
    """What to do with a stored secret the browser never receives back."""

    operation: str = KEEP
    value: object = None

    @classmethod
    def from_draft(cls, values, key="password", clear_key=None):
        """Read the keep/replace/clear intent out of a draft fragment."""

        if not isinstance(values, dict):
            return cls(KEEP)
        if clear_key is None:
            clear_key = f"clear_{key}"
        if values.get(clear_key):
            return cls(CLEAR)
        value = values.get(key)
        if isinstance(value, str) and value:
            return cls(SET, value)
        return cls(KEEP)


@dataclass(frozen=True)
class MutationPolicy:
    """The workflow-specific half of a mutation.

    ``preserve_legacy_representations`` is the Maintenance promise that an
    existing Core-supported shape (a legacy flat MQTT grid meter) is edited
    where it lives instead of being migrated by the act of previewing it.
    Setup generates a new config and always writes the canonical shape.
    """

    workflow: str
    scope: str
    allow_secret: bool = True
    preserve_unknown_keys: bool = True
    preserve_legacy_representations: bool = False
    allow_create: bool = True
    allow_remove: bool = True


SETUP_POLICY = MutationPolicy(
    workflow=WORKFLOW_SETUP,
    scope="setup",
    allow_secret=True,
    preserve_legacy_representations=False,
)

MAINTENANCE_POLICY = MutationPolicy(
    workflow=WORKFLOW_MAINTENANCE,
    scope="maintenance",
    allow_secret=False,
    preserve_legacy_representations=True,
)


@dataclass(frozen=True)
class MutationIssue:
    code: str
    severity: str
    message: str
    path: str = ""

    def as_dict(self):
        payload = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.path:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class AppliedChange:
    path: str
    operation: str


@dataclass(frozen=True)
class ConfigMutationResult:
    config: dict
    applied_changes: tuple = ()
    issues: tuple = ()
    diff: dict = dataclass_field(default_factory=dict)

    @property
    def applied_paths(self):
        return tuple(change.path for change in self.applied_changes)

    @property
    def errors(self):
        return tuple(issue for issue in self.issues if issue.severity == SEVERITY_ERROR)


def coerce_catalog_value(field, value):
    """Coerce a browser-supplied value by its catalog field type."""

    if value is None:
        return None
    field_type = (field or {}).get("type")
    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if field_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if field_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else number
    if field_type in ("month_list", "integer_list"):
        return _coerce_int_list(value)
    if field_type == "string_list":
        return _coerce_string_list(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _coerce_int_list(value):
    items = value
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    if not isinstance(items, (list, tuple)):
        return value
    result = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            result.append(int(float(text)))
        except (TypeError, ValueError):
            return value
    return result


def _coerce_string_list(value):
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def resolve_change(field, change):
    """Resolve one change to a final ``(operation, value)``.

    An explicit operation wins. Otherwise an empty answer clears the key — with
    the credential exception: a blank secret box is "not retyped", never
    "delete the stored secret".
    """

    if change.operation == KEEP:
        return KEEP, None
    if change.operation == CLEAR:
        return CLEAR, None

    value = change.value
    if isinstance(value, str):
        value = value.strip()
    empty = value is None or value == ""
    if empty:
        return (KEEP if is_secret_catalog_field(field or {}) else CLEAR), None
    return SET, coerce_catalog_value(field, value)


def get_config_path(config, path):
    cursor = config
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return _MISSING
        cursor = cursor[part]
    return cursor


def set_config_path(config, path, value):
    parts = path.split(".")
    cursor = config
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def clear_config_path(config, path):
    """Remove a leaf. Returns True when something was actually removed."""

    parts = path.split(".")
    cursor = config
    for part in parts[:-1]:
        cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if not isinstance(cursor, dict):
            return False
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return False
    cursor.pop(parts[-1])
    return True


def catalog_field_index(policy, **kwargs):
    """The writable catalog field index for a workflow policy."""

    options = {"scope": policy.scope, "allow_secret": policy.allow_secret}
    options.update(kwargs)
    return config_field_index(**options)


def apply_config_changes(config, changes, policy, *, field_index=None):
    """Apply typed changes onto ``config`` in place and record what happened.

    Paths outside the policy's writable catalog index are ignored, so a value
    the browser invents can never become writable by appearing in a payload.
    Repeated (``[]``) paths belong to their own per-entry draft and are skipped.
    """

    index = catalog_field_index(policy) if field_index is None else field_index
    applied = []
    issues = []
    for change in changes or ():
        path = change.path
        if not isinstance(path, str) or not path or "[]" in path:
            continue
        field = index.get(path)
        if field is None:
            issues.append(
                MutationIssue(
                    "config_field_not_writable",
                    SEVERITY_INFO,
                    f"{path} is not a writable {policy.workflow} config field.",
                    path,
                )
            )
            continue
        applied.extend(_apply_one(config, path, field, change, policy))
    return ConfigMutationResult(config=config, applied_changes=tuple(applied), issues=tuple(issues))


def _apply_one(config, path, field, change, policy):
    operation, value = resolve_change(field, change)
    if operation == KEEP:
        return ()
    if operation == CLEAR:
        if not policy.allow_remove:
            return ()
        return (AppliedChange(path, CLEAR),) if clear_config_path(config, path) else ()
    if get_config_path(config, path) is _MISSING and not policy.allow_create:
        return ()
    set_config_path(config, path, value)
    return (AppliedChange(path, SET),)


def mqtt_grid_meter_keys():
    """Every ``grid_meter.mqtt`` key any known variant may carry."""

    return frozenset(GRID_METER_KNOWN_MQTT_KEYS)


def strip_incompatible_grid_meter_fields(grid, grid_type):
    """Drop grid-meter fields that belong to a different variant.

    The allowed keys come from the catalog variant spec, so a switch *inside*
    the MQTT family is cleaned up as precisely as a switch between HTTP and
    MQTT. Only keys some known variant claims are eligible for removal;
    operator-defined custom keys survive untouched.
    """

    spec = grid_meter_variant_field_spec(grid_type)
    if spec is None:
        grid.pop("mqtt", None)
        return

    allowed = spec["keys"]
    for key in list(grid.keys()):
        if key in GRID_METER_KNOWN_TOP_KEYS and key not in allowed:
            grid.pop(key, None)

    mqtt = grid.get("mqtt")
    if isinstance(mqtt, dict):
        allowed_mqtt = spec["mqtt_keys"]
        if not allowed_mqtt:
            grid.pop("mqtt", None)
        else:
            for key in list(mqtt.keys()):
                if key in GRID_METER_KNOWN_MQTT_KEYS and key not in allowed_mqtt:
                    mqtt.pop(key, None)


def strip_stale_grid_meter_keys(grid):
    """Full variant cleanup for the meter's own declared type.

    On top of the variant cleanup this drops the legacy *flat* MQTT keys, which
    only ever exist one level above where the target variant reads them. Setup
    never writes that representation; Maintenance may be editing a config that
    still carries it from an older EMS.
    """

    meter_type = str(grid.get("type") or "").strip().lower()
    strip_incompatible_grid_meter_fields(grid, meter_type)
    for key in mqtt_grid_meter_keys():
        if key in GRID_METER_KNOWN_TOP_KEYS:
            # Also a legitimate nested-variant top key (e.g. ``port``); the
            # variant cleanup above already decided it.
            continue
        grid.pop(key, None)


def editable_grid_meter_mqtt_keys(meter_type):
    """MQTT keys one variant's editor may write, or the union for unknown."""

    known = (mqtt_grid_meter_keys() - {"password"}) | set(_GRID_METER_MQTT_EXTRA_KEYS)
    spec = grid_meter_variant_field_spec(meter_type) if meter_type else None
    if spec is None:
        return tuple(sorted(known))
    allowed = (set(spec["mqtt_keys"]) - {"password"}) | set(_GRID_METER_MQTT_EXTRA_KEYS)
    return tuple(sorted(known & allowed))


def _grid_meter_field(policy, index, key):
    field = index.get(GRID_METER_PREFIX + key)
    if field is not None:
        return field
    return _UNDECLARED_GRID_METER_FIELDS.get(key)


def apply_grid_meter_changes(grid, changes, policy, *, credential=None, field_index=None):
    """Apply grid-meter edits onto one ``grid_meter`` block, in place.

    ``changes`` carry block-relative paths (``type``, ``ip``, ``mqtt.topic``).
    A resolved ``type`` change is applied first; the incompatible-key cleanup
    runs after the top-level writes, because a draft still holds the values of
    the variant it was loaded from and would otherwise write a stale key back
    in behind the cleanup. MQTT values are written into the representation the
    block already uses when the policy preserves legacy shapes, and into the
    canonical nested block otherwise.
    """

    index = catalog_field_index(policy, allow_secret=True) if field_index is None else field_index
    applied = []
    issues = []

    original_type = str(grid.get("type") or "").strip().lower()
    top_changes, mqtt_changes = [], []
    for change in changes or ():
        path = change.path
        if not isinstance(path, str) or not path:
            continue
        head, _, tail = path.partition(".")
        if head == "mqtt" and tail:
            mqtt_changes.append(ConfigChange(tail, change.value, change.operation))
        elif head == "type":
            applied.extend(_apply_grid_meter_type(grid, change))
        elif not tail:
            top_changes.append(change)

    new_type = str(grid.get("type") or "").strip().lower()

    for change in top_changes:
        field = _grid_meter_field(policy, index, change.path)
        if field is None:
            issues.append(
                MutationIssue(
                    "config_field_not_writable",
                    SEVERITY_INFO,
                    f"grid_meter.{change.path} is not a writable grid-meter field.",
                    GRID_METER_PREFIX + change.path,
                )
            )
            continue
        applied.extend(
            AppliedChange(GRID_METER_PREFIX + entry.path, entry.operation)
            for entry in _apply_one(grid, change.path, field, change, policy)
        )

    if new_type != original_type:
        strip_stale_grid_meter_keys(grid)
        applied = [
            entry
            for entry in applied
            if get_config_path(grid, entry.path[len(GRID_METER_PREFIX):]) is not _MISSING
            or entry.operation == CLEAR
        ]

    if new_type in MQTT_GRID_METER_TYPES:
        applied.extend(
            _apply_grid_meter_mqtt(grid, mqtt_changes, policy, index, new_type, credential)
        )

    return ConfigMutationResult(config=grid, applied_changes=tuple(applied), issues=tuple(issues))


def _apply_grid_meter_type(grid, change):
    operation, value = resolve_change({"type": "select"}, change)
    if operation != SET:
        return ()
    resolved = str(value).strip().lower()
    if grid.get("type") == resolved:
        return ()
    grid["type"] = resolved
    return (AppliedChange(GRID_METER_PREFIX + "type", SET),)


def _apply_grid_meter_mqtt(grid, changes, policy, index, meter_type, credential):
    """Write MQTT values into the representation this meter already uses."""

    editable = editable_grid_meter_mqtt_keys(meter_type)
    current = grid_meter_mqtt_settings(grid)
    nested = grid.get("mqtt")
    has_nested = isinstance(nested, dict)
    has_flat = any(key in grid for key in mqtt_grid_meter_keys())
    keep_flat = policy.preserve_legacy_representations and has_flat and not has_nested

    def container():
        nonlocal nested, has_nested
        if has_nested:
            return nested
        if keep_flat:
            return grid
        nested = {}
        grid["mqtt"] = nested
        has_nested = True
        return nested

    applied = []
    by_key = {}
    for change in changes:
        by_key[change.path] = change
    for key in editable:
        change = by_key.get(key)
        if change is None:
            continue
        field = _grid_meter_field(policy, index, f"mqtt.{key}") or {}
        operation, value = resolve_change(field, change)
        if operation == KEEP:
            continue
        path = f"{GRID_METER_PREFIX}mqtt.{key}"
        if operation == CLEAR:
            if not policy.allow_remove:
                continue
            removed = False
            if has_nested:
                removed = nested.pop(key, _MISSING) is not _MISSING
            if key in grid and key not in GRID_METER_KNOWN_TOP_KEYS:
                grid.pop(key)
                removed = True
            if removed:
                applied.append(AppliedChange(path, CLEAR))
            continue
        if key in current and current[key] == value and type(current[key]) is type(value):
            continue
        container()[key] = value
        applied.append(AppliedChange(path, SET))

    applied.extend(_apply_grid_meter_credential(grid, credential, container))
    return applied


def _apply_grid_meter_credential(grid, credential, container):
    if credential is None or credential.operation == KEEP:
        return ()
    path = f"{GRID_METER_PREFIX}mqtt.password"
    if credential.operation == CLEAR:
        removed = False
        nested = grid.get("mqtt")
        if isinstance(nested, dict) and nested.pop("password", _MISSING) is not _MISSING:
            removed = True
        if grid.pop("password", _MISSING) is not _MISSING:
            removed = True
        return (AppliedChange(path, CLEAR),) if removed else ()
    value = credential.value
    if not isinstance(value, str) or not value:
        return ()
    container()["password"] = value
    return (AppliedChange(path, SET),)


def flatten_config_leaves(value, prefix, out):
    """Flatten a config into ``path -> leaf`` pairs.

    Dict keys join with dots and list indices render as ``[i]``, so a per-device
    field reads as ``devices[0].max_power``. A scalar list stays one leaf.
    """

    if isinstance(value, dict):
        for key in sorted(value):
            if str(key).startswith("_"):
                continue
            flatten_config_leaves(value[key], f"{prefix}.{key}" if prefix else str(key), out)
        return
    if isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
        for position, item in enumerate(value):
            flatten_config_leaves(item, f"{prefix}[{position}]", out)
        return
    out[prefix] = value


def mutation_diff(before, after, *, is_secret_leaf, bound_value=None):
    """Deterministic, secret-safe leaf diff of two configs.

    Paths are sorted, secret leaves never surface a value on either side, and
    values pass through ``bound_value`` so a diff stays renderable. Suitable as
    a preview input: the same two configs always digest the same way.
    """

    before_leaves, after_leaves = {}, {}
    flatten_config_leaves(before, "", before_leaves)
    flatten_config_leaves(after, "", after_leaves)
    bound = bound_value if bound_value is not None else (lambda value: value)

    def render(path, value):
        return REDACTED_DIFF_VALUE if is_secret_leaf(path) else bound(value)

    changes, added, removed = [], [], []
    for path in sorted(set(before_leaves) | set(after_leaves)):
        in_before = path in before_leaves
        in_after = path in after_leaves
        if in_before and in_after:
            if before_leaves[path] != after_leaves[path]:
                changes.append(
                    {
                        "path": path,
                        "before": render(path, before_leaves[path]),
                        "after": render(path, after_leaves[path]),
                    }
                )
        elif in_after:
            added.append({"path": path, "after": render(path, after_leaves[path])})
        else:
            removed.append({"path": path, "before": render(path, before_leaves[path])})

    return {
        "changed": bool(changes or added or removed),
        "changes": changes,
        "added": added,
        "removed": removed,
    }


# The placeholder a diff shows instead of a secret. Never a usable value.
REDACTED_DIFF_VALUE = "••••"


def apply_common_values(target, values, fields):
    """Apply the catalog-backed value set of one repeated entry (a device).

    Repeated entries have no dotted config path of their own, so they carry a
    flat key set; the interpretation of each key is the shared one.
    """

    applied = []
    for key, field in fields.items():
        if key not in values:
            continue
        change = ConfigChange(key, values[key])
        operation, value = resolve_change(field, change)
        if operation == KEEP:
            continue
        if operation == CLEAR:
            if target.pop(key, _MISSING) is not _MISSING:
                applied.append(AppliedChange(key, CLEAR))
            continue
        target[key] = value
        applied.append(AppliedChange(key, SET))
    return tuple(applied)


def copy_config(config):
    return copy.deepcopy(config)


__all__ = [
    "CONFIG_MUTATION_CONTRACT_VERSION",
    "SET",
    "CLEAR",
    "KEEP",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "WORKFLOW_SETUP",
    "WORKFLOW_MAINTENANCE",
    "GRID_METER_PREFIX",
    "REDACTED_DIFF_VALUE",
    "ConfigChange",
    "CredentialIntent",
    "MutationPolicy",
    "MutationIssue",
    "AppliedChange",
    "ConfigMutationResult",
    "SETUP_POLICY",
    "MAINTENANCE_POLICY",
    "coerce_catalog_value",
    "resolve_change",
    "get_config_path",
    "set_config_path",
    "clear_config_path",
    "catalog_field_index",
    "apply_config_changes",
    "apply_common_values",
    "mqtt_grid_meter_keys",
    "strip_incompatible_grid_meter_fields",
    "strip_stale_grid_meter_keys",
    "editable_grid_meter_mqtt_keys",
    "apply_grid_meter_changes",
    "flatten_config_leaves",
    "mutation_diff",
    "copy_config",
]
