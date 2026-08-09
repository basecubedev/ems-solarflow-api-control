# SPDX-License-Identifier: AGPL-3.0-or-later
"""The required authority of a persisted operation, per operation kind.

An operation record outlives the request that created it: it is confirmed
later, and executed by an agent that may have restarted in between. What it
carries is therefore the only authority the mutation has. A record that lost a
field cannot be repaired by guessing — a missing hash is not "no validation
requested", and a missing recovery digest is not "nothing to restore".

Every record written by this version carries ``schema_version``. Records
without it, or with a version this code does not know, are visible for
diagnostics and refused for execution.
"""

import hashlib
import json
import secrets

from appliance.validation import (
    ValidationError,
    build_digest_ref,
    validate_architecture,
    validate_digest,
    validate_image_repository,
    validate_release_tag,
)

OPERATION_SCHEMA_VERSION = 2

CODE_REPLAN = "operation_plan_requires_replanning"
CODE_PLAN_CHANGED = "operation_plan_changed"

TYPE_ADMIN_INSTALL = "admin.install"
TYPE_ADMIN_ROLLBACK = "admin.rollback"
TYPE_ADMIN_LIFECYCLE = "admin.lifecycle"
TYPE_ADMIN_REPAIR = "admin.repair"

# Every field a confirmed Admin replacement is bound to. The recovery identity
# is validated separately because its presence depends on an Admin existing.
# Only an install resolves a new image, so only an install carries the
# architecture it validated; a rollback targets an image this appliance already
# installed and validated once.
ADMIN_REPLACEMENT_FIELDS = (
    "repository",
    "tag",
    "digest",
    "reference",
    "compose_file",
    "compose_hash",
    "environment_file",
    "environment_hash",
)

REQUIRED_FIELDS = {
    TYPE_ADMIN_INSTALL: ADMIN_REPLACEMENT_FIELDS + ("architecture",),
    TYPE_ADMIN_ROLLBACK: ADMIN_REPLACEMENT_FIELDS,
    TYPE_ADMIN_LIFECYCLE: ("action",),
    TYPE_ADMIN_REPAIR: ("actions",),
}

# A rollback digest is validated by the known-good record check, which reports
# its own stable ``invalid_known_good_record`` code.
DIGEST_FIELDS = {
    TYPE_ADMIN_INSTALL: ("digest",),
}

ABSOLUTE_PATH_FIELDS = {
    TYPE_ADMIN_INSTALL: ("compose_file", "environment_file"),
    TYPE_ADMIN_ROLLBACK: ("compose_file", "environment_file"),
}

RECOVERY_REQUIRED = (TYPE_ADMIN_INSTALL, TYPE_ADMIN_ROLLBACK)

RECOVERY_SCHEMA_VERSION = 1

# What restoring the previous Admin needs, captured before it is touched.
RECOVERY_FIELDS = (
    "repository",
    "digest",
    "reference",
    "version",
    "compose_file",
    "compose_hash",
    "environment_file",
    "environment_hash",
    "healthy",
)
RECOVERY_PATH_FIELDS = ("compose_file", "environment_file")
RECOVERY_FINGERPRINT_FIELDS = (
    "compose_file",
    "compose_hash",
    "environment_file",
    "environment_hash",
)
RECOVERY_BOOLEAN_FIELDS = ("admin_present", "healthy")

HASH_FIELDS = ("compose_hash", "environment_hash")
HASH_LENGTH = 64

# The tag a known-good record carries when the Admin that was running could not
# be named. It is metadata, never resolved into an image reference — the digest
# is what a rollback deploys.
UNKNOWN_VERSION = "unknown"

AUTHORITY_SCHEMA_VERSION = 1
AUTHORITY_FIELD = "authority"
AUTHORITY_PLAN_FIELD = "authority_plan"
AUTHORITY_FIELDS = (AUTHORITY_FIELD, AUTHORITY_PLAN_FIELD)


class OperationSchemaError(Exception):
    def __init__(self, message, *, code=CODE_REPLAN):
        super().__init__(message)
        self.code = code
        self.message = message


def _present(target, field):
    value = target.get(field)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return True
    return True


