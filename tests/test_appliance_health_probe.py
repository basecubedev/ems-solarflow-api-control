# SPDX-License-Identifier: AGPL-3.0-or-later
"""The check that decides whether a deployed Admin container is answering.

Its verdict is what records a version as known-good and, through
ab_docker_health, whether a trial slot may commit -- and its exception allowlist
had never been executed. A container that is still starting can raise
``http.client`` errors while the body is being read, and those are not in it.
"""

import http.client
import io
import json
import urllib.error

import pytest

from appliance.health import HttpHealthChecker

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


class _Response:
    def __init__(self, body, status=200):
        self._body = body.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _limit=None):
        return self._body

    def getcode(self):
        return self.status


def checker(opener, **kwargs):
    return HttpHealthChecker(opener=opener, **kwargs)


def test_a_healthy_answer_reports_its_version():
    result = checker(lambda *_a, **_k: _Response(json.dumps({"version": "1.4.0"}))).probe(
        "http://127.0.0.1:8090/api"
    )

    assert result.reachable
    assert result.version == "1.4.0"
    assert result.status_code == 200


def test_a_non_json_body_is_still_reachable():
    result = checker(lambda *_a, **_k: _Response("<html>ok</html>")).probe("http://x/")

    assert result.reachable
    assert result.version == ""


def test_a_server_error_is_not_reachable():
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("http://x/", 500, "boom", {}, io.BytesIO(b""))

    result = checker(_raise).probe("http://x/")

    assert not result.reachable
    assert result.status_code == 500
    assert result.error == "http_error"


def test_a_refused_connection_is_not_reachable():
    def _raise(*_a, **_k):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    assert not checker(_raise).probe("http://x/").reachable


def test_a_socket_timeout_is_not_reachable():
    def _raise(*_a, **_k):
        raise TimeoutError("timed out")

    assert not checker(_raise).probe("http://x/").reachable


def test_a_container_that_dies_mid_body_is_not_reachable():
    """The regression: `http.client` errors are raised while reading a response
    from a container that is still starting, and they were not in the allowlist
    -- so the probe raised instead of reporting a slot as not yet answering."""

    class _Dying(_Response):
        def read(self, _limit=None):
            raise http.client.IncompleteRead(b"partial")

    def _open(*_a, **_k):
        return _Dying("")

    result = checker(_open).probe("http://x/")

    assert not result.reachable
    assert result.error


# --- waiting -----------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_waiting_returns_as_soon_as_the_container_answers():
    clock = _Clock()
    attempts = {"n": 0}

    def _open(*_a, **_k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.URLError(ConnectionRefusedError("starting"))
        return _Response(json.dumps({"version": "9.9.9"}))

    result = checker(_open, sleep=clock.sleep, time_fn=clock.time).wait_until_healthy(
        "http://x/", timeout=600, interval=5
    )

    assert result.reachable
    assert attempts["n"] == 3


def test_waiting_gives_up_at_the_deadline_without_hanging():
    clock = _Clock()

    def _open(*_a, **_k):
        raise urllib.error.URLError(ConnectionRefusedError("never"))

    result = checker(_open, sleep=clock.sleep, time_fn=clock.time).wait_until_healthy(
        "http://x/", timeout=30, interval=5
    )

    assert not result.reachable
    assert clock.now >= 30
