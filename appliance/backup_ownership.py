# SPDX-License-Identifier: AGPL-3.0-or-later
"""What this package owns of the backup account, bound to an exact identity.

A username is not an identity. The account, its home directory and its key
material can all be replaced under the same name, so every destructive step —
purge, key withdrawal, ACL cleanup — is gated on the record written when the
account was created: uid, primary gid, home path, and a marker file inside the
home whose secret this package generated.

Device and inode alone cannot carry that authority. A filesystem is free to
hand a freshly created directory the inode a deleted one just released, so a
replacement home can present the exact device/inode pair that was recorded. The
marker is the durable half of the identity: it lives in a root-owned home the
backup account cannot write, and a directory that replaced the recorded one does
not contain it.

The packaged shell tooling writes and reads the same files, because dpkg has
already removed this package's own programs by the time ``postrm purge`` runs.
This module is the Python side of that one contract.
"""

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

RECORD_SCHEMA_VERSION = 3
LEGACY_RECORD_SCHEMA_VERSION = 2
ACL_MANIFEST_SCHEMA_VERSION = 3
RECORD_NAME = "backup-account.json"
MANAGED_KEYS_NAME = "managed-keys.list"
ACL_MANIFEST_NAME = "acl-manifest.tsv"

HOME_MARKER_NAME = ".ems-appliance-backup-home"
HOME_MARKER_SCHEMA_VERSION = 1
HOME_MARKER_MODE = 0o400
UNSAFE_MARKER_MODE = stat.S_IWGRP | stat.S_IWOTH

OWNED = "owned"
NO_RECORD = "no_ownership_record"
UNSUPPORTED_RECORD = "ownership_record_unsupported"
MIGRATION_REQUIRED = "ownership_record_requires_migration"
NOT_PACKAGE_CREATED = "account_not_created_by_package"
ACCOUNT_MISSING = "account_missing"
IDENTITY_MISMATCH = "account_identity_mismatch"
HOME_MISMATCH = "home_identity_mismatch"
MARKER_MISSING = "home_marker_missing"
MARKER_MISMATCH = "home_marker_mismatch"

# Everything ``verify_ownership`` can answer other than "owned". A caller that
# cannot control which half of the identity a fixture breaks — a filesystem that
# hands a released inode straight back decides that, not the caller — asserts
# membership here rather than one exact value. What must never vary is that none
# of them is ownership.
MISMATCH_REASONS = (
    NO_RECORD,
    UNSUPPORTED_RECORD,
    MIGRATION_REQUIRED,
    NOT_PACKAGE_CREATED,
    ACCOUNT_MISSING,
    IDENTITY_MISMATCH,
    HOME_MISMATCH,
    MARKER_MISSING,
    MARKER_MISMATCH,
)

# The closed set of states an operator acts on, mirroring
# ``backup-account.sh ownership-state``. Unresolved legacy ownership is a state
# of its own and is never reported as package-owned.
STATE_CURRENT = "current"
STATE_NO_RECORD = "no_ownership_record"
STATE_LEGACY = "legacy_manual_migration_required"
STATE_CONFLICT = "ownership_conflict"
STATE_MARKER_MISSING = "marker_missing"
STATE_MARKER_MISMATCH = "marker_mismatch"
STATE_RECORD_CORRUPT = "record_corrupt"

OWNERSHIP_STATES = (
    STATE_CURRENT,
    STATE_NO_RECORD,
    STATE_LEGACY,
    STATE_CONFLICT,
    STATE_MARKER_MISSING,
    STATE_MARKER_MISMATCH,
    STATE_RECORD_CORRUPT,
)


