# SPDX-License-Identifier: AGPL-3.0-or-later
"""An inverter that is not ours, read through the MQTT telemetry path.

The case this was built for: an inverter whose only interface is a web page,
scraped by a home-automation system that republishes the reading on a topic of
its own choosing -- typically ``<device>/<serial>/<value>``, a whole number of
watts as plain text.

It is a ``devices[]`` entry like any other MQTT device, on a broker profile like
any other, and it behaves exactly like a Zendure MQTT device with no write
method: read, shown, counted, never commanded. The only thing it needs that a
Zendure device does not is to be told which topic carries which metric, because
its topics follow nobody's convention.

The safety property is the one worth stating twice: an entry of this type must
never reach a control path. Not the HTTP control list, not the MQTT control
list, not even when the configuration asks for it.
"""

import pytest

from ems.config import http_control_device_configs, mqtt_control_device_configs
from ems.zendure_mqtt.config_entries import (
    EXTERNAL_MQTT_TYPE,
    external_mqtt_topic_map,
    is_external_mqtt_device_config,
    is_zendure_mqtt_device_config,
    validate_external_mqtt_device_config,
)
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]


# Deliberately an odd spelling: a topic is whatever the publisher chose, and a
# reporting installation's really was misspelled. Nothing here may normalise it.
OUTPUT_TOPIC = "KostlPico/EXAMPLE0000001/solarPower"
PV_TOPIC = "KostlPico/EXAMPLE0000001/dcPower"


def _entry(**overrides):
    entry = {
        "name": "Kostal Piko",
        "type": EXTERNAL_MQTT_TYPE,
        "mqtt": {
            "broker_ref": "local_mqtt",
            "device_id": "EXAMPLE0000001",
            "topics": {"outputHomePower": OUTPUT_TOPIC},
        },
    }
    entry.update(overrides)
    return entry


# --- it is never a control device ---------------------------------------


def test_an_external_entry_never_becomes_an_http_control_device():
    # The HTTP list is what gets a ZendureClient and a write path. An entry
    # that is not recognised here would be handed one.
    assert http_control_device_configs([_entry()]) == []


def test_an_external_entry_never_becomes_an_mqtt_control_device():
    assert mqtt_control_device_configs([_entry()]) == []


def test_asking_for_write_output_limit_is_refused_rather_than_honoured():
    asking = _entry(capabilities={"write_output_limit": True})

    issues = validate_external_mqtt_device_config(asking)

    assert any(issue["code"] == "write_output_limit_unsupported" for issue in issues)
    # And it still reaches no control list, refused or not.
    assert http_control_device_configs([asking]) == []
    assert mqtt_control_device_configs([asking]) == []


def test_it_is_not_mistaken_for_a_zendure_device():
    assert is_external_mqtt_device_config(_entry())
    assert not is_zendure_mqtt_device_config(_entry())


# --- configuration -------------------------------------------------------


def test_a_topic_and_an_identity_are_enough():
    assert validate_external_mqtt_device_config(_entry()) == []


def test_an_entry_without_topics_is_refused():
    issues = validate_external_mqtt_device_config(
        _entry(mqtt={"broker_ref": "local_mqtt", "device_id": "X"})
    )

    assert any(issue["code"] == "external_mqtt_topics_missing" for issue in issues)


def test_an_entry_without_a_device_id_is_refused():
    # Two inverters must never collapse into one tile, so identity is required
    # rather than derived from a name someone may reuse.
    issues = validate_external_mqtt_device_config(
        _entry(mqtt={"broker_ref": "local_mqtt", "topics": {"outputHomePower": OUTPUT_TOPIC}})
    )

    assert issues


def test_an_unknown_metric_name_is_refused_rather_than_stored():
    # A typo must not become a metric nobody reads. The accepted names are the
    # ones the rest of the project already speaks.
    issues = validate_external_mqtt_device_config(
        _entry(
            mqtt={
                "broker_ref": "local_mqtt",
                "device_id": "X",
                "topics": {"solarPower": OUTPUT_TOPIC},
            }
        )
    )

    assert any("solarPower" in issue["message"] for issue in issues)


