# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loopback health verification for the Admin container.

A container that reports ``healthy`` has only proven its own health check ran.
The appliance additionally asks the Admin HTTP endpoint on the loopback address
before it marks a version known-good.
"""

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_INTERVAL = 3.0
DEFAULT_REQUEST_TIMEOUT = 5.0


@dataclass(frozen=True)
class HealthResult:
    reachable: bool
    status_code: int = 0
    version: str = ""
    error: str = ""
    body: str = ""

    def to_dict(self):
        return {
            "reachable": self.reachable,
            "status_code": self.status_code,
            "version": self.version,
            "error": self.error,
        }


class HttpHealthChecker:
    def __init__(self, *, timeout=DEFAULT_REQUEST_TIMEOUT, opener=None, sleep=None, time_fn=None):
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._time = time_fn or time.monotonic

    def probe(self, url):
        request = urllib.request.Request(url, method="GET")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(64 * 1024).decode("utf-8", errors="replace")
                status = getattr(response, "status", None) or response.getcode()
        except urllib.error.HTTPError as exc:
            return HealthResult(reachable=False, status_code=exc.code, error="http_error")
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ) as exc:
            # A container that is still starting can drop the connection while
            # the body is being read, which surfaces as IncompleteRead or
            # BadStatusLine rather than a URLError. "Not answering yet" is what
            # this probe exists to report, not something for it to raise.
            return HealthResult(reachable=False, error=str(exc.__class__.__name__))

        version = ""
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                version = str(payload.get("version") or payload.get("admin_version") or "")
        except ValueError:
            payload = None

        return HealthResult(
            reachable=200 <= int(status) < 400,
            status_code=int(status),
            version=version,
            body=raw[:1024],
        )

    def wait_until_healthy(self, url, *, timeout, interval=DEFAULT_INTERVAL, on_attempt=None):
        deadline = self._time() + float(timeout)
        attempt = 0
        result = HealthResult(reachable=False, error="not_attempted")
        while True:
            attempt += 1
            result = self.probe(url)
            if on_attempt is not None:
                on_attempt(attempt, result)
            if result.reachable:
                return result
            if self._time() >= deadline:
                return result
            self._sleep(interval)
