# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SML telegram decoding against real meter telegrams and built edge cases."""

import json
from pathlib import Path

import pytest

from ems import sml

pytestmark = [pytest.mark.unit, pytest.mark.power_control]

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "sml" / "telegrams.json").read_text())
TELEGRAMS = {t["id"]: t for t in FIXTURES["telegrams"]}

ESC = sml.ESCAPE


def _decode(telegram):
    if "frame" in telegram:
        return sml.parse(bytes.fromhex(telegram["frame"]))
    return sml.parse_messages(bytes.fromhex(telegram["messages"]))


@pytest.mark.parametrize("telegram", FIXTURES["telegrams"], ids=lambda t: t["id"])
def test_real_telegrams_decode_to_the_expected_grid_power(telegram):
    expect = telegram["expect"]
    if "error" in expect:
        with pytest.raises(sml.SmlError) as raised:
            sml.grid_power_w(_decode(telegram))
        assert raised.value.reason == expect["error"]
    else:
        assert sml.grid_power_w(_decode(telegram)) == pytest.approx(expect["grid_power_w"])


def test_the_frame_crc_is_what_rejects_a_damaged_bridge_telegram(monkeypatch):
    """The bridge sometimes serves a damaged telegram whose structure still
    parses; without the CRC this one reads as 66 kW of export."""
    telegram = TELEGRAMS["bridge-emh-corrupt"]
    monkeypatch.setattr(sml, "_crc_variant", lambda data, stored, variants: variants[0])

    assert sml.grid_power_w(_decode(telegram)) == telegram["expect"]["unchecked_grid_power_w"]


def test_a_dzg_meter_short_negative_power_is_read_as_positive():
    telegram = TELEGRAMS["dzg-short-signed"]
    decoded = _decode(telegram)

    assert decoded.has_dzg_sign_bug
    assert sml.grid_power_w(decoded) == pytest.approx(447.98)


def test_the_same_encoding_from_another_maker_keeps_its_sign():
    messages = bytes.fromhex(TELEGRAMS["dzg-short-signed"]["messages"]).replace(b"DZG", b"EMH")
    decoded = sml.parse_messages(messages)

    assert not decoded.has_dzg_sign_bug
    assert sml.grid_power_w(decoded) == pytest.approx(TELEGRAMS["dzg-short-signed"]["expect"]["uncorrected_grid_power_w"])


@pytest.mark.parametrize("serial", [41_999_999, 49_000_000, 54_999_999, 59_000_000, 60_000_000])
def test_a_dzg_meter_outside_the_affected_serials_keeps_its_sign(serial):
    affected = bytes.fromhex("0a01445a4700") + (45_000_000).to_bytes(4, "big")
    other = bytes.fromhex("0a01445a4700") + serial.to_bytes(4, "big")
    messages = bytes.fromhex(TELEGRAMS["dzg-short-signed"]["messages"]).replace(affected, other)
    decoded = sml.parse_messages(messages)

    assert not decoded.has_dzg_sign_bug
    assert sml.grid_power_w(decoded) == pytest.approx(TELEGRAMS["dzg-short-signed"]["expect"]["uncorrected_grid_power_w"])


def _tl(kind, length):
    assert length < 16
    return bytes([(kind << 4) | length])


def _octets(value):
    return _tl(0, len(value) + 1) + value


def _int(value, size, signed=True):
    return _tl(5 if signed else 6, size + 1) + value.to_bytes(size, "big", signed=signed)


def _list(*items):
    return _tl(7, len(items)) + b"".join(items)


def _entry(obis, value, *, unit=27, scaler=0):
    return _list(
        _octets(obis), b"\x01", b"\x01",
        _int(unit, 1, signed=False), _int(scaler, 1), value, b"\x01",
    )


def _message(tag, body):
    content = _tl(7, 6) + _octets(b"\x01") + _int(0, 1, signed=False) + _int(0, 1, signed=False) + _list(_int(tag, 2, signed=False), body)
    return content + _int(sml.crc16_x25(content), 2, signed=False) + b"\x00"


