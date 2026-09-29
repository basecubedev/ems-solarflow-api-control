# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only Modbus/TCP probe for an E3/DC (HagerEnergy) energy storage system.

A diagnostic tool beside the EMS integration. It answers three questions about
a real installation:

1. Does the system answer Modbus/TCP at all, and in which register mapping?
2. Which of the documented power values are actually readable, and with which
   address offset, unit id, function code and datatype?
3. How often does the system refresh those values? The E3/DC manual documents
   no refresh rate, so the cadence has to be measured against the device.

Confirmed against an S10 M4 on 2026-09-27: offset -1, unit id 1, function code
3, and a 1 s refresh rate. The phase frequency register is the exception at
20 s, and the root power meter sat at index 6 rather than 0.

Register knowledge, the decoders and the read-only Modbus/TCP client live in
``ems/e3dc_modbus.py``, which is also what the EMS grid-meter client reads
through. The probe imports that one module and nothing else from ``ems``, so the
EMS and this probe cannot disagree about a register.

Safety: the shared client only ever issues Modbus function codes 3 and 4, both
reads. There is no code path that can emit a write function code -- the request
builder refuses anything outside ``READ_ONLY_FUNCTION_CODES``. The probe does not
touch config.json, runtime state or a running EMS.

Usage:

    python3 scripts/e3dc_modbus_probe.py 192.168.1.50
    python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --inverter 0 --power-meters
    python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --measure-seconds 120
    python3 scripts/e3dc_modbus_probe.py 192.168.1.50 --inverter 0 --serve 8088
    python3 scripts/e3dc_modbus_probe.py --self-test

``--serve`` adds a live page: one poller owns the Modbus connection and pushes
each new reading to every viewer over server-sent events, so the number of open
browsers does not change what the device sees. It binds to localhost by default
because the page is unauthenticated and shows a household's power data.

