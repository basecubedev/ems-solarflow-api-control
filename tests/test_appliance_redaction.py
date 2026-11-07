# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What redaction lets through reaches a browser and a support archive.

Logs read in the console and the archive operators are told to attach to an
issue both pass through appliance/redaction.py; each shape below went through
unredacted.
"""

import time

import pytest

from appliance import redaction
from appliance.redaction import bounded_redacted_log, redact_text

pytestmark = [pytest.mark.unit, pytest.mark.appliance]

SECRET = "hunter2-s3cret"


@pytest.mark.parametrize(
    "line",
    [
        "config: {'password': 'hunter2-s3cret', 'host': 'broker'}",
        "{'mqtt_password': 'hunter2-s3cret'}",
        '{"password": "hunter2-s3cret"}',
        '{"secret_key": "hunter2-s3cret"}',
        "session_id=hunter2-s3cret",
        "password_hash=hunter2-s3cret",
        "mosquitto_pub -h broker -u ems -P hunter2-s3cret -t x",
        'password="ab\\"hunter2-s3cret"',
        "mqtt://ems:pa@hunter2-s3cret@broker:1883",
    ],
)
def test_a_secret_in_a_common_shape_is_redacted(line):
    assert SECRET not in redact_text(line), redact_text(line)


def test_a_numeric_secret_value_is_redacted():
    assert "12345678" not in redact_text('{"password": 12345678}')


def test_a_private_key_whose_first_line_fell_outside_the_window_is_still_redacted():
    """Clamping before redaction kept the newest lines, the key body among
    them, and cut the BEGIN line the pattern needs."""

    key = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(["QUJDREVGR0hJSktMTU5PUA=="] * 30) + "\n-----END OPENSSH PRIVATE KEY-----"
    log = "\n".join(["noise"] * 10) + "\n" + key + "\nafter"

    bounded = bounded_redacted_log(log, max_lines=20)

    assert "QUJDREVGR0hJSktMTU5PUA==" not in bounded["text"]


def test_public_material_stays_readable():
    assert redact_text("public_key=ssh-ed25519") == "public_key=ssh-ed25519"
    assert "device_key" in redact_text("device_key=abc123")


def test_a_word_that_repeats_a_secret_name_is_redacted_in_linear_time():
    started = time.perf_counter()
    redact_text("token" * 64000)
    redact_text("a." * 32000)
    redact_text("ssh-" * 32000)

    assert time.perf_counter() - started < 2.0


def test_a_huge_log_is_cut_before_it_is_redacted(monkeypatch):
    seen = []
    original = redaction.redact_text

    def measuring(text):
        seen.append(len(text))
        return original(text)

    monkeypatch.setattr(redaction, "redact_text", measuring)
    bounded = bounded_redacted_log("x" * 10_000 + "\n" + "line\n" * 2_000_000, max_bytes=4096)

    assert max(seen) <= 4 * 4096, seen
    assert bounded["truncated"] is True


def test_a_private_key_cut_by_the_raw_window_does_not_leave_its_body():
    body = "QUJDREVGR0hJSktMTU5PUA=="
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join([body] * 400) + "\n-----END OPENSSH PRIVATE KEY-----"

    bounded = bounded_redacted_log(key + "\nafter", max_bytes=1024)

    assert body not in bounded["text"], bounded["text"]
    assert bounded["text"].endswith("after")


def test_a_secret_behind_a_long_qualifier_is_still_redacted():
    """Bounding the qualifier to stop the backtracking let a key preceded by
    more than 64 identifier characters through."""

    assert "hunter2" not in redact_text("x" * 70 + "_password=hunter2")
    assert "hunter2" not in redact_text('{"' + "y" * 200 + '_token": "hunter2"}')
    assert "hunter2" not in redact_text("api-key=hunter2")
    assert "hunter2" not in redact_text("hello token hunter2")
