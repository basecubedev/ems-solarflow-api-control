# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Admin does not edit, it does not touch.

The maintenance page has no editor for an installed external MQTT inverter.
That is a reason to leave it exactly as it is, not a reason to treat it as the
nearest thing the Admin does know.

Before this was pinned, the maintenance draft classified such an entry as a
local-API device and the preview wrote it back as one: the type gone, the
topics gone, an empty ip and a full set of Zendure control defaults invented
for hardware that has none. Saving anything on the maintenance page would have
destroyed the configuration of a device the page never showed.

Leaving it alone also means the browser does not get to say what it is. The
draft names which installed entry stays; its content always comes from the
installed config. A draft that carried its own entry used to be written back
verbatim, so a request could smuggle any device past every check the normal
device path applies, a write-capable Zendure control entry included.
"""

import copy
import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.observation_identity import (
    CONNECTION_ID_FIELD,
    IDENTITY_STATUS_FIELD,
    PHYSICAL_DEVICE_ID_FIELD,
)
from ems.device_identity import (
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
    PHYSICAL_IDENTITY_TOKEN_FIELD,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

IDENTITY_KEY = b"external-mqtt-identity-key-32byt"

EXTERNAL = {
    "name": "Garage inverter",
    "type": "external_mqtt",
    "mqtt": {
        "broker_ref": "house",
        "topic_family": "ems_solarflow",
        "device_id": "EXAMPLE0000001",
    },
}

FORGED_CONTROL_ENTRY = {
    "name": "Garage inverter",
    "type": "zendure_mqtt",
    "serial_number": "EXAMPLE0000001",
    "hardware_model": "solarflow_800_pro",
    "mqtt": {
        "broker_ref": "house",
        "device_id": "EXAMPLE0000001",
        "product_key": "EXAMPLEPK",
        "topic_family": "zendure_json",
    },
    "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
}


def _config():
    return {
        "devices": [
            {"name": "WR1", "ip": "192.0.2.10", "sn": "SN1"},
            copy.deepcopy(EXTERNAL),
        ],
        "zendure_mqtt": {
            "brokers": {
                "house": {"enabled": True, "source": "local_mqtt", "host": "10.0.0.5"}
            }
        },
        "grid_meter": {"type": "shelly", "ip": "192.0.2.50"},
    }


CLOUD_ROUTE = "abcd1234efgh"

CLOUD_BROKER = {
    "enabled": True,
    "source": "zendure_cloud_mqtt",
    "host": "mqtt.example.invalid",
    "port": 8883,
    "tls": True,
    "credentials_ref": "zendure-cloud",
}

CLOUD_DEVICE = {
    "type": "zendure_mqtt",
    "name": "Cloud shed",
    "enabled": True,
    "hardware_profile": "solarflow_800_pro_2",
    "power_write_profile": "zensdk_properties_write",
    "mqtt": {
        "broker_ref": "cloud_a",
        "topic_family": "legacy_zendure_json_alt",
        "device_id": CLOUD_ROUTE,
        "product_key": "PRODUCTKEY01",
        "write_topic": f"iot/PRODUCTKEY01/{CLOUD_ROUTE}/properties/write",
    },
    "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
}


def _config_with_cloud_device():
    config = _config()
    config["zendure_mqtt"]["brokers"]["cloud_a"] = copy.deepcopy(CLOUD_BROKER)
    config["devices"].append(copy.deepcopy(CLOUD_DEVICE))
    return config


def _install(base_dir, config):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    raw = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    (config_dir / "config.json").write_bytes(raw)
    return raw


def _loaded(tmp_path, config=None):
    """What the browser holds: the redacted draft the maintenance route serves."""
    raw = _install(tmp_path, config if config is not None else _config())
    loaded = load_maintenance_config(base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY)
    assert loaded["status"] == "ok", loaded
    return raw, loaded


def _preview_of(tmp_path, draft):
    return preview_maintenance_config(
        draft, base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY
    )


def _preview(tmp_path):
    """The preview merges the draft with the installed config, not the draft alone."""
    _raw, loaded = _loaded(tmp_path)
    result = _preview_of(tmp_path, loaded["draft"])
    assert result.get("status") == "ok", result
    assert result["validation"]["ok"] is True, result["validation"]
    return result["preview"]


def _apply(tmp_path, loaded, draft):
    return prepare_maintenance_config_apply(
        draft,
        loaded["revision"],
        base_dir=str(tmp_path),
        identity_token_key=IDENTITY_KEY,
    )


def _external_item(draft):
    return next(item for item in draft["devices"] if item.get("kind") == "external_mqtt")


def _error_codes(result):
    return [issue["code"] for issue in result["validation"]["errors"]]


_BROWSER_IDENTITY_FIELDS = (
    CONNECTION_ID_FIELD,
    IDENTITY_STATUS_FIELD,
    PHYSICAL_DEVICE_ID_FIELD,
    PHYSICAL_IDENTITY_TOKEN_FIELD,
    PHYSICAL_IDENTITY_ALIAS_TOKENS_FIELD,
)


def _as_configured(device):
    """A preview device without the identity projection the browser view adds."""
    return {
        key: value for key, value in device.items() if key not in _BROWSER_IDENTITY_FIELDS
    }


def _write_capable(devices):
    return [
        device
        for device in devices
        if device.get("type") == "zendure_mqtt"
        or (device.get("capabilities") or {}).get("write_output_limit") is True
    ]


def test_the_maintenance_preview_returns_the_entry_unchanged(tmp_path):
    external = _installed_external(_preview(tmp_path))

    assert external == [EXTERNAL], external


def test_the_other_devices_are_still_editable_beside_it(tmp_path):
    """Leaving one entry alone must not freeze the page."""
    devices = _preview(tmp_path)["devices"]
    wr1 = next(device for device in devices if device["name"] == "WR1")

    assert wr1["ip"] == "192.0.2.10"
    assert wr1["sn"] == "SN1"


def test_a_broker_used_only_by_an_external_device_is_not_pruned():
    """Same question as the credential scan: a profile used only by an external
    device must not look unreferenced and be dropped."""
    from admin.config_preview import _prune_unreferenced_new_brokers

    preview = _config()
    _prune_unreferenced_new_brokers(preview, set())

    assert "house" in preview["zendure_mqtt"]["brokers"]


def test_an_unchanged_page_writes_the_installed_config_back_byte_for_byte(tmp_path):
    """The browser round trip, redaction and identity tokens included."""
    raw, loaded = _loaded(tmp_path)

    prepared = _apply(tmp_path, loaded, loaded["draft"])

    assert prepared["status"] == "ok", prepared.get("validation")
    assert prepared["payload"] == raw


def test_the_external_entry_is_validated_by_its_own_rules_not_as_a_local_api_device(
    tmp_path,
):
    """An entry with no ip or serial is not a local-API device missing both."""
    _raw, loaded = _loaded(tmp_path)

    result = _preview_of(tmp_path, loaded["draft"])

    validation = result["validation"]
    about_it = [
        issue
        for issue in validation["errors"] + validation["warnings"]
        if "Garage inverter" in issue["message"]
    ]
    assert about_it == []
    assert validation["ok"] is True


@pytest.mark.parametrize(
    "breakage",
    [
        pytest.param(
            lambda config: config["devices"][1]["mqtt"].update(topic_family="hallo"),
            id="its-own-entry",
        ),
        pytest.param(
            lambda config: config["zendure_mqtt"]["brokers"]["house"].update(
                enabled=False
            ),
            id="its-broker-profile",
        ),
    ],
)
def test_what_blocks_the_page_says_where_it_is_fixed(tmp_path, breakage):
    """Neither the entry nor a named profile can be changed here."""

    config = _config()
    breakage(config)
    _raw, loaded = _loaded(tmp_path, config)

    result = _preview_of(tmp_path, loaded["draft"])

    about_it = [
        issue["message"]
        for issue in result["validation"]["errors"]
        if "Garage inverter" in issue["message"] or "devices.1" in issue["message"]
    ]
    assert about_it, result["validation"]
    assert all("config.json" in message for message in about_it)


def test_a_broken_external_entry_is_reported_with_the_core_reason(tmp_path):
    config = _config()
    config["devices"][1]["mqtt"]["topic_family"] = "hallo"
    _raw, loaded = _loaded(tmp_path, config)

    result = _preview_of(tmp_path, loaded["draft"])

    codes = _error_codes(result)
    assert "external_mqtt_family_unknown" in codes
    assert "device_host_invalid" not in codes
    assert "device_serial_missing" not in codes


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(
            lambda item: item.update(entry=copy.deepcopy(FORGED_CONTROL_ENTRY)),
            id="zendure-control-entry",
        ),
        pytest.param(
            lambda item: item["entry"]["mqtt"].update(
                topic_family="hallo", device_id="SOMEONE-ELSE"
            ),
            id="changed-catalog-route",
        ),
        pytest.param(
            lambda item: item.update(
                catalog_device_id="catalog:v1:forged", catalog_label="Something else"
            ),
            id="changed-catalog-identity",
        ),
        pytest.param(
            lambda item: item["entry"].update(name="Renamed", enabled=False),
            id="new-name-and-disabled-inside-the-entry",
        ),
        pytest.param(
            lambda item: item.update(
                topic_family="hallo", device_id="SOMEONE-ELSE", type="zendure_mqtt"
            ),
            id="route-fields-on-the-row",
        ),
        pytest.param(
            lambda item: item.pop("entry"),
            id="no-entry-at-all",
        ),
    ],
)
def test_what_the_draft_carries_for_an_external_entry_changes_nothing(tmp_path, forge):
    raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    forge(_external_item(draft))

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "ok", prepared.get("validation")
    assert prepared["payload"] == raw
    assert _write_capable(json.loads(prepared["payload"])["devices"]) == []


def test_the_name_and_the_on_off_switch_are_the_operators(tmp_path):
    """The basic edits every device has: a new name, switched off."""

    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    item = _external_item(draft)
    item.update(name="Garage roof", enabled=False)

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "ok", prepared.get("validation")
    (written,) = [
        device
        for device in json.loads(prepared["payload"])["devices"]
        if device.get("type") == "external_mqtt"
    ]
    assert written == {**EXTERNAL, "name": "Garage roof", "enabled": False}


def test_an_unchanged_row_writes_the_entry_back_byte_for_byte(tmp_path):
    """Name and on/off as served are no edit: no key appears that was not there."""

    raw, loaded = _loaded(tmp_path)

    prepared = _apply(tmp_path, loaded, copy.deepcopy(loaded["draft"]))

    assert prepared["status"] == "ok", prepared.get("validation")
    assert prepared["payload"] == raw


@pytest.mark.parametrize(
    "original_name",
    [
        pytest.param("Not Installed", id="unknown-name"),
        pytest.param(None, id="no-name"),
        pytest.param("WR1", id="a-local-api-device"),
    ],
)
def test_a_draft_naming_an_external_entry_that_is_not_installed_is_refused(
    tmp_path, original_name
):
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    draft["devices"].append(
        {
            "kind": "external_mqtt",
            "original_name": original_name,
            "name": "Garage inverter 2",
            "editable": False,
            "entry": copy.deepcopy(FORGED_CONTROL_ENTRY),
        }
    )

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared
    assert "external_mqtt_device_not_installed" in _error_codes(prepared)
    assert _write_capable(prepared["preview"]["devices"]) == []


@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param(
            {"kind": "local_api", "ip": "192.0.2.99", "sn": "SN9", "enabled": True},
            id="as-local-api",
        ),
        pytest.param(
            {
                "kind": "zendure_mqtt",
                "serial_number": "EXAMPLE0000001",
                "mqtt": copy.deepcopy(FORGED_CONTROL_ENTRY["mqtt"]),
                "capabilities": copy.deepcopy(FORGED_CONTROL_ENTRY["capabilities"]),
                "output_control": True,
            },
            id="as-zendure-control",
        ),
    ],
)
def test_an_external_entry_cannot_be_rewritten_as_another_kind(tmp_path, replacement):
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    index = draft["devices"].index(_external_item(draft))
    draft["devices"][index] = dict(
        replacement, original_name="Garage inverter", name="Garage inverter"
    )

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared
    assert "external_mqtt_device_read_only" in _error_codes(prepared)
    external = [
        _as_configured(device)
        for device in prepared["preview"]["devices"]
        if device["name"] == "Garage inverter"
    ]
    assert external == [EXTERNAL]
    assert _write_capable(prepared["preview"]["devices"]) == []


def _installed_external(preview):
    return [
        _as_configured(device)
        for device in preview["devices"]
        if device.get("type") == "external_mqtt"
    ]


def _omit(draft):
    draft["devices"].remove(_external_item(draft))


def _mark_removed(draft):
    _external_item(draft)["removed"] = True


def _remove_as_another_kind(draft):
    index = draft["devices"].index(_external_item(draft))
    draft["devices"][index] = {
        "kind": "local_api",
        "original_name": "Garage inverter",
        "name": "Garage inverter",
        "removed": True,
    }


_DROPS = [
    pytest.param(_omit, id="omitted"),
    pytest.param(_mark_removed, id="marked-removed"),
    pytest.param(_remove_as_another_kind, id="removed-as-another-kind"),
]


def _remove_with_its_button(draft):
    """What the card's Remove sends: the row goes, its issued reference is named."""

    item = _external_item(draft)
    draft["devices"].remove(item)
    draft.setdefault("removed_external", []).append(item["entry_ref"])