Polling defaults to 3 s and is floored at 1 s, because that is the period a
control loop actually runs at. The cost is deliberate: a refresh faster than
1 s cannot be told apart from our own sampling, so it is reported as "at least
this often" rather than as a number.
"""

import argparse
import http.server
import json
import socket
import statistics
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ems.e3dc_modbus import (  # noqa: E402
    ADDRESS_OFFSET_CANDIDATES,
    DC_STRING_COUNT,
    DC_STRING_FIELDS,
    DC_STRING_FIRST,
    IDENTIFICATION_COUNT,
    IDENTIFICATION_FIELDS,
    IDENTIFICATION_FIRST,
    INVERTER_COUNT,
    INVERTER_FIELDS,
    INVERTER_FIRST,
    INVERTER_STRIDE,
    MAGIC_ADDRESS,
    MAGIC_VALUE,
    MODBUS_TCP_PORT,
    POWER_COUNT,
    POWER_FIELDS,
    POWER_FIRST,
    POWER_METER_COUNT,
    POWER_METER_FIRST,
    POWER_METER_INDICES,
    POWER_METER_TYPES,
    READ_HOLDING_REGISTERS,
    READ_INPUT_REGISTERS,
    READ_ONLY_FUNCTION_CODES,
    SUNSPEC_FIRST_WORD,
    UNIT_ID_CANDIDATES,
    detect_mapping,
    looks_like_sunspec,
    Mapping,
    power_meter_fields,
    read_fields,
    ReadOnlyModbusTcpClient,
)
from ems.e3dc_modbus import ModbusError as ProbeError  # noqa: E402

DEFAULT_POLL_INTERVAL = 3.0
MINIMUM_POLL_INTERVAL = 1.0
MINIMUM_GAPS_FOR_HIGH_CONFIDENCE = 3

INVERTER_CADENCE_KEYS = ("active_power_l1", "active_power_l2", "active_power_l3")


@dataclass
class Block:
    """One contiguous group of registers polled as a unit."""

    name: str
    first: int
    count: int
    fields: tuple
    base: int = 0


@dataclass
class Series:
    """Observed change instants of one field during a cadence measurement."""

    key: str
    label: str
    unit: str
    samples: int = 0
    failures: int = 0
    first_value: object = None
    last_value: object = None
    change_times: list = field(default_factory=list)
    _seen: bool = False

    def observe(self, value, timestamp):
        self.samples += 1
        if not self._seen:
            self._seen = True
            self.first_value = value
            self.last_value = value
            return
        if value != self.last_value:
            self.change_times.append(timestamp)
            self.last_value = value


def gap_statistics(timestamps, bucket=0.1):
    """Summarise the intervals between consecutive changes of one value."""

    gaps = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])]
    if not gaps:
        return None
    buckets = {}
    for gap in gaps:
        key = round(round(gap / bucket) * bucket, 3)
        buckets[key] = buckets.get(key, 0) + 1
    modal_gap, modal_count = max(buckets.items(), key=lambda item: (item[1], -item[0]))
    ordered = sorted(gaps)
    return {
        "changes": len(timestamps),
        "gaps": len(gaps),
        "min_s": round(ordered[0], 3),
        "median_s": round(statistics.median(ordered), 3),
        "max_s": round(ordered[-1], 3),
        "modal_gap_s": modal_gap,
        "modal_share": round(modal_count / len(gaps), 3),
        "histogram": dict(sorted(buckets.items())),
    }


def measure_cadence(client, mapping, blocks, duration, interval):
    """Poll faster than the expected refresh and time the value changes.

    The refresh rate is not documented, so it is inferred from how far apart
    consecutive distinct values are. Sampling is on an absolute grid so a slow
    response does not accumulate drift into the measured intervals.
    """

    series = {}
    for block in blocks:
        for item in block.fields:
            series[item.key] = Series(item.key, item.label, item.unit)
    any_change_times = []
    round_trips = []
    read_failures = []
    started = time.monotonic()
    tick = 0
    skipped_ticks = 0

    while True:
        target = started + tick * interval
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        elif tick and now - target > interval:
            skipped_ticks += 1
            tick = int((now - started) / interval) + 1
            continue
        if time.monotonic() - started > duration:
            break
        tick += 1

        changed_this_tick = False
        for block in blocks:
            before = time.monotonic()
            try:
                registers = client.read_registers(
                    mapping.wire_address(block.base + block.first), block.count,
                    mapping.function_code, mapping.unit_id,
                )
            except ProbeError as exc:
                read_failures.append(str(exc))
                for item in block.fields:
                    series[item.key].failures += 1
                try:
                    client.connect()
                except ProbeError:
                    return _cadence_result(
                        series, any_change_times, round_trips, read_failures,
                        duration, interval, skipped_ticks, aborted="connection lost"
                    )
                continue
            observed_at = time.monotonic()
            round_trips.append(observed_at - before)
            for item in block.fields:
                start = item.address - block.first
                chunk = registers[start:start + item.length]
                if len(chunk) != item.length:
                    continue
                current = series[item.key]
                previous = current.last_value
                current.observe(item.decode(chunk), observed_at)
                if current.samples > 1 and current.last_value != previous:
                    changed_this_tick = True
        if changed_this_tick:
            any_change_times.append(time.monotonic())

    return _cadence_result(
        series, any_change_times, round_trips, read_failures, duration, interval, skipped_ticks
    )


def _cadence_result(
    series, any_change_times, round_trips, read_failures,
    duration, interval, skipped_ticks, aborted=""
):
    fields_summary = {}
    for key, entry in series.items():
        stats = gap_statistics(entry.change_times)
        fields_summary[key] = {
            "label": entry.label,
            "unit": entry.unit,
            "samples": entry.samples,
            "failures": entry.failures,
            "first_value": entry.first_value,
            "last_value": entry.last_value,
            "changed": bool(entry.change_times),
            "statistics": stats,
        }
    combined = gap_statistics(any_change_times)
    result = {
        "requested_seconds": duration,
        "poll_interval_s": interval,
        "skipped_ticks": skipped_ticks,
        "read_failures": len(read_failures),
        "first_read_failure": read_failures[0] if read_failures else "",
        "round_trip_s": _round_trip_summary(round_trips),
        "any_field": combined,
        "fields": fields_summary,
        "estimate": _cadence_estimate(combined, fields_summary, interval),
    }
    if aborted:
        result["aborted"] = aborted
    return result


def _round_trip_summary(round_trips):
    if not round_trips:
        return None
    ordered = sorted(round_trips)
    return {
        "requests": len(ordered),
        "min_s": round(ordered[0], 4),
        "median_s": round(statistics.median(ordered), 4),
        "max_s": round(ordered[-1], 4),
    }


def _cadence_estimate(combined, fields_summary, interval):
    """Turn the measured gaps into a stated refresh period, or refuse to.

    A gap that is not clearly longer than the poll interval cannot be
    distinguished from the probe's own sampling. At the poll floor that is the
    end of the answer rather than a defect: the system refreshes at least as
    often as we ask, which is what a multi-second control loop needs to know.
    Above the floor it is a reason to poll faster.
    """

    changed = [key for key, entry in fields_summary.items() if entry["changed"]]
    if combined is None:
        return {
            "refresh_period_s": None,
            "confidence": "none",
            "reason": "no value changed during the measurement window",
            "changed_fields": changed,
        }
    period = combined["modal_gap_s"]
    if combined["min_s"] <= interval * 2:
        if interval <= MINIMUM_POLL_INTERVAL:
            return {
                "refresh_period_s": period,
                "confidence": "at_poll_floor",
                "reason": (
                    f"values change as fast as the {interval:g} s poll floor allows us to see "
                    f"(shortest gap {combined['min_s']} s), so the system refreshes at least "
                    "this often; resolving it further would need sub-second polling"
                ),
                "changed_fields": changed,
            }
        return {
            "refresh_period_s": period,
            "confidence": "undersampled",
            "reason": (
                f"shortest observed gap {combined['min_s']} s is within twice the poll "
                f"interval {interval} s; poll faster (down to the "
                f"{MINIMUM_POLL_INTERVAL:g} s floor) to resolve it"
            ),
            "changed_fields": changed,
        }
    gaps = combined["gaps"]
    share = combined["modal_share"]
    if gaps < MINIMUM_GAPS_FOR_HIGH_CONFIDENCE:
        return {
            "refresh_period_s": period,
            "confidence": "low",
            "reason": (
                f"only {gaps} interval{'' if gaps == 1 else 's'} observed so far; "
                f"{MINIMUM_GAPS_FOR_HIGH_CONFIDENCE} are needed before a period is worth trusting"
            ),
            "changed_fields": changed,
        }
    return {
        "refresh_period_s": period,
        "confidence": "high" if share >= 0.6 else "low",
        "reason": f"{int(share * 100)}% of {gaps} intervals fall in the {period} s bucket",
        "changed_fields": changed,
    }


def format_value(reading):
    if not reading.available:
        return "unavailable"
    value = reading.value
    if isinstance(value, list):
        return " / ".join(str(part) for part in value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


DATATYPE_TEXT = {
    "uint16": "uint16 big-endian",
    "int16": "int16 big-endian",
    "int32sw": "int32 word-swapped",
    "string": "string (16 registers)",
    "version": "uint8 + uint8",
    "u8u8": "uint8 + uint8",
}


def print_reading_table(title, readings):
    print(f"\n{title}")
    if not readings:
        print("  (nothing read)")
        return
    width = max(len(item.field.label) for item in readings)
    for item in readings:
        text = format_value(item)
        unit = f" {item.field.unit}" if item.field.unit and item.available else ""
        line = f"  {item.field.label:<{width}}  {text:>12}{unit}"
        details = f"reg {item.register}, {DATATYPE_TEXT[item.field.datatype]}"
        if item.field.scale != 1.0:
            details += f", x{item.field.scale:g}"
        note = item.field.note or ("" if item.available else item.error)
        if not item.available and item.error:
            note = item.error
        print(f"{line:<{width + 22}}   [{details}]" + (f"  {note}" if note else ""))


def print_cadence(cadence):
    print("\nRefresh cadence")
    print(f"  Window:          {cadence['requested_seconds']:g} s")
    print(f"  Poll interval:   {cadence['poll_interval_s']:g} s")
    round_trip = cadence["round_trip_s"]
    if round_trip:
        print(
            f"  Round trip:      min {round_trip['min_s'] * 1000:.1f} ms, "
            f"median {round_trip['median_s'] * 1000:.1f} ms, "
            f"max {round_trip['max_s'] * 1000:.1f} ms  ({round_trip['requests']} requests)"
        )
    if cadence["read_failures"]:
        print(f"  Read failures:   {cadence['read_failures']} ({cadence['first_read_failure']})")
    if cadence["skipped_ticks"]:
        print(f"  Skipped ticks:   {cadence['skipped_ticks']}")
    if cadence.get("aborted"):
        print(f"  Aborted:         {cadence['aborted']}")

    estimate = cadence["estimate"]
    period = estimate["refresh_period_s"]
    period_text = "not determined" if period is None else f"{period:g} s"
    print(f"\n  Refresh period:  {period_text}   (confidence: {estimate['confidence']})")
    print(f"  Basis:           {estimate['reason']}")

    combined = cadence["any_field"]
    if combined:
        print(
            f"  Any value:       {combined['changes']} changes, gaps min {combined['min_s']:g} s / "
            f"median {combined['median_s']:g} s / max {combined['max_s']:g} s"
        )
        print(f"  Gap histogram:   {combined['histogram']}")

    print("\n  Per value:")
    width = max(len(entry["label"]) for entry in cadence["fields"].values())
    for entry in cadence["fields"].values():
        stats = entry["statistics"]
        if stats is None:
            detail = f"no change in {entry['samples']} samples (value {entry['last_value']})"
        else:
            detail = (
                f"{stats['changes']} changes, modal gap {stats['modal_gap_s']:g} s "
                f"({int(stats['modal_share'] * 100)}%), min {stats['min_s']:g} s"
            )
        print(f"    {entry['label']:<{width}}  {detail}")


def readings_to_json(readings):
    payload = {}
    for item in readings:
        payload[item.field.key] = {
            "available": item.available,
            "value": item.value if item.available else None,
            "unit": item.field.unit,
            "register": item.register,
            "datatype": item.field.datatype,
            "scaling": item.field.scale,
            "note": item.field.note,
            "error": item.error,
        }
    return payload


def run_probe(args):
    """Connect, establish the mapping, read the requested blocks, report."""

    unit_ids = UNIT_ID_CANDIDATES if args.unit_id is None else (args.unit_id,)
    function_codes = (
        (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS)
        if args.function_code is None
        else (args.function_code,)
    )

    result = {
        "schema": "e3dc-modbus-probe/1",
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "target": {"host": args.host, "port": args.port},
        "modbus_tcp": {"reachable": False},
    }

    print("E3/DC Modbus TCP probe")
    print("=" * 22)
    print(f"\nTarget:          {args.host}:{args.port}")

    client = ReadOnlyModbusTcpClient(args.host, args.port, timeout=args.timeout)
    try:
        client.connect()
    except ProbeError as exc:
        print(f"Connection:      FAILED - {exc}")
        print(
            "\nModbus/TCP is not answering. On the device, enable "
            "Main menu > Functions > Modbus with protocol 'E3/DC Simple Mode'.\n"
            "The interface is reachable only from the same subnet."
        )
        result["modbus_tcp"]["error"] = str(exc)
        return result, 1

    print(f"Connection:      OK ({client.connect_seconds * 1000:.1f} ms)")

    with client:
        mapping, attempts = detect_mapping(
            client, unit_ids, function_codes, ADDRESS_OFFSET_CANDIDATES
        )
        result["modbus_tcp"]["reachable"] = True
        result["modbus_tcp"]["detection_attempts"] = [
            {
                "unit_id": item.unit_id,
                "function_code": item.function_code,
                "address_offset": item.address_offset,
                "outcome": item.outcome,
            }
            for item in attempts
        ]
        if mapping is None:
            print("Protocol:        NOT IDENTIFIED - magic word 0xE3DC not found")
            if looks_like_sunspec(attempts):
                print(
                    "                 A SunSpec base address answered instead. The device is in\n"
                    "                 SunSpec mode; switch it to 'E3/DC Simple Mode'."
                )
                result["modbus_tcp"]["protocol"] = "sunspec_mode"
            else:
                result["modbus_tcp"]["protocol"] = "unknown"
            print("\nTried (unit id / function code / offset -> answer):")
            for item in attempts:
                print(
                    f"  {item.unit_id:>3} / {item.function_code} / {item.address_offset:+d}"
                    f"  -> {item.outcome}"
                )
            return result, 1

        print("Protocol:        E3/DC Simple Mode (magic 0xE3DC)")
        print(f"Unit ID:         {mapping.unit_id}")
        print(
            f"Function code:   {mapping.function_code} "
            f"({'read holding registers' if mapping.function_code == 3 else 'read input registers'})"
        )
        print(
            f"Address offset:  {mapping.address_offset:+d} "
            f"(manual register {MAGIC_ADDRESS} -> wire address {mapping.wire_address(MAGIC_ADDRESS)})"
        )
        result["modbus_tcp"].update(
            {
                "protocol": "e3dc_simple_mode",
                "port": args.port,
                "unit_id": mapping.unit_id,
                "function_code": mapping.function_code,
                "address_offset": mapping.address_offset,
                "magic": f"0x{mapping.magic_value:04X}",
            }
        )

        identification = read_fields(
            client, mapping, IDENTIFICATION_FIELDS, IDENTIFICATION_FIRST, IDENTIFICATION_COUNT
        )
        print_reading_table("Identification", identification)
        result["identification"] = readings_to_json(identification)

        blocks = [Block("power", POWER_FIRST, POWER_COUNT, POWER_FIELDS)]
        if args.dc_strings:
            blocks.append(Block("dc_strings", DC_STRING_FIRST, DC_STRING_COUNT, DC_STRING_FIELDS))
        if args.power_meters:
            meter_fields = tuple(
                item for index in POWER_METER_INDICES for item in power_meter_fields(index)
            )
            blocks.append(
                Block("power_meters", POWER_METER_FIRST, POWER_METER_COUNT, meter_fields)
            )
        for index in args.inverter or ():
            blocks.append(
                Block(
                    f"inverter_{index}", 0, INVERTER_COUNT, INVERTER_FIELDS,
                    base=INVERTER_FIRST + index * INVERTER_STRIDE,
                )
            )

        result["values"] = {}
        for block in blocks:
            readings = read_fields(
                client, mapping, block.fields, block.first, block.count, base=block.base
            )
            title = block.name.replace("_", " ").title()
            if block.base:
                title += f" (base register {block.base})"
            print_reading_table(title, readings)
            result["values"][block.name] = readings_to_json(readings)
            if block.name == "power_meters":
                _print_power_meter_types(readings)

        if args.serve is not None:
            device = {
                item.field.key: item.value
                for item in identification
                if item.available and item.field.datatype in {"string", "version"}
            }
            result["live_view"] = {"bind": args.bind, "port": args.serve}
            return result, serve_live(args, client, mapping, blocks, device)

        if args.measure_seconds > 0:
            print(
                f"\nMeasuring the refresh cadence for {args.measure_seconds:g} s "
                f"at {args.interval:g} s intervals ..."
            )
            cadence_blocks = [
                block for block in blocks if block.name == "power" or block.name.startswith("inverter_")
            ]
            cadence = measure_cadence(
                client, mapping, cadence_blocks, args.measure_seconds, args.interval
            )
            print_cadence(cadence)
            result["cadence"] = cadence
        else:
            print(
                f"\nRefresh cadence not measured. To measure it:\n"
                f"  python3 scripts/e3dc_modbus_probe.py {args.host} --measure-seconds 120"
            )

    return result, 0


def _print_power_meter_types(readings):
    by_key = {item.field.key: item for item in readings}
    print("\n  Power meter roles:")
    for index in POWER_METER_INDICES:
        entry = by_key.get(f"lm{index}_type")
        if entry is None or not entry.available or entry.value == 0:
            continue
        role = POWER_METER_TYPES.get(entry.value, "undocumented type")
        print(f"    Power meter {index}: type {entry.value} - {role}")


class _LoopbackE3dcServer:
    """Fake E3/DC Modbus/TCP device used by --self-test.

    It exists so the framing, offset detection, decoders and cadence maths can
    be verified end to end on a machine with no E3/DC in reach.
    """

    def __init__(self, words, unit_id=1, unsupported=frozenset(), function_codes=(3,)):
        self.words = dict(words)
        self.unit_id = unit_id
        self.unsupported = frozenset(unsupported)
        self.function_codes = frozenset(function_codes)
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def close(self):
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self):
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                self._handle(connection)

    def _handle(self, connection):
        connection.settimeout(0.5)
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = connection.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while len(buffer) >= 12:
                request, buffer = buffer[:12], buffer[12:]
                try:
                    connection.sendall(self._answer(request))
                except OSError:
                    return

    def _answer(self, request):
        transaction_id, _protocol, _length, unit, function, address, count = struct.unpack(
            ">HHHBBHH", request
        )
        if unit != self.unit_id or function not in self.function_codes:
            return self._exception(transaction_id, unit, function, 1)
        addresses = range(address, address + count)
        if any(item in self.unsupported or item not in self.words for item in addresses):
            return self._exception(transaction_id, unit, function, 2)
        data = struct.pack(f">{count}H", *(self.words[item] for item in addresses))
        body = struct.pack(">BBB", unit, function, len(data)) + data
        return struct.pack(">HHH", transaction_id, 0, len(body)) + body

    def _exception(self, transaction_id, unit, function, code):
        body = struct.pack(">BBB", unit, function | 0x80, code)
        return struct.pack(">HHH", transaction_id, 0, len(body)) + body


SELF_TEST_EXPECTATIONS = {
    "pv_power": 4321,
    "battery_power": -1500,
    "home_power": 800,
    "grid_power": -600,
    "additional_power": 0,
    "autarky_self_consumption": [26, 82],
    "battery_soc": 87,
    "emergency_power_status": 2,
    "ems_status": 16,
}


def build_self_test_words(address_offset=-1, magic=MAGIC_VALUE):
    """A register image of a plausible S10, addressed manual-relative."""

    words = {}

    def put(manual_address, values):
        for index, value in enumerate(values):
            words[manual_address + address_offset + index] = value & 0xFFFF

    def put_int32(manual_address, value):
        raw = value & 0xFFFFFFFF
        put(manual_address, [raw & 0xFFFF, (raw >> 16) & 0xFFFF])

    def put_string(manual_address, text):
        raw = text.encode("latin-1").ljust(32, b"\x00")[:32]
        put(manual_address, list(struct.unpack(">16H", raw)))

    for manual_address in range(40001, 40133):
        put(manual_address, [0])
    for manual_address in range(41000, 41000 + INVERTER_STRIDE):
        put(manual_address, [0])

    put(40001, [magic])
    put(40002, [0x0104])
    put(40003, [132])
    put_string(40004, "HagerEnergy GmbH")
    put_string(40020, "S10 E AIO")
    put_string(40036, "S10-12345678912")
    put_string(40052, "S10_2021_04")

    put_int32(40068, 4321)
    put_int32(40070, -1500)
    put_int32(40072, 800)
    put_int32(40074, -600)
    put_int32(40076, 0)
    put_int32(40078, 0)
    put_int32(40080, 0)
    put(40082, [26 * 256 + 82])
    put(40083, [87])
    put(40084, [2])
    put(40085, [16])

    put(40096, [380, 375, 0])
    put(40099, [512, 498, 0])
    put(40102, [2200, 2121, 0])

    put(40105, [1, 200, 210, 190])
    put(40109, [7, 0, 0, 0])

    put_int32(41000 + 6, 1234)
    put_int32(41000 + 8, 1200)
    put_int32(41000 + 10, 1190)
    put(41000 + 18, [2301, 2298, 2305])
    put(41000 + 21, [537, 530, 528])
    put(41000 + 24, [5001])
    put(41000 + 25, [2200, 2121, 0])

    return words


def _check(failures, description, actual, expected):
    if actual != expected:
        failures.append(f"{description}: expected {expected!r}, got {actual!r}")
        return False
    return True


def run_self_test():
    """Verify framing, offset detection, decoding and cadence maths offline.

    The cadence case drives ``measure_cadence`` below the CLI's poll floor on
    purpose: the floor exists to spare real hardware, and the device here is a
    thread. A sub-second source keeps the self-test short.
    """

    failures = []
    print("E3/DC Modbus TCP probe - self test")
    print("=" * 34)

    words = build_self_test_words()
    with _LoopbackE3dcServer(words, unsupported={40095}) as server:
        client = ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0)
        with client:
            mapping, attempts = detect_mapping(
                client, UNIT_ID_CANDIDATES, (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS),
                ADDRESS_OFFSET_CANDIDATES,
            )
            if mapping is None:
                failures.append("offset detection found no magic word")
            else:
                _check(failures, "detected offset", mapping.address_offset, -1)
                _check(failures, "detected unit id", mapping.unit_id, 1)
                _check(failures, "detected function code", mapping.function_code, 3)
                _check(failures, "detection attempts", len(attempts), 1)
                print(f"  offset detection          offset {mapping.address_offset:+d}, "
                      f"unit {mapping.unit_id}, fc {mapping.function_code}, "
                      f"{len(attempts)} attempt(s)")

            if mapping is not None:
                identification = {
                    item.field.key: item
                    for item in read_fields(
                        client, mapping, IDENTIFICATION_FIELDS,
                        IDENTIFICATION_FIRST, IDENTIFICATION_COUNT,
                    )
                }
                _check(failures, "manufacturer", identification["manufacturer"].value,
                       "HagerEnergy GmbH")
                _check(failures, "model", identification["model"].value, "S10 E AIO")
                _check(failures, "serial", identification["serial_number"].value,
                       "S10-12345678912")
                _check(failures, "firmware", identification["firmware_release"].value,
                       "S10_2021_04")
                _check(failures, "modbus firmware", identification["modbus_firmware"].value, "1.4")
                print("  identification block      manufacturer, model, serial, firmware decoded")

                power = {
                    item.field.key: item
                    for item in read_fields(
                        client, mapping, POWER_FIELDS, POWER_FIRST, POWER_COUNT
                    )
                }
                for key, expected in SELF_TEST_EXPECTATIONS.items():
                    _check(failures, key, power[key].value, expected)
                print("  power block               "
                      f"pv {power['pv_power'].value} W, "
                      f"battery {power['battery_power'].value} W, "
                      f"grid {power['grid_power'].value} W, "
                      f"soc {power['battery_soc'].value} %")

                inverter = {
                    item.field.key: item
                    for item in read_fields(
                        client, mapping, INVERTER_FIELDS, 0, INVERTER_COUNT, base=INVERTER_FIRST
                    )
                }
                _check(failures, "inverter active power L1",
                       inverter["active_power_l1"].value, 1234)
                _check(failures, "inverter active power L2",
                       inverter["active_power_l2"].value, 1200)
                _check(failures, "inverter active power L3",
                       inverter["active_power_l3"].value, 1190)
                _check(failures, "inverter AC voltage L1", inverter["ac_voltage_l1"].value, 230.1)
                _check(failures, "inverter frequency", inverter["frequency"].value, 50.01)
                print("  inverter block            "
                      f"active {inverter['active_power_l1'].value}/"
                      f"{inverter['active_power_l2'].value}/"
                      f"{inverter['active_power_l3'].value} W, "
                      f"{inverter['ac_voltage_l1'].value} V, "
                      f"{inverter['frequency'].value} Hz")

                meters = {
                    item.field.key: item
                    for item in read_fields(
                        client, mapping,
                        tuple(item for index in POWER_METER_INDICES
                              for item in power_meter_fields(index)),
                        POWER_METER_FIRST, POWER_METER_COUNT,
                    )
                }
                _check(failures, "power meter 0 type", meters["lm0_type"].value, 1)
                _check(failures, "power meter 0 L1", meters["lm0_l1"].value, 200)
                _check(failures, "power meter 1 type", meters["lm1_type"].value, 7)
                print("  power meter block         lm0 type 1 (root), lm1 type 7 (wallbox)")

                strings = {
                    item.field.key: item
                    for item in read_fields(
                        client, mapping, DC_STRING_FIELDS, DC_STRING_FIRST, DC_STRING_COUNT
                    )
                }
                _check(failures, "refused register is unavailable",
                       strings["dc_string_1_voltage"].available, False)
                _check(failures, "neighbour of a refused register still reads",
                       strings["dc_string_2_voltage"].value, 375)
                _check(failures, "scaled current", strings["dc_string_1_current"].value, 5.12)
                print("  single-register fallback  refused register reported unavailable, "
                      "neighbours still read")

            for forbidden in (5, 6, 15, 16):
                try:
                    client.read_registers(40000, 1, forbidden)
                except ProbeError:
                    continue
                failures.append(f"function code {forbidden} was not refused")
            print("  write function codes       5, 6, 15, 16 refused by the request builder")

    with _LoopbackE3dcServer(build_self_test_words(magic=SUNSPEC_FIRST_WORD)) as server:
        with ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
            mapping, attempts = detect_mapping(
                client, (1,), (READ_HOLDING_REGISTERS,), ADDRESS_OFFSET_CANDIDATES
            )
            _check(failures, "SunSpec image yields no E3/DC mapping", mapping, None)
            _check(failures, "SunSpec image is recognised", looks_like_sunspec(attempts), True)
            print("  wrong register mapping     SunSpec base address recognised and reported")

    period = 0.5
    with _LoopbackE3dcServer(build_self_test_words()) as server:
        stop = threading.Event()

        def advance():
            step = 0
            while not stop.wait(period):
                step += 1
                server.words[40068 - 1] = (4321 + step * 7) & 0xFFFF

        mutator = threading.Thread(target=advance, daemon=True)
        mutator.start()
        try:
            with ReadOnlyModbusTcpClient("127.0.0.1", server.port, timeout=2.0) as client:
                mapping = Mapping(1, READ_HOLDING_REGISTERS, -1, MAGIC_VALUE)
                cadence = measure_cadence(
                    client, mapping,
                    [Block("power", POWER_FIRST, POWER_COUNT, POWER_FIELDS)],
                    duration=3.0, interval=0.05,
                )
        finally:
            stop.set()
            mutator.join(timeout=2)

    estimate = cadence["estimate"]
    _check(failures, "measured refresh period", estimate["refresh_period_s"], period)
    _check(failures, "cadence confidence", estimate["confidence"], "high")
    _check(failures, "only the changing field is reported as changed",
           estimate["changed_fields"], ["pv_power"])
    print(f"  cadence measurement       {period:g} s source measured as "
          f"{estimate['refresh_period_s']} s ({estimate['confidence']})")

    print()
    if failures:
        print(f"SELF TEST FAILED ({len(failures)} problem(s)):")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("SELF TEST PASSED")
    return 0


LIVE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E3/DC live values</title>
<style>
:root {
  color-scheme: light dark;
  --ink: #16181d;
  --ink-soft: #5c6370;
  --ground: #f2f3f5;
  --card: #ffffff;
  --edge: #dcdfe4;
  --solar: #c98a12;
  --battery: #2f8f5b;
  --grid: #3b6fd4;
  --home: #7a5ac4;
  --warn: #c0392b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e8eaee;
    --ink-soft: #9aa2b1;
    --ground: #15171c;
    --card: #1e2128;
    --edge: #2e333d;
    --solar: #e8b341;
    --battery: #5ec98a;
    --grid: #6e9bf0;
    --home: #a78be0;
    --warn: #e8705f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px 16px 40px;
  background: var(--ground);
  color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1000px; margin: 0 auto; }
h1 { margin: 0 0 4px; font-size: 20px; font-weight: 650; letter-spacing: -0.01em; }
.sub { margin: 0 0 20px; color: var(--ink-soft); font-size: 13px; }
.sub code { font-size: 12px; }
.status {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 4px 10px; margin-bottom: 20px;
  border: 1px solid var(--edge); border-radius: 20px;
  background: var(--card); font-size: 13px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--battery); }
.status[data-state="stale"] .dot, .status[data-state="error"] .dot { background: var(--warn); }
.status[data-state="stale"], .status[data-state="error"] { color: var(--warn); }
.tiles {
  display: grid; gap: 12px; margin-bottom: 24px;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
}
.tile {
  padding: 14px 16px 16px;
  background: var(--card); border: 1px solid var(--edge); border-radius: 10px;
  border-top: 3px solid var(--accent, var(--edge));
}
.tile .name { color: var(--ink-soft); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
.tile .value { margin-top: 6px; font-size: 30px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.tile .value.na { font-size: 19px; color: var(--ink-soft); font-weight: 500; }
.tile .unit { font-size: 15px; font-weight: 500; color: var(--ink-soft); margin-left: 3px; }
.tile .hint { margin-top: 3px; color: var(--ink-soft); font-size: 12px; min-height: 1.4em; }
.tile.solar { --accent: var(--solar); }
.tile.battery { --accent: var(--battery); }
.tile.inverter { --accent: var(--solar); }
.tile.grid { --accent: var(--grid); }
.tile.home { --accent: var(--home); }
.tile.soc { --accent: var(--battery); }
section { margin-bottom: 24px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-soft); margin: 0 0 8px; font-weight: 600; }
.panel { background: var(--card); border: 1px solid var(--edge); border-radius: 10px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th, td { padding: 7px 14px; text-align: left; border-bottom: 1px solid var(--edge); }
th { color: var(--ink-soft); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.reg, td.meta { color: var(--ink-soft); font-size: 12.5px; white-space: nowrap; }
td.na { color: var(--ink-soft); font-style: italic; }
.cadence { padding: 14px; }
.cadence .big { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.cadence .why { color: var(--ink-soft); font-size: 12.5px; margin-top: 4px; }
footer { color: var(--ink-soft); font-size: 12px; margin-top: 28px; }
@media (max-width: 560px) { td.reg { display: none; } th.reg { display: none; } }
</style>
</head>
<body>
<main>
  <h1>E3/DC live values</h1>
  <p class="sub" id="device">reading&hellip;</p>
  <div class="status" id="status" data-state="wait"><span class="dot"></span><span id="statusText">connecting</span></div>

  <div class="tiles">
    <div class="tile solar"><div class="name">Solar</div><div class="value" id="v-pv_power">&mdash;</div><div class="hint" id="h-pv_power"></div></div>
    <div class="tile battery"><div class="name">Battery</div><div class="value" id="v-battery_power">&mdash;</div><div class="hint" id="h-battery_power"></div></div>
    <div class="tile inverter"><div class="name">Inverter</div><div class="value" id="v-inverter_active_power">&mdash;</div><div class="hint" id="h-inverter_active_power"></div></div>
    <div class="tile grid"><div class="name">Grid</div><div class="value" id="v-grid_power">&mdash;</div><div class="hint" id="h-grid_power"></div></div>
    <div class="tile home"><div class="name">Home</div><div class="value" id="v-home_power">&mdash;</div><div class="hint" id="h-home_power"></div></div>
    <div class="tile soc"><div class="name">Battery SOC</div><div class="value" id="v-battery_soc">&mdash;</div><div class="hint" id="h-battery_soc"></div></div>
  </div>

  <section>
    <h2>Observed refresh rate</h2>
    <div class="panel cadence">
      <div class="big" id="cadence">measuring&hellip;</div>
      <div class="why" id="cadenceWhy">The manual documents no refresh rate, so it is measured here.</div>
    </div>
  </section>

  <section>
    <h2>All values read</h2>
    <div class="panel">
      <table>
        <thead><tr><th>Value</th><th class="num">Reading</th><th class="reg">Register</th><th class="meta">Changes</th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </section>

  <footer id="footer"></footer>
</main>
<script>
const POWER_KEYS = ["pv_power", "battery_power", "inverter_active_power", "grid_power", "home_power", "battery_soc"];
const POWER_UNITS = new Set(["W", "VA", "var"]);
const HINTS = {
  battery_power: v => v === null ? "" : (v < 0 ? "discharging" : (v > 0 ? "charging" : "idle")),
  grid_power: v => v === null ? "" : (v < 0 ? "feeding in" : (v > 0 ? "importing" : "balanced")),
};
let lastState = null;

function shorten(value, unit) {
  if (typeof value !== "number") return value;
  return POWER_UNITS.has(unit) ? Math.round(value) : value;
}

function fmt(value, unit) {
  if (value === null || value === undefined) return null;
  if (Array.isArray(value)) return value.join(" / ") + (unit ? " " + unit : "");
  return shorten(value, unit) + (unit ? "\u2009" + unit : "");
}

function setTile(key, entry) {
  const valueEl = document.getElementById("v-" + key);
  const hintEl = document.getElementById("h-" + key);
  if (!valueEl) return;
  if (!entry || !entry.available) {
    valueEl.textContent = "unavailable";
    valueEl.classList.add("na");
    if (hintEl) hintEl.textContent = entry && entry.error ? entry.error : "";
    return;
  }
  valueEl.classList.remove("na");
  const raw = entry.value;
  const shown = Array.isArray(raw) ? raw.join(" / ") : shorten(raw, entry.unit);
  valueEl.textContent = "";
  valueEl.append(document.createTextNode(String(shown)));
  if (entry.unit) {
    const u = document.createElement("span");
    u.className = "unit";
    u.textContent = entry.unit;
    valueEl.append(u);
  }
  if (hintEl) {
    const hint = HINTS[key];
    hintEl.textContent = hint ? hint(typeof raw === "number" ? raw : null) : (entry.note || "");
  }
}

function render(state) {
  lastState = state;
  const device = state.device || {};
  const parts = [];
  if (device.model) parts.push(device.model);
  if (device.serial_number) parts.push(device.serial_number);
  if (device.firmware_release) parts.push("firmware " + device.firmware_release);
  const mapping = state.mapping || {};
  const tail = "unit " + mapping.unit_id + ", function code " + mapping.function_code + ", offset " + mapping.address_offset;
  document.getElementById("device").textContent = (parts.join(" \u00b7 ") || "E3/DC") + " \u2014 " + tail;

  for (const key of POWER_KEYS) setTile(key, state.values[key]);

  const rows = document.getElementById("rows");
  rows.textContent = "";
  for (const [key, entry] of Object.entries(state.values)) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = entry.label;
    const value = document.createElement("td");
    value.className = "num";
    if (entry.available) {
      value.textContent = fmt(entry.value, entry.unit);
    } else {
      value.className = "num na";
      value.textContent = "unavailable";
    }
    const reg = document.createElement("td");
    reg.className = "reg";
    reg.textContent = entry.derived_from ? entry.derived_from : String(entry.register);
    const meta = document.createElement("td");
    meta.className = "meta";
    meta.textContent = entry.changes === undefined ? "" : String(entry.changes);
    tr.append(name, value, reg, meta);
    rows.append(tr);
  }

  const cadence = state.cadence || {};
  const period = cadence.refresh_period_s;
  document.getElementById("cadence").textContent =
    period === null || period === undefined ? "not determined yet" : period + " s";
  document.getElementById("cadenceWhy").textContent =
    (cadence.reason || "") + (cadence.confidence ? " \u2014 confidence: " + cadence.confidence : "");

  document.getElementById("footer").textContent =
    state.polls + " polls, " + state.errors + " read errors, polling every " + state.poll_interval_s + " s. Read-only.";
  paintAge();
}

function paintAge() {
  if (!lastState) return;
  const status = document.getElementById("status");
  const text = document.getElementById("statusText");
  const age = (Date.now() - lastState.wall_clock * 1000) / 1000;
  if (lastState.error) {
    status.dataset.state = "error";
    text.textContent = "read failed: " + lastState.error;
  } else if (age > Math.max(3 * lastState.poll_interval_s, 3)) {
    status.dataset.state = "stale";
    text.textContent = "no fresh reading for " + age.toFixed(0) + " s";
  } else {
    status.dataset.state = "live";
    text.textContent = "live \u00b7 updated " + age.toFixed(1) + " s ago";
  }
}
setInterval(paintAge, 500);

fetch("api/snapshot").then(r => r.json()).then(render).catch(() => {});
const stream = new EventSource("events");
stream.onmessage = event => render(JSON.parse(event.data));
stream.onerror = () => {
  const status = document.getElementById("status");
  status.dataset.state = "error";
  document.getElementById("statusText").textContent = "lost the event stream; retrying";
};
</script>
</body>
</html>
"""