def _get_list(*entries, server_id=b"\x0a\x01EMH\x00\x00\x00\x00\x01"):
    return _message(sml.GET_LIST_RESPONSE, _list(b"\x01", _octets(server_id), b"\x01", b"\x01", _list(*entries), b"\x01", b"\x01"))


def _frame(messages, *, crc=sml.crc16_x25, byteorder="big"):
    padding = -len(messages) % 4
    body = messages + bytes(padding)
    escaped = b"".join(
        ESC + ESC if body[i:i + 4] == ESC else body[i:i + 4] for i in range(0, len(body), 4)
    )
    head = sml.START + escaped + ESC + b"\x1a" + bytes([padding])
    return head + crc(head).to_bytes(2, byteorder)


def test_a_built_frame_round_trips():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-1500, 4))))

    assert sml.grid_power_w(sml.parse(frame)) == -1500


@pytest.mark.parametrize("crc", [sml.crc16_x25, sml.crc16_kermit])
@pytest.mark.parametrize("byteorder", ["big", "little"])
def test_both_crc_variants_in_either_byte_order_are_accepted(crc, byteorder):
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(812, 2))), crc=crc, byteorder=byteorder)

    assert sml.grid_power_w(sml.parse(frame)) == 812


def test_a_changed_byte_fails_the_crc():
    frame = bytearray(_frame(_get_list(_entry(sml.POWER_TOTAL, _int(812, 2)))))
    frame[40] ^= 0x01

    with pytest.raises(sml.SmlError, match="crc_mismatch"):
        sml.parse(bytes(frame))


def test_the_frame_is_found_behind_leading_bytes():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(240, 2))))

    assert sml.grid_power_w(sml.parse(b"\x00\x7f\x1b" + frame)) == 240


def test_an_escape_sequence_inside_the_data_is_unescaped():
    device_id = bytes.fromhex("0100000009ff")
    frames = [
        _frame(_get_list(_entry(sml.POWER_TOTAL, _int(300, 2)), _entry(device_id, _octets(bytes(shift) + ESC), unit=0)))
        for shift in range(4)
    ]
    frame = next(f for f in frames if ESC + ESC in f)
    decoded = sml.parse(frame)

    assert decoded.entry(device_id).value.endswith(ESC)
    assert sml.grid_power_w(decoded) == 300


def test_net_power_falls_back_to_import_minus_export():
    frame = _frame(_get_list(
        _entry(sml.POWER_IMPORT, _int(500, 2, signed=False)),
        _entry(sml.POWER_EXPORT, _int(1250, 2, signed=False), scaler=-1),
    ))

    assert sml.grid_power_w(sml.parse(frame)) == pytest.approx(375.0)


def test_an_unsigned_total_is_refused_because_it_cannot_show_export():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(400, 2, signed=False))))

    with pytest.raises(sml.SmlError, match="no_signed_power"):
        sml.grid_power_w(sml.parse(frame))


def test_power_in_another_unit_is_refused():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(400, 2), unit=30)))

    with pytest.raises(sml.SmlError, match="wrong_unit"):
        sml.grid_power_w(sml.parse(frame))


def test_a_full_width_negative_value_from_a_dzg_meter_stays_negative():
    dzg = b"\x0a\x01DZG\x00\x00\x00\x00\x01"
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-126061, 4), scaler=-2), server_id=dzg))

    assert sml.grid_power_w(sml.parse(frame)) == pytest.approx(-1260.61)


def test_non_zero_padding_is_refused():
    frames = [bytearray(_frame(_get_list(_entry(sml.POWER_TOTAL, _int(5, size))))) for size in (1, 2, 3, 4)]
    frame = next(f for f in frames if f[f.rfind(ESC + b"\x1a") + 5])
    end = frame.rfind(ESC + b"\x1a")
    frame[end - 1] = 0x07
    frame[end + 6:end + 8] = sml.crc16_x25(bytes(frame[:end + 6])).to_bytes(2, "big")

    with pytest.raises(sml.SmlError, match="bad_padding"):
        sml.parse(bytes(frame))


