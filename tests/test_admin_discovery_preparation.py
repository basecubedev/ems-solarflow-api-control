# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery preparation store: defaults, priority editing, persistence."""

import json

import pytest

from admin.discovery_preparation import (
    DEFAULT_PRIORITY,
    DISCOVERY_SOURCES,
    SOURCE_LOCAL_API,
    SOURCE_LOCAL_MQTT,
    SOURCE_ZENDURE_MQTT,
    DiscoveryPreparationStore,
    default_preparation,
    enabled_sources_in_priority,
    normalize_preparation,
    source_settings,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def test_default_priority_is_local_api_local_mqtt_zendure():
    assert DEFAULT_PRIORITY == ["local_api", "local_mqtt", "zendure_mqtt"]
    assert default_preparation()["discovery_priority"] == DEFAULT_PRIORITY
    assert set(default_preparation()["sources"]) == set(DISCOVERY_SOURCES)


def test_fresh_store_returns_defaults(tmp_path):
    store = DiscoveryPreparationStore(tmp_path)
    loaded = store.load()
    assert loaded == default_preparation()
    assert all(loaded["sources"][s]["enabled"] for s in DISCOVERY_SOURCES)


def test_priority_can_be_saved_and_loaded(tmp_path):
    store = DiscoveryPreparationStore(tmp_path)
    store.save(
        {
            "discovery_priority": [
                SOURCE_ZENDURE_MQTT,
                SOURCE_LOCAL_MQTT,
                SOURCE_LOCAL_API,
            ],
            "sources": {SOURCE_LOCAL_MQTT: {"enabled": False}},
        }
    )
    reloaded = DiscoveryPreparationStore(tmp_path).load()
    assert reloaded["discovery_priority"] == [
        SOURCE_ZENDURE_MQTT,
        SOURCE_LOCAL_MQTT,
        SOURCE_LOCAL_API,
    ]
    assert reloaded["sources"][SOURCE_LOCAL_MQTT]["enabled"] is False
    assert reloaded["sources"][SOURCE_LOCAL_API]["enabled"] is True


def test_partial_priority_is_completed_with_missing_sources():
    normalized = normalize_preparation({"discovery_priority": [SOURCE_ZENDURE_MQTT]})
    assert normalized["discovery_priority"][0] == SOURCE_ZENDURE_MQTT
    assert set(normalized["discovery_priority"]) == set(DISCOVERY_SOURCES)


def test_unknown_and_duplicate_priority_entries_are_dropped():
    normalized = normalize_preparation(
        {
            "discovery_priority": [
                "bogus",
                SOURCE_LOCAL_MQTT,
                SOURCE_LOCAL_MQTT,
                SOURCE_LOCAL_API,
            ]
        }
    )
    assert normalized["discovery_priority"] == [
        SOURCE_LOCAL_MQTT,
        SOURCE_LOCAL_API,
        SOURCE_ZENDURE_MQTT,
    ]


def test_source_settings_expose_priority_position_and_enabled():
    settings = source_settings(
        {
            "discovery_priority": [SOURCE_LOCAL_MQTT, SOURCE_LOCAL_API, SOURCE_ZENDURE_MQTT],
            "sources": {SOURCE_LOCAL_API: {"enabled": False}},
        }
    )
    assert [(s.source, s.priority, s.enabled) for s in settings] == [
        (SOURCE_LOCAL_MQTT, 1, True),
        (SOURCE_LOCAL_API, 2, False),
        (SOURCE_ZENDURE_MQTT, 3, True),
    ]


def test_enabled_sources_in_priority_skips_disabled():
    prep = {
        "discovery_priority": [SOURCE_ZENDURE_MQTT, SOURCE_LOCAL_API, SOURCE_LOCAL_MQTT],
        "sources": {SOURCE_LOCAL_API: {"enabled": False}},
    }
    assert enabled_sources_in_priority(prep) == [SOURCE_ZENDURE_MQTT, SOURCE_LOCAL_MQTT]


def test_corrupt_state_file_degrades_to_defaults(tmp_path):
    store = DiscoveryPreparationStore(tmp_path)
    store.state_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == default_preparation()


def test_saved_file_only_contains_priority_and_enabled_flags(tmp_path):
    store = DiscoveryPreparationStore(tmp_path)
    store.save(default_preparation())
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"discovery_priority", "sources"}
    for entry in on_disk["sources"].values():
        assert set(entry) == {"enabled"}
