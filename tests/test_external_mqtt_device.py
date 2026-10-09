# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device from the external catalog, read through the MQTT telemetry path.

The case this was built for: an inverter whose only interface is a web page,
scraped by a home-automation system and published into the project's own
namespace as ``ems-solarflow/<id>/inverterPower`` -- a number of watts as plain
text.

The catalog is a whitelist: it fixes the shape of the device's topics and what
each known key means. An entry names its catalog family and its device id, and
nothing about its topics is configured. A topic of any other shape, or a key
the catalog does not list, is never read.

The safety property is the one worth stating twice: an entry of this type must
never reach a control path. Not the HTTP control list, not the MQTT control
list, not even when the configuration asks for it.
"""

from types import SimpleNamespace

import pytest

from dashboard.runtime_write import ac_role_supported, build_validation_context
from ems.config import http_control_device_configs, mqtt_control_device_configs
from ems.zendure_mqtt.config_entries import (
    EXTERNAL_MQTT_TYPE,
    external_device_subscriptions,
    is_external_mqtt_device_config,
    is_zendure_mqtt_device_config,
    validate_external_mqtt_device_config,
)
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]


OUTPUT_TOPIC = "ems-solarflow/EXAMPLE0000001/inverterPower"


def _entry(**overrides):
    entry = {
        "name": "Garage inverter",
        "type": EXTERNAL_MQTT_TYPE,
        "mqtt": {
            "broker_ref": "local_mqtt",
            "topic_family": "ems_solarflow",
            "device_id": "EXAMPLE0000001",
        },
    }
    entry.update(overrides)
    return entry


def _mqtt(**overrides):
    mqtt = {
        "broker_ref": "local_mqtt",
        "topic_family": "ems_solarflow",
        "device_id": "EXAMPLE0000001",
    }
    mqtt.update(overrides)
    return {key: value for key, value in mqtt.items() if value is not None}


def _codes(issues):
    return {issue["code"] for issue in issues}


# --- it is never a control device ---------------------------------------


def test_an_external_entry_never_becomes_an_http_control_device():
    assert http_control_device_configs([_entry()]) == []


def test_an_external_entry_never_becomes_an_mqtt_control_device():
    assert mqtt_control_device_configs([_entry()]) == []


def test_asking_for_write_output_limit_is_refused_rather_than_honoured():
    asking = _entry(capabilities={"write_output_limit": True})

    assert "write_output_limit_unsupported" in _codes(
        validate_external_mqtt_device_config(asking)
    )
    assert http_control_device_configs([asking]) == []
    assert mqtt_control_device_configs([asking]) == []


def test_neither_the_dashboard_nor_emsctl_offers_it_an_ac_role():
    context = build_validation_context(
        {"devices": [_entry(), {"name": "WR1", "ip": "192.0.2.10"}]}
    )

    assert not ac_role_supported("Garage inverter", context)
    assert ac_role_supported("WR1", context)


def test_the_ac_charge_diagnosis_never_counts_it_as_able_to_charge():
    """It has no command path, whatever battery it reports.

    The diagnosis asked only whether an entry was a Zendure MQTT device, so an
    external one fell through to "no refusal" and was listed as permitted.
    """

    from ems.diagnostics import diagnose_ac_charge_model_refusal

    assert diagnose_ac_charge_model_refusal(_entry()) == "telemetry_only"


def test_it_is_not_mistaken_for_a_zendure_device():
    assert is_external_mqtt_device_config(_entry())
    assert not is_zendure_mqtt_device_config(_entry())


# --- configuration: the catalog names the device, nothing else does ----------


def test_a_catalog_family_and_a_device_id_are_enough():
    assert validate_external_mqtt_device_config(_entry()) == []


def test_a_family_the_catalog_does_not_list_is_refused():
    issues = validate_external_mqtt_device_config(_entry(mqtt=_mqtt(topic_family="hallo")))

    assert "external_mqtt_family_unknown" in _codes(issues)


def test_an_entry_without_a_family_is_refused():
    issues = validate_external_mqtt_device_config(_entry(mqtt=_mqtt(topic_family=None)))

    assert "topic_family_missing" in _codes(issues)


def test_an_entry_without_a_device_id_is_refused():
    """The device id is the segment of its topics that names it.

    A serial number elsewhere in the entry identifies the device but routes
    nothing, so it is not a substitute.
    """

    issues = validate_external_mqtt_device_config(
        _entry(serial_number="EXAMPLE0000001", mqtt=_mqtt(device_id=None))
    )

    assert "external_mqtt_device_id_missing" in _codes(issues)


@pytest.mark.parametrize("device_id", ["+", "#", "garage/inverter"])
def test_a_device_id_carrying_topic_syntax_is_refused_once(device_id):
    issues = validate_external_mqtt_device_config(_entry(mqtt=_mqtt(device_id=device_id)))

    assert "mqtt_route_segment_invalid" in _codes(issues)
    assert "external_mqtt_device_id_invalid" not in _codes(issues)


@pytest.mark.parametrize(
    "device_id", ["garage.inverter", "garage inverter", "Wechselrichter-Süd", "x" * 65]
)
def test_a_device_id_the_catalog_would_never_read_is_refused(device_id):
    """A segment the topic matcher rejects would leave the device dark for good."""

    issues = validate_external_mqtt_device_config(_entry(mqtt=_mqtt(device_id=device_id)))

    assert "external_mqtt_device_id_invalid" in _codes(issues)


@pytest.mark.parametrize("device_id", ["garage-inverter", "WR_1", "EXAMPLE0000001", "x" * 64])
def test_a_device_id_of_letters_digits_dash_and_underscore_is_accepted(device_id):
    assert validate_external_mqtt_device_config(_entry(mqtt=_mqtt(device_id=device_id))) == []


def test_a_configured_topic_list_is_refused_rather_than_read():
    """Topics are the catalog's to fix, not the entry's to choose."""

    issues = validate_external_mqtt_device_config(
        _entry(mqtt=_mqtt(topics={"outputHomePower": "hallo/x/y"}))
    )

    assert "external_mqtt_topics_unsupported" in _codes(issues)


def test_a_disabled_broker_profile_is_reported_like_any_other_mqtt_device():
    """Otherwise the entry is written and then never read, with nothing said."""

    from ems.zendure_mqtt.config_entries import find_zendure_mqtt_broker_profile_issues

    config = {
        "zendure_mqtt": {
            "brokers": {
                "local_mqtt": {
                    "enabled": False,
                    "source": "local_mqtt",
                    "host": "10.0.0.71",
                    "port": 1883,
                }
            }
        },
        "devices": [_entry()],
    }

    issues = find_zendure_mqtt_broker_profile_issues(config)

    assert "zendure_mqtt_broker_ref_disabled" in _codes(issues)
    assert [issue["device_index"] for issue in issues] == [0]


def test_the_diagnosis_runtime_section_agrees_with_the_runtime_on_a_cloud_broker():
    from ems.diagnostics import diagnose_zendure_mqtt_runtime

    config = {
        "zendure_mqtt": {
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtt.example.invalid",
                    "port": 8883,
                    "tls": True,
                    "credentials_ref": "zendure-cloud",
                }
            }
        },
        "devices": [_entry(mqtt=_mqtt(broker_ref="zendure_cloud"))],
    }
    checks = []

    diagnose_zendure_mqtt_runtime(checks, config)

    codes = {check.get("code") for check in checks}
    assert "zendure_mqtt_external_mqtt_cloud_broker" in codes


def test_the_zendure_cloud_broker_is_refused():
    """The cloud never carries such a device, and its sessions are ACL-scoped.

    A subscription outside the account's tree is at best useless there.
    """

    issues = validate_external_mqtt_device_config(
        _entry(mqtt=_mqtt(broker_ref="zendure_cloud")),
        broker_sources={"zendure_cloud": "zendure_cloud_mqtt"},
    )

    assert "external_mqtt_cloud_broker" in _codes(issues)
    assert validate_external_mqtt_device_config(
        _entry(), broker_sources={"local_mqtt": "local_mqtt"}
    ) == []


def test_the_runtime_refuses_it_on_the_zendure_cloud_and_subscribes_nothing_there():
    """The refusal holds where it matters, not only in diagnose and Admin."""

    from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

    config = {
        "zendure_mqtt": {
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtt.example.invalid",
                    "port": 8883,
                    "tls": True,
                    "credentials_ref": "zendure-cloud",
                }
            }
        },
        "devices": [_entry(mqtt=_mqtt(broker_ref="zendure_cloud"))],
    }

    runtime = build_zendure_mqtt_runtime(config)

    (device,) = runtime.status()["devices"]
    assert device["status"] == "invalid"
    assert "external_mqtt_cloud_broker" in device["issues"]
    assert external_device_subscriptions(
        config["devices"], "zendure_cloud", broker_source="zendure_cloud_mqtt"
    ) == ()


def test_a_zendure_entry_naming_a_catalog_family_is_refused():
    """It would read nothing it names and carry battery defaults it has no use for."""

    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
        validate_zendure_mqtt_device_config,
    )

    entry = {"name": "Garage", "type": "zendure_mqtt", "mqtt": _mqtt()}
    assert "topic_family_external_device" in _codes(validate_zendure_mqtt_device_config(entry))
    control = {**entry, "capabilities": {"write_output_limit": True}}
    assert "topic_family_external_device" in _codes(
        validate_zendure_mqtt_control_device_config(control)
    )


def test_a_broker_reference_nobody_configured_is_refused():
    assert validate_external_mqtt_device_config(
        _entry(), known_broker_refs=("local_mqtt",)
    ) == []
    issues = validate_external_mqtt_device_config(
        _entry(mqtt=_mqtt(broker_ref="typo")), known_broker_refs=("local_mqtt",)
    )

    assert "broker_ref_unknown" in _codes(issues)


def test_an_entry_without_a_name_is_refused():
    assert "name_missing" in _codes(validate_external_mqtt_device_config(_entry(name="")))


def test_two_entries_sharing_a_name_are_caught_by_the_shared_check():
    from ems.zendure_mqtt.config_entries import find_duplicate_device_names

    issues = find_duplicate_device_names([_entry(), _entry()])

    assert "device_name_duplicate" in _codes(issues)


@pytest.mark.parametrize("second_broker", ["local_mqtt", "bridged_copy"])
def test_one_device_configured_twice_is_caught_on_any_broker(second_broker):
    """Two brokers carrying one device's topics would count its output twice."""

    from ems.zendure_mqtt.config_entries import find_duplicate_zendure_device_identities

    issues = find_duplicate_zendure_device_identities(
        [_entry(), _entry(name="Again", mqtt=_mqtt(broker_ref=second_broker))],
        broker_sources={"local_mqtt": "local_mqtt", "bridged_copy": "local_mqtt"},
    )

    assert "external_device_duplicate" in _codes(issues)


