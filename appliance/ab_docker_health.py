# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one Docker question set a trial slot is judged by.

A trial slot commits itself only if an operator can still reach the appliance
afterwards, which means the Admin console has to be running the exact image the
source slot recorded and has to answer over the loopback address. "The container
exists" and "the container is running" are both weaker than that: a container
that starts and immediately fails its own health check satisfies them.

One protocol, implemented once over the production ``DockerBackend`` and once by
the test doubles. The health service used to ask for method names the production
backend did not have, every call raised, and each exception was converted into a
failed gate — so a real trial slot could never have committed while a fake that
happened to implement those names kept the suite green.

The probe never reaches beyond the loopback address. An appliance whose WAN is
down is not a broken slot.
"""

from dataclasses import dataclass
from typing import Protocol

ADMIN_CONTAINER = "ems-admin"
EMS_CONTAINER = "ems-solarflow"

DEFAULT_ADMIN_URL = "http://127.0.0.1:8090/health"
DEFAULT_TIMEOUT = 10.0


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
        timeout=DEFAULT_TIMEOUT,
    ):
        self.backend = backend
        self.admin_url = str(admin_url)
        self.admin_container = str(admin_container)
        self.ems_container = str(ems_container)
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
        """Exact image, running, and answering. All three, or no commit."""

        container = self._container(self.admin_container)
        if container is None or not container.exists:
            return failed(
                "admin_container_missing",
                f"{self.admin_container} was not reconstructed in this slot",
            )
        digest = self._digest(container)
        if expected_digest and digest != expected_digest:
            return failed(
                "admin_image_digest_mismatch",
                f"{self.admin_container} runs {digest or 'an unresolvable image'}, the source "
                f"slot recorded {expected_digest}",
            )
        if container.state != "running":
            return failed(
                "admin_container_not_running",
                f"{self.admin_container} is {container.state}",
            )
        status, body = self._http()
        if not 200 <= int(status) < 400:
            return failed(
                "admin_http_unhealthy",
                f"{self.admin_url} answered {status or 'nothing'}",
            )
        return passed(f"{self.admin_container} answers on {self.admin_url}{_identified(body)}")

    def ems_runtime(self, expected_digest, *, expected_running=True):
        """Respect what the appliance was doing before the update.

        An EMS deliberately stopped before the OS update must not be started by
        a health gate, and must not block a commit for being stopped. What is
        required either way is that the image the source slot recorded is still
        the image this slot has.
        """

        container = self._container(self.ems_container)
        if container is None or not container.exists:
            if expected_running:
                return failed(
                    "ems_container_missing", f"{self.ems_container} was not reconstructed"
                )
            return passed(f"{self.ems_container} is absent, as it was before the update")
        digest = self._digest(container)
        if expected_digest and digest != expected_digest:
            return failed(
                "ems_image_digest_mismatch",
                f"{self.ems_container} runs {digest or 'an unresolvable image'}, the source "
                f"slot recorded {expected_digest}",
            )
        if expected_running and container.state != "running":
            return failed(
                "ems_container_not_running",
                f"{self.ems_container} is {container.state} but was running before the update",
            )
        return passed(f"{self.ems_container} is {container.state}, as recorded")

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


def _identified(body):
    text = str(body or "")
    return " (identified)" if "ems-admin" in text or "version" in text else ""


def _loopback_probe(url, timeout):
    """A bounded GET against the loopback address. No WAN, no redirects out."""

    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            return int(status), response.read(16 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""
