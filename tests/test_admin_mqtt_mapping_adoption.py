# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A saved mapping's device is offered from the mapping, ahead of what a search saw.

The routes, from the mapping file to the config, are in
``tests/test_admin_mqtt_endpoints.py``; this holds the merge rule on its own.
"""

import pytest

from admin.models import MqttHardwareCandidate
from admin.mqtt_mapping_adoption import MAPPING_BROKER_REF_FIELD, with_mapped_devices
from admin.zendure_mqtt_config_proposals import proposals_from_brokers

pytestmark = [pytest.mark.unit, pytest.mark.admin, pytest.mark.mqtt]

HOST, PORT = "192.0.2.10", 1883


def _observation(device_id):
    candidate = MqttHardwareCandidate(
        broker_id=f"mqtt:{HOST}:{PORT}",
        broker_host=HOST,
        broker_port=PORT,
        topic_family="ems_solarflow",
        device_id=device_id,
        display_name="External device",
        credentials_ref="ems-mqtt",
    )
    candidate.metrics_seen = ["outputHomePower"]
    candidate.topics_seen = [f"ems-solarflow/{device_id}/state"]
    return {**candidate.to_dict(), "tls": False, "tls_insecure": False}


def _searched(*device_ids):
    """What a search on the same broker proposes, stamped with its generation."""

    return proposals_from_brokers(
        [{"host": HOST, "port": PORT, "discovery_generation": 7, "devices": [_observation(item) for item in device_ids]}]
    )


def test_a_search_that_saw_the_mapped_device_gives_way_to_the_mapping():
    searched = _searched("balkon", "fhem-wr")

    merged = with_mapped_devices(searched, {"ems-mqtt": [_observation("balkon")]})

    balkon = [proposal for proposal in merged if proposal["device_id"] == "balkon"]
    assert len(balkon) == 1
    assert balkon[0][MAPPING_BROKER_REF_FIELD] == "ems-mqtt"
    assert not balkon[0]["id"].endswith(":g7")
    assert [proposal["device_id"] for proposal in merged if MAPPING_BROKER_REF_FIELD not in proposal] == ["fhem-wr"]


def test_without_mapped_devices_the_searches_proposals_are_unchanged():
    searched = _searched("fhem-wr")

    assert with_mapped_devices(searched, {}) == searched
