# SPDX-License-Identifier: AGPL-3.0-or-later
"""What survives a slot switch, declared to upstream and proven at runtime.

The mounting itself belongs to ``rpi-image-gen``: the appliance declares its
paths in ``/etc/rpi-image-gen/slot-shared.d/`` and upstream's
``slot-shared-generator`` binds ``/persistent/shared/<path>`` over each one.
This module owns the declaration and the verifier, not a second mount framework.

The verifier exists because upstream's generator fails **open**. Every bind is
guarded by ``ConditionPathIsDirectory`` on its source, so a shared path whose
directory is missing is silently skipped and the service writes into the
read-only root's fallback copy instead. On a general-purpose image that is safe
degradation. Here it means every write since the last flash is lost at the next
slot switch, without a single error, so each required path is checked against
the device ``/persistent`` is mounted from rather than against its own existence.
"""

from dataclasses import dataclass

from appliance import image_variants

PERSISTENT_SCHEMA_VERSION = 3

PERSISTENT_MOUNTPOINT = "/persistent"
SHARED_ROOT = "/persistent/shared"

# Upstream owns both of these; the appliance reads them and never writes them.
MACHINE_ID_SOURCE = "/persistent/common/etc/machine-id"
MACHINE_ID_UNIT = "machine-id-sync.service"

SLOT_SHARED_CONF_DIR = "/etc/rpi-image-gen/slot-shared.d"
SLOT_SHARED_CONF_NAME = "50-ems-appliance.conf"
SLOT_SHARED_VERSION = "1"

# Named by a project sshd drop-in rather than shared as /etc/ssh, so the host
# identity survives a slot switch while each slot keeps its own distro config.
SSH_HOST_KEY_DIRECTORY = "/var/lib/ems-appliance-manager/ssh"
SSH_DROP_IN = "/etc/ssh/sshd_config.d/50-ems-appliance-hostkeys.conf"

CATEGORY_EMS = "ems"
CATEGORY_APPLIANCE = "appliance"
CATEGORY_NETWORK = "network"
CATEGORY_RECOVERY = "recovery"


@dataclass(frozen=True)
class SharedPath:
    """One location that must survive a slot switch."""

    name: str
    category: str
    target: str
    required: bool = True
    description: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "category": self.category,
            "target": self.target,
            "source": source_path(self),
            "required": self.required,
            "description": self.description,
        }


SHARED_PATHS = (
    SharedPath(
        name="ems_installation",
        category=CATEGORY_EMS,
        target="/opt/ems-solarflow",
        description="EMS config, data, backups, compose file and Admin bootstrap metadata",
    ),
    SharedPath(
        name="appliance_state",
        category=CATEGORY_APPLIANCE,
        target="/var/lib/ems-appliance-manager",
        description=(
            "authentication, operations, package ownership, known-good Admin metadata "
            "and the SSH host keys"
        ),
    ),
    SharedPath(
        name="appliance_logs",
        category=CATEGORY_APPLIANCE,
        target="/var/log/ems-appliance-manager",
        description="appliance, agent and audit logs",
    ),
    SharedPath(
        name="appliance_config",
        category=CATEGORY_APPLIANCE,
        target="/etc/ems-appliance-manager",
        description="host configuration, the image allowlist and the A/B layout descriptor",
    ),
    SharedPath(
        name="os_update_state",
        category=CATEGORY_RECOVERY,
        target="/var/lib/ems-appliance-os-update",
        description="staged OS artifacts, pending A/B state and post-fallback evidence",
    ),
    SharedPath(
        name="network_profiles",
        category=CATEGORY_NETWORK,
        target="/etc/NetworkManager/system-connections",
        description="persistent NetworkManager connection profiles",
    ),
    SharedPath(
        name="backup_account",
        category=CATEGORY_APPLIANCE,
        target="/var/lib/ems-backup",
        description=(
            "the confined backup account's home: the operator's authorized_keys and "
            "the ownership marker proving this package created it"
        ),
    ),
)

