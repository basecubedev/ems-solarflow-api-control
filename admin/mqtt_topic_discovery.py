# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only MQTT topic discovery for the Admin Console.

Admin listens briefly to a broker candidate and classifies the topics it sees
into Zendure hardware candidates. It never publishes, never authenticates
beyond an anonymous connection, and treats every topic and payload as untrusted
input. Discovery output is display-only and bounded; it is never promoted into
the EMS config.
"""

import json
import time
from dataclasses import dataclass

from admin.models import MqttHardwareCandidate
from ems.config import MQTT_TLS_MODE_PLAIN, mqtt_tls_mode_name


def _effective_tls_mode(broker):
    """The canonical TLS mode for a broker connection.

    An explicit stored ``tls_mode`` (``system_ca``/``insecure_no_verify``) is
    kept as-is; otherwise a TLS connection means system-CA verification and a
    plain connection means no TLS. Never downgrades an explicit mode.
    """

    mode = broker.get("tls_mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip()
    return mqtt_tls_mode_name(tls=broker.get("tls")) or MQTT_TLS_MODE_PLAIN

FAMILY_ZENSDK_HA_SCALAR = "zensdk_ha_scalar"
FAMILY_LEGACY_JSON = "legacy_zendure_json"
FAMILY_LEGACY_JSON_WRITE = "legacy_zendure_json_write_observed"
FAMILY_LEGACY_JSON_ALT = "legacy_zendure_json_alt"
FAMILY_ZENDURE_CLOUD_SCALAR = "zendure_cloud_scalar"
FAMILY_UNKNOWN = "unknown"

_JSON_FAMILIES = frozenset(
    {FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_WRITE, FAMILY_LEGACY_JSON_ALT}
)

# Zendure cloud-prefixed scalar components: `<appKey>/<component>/<dev>/<metric>`.
_CLOUD_SCALAR_COMPONENTS = frozenset(
    {"sensor", "number", "switch", "select", "binary_sensor"}
)

# Conservative bounded subscriptions. Deliberately not `#`: only the Zendure
# HA-scalar tree, the legacy `iot/<pk>/<dev>/...` tree, and the slash-prefixed
# `/<pk>/<dev>/...` tree (legacy_zendure_json_alt) are observed.
DEFAULT_SUBSCRIPTIONS = ("Zendure/#", "iot/+/+/#", "/+/+/#")

DEFAULT_TIMEOUT_S = 8.0
MAX_TIMEOUT_S = 10.0
MAX_TOPICS_PER_BROKER = 200
MAX_CANDIDATES_PER_BROKER = 32
MAX_METRICS_PER_CANDIDATE = 64
MAX_TOPICS_PER_CANDIDATE = 20
MAX_PAYLOAD_BYTES = 64 * 1024


@dataclass
class TopicMatch:
    family: str
    device_id: str | None = None
    serial_number: str | None = None
    metric: str | None = None
    product_key: str | None = None


def classify_topic(topic):
    """Classify an MQTT topic into a Zendure topic family.

    Parses device id / metric from the topic path only; payloads are handled
    separately. Unknown shapes return ``FAMILY_UNKNOWN`` rather than raising.
    """

    if not isinstance(topic, str) or not topic:
        return TopicMatch(FAMILY_UNKNOWN)
    segments = topic.split("/")
    if len(segments) >= 4 and segments[0] == "Zendure" and segments[2] and segments[3]:
        device = segments[2]
        return TopicMatch(
            FAMILY_ZENSDK_HA_SCALAR,
            device_id=device,
            serial_number=device,
            metric="/".join(segments[3:]),
        )
    if len(segments) == 5 and segments[0] == "iot" and segments[3] == "properties":
        if segments[4] == "report" and segments[1] and segments[2]:
            return TopicMatch(
                FAMILY_LEGACY_JSON, device_id=segments[2], product_key=segments[1]
            )
        if segments[4] == "write" and segments[1] and segments[2]:
            return TopicMatch(
                FAMILY_LEGACY_JSON_WRITE, device_id=segments[2], product_key=segments[1]
            )
    if (
        len(segments) == 5
        and segments[0] == ""
        and segments[3] == "properties"
        and segments[4] == "report"
        and segments[1]
        and segments[2]
    ):
        return TopicMatch(
            FAMILY_LEGACY_JSON_ALT, device_id=segments[2], product_key=segments[1]
        )
    # Cloud-prefixed scalar: `<appKey>/<component>/<device>/<metric>`. The
    # appKey prefix is a secret account scope, so it is never stored on the match.
    if (
        len(segments) >= 4
        and segments[0]
        and segments[0] not in ("Zendure", "iot")
        and segments[1] in _CLOUD_SCALAR_COMPONENTS
        and segments[2]
        and segments[3]
    ):
        return TopicMatch(
            FAMILY_ZENDURE_CLOUD_SCALAR,
            device_id=segments[2],
            serial_number=segments[2],
            metric="/".join(segments[3:]),
        )
    return TopicMatch(FAMILY_UNKNOWN)


def _coerce_json(payload):
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_PAYLOAD_BYTES:
            return None
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return None
    if len(payload) > MAX_PAYLOAD_BYTES:
        return None
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return None


def parse_report_payload(payload):
    """Best-effort extraction from a JSON report payload; ``{}`` when unusable.

    Never raises on malformed input, so a hostile broker cannot crash discovery.
    """

    data = _coerce_json(payload)
    if not isinstance(data, dict):
        return {}
    result = {"metrics": [], "pack_data": False}
    for key in ("sn", "serialNumber", "deviceSn"):
        value = data.get(key)
        if isinstance(value, str) and value:
            result["serial_number"] = value
            break
    product = data.get("product")
    if isinstance(product, str) and product:
        result["model_hint"] = product
    properties = data.get("properties")
    if isinstance(properties, dict):
        result["metrics"] = [key for key in properties if isinstance(key, str)]
        if properties.get("packData") is not None:
            result["pack_data"] = True
    if isinstance(data.get("packData"), (list, dict)):
        result["pack_data"] = True
    return result


def _score(candidate, pack_data):
    base = {
        FAMILY_ZENSDK_HA_SCALAR: 0.6,
        FAMILY_ZENDURE_CLOUD_SCALAR: 0.6,
        FAMILY_LEGACY_JSON: 0.55,
        FAMILY_LEGACY_JSON_ALT: 0.5,
        FAMILY_LEGACY_JSON_WRITE: 0.35,
    }.get(candidate.topic_family, 0.3)
    if candidate.serial_number:
        base += 0.15
    if candidate.metrics_seen:
        base += 0.1
    if pack_data:
        base += 0.1
    return round(min(base, 0.95), 2)


class MqttTopicAggregator:
    """Groups observed topics into bounded per-device hardware candidates.

    Pure and side-effect-free apart from its own state, so it can be tested
    without any network by feeding topics/payloads directly to ``observe``.
    """

    def __init__(
        self,
        broker,
        max_topics=MAX_TOPICS_PER_BROKER,
        max_candidates=MAX_CANDIDATES_PER_BROKER,
    ):
        self.broker_id = broker.get("id") or f"mqtt:{broker.get('host')}:{broker.get('port')}"
        self.broker_host = str(broker.get("host") or "")
        self.broker_port = int(broker.get("port") or 0)
        # Effective TLS/credential metadata of the connection candidates are seen
        # on, stamped onto each candidate so a proposal can preserve it. An
        # explicit stored ``tls_mode`` wins; otherwise a TLS connection defaults
        # to system-CA verification. ``credentials_ref`` is a non-secret pool id.
        self.broker_tls_mode = _effective_tls_mode(broker)
        ref = broker.get("credentials_ref")
        self.broker_credentials_ref = (
            ref.strip() if isinstance(ref, str) and ref.strip() else None
        )
        self._max_topics = max_topics
        self._max_candidates = max_candidates
        self._topic_count = 0
        self._candidates = {}
        self._pack_data = {}

    @property
    def topics_seen_count(self):
        return self._topic_count

    def observe(self, topic, payload=None):
        if self._topic_count >= self._max_topics:
            return
        self._topic_count += 1
        match = classify_topic(topic)
        if match.family == FAMILY_UNKNOWN:
            return
        device_key = match.serial_number or match.device_id or "unknown"
        key = (match.family, device_key)
        candidate = self._candidates.get(key)
        if candidate is None:
            if len(self._candidates) >= self._max_candidates:
                return
            candidate = MqttHardwareCandidate(
                broker_id=self.broker_id,
                broker_host=self.broker_host,
                broker_port=self.broker_port,
                topic_family=match.family,
                device_id=match.device_id,
                serial_number=match.serial_number,
                tls_mode=self.broker_tls_mode,
                credentials_ref=self.broker_credentials_ref,
            )
            self._candidates[key] = candidate
            self._pack_data[key] = False
        if topic not in candidate.topics_seen and (
            len(candidate.topics_seen) < MAX_TOPICS_PER_CANDIDATE
        ):
            candidate.topics_seen.append(topic)
        if match.metric:
            self._add_metric(candidate, match.metric)
        if payload is not None and match.family in _JSON_FAMILIES:
            parsed = parse_report_payload(payload)
            if parsed.get("serial_number") and not candidate.serial_number:
                candidate.serial_number = parsed["serial_number"]
            if parsed.get("model_hint") and not candidate.model_hint:
                candidate.model_hint = parsed["model_hint"]
            for metric in parsed.get("metrics", []):
                self._add_metric(candidate, metric)
            if parsed.get("pack_data"):
                self._pack_data[key] = True
        candidate.confidence = _score(candidate, self._pack_data[key])

    def _add_metric(self, candidate, metric):
        if metric not in candidate.metrics_seen and (
            len(candidate.metrics_seen) < MAX_METRICS_PER_CANDIDATE
        ):
            candidate.metrics_seen.append(metric)

    def candidates(self):
        return list(self._candidates.values())

    def results(self):
        return [candidate.to_dict() for candidate in self._candidates.values()]


class FakeMqttListener:
    """Replays a fixed sequence of ``(topic, payload)`` messages.

    Used by tests and simulation; ``publish`` exists only to assert discovery
    never calls it.
    """

    def __init__(self, messages, *, subscriptions=DEFAULT_SUBSCRIPTIONS):
        self._messages = list(messages)
        self.subscriptions = tuple(subscriptions)
        self.published = []

    def listen(self, timeout_s, on_message):
        for topic, payload in self._messages:
            on_message(topic, payload)

    def publish(self, *args, **kwargs):  # pragma: no cover - guard only
        self.published.append((args, kwargs))


class PahoMqttListener:
    """Anonymous, read-only paho-mqtt listener bounded by a timeout.

    Subscribes to the given topic filters and forwards messages to a callback.
    It never publishes. Imported lazily so Admin has no hard paho dependency.
    """

    def __init__(
        self,
        host,
        port,
        *,
        subscriptions=DEFAULT_SUBSCRIPTIONS,
        username=None,
        password=None,
        tls=False,
        tls_mode=None,
    ):
        self.host = host
        self.port = int(port)
        self.subscriptions = tuple(subscriptions)
        self.username = username
        self.password = password
        self.tls = bool(tls)
        self.tls_mode = tls_mode

    def listen(self, timeout_s, on_message):
        import paho.mqtt.client as mqtt

        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):  # paho < 2.0 has no versioned ctor
            client = mqtt.Client()

        from ems.config import configure_mqtt_client_tls

        configure_mqtt_client_tls(
            client,
            tls=self.tls,
            tls_insecure=self.tls_mode == "insecure_no_verify",
        )
        if self.username is not None:
            client.username_pw_set(self.username, self.password)

        def _on_connect(client_, *_args, **_kwargs):
            for topic in self.subscriptions:
                client_.subscribe(topic, qos=0)

        def _on_message(_client, _userdata, message):
            on_message(message.topic, message.payload)

        client.on_connect = _on_connect
        client.on_message = _on_message
        client.connect(self.host, self.port, keepalive=max(2, int(timeout_s) + 2))
        client.loop_start()
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass


def default_listener_factory(
    host,
    port,
    *,
    subscriptions=DEFAULT_SUBSCRIPTIONS,
    username=None,
    password=None,
    tls=False,
    tls_mode=None,
):
    return PahoMqttListener(
        host,
        port,
        subscriptions=subscriptions,
        username=username,
        password=password,
        tls=tls,
        tls_mode=tls_mode,
    )


def discover_broker_topics(
    broker,
    *,
    listener_factory=default_listener_factory,
    timeout_s=DEFAULT_TIMEOUT_S,
    subscriptions=DEFAULT_SUBSCRIPTIONS,
    max_topics=MAX_TOPICS_PER_BROKER,
    max_candidates=MAX_CANDIDATES_PER_BROKER,
):
    """Listen briefly to one broker and return hardware candidate dicts.

    Any listener failure (unreachable broker, missing paho, protocol error) is
    swallowed and yields whatever was collected so far, so discovery of one
    broker can never break the Admin request.
    """

    timeout_s = max(0.0, min(float(timeout_s), MAX_TIMEOUT_S))
    aggregator = MqttTopicAggregator(
        broker, max_topics=max_topics, max_candidates=max_candidates
    )
    listener = listener_factory(
        broker.get("host"),
        broker.get("port"),
        subscriptions=subscriptions,
        username=broker.get("username"),
        password=broker.get("password"),
        tls=bool(broker.get("tls")),
        tls_mode=broker.get("tls_mode"),
    )
    try:
        listener.listen(timeout_s, aggregator.observe)
    except Exception:
        pass
    return aggregator.results()


def default_topic_discoverer(broker):
    """Broker-store hook: return hardware candidates for a reachable broker."""

    try:
        return discover_broker_topics(broker)
    except Exception:
        return []