def test_the_topic_map_is_built_for_the_aggregator():
    mapping = external_mqtt_topic_map(
        [
            _entry(
                mqtt={
                    "broker_ref": "local_mqtt",
                    "device_id": "EXAMPLE0000001",
                    "topics": {
                        "outputHomePower": OUTPUT_TOPIC,
                        "solarInputPower": PV_TOPIC,
                    },
                }
            )
        ]
    )

    assert mapping == {
        OUTPUT_TOPIC: ("EXAMPLE0000001", "outputHomePower"),
        PV_TOPIC: ("EXAMPLE0000001", "solarInputPower"),
    }


def test_a_disabled_entry_contributes_no_topics():
    assert external_mqtt_topic_map([_entry(enabled=False)]) == {}


# --- reading through the shared aggregator --------------------------------


def _aggregator():
    return ZendureMqttAggregator(
        external_topics={
            OUTPUT_TOPIC: ("EXAMPLE0000001", "outputHomePower"),
            PV_TOPIC: ("EXAMPLE0000001", "solarInputPower"),
        }
    )


def _snapshot(aggregator, device_id="EXAMPLE0000001"):
    return next(
        snap for snap in aggregator.snapshots() if snap.device_id == device_id
    )


def test_a_plain_watt_reading_becomes_the_inverter_output():
    aggregator = _aggregator()

    aggregator.observe(OUTPUT_TOPIC, b"1234")

    snapshot = _snapshot(aggregator)
    assert snapshot.metrics["outputHomePower"] == 1234
    assert snapshot.serial_number == "EXAMPLE0000001"


def test_the_second_topic_is_kept_apart_from_the_first():
    aggregator = _aggregator()

    aggregator.observe(OUTPUT_TOPIC, b"900")
    aggregator.observe(PV_TOPIC, b"1100")

    metrics = _snapshot(aggregator).metrics
    assert metrics["outputHomePower"] == 900
    assert metrics["solarInputPower"] == 1100


def test_a_reading_that_cannot_be_read_leaves_the_last_one_standing():
    aggregator = _aggregator()

    aggregator.observe(OUTPUT_TOPIC, b"1234")
    aggregator.observe(OUTPUT_TOPIC, b"OFF")

    assert _snapshot(aggregator).metrics["outputHomePower"] == 1234


def test_a_topic_nobody_configured_is_still_ignored():
    # The aggregator sees every message on the broker. An unconfigured topic
    # must not become a device.
    aggregator = _aggregator()

    aggregator.observe("SomeoneElse/thing/value", b"5")

    assert aggregator.snapshots() == []


def test_zendure_topics_still_classify_as_before():
    # The external map is consulted first; it must not shadow the families the
    # aggregator already knows.
    aggregator = _aggregator()

    aggregator.observe("Zendure/HB/ZENDURE1/outputHomePower", b"480")

    snapshot = _snapshot(aggregator, "ZENDURE1")
    assert snapshot.metrics["outputHomePower"] == 480


def test_a_topic_is_matched_exactly_as_published():
    """Case and spelling are the publisher's, not ours.

    The installation this was built for publishes a misspelled device name.
    Matching it loosely would be worse than not matching it: a topic that
    differs by a letter must stay unmatched rather than quietly attach to the
    wrong device.
    """

    aggregator = _aggregator()

    aggregator.observe("KostalPiko/EXAMPLE0000001/solarPower", b"1234")
    aggregator.observe("kostlpico/EXAMPLE0000001/solarPower", b"4321")

    assert aggregator.snapshots() == []

    aggregator.observe(OUTPUT_TOPIC, b"1234")
    assert _snapshot(aggregator).metrics["outputHomePower"] == 1234


