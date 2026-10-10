# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three Admin call sites classify config keys as secret, each with its own list.

``maintenance_config`` redacts before a config reaches the browser,
``zendure_mqtt_config_draft`` strips secrets out of a proposal fragment, and
``guided_setup_workflow`` digests them inside the setup mutation fingerprint.
The three fragment lists differ, and the differences are deliberate: the
Maintenance broker form edits the MQTT ``username``, so redacting it would blank
a field the operator has to see, while a proposal fragment must never carry one.

What matters is not that the lists match but that each site's *effective*
coverage holds. These tests pin that against the shipped config schema, so a
future divergence has to break a test instead of quietly opening a hole.
"""

import json
import pathlib

import pytest

from admin.guided_setup_workflow import setup_mutation_fingerprint
from admin.maintenance_config import redact_config_for_browser
from admin.zendure_mqtt_config_draft import _is_secret_key as fragment_is_secret

pytestmark = [
    pytest.mark.admin,
    pytest.mark.config,
    pytest.mark.contract,
    pytest.mark.simulation,
]

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "config" / "config.template.json"

SECRET_SHAPED = ("key", "token", "secret", "password", "passphrase", "credential")

# Keys whose name only *looks* secret: both name a file on disk, and an operator
# configuring TLS or a bundled InfluxDB has to see which path is in use.
PATH_NOT_SECRET = frozenset({"ssl_key_file"})

PLAINTEXT = "PLAINTEXT-SENTINEL"


def _secret_shaped_keys(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if any(fragment in key.lower() for fragment in SECRET_SHAPED):
                found.add(key)
            _secret_shaped_keys(value, found)
    elif isinstance(node, list):
        for item in node:
            _secret_shaped_keys(item, found)
    return found


def _seed_plaintext(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _seed_plaintext(value)
            elif any(fragment in key.lower() for fragment in SECRET_SHAPED):
                node[key] = PLAINTEXT
    elif isinstance(node, list):
        for item in node:
            _seed_plaintext(item)
    return node


def _template():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def test_the_template_still_carries_secret_shaped_keys():
    """Guards the guard: an empty key set would make the leak test vacuous."""

    assert _secret_shaped_keys(_template(), set()) - PATH_NOT_SECRET


def test_browser_redaction_masks_every_secret_shaped_template_key():
    """Nothing secret-shaped in the shipped schema reaches the browser in clear.

    The exemption is named rather than implied: a path is not a credential, and
    blanking it would hide configuration the operator is expected to check.
    """

    seeded = _seed_plaintext(_template())
    redacted = json.dumps(redact_config_for_browser(seeded))

    leaked = {
        key
        for key in _secret_shaped_keys(_template(), set())
        if f'"{key}": "{PLAINTEXT}"' in redacted
    }
    assert leaked <= PATH_NOT_SECRET


def test_a_proposal_fragment_never_carries_the_broker_username():
    """The Maintenance form edits it; a discovery fragment must not ship it."""

    assert fragment_is_secret("username") is True
    assert fragment_is_secret("password") is True


def test_the_mutation_fingerprint_never_returns_a_raw_value():
    """The digest is the whole contract: no input value survives it readable.

    Per-key digesting inside the body is defence in depth, so this holds for a
    key no marker list happens to name.
    """

    fingerprint = setup_mutation_fingerprint(
        draft={"passphrase": PLAINTEXT, "unnamed_by_any_list": PLAINTEXT},
        supported_grid_meter_count=0,
        features={"app_key": PLAINTEXT},
        zendure_mqtt_proposals=[{"zendure_credential": PLAINTEXT}],
        zendure_mqtt_broker={"password": PLAINTEXT},
        zendure_mqtt_manual_devices=[],
    )

    assert fingerprint.startswith("sha256:")
    assert PLAINTEXT not in fingerprint


CATALOGUED_SECRETS = frozenset({
    "grid_meter.mqtt.password",
    "ha.token",
    "influxdb.token",
    "zendure_mqtt.app_key",
    "zendure_mqtt.password",
    "zendure_mqtt.username",
})

# Each names, points at or governs a secret without being one: a path, an
# environment variable's name, a switch.
CATALOGUED_NEAR_SECRETS = (
    "dashboard.auth_file",
    "dashboard.ssl_cert_file",
    "dashboard.ssl_key_file",
    "dashboard.log_redaction",
    "influxdb.secret_file",
    "influxdb.token_env",
)


def test_only_credentials_are_catalogued_as_secrets():
    from ems.config_catalog import get_config_feature_field_index, is_secret_catalog_field

    fields = get_config_feature_field_index()
    secret = {path for path, field in fields.items() if is_secret_catalog_field(field)}

    assert secret == CATALOGUED_SECRETS


@pytest.mark.parametrize("path", CATALOGUED_NEAR_SECRETS)
def test_a_field_next_to_a_secret_keeps_its_default_and_renders_in_clear(path):
    from admin.setup_config import build_setup_catalog
    from ems.config_catalog import get_config_feature_field_index

    catalogued = get_config_feature_field_index()[path]
    setup_fields = {
        field["path"]: field
        for section in build_setup_catalog()["sections"]
        for field in section["fields"]
    }

    assert catalogued["type"] != "password"
    if path in setup_fields:
        assert setup_fields[path]["secret"] is False
        assert setup_fields[path].get("default") == catalogued.get("default")


def test_a_catalogued_path_is_redacted_by_the_catalog_not_by_its_name():
    view = redact_config_for_browser({
        "features": {
            "influxdb.token_env": "INFLUXDB_TOKEN",
            "influxdb.secret_file": "deploy/docker/influxdb.env",
            "influxdb.token": PLAINTEXT,
            "ha.token": PLAINTEXT,
        },
    })

    assert view["features"]["influxdb.token_env"] == "INFLUXDB_TOKEN"
    assert view["features"]["influxdb.secret_file"] == "deploy/docker/influxdb.env"
    assert view["features"]["influxdb.token"] != PLAINTEXT
    assert view["features"]["ha.token"] != PLAINTEXT
