# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single policy for which config keys are secret and how they may appear.

Admin has three places that must decide "is this value safe to hand out": the
MQTT draft sanitizer (strip), the Maintenance browser view (redact) and the
Setup mutation fingerprint (digest). Each one used to carry its own marker
list, so a new credential field had to be remembered three times.

The vocabulary lives here once. The *purposes* stay distinct, because they are
genuinely different questions:

``SCOPE_DRAFT``
    a fragment travelling browser → config. A broker login is stripped, because
    the browser has no business round-tripping one, and the server-issued
    identity tokens are stripped with it — the server recomputes them.

``SCOPE_BROWSER_VIEW``
    a config copy travelling config → browser. The account-scoped write target
    (``product_key``) is masked here on top of the credentials, while the
    credential *reference*, the presence flag and the equality tokens stay
    readable, because the UI renders keep/clear/set controls from them.

``SCOPE_FINGERPRINT``
    a canonical digest of a pending mutation. The whole body is hashed anyway;
    per-key digesting keeps raw credentials out of the intermediate structure.

Catalog-backed fields do not guess at all: an explicit ``risk``/``type`` in the
central config catalog is authoritative and checked first.
"""

from ems.device_identity import (
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
    PHYSICAL_IDENTITY_TOKEN_FIELD,
)

SCOPE_DRAFT = "draft"
SCOPE_BROWSER_VIEW = "browser_view"
SCOPE_FINGERPRINT = "fingerprint"

CLASS_SECRET = "secret"
CLASS_CREDENTIAL_REFERENCE = "credential_reference"
CLASS_PRESENCE_FLAG = "presence_flag"
CLASS_PUBLIC_IDENTITY_TOKEN = "public_identity_token"
CLASS_SENSITIVE_IDENTITY = "sensitive_identity"
CLASS_PUBLIC = "public"

# The placeholder a redacted view shows. Never a usable value, and never
# accepted back as identity evidence.
REDACTED_PLACEHOLDER = "••••"

# Credential markers every scope treats as secret.
_SECRET_MARKERS = (
    "password",
    "passphrase",
    "token",
    "secret",
    "credential",
    "app_key",
    "apikey",
)

# A broker login is a draft-only concern: it must not round-trip through the
# browser, but a stored config view may show which user is configured.
_DRAFT_MARKERS = _SECRET_MARKERS + ("username",)

# The account-scoped write target is masked in a browser view; a draft keeps it,
# because a manual entry legitimately carries one.
_BROWSER_VIEW_MARKERS = _SECRET_MARKERS + ("product_key",)

# ``api_key`` is only reachable with an explicit separator, so it stays listed
# next to the shared markers rather than being folded into ``apikey``.
_FINGERPRINT_MARKERS = _SECRET_MARKERS + ("api_key",)

# Keys that merely *name* or *describe* a credential. They match a secret marker
# by substring but carry no credential value, so a browser view keeps them.
CREDENTIAL_REFERENCE_KEYS = ("credentials_ref",)
PRESENCE_FLAG_KEYS = ("has_password",)
PUBLIC_IDENTITY_TOKEN_KEYS = (
    PHYSICAL_IDENTITY_TOKEN_FIELD,
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
)

# Identifiers that are not credentials but are account-scoped write targets: a
# view may mask them, and a masked value must never be read back as identity.
SENSITIVE_IDENTITY_KEYS = ("product_key", "device_key", "device_id", "serial_number")

_SCOPE_MARKERS = {
    SCOPE_DRAFT: _DRAFT_MARKERS,
    SCOPE_BROWSER_VIEW: _BROWSER_VIEW_MARKERS,
    SCOPE_FINGERPRINT: _FINGERPRINT_MARKERS,
}

# Only a browser view keeps the reference/flag/token keys readable. A draft
# strips them because the server owns them, and the fingerprint digests them
# because its historic vocabulary already did.
_SCOPE_EXEMPT_KEYS = {
    SCOPE_BROWSER_VIEW: frozenset(
        CREDENTIAL_REFERENCE_KEYS + PRESENCE_FLAG_KEYS + PUBLIC_IDENTITY_TOKEN_KEYS
    ),
}


def classify_config_key(key, *, scope):
    """Classify one config key for a scope.

    Returns one of the ``CLASS_*`` values. Everything that is not a credential,
    a credential reference, a presence flag, a public equality token or an
    account-scoped identifier is public.
    """

    lowered = str(key).lower()
    if lowered in _SCOPE_EXEMPT_KEYS.get(scope, ()):
        if lowered in CREDENTIAL_REFERENCE_KEYS:
            return CLASS_CREDENTIAL_REFERENCE
        if lowered in PRESENCE_FLAG_KEYS:
            return CLASS_PRESENCE_FLAG
        return CLASS_PUBLIC_IDENTITY_TOKEN
    if any(marker in lowered for marker in _SCOPE_MARKERS[scope]):
        return CLASS_SECRET
    if lowered in SENSITIVE_IDENTITY_KEYS:
        return CLASS_SENSITIVE_IDENTITY
    return CLASS_PUBLIC


def is_secret_key(key, *, scope):
    """True when a scope must not expose this key's raw value."""

    return classify_config_key(key, scope=scope) == CLASS_SECRET


def is_secret_catalog_field(field):
    """True for a catalog field whose value must never be surfaced.

    Explicit catalog metadata, never a name guess: the central catalog already
    declares which fields hold credentials.
    """

    return field.get("risk") == "secret" or field.get("type") == "password"


__all__ = [
    "SCOPE_DRAFT",
    "SCOPE_BROWSER_VIEW",
    "SCOPE_FINGERPRINT",
    "CLASS_SECRET",
    "CLASS_CREDENTIAL_REFERENCE",
    "CLASS_PRESENCE_FLAG",
    "CLASS_PUBLIC_IDENTITY_TOKEN",
    "CLASS_SENSITIVE_IDENTITY",
    "CLASS_PUBLIC",
    "REDACTED_PLACEHOLDER",
    "CREDENTIAL_REFERENCE_KEYS",
    "PRESENCE_FLAG_KEYS",
    "PUBLIC_IDENTITY_TOKEN_KEYS",
    "SENSITIVE_IDENTITY_KEYS",
    "classify_config_key",
    "is_secret_key",
    "is_secret_catalog_field",
]
