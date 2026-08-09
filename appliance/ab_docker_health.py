# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one Docker question set a trial slot is judged by.

A trial slot commits itself only if the appliance an operator had before the
reboot is the one they have after it. Per service that is three claims, not
one: the image is the digest the source slot recorded, the container is in the
state the source slot recorded, and — where the service answers over a socket —
it answers.

One protocol, implemented once over the production ``DockerBackend`` and once by
the test doubles. The health service used to ask for method names the production
backend did not have, every call raised, and each exception was converted into a
failed gate — so a real trial slot could never have committed while a fake that
happened to implement those names kept the suite green.

The Admin probe never reaches beyond the loopback address, and "something
answered on that port" is not an answer: the body has to identify itself as the
Admin console. An appliance whose WAN is down is not a broken slot, and a
reverse proxy that outlived the update is not an Admin console.
"""

import json
import re
from dataclasses import dataclass
from typing import Protocol

ADMIN_CONTAINER = "ems-solarflow-admin"
EMS_CONTAINER = "ems-solarflow"
INFLUX_CONTAINER = "ems-influxdb"

DEFAULT_ADMIN_URL = "http://127.0.0.1:8090/api/admin/auth/status"
DEFAULT_TIMEOUT = 10.0

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

# The Admin auth-status contract exactly as ``admin/server.py`` answers it.
# Every field is required and typed, because "something answered with JSON" is
# also what a leftover reverse proxy, a captive portal and a generic service
# banner do. A `version` key is not an identity.
ADMIN_BOOLEAN_FIELDS = (
    "auth_configured",
    "authenticated",
    "requires_initial_password",
    "recovery_required",
)
ADMIN_INSTANCE_ID = re.compile(r"^[0-9a-f]{32}$")

# EMS and InfluxDB are asked in their own terms. The EMS answer is its own
# versioned diagnose contract, not a second implementation of it here.
EMS_DIAGNOSE_ARGV = ("python3", "-B", "emsctl.py", "diagnose", "--json")
INFLUX_PING_ARGV = ("influx", "ping")
DIAGNOSIS_FAILED = "error"


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    code: str = ""
    detail: str = ""

    def to_dict(self):
        return {"ok": self.ok, "code": self.code, "detail": self.detail}


def passed(detail):
    return HealthResult(ok=True, detail=detail)


def failed(code, detail):
    return HealthResult(ok=False, code=code, detail=detail)


class TrialDockerHealth(Protocol):
    """What ``TrialHealthService`` may ask, and nothing else."""

    def daemon_usable(self) -> HealthResult: ...

    def admin_runtime(self, expected_digest: str) -> HealthResult: ...

    def ems_runtime(self, expected_digest, *, expected_running: bool) -> HealthResult: ...

    def influxdb_runtime(self, expected_digest, *, expected_running: bool) -> HealthResult: ...


class DockerTrialHealth:
    """The protocol over the production ``DockerBackend``."""

    def __init__(
        self,
        backend,
        *,
        http_probe=None,
        admin_url=DEFAULT_ADMIN_URL,
        admin_container=ADMIN_CONTAINER,
        ems_container=EMS_CONTAINER,
        influx_container=INFLUX_CONTAINER,
        timeout=DEFAULT_TIMEOUT,
    ):
        self.backend = backend
        self.admin_url = str(admin_url)
        self.admin_container = str(admin_container)
        self.ems_container = str(ems_container)
        self.influx_container = str(influx_container)
        self.timeout = float(timeout)
        self._probe = http_probe or _loopback_probe

    def daemon_usable(self):
        try:
            state = self.backend.daemon_state()
        except Exception as exc:
            return failed("docker_daemon_unreachable", f"the Docker daemon could not be asked: {exc}")
        if state.get("state") != "running":
            return failed(
                state.get("error") or "docker_daemon_unreachable",
                "the Docker daemon does not answer in this slot",
            )
        return passed(f"Docker {state.get('version') or 'unknown'} answers")

    def admin_runtime(self, expected_digest):
        """Exact image, running, and identifying itself. All three, or no commit."""

        result = self._container_runtime(
            self.admin_container, "admin", expected_digest, expected_running=True
        )
        if not result.ok:
            return result
        if not _loopback(self.admin_url):
            return failed(
                "admin_health_url_not_loopback",
                f"{self.admin_url} is not on the loopback address",
            )
        status, body = self._http()
        if int(status) != 200:
            return failed(
                "admin_http_unhealthy",
                f"{self.admin_url} answered {status or 'nothing'}",
            )
        if not _identifies_admin(body):
            return failed(
                "admin_http_unidentified",
                f"{self.admin_url} answered something that is not the Admin console",
            )
        return passed(f"{self.admin_container} answers on {self.admin_url}")

    def ems_runtime(self, expected_digest, *, expected_running=True):
        """Respect what the appliance was doing, then ask whether it works.

        An EMS deliberately stopped before the OS update must not be started by
        a health gate, must not be probed, and must not block a commit for being
        stopped. An EMS that was running has to answer its own diagnose
        contract: a process that exists is not an EMS that controls power, and
        the repository defines no container health check that would say so.
        """

        result = self._container_runtime(
            self.ems_container, "ems", expected_digest, expected_running=expected_running
        )
        if not result.ok or not expected_running:
            return result
        return self._ems_functional()

    def influxdb_runtime(self, expected_digest, *, expected_running=True):
        """Only asked when the source slot actually had InfluxDB deployed."""

        result = self._container_runtime(
            self.influx_container,
            "influxdb",
            expected_digest,
            expected_running=expected_running,
        )
        if not result.ok or not expected_running:
            return result
        return self._influx_functional()

    def _ems_functional(self):
        outcome = self._exec(self.ems_container, EMS_DIAGNOSE_ARGV)
        if outcome is None or not getattr(outcome, "ok", False):
            return failed(
                "ems_not_functional",
                f"{self.ems_container} is running but did not answer emsctl diagnose",
            )
        try:
            payload = json.loads((getattr(outcome, "stdout", "") or "").strip() or "{}")
        except ValueError:
            return failed(
                "ems_not_functional",
                f"{self.ems_container} answered emsctl diagnose with something that is not JSON",
            )
        status = str(((payload.get("diagnosis") or {}).get("status") or "")).lower()
        if not status:
            return failed(
                "ems_not_functional",
                f"{self.ems_container} answered no diagnose status",
            )
        if status == DIAGNOSIS_FAILED:
            return failed(
                "ems_diagnosis_failed",
                f"{self.ems_container} reports its own diagnosis as {status}",
            )
        return passed(f"{self.ems_container} reports diagnosis {status}")

    def _influx_functional(self):
        outcome = self._exec(self.influx_container, INFLUX_PING_ARGV)
        if outcome is None or not getattr(outcome, "ok", False):
            return failed(
                "influxdb_not_functional",
                f"{self.influx_container} is running but did not answer a ping",
            )
        return passed(f"{self.influx_container} answers a ping")

    def _exec(self, container, argv):
        try:
            return self.backend.exec_in_container(container, argv, timeout=self.timeout)
        except Exception:
            return None

    def _container_runtime(self, name, code, expected_digest, *, expected_running):
        container = self._container(name)
        if container is None or not container.exists:
            if expected_running:
                return failed(f"{code}_container_missing", f"{name} was not reconstructed")
            return passed(f"{name} is absent, as it was before the update")
        digest = self._digest(container)
        if expected_digest and digest != expected_digest:
            return failed(
                f"{code}_image_digest_mismatch",
                f"{name} runs {digest or 'an unresolvable image'}, the source slot "
                f"recorded {expected_digest}",
            )
        if expected_running and container.state != "running":
            return failed(
                f"{code}_container_not_running",
                f"{name} is {container.state} but was running before the update",
            )
        if not expected_running and container.state == "running":
            return failed(
                f"{code}_container_unexpectedly_running",
                f"{name} is running but was stopped before the update",
            )
        if expected_running and container.health == "unhealthy":
            return failed(
                f"{code}_container_unhealthy",
                f"{name} reports its own health check as unhealthy",
            )
        return passed(f"{name} is {container.state}, as recorded")

    def _container(self, name):
        try:
            return self.backend.inspect_container(name)
        except Exception:
            return None

    def _digest(self, container):
        reference = str(getattr(container, "image", "") or "")
        _, _, pinned = reference.partition("@")
        if pinned.startswith("sha256:"):
            return pinned
        try:
            state = self.backend.inspect_image(reference)
        except Exception:
            return ""
        return str(getattr(state, "digest", "") or "")

    def _http(self):
        try:
            return self._probe(self.admin_url, self.timeout)
        except Exception:
            return 0, ""


def _loopback(url):
    from urllib.parse import urlsplit

    parts = urlsplit(str(url))
    if parts.scheme != "http":
        return False
    host = parts.netloc.rpartition(":")[0] or parts.netloc
    return host in LOOPBACK_HOSTS


def _identifies_admin(body):
    """Is this the Admin console's auth status, or something that answered?

    Presence of a plausible key is not identity. The whole contract has to be
    there with the right value shapes, so a service banner, an error page
    rendered as JSON and a captive portal all fail.
    """

    try:
        payload = json.loads(str(body or ""))
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    for field in ADMIN_BOOLEAN_FIELDS:
        if not isinstance(payload.get(field), bool):
            return False
    return bool(ADMIN_INSTANCE_ID.match(str(payload.get("admin_instance_id") or "")))


def _loopback_probe(url, timeout):
    """A bounded GET against the loopback address. No WAN, no redirects out."""

    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            return int(status), response.read(16 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""