# Slot-local by decision, so the contract is a statement rather than an omission.
# /var as a whole is per-slot through upstream's slot-perst-generator, which is
# what keeps the Docker engine's version-coupled content store out of a rollback.
SLOT_LOCAL_PATHS = (
    "/",
    "/usr",
    "/lib",
    "/lib/modules",
    "/boot/firmware",
    "/var",
    "/var/lib/docker",
    "/var/lib/dpkg",
    "/var/lib/apt",
    "/var/cache/apt",
    "/etc/systemd/system",
    "/etc/ssh",
)

# Paths upstream shares on this appliance's behalf. Verified, never declared
# again: a second declaration would be a second authority over the same bind.
UPSTREAM_SHARED_PATHS = (
    ("machine_identity", MACHINE_ID_SOURCE, "/etc/machine-id"),
    ("home", "/persistent/home", "/home"),
    ("journal", "/persistent/log/journal", "/var/log/journal"),
)

STATE_OK = "ok"
# Not a weaker "ok". The check did not pass; it did not apply, because the
# image says it has no persistent partition to share. Kept distinct from
# STATE_OK so no report can claim a contract was verified that never existed.
STATE_NOT_APPLICABLE = "persistence_not_applicable"
STATE_MISSING = "persistence_missing"
STATE_IDENTITY_MISMATCH = "persistence_identity_mismatch"
STATE_OPTIONS_UNEXPECTED = "persistence_options_unexpected"
STATE_PATH_NOT_SHARED = "persistence_path_not_shared"

REQUIRED_OPTIONS = ("rw",)
FORBIDDEN_OPTIONS = ("ro",)


@dataclass(frozen=True)
class PersistenceReport:
    state: str
    mounted: bool
    mountpoint: str
    source: str
    expected_source: str
    schema_version: int
    paths: tuple = ()
    problems: tuple = ()
    # Findings that do not stop the appliance. Every unit that can write state
    # Requires= this verification, so a problem here means no agent and no web
    # service; anything that leaves the appliance usable and repairable belongs
    # in this tuple instead.
    warnings: tuple = ()

    @property
    def ok(self):
        return self.state in (STATE_OK, STATE_NOT_APPLICABLE) and not self.problems

    def to_dict(self):
        return {
            "state": self.state,
            "ok": self.ok,
            "mounted": self.mounted,
            "mountpoint": self.mountpoint,
            "source": self.source,
            "expected_source": self.expected_source,
            "schema_version": self.schema_version,
            "paths": [dict(entry) for entry in self.paths],
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }


def source_path(shared, *, shared_root=SHARED_ROOT):
    """Where upstream binds ``shared`` from on the persistent partition."""

    return shared_root.rstrip("/") + shared.target


def shared_subtree(shared, *, shared_root=SHARED_ROOT):
    """The path within the persistent filesystem, as ``mountinfo`` reports it."""

    return source_path(shared, shared_root=shared_root)[len(PERSISTENT_MOUNTPOINT) :]


def slot_shared_conf():
    """The declaration upstream's generator reads. Exact bytes, generated."""

    lines = [
        "# Generated from appliance/ab_persistence.py. Do not edit.",
        "# Consumed by rpi-image-gen's slot-shared-generator; see",
        "# docs/appliance/ab-persistence-contract.md.",
        f"Version={SLOT_SHARED_VERSION}",
    ]
    lines.extend(f"Path={shared.target}" for shared in SHARED_PATHS)
    return "\n".join(lines) + "\n"


# --- activating what upstream generates --------------------------------------

# The pinned generator writes one .mount unit per declared path and activates
# exactly one of them, so the appliance supplies the rest of the activation
# itself. See docs/appliance/ab-persistence-contract.md.
SYSTEM_UNIT_DIR = "/etc/systemd/system"
ACTIVATION_TARGET = "local-fs.target"
GENERATOR_UNIT_DIR = "/run/systemd/generator"

_UNRESERVED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_."
)