def test_the_remove_button_takes_the_entry_out(tmp_path):
    raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    _remove_with_its_button(draft)

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "ok", prepared.get("validation")
    written = json.loads(prepared["payload"])
    assert [device["name"] for device in written["devices"]] == ["WR1"]
    # Nothing else changes with it, the broker profile included.
    assert written["zendure_mqtt"] == json.loads(raw)["zendure_mqtt"]


@pytest.mark.parametrize(
    "refs",
    [
        pytest.param(["entry:v1:not-issued-by-this-server"], id="unknown-reference"),
        pytest.param([None], id="no-reference"),
    ],
)
def test_a_removal_naming_no_installed_entry_is_refused(tmp_path, refs):
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    draft["removed_external"] = refs

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "external_mqtt_device_not_installed" in _error_codes(prepared)
    assert _installed_external(prepared["preview"]) == [EXTERNAL]


def test_a_draft_that_keeps_and_removes_the_entry_is_refused(tmp_path):
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    draft["removed_external"] = [_external_item(draft)["entry_ref"]]

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "external_mqtt_device_read_only" in _error_codes(prepared)
    assert _installed_external(prepared["preview"]) == [EXTERNAL]


@pytest.mark.parametrize("drop", _DROPS)
def test_a_preview_that_drops_the_external_entry_is_refused(tmp_path, drop):
    """Removal names the entry; a draft that merely lacks it was not made by Remove."""
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    drop(draft)

    result = _preview_of(tmp_path, draft)

    assert result["validation"]["ok"] is False
    assert "external_mqtt_device_read_only" in _error_codes(result)
    assert _installed_external(result["preview"]) == [EXTERNAL]