DERIVED_INVERTER_TOTAL = "inverter_active_power"


class LiveState:
    """The latest reading, shared by every browser without extra Modbus traffic.

    One poller owns the Modbus connection. Viewers wait on a generation counter,
    so ten open tabs cost the device exactly what one costs.
    """

    def __init__(self, poll_interval):
        self._condition = threading.Condition()
        self._generation = 0
        self._payload = {
            "generation": 0,
            "values": {},
            "polls": 0,
            "errors": 0,
            "error": "",
            "poll_interval_s": poll_interval,
            "wall_clock": time.time(),
        }

    def publish(self, payload):
        with self._condition:
            self._generation += 1
            payload["generation"] = self._generation
            self._payload = payload
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return self._payload

    def wait_for_next(self, seen, timeout):
        """Return the payload once it is newer than ``seen``, else None."""

        with self._condition:
            if self._generation != seen:
                return self._payload
            self._condition.wait(timeout)
            return self._payload if self._generation != seen else None


class LivePoller(threading.Thread):
    """Single owner of the Modbus connection behind the live page."""

    def __init__(self, client, mapping, blocks, state, device, interval):
        super().__init__(daemon=True, name="e3dc-live-poller")
        self._client = client
        self._mapping = mapping
        self._blocks = blocks
        self._state = state
        self._device = device
        self._interval = interval
        self._stop = threading.Event()
        self._series = {}
        for block in blocks:
            for item in block.fields:
                self._series[item.key] = Series(item.key, item.label, item.unit)
        self._series[DERIVED_INVERTER_TOTAL] = Series(
            DERIVED_INVERTER_TOTAL, "Inverter active power", "W"
        )
        self._any_change_times = []
        self._polls = 0
        self._errors = 0

    def stop(self):
        self._stop.set()

    def run(self):
        started = time.monotonic()
        tick = 0
        while not self._stop.is_set():
            target = started + tick * self._interval
            delay = target - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            tick += 1
            self._poll_once()

    def _poll_once(self):
        values = {}
        error = ""
        for block in self._blocks:
            readings = read_fields(
                self._client, self._mapping, block.fields,
                block.first, block.count, base=block.base,
            )
            for item in readings:
                values[item.field.key] = item
                if not item.available and not error:
                    error = item.error
        observed_at = time.monotonic()
        self._polls += 1
        if error:
            self._errors += 1

        payload = {}
        changed = False
        for key, reading in values.items():
            entry = self._series[key]
            previous = entry.last_value
            if reading.available:
                entry.observe(reading.value, observed_at)
                if entry.samples > 1 and entry.last_value != previous:
                    changed = True
            payload[key] = {
                "label": reading.field.label,
                "available": reading.available,
                "value": reading.value if reading.available else None,
                "unit": reading.field.unit,
                "register": reading.register,
                "note": reading.field.note,
                "error": reading.error,
                "changes": len(entry.change_times),
            }

        total = self._inverter_total(values)
        if total is not None:
            entry = self._series[DERIVED_INVERTER_TOTAL]
            previous = entry.last_value
            entry.observe(total["value"], observed_at)
            if entry.samples > 1 and entry.last_value != previous:
                changed = True
            total["changes"] = len(entry.change_times)
            payload[DERIVED_INVERTER_TOTAL] = total

        if changed:
            self._any_change_times.append(observed_at)

        combined = gap_statistics(self._any_change_times)
        summary = {
            key: {"changed": bool(entry.change_times)} for key, entry in self._series.items()
        }
        self._state.publish({
            "device": self._device,
            "mapping": {
                "unit_id": self._mapping.unit_id,
                "function_code": self._mapping.function_code,
                "address_offset": self._mapping.address_offset,
            },
            "values": payload,
            "cadence": _cadence_estimate(combined, summary, self._interval),
            "polls": self._polls,
            "errors": self._errors,
            "error": error,
            "poll_interval_s": self._interval,
            "wall_clock": time.time(),
        })

    def _inverter_total(self, values):
        """Active power across the three phases, marked as derived rather than read."""

        phases = [values.get(key) for key in INVERTER_CADENCE_KEYS]
        if not all(phase is not None and phase.available for phase in phases):
            return None
        registers = ", ".join(str(phase.register) for phase in phases)
        return {
            "label": "Inverter active power",
            "available": True,
            "value": sum(phase.value for phase in phases),
            "unit": "W",
            "register": None,
            "derived_from": f"{registers} (sum)",
            "note": "sum of L1, L2 and L3",
            "error": "",
        }


