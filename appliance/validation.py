# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed value validation for every Appliance Manager input.

Nothing here touches the filesystem, the network or a subprocess. The agent and
the web service both call these validators, so a value that reaches a command
line has been checked at least twice and always against the same rule.
"""

import re

MAX_TAG_LENGTH = 64
MAX_HOSTNAME_LENGTH = 63
MAX_SSID_LENGTH = 32
MAX_PUBLIC_KEY_LENGTH = 8192
MIN_WIFI_PASSPHRASE_LENGTH = 8
MAX_WIFI_PASSPHRASE_LENGTH = 63
MAX_LOG_LINES = 2000
DEFAULT_LOG_LINES = 200

RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.]{0,31})?$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")
HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")

CHANNEL_LATEST_STABLE = "latest_stable"
CHANNEL_CURRENT = "current"
CHANNEL_PREVIOUS_KNOWN_GOOD = "previous_known_good"
CHANNEL_EXACT = "exact"
RELEASE_CHANNELS = (
    CHANNEL_LATEST_STABLE,
    CHANNEL_CURRENT,
    CHANNEL_PREVIOUS_KNOWN_GOOD,
    CHANNEL_EXACT,
)

UPDATE_SCOPE_SECURITY = "security"
UPDATE_SCOPE_ALL = "all"
UPDATE_SCOPES = (UPDATE_SCOPE_SECURITY, UPDATE_SCOPE_ALL)

LIFECYCLE_START = "start"
LIFECYCLE_STOP = "stop"
LIFECYCLE_RESTART = "restart"
LIFECYCLE_ACTIONS = (LIFECYCLE_START, LIFECYCLE_STOP, LIFECYCLE_RESTART)

PACKAGE_REPAIR_CONFIGURE = "configure_pending"
PACKAGE_REPAIR_FIX_BROKEN = "fix_broken"
PACKAGE_REPAIR_REFRESH_INDEX = "refresh_index"
PACKAGE_REPAIR_ACTIONS = (
    PACKAGE_REPAIR_CONFIGURE,
    PACKAGE_REPAIR_FIX_BROKEN,
    PACKAGE_REPAIR_REFRESH_INDEX,
)

LOG_SOURCE_APPLIANCE_WEB = "appliance_web"
LOG_SOURCE_APPLIANCE_AGENT = "appliance_agent"
LOG_SOURCE_OPERATIONS = "operations"
LOG_SOURCE_AUDIT = "audit"
LOG_SOURCE_ADMIN_CONTAINER = "admin_container"
LOG_SOURCE_EMS_CONTAINER = "ems_container"
LOG_SOURCE_DOCKER_DAEMON = "docker_daemon"
LOG_SOURCE_BOOT = "boot"
LOG_SOURCE_PACKAGES = "packages"
LOG_SOURCES = (
    LOG_SOURCE_APPLIANCE_WEB,
    LOG_SOURCE_APPLIANCE_AGENT,
    LOG_SOURCE_OPERATIONS,
    LOG_SOURCE_AUDIT,
    LOG_SOURCE_ADMIN_CONTAINER,
    LOG_SOURCE_EMS_CONTAINER,
    LOG_SOURCE_DOCKER_DAEMON,
    LOG_SOURCE_BOOT,
    LOG_SOURCE_PACKAGES,
)

WEB_AUDIT_EVENTS = (
    "login.success",
    "login.failure",
    "logout",
    "password.change",
    "password.reset",
)

AUDIT_RESULTS = ("success", "failure", "denied")

WEB_AUDIT_REASONS = (
    "",
    "first_password",
    "invalid_password",
    "password_changed",
    "rate_limited",
    "session_ended",
)

MAX_SOURCE_IP_LENGTH = 64
MAX_ACTOR_LENGTH = 64
SOURCE_IP_RE = re.compile(r"^[0-9A-Fa-f:.%\[\]]{1,64}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")

REQUIRED_OCI_LABELS = (
    "org.opencontainers.image.source",
    "org.opencontainers.image.version",
    "org.opencontainers.image.revision",
    "org.opencontainers.image.created",
)

SUPPORTED_KEY_TYPES = (
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)


class ValidationError(ValueError):
    """A typed input failed validation. ``code`` is a stable public error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _require_text(value, code, *, max_length, allow_empty=False):
    if not isinstance(value, str):
        raise ValidationError(code, "value must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ValidationError(code, "value must not be empty")
    if len(text) > max_length:
        raise ValidationError(code, f"value exceeds {max_length} characters")
    if any(char in text for char in "\r\n\x00"):
        raise ValidationError(code, "value must not contain control characters")
    return text


def validate_release_tag(value):
    """Accept a concrete release tag only; mutable channel names are rejected."""

    text = _require_text(value, "invalid_release_tag", max_length=MAX_TAG_LENGTH)
    if not RELEASE_TAG_RE.match(text):
        raise ValidationError("invalid_release_tag", f"{text!r} is not a supported release tag")
    return text


def is_prerelease_tag(tag):
    return "-" in validate_release_tag(tag)


def normalize_version(value):
    """Compare tags and OCI version labels without their optional ``v`` prefix."""

    text = str(value or "").strip()
    return text[1:] if text.startswith("v") else text


def validate_release_channel(value):
    text = _require_text(value, "invalid_release_channel", max_length=MAX_TAG_LENGTH)
    if text not in RELEASE_CHANNELS:
        raise ValidationError("invalid_release_channel", f"{text!r} is not a release channel")
    return text


def validate_image_repository(value, allowed_repositories):
    """Only a repository from the host allowlist may reach a Docker command."""

    text = _require_text(value, "invalid_image_repository", max_length=255)
    if not IMAGE_REPOSITORY_RE.match(text):
        raise ValidationError("invalid_image_repository", f"{text!r} is not a repository reference")
    if text not in tuple(allowed_repositories):
        raise ValidationError(
            "image_repository_not_allowed",
            f"{text!r} is not in the host image allowlist",
        )
    return text


def validate_digest(value):
    text = _require_text(value, "invalid_digest", max_length=128)
    if not DIGEST_RE.match(text):
        raise ValidationError("invalid_digest", "digest must be sha256:<64 hex characters>")
    return text


def build_image_ref(repository, tag):
    return f"{validate_image_repository(repository, [repository])}:{validate_release_tag(tag)}"


def build_digest_ref(repository, digest):
    repo = validate_image_repository(repository, [repository])
    return f"{repo}@{validate_digest(digest)}"


def validate_architecture(image_architecture, supported):
    text = _require_text(image_architecture, "invalid_architecture", max_length=32)
    if text not in tuple(supported):
        raise ValidationError(
            "architecture_mismatch",
            f"image architecture {text!r} is not supported by this appliance",
        )
    return text


def validate_oci_labels(labels, *, requested_tag, expected_source, legacy_exempt_tags=()):
    """Prove that a pulled image is the release the operator asked for.

    A missing label set is only tolerated for tags the host explicitly lists as
    legacy; everything else fails closed so an unlabelled image can never
    replace a running Admin.
    """

    if not isinstance(labels, dict):
        raise ValidationError("image_labels_missing", "image labels could not be inspected")

    tag = validate_release_tag(requested_tag)
    missing = [name for name in REQUIRED_OCI_LABELS if not str(labels.get(name) or "").strip()]
    if missing:
        if tag in tuple(legacy_exempt_tags):
            return {"legacy_exempt": True, "missing_labels": missing}
        raise ValidationError(
            "image_labels_missing",
            "image is missing required OCI labels: " + ", ".join(missing),
        )

    source = str(labels["org.opencontainers.image.source"]).strip()
    if expected_source and source.rstrip("/") != str(expected_source).rstrip("/"):
        raise ValidationError(
            "image_source_mismatch",
            f"image source {source!r} does not match the allowlisted project source",
        )

    version = str(labels["org.opencontainers.image.version"]).strip()
    if normalize_version(version) != normalize_version(tag):
        raise ValidationError(
            "image_version_mismatch",
            f"image version label {version!r} does not match requested tag {tag!r}",
        )

    return {"legacy_exempt": False, "missing_labels": []}


def validate_hostname(value):
    text = _require_text(value, "invalid_hostname", max_length=MAX_HOSTNAME_LENGTH)
    lowered = text.lower()
    if not HOSTNAME_RE.match(lowered):
        raise ValidationError(
            "invalid_hostname",
            "hostname must be an RFC 1123 label of letters, digits and hyphens",
        )
    return lowered


def validate_container_name(value, allowed_containers):
    text = _require_text(value, "invalid_container", max_length=64)
    if not CONTAINER_NAME_RE.match(text):
        raise ValidationError("invalid_container", f"{text!r} is not a container name")
    if text not in tuple(allowed_containers):
        raise ValidationError(
            "container_not_allowed", f"{text!r} is not an appliance-managed container"
        )
    return text


def validate_account(value, allowed_accounts):
    text = _require_text(value, "invalid_account", max_length=32)
    if not ACCOUNT_RE.match(text):
        raise ValidationError("invalid_account", f"{text!r} is not a host account name")
    if text not in tuple(allowed_accounts):
        raise ValidationError(
            "account_not_allowed", f"{text!r} is not an appliance-managed host account"
        )
    return text


def validate_lifecycle_action(value):
    text = _require_text(value, "invalid_lifecycle_action", max_length=16)
    if text not in LIFECYCLE_ACTIONS:
        raise ValidationError("invalid_lifecycle_action", f"{text!r} is not a lifecycle action")
    return text


def validate_log_source(value):
    text = _require_text(value, "invalid_log_source", max_length=32)
    if text not in LOG_SOURCES:
        raise ValidationError("invalid_log_source", f"{text!r} is not a readable log source")
    return text


def validate_line_count(value, default=DEFAULT_LOG_LINES):
    if value is None:
        return default
    try:
        lines = int(value)
    except (TypeError, ValueError):
        raise ValidationError("invalid_line_count", "line count must be an integer")
    if lines < 1:
        raise ValidationError("invalid_line_count", "line count must be positive")
    return min(lines, MAX_LOG_LINES)


def validate_update_scope(value):
    text = _require_text(value, "invalid_update_scope", max_length=16)
    if text not in UPDATE_SCOPES:
        raise ValidationError("invalid_update_scope", f"{text!r} is not an update scope")
    return text


def validate_package_repair_action(value):
    text = _require_text(value, "invalid_repair_action", max_length=32)
    if text not in PACKAGE_REPAIR_ACTIONS:
        raise ValidationError("invalid_repair_action", f"{text!r} is not a repair action")
    return text


# An OS release is named, never located. The browser may send this identifier and
# nothing else about an artifact; every URL, path, device, key and checksum comes
# from the root-owned release configuration or from the signed manifest.
OS_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def validate_os_release_id(value):
    text = str(value or "").strip()
    if not OS_RELEASE_ID.match(text):
        raise ValidationError(
            "invalid_release_id", "an OS release id may only contain letters, digits, . _ + and -"
        )
    return text


def validate_operation_id(value):
    text = _require_text(value, "invalid_operation_id", max_length=32)
    if not OPERATION_ID_RE.match(text):
        raise ValidationError("invalid_operation_id", "operation id must be 32 hex characters")
    return text


def validate_confirmation_token(value):
    text = _require_text(value, "invalid_confirmation_token", max_length=128)
    if not TOKEN_RE.match(text):
        raise ValidationError("invalid_confirmation_token", "confirmation token is malformed")
    return text


def validate_fingerprint(value):
    text = _require_text(value, "invalid_fingerprint", max_length=64)
    if not FINGERPRINT_RE.match(text):
        raise ValidationError("invalid_fingerprint", "fingerprint must be SHA256:<base64>")
    return text


def validate_ssid(value):
    if not isinstance(value, str):
        raise ValidationError("invalid_ssid", "SSID must be a string")
    if not value or len(value.encode("utf-8")) > MAX_SSID_LENGTH:
        raise ValidationError("invalid_ssid", "SSID must be 1..32 bytes")
    if any(char in value for char in "\r\n\x00"):
        raise ValidationError("invalid_ssid", "SSID must not contain control characters")
    return value


def validate_wifi_passphrase(value, *, allow_open=True):
    if value in (None, "") and allow_open:
        return ""
    if not isinstance(value, str):
        raise ValidationError("invalid_wifi_passphrase", "passphrase must be a string")
    if any(char in value for char in "\r\n\x00"):
        raise ValidationError("invalid_wifi_passphrase", "passphrase must not contain newlines")
    if not MIN_WIFI_PASSPHRASE_LENGTH <= len(value) <= MAX_WIFI_PASSPHRASE_LENGTH:
        raise ValidationError(
            "invalid_wifi_passphrase",
            f"passphrase must be {MIN_WIFI_PASSPHRASE_LENGTH}..{MAX_WIFI_PASSPHRASE_LENGTH} characters",
        )
    return value


def validate_web_audit_event(value):
    """Only the fixed authentication events the web service may report."""

    text = _require_text(value, "invalid_audit_event", max_length=32)
    if text not in WEB_AUDIT_EVENTS:
        raise ValidationError("invalid_audit_event", f"{text!r} is not a web audit event")
    return text


def validate_audit_result(value):
    text = _require_text(value, "invalid_audit_result", max_length=16)
    if text not in AUDIT_RESULTS:
        raise ValidationError("invalid_audit_result", f"{text!r} is not an audit result")
    return text


def validate_web_audit_reason(value):
    if value in (None, ""):
        return ""
    text = _require_text(value, "invalid_audit_reason", max_length=32)
    if text not in WEB_AUDIT_REASONS:
        raise ValidationError("invalid_audit_reason", f"{text!r} is not an audit reason")
    return text


def sanitize_source_ip(value):
    """Bound the caller-supplied source address before it reaches a log line."""

    text = str(value or "").strip()
    if not text or not SOURCE_IP_RE.match(text):
        return ""
    return text[:MAX_SOURCE_IP_LENGTH]


def sanitize_actor(value):
    text = str(value or "").strip()
    if not text or not ACTOR_RE.match(text):
        return ""
    return text[:MAX_ACTOR_LENGTH]


def validate_boolean(value, *, field="value"):
    if isinstance(value, bool):
        return value
    raise ValidationError("invalid_boolean", f"{field} must be a boolean")
