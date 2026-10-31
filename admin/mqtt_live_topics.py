# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live topics: what a broker carries, recognised, grouped and named, for mapping.

A session listens to ``#`` on one local broker while its dialog is open and
ends two minutes after the last poll; nothing listens to ``#`` otherwise. It
only reads, and nothing it shows reaches the EMS: only a saved mapping does.

Everything is shown, known or not, because unknown devices are what the mapping
is for. A recogniser gives a group its key and a label such as ``shelly_gen2``;
a label is a name, never an assignment, so a wrong one does no harm. Devices
the EMS reads directly, and the mapping's own outputs, are labelled too and
marked as not mappable. Every payload counts as hostile and is bounded.
"""

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from admin.mqtt_topic_discovery import classify_topic
from ems.config import is_zendure_smartmeter_d0_topic
from ems.mqtt_control.topic_families import FAMILY_UNKNOWN
from ems.zendure_mqtt.external_catalog import in_catalog_namespace
from ems.zendure_mqtt.payload_values import MAX_PATH_SEGMENTS, plain_number

MAX_TOPICS = 1000
MAX_PAYLOAD_BYTES = 16 * 1024
PAYLOAD_PREVIEW_CHARS = 200
MAX_VALUES_PER_TOPIC = 64
SESSION_IDLE_SECONDS = 120
REAP_INTERVAL_SECONDS = 15
COLLECTION_PREFIXES = ("shellies", "tele", "stat", "cmnd")
LABELS = (
    "shelly_gen1",
    "shelly_gen2",
    "tasmota",
    "opendtu",
    "ahoydtu",
    "zendure_native",
    "zendure_meter_native",
    "ems_output",
    "unknown",
)
NOT_MAPPABLE = frozenset({"zendure_native", "zendure_meter_native", "ems_output"})
_SHELLY_COMPONENT = re.compile(r"[a-z0-9]+:\d+")
_DTU_SERIAL = re.compile(r"\d{10,16}")
_AHOY_CHANNEL = re.compile(r"ch\d+")
_LIKELY_POWER = re.compile(r"(power|p_ac|act_power|apower|watt)", re.IGNORECASE)
_LIKELY_SOC = re.compile(r"(soc|level|charge_state)", re.IGNORECASE)


@dataclass(frozen=True)
class Recognized:
    label: str
    group: str


def recognize(topic) -> Recognized:
    """The label and group key of one topic; ``unknown`` grouped by its tree otherwise."""

    segments = topic.split("/")
    if in_catalog_namespace(topic) and len(segments) >= 2:
        return Recognized("ems_output", segments[1])
    if is_zendure_smartmeter_d0_topic(topic):
        return Recognized("zendure_meter_native", segments[2] if len(segments) > 2 else topic)
    match = classify_topic(topic)
    if match.family != FAMILY_UNKNOWN:
        return Recognized("zendure_native", match.device_id or topic)
    if segments[0] == "shellies" and len(segments) >= 3:
        return Recognized("shelly_gen1", segments[1])
    if len(segments) >= 2 and (
        (len(segments) == 2 and segments[1] == "online")
        or (len(segments) == 3 and segments[1] == "status" and _SHELLY_COMPONENT.fullmatch(segments[2]))
        or (len(segments) == 3 and segments[1] == "events" and segments[2] == "rpc")
    ):
        return Recognized("shelly_gen2", segments[0])
    if segments[0] in ("tele", "stat", "cmnd") and len(segments) >= 3:
        return Recognized("tasmota", segments[1])
    if len(segments) >= 4 and _DTU_SERIAL.fullmatch(segments[1]) and segments[2].isdigit():
        return Recognized("opendtu", segments[1])
    if len(segments) >= 4 and _AHOY_CHANNEL.fullmatch(segments[2]):
        return Recognized("ahoydtu", segments[1])
    if segments[0] in COLLECTION_PREFIXES and len(segments) >= 2:
        return Recognized("unknown", f"{segments[0]}/{segments[1]}")
    return Recognized("unknown", segments[0] or "/")


def _leaves(value, path, depth, out):
    if len(out) >= MAX_VALUES_PER_TOPIC:
        return
    if isinstance(value, dict) and depth < MAX_PATH_SEGMENTS:
        for key, item in value.items():
            if isinstance(key, str) and "." not in key and key:
                _leaves(item, f"{path}.{key}" if path else key, depth + 1, out)
    elif isinstance(value, list) and depth < MAX_PATH_SEGMENTS:
        for index, item in enumerate(value[:MAX_VALUES_PER_TOPIC]):
            _leaves(item, f"{path}.{index}" if path else str(index), depth + 1, out)
    elif path:
        number = plain_number(value)
        if number is not None:
            out.append((path, number))


def _likely(path):
    last = (path or "").rsplit(".", 1)[-1]
    if _LIKELY_POWER.search(last):
        return "power"
    if _LIKELY_SOC.search(last):
        return "soc"
    return None


def topic_values(payload, topic=None) -> tuple[str | None, list[dict]]:
    """``(format, values)``: the numbers a payload carries and the path to each.

    A plain number has no path, so the topic's last level says what it likely is.
    """

    number = plain_number(payload)
    if number is not None:
        name = str(topic or "").rsplit("/", 1)[-1]
        return "number", [{"path": None, "value": number, "likely": _likely(name) if name else None}]
    try:
        data = json.loads(bytes(payload).decode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        return None, []
    leaves = []
    _leaves(data, "", 0, leaves)
    values = [{"path": path, "value": value, "likely": _likely(path)} for path, value in leaves]
    values.sort(key=lambda item: (item["likely"] is None, item["path"]))
    return ("json" if values else None), values


@dataclass
class _Topic:
    payload: bytes
    retained: bool
    first_seen: float
    last_seen: float
    count: int = 1
    changes: int = 0


@dataclass
class TopicTree:
    """The bounded record of what one session has seen."""

    clock: callable = time.monotonic
    topics: dict = field(default_factory=dict)
    truncated: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, topic, payload, retained=False):
        if not isinstance(topic, str) or topic.startswith("$"):
            return
        payload = bytes(payload or b"")[:MAX_PAYLOAD_BYTES]
        now = self.clock()
        with self.lock:
            known = self.topics.get(topic)
            if known is None:
                if len(self.topics) >= MAX_TOPICS:
                    self.truncated = True
                    return
                self.topics[topic] = _Topic(payload, bool(retained), now, now)
                return
            if payload != known.payload:
                known.changes += 1
            known.payload, known.last_seen, known.count = payload, now, known.count + 1
            known.retained = known.retained or bool(retained)

    def groups(self, mapped=None) -> list[dict]:
        """Groups of topics with their values, unknown ones last; ``mapped`` maps topic to device ids."""

        mapped = mapped or {}
        now = self.clock()
        with self.lock:
            items = list(self.topics.items())
        recognized_by_topic = {topic: recognize(topic) for topic, _seen in items}
        device_prefixes = {
            rec.group: rec.label for rec in recognized_by_topic.values() if rec.label == "shelly_gen2"
        }
        groups = {}
        for topic, seen in sorted(items):
            recognized = recognized_by_topic[topic]
            first = topic.split("/", 1)[0]
            if recognized.label == "unknown" and first in device_prefixes:
                recognized = Recognized(device_prefixes[first], first)
            key = (recognized.label if recognized.label != "unknown" else "", recognized.group)
            group = groups.setdefault(
                key,
                {
                    "key": recognized.group,
                    "label": recognized.label,
                    "mappable": recognized.label not in NOT_MAPPABLE,
                    "topics": [],
                },
            )
            payload_format, values = topic_values(seen.payload, topic)
            minutes = max(1.0, (now - seen.first_seen) / 60.0)
            interval = (seen.last_seen - seen.first_seen) / (seen.count - 1) if seen.count >= 3 else None
            group["topics"].append(
                {
                    "topic": topic,
                    "format": payload_format,
                    "retained": seen.retained,
                    "messages_per_minute": round(seen.count / minutes, 1),
                    "interval_seconds": round(interval, 1) if interval else None,
                    "changing": seen.changes > 0,
                    "last_seen_seconds": round(now - seen.last_seen, 1),
                    "first_seen_seconds": round(now - seen.first_seen, 1),
                    "payload_preview": seen.payload[:PAYLOAD_PREVIEW_CHARS].decode("utf-8", errors="replace"),
                    "values": [
                        {**value, "mapped_to": sorted(mapped.get((topic, value["path"]), ()))}
                        for value in values
                    ],
                }
            )
        ordered = sorted(groups.values(), key=lambda item: (item["label"] == "unknown", item["label"], item["key"]))
        return ordered

    def latest_payloads(self) -> dict:
        with self.lock:
            return {topic: seen.payload for topic, seen in self.topics.items()}


class PahoWatcher:
    """Read-only ``#`` subscriber that runs until stopped; imported lazily."""

    def __init__(self, endpoint, tree):
        self.endpoint = endpoint
        self.tree = tree
        self.client = None
        self.state = "connecting"

    def start(self):
        import paho.mqtt.client as mqtt

        from ems.config import configure_mqtt_client_tls

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        configure_mqtt_client_tls(
            client, tls=bool(self.endpoint.get("tls")), tls_insecure=bool(self.endpoint.get("tls_insecure"))
        )
        if self.endpoint.get("username"):
            client.username_pw_set(self.endpoint["username"], self.endpoint.get("password"))

        def on_connect(client_, _userdata, _flags, reason, _properties=None):
            if getattr(reason, "is_failure", False):
                self.state = "refused"
                return
            self.state = "listening"
            client_.subscribe("#", qos=0)

        client.on_connect = on_connect
        client.on_disconnect = lambda *_args: setattr(self, "state", "disconnected")
        client.on_message = lambda _c, _u, message: self.tree.observe(message.topic, message.payload, message.retain)
        self.client = client
        try:
            client.connect_async(self.endpoint["host"], int(self.endpoint["port"]), keepalive=30)
        except (OSError, ValueError):
            self.state = "unreachable"
            return
        client.loop_start()

    def stop(self):
        if self.client is None:
            return
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.client.loop_stop()