def test_two_devices_on_one_broker_are_no_duplicate():
    from ems.zendure_mqtt.config_entries import find_duplicate_zendure_device_identities

    assert find_duplicate_zendure_device_identities(
        [_entry(), _entry(name="Second", mqtt=_mqtt(device_id="EXAMPLE0000002"))]
    ) == []


# --- subscriptions --------------------------------------------------------


def test_each_device_subscribes_its_own_topics_and_no_more():
    devices = [
        _entry(),
        _entry(name="Second", mqtt=_mqtt(device_id="EXAMPLE0000002")),
        _entry(name="Elsewhere", mqtt=_mqtt(broker_ref="other", device_id="EXAMPLE0000003")),
    ]

    assert external_device_subscriptions(devices, "local_mqtt") == (
        "ems-solarflow/EXAMPLE0000001/+",
        "ems-solarflow/EXAMPLE0000002/+",
    )


def test_a_disabled_entry_is_neither_read_nor_shown_nor_counted():
    """Off means the EMS leaves it alone; the inverter itself is never switched."""

    from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

    config = {
        "zendure_mqtt": {
            "brokers": {
                "local_mqtt": {"enabled": True, "source": "local_mqtt", "host": "10.0.0.5"}
            }
        },
        "devices": [_entry(enabled=False)],
    }

    assert external_device_subscriptions(config["devices"], "local_mqtt") == ()
    runtime = build_zendure_mqtt_runtime(config)
    (device,) = runtime.status()["devices"]
    assert device["status"] == "unseen"

    from dashboard.telemetry import build_dashboard_snapshot

    controller = SimpleNamespace(
        devices=[],
        runtime_state=None,
        device_online={},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=runtime,
    )
    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[],
        targets=[],
        effective_targets=[],
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )
    assert "Garage inverter" not in snapshot["devices"]
    assert snapshot["inverter_output_w"] == 0