def _canonical(value):
    """A stable text form, so the same record always hashes to the same value."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hashed(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _target_core(target):
    return {
        key: value
        for key, value in dict(target or {}).items()
        if key not in AUTHORITY_FIELDS
    }


def plan_fingerprint(plan):
    return _hashed(_target_core(plan))


def authority_fingerprint(*, operation_id, operation_type, schema_version, target, plan_hash):
    """One hash over everything the confirmation was shown for.

    The plan is folded in as its own hash rather than by value, because a
    failed-and-retried operation no longer carries the rendered plan: the
    authority has to be checkable from the record alone.
    """

    return _hashed(
        {
            "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
            "operation_id": str(operation_id),
            "type": str(operation_type),
            "schema_version": int(schema_version or 0),
            "target": _target_core(target),
            "plan": str(plan_hash or ""),
        }
    )


def seal(operation, plan):
    """The authority values to store once the planner has finalised the target."""

    plan_hash = plan_fingerprint(plan)
    return {
        AUTHORITY_PLAN_FIELD: plan_hash,
        AUTHORITY_FIELD: authority_fingerprint(
            operation_id=operation.operation_id,
            operation_type=operation.type,
            schema_version=operation.schema_version,
            target=operation.requested_target,
            plan_hash=plan_hash,
        ),
    }


def verify_authority(operation):
    """Is this still the plan the operator confirmed?

    An integrity check, not a defence against a root that can rewrite every
    field: it catches a partial write, a hand-edited record and a mutation that
    changed the target after the plan was rendered.
    """

    target = operation.requested_target or {}
    stored = str(target.get(AUTHORITY_FIELD) or "")
    if not stored:
        raise OperationSchemaError(
            "this plan carries no confirmation authority; plan again", code=CODE_PLAN_CHANGED
        )
    expected = authority_fingerprint(
        operation_id=operation.operation_id,
        operation_type=operation.type,
        schema_version=operation.schema_version,
        target=target,
        plan_hash=target.get(AUTHORITY_PLAN_FIELD),
    )
    if not secrets.compare_digest(stored, expected):
        raise OperationSchemaError(
            "this plan is not the one that was confirmed; plan again",
            code=CODE_PLAN_CHANGED,
        )
    return True


def _require_hash(value, label):
    text = str(value or "")
    if len(text) != HASH_LENGTH or any(char not in "0123456789abcdef" for char in text):
        raise OperationSchemaError(f"the {label} is not a canonical sha256 digest")
    return text


def validate_admin_target(target, *, repositories=(), architectures=(), require_release_tag=True):
    """The immutable identity an Admin replacement is allowed to deploy.

    The reference is what reaches Docker, so it may not be validated separately
    from the repository and digest that were recorded beside it: a record whose
    reference names a different image is not a record to execute from.
    """

    repository = str(target.get("repository") or "")
    digest = str(target.get("digest") or "")
    reference = str(target.get("reference") or "")
    tag = str(target.get("tag") or "")

    try:
        validate_digest(digest)
        if repositories:
            validate_image_repository(repository, repositories)
        else:
            validate_image_repository(repository, [repository])
        if require_release_tag or tag != UNKNOWN_VERSION:
            validate_release_tag(tag)
        architecture = str(target.get("architecture") or "")
        if architecture and architectures:
            validate_architecture(architecture, architectures)
    except ValidationError as exc:
        raise OperationSchemaError(f"the plan target is not deployable: {exc.message}")

    if reference != build_digest_ref(repository, digest):
        raise OperationSchemaError(
            "the plan reference does not name the recorded repository and digest"
        )
    for field in HASH_FIELDS:
        _require_hash(target.get(field), f"planned {field}")
    return True


def validate_recovery(recovery, target=None):
    """An Admin that exists must be restorable by an exact immutable identity.

    Every field is required because none of them can be recovered from the
    running system at execution time: that is the state the record exists to
    describe from before the mutation.
    """

    if not isinstance(recovery, dict):
        raise OperationSchemaError("the plan carries no recovery identity")

    version = recovery.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise OperationSchemaError("the recovery schema version is not a number; plan again")
    if version != RECOVERY_SCHEMA_VERSION:
        raise OperationSchemaError(
            f"the recovery identity was written for schema {version or 'none'}; plan again"
        )
    if "admin_present" not in recovery:
        raise OperationSchemaError("the recovery identity does not say whether an Admin exists")
    # "healthy" decides whether an automatic restore is expected to come back
    # up, so a string that merely looks truthy is not an answer.
    for field in RECOVERY_BOOLEAN_FIELDS:
        if field in recovery and not isinstance(recovery[field], bool):
            raise OperationSchemaError(f"the recovery {field} is not a boolean; plan again")
    if not recovery.get("admin_present"):
        return True

    missing = [field for field in RECOVERY_FIELDS if not _present(recovery, field)]
    if missing:
        raise OperationSchemaError(
            "the recovery identity is missing " + ", ".join(sorted(missing))
        )

    digest = str(recovery["digest"])
    try:
        validate_digest(digest)
    except ValidationError as exc:
        raise OperationSchemaError(f"the recovery digest is malformed: {exc.message}")

    repository = str(recovery["repository"])
    expected_reference = f"{repository}@{digest}"
    if str(recovery["reference"]) != expected_reference:
        raise OperationSchemaError(
            "the recovery reference does not name the recorded repository and digest"
        )

    for field in RECOVERY_PATH_FIELDS:
        if not str(recovery[field]).startswith("/"):
            raise OperationSchemaError(f"the recovery {field} is not an absolute path")

    for field in HASH_FIELDS:
        _require_hash(recovery.get(field), f"recovery {field}")

    # The recovery identity describes the deployment the plan is bound to. Two
    # different answers to "which compose file" is not a record to execute from.
    for field in RECOVERY_FINGERPRINT_FIELDS:
        planned = str((target or {}).get(field) or "")
        if planned and str(recovery[field]) != planned:
            raise OperationSchemaError(
                f"the recovery {field} is not the one the plan was made against"
            )
    return True


def validate_confirmation(operation, *, repositories=(), architectures=()):
    """Everything that has to hold before an operation may start.

    Checked at confirmation and again at execution: the record is durable and
    the two are separated by however long the operator took to decide.
    """

    if operation.type in REQUIRED_FIELDS:
        return validate_operation(
            operation, repositories=repositories, architectures=architectures
        )
    return verify_authority(operation)


def validate_operation(operation, *, repositories=(), architectures=()):
    """Raise ``OperationSchemaError`` unless the record may still be executed.

    The structure is judged first, so a record that lost a field says which one
    instead of only that it is no longer the plan that was confirmed.
    """

    version = getattr(operation, "schema_version", 0)
    if version != OPERATION_SCHEMA_VERSION:
        raise OperationSchemaError(
            f"this plan was written for operation schema {version or 'none'}; plan again"
        )

    # An unknown type has no known authority, so nothing can decide it is safe.
    required = REQUIRED_FIELDS.get(operation.type)
    if required is None:
        raise OperationSchemaError(
            f"this appliance does not know the operation type {operation.type!r}; plan again"
        )

    target = operation.requested_target or {}
    if not isinstance(target, dict):
        raise OperationSchemaError("the plan target is not a mapping")

    missing = [field for field in required if not _present(target, field)]
    if missing:
        raise OperationSchemaError("the plan is missing " + ", ".join(sorted(missing)))

    for field in DIGEST_FIELDS.get(operation.type, ()):  # noqa: B007 - names are the message
        try:
            validate_digest(str(target.get(field) or ""))
        except ValidationError as exc:
            raise OperationSchemaError(f"the planned {field} is malformed: {exc.message}")

    for field in ABSOLUTE_PATH_FIELDS.get(operation.type, ()):
        value = str(target.get(field) or "")
        if not value.startswith("/"):
            raise OperationSchemaError(f"the planned {field} is not an absolute path")

    if operation.type in RECOVERY_REQUIRED:
        validate_admin_target(
            target,
            repositories=repositories,
            architectures=architectures,
            require_release_tag=operation.type == TYPE_ADMIN_INSTALL,
        )
        validate_recovery(target.get("recovery"), target)
    return verify_authority(operation)