@dataclass
class _Session:
    id: str
    broker_ref: str
    watcher: object
    tree: TopicTree
    last_poll: float


class LiveTopicSessions:
    """At most one session per broker; each ends when nobody polls it."""

    def __init__(self, *, watcher_factory=PahoWatcher, clock=time.monotonic, wait=None, spawn=None):
        self._watcher_factory = watcher_factory
        self._clock = clock
        self._sessions = {}
        self._lock = threading.Lock()
        self._wait = wait or threading.Event().wait
        self._spawn = spawn or (lambda target: threading.Thread(target=target, daemon=True).start())
        self._reaping = False

    def start(self, broker_ref, endpoint) -> str:
        self.reap()
        with self._lock:
            for session in list(self._sessions.values()):
                if session.broker_ref == broker_ref:
                    session.watcher.stop()
                    del self._sessions[session.id]
            tree = TopicTree(clock=self._clock)
            watcher = self._watcher_factory(endpoint, tree)
            session = _Session(secrets.token_urlsafe(12), broker_ref, watcher, tree, self._clock())
            self._sessions[session.id] = session
            if not self._reaping:
                self._reaping = True
                self._spawn(self.reap_until_empty)
        watcher.start()
        return session.id

    def get(self, session_id):
        self.reap()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_poll = self._clock()
            return session

    def snapshot(self, session_id, mapped=None) -> dict | None:
        session = self.get(session_id)
        if session is None:
            return None
        return {
            "session": session.id,
            "broker_ref": session.broker_ref,
            "state": getattr(session.watcher, "state", None),
            "truncated": session.tree.truncated,
            "groups": session.tree.groups(mapped),
        }

    def stop(self, session_id) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.watcher.stop()
        return True

    def reap_until_empty(self):
        """End idle sessions until none is left, so a closed dialog never keeps listening."""

        while True:
            self._wait(REAP_INTERVAL_SECONDS)
            self.reap()
            with self._lock:
                if not self._sessions:
                    self._reaping = False
                    return

    def reap(self):
        now = self._clock()
        with self._lock:
            idle = [s for s in self._sessions.values() if now - s.last_poll > SESSION_IDLE_SECONDS]
            for session in idle:
                del self._sessions[session.id]
        for session in idle:
            session.watcher.stop()