def test_a_disabled_or_invalid_entry_subscribes_nothing():
    devices = [
        _entry(enabled=False),
        _entry(name="Unknown", mqtt=_mqtt(topic_family="hallo")),
        _entry(name="Asking", capabilities={"write_output_limit": True}),
    ]

    assert external_device_subscriptions(devices, "local_mqtt") == ()


def test_a_control_broker_shared_with_the_telemetry_side_subscribes_them_too():
    """The telemetry runtime borrows a control device's broker connection.

    That connection is built by the control runtime, so if it did not subscribe
    the catalog device's topics, an external device on the same broker as a
    controlled Zendure device would stay dark.
    """

    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    built = []

    def factory(broker_config):
        built.append(broker_config)
        return SimpleNamespace(add_listener=lambda *_args, **_kwargs: None)

    config = {
        "zendure_mqtt": {
            "brokers": {
                "local_mqtt": {"enabled": True, "source": "local_mqtt", "host": "10.0.0.5"}
            }
        },
        "devices": [
            {
                "name": "Hyper",
                "type": "zendure_mqtt",
                "hardware_profile": "hyper_2000",
                "capabilities": {"write_output_limit": True},
                "mqtt": {
                    "broker_ref": "local_mqtt",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV",
                    "product_key": "PK",
                },
            },
            _entry(),
        ],
    }

    control = build_zendure_mqtt_control_runtime(config, service_factory=factory)

    assert built, "the control runtime built no broker service"
    assert "ems-solarflow/EXAMPLE0000001/+" in built[0].client_config().resolved_subscriptions()
    assert [device.name for device in control.devices] == ["Hyper"]
    assert control.rejected == []