def test_a_truncated_frame_before_an_intact_one_does_not_hide_it():
    whole = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(321, 2))))

    assert sml.grid_power_w(sml.parse(whole[:20] + whole)) == 321


def test_the_frame_reports_its_crc_variant_and_a_pinned_variant_refuses_the_others():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(5, 1))), crc=sml.crc16_kermit, byteorder="little")

    assert sml.parse(frame).crc_variant == ("kermit", "little")
    with pytest.raises(sml.SmlError, match="crc_mismatch"):
        sml.parse(frame, crc_variants=(("x25", "big"),))


def test_a_three_byte_negative_from_a_dzg_meter_is_left_as_it_is():
    """libsml corrects one and two byte payloads only; a longer short value
    could be a real export and must not be flipped into import."""
    dzg = b"\x0a\x01DZG\x00" + (45_000_000).to_bytes(4, "big")
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-70000, 3), scaler=-2), server_id=dzg))

    assert sml.grid_power_w(sml.parse(frame)) == pytest.approx(-700.0)


def test_a_list_nested_deeper_than_any_telegram_is_refused():
    nested = b"\x71" * (sml.MAX_DEPTH + 2) + b"\x01"

    with pytest.raises(sml.SmlError, match="too_deep"):
        sml.read_element(nested, 0)


@pytest.mark.parametrize("scaler", [128, -129, 2**40])
def test_a_scaler_outside_integer8_is_refused(scaler):
    size = max(1, (scaler.bit_length() + 8) // 8)
    entry = _list(
        _octets(sml.POWER_TOTAL), b"\x01", b"\x01",
        _int(27, 1, signed=False), _int(scaler, size), _int(5, 1), b"\x01",
    )

    decoded = sml.parse(_frame(_get_list(entry)))

    with pytest.raises(sml.SmlError, match="bad_scaler"):
        sml.grid_power_w(decoded)


def test_an_odd_entry_beside_the_power_does_not_hide_it():
    odd_shape = _list(_octets(bytes.fromhex("8181c78203ff")), b"\x01", b"\x01")
    odd_scaler = _list(
        _octets(bytes.fromhex("0100010800ff")), b"\x01", b"\x01",
        _int(30, 1, signed=False), _int(200, 2), _int(5, 1), b"\x01",
    )
    frame = _frame(_get_list(odd_shape, odd_scaler, _entry(sml.POWER_TOTAL, _int(-250, 2))))

    assert sml.grid_power_w(sml.parse(frame)) == -250


def test_import_minus_export_stands_in_for_an_unusable_signed_total():
    frame = _frame(_get_list(
        _entry(sml.POWER_TOTAL, _int(-250, 2), unit=0),
        _entry(sml.POWER_IMPORT, _int(100, 2, signed=False)),
        _entry(sml.POWER_EXPORT, _int(350, 2, signed=False)),
    ))

    assert sml.grid_power_w(sml.parse(frame)) == -250


def test_an_unusable_signed_total_without_the_pair_is_refused():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-250, 2), unit=0)))

    with pytest.raises(sml.SmlError, match="wrong_unit"):
        sml.grid_power_w(sml.parse(frame))


def test_a_complete_frame_failing_its_crc_is_named_before_a_fragment_beside_it():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(5, 1))), crc=sml.crc16_kermit, byteorder="little")

    with pytest.raises(sml.SmlError, match="crc_mismatch"):
        sml.parse(sml.START + b"\x01\x02\x03\x04" + frame, crc_variants=(("x25", "big"),))


def test_zero_bytes_between_messages_are_filler():
    frame = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-250, 2))) + b"\x00" * 5)

    assert sml.grid_power_w(sml.parse(frame)) == -250
