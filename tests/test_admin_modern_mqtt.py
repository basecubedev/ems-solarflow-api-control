# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Modern Admin's MQTT code lists are the server's own.

frontend/admin-modern/src/lib/mqtt.js translates the codes the broker, the
mapping and the live topics answer with; each list is walked against the
module that produces the codes, so a new code fails here until it has words.
"""

import re
from pathlib import Path

import pytest

from admin.mqtt_broker import ISSUE_CODES as BROKER_CODES
from admin.mqtt_live_topics import LABELS
from ems.zendure_mqtt.builtin_broker import BUILTIN_PROFILE_REF
from ems.zendure_mqtt.payload_values import EXTRACT_REASONS
from mqtt_mapping.config import ISSUE_CODES as MAPPING_CODES
from mqtt_mapping.config import TARGETS
from mqtt_mapping.status import STATUS_REASONS

pytestmark = [
    pytest.mark.contract,
    pytest.mark.admin,
    pytest.mark.mqtt,
]

SOURCE = (Path(__file__).resolve().parents[1] / "frontend" / "admin-modern" / "src" / "lib" / "mqtt.js").read_text(
    encoding="utf-8"
)


def _list(name):
    match = re.search(rf"export const {name} = Object\.freeze\(\[([^\]]*)\]\)", SOURCE)
    assert match, name
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


@pytest.mark.parametrize(
    ("name", "server"),
    [
        ("BROKER_ISSUES", BROKER_CODES),
        ("MAPPING_ISSUES", MAPPING_CODES),
        ("LIVE_LABELS", LABELS),
        ("SILENCE_REASONS", STATUS_REASONS),
        ("VALUE_REASONS", EXTRACT_REASONS),
        ("TARGETS", TARGETS),
    ],
)
def test_each_list_is_the_servers(name, server):
    assert _list(name) == tuple(server)


def test_the_built_in_broker_has_the_servers_name():
    assert f'export const BUILTIN_BROKER = "{BUILTIN_PROFILE_REF}";' in SOURCE