# --- reading through the shared aggregator --------------------------------


def _snapshot(aggregator, device_id="ems-solarflow/EXAMPLE0000001"):
    return next(snap for snap in aggregator.snapshots() if snap.device_id == device_id)


def test_a_plain_watt_reading_becomes_the_inverter_output():
    aggregator = ZendureMqttAggregator()

    aggregator.observe(OUTPUT_TOPIC, b"1234")

    snapshot = _snapshot(aggregator)
    assert snapshot.metrics["outputHomePower"] == 1234
    assert snapshot.serial_number is None
    assert snapshot.topic_families == {"ems_solarflow"}


def test_a_reading_that_cannot_be_read_leaves_the_last_one_standing():
    aggregator = ZendureMqttAggregator()

    aggregator.observe(OUTPUT_TOPIC, b"1234")
    for payload in (b"OFF", b"765 W", b'{"value": 1}', b"765,0", b"true", b"", b"-5", b"nan"):
        aggregator.observe(OUTPUT_TOPIC, payload)

    assert _snapshot(aggregator).metrics["outputHomePower"] == 1234


def test_a_reading_that_cannot_be_read_is_no_sign_of_life():
    """The last good value stays, but it goes stale on schedule.

    Otherwise a publisher that switched to "0 W" at night would keep the last
    daytime output on the dashboard, fresh, for as long as it kept publishing.
    """

    clock = [0.0]
    aggregator = ZendureMqttAggregator(
        monotonic=lambda: clock[0], wall_clock=lambda: clock[0]
    )
    aggregator.observe(OUTPUT_TOPIC, b"765")
    clock[0] = 600.0
    aggregator.observe(OUTPUT_TOPIC, b"0 W")

    snapshot = _snapshot(aggregator)
    assert snapshot.metrics["outputHomePower"] == 765
    assert snapshot.last_seen_monotonic == 0.0