def escape_path(path):
    """``systemd-escape --path``, without needing systemd to be installed."""

    text = str(path).strip("/")
    if not text:
        return "-"
    escaped = []
    for index, char in enumerate(text):
        if char == "/":
            # The separator. A literal dash in the path is escaped below, so
            # the two can never be confused when a unit name is read back.
            escaped.append("-")
        elif char == "." and index == 0:
            escaped.append("\\x2e")
        elif char in _UNRESERVED:
            escaped.append(char)
        else:
            escaped.append("".join(f"\\x{byte:02x}" for byte in char.encode("utf-8")))
    return "".join(escaped)


def mount_unit_name(target):
    """The unit name systemd gives the bind mount at ``target``."""

    return f"{escape_path(target)}.mount"


def shared_mount_units():
    """Every mount unit upstream's generator produces for this appliance."""

    return tuple(mount_unit_name(shared.target) for shared in SHARED_PATHS)


def activation_links():
    """The ``local-fs.target.wants`` entries the image ships, by unit name.

    Maps the path of each link to what it points at. systemd resolves a wants
    entry by its file name, so the target only has to name where the generator
    will have written the unit.
    """

    directory = f"{SYSTEM_UNIT_DIR}/{ACTIVATION_TARGET}.wants"
    return {
        f"{directory}/{unit}": f"{GENERATOR_UNIT_DIR}/{unit}"
        for unit in shared_mount_units()
    }


def requires_mounts_for():
    """The ``RequiresMountsFor=`` line the appliance units carry."""

    return " ".join(shared.target for shared in SHARED_PATHS)