# --- it is validated by the checks every MQTT entry gets ------------------


def test_an_entry_without_a_name_is_refused():
    # The name is the EMS runtime identity key (controller state, runtime-state,
    # dashboard, history). An entry without one is not usable.
    issues = validate_external_mqtt_device_config(_entry(name=""))

    assert any(issue["code"] == "name_missing" for issue in issues)


def test_a_broker_reference_nobody_configured_is_refused():
    # Otherwise a typo quietly lands the device on the wrong broker, or on none.
    issues = validate_external_mqtt_device_config(
        _entry(), known_broker_refs=("local_mqtt", "zendure_cloud")
    )
    assert issues == []

    issues = validate_external_mqtt_device_config(
        _entry(mqtt={
            "broker_ref": "typo",
            "device_id": "EXAMPLE0000001",
            "topics": {"outputHomePower": OUTPUT_TOPIC},
        }),
        known_broker_refs=("local_mqtt",),
    )
    assert any(issue["code"] == "broker_ref_unknown" for issue in issues)


def test_the_identity_is_resolved_the_way_every_other_entry_resolves_it():
    # mqtt.device_id, then serial_number, then device_id -- one answer to
    # "which device is this", shared with the Zendure entries.
    from ems.zendure_mqtt.config_entries import zendure_mqtt_device_identifier

    by_serial = {
        "name": "Kostal Piko",
        "type": EXTERNAL_MQTT_TYPE,
        "serial_number": "EXAMPLE0000001",
        "mqtt": {
            "broker_ref": "local_mqtt",
            "topics": {"outputHomePower": OUTPUT_TOPIC},
        },
    }

    assert validate_external_mqtt_device_config(by_serial) == []
    assert zendure_mqtt_device_identifier(by_serial) == "EXAMPLE0000001"
    assert external_mqtt_topic_map([by_serial]) == {
        OUTPUT_TOPIC: ("EXAMPLE0000001", "outputHomePower")
    }


def test_two_entries_sharing_a_name_are_caught_by_the_shared_check():
    # The name check is transport-independent and already exists; an external
    # entry must be inside it, not beside it.
    from ems.zendure_mqtt.config_entries import find_duplicate_device_names

    issues = find_duplicate_device_names([_entry(), _entry()])

    assert any(issue["code"] == "device_name_duplicate" for issue in issues)


# --- the rest of the project sees it for what it is ----------------------


def test_it_is_counted_as_a_user_of_its_broker_profile():
    """Otherwise its broker's credential looks unused.

    `collect_mqtt_credential_consumers` is the one place that answers "which
    stored credentials does this installation still need". A device it does not
    see is a credential that looks safe to drop.
    """

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
        "devices": [_entry(mqtt={
            "broker_ref": "house",
            "device_id": "EXAMPLE0000001",
            "topics": {"outputHomePower": OUTPUT_TOPIC},
        })],
    }

    refs = {consumer.credentials_ref for consumer in collect_mqtt_credential_consumers(config)}
    assert "house-broker" in refs


def test_it_is_never_asked_for_an_ip_or_a_serial_field():
    """It has neither, by construction.

    `template_placeholder_paths` reports what a config still has to be filled
    in. Asking an MQTT-read device for an ip would make a complete config look
    incomplete forever.
    """

    from ems.config import template_placeholder_paths

    paths = template_placeholder_paths({"devices": [_entry()]})

    assert not [path for path in paths if path.startswith("devices[")]


def test_it_alone_does_not_make_a_config_bootable():
    """It is read, not controlled.

    A configuration whose only device is an external inverter has nothing for
    the control loop to do, and must not look like a working installation.
    """

    from ems.zendure_mqtt.config_entries import has_runtime_control_device

    assert not has_runtime_control_device({"devices": [_entry()]})