@pytest.mark.parametrize("drop", _DROPS)
def test_an_apply_that_drops_the_external_entry_is_refused(tmp_path, drop):
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    drop(draft)

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared
    assert "external_mqtt_device_read_only" in _error_codes(prepared)
    assert _installed_external(prepared["preview"]) == [EXTERNAL]


@pytest.mark.parametrize(
    "name, config_factory, hidden",
    [
        pytest.param(" Garage inverter ", _config, None, id="surrounding-whitespace"),
        pytest.param(
            f"Garage near {CLOUD_ROUTE}",
            _config_with_cloud_device,
            CLOUD_ROUTE,
            id="contains-a-cloud-route-id",
        ),
        pytest.param(
            "Garage token=abc", _config, "token=abc", id="contains-a-secret-label"
        ),
    ],
)
def test_an_unchanged_page_round_trips_whatever_the_external_entry_is_called(
    tmp_path, name, config_factory, hidden
):
    """The browser never sees some names as installed, so it cannot name the entry.

    A name the draft strips or the browser view masks used to lock every apply:
    the row could not be matched to its installed entry, and reloading changed
    nothing because the reload masked the name again.
    """
    config = config_factory()
    config["devices"][1]["name"] = name
    raw, loaded = _loaded(tmp_path, config)
    if hidden is not None:
        assert hidden not in json.dumps(loaded)

    preview = _preview_of(tmp_path, loaded["draft"])
    prepared = _apply(tmp_path, loaded, loaded["draft"])

    assert preview["validation"]["ok"] is True, preview["validation"]
    assert prepared["status"] == "ok", prepared.get("validation")
    assert prepared["payload"] == raw


