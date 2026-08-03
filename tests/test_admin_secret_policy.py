# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one classification matrix for Admin secret handling.

Every Admin path that strips, redacts or digests a config value resolves
through :mod:`admin.secret_policy`. The full key/scope matrix lives here; the
draft, browser-view and fingerprint tests only assert that their path asks the
policy and applies the answer.
"""

import pytest

from admin.secret_policy import (
    CLASS_CREDENTIAL_REFERENCE,
    CLASS_PRESENCE_FLAG,
    CLASS_PUBLIC,
    CLASS_PUBLIC_IDENTITY_TOKEN,
    CLASS_SECRET,
    CLASS_SENSITIVE_IDENTITY,
    SCOPE_BROWSER_VIEW,
    SCOPE_DRAFT,
    SCOPE_FINGERPRINT,
    classify_config_key,
    is_secret_catalog_field,
    is_secret_key,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.config,
    pytest.mark.contract,
    pytest.mark.simulation,
]

CREDENTIAL_KEYS = (
    "password",
    "broker_password",
    "passphrase",
    "token",
    "api_token",
    "secret",
    "client_secret",
    "credential",
    "app_key",
    "apikey",
)

# (key, draft, browser view, fingerprint)
MATRIX = (
    *(
        (key, CLASS_SECRET, CLASS_SECRET, CLASS_SECRET)
        for key in CREDENTIAL_KEYS
    ),
    # Only an explicit separator reaches this historic marker.
    ("api_key", CLASS_PUBLIC, CLASS_PUBLIC, CLASS_SECRET),
    # A broker login must not round-trip through a draft, but a stored config
    # view may show which user is configured.
    ("username", CLASS_SECRET, CLASS_PUBLIC, CLASS_PUBLIC),
    # An account-scoped identifier stays classified as identity evidence in
    # every scope; only the browser view additionally masks the write target.
    (
        "product_key",
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SECRET,
        CLASS_SENSITIVE_IDENTITY,
    ),
    (
        "device_key",
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SENSITIVE_IDENTITY,
    ),
    (
        "device_id",
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SENSITIVE_IDENTITY,
    ),
    (
        "serial_number",
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SENSITIVE_IDENTITY,
        CLASS_SENSITIVE_IDENTITY,
    ),
    # References, presence flags and equality tokens name a secret without
    # being one; only a browser view is allowed to keep them readable.
    ("credentials_ref", CLASS_SECRET, CLASS_CREDENTIAL_REFERENCE, CLASS_SECRET),
    ("has_password", CLASS_SECRET, CLASS_PRESENCE_FLAG, CLASS_SECRET),
    (
        "physical_identity_token",
        CLASS_SECRET,
        CLASS_PUBLIC_IDENTITY_TOKEN,
        CLASS_SECRET,
    ),
    (
        "physical_identity_alias_tokens",
        CLASS_SECRET,
        CLASS_PUBLIC_IDENTITY_TOKEN,
        CLASS_SECRET,
    ),
    ("host", CLASS_PUBLIC, CLASS_PUBLIC, CLASS_PUBLIC),
    ("port", CLASS_PUBLIC, CLASS_PUBLIC, CLASS_PUBLIC),
    ("name", CLASS_PUBLIC, CLASS_PUBLIC, CLASS_PUBLIC),
    ("base_topic", CLASS_PUBLIC, CLASS_PUBLIC, CLASS_PUBLIC),
)


@pytest.mark.parametrize("key,draft,browser,fingerprint", MATRIX, ids=[c[0] for c in MATRIX])
def test_classification_matrix(key, draft, browser, fingerprint):
    assert classify_config_key(key, scope=SCOPE_DRAFT) == draft
    assert classify_config_key(key, scope=SCOPE_BROWSER_VIEW) == browser
    assert classify_config_key(key, scope=SCOPE_FINGERPRINT) == fingerprint


@pytest.mark.parametrize("key,draft,browser,fingerprint", MATRIX, ids=[c[0] for c in MATRIX])
def test_is_secret_key_matches_the_classification(key, draft, browser, fingerprint):
    assert is_secret_key(key, scope=SCOPE_DRAFT) is (draft == CLASS_SECRET)
    assert is_secret_key(key, scope=SCOPE_BROWSER_VIEW) is (browser == CLASS_SECRET)
    assert is_secret_key(key, scope=SCOPE_FINGERPRINT) is (fingerprint == CLASS_SECRET)


def test_classification_is_case_insensitive():
    assert is_secret_key("BROKER_PASSWORD", scope=SCOPE_DRAFT) is True
    assert classify_config_key("Credentials_Ref", scope=SCOPE_BROWSER_VIEW) == (
        CLASS_CREDENTIAL_REFERENCE
    )


def test_every_credential_marker_is_secret_in_every_scope():
    for scope in (SCOPE_DRAFT, SCOPE_BROWSER_VIEW, SCOPE_FINGERPRINT):
        for key in CREDENTIAL_KEYS:
            assert is_secret_key(key, scope=scope) is True, (scope, key)


def test_catalog_metadata_decides_before_any_name_guess():
    assert is_secret_catalog_field({"risk": "secret"}) is True
    assert is_secret_catalog_field({"type": "password"}) is True
    # A field named like a secret but declared plain stays plain: the catalog is
    # the authority for catalog-backed fields.
    assert is_secret_catalog_field({"key": "password_policy", "type": "string"}) is False


def test_admin_modules_do_not_keep_a_second_marker_list():
    """The four historic classifiers now delegate to the policy."""

    import admin.guided_setup_workflow as workflow
    import admin.maintenance_config as maintenance
    import admin.setup_config as setup
    import admin.zendure_mqtt_config_draft as draft

    assert not hasattr(maintenance, "_SECRET_LEAF_FRAGMENTS")
    assert not hasattr(maintenance, "_NON_SECRET_LEAF_KEYS")
    assert not hasattr(workflow, "_SECRET_KEY_MARKERS")
    assert not hasattr(draft, "SECRET_KEY_FRAGMENTS")

    assert maintenance._is_secret_leaf("mqtt_password") is True
    assert maintenance._is_secret_leaf("credentials_ref") is False
    assert draft._is_secret_key("username") is True
    assert workflow._is_secret_key("app_key") is True
    assert setup._is_secret({"risk": "secret"}) is True
