# SPDX-License-Identifier: AGPL-3.0-or-later
"""Secret redaction and output bounding for logs, audit entries and archives.

Every log line, command output and support-archive file passes through here
before it reaches a browser or a downloadable artifact.
"""

import re

REDACTED = "***"
DEFAULT_MAX_LINES = 500
DEFAULT_MAX_BYTES = 256 * 1024

_SECRET_KEYS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "psk",
    "private_key",
    "client_secret",
    "session",
    "cookie",
)

# Secret on their own, wherever they appear in a compound name: "csrf_token"
# and "current_password" name exactly what "token" and "password" do.
_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "apikey",
        "authorization",
        "psk",
        "session",
        "cookie",
    }
)

_KEYS = "|".join(re.escape(key) for key in _SECRET_KEYS)
# A qualifier in front of the name does not make the value public, and \b
# cannot see the boundary in "csrf_token" because "_" is a word character.
_QUALIFIED = r"[a-z0-9_]*(?:" + _KEYS + r")"

_ASSIGNMENT = re.compile(r"(?i)\b(" + _QUALIFIED + r")(\s*[:=]\s*|\s+)([^\s,;\"']+)")
_QUOTED_ASSIGNMENT = re.compile(r"(?i)\b(" + _QUALIFIED + r")(\s*[:=]\s*)([\"'])[^\"']*\3")
_JSON_FIELD = re.compile(r"(?i)(\"(?:" + _QUALIFIED + r")\"\s*:\s*)\"[^\"]*\"")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_PUBLIC_KEY_BODY = re.compile(r"\b((?:ssh|sk|ecdsa)-[A-Za-z0-9@.-]+)\s+([A-Za-z0-9+/=]{20,})")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_text(value):
    """Replace credential-looking values in free-form text."""

    if value is None:
        return ""
    text = str(value)
    text = _PRIVATE_KEY_BLOCK.sub("-----BEGIN PRIVATE KEY----- " + REDACTED, text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JSON_FIELD.sub(lambda m: f'{m.group(1)}"{REDACTED}"', text)
    text = _QUOTED_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}", text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _PUBLIC_KEY_BODY.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return text


def _is_secret_key(key):
    """Whether a mapping key names a secret, qualified or not.

    ``key`` alone is deliberately not a secret word: a fingerprint and a
    public key are the evidence an operator compares against the appliance,
    so ``device_key`` and ``public_key`` stay readable while ``api_key`` and
    ``private_key`` do not.
    """

    normalised = key.strip().lower().replace("-", "_")
    if normalised in _SECRET_KEYS:
        return True
    parts = normalised.split("_")
    if any(part in _SECRET_WORDS for part in parts):
        return True
    return any(f"{first}_{second}" in _SECRET_KEYS for first, second in zip(parts, parts[1:]))


def redact_mapping(value):
    """Recursively redact secret-looking keys in a JSON-shaped structure."""

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact_mapping(item)
        return result
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def clamp_log(text, *, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES):
    """Bound log output by line count and byte size, keeping the newest lines."""

    raw = "" if text is None else str(text)
    lines = raw.splitlines()
    truncated_lines = len(lines) > max_lines
    if truncated_lines:
        lines = lines[-max_lines:]
    body = "\n".join(lines)
    encoded = body.encode("utf-8", errors="replace")
    truncated_bytes = len(encoded) > max_bytes
    if truncated_bytes:
        encoded = encoded[-max_bytes:]
        body = encoded.decode("utf-8", errors="replace")
        newline = body.find("\n")
        if newline != -1:
            body = body[newline + 1 :]
    return {
        "text": body,
        "lines": len(body.splitlines()),
        "truncated": truncated_lines or truncated_bytes,
    }


def bounded_redacted_log(text, *, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES):
    clamped = clamp_log(text, max_lines=max_lines, max_bytes=max_bytes)
    clamped["text"] = redact_text(clamped["text"])
    return clamped