def _issued_under_another_key(tmp_path):
    other = load_maintenance_config(
        base_dir=str(tmp_path), identity_token_key=b"another-identity-key-of-32-bytes"
    )
    return _external_item(other["draft"])["entry_ref"]


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(
            lambda item, tmp_path: item.update(entry_ref="entry:v1:forged"),
            id="unknown",
        ),
        pytest.param(lambda item, tmp_path: item.pop("entry_ref"), id="missing"),
        pytest.param(
            lambda item, tmp_path: item.update(entry_ref="Garage inverter"),
            id="the-name",
        ),
        pytest.param(
            lambda item, tmp_path: item.update(entry_ref={"name": "Garage inverter"}),
            id="not-a-string",
        ),
        pytest.param(
            lambda item, tmp_path: item.update(
                entry_ref=_issued_under_another_key(tmp_path)
            ),
            id="issued-under-another-key",
        ),
    ],
)
def test_an_external_row_without_a_reference_the_server_issued_is_refused(
    tmp_path, forge
):
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    forge(_external_item(draft), tmp_path)

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared
    assert "external_mqtt_device_not_installed" in _error_codes(prepared)
    assert _installed_external(prepared["preview"]) == [EXTERNAL]
    assert _write_capable(prepared["preview"]["devices"]) == []


