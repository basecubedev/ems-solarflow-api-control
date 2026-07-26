# SPDX-License-Identifier: AGPL-3.0-or-later
"""Central default-device resolver contract (ems.config_catalog).

One authoritative source for a new inverter's common values: the same template
prototype that generates config.template.json, split into identity, common and
comment parts. Every Admin creation path materializes from here; no flow may
maintain its own literal defaults.
"""

import pytest

from ems.config_catalog import (
    DEVICE_IDENTITY_FIELD_KEYS,
    build_default_template,
    default_device_config,
    device_common_defaults,
    device_common_field_keys,
    get_config_feature_field_index,
)

pytestmark = pytest.mark.simulation


def test_common_defaults_derive_from_template_prototype():
    prototype = build_default_template(device_count=1)["devices"][0]
    defaults = device_common_defaults()
    expected = {
        key: value
        for key, value in prototype.items()
        if not str(key).startswith("_") and key not in DEVICE_IDENTITY_FIELD_KEYS
    }
    assert defaults == expected
    assert defaults, "the template prototype must provide common defaults"


def test_default_device_config_separates_identity_common_comments():
    parts = default_device_config()
    assert set(parts) == {"identity", "common", "comments"}
    assert set(parts["identity"]) <= set(DEVICE_IDENTITY_FIELD_KEYS)
    for key in parts["comments"]:
        assert str(key).startswith("_")
    for key in parts["common"]:
        assert not str(key).startswith("_")
        assert key not in DEVICE_IDENTITY_FIELD_KEYS
    # Sample identity values never leak into the common defaults.
    for sample in ("WR1", "192.168.1.100", "YOUR_SN"):
        assert sample not in parts["common"].values()


def test_returned_defaults_are_fresh_deep_copies():
    first = device_common_defaults()
    first["max_power"] = -1
    first["_injected"] = True
    second = device_common_defaults()
    assert second != first
    assert "_injected" not in second
    assert second["max_power"] != -1

    config = default_device_config()
    config["common"]["max_power"] = -2
    assert default_device_config()["common"]["max_power"] != -2


def test_common_field_keys_come_from_catalog_metadata():
    keys = device_common_field_keys()
    index_keys = [
        path[len("devices[].") :]
        for path in get_config_feature_field_index()
        if path.startswith("devices[].")
        and path[len("devices[].") :] not in DEVICE_IDENTITY_FIELD_KEYS
    ]
    assert list(keys) == index_keys
    for identity_key in DEVICE_IDENTITY_FIELD_KEYS:
        assert identity_key not in keys


def test_catalog_and_template_agree_on_common_fields():
    """A future common devices[] field must land in catalog AND template.

    Drift between the two sources would silently split the default set again,
    so both directions are pinned: every catalog common field has a template
    default, and every template common value is a catalog field.
    """

    assert set(device_common_field_keys()) == set(device_common_defaults())