@dataclass(frozen=True)
class OwnershipRecord:
    present: bool = False
    schema_version: int = 0
    account: str = ""
    created_by_package: bool = False
    uid: int = None
    primary_gid: int = None
    home: str = ""
    home_device: str = ""
    home_inode: str = ""
    home_marker: str = ""
    home_marker_nonce: str = ""
    home_created_by_package: bool = False
    installation_id: str = ""

    @property
    def supported(self):
        return self.present and self.schema_version == RECORD_SCHEMA_VERSION

    @property
    def legacy(self):
        return self.present and self.schema_version == LEGACY_RECORD_SCHEMA_VERSION

    @property
    def marker_path(self):
        if self.home_marker:
            return self.home_marker
        return os.path.join(self.home, HOME_MARKER_NAME) if self.home else ""

    def to_dict(self):
        return {
            "present": self.present,
            "schema_version": self.schema_version,
            "account": self.account,
            "created_by_package": self.created_by_package,
            "uid": self.uid,
            "primary_gid": self.primary_gid,
            "home": self.home,
            "home_marker": self.marker_path,
            "home_created_by_package": self.home_created_by_package,
            "installation_id": self.installation_id,
        }


def record_file(paths):
    return paths.package_state_dir / RECORD_NAME


def managed_keys_file(paths):
    return paths.package_state_dir / MANAGED_KEYS_NAME