def test_one_reference_carried_by_two_rows_is_refused(tmp_path):
    """A second row for the same entry would write the device into the config twice."""
    _raw, loaded = _loaded(tmp_path)
    draft = copy.deepcopy(loaded["draft"])
    draft["devices"].append(copy.deepcopy(_external_item(draft)))

    prepared = _apply(tmp_path, loaded, draft)

    assert prepared["status"] == "invalid", prepared
    assert "payload" not in prepared
    assert _installed_external(prepared["preview"]) == [EXTERNAL]
    assert "external_mqtt_device_not_installed" in _error_codes(prepared)


def test_a_zendure_device_sharing_its_id_on_its_broker_previews_without_a_conflict(tmp_path):
    """Two devices with one id on one broker are two devices, not one seen twice.

    Before an external device's identity named its catalog family, both entries
    resolved to one anchor and every preview of the page failed with
    ``device_identity_conflict``.
    """

    config = _config()
    config["devices"].append(
        {
            "name": "Hyper",
            "type": "zendure_mqtt",
            "mqtt": {
                "broker_ref": EXTERNAL["mqtt"]["broker_ref"],
                "topic_family": "zensdk_ha_scalar",
                "device_id": EXTERNAL["mqtt"]["device_id"],
            },
        }
    )
    _raw, loaded = _loaded(tmp_path, config)

    result = _preview_of(tmp_path, loaded["draft"])

    assert result.get("status") == "ok", result
    assert result["validation"]["errors"] == [], result["validation"]