class _LiveRequestHandler(http.server.BaseHTTPRequestHandler):
    """Serves the page, a JSON snapshot and an SSE stream. GET only."""

    protocol_version = "HTTP/1.1"
    server_version = "e3dc-modbus-probe"
    state = None
    heartbeat_seconds = 15.0

    def log_message(self, *_args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send_bytes(LIVE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/snapshot":
            body = json.dumps(self.state.snapshot()).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
        elif path == "/events":
            self._stream_events()
        else:
            self._send_bytes(b"not found\n", "text/plain; charset=utf-8", status=404)

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        seen = 0
        while True:
            payload = self.state.wait_for_next(seen, self.heartbeat_seconds)
            try:
                if payload is None:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    seen = payload["generation"]
                    body = json.dumps(payload)
                    self.wfile.write(f"data: {body}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return


def serve_live(args, client, mapping, blocks, device):
    """Poll in one thread, serve the reading to any number of viewers."""

    state = LiveState(args.interval)
    poller = LivePoller(client, mapping, blocks, state, device, args.interval)
    handler = type("_BoundLiveHandler", (_LiveRequestHandler,), {"state": state})
    server = http.server.ThreadingHTTPServer((args.bind, args.serve), handler)
    server.daemon_threads = True

    shown = "localhost" if args.bind in {"127.0.0.1", "::1"} else args.bind
    print(f"\nLive view on http://{shown}:{server.server_port}/")
    print(f"Polling every {args.interval:g} s. One poller feeds every viewer. Ctrl-C to stop.")
    if args.bind not in {"127.0.0.1", "::1"}:
        print(f"Reachable from the network on {args.bind} - the page is unauthenticated.")

    poller.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        poller.stop()
        server.shutdown()
        server.server_close()
    return 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description="Read-only Modbus/TCP probe for an E3/DC energy storage system.",
    )
    parser.add_argument("host", nargs="?", help="IP address of the E3/DC system")
    parser.add_argument("--port", type=int, default=MODBUS_TCP_PORT, help="default 502")
    parser.add_argument(
        "--unit-id", type=int, default=None,
        help=f"Modbus unit id; default tries {', '.join(str(item) for item in UNIT_ID_CANDIDATES)}",
    )
    parser.add_argument(
        "--function-code", type=int, default=None,
        choices=sorted(READ_ONLY_FUNCTION_CODES),
        help="3 = holding registers, 4 = input registers; default tries both",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="socket timeout in seconds")
    parser.add_argument(
        "--inverter", type=int, action="append", metavar="INDEX",
        help="also read inverter block INDEX (0 = built-in); repeatable",
    )
    parser.add_argument("--power-meters", action="store_true", help="also read power meters 0..6")
    parser.add_argument("--dc-strings", action="store_true", help="also read the DC string values")
    parser.add_argument(
        "--measure-seconds", type=float, default=0.0,
        help="measure the refresh cadence for this long (0 = snapshot only)",
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_POLL_INTERVAL,
        help=(
            f"poll interval in seconds (default {DEFAULT_POLL_INTERVAL:g}, minimum "
            f"{MINIMUM_POLL_INTERVAL:g}); {DEFAULT_POLL_INTERVAL:g} s is the usual control-loop "
            "period, and sub-second polling of an inverter buys nothing"
        ),
    )
    parser.add_argument(
        "--serve", type=int, metavar="PORT",
        help="serve a live page on PORT that updates as the values change",
    )
    parser.add_argument(
        "--bind", default="127.0.0.1",
        help="interface for --serve; the page is unauthenticated, so default localhost",
    )
    parser.add_argument("--json", metavar="PATH", help="also write the result as JSON")
    parser.add_argument("--self-test", action="store_true", help="run offline against a fake device")
    args = parser.parse_args(argv)

    if args.self_test:
        return args
    if not args.host:
        parser.error("a host is required unless --self-test is given")
    if args.interval < MINIMUM_POLL_INTERVAL:
        parser.error(
            f"--interval must be at least {MINIMUM_POLL_INTERVAL:g} s; a control loop runs at "
            f"about {DEFAULT_POLL_INTERVAL:g} s, so polling an inverter faster buys nothing"
        )
    if args.serve is not None:
        if not 1 <= args.serve <= 65535:
            parser.error("--serve needs a port between 1 and 65535")
        if args.measure_seconds:
            parser.error("--serve and --measure-seconds are alternatives; the page measures as it runs")
        if args.json:
            parser.error("--serve runs until interrupted; --json has no single result to write")
    if args.measure_seconds and args.measure_seconds < args.interval:
        parser.error("--measure-seconds must be at least one --interval")
    return args


def main(argv=None):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return run_self_test()

    try:
        result, status = run_probe(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"\nJSON written to {args.json}")
    return status


if __name__ == "__main__":
    sys.exit(main())