def verify(status, mounts, *, schema_version=PERSISTENT_SCHEMA_VERSION):
    """Prove the persistent partition is mounted and every shared path uses it.

    Nothing here writes, mounts or repairs: an appliance that cannot prove its
    persistence must fail closed, not be fixed up by a status call.
    """

    manifest = status.manifest
    if manifest is None:
        variant = image_variants.variant_of_build_marker(status.os_build)
        if variant is not None and not variant.has_ab_layout:
            return PersistenceReport(
                state=STATE_NOT_APPLICABLE,
                mounted=False,
                mountpoint="",
                source="",
                expected_source="",
                schema_version=schema_version,
            )
        return PersistenceReport(
            state=STATE_MISSING,
            mounted=False,
            mountpoint="",
            source="",
            expected_source="",
            schema_version=schema_version,
            problems=("this host has no A/B layout descriptor",),
        )

    mountpoint = manifest.persist_mountpoint
    expected = status.persist_device.path if status.persist_device else ""
    record = mounts.get(mountpoint)
    if record is None:
        return PersistenceReport(
            state=STATE_MISSING,
            mounted=False,
            mountpoint=mountpoint,
            source="",
            expected_source=expected,
            schema_version=schema_version,
            problems=(f"{mountpoint} is not mounted",),
        )

    source = record.get("source") or ""
    aliases = _persistent_aliases(manifest, status)
    problems = []
    state = STATE_OK

    if expected and source and source not in aliases:
        state = STATE_IDENTITY_MISMATCH
        problems.append(f"{mountpoint} is mounted from {source}, expected {expected}")
    elif source:
        # The mountpoint's own source, once it survived the check above, is the
        # partition the binds have to come from. Without this the two checks
        # measure one partition against two different authorities: a host whose
        # descriptor names no resolvable device passes here and has every bind
        # called foreign. A mismatched mountpoint never reaches this branch, so
        # a wrong partition cannot make itself the authority for its own binds.
        aliases = aliases | {source}

    options = record.get("options") or frozenset()
    for option in REQUIRED_OPTIONS:
        if option not in options:
            state = STATE_OPTIONS_UNEXPECTED if state == STATE_OK else state
            problems.append(f"{mountpoint} is not mounted {option}")
    for option in FORBIDDEN_OPTIONS:
        if option in options:
            state = STATE_OPTIONS_UNEXPECTED if state == STATE_OK else state
            problems.append(f"{mountpoint} is mounted {option}")

    warnings = []
    if manifest.persistent_schema_version != schema_version:
        # Reported, not fatal, and no longer the state authority. Both operands
        # come out of the same image -- upstream re-seeds the descriptor from
        # the booting slot before the binds activate -- so this normally
        # compares a number with itself. It can differ only when that seeding
        # did not run, because upstream's generated mounts merely Want= it, and
        # the binds then activate over the shared copy the other slot left. That
        # is a stale descriptor on a working appliance, and stopping the agent
        # and the web service over it would take away the only remote way to
        # repair it. What the partition actually holds is answered by
        # appliance/persistent_state.py, which does not travel with the slot.
        warnings.append(
            f"the image declares persistent schema {manifest.persistent_schema_version}, "
            f"this appliance implements {schema_version}"
        )

    entries = []
    for shared in SHARED_PATHS:
        entry = _verify_shared_path(shared, mounts, aliases)
        entries.append(entry)
        if entry["problem"] and shared.required:
            state = STATE_PATH_NOT_SHARED if state == STATE_OK else state
            problems.append(entry["problem"])

    return PersistenceReport(
        state=state,
        mounted=True,
        mountpoint=mountpoint,
        source=source,
        expected_source=expected,
        schema_version=schema_version,
        paths=tuple(entries),
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def _persistent_aliases(manifest, status):
    """Every name the persistent partition legitimately answers to.

    systemd mounts it through upstream's ``/dev/disk/by-slot`` alias, and the
    kernel reports back whatever string was passed, so the resolved device path
    and the alias are the same partition under two names. What the mountpoint
    is actually mounted from joins this set in ``verify``, but only after it
    has been judged.
    """

    names = {manifest.persistent_alias}
    if status.persist_device is not None:
        names.add(status.persist_device.path)
    return {name for name in names if name}


def _verify_shared_path(shared, mounts, aliases):
    entry = {
        "name": shared.name,
        "category": shared.category,
        "target": shared.target,
        "source": source_path(shared),
        "shared": False,
        "problem": "",
    }
    record = mounts.get(shared.target)
    if record is None:
        entry["problem"] = (
            f"{shared.target} is not bound from the persistent partition; upstream's "
            "generator skipped it and it would fall back to the read-only root, losing "
            "every write at the next slot switch"
        )
        return entry
    observed = record.get("source") or ""
    if aliases and observed and observed not in aliases:
        entry["problem"] = (
            f"{shared.target} is mounted from {observed}, not from the persistent partition"
        )
        return entry
    expected_subtree = shared_subtree(shared)
    subtree = record.get("root") or ""
    if subtree and subtree != expected_subtree:
        entry["problem"] = (
            f"{shared.target} exposes {subtree} of the persistent partition, expected "
            f"{expected_subtree}"
        )
        return entry
    entry["shared"] = True
    return entry


def contract():
    """The whole persistence contract as data, for docs, tests and the API."""

    return {
        "schema_version": PERSISTENT_SCHEMA_VERSION,
        "mechanism": "slot-shared",
        "persistent_mountpoint": PERSISTENT_MOUNTPOINT,
        "shared_root": SHARED_ROOT,
        "declaration": f"{SLOT_SHARED_CONF_DIR}/{SLOT_SHARED_CONF_NAME}",
        "shared": [shared.to_dict() for shared in SHARED_PATHS],
        "slot_local": list(SLOT_LOCAL_PATHS),
        "upstream_shared": [
            {"name": name, "source": source, "target": target}
            for name, source, target in UPSTREAM_SHARED_PATHS
        ],
        "machine_identity": {
            "source": MACHINE_ID_SOURCE,
            "target": "/etc/machine-id",
            "unit": MACHINE_ID_UNIT,
            "owner": "rpi-image-gen",
            "stable_across_slots": True,
        },
        "ssh_host_identity": {
            "directory": SSH_HOST_KEY_DIRECTORY,
            "drop_in": SSH_DROP_IN,
            "shares_etc_ssh": False,
        },
    }