def test_a_key_the_catalog_does_not_list_is_never_read():
    aggregator = ZendureMqttAggregator()

    aggregator.observe("ems-solarflow/EXAMPLE0000001/dailyYield", b"5")

    assert aggregator.snapshots() == []


def test_a_topic_of_any_other_shape_is_never_read():
    aggregator = ZendureMqttAggregator()

    for topic in (
        "hallo/EXAMPLE0000001/inverterPower",
        "emsSolarflow/EXAMPLE0000001/inverterPower",
        "EMS-SolarFlow/EXAMPLE0000001/inverterPower",
        "ems-solarflow/EXAMPLE0000001/InverterPower",
        "ems-solarflow/EXAMPLE0000001/inverterPower/extra",
        "ems-solarflow//inverterPower",
        "ems-solarflow/garage inverter/inverterPower",
        "ems-solarflow/garage.inverter/inverterPower",
        "/ems-solarflow/EXAMPLE0000001/inverterPower",
        "KostalPiko/EXAMPLE0000001/solarPower",
    ):
        aggregator.observe(topic, b"1234")

    assert aggregator.snapshots() == []


def test_zendure_topics_still_classify_as_before():
    aggregator = ZendureMqttAggregator()

    aggregator.observe("Zendure/HB/ZENDURE1/outputHomePower", b"480")

    assert _snapshot(aggregator, "ZENDURE1").metrics["outputHomePower"] == 480


# --- the rest of the project sees it for what it is ----------------------


def test_it_is_counted_as_a_user_of_its_broker_profile():
    """Otherwise its broker's credential looks unused and could be dropped."""

    from ems.mqtt_credentials import collect_mqtt_credential_consumers

    config = {
        "zendure_mqtt": {
            "brokers": {
                "house": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.5",
                    "credentials_ref": "house-broker",
                }
            }
        },
        "devices": [_entry(mqtt=_mqtt(broker_ref="house"))],
    }

    refs = {consumer.credentials_ref for consumer in collect_mqtt_credential_consumers(config)}
    assert "house-broker" in refs


def test_it_is_never_asked_for_an_ip_or_a_serial_field():
    from ems.config import template_placeholder_paths

    paths = template_placeholder_paths({"devices": [_entry()]})

    assert not [path for path in paths if path.startswith("devices[")]


def test_it_alone_does_not_make_a_config_bootable():
    from ems.zendure_mqtt.config_entries import has_runtime_control_device

    assert not has_runtime_control_device({"devices": [_entry()]})


def test_the_diagnosis_does_not_treat_it_as_a_local_api_device():
    from ems.diagnostics import diagnose_config_plausibility

    grid = {"type": "shelly", "ip": "192.0.2.50"}
    checks = []
    diagnose_config_plausibility(checks, SimpleNamespace(), {"devices": [_entry()], "grid_meter": grid})

    codes = {check.get("code") for check in checks}
    assert "device_ip_missing" not in codes
    assert "device_sn_missing" not in codes

    broken = []
    diagnose_config_plausibility(
        broken,
        SimpleNamespace(),
        {"devices": [_entry(mqtt=_mqtt(topic_family="hallo"))], "grid_meter": grid},
    )
    assert "external_mqtt_family_unknown" in {check.get("code") for check in broken}


def test_the_diagnosis_knows_the_broker_it_sits_on():
    from ems.diagnostics import diagnose_config_plausibility

    checks = []
    diagnose_config_plausibility(
        checks,
        SimpleNamespace(),
        {
            "zendure_mqtt": {
                "brokers": {
                    "zendure_cloud": {
                        "enabled": True,
                        "source": "zendure_cloud_mqtt",
                        "app_key": "app",
                        "secret": "secret",
                    }
                }
            },
            "devices": [_entry(mqtt=_mqtt(broker_ref="zendure_cloud"))],
            "grid_meter": {"type": "shelly", "ip": "192.0.2.50"},
        },
    )

    assert "external_mqtt_cloud_broker" in {check.get("code") for check in checks}
