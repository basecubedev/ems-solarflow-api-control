# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fixed agent operation allowlist.

A request names an operation and passes typed fields. There is no field that
carries a command, an executable, a filesystem path or an image reference: the
agent derives all of those from host configuration. An unknown operation, an
unknown field or a field that fails its typed validator is refused before any
handler runs.
"""

from dataclasses import dataclass

from appliance import validation
from appliance.validation import ValidationError

KIND_RELEASE_CHANNEL = "release_channel"
KIND_RELEASE_TAG = "release_tag"
KIND_BOOL = "bool"
KIND_CONTAINER = "container"
KIND_LOG_SOURCE = "log_source"
KIND_LINE_COUNT = "line_count"
KIND_OPERATION_ID = "operation_id"
KIND_TOKEN = "confirmation_token"
KIND_UPDATE_SCOPE = "update_scope"
KIND_REPAIR_ACTION = "repair_action"
KIND_LIFECYCLE_ACTION = "lifecycle_action"
KIND_ACCOUNT = "account"
KIND_PUBLIC_KEY = "public_key"
KIND_FINGERPRINT = "fingerprint"
KIND_SSID = "ssid"
KIND_WIFI_PASSPHRASE = "wifi_passphrase"
KIND_HOSTNAME = "hostname"
KIND_AUDIT_EVENT = "audit_event"
KIND_AUDIT_RESULT = "audit_result"
KIND_AUDIT_REASON = "audit_reason"
KIND_OS_RELEASE_ID = "os_release_id"


class ProtocolError(Exception):
    def __init__(self, code, message, *, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool = True
    default: object = None


@dataclass(frozen=True)
class OperationSpec:
    name: str
    mutating: bool = False
    takes_lock: bool = False
    fields: tuple = ()
    summary: str = ""


def _spec(name, *, mutating=False, takes_lock=False, fields=(), summary=""):
    return OperationSpec(
        name=name, mutating=mutating, takes_lock=takes_lock, fields=tuple(fields), summary=summary
    )


READ_ONLY_OPERATIONS = (
    _spec("status.get", summary="Aggregated appliance overview"),
    _spec("system.get", summary="Host, OS and hardware status"),
    _spec("network.get", summary="Interfaces, addresses, DNS and mDNS"),
    _spec("docker.get", summary="Docker daemon and managed containers"),
    _spec("admin.get", summary="Admin container, image and health"),
    _spec("updates.get", summary="Package update and reboot state"),
    _spec("ssh.get", summary="SSH service state and authorized keys"),
    _spec("backup.get", summary="Backup account and export paths"),
    _spec("operations.list", summary="Recent appliance operations"),
    _spec(
        "operations.get",
        fields=(Field("operation_id", KIND_OPERATION_ID),),
        summary="One operation record",
    ),
    _spec(
        "logs.read",
        fields=(
            Field("source", KIND_LOG_SOURCE),
            Field("lines", KIND_LINE_COUNT, required=False, default=validation.DEFAULT_LOG_LINES),
        ),
        summary="Bounded, redacted log output",
    ),
    _spec("network.wifi.scan", summary="Visible WLAN networks"),
    _spec("admin.releases", summary="Installable Admin versions"),
    _spec("ab.status", summary="A/B slot, persistence and OS release state"),
)

MUTATING_OPERATIONS = (
    _spec(
        "admin.plan_install",
        mutating=True,
        takes_lock=True,
        fields=(
            Field("channel", KIND_RELEASE_CHANNEL),
            Field("tag", KIND_RELEASE_TAG, required=False),
            Field("reinstall", KIND_BOOL, required=False, default=False),
        ),
        summary="Plan an Admin installation",
    ),
    _spec("admin.plan_rollback", mutating=True, takes_lock=True, summary="Plan an Admin rollback"),
    _spec("admin.plan_repair", mutating=True, takes_lock=True, summary="Plan an Admin repair"),
    _spec(
        "admin.plan_lifecycle",
        mutating=True,
        takes_lock=True,
        fields=(Field("action", KIND_LIFECYCLE_ACTION),),
        summary="Plan a start/stop/restart",
    ),
    _spec(
        "updates.plan",
        mutating=True,
        takes_lock=True,
        fields=(Field("scope", KIND_UPDATE_SCOPE),),
        summary="Plan an OS package installation",
    ),
    _spec(
        "updates.plan_repair",
        mutating=True,
        takes_lock=True,
        fields=(Field("action", KIND_REPAIR_ACTION),),
        summary="Plan a package-manager repair",
    ),
    _spec(
        "network.wifi.plan",
        mutating=True,
        takes_lock=True,
        fields=(
            Field("ssid", KIND_SSID),
            Field("passphrase", KIND_WIFI_PASSPHRASE, required=False, default=""),
            Field("hidden", KIND_BOOL, required=False, default=False),
        ),
        summary="Plan a WLAN change with automatic revert",
    ),
    _spec(
        "network.hostname.plan",
        mutating=True,
        takes_lock=True,
        fields=(Field("hostname", KIND_HOSTNAME),),
        summary="Plan a hostname change",
    ),
    _spec("system.plan_reboot", mutating=True, takes_lock=True, summary="Plan a host reboot"),
    _spec("system.plan_shutdown", mutating=True, takes_lock=True, summary="Plan a host shutdown"),
    _spec(
        "ssh.plan_service",
        mutating=True,
        takes_lock=True,
        fields=(Field("enabled", KIND_BOOL),),
        summary="Plan enabling or disabling SSH",
    ),
    _spec(
        "ssh.plan_key_add",
        mutating=True,
        takes_lock=True,
        fields=(Field("account", KIND_ACCOUNT), Field("public_key", KIND_PUBLIC_KEY)),
        summary="Plan adding an SSH public key",
    ),
    _spec(
        "ssh.plan_key_remove",
        mutating=True,
        takes_lock=True,
        fields=(Field("account", KIND_ACCOUNT), Field("fingerprint", KIND_FINGERPRINT)),
        summary="Plan removing an SSH public key",
    ),
    _spec(
        "ssh.plan_revoke_all",
        mutating=True,
        takes_lock=True,
        fields=(Field("account", KIND_ACCOUNT),),
        summary="Plan revoking every SSH key of an account",
    ),
    _spec("support.plan_archive", mutating=True, takes_lock=True, summary="Plan a support archive"),
    # The browser names a release and nothing else. Every device path, PARTUUID,
    # download URL, signing key and partition number comes from the root-owned
    # configuration, the signed manifest or verified layout discovery.
    _spec(
        "ab.plan_update",
        mutating=True,
        takes_lock=True,
        fields=(
            Field("release_id", KIND_OS_RELEASE_ID),
            Field("repair", KIND_BOOL, required=False, default=False),
        ),
        summary="Plan an A/B operating-system update",
    ),
    _spec(
        "ab.plan_rollback",
        mutating=True,
        takes_lock=True,
        summary="Plan a rollback to the previous known-good slot",
    ),
    _spec(
        "ab.acknowledge",
        mutating=True,
        fields=(Field("operation_id", KIND_OPERATION_ID),),
        summary="Acknowledge an A/B result or an observed fallback",
    ),
    # The web service owns authentication but not the audit trail: it may only
    # ask the agent to record one of a fixed set of events, with no free-form
    # field of its own.
    _spec(
        "audit.record_web_event",
        mutating=True,
        fields=(
            Field("event", KIND_AUDIT_EVENT),
            Field("result", KIND_AUDIT_RESULT),
            Field("reason", KIND_AUDIT_REASON, required=False, default=""),
        ),
        summary="Record a web authentication event in the audit log",
    ),
    _spec(
        "operations.execute",
        mutating=True,
        fields=(
            Field("operation_id", KIND_OPERATION_ID),
            Field("confirmation_token", KIND_TOKEN),
        ),
        summary="Confirm and run a planned operation",
    ),
    _spec(
        "operations.cancel",
        mutating=True,
        fields=(Field("operation_id", KIND_OPERATION_ID),),
        summary="Cancel a planned or failed operation",
    ),
    _spec(
        "operations.acknowledge",
        mutating=True,
        fields=(Field("operation_id", KIND_OPERATION_ID),),
        summary="Acknowledge a finished operation",
    ),
)

OPERATIONS = {spec.name: spec for spec in READ_ONLY_OPERATIONS + MUTATING_OPERATIONS}

RESERVED_REQUEST_KEYS = frozenset({"operation"})


class ValidationContext:
    """Host-owned value sets a request is validated against."""

    def __init__(self, config):
        self.config = config

    @property
    def accounts(self):
        return tuple(self.config.ssh_key_accounts)

    @property
    def containers(self):
        return tuple(self.config.managed_containers)

    @property
    def allow_prerelease(self):
        return bool(self.config.images.allow_prerelease)


def _coerce(field, value, context):
    kind = field.kind
    if kind == KIND_BOOL:
        return validation.validate_boolean(value, field=field.name)
    if kind == KIND_RELEASE_CHANNEL:
        return validation.validate_release_channel(value)
    if kind == KIND_RELEASE_TAG:
        tag = validation.validate_release_tag(value)
        if validation.is_prerelease_tag(tag) and not context.allow_prerelease:
            raise ValidationError(
                "prerelease_not_allowed", f"{tag} is a prerelease and is not enabled on this host"
            )
        return tag
    if kind == KIND_CONTAINER:
        return validation.validate_container_name(value, context.containers)
    if kind == KIND_LOG_SOURCE:
        return validation.validate_log_source(value)
    if kind == KIND_LINE_COUNT:
        return validation.validate_line_count(value)
    if kind == KIND_OPERATION_ID:
        return validation.validate_operation_id(value)
    if kind == KIND_TOKEN:
        return validation.validate_confirmation_token(value)
    if kind == KIND_UPDATE_SCOPE:
        return validation.validate_update_scope(value)
    if kind == KIND_REPAIR_ACTION:
        return validation.validate_package_repair_action(value)
    if kind == KIND_LIFECYCLE_ACTION:
        return validation.validate_lifecycle_action(value)
    if kind == KIND_ACCOUNT:
        return validation.validate_account(value, context.accounts)
    if kind == KIND_PUBLIC_KEY:
        from appliance.sshkeys import validate_public_key

        return validate_public_key(value).line
    if kind == KIND_FINGERPRINT:
        return validation.validate_fingerprint(value)
    if kind == KIND_SSID:
        return validation.validate_ssid(value)
    if kind == KIND_WIFI_PASSPHRASE:
        return validation.validate_wifi_passphrase(value)
    if kind == KIND_HOSTNAME:
        return validation.validate_hostname(value)
    if kind == KIND_AUDIT_EVENT:
        return validation.validate_web_audit_event(value)
    if kind == KIND_AUDIT_RESULT:
        return validation.validate_audit_result(value)
    if kind == KIND_AUDIT_REASON:
        return validation.validate_web_audit_reason(value)
    if kind == KIND_OS_RELEASE_ID:
        return validation.validate_os_release_id(value)
    raise ProtocolError("invalid_field_kind", f"unknown field kind {kind!r}", field=field.name)


def validate_request(payload, context):
    """Return ``(spec, args)`` for a well-formed request or raise ``ProtocolError``."""

    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "request must be a JSON object")

    name = payload.get("operation")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError("invalid_request", "request must name an operation")

    spec = OPERATIONS.get(name.strip())
    if spec is None:
        raise ProtocolError("unknown_operation", f"{name.strip()!r} is not an allowed operation")

    known = {field.name for field in spec.fields}
    for key in payload:
        if key in RESERVED_REQUEST_KEYS:
            continue
        if key not in known:
            raise ProtocolError("unknown_field", f"{key!r} is not a field of {spec.name}", field=key)

    args = {}
    for field in spec.fields:
        if field.name not in payload:
            if field.required:
                raise ProtocolError(
                    "missing_field", f"{spec.name} requires {field.name}", field=field.name
                )
            args[field.name] = field.default
            continue
        try:
            args[field.name] = _coerce(field, payload[field.name], context)
        except ValidationError as exc:
            raise ProtocolError(exc.code, exc.message, field=field.name)

    return spec, args