def acl_manifest_file(paths):
    return paths.package_state_dir / ACL_MANIFEST_NAME


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_record(paths):
    try:
        payload = json.loads(record_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OwnershipRecord()
    if not isinstance(payload, dict):
        return OwnershipRecord()
    return OwnershipRecord(
        present=True,
        schema_version=_as_int(payload.get("schema_version")) or 0,
        account=str(payload.get("account") or ""),
        created_by_package=bool(payload.get("created_by_package")),
        uid=_as_int(payload.get("uid")),
        primary_gid=_as_int(payload.get("primary_gid")),
        home=str(payload.get("home") or ""),
        home_device=str(payload.get("home_device") or ""),
        home_inode=str(payload.get("home_inode") or ""),
        home_marker=str(payload.get("home_marker") or ""),
        home_marker_nonce=str(payload.get("home_marker_nonce") or ""),
        home_created_by_package=bool(payload.get("home_created_by_package")),
        installation_id=str(payload.get("installation_id") or ""),
    )


def account_entry(name):
    import pwd

    try:
        return pwd.getpwnam(str(name))
    except (KeyError, TypeError):
        return None


def home_identity(path):
    """``device:inode`` of a real directory, or "" when there is nothing to bind.

    ``lstat`` on purpose: a symbolic link at the home path is not the directory
    that was recorded, however its target stats.

    This pair is a *supporting* signal only. An inode a filesystem released and
    handed straight back out identifies a different directory just as well, so
    the marker below is what actually decides ownership.
    """

    try:
        entry = os.lstat(str(path))
    except OSError:
        return ""
    if not stat.S_ISDIR(entry.st_mode):
        return ""
    return f"{entry.st_dev}:{entry.st_ino}"


def new_marker_nonce():
    return secrets.token_hex(32)


def render_home_marker(*, account, uid, primary_gid, home, installation_id, nonce):
    """The exact bytes ``backup-account.sh ensure`` writes into the home.

    One renderer for both sides of the contract: the packaged shell writes this
    layout and this module reads it back, so a change to either is a change to
    the same text.
    """

    return (
        "# ems-appliance backup home marker. Written by the package; do not edit.\n"
        f"schema_version={HOME_MARKER_SCHEMA_VERSION}\n"
        f"account={account}\n"
        f"uid={uid}\n"
        f"primary_gid={primary_gid}\n"
        f"home={home}\n"
        f"installation_id={installation_id}\n"
        f"nonce={nonce}\n"
    )


def read_home_marker(path):
    values = {}
    try:
        text = Path(str(path)).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return values
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        values[key.strip()] = value.strip()
    return values


def _marker_file_usable(path):
    """A marker only proves something when nobody else could have put it there."""

    try:
        status = os.lstat(str(path))
    except OSError:
        return False
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        return False
    if stat.S_IMODE(status.st_mode) & UNSAFE_MARKER_MODE:
        return False
    # Only a run that is actually privileged can judge root ownership; an
    # unprivileged test host has no root-owned files to compare against.
    return not (os.geteuid() == 0 and status.st_uid != 0)


def verify_home(record):
    """Is the directory at the recorded path still the home this package created?

    Returns ``""`` when it is, or the reason it cannot be proven.
    """

    recorded = f"{record.home_device}:{record.home_inode}"
    observed = home_identity(record.home)
    if not observed or recorded == ":" or observed != recorded:
        return HOME_MISMATCH

    marker = record.marker_path
    if not record.home_marker_nonce or not marker:
        return MARKER_MISSING
    if os.path.dirname(marker) != str(record.home).rstrip("/"):
        return MARKER_MISMATCH
    if not os.path.lexists(marker):
        return MARKER_MISSING
    if not _marker_file_usable(marker):
        return MARKER_MISMATCH

    values = read_home_marker(marker)
    if _as_int(values.get("schema_version")) != HOME_MARKER_SCHEMA_VERSION:
        return MARKER_MISMATCH
    if values.get("account") != record.account:
        return MARKER_MISMATCH
    if _as_int(values.get("uid")) != record.uid or _as_int(values.get("primary_gid")) != (
        record.primary_gid
    ):
        return MARKER_MISMATCH
    if values.get("home") != record.home:
        return MARKER_MISMATCH
    # One installation writes one identifier into both halves of the ownership
    # proof. A marker naming another installation belongs to another record.
    if str(values.get("installation_id") or "") != str(record.installation_id or ""):
        return MARKER_MISMATCH
    if not secrets.compare_digest(
        str(values.get("nonce") or ""), str(record.home_marker_nonce)
    ):
        return MARKER_MISMATCH
    return ""


_LOOKUP = object()


def verify_ownership(paths, name, *, entry=_LOOKUP):
    """Is the live account the exact one this package created?

    ``entry`` may be a passwd entry the caller already resolved, or ``None`` to
    say the account does not exist. Omitting it looks the account up here.
    """

    verdict = verify_account(paths, name, entry=entry)
    if not verdict["owned"]:
        return verdict
    reason = verify_home(verdict["record"])
    if reason:
        return {"owned": False, "reason": reason, "record": verdict["record"]}
    return verdict


def verify_account(paths, name, *, entry=_LOOKUP):
    """The account half of the identity, without judging its home directory.

    Separating the two is what lets a fail-closed step act through the exact
    package-owned account — expiring it — while a home the package cannot prove
    is its own stays untouched.
    """

    record = read_record(paths)
    if not record.present:
        return {"owned": False, "reason": NO_RECORD, "record": record}
    if record.legacy:
        # A record from before the marker existed cannot prove that the home it
        # names is this package's, so nothing adopts it on its own. Only
        # ``backup-account.sh migrate-ownership`` may, and only after proving
        # every other part of the identity by hand.
        return {"owned": False, "reason": MIGRATION_REQUIRED, "record": record}
    if not record.supported:
        return {"owned": False, "reason": UNSUPPORTED_RECORD, "record": record}
    if record.account != str(name) or not record.created_by_package:
        return {"owned": False, "reason": NOT_PACKAGE_CREATED, "record": record}

    entry = account_entry(name) if entry is _LOOKUP else entry
    if entry is None:
        return {"owned": False, "reason": ACCOUNT_MISSING, "record": record}
    if (
        entry.pw_uid != record.uid
        or entry.pw_gid != record.primary_gid
        or str(entry.pw_dir) != record.home
    ):
        return {"owned": False, "reason": IDENTITY_MISMATCH, "record": record}
    return {"owned": True, "reason": OWNED, "record": record}


def _record_is_corrupt(paths):
    try:
        payload = json.loads(record_file(paths).read_text(encoding="utf-8"))
    except OSError:
        return False
    except ValueError:
        return True
    if not isinstance(payload, dict):
        return True
    return not (payload.get("account") and payload.get("home"))


def ownership_state(paths, name, *, entry=_LOOKUP):
    """One of ``OWNERSHIP_STATES``, the same verdict the packaged shell reports.

    Separate from ``verify_ownership`` on purpose: that answers "may this run
    change the account", this answers "what does an operator have to do".
    """

    if not record_file(paths).is_file():
        return STATE_NO_RECORD
    if _record_is_corrupt(paths):
        return STATE_RECORD_CORRUPT
    record = read_record(paths)
    if record.legacy or record.schema_version == 0:
        return STATE_LEGACY
    if not record.supported:
        return STATE_RECORD_CORRUPT

    verdict = verify_account(paths, name, entry=entry)
    if not verdict["owned"]:
        return STATE_CONFLICT
    if home_identity(record.home) != f"{record.home_device}:{record.home_inode}":
        return STATE_CONFLICT
    marker = record.marker_path
    if not marker or not os.path.lexists(marker):
        return STATE_MARKER_MISSING
    if verify_home(record):
        return STATE_MARKER_MISMATCH
    return STATE_CURRENT


# --- managed key material ---------------------------------------------------


def key_hash(blob):
    """The attribution the packaged purge computes for one key body."""

    return hashlib.sha256(str(blob).encode("utf-8")).hexdigest()


def managed_key_hashes(paths):
    try:
        text = managed_keys_file(paths).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _write_hashes(paths, hashes):
    target = managed_keys_file(paths)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(f"{item}\n" for item in sorted(hashes)), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError:
        return False
    return True


def record_managed_keys(paths, blobs):
    """Attribute these key bodies to this package, so purge may remove them."""

    hashes = managed_key_hashes(paths)
    hashes.update(key_hash(blob) for blob in blobs if blob)
    return _write_hashes(paths, hashes)


def forget_managed_keys(paths, blobs):
    hashes = managed_key_hashes(paths)
    hashes.difference_update(key_hash(blob) for blob in blobs if blob)
    return _write_hashes(paths, hashes)


def unmanaged_keys(paths, keys):
    """The authorised keys this package cannot attribute to itself."""

    known = managed_key_hashes(paths)
    return [key for key in keys if key_hash(key.blob) not in known]


# --- ACL manifest -----------------------------------------------------------


def read_acl_manifest(paths):
    """The exact objects and entries the export setup granted.

    One record per object and ACL scope, with the permissions that were there
    before this package touched it. A manifest of another schema is reported as
    unsupported rather than reinterpreted.
    """

    empty = {
        "present": False,
        "supported": False,
        "schema_version": 0,
        "user": "",
        "installation_id": "",
        "roots": [],
        "entries": [],
    }
    try:
        text = acl_manifest_file(paths).read_text(encoding="utf-8")
    except OSError:
        return empty

    header, roots, entries = {}, [], []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "\t" not in line and "=" in line:
            key, _, value = line.partition("=")
            header[key.strip()] = value.strip()
            continue
        fields = line.split("\t")
        if fields[0] == "root" and len(fields) >= 4:
            roots.append({"path": fields[1], "identity": fields[2], "mode": fields[3]})
        elif fields[0] == "entry" and len(fields) >= 7:
            entries.append(
                {
                    "path": fields[1],
                    "identity": fields[2],
                    "scope": fields[3],
                    "preexisting": fields[4] == "yes",
                    "previous": "" if fields[5] == "-" else fields[5],
                    "granted": fields[6],
                }
            )
    schema_version = _as_int(header.get("schema_version") or header.get("schema")) or 0
    return {
        "present": True,
        "supported": schema_version == ACL_MANIFEST_SCHEMA_VERSION,
        "schema_version": schema_version,
        "user": header.get("user", ""),
        "installation_id": header.get("installation_id", ""),
        "roots": roots,
        "entries": entries,
    }


def quarantine_directory():
    return Path(
        os.environ.get("EMS_APPLIANCE_QUARANTINE_DIR") or "/var/backups/ems-appliance-manager"
    )