def test_the_diagnosis_does_not_treat_it_as_a_local_api_device():
    """It would otherwise be reported as missing its ip and serial."""

    from types import SimpleNamespace

    from ems.diagnostics import diagnose_config_plausibility

    checks = []
    diagnose_config_plausibility(
        checks, SimpleNamespace(), {"devices": [_entry()], "grid_meter": {"type": "shelly", "ip": "192.0.2.50"}}
    )

    codes = [check.get("code") for check in checks]
    assert "device_ip_missing" not in codes
    assert "device_sn_missing" not in codes
    # A malformed one is still reported, through its own validator.
    broken = []
    diagnose_config_plausibility(
        broken,
        SimpleNamespace(),
        {
            "devices": [_entry(mqtt={"broker_ref": "local_mqtt", "device_id": "X"})],
            "grid_meter": {"type": "shelly", "ip": "192.0.2.50"},
        },
    )
    assert "external_mqtt_topics_missing" in [check.get("code") for check in broken]


# --- topics that would fail quietly --------------------------------------


def test_a_wildcard_topic_is_refused_rather_than_silently_inert():
    # A subscription filter would match, but readings are attributed by exact
    # topic, so a wildcard entry would subscribe and never record anything.
    issues = validate_external_mqtt_device_config(
        _entry(mqtt={
            "broker_ref": "local_mqtt",
            "device_id": "X",
            "topics": {"outputHomePower": "KostlPico/+/solarPower"},
        })
    )

    assert any(issue["code"] == "external_mqtt_topic_wildcard" for issue in issues)


def test_a_topic_belonging_to_a_known_family_is_refused():
    # Otherwise a mistyped entry quietly takes a Zendure device's readings and
    # that device goes dark with nothing said.
    issues = validate_external_mqtt_device_config(
        _entry(mqtt={
            "broker_ref": "local_mqtt",
            "device_id": "X",
            "topics": {"outputHomePower": "Zendure/HB/ZEN1/outputHomePower"},
        })
    )

    assert any(issue["code"] == "external_mqtt_topic_recognised" for issue in issues)


def test_two_devices_claiming_one_topic_are_reported():
    # The last one wins in a plain mapping, so the other device would sit there
    # receiving nothing, for no visible reason.
    from ems.zendure_mqtt.config_entries import find_duplicate_external_topics

    shared = "Shared/topic/value"
    issues = find_duplicate_external_topics(
        [
            _entry(mqtt={"broker_ref": "local_mqtt", "device_id": "A",
                         "topics": {"outputHomePower": shared}}),
            _entry(name="Second", mqtt={"broker_ref": "local_mqtt", "device_id": "B",
                                        "topics": {"outputHomePower": shared}}),
        ]
    )

    assert any(issue["code"] == "external_mqtt_topic_duplicate" for issue in issues)


def test_one_device_may_of_course_keep_its_own_topics():
    from ems.zendure_mqtt.config_entries import find_duplicate_external_topics

    assert find_duplicate_external_topics([_entry(), _entry(name="Other", mqtt={
        "broker_ref": "local_mqtt", "device_id": "B",
        "topics": {"outputHomePower": PV_TOPIC},
    })]) == []


def test_the_diagnosis_reports_a_topic_two_devices_claim():
    # The check is only worth having if something runs it.
    from types import SimpleNamespace

    from ems.diagnostics import diagnose_config_plausibility

    shared = "Shared/topic/value"
    checks = []
    diagnose_config_plausibility(
        checks,
        SimpleNamespace(),
        {
            "devices": [
                _entry(mqtt={"broker_ref": "local_mqtt", "device_id": "A",
                             "topics": {"outputHomePower": shared}}),
                _entry(name="Second", mqtt={"broker_ref": "local_mqtt", "device_id": "B",
                                            "topics": {"outputHomePower": shared}}),
            ],
            "grid_meter": {"type": "shelly", "ip": "192.0.2.50"},
        },
    )

    assert "external_mqtt_topic_duplicate" in [check.get("code") for check in checks]
