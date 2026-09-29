# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic tests for the read-only E3/DC Modbus/TCP probe (no hardware).

Three classes of defect are pinned here because each one has a history of
shipping silently in register work:

* the int32 word order, which the E3/DC manual's own feed-in example gets
  backwards in its table while getting it right in its arithmetic;
* the block tables, where a field list and the ``FIRST``/``COUNT`` pair that is
  read for it are two registries that have to agree;
* the refusal to state a refresh period that the poll interval cannot resolve.

End-to-end reads run against the probe's own loopback device, so framing and
offset detection are exercised without an E3/DC in reach. The cadence maths is
tested on synthetic timestamps rather than by waiting, so no test depends on
wall-clock timing.
"""

import contextlib
import http.server
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from ems import e3dc_modbus as modbus

pytestmark = [
    pytest.mark.unit,
]

_PROBE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "scripts",
    "e3dc_modbus_probe.py",
)
_spec = importlib.util.spec_from_file_location("e3dc_modbus_probe", _PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


# --- datatypes -------------------------------------------------------------


def test_int32_takes_the_low_word_from_the_lower_address():
    """Manual 4.2: ``[40075] * 65536 + [40074]``, so 40074 carries the low word."""

    assert modbus.decode_int32_word_swapped((400, 0)) == 400
    assert modbus.decode_int32_word_swapped((64936, 65535)) == -600
    assert modbus.decode_int32_word_swapped((0, 0)) == 0


def test_int32_read_in_the_wrong_word_order_would_not_give_minus_600():
    """Guards the trap itself: the transposed reading is a different number."""

    assert modbus.decode_int32_word_swapped((65535, 64936)) != -600


def test_int32_covers_the_full_signed_range():
    assert modbus.decode_int32_word_swapped((0xFFFF, 0x7FFF)) == 2147483647
    assert modbus.decode_int32_word_swapped((0x0000, 0x8000)) == -2147483648


def test_int16_is_signed_and_uint16_is_not():
    assert modbus.decode_int16((0xFFFF,)) == -1
    assert modbus.decode_int16((0x8000,)) == -32768
    assert modbus.decode_uint16((0xFFFF,)) == 65535


def test_byte_pair_splits_autarky_from_self_consumption():
    """Manual 4.3: 6738 means 26 % autarky and 82 % self-consumption."""

    assert modbus.decode_byte_pair((6738,)) == [26, 82]


def test_string_stops_at_the_first_nul_and_trims():
    registers = tuple(probe.struct.unpack(">16H", b"HagerEnergy GmbH".ljust(32, b"\x00")))
    assert modbus.decode_string(registers) == "HagerEnergy GmbH"


def test_version_register_is_two_bytes():
    assert modbus.decode_version((0x0104,)) == "1.4"


def test_scaling_applies_only_to_numeric_datatypes():
    current = modbus.Field("c", 40099, 1, "uint16", "current", "A", scale=0.01)
    assert current.decode((512,)) == 5.12
    packed = modbus.Field("p", 40082, 1, "u8u8", "packed", "%", scale=0.01)
    assert packed.decode((6738,)) == [26, 82]


# --- register tables -------------------------------------------------------


def test_documented_addresses_match_the_manual():
    addresses = {item.key: item.address for item in modbus.POWER_FIELDS}
    assert addresses["pv_power"] == 40068
    assert addresses["battery_power"] == 40070
    assert addresses["home_power"] == 40072
    assert addresses["grid_power"] == 40074
    assert addresses["additional_power"] == 40076
    assert addresses["autarky_self_consumption"] == 40082
    assert addresses["battery_soc"] == 40083


def test_inverter_active_power_sits_at_offsets_six_eight_ten():
    offsets = {item.key: item.address for item in modbus.INVERTER_FIELDS}
    assert offsets["active_power_l1"] == 6
    assert offsets["active_power_l2"] == 8
    assert offsets["active_power_l3"] == 10
    assert modbus.INVERTER_FIRST == 41000
    assert modbus.INVERTER_STRIDE == 34


def test_signed_quantities_use_a_signed_datatype():
    """Battery and grid power carry direction in the sign; unsigned would hide it."""

    signed = {item.key: item.datatype for item in modbus.POWER_FIELDS}
    assert signed["battery_power"] == "int32sw"
    assert signed["grid_power"] == "int32sw"
    for item in modbus.INVERTER_FIELDS:
        assert item.datatype in {"int16", "int32sw"}, item.key


BLOCKS = [
    ("identification", modbus.IDENTIFICATION_FIRST, modbus.IDENTIFICATION_COUNT,
     modbus.IDENTIFICATION_FIELDS),
    ("power", modbus.POWER_FIRST, modbus.POWER_COUNT, modbus.POWER_FIELDS),
    ("dc_strings", modbus.DC_STRING_FIRST, modbus.DC_STRING_COUNT, modbus.DC_STRING_FIELDS),
    ("inverter", 0, modbus.INVERTER_COUNT, modbus.INVERTER_FIELDS),
    ("power_meters", modbus.POWER_METER_FIRST, modbus.POWER_METER_COUNT,
     tuple(item for index in modbus.POWER_METER_INDICES
           for item in modbus.power_meter_fields(index))),
]


@pytest.mark.parametrize(("name", "first", "count", "fields"), BLOCKS)
def test_every_field_lies_inside_the_block_that_is_read_for_it(name, first, count, fields):
    """The field list and the block bounds are two registries that must agree."""

    for item in fields:
        start = item.address - first
        assert start >= 0, f"{name}/{item.key} starts before the block"
        assert start + item.length <= count, f"{name}/{item.key} runs past the block"


@pytest.mark.parametrize(("name", "first", "count", "fields"), BLOCKS)
def test_block_fits_one_modbus_request(name, first, count, fields):
    assert count <= modbus.MAX_REGISTERS_PER_READ, name


@pytest.mark.parametrize(("name", "first", "count", "fields"), BLOCKS)
def test_field_keys_and_datatypes_are_known(name, first, count, fields):
    keys = [item.key for item in fields]
    assert len(keys) == len(set(keys)), name
    for item in fields:
        assert item.datatype in modbus.DECODERS, f"{name}/{item.key}"


def test_power_meter_blocks_do_not_overlap():
    seen = set()
    for index in modbus.POWER_METER_INDICES:
        for item in modbus.power_meter_fields(index):
            assert item.address not in seen, item.key
            seen.add(item.address)
    assert min(seen) == modbus.POWER_METER_FIRST
    assert max(seen) == modbus.POWER_METER_FIRST + modbus.POWER_METER_COUNT - 1


def test_documented_power_meter_types_are_complete():
    assert set(modbus.POWER_METER_TYPES) == set(range(1, 11))
    assert "root power meter" in modbus.POWER_METER_TYPES[1]


# --- read-only guarantee ---------------------------------------------------


def test_only_the_two_read_function_codes_are_allowed():
    assert modbus.READ_ONLY_FUNCTION_CODES == {3, 4}


@pytest.mark.parametrize("function_code", [1, 2, 5, 6, 8, 15, 16, 22, 23])
def test_request_builder_refuses_every_non_read_function_code(function_code):
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            with pytest.raises(modbus.ModbusError, match="not a read"):
                client.read_registers(40000, 1, function_code)


@pytest.mark.parametrize("count", [0, -1, modbus.MAX_REGISTERS_PER_READ + 1])
def test_implausible_register_counts_are_refused(count):
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            with pytest.raises(modbus.ModbusError, match="register count"):
                client.read_registers(40000, count)


# --- mapping detection -----------------------------------------------------


@pytest.mark.parametrize("address_offset", [-1, 0, -2, -3, 1])
def test_detection_finds_whichever_offset_the_device_uses(address_offset):
    """Manual 4.1 states the offset is not uniform, so none may be assumed."""

    words = probe.build_self_test_words(address_offset=address_offset)
    with probe._LoopbackE3dcServer(words) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            mapping, _attempts = modbus.detect_mapping(
                client, modbus.UNIT_ID_CANDIDATES, (3, 4), modbus.ADDRESS_OFFSET_CANDIDATES
            )
    assert mapping is not None
    assert mapping.address_offset == address_offset
    assert mapping.wire_address(modbus.MAGIC_ADDRESS) == modbus.MAGIC_ADDRESS + address_offset


def test_detection_finds_a_non_default_unit_id():
    words = probe.build_self_test_words()
    with probe._LoopbackE3dcServer(words, unit_id=255) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            mapping, _attempts = modbus.detect_mapping(
                client, modbus.UNIT_ID_CANDIDATES, (3,), modbus.ADDRESS_OFFSET_CANDIDATES
            )
    assert mapping is not None
    assert mapping.unit_id == 255


def test_detection_falls_back_to_input_registers():
    words = probe.build_self_test_words()
    with probe._LoopbackE3dcServer(words, function_codes=(4,)) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            mapping, _attempts = modbus.detect_mapping(
                client, (1,), (3, 4), modbus.ADDRESS_OFFSET_CANDIDATES
            )
    assert mapping is not None
    assert mapping.function_code == 4


def test_sunspec_mapping_is_reported_instead_of_guessed():
    words = probe.build_self_test_words(magic=modbus.SUNSPEC_FIRST_WORD)
    with probe._LoopbackE3dcServer(words) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            mapping, attempts = modbus.detect_mapping(
                client, (1,), (3,), modbus.ADDRESS_OFFSET_CANDIDATES
            )
    assert mapping is None
    assert modbus.looks_like_sunspec(attempts)


# --- reading -------------------------------------------------------------


def _read(fields, first, count, base=0, unsupported=frozenset()):
    words = probe.build_self_test_words()
    with probe._LoopbackE3dcServer(words, unsupported=unsupported) as server:
        with modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            mapping = modbus.Mapping(1, 3, -1, modbus.MAGIC_VALUE)
            readings = modbus.read_fields(client, mapping, fields, first, count, base=base)
    return {item.field.key: item for item in readings}


def test_power_block_decodes_signs_and_percentages():
    values = _read(modbus.POWER_FIELDS, modbus.POWER_FIRST, modbus.POWER_COUNT)
    assert values["pv_power"].value == 4321
    assert values["battery_power"].value == -1500
    assert values["home_power"].value == 800
    assert values["grid_power"].value == -600
    assert values["battery_soc"].value == 87
    assert values["autarky_self_consumption"].value == [26, 82]
    assert all(item.available for item in values.values())


def test_inverter_block_decodes_active_power_per_phase():
    values = _read(modbus.INVERTER_FIELDS, 0, modbus.INVERTER_COUNT, base=modbus.INVERTER_FIRST)
    assert values["active_power_l1"].value == 1234
    assert values["active_power_l2"].value == 1200
    assert values["active_power_l3"].value == 1190
    assert values["ac_voltage_l1"].value == 230.1
    assert values["frequency"].value == 50.01


def test_a_based_block_reports_its_absolute_register():
    """An inverter value must name register 41006, not the offset 6 inside its block."""

    values = _read(modbus.INVERTER_FIELDS, 0, modbus.INVERTER_COUNT, base=modbus.INVERTER_FIRST)
    assert values["active_power_l1"].register == 41006
    assert values["active_power_l3"].register == 41010
    payload = probe.readings_to_json(values.values())
    assert payload["active_power_l1"]["register"] == 41006


def test_an_unbased_block_reports_the_manual_register_unchanged():
    values = _read(modbus.POWER_FIELDS, modbus.POWER_FIRST, modbus.POWER_COUNT)
    assert values["pv_power"].register == 40068
    assert values["battery_soc"].register == 40083


def test_one_refused_register_does_not_hide_its_neighbours():
    values = _read(
        modbus.DC_STRING_FIELDS, modbus.DC_STRING_FIRST, modbus.DC_STRING_COUNT,
        unsupported={modbus.DC_STRING_FIRST - 1},
    )
    assert values["dc_string_1_voltage"].available is False
    assert values["dc_string_1_voltage"].error
    assert values["dc_string_2_voltage"].value == 375
    assert values["dc_string_1_current"].value == 5.12


def test_unavailable_reading_reports_no_value_in_json():
    values = _read(
        modbus.DC_STRING_FIELDS, modbus.DC_STRING_FIRST, modbus.DC_STRING_COUNT,
        unsupported={modbus.DC_STRING_FIRST - 1},
    )
    payload = probe.readings_to_json(values.values())
    assert payload["dc_string_1_voltage"]["available"] is False
    assert payload["dc_string_1_voltage"]["value"] is None
    assert payload["dc_string_2_voltage"]["value"] == 375
    assert payload["dc_string_2_voltage"]["register"] == 40097
    assert payload["dc_string_1_current"]["scaling"] == 0.01


# --- cadence maths --------------------------------------------------------


def test_gap_statistics_reports_the_modal_interval():
    stats = probe.gap_statistics([0.0, 1.0, 2.0, 3.0, 4.5])
    assert stats["changes"] == 5
    assert stats["gaps"] == 4
    assert stats["min_s"] == 1.0
    assert stats["max_s"] == 1.5
    assert stats["modal_gap_s"] == 1.0
    assert stats["modal_share"] == 0.75


def test_gap_statistics_needs_two_changes():
    assert probe.gap_statistics([]) is None
    assert probe.gap_statistics([1.0]) is None


def test_no_observed_change_is_not_reported_as_a_period():
    estimate = probe._cadence_estimate(None, {"pv_power": {"changed": False}}, 0.25)
    assert estimate["refresh_period_s"] is None
    assert estimate["confidence"] == "none"


def test_a_gap_within_twice_the_poll_interval_is_called_undersampled():
    """Above the floor, a period the sampling cannot resolve says so."""

    combined = probe.gap_statistics([0.0, 2.0, 4.0, 6.0])
    estimate = probe._cadence_estimate(combined, {"pv_power": {"changed": True}}, 5.0)
    assert estimate["confidence"] == "undersampled"
    assert "poll faster" in estimate["reason"]


def test_at_the_poll_floor_an_unresolved_period_is_a_lower_bound_not_a_complaint():
    """At 1 s we stop asking, so the honest answer is 'at least this often'."""

    combined = probe.gap_statistics([0.0, 1.0, 2.0, 3.0])
    estimate = probe._cadence_estimate(
        combined, {"pv_power": {"changed": True}}, probe.MINIMUM_POLL_INTERVAL
    )
    assert estimate["confidence"] == "at_poll_floor"
    assert estimate["refresh_period_s"] == 1.0
    assert "at least" in estimate["reason"]
    assert "poll faster" not in estimate["reason"]


def test_a_well_resolved_period_is_reported_with_confidence():
    combined = probe.gap_statistics([0.0, 5.0, 10.0, 15.0, 20.0])
    estimate = probe._cadence_estimate(combined, {"pv_power": {"changed": True}}, 1.0)
    assert estimate["refresh_period_s"] == 5.0
    assert estimate["confidence"] == "high"
    assert estimate["changed_fields"] == ["pv_power"]


def test_a_single_interval_is_never_high_confidence():
    """100 % of one interval is arithmetically true and evidentially worthless."""

    combined = probe.gap_statistics([0.0, 3.0])
    assert combined["gaps"] == 1
    assert combined["modal_share"] == 1.0
    estimate = probe._cadence_estimate(combined, {"pv_power": {"changed": True}}, 1.0)
    assert estimate["confidence"] == "low"
    assert estimate["refresh_period_s"] == 3.0
    assert "only 1 interval observed" in estimate["reason"]


def test_confidence_rises_once_enough_intervals_agree():
    thin = probe.gap_statistics([0.0, 5.0, 10.0])
    assert probe._cadence_estimate(thin, {"k": {"changed": True}}, 1.0)["confidence"] == "low"
    enough = probe.gap_statistics([0.0, 5.0, 10.0, 15.0])
    assert probe._cadence_estimate(enough, {"k": {"changed": True}}, 1.0)["confidence"] == "high"


def test_a_scattered_period_is_reported_with_low_confidence():
    combined = probe.gap_statistics([0.0, 3.0, 9.1, 12.0, 21.2])
    estimate = probe._cadence_estimate(combined, {"pv_power": {"changed": True}}, 1.0)
    assert estimate["confidence"] == "low"


# --- cli ------------------------------------------------------------------


def test_host_is_required_unless_self_testing():
    with pytest.raises(SystemExit):
        probe.parse_arguments([])
    assert probe.parse_arguments(["--self-test"]).self_test is True


def test_measurement_window_must_hold_at_least_one_interval():
    with pytest.raises(SystemExit):
        probe.parse_arguments(["10.0.0.1", "--measure-seconds", "2", "--interval", "3"])


@pytest.mark.parametrize("interval", ["0", "0.25", "0.9", "-1"])
def test_polling_faster_than_the_floor_is_refused(interval):
    """Sub-second polling of an inverter buys nothing a control loop can use."""

    with pytest.raises(SystemExit):
        probe.parse_arguments(["10.0.0.1", "--interval", interval])


def test_the_floor_itself_is_accepted():
    args = probe.parse_arguments(["10.0.0.1", "--interval", "1"])
    assert args.interval == probe.MINIMUM_POLL_INTERVAL


def test_the_default_interval_is_a_control_loop_period():
    assert probe.DEFAULT_POLL_INTERVAL == 3.0
    assert probe.parse_arguments(["10.0.0.1"]).interval == 3.0


def test_cli_only_offers_read_function_codes():
    with pytest.raises(SystemExit):
        probe.parse_arguments(["10.0.0.1", "--function-code", "6"])
    assert probe.parse_arguments(["10.0.0.1", "--function-code", "4"]).function_code == 4


def test_unreachable_host_reports_failure_without_raising(capsys):
    args = probe.parse_arguments(["127.0.0.1", "--port", "1", "--timeout", "0.2"])
    result, status = probe.run_probe(args)
    assert status == 1
    assert result["modbus_tcp"]["reachable"] is False
    assert "E3/DC Simple Mode" in capsys.readouterr().out


# --- live view ------------------------------------------------------------


@contextlib.contextmanager
def _served(state):
    """The live HTTP surface with no Modbus client anywhere near it."""

    handler = type("_BoundTestHandler", (probe._LiveRequestHandler,), {"state": state})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _synthetic_state():
    state = probe.LiveState(0.25)
    state.publish({
        "device": {"model": "S10 E AIO"},
        "mapping": {"unit_id": 1, "function_code": 3, "address_offset": -1},
        "values": {"pv_power": {"label": "PV power", "available": True, "value": 4321,
                                "unit": "W", "register": 40068, "changes": 0}},
        "cadence": {"refresh_period_s": None, "confidence": "none", "reason": "nothing yet"},
        "polls": 1,
        "errors": 0,
        "error": "",
        "poll_interval_s": 0.25,
        "wall_clock": 1.0,
    })
    return state


def test_live_state_hands_out_the_latest_payload():
    state = _synthetic_state()
    assert state.snapshot()["values"]["pv_power"]["value"] == 4321
    assert state.snapshot()["generation"] == 1


def test_live_state_generation_advances_with_every_publish():
    state = probe.LiveState(0.25)
    assert state.snapshot()["generation"] == 0
    state.publish({"values": {}})
    state.publish({"values": {}})
    assert state.snapshot()["generation"] == 2


def test_a_viewer_that_has_not_seen_the_latest_gets_it_without_waiting():
    state = _synthetic_state()
    assert state.wait_for_next(seen=0, timeout=0.05)["generation"] == 1


def test_a_viewer_that_is_up_to_date_is_told_nothing_changed():
    state = _synthetic_state()
    assert state.wait_for_next(seen=1, timeout=0.05) is None


def test_a_waiting_viewer_is_woken_by_a_publish():
    """Pins the wake-up, not a duration: the wait must end on the publish."""

    state = _synthetic_state()
    woken = threading.Event()
    received = []

    def viewer():
        received.append(state.wait_for_next(seen=1, timeout=5.0))
        woken.set()

    thread = threading.Thread(target=viewer, daemon=True)
    thread.start()
    state.publish({"values": {}, "marker": "second"})
    assert woken.wait(5.0), "publish did not wake the waiting viewer"
    thread.join(timeout=2)
    assert received[0]["marker"] == "second"


def test_the_page_the_snapshot_and_the_stream_are_served():
    state = _synthetic_state()
    with _served(state) as base:
        with urllib.request.urlopen(f"{base}/", timeout=5) as page:
            body = page.read().decode()
            assert page.status == 200
            assert "E3/DC live values" in body
            assert "text/html" in page.headers["Content-Type"]
        with urllib.request.urlopen(f"{base}/api/snapshot", timeout=5) as api:
            payload = json.loads(api.read())
            assert api.status == 200
            assert payload["values"]["pv_power"]["value"] == 4321


def test_an_unknown_path_is_not_found():
    with _served(_synthetic_state()) as base:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{base}/nope", timeout=5)
        assert caught.value.code == 404


def test_the_stream_delivers_the_current_reading_as_one_event():
    with _served(_synthetic_state()) as base:
        with urllib.request.urlopen(f"{base}/events", timeout=5) as stream:
            assert stream.headers["Content-Type"] == "text/event-stream"
            line = stream.readline().decode()
            assert line.startswith("data: ")
            assert json.loads(line[6:])["values"]["pv_power"]["value"] == 4321


def test_the_reading_keeps_its_register_order_for_the_page():
    """Alphabetical keys would scatter PV, battery and grid across the table."""

    blocks = [probe.Block("power", modbus.POWER_FIRST, modbus.POWER_COUNT, modbus.POWER_FIELDS)]
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        client, state, poller = _poller_against_loopback(server, blocks)
        try:
            poller._poll_once()
        finally:
            client.close()
    keys = list(state.snapshot()["values"])
    assert keys[:4] == ["pv_power", "battery_power", "home_power", "grid_power"]
    with _served(state) as base:
        with urllib.request.urlopen(f"{base}/api/snapshot", timeout=5) as api:
            assert list(json.loads(api.read())["values"]) == keys


def test_the_live_surface_never_offers_a_write_verb():
    """GET only: no do_POST/do_PUT/do_DELETE/do_PATCH on the handler."""

    for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH", "do_HEAD"):
        assert not hasattr(probe._LiveRequestHandler, verb), verb
    assert hasattr(probe._LiveRequestHandler, "do_GET")


def _poller_against_loopback(server, blocks, interval=0.25):
    client = modbus.ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0)
    client.connect()
    state = probe.LiveState(interval)
    mapping = modbus.Mapping(1, 3, -1, modbus.MAGIC_VALUE)
    poller = probe.LivePoller(client, mapping, blocks, state, {"model": "S10"}, interval)
    return client, state, poller


def test_one_poll_publishes_the_values_the_page_shows():
    blocks = [probe.Block("power", modbus.POWER_FIRST, modbus.POWER_COUNT, modbus.POWER_FIELDS)]
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        client, state, poller = _poller_against_loopback(server, blocks)
        try:
            poller._poll_once()
        finally:
            client.close()
    payload = state.snapshot()
    assert payload["polls"] == 1
    assert payload["errors"] == 0
    assert payload["values"]["pv_power"]["value"] == 4321
    assert payload["values"]["battery_power"]["value"] == -1500
    assert payload["values"]["battery_soc"]["unit"] == "%"
    assert payload["mapping"]["address_offset"] == -1


def test_the_inverter_total_is_marked_derived_and_names_its_registers():
    blocks = [
        probe.Block("power", modbus.POWER_FIRST, modbus.POWER_COUNT, modbus.POWER_FIELDS),
        probe.Block("inverter_0", 0, modbus.INVERTER_COUNT, modbus.INVERTER_FIELDS,
                    base=modbus.INVERTER_FIRST),
    ]
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        client, state, poller = _poller_against_loopback(server, blocks)
        try:
            poller._poll_once()
        finally:
            client.close()
    total = state.snapshot()["values"][probe.DERIVED_INVERTER_TOTAL]
    assert total["value"] == 1234 + 1200 + 1190
    assert total["register"] is None
    assert total["derived_from"] == "41006, 41008, 41010 (sum)"


def test_no_inverter_block_means_no_derived_total():
    blocks = [probe.Block("power", modbus.POWER_FIRST, modbus.POWER_COUNT, modbus.POWER_FIELDS)]
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        client, state, poller = _poller_against_loopback(server, blocks)
        try:
            poller._poll_once()
        finally:
            client.close()
    assert probe.DERIVED_INVERTER_TOTAL not in state.snapshot()["values"]


def test_a_changing_value_is_counted_and_a_still_one_is_not():
    blocks = [probe.Block("power", modbus.POWER_FIRST, modbus.POWER_COUNT, modbus.POWER_FIELDS)]
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        client, state, poller = _poller_against_loopback(server, blocks)
        try:
            poller._poll_once()
            server.words[modbus.POWER_FIRST - 1] = 5000
            poller._poll_once()
            poller._poll_once()
        finally:
            client.close()
    values = state.snapshot()["values"]
    assert values["pv_power"]["value"] == 5000
    assert values["pv_power"]["changes"] == 1
    assert values["home_power"]["changes"] == 0
    assert state.snapshot()["polls"] == 3


def test_a_read_failure_is_surfaced_instead_of_freezing_the_page():
    blocks = [probe.Block("dc", modbus.DC_STRING_FIRST, modbus.DC_STRING_COUNT,
                          modbus.DC_STRING_FIELDS)]
    unsupported = {modbus.DC_STRING_FIRST - 1}
    with probe._LoopbackE3dcServer(probe.build_self_test_words(),
                                   unsupported=unsupported) as server:
        client, state, poller = _poller_against_loopback(server, blocks)
        try:
            poller._poll_once()
        finally:
            client.close()
    payload = state.snapshot()
    assert payload["errors"] == 1
    assert payload["error"]
    assert payload["values"]["dc_string_1_voltage"]["available"] is False
    assert payload["values"]["dc_string_2_voltage"]["value"] == 375


def test_serve_defaults_to_localhost_because_the_page_is_unauthenticated():
    args = probe.parse_arguments(["10.0.0.1", "--serve", "8088"])
    assert args.bind == "127.0.0.1"
    assert args.serve == 8088
    assert args.interval == probe.DEFAULT_POLL_INTERVAL


@pytest.mark.parametrize("extra", [
    ["--measure-seconds", "60"],
    ["--json", "/dev/null"],
])
def test_serve_refuses_the_options_it_cannot_honour(extra):
    with pytest.raises(SystemExit):
        probe.parse_arguments(["10.0.0.1", "--serve", "8088", *extra])


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_serve_refuses_an_impossible_port(port):
    with pytest.raises(SystemExit):
        probe.parse_arguments(["10.0.0.1", "--serve", port])
