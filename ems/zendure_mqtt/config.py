# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection config for the read-only Zendure MQTT client.

Read-only by construction: there is no publish/write/control setting here. The
password is held but is deliberately excluded from ``repr`` so it cannot leak
into logs, tracebacks or test output.
"""

from dataclasses import dataclass, field, replace


class ZendureMqttClientError(RuntimeError):
    """Connection/setup failure that never carries credential detail.

    Lives here (not in the paho-backed client module) so its identity is
    stable for every consumer — service.start()'s except clause must match
    even if the client module is absent or was re-imported, and status-only
    deployments (the Admin image) ship no client module at all.
    """


# Local Zendure telemetry topic families. These are intentionally narrow: never
# subscribe to a bare ``#`` by default, so a shared broker is not vacuumed up.
DEFAULT_LOCAL_SUBSCRIPTIONS = (
    "Zendure/#",
    "iot/+/+/#",
    "/+/+/#",
)


@dataclass(frozen=True)
class ZendureMqttClientConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    tls_insecure: bool = False
    client_id: str | None = None
    app_key: str | None = None
    subscriptions: tuple[str, ...] | None = None
    connect_timeout_seconds: float = 10.0
    keepalive_seconds: int = 30

    def resolved_subscriptions(self) -> tuple[str, ...]:
        """Topic filters to subscribe to, de-duplicated and order-stable.

        Uses ``subscriptions`` when given, else the known local families. When an
        ``app_key`` is configured the cloud-prefixed scalar tree ``<app_key>/#``
        is added — never a global ``#``.
        """

        if self.subscriptions is not None:
            base = tuple(self.subscriptions)
        else:
            base = DEFAULT_LOCAL_SUBSCRIPTIONS
        topics: list[str] = []
        for topic in base:
            if topic and topic not in topics:
                topics.append(topic)
        if self.app_key:
            cloud = f"{self.app_key}/#"
            if cloud not in topics:
                topics.append(cloud)
        return tuple(topics)

    def redacted(self) -> "ZendureMqttClientConfig":
        """Copy with credentials removed, safe to log or embed in errors."""

        return replace(self, username=None, password=None)
