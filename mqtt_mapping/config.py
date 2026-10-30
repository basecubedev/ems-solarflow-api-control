# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mapping file, ``config/mqtt/mappings.json``, and every rule it must keep.

The Admin validates a draft here before it writes the file, and the service
parses the file here again before it subscribes to anything, so the two never
disagree on what a mapping means. Targets are the external catalog's keys and
nothing else: a mapping adds no topic and no key to the EMS.
"""

import json
import math
import os
from dataclasses import dataclass

from ems.file_io import file_signature
from ems.zendure_mqtt.builtin_broker import NATIVE_TOPIC_ROOTS
from ems.zendure_mqtt.external_catalog import EMS_SOLARFLOW, valid_device_id
from ems.zendure_mqtt.payload_values import PAYLOAD_FORMATS, valid_json_path

SCHEMA_VERSION = 1
OUTPUT_PREFIX = EMS_SOLARFLOW.prefix
OUTPUT_KEY = EMS_SOLARFLOW.bundle_key
TARGETS = tuple(EMS_SOLARFLOW.keys)
REQUIRED_TARGET = EMS_SOLARFLOW.required_key
SPLIT_TARGET = "batteryPower"
NEGATIVE_MODES = ("keep", "zero")

MAX_DEVICES = 32
MAX_SUBSCRIPTIONS = 64
MAX_BROKERS = 4
MAX_SOURCES = 8
MAX_TOPIC_LENGTH = 256
MAX_LABEL_LENGTH = 64
MAX_BROKER_REF_LENGTH = 64
MAX_FACTOR = 1_000_000
MIN_MAX_AGE_SECONDS = 10
MAX_MAX_AGE_SECONDS = 900
DEFAULT_MAX_AGE_SECONDS = 60
MIN_PUBLISH_INTERVAL_SECONDS = 5
MAX_PUBLISH_INTERVAL_SECONDS = 30
DEFAULT_PUBLISH_INTERVAL_SECONDS = 15

ISSUE_CODES = (
    "mqtt_mapping_document_invalid",
    "mqtt_mapping_schema_unsupported",
    "mqtt_mapping_limit_exceeded",
    "mqtt_mapping_broker_invalid",
    "mqtt_mapping_device_id_invalid",
    "mqtt_mapping_device_id_duplicate",
    "mqtt_mapping_target_unknown",
    "mqtt_mapping_target_required_missing",
    "mqtt_mapping_battery_shape_invalid",
    "mqtt_mapping_topic_invalid",
    "mqtt_mapping_topic_native",
    "mqtt_mapping_path_invalid",
    "mqtt_mapping_option_invalid",
    "mqtt_mapping_source_shared",
    "mqtt_mapping_brokers_unreadable",
)


@dataclass(frozen=True)
class Source:
    """One value read from one topic, and how it is turned into a reading."""

    topic: str
    format: str
    path: str | None = None
    factor: int | float = 1
    invert: bool = False
    negative: str = "keep"
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.topic, self.path)


@dataclass(frozen=True)
class Target:
    """A catalog key and the sources it sums; ``charge`` minus ``discharge`` for a split battery."""

    key: str
    sources: tuple[Source, ...] = ()
    charge: tuple[Source, ...] = ()
    discharge: tuple[Source, ...] = ()

    @property
    def split(self) -> bool:
        return bool(self.charge or self.discharge)

    def all_sources(self) -> tuple[Source, ...]:
        return self.sources + self.charge + self.discharge


@dataclass(frozen=True)
class MappedDevice:
    id: str
    broker_ref: str
    targets: tuple[Target, ...]
    label: str = ""
    enabled: bool = True

    @property
    def output_topic(self) -> str:
        return f"{OUTPUT_PREFIX}/{self.id}/{OUTPUT_KEY}"

    def all_sources(self) -> tuple[Source, ...]:
        return tuple(source for target in self.targets for source in target.all_sources())


@dataclass(frozen=True)
class Mappings:
    devices: tuple[MappedDevice, ...] = ()
    publish_interval_seconds: int = DEFAULT_PUBLISH_INTERVAL_SECONDS

    def active_devices(self) -> tuple[MappedDevice, ...]:
        return tuple(device for device in self.devices if device.enabled)

    def subscriptions(self) -> dict[str, tuple[str, ...]]:
        """The topics each broker must deliver for the active devices, in first-seen order."""

        topics: dict[str, dict[str, None]] = {}
        for device in self.active_devices():
            bucket = topics.setdefault(device.broker_ref, {})
            for source in device.all_sources():
                bucket.setdefault(source.topic, None)
        return {ref: tuple(names) for ref, names in topics.items()}


def empty_document() -> dict:
    return {"schema_version": SCHEMA_VERSION, "devices": []}


def _issue(code, path, message):
    return {"code": code, "path": path, "message": message}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def topic_problem(topic) -> str | None:
    """The issue code a source topic earns, or ``None`` when it may be read."""

    if not isinstance(topic, str) or not topic or len(topic) > MAX_TOPIC_LENGTH:
        return "mqtt_mapping_topic_invalid"
    if any(char in topic for char in "+#") or topic.startswith("$"):
        return "mqtt_mapping_topic_invalid"
    if any(ord(char) < 32 or ord(char) == 127 for char in topic):
        return "mqtt_mapping_topic_invalid"
    root = topic.split("/", 1)[0]
    if root == OUTPUT_PREFIX:
        return "mqtt_mapping_topic_invalid"
    if root in NATIVE_TOPIC_ROOTS:
        return "mqtt_mapping_topic_native"
    return None


class _Parser:
    def __init__(self, broker_refs):
        self.broker_refs = None if broker_refs is None else frozenset(broker_refs)
        self.issues = []

    def add(self, code, path, message):
        self.issues.append(_issue(code, path, message))

    def document(self, document):
        if not isinstance(document, dict):
            self.add("mqtt_mapping_document_invalid", "", "The mapping file must be a JSON object.")
            return None
        if document.get("schema_version") != SCHEMA_VERSION:
            self.add(
                "mqtt_mapping_schema_unsupported",
                "schema_version",
                f"Only schema_version {SCHEMA_VERSION} is understood.",
            )
            return None
        interval = document.get("publish_interval_seconds", DEFAULT_PUBLISH_INTERVAL_SECONDS)
        if not _is_int(interval) or not (
            MIN_PUBLISH_INTERVAL_SECONDS <= interval <= MAX_PUBLISH_INTERVAL_SECONDS
        ):
            self.add(
                "mqtt_mapping_option_invalid",
                "publish_interval_seconds",
                f"The publish interval is a whole number of seconds from "
                f"{MIN_PUBLISH_INTERVAL_SECONDS} to {MAX_PUBLISH_INTERVAL_SECONDS}.",
            )
            interval = DEFAULT_PUBLISH_INTERVAL_SECONDS
        raw_devices = document.get("devices", [])
        if not isinstance(raw_devices, list):
            self.add("mqtt_mapping_document_invalid", "devices", "devices must be a list.")
            return None
        if len(raw_devices) > MAX_DEVICES:
            self.add(
                "mqtt_mapping_limit_exceeded",
                "devices",
                f"At most {MAX_DEVICES} mapped devices are allowed.",
            )
            return None
        devices = []
        seen_ids = set()
        for index, raw in enumerate(raw_devices):
            device = self.device(raw, f"devices[{index}]")
            if device is None:
                continue
            if device.id in seen_ids:
                self.add(
                    "mqtt_mapping_device_id_duplicate",
                    f"devices[{index}].id",
                    f"The id {device.id} is used twice.",
                )
                continue
            seen_ids.add(device.id)
            devices.append(device)
        mappings = Mappings(tuple(devices), interval)
        self.limits(mappings)
        self.shared_sources(mappings)
        return mappings

    def device(self, raw, path):
        if not isinstance(raw, dict):
            self.add("mqtt_mapping_document_invalid", path, "A mapped device must be a JSON object.")
            return None
        before = len(self.issues)
        device_id = raw.get("id")
        if not valid_device_id(device_id):
            self.add(
                "mqtt_mapping_device_id_invalid",
                f"{path}.id",
                "The id is 1 to 64 characters of A-Z, a-z, 0-9, _ and -.",
            )
        label = raw.get("label", "")
        if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
            self.add(
                "mqtt_mapping_option_invalid",
                f"{path}.label",
                f"The label is text of at most {MAX_LABEL_LENGTH} characters.",
            )
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            self.add("mqtt_mapping_option_invalid", f"{path}.enabled", "enabled is true or false.")
        broker_ref = raw.get("broker_ref")
        if not self.valid_broker_ref(broker_ref):
            self.add(
                "mqtt_mapping_broker_invalid",
                f"{path}.broker_ref",
                "The broker must be the built-in broker or a local MQTT profile.",
            )
        targets = self.targets(raw.get("values"), f"{path}.values")
        if len(self.issues) != before:
            return None
        return MappedDevice(
            id=device_id,
            broker_ref=broker_ref,
            targets=targets,
            label=label,
            enabled=enabled,
        )

    def valid_broker_ref(self, ref) -> bool:
        if not isinstance(ref, str) or not ref.strip() or len(ref) > MAX_BROKER_REF_LENGTH:
            return False
        return self.broker_refs is None or ref in self.broker_refs

    def targets(self, raw, path):
        if not isinstance(raw, dict):
            self.add("mqtt_mapping_document_invalid", path, "values must be a JSON object.")
            return ()
        targets = []
        for key, spec in raw.items():
            if key not in TARGETS:
                self.add(
                    "mqtt_mapping_target_unknown",
                    f"{path}.{key}",
                    f"{key} is no key of the external-device standard.",
                )
                continue
            target = self.target(key, spec, f"{path}.{key}")
            if target is not None:
                targets.append(target)
        if REQUIRED_TARGET not in raw:
            self.add(
                "mqtt_mapping_target_required_missing",
                path,
                f"Every mapped device needs {REQUIRED_TARGET}.",
            )
        return tuple(targets)

    def target(self, key, spec, path):
        if not isinstance(spec, dict):
            self.add("mqtt_mapping_document_invalid", path, "A target must be a JSON object.")
            return None
        has_sources = "sources" in spec
        has_split = "charge" in spec or "discharge" in spec
        unknown = set(spec) - {"sources", "charge", "discharge"}
        if unknown or (has_split and (key != SPLIT_TARGET or has_sources)) or not (
            has_sources or has_split
        ):
            self.add(
                "mqtt_mapping_battery_shape_invalid",
                path,
                f"A target sums sources; only {SPLIT_TARGET} may instead take charge and discharge.",
            )
            return None
        if has_sources:
            return Target(key, sources=self.sources(spec["sources"], f"{path}.sources", required=True))
        charge = self.sources(spec.get("charge", []), f"{path}.charge", required=False)
        discharge = self.sources(spec.get("discharge", []), f"{path}.discharge", required=False)
        if not charge and not discharge:
            self.add(
                "mqtt_mapping_battery_shape_invalid",
                path,
                "A split battery needs a charge or a discharge source.",
            )
            return None
        return Target(key, charge=charge, discharge=discharge)

    def sources(self, raw, path, *, required):
        if not isinstance(raw, list) or len(raw) > MAX_SOURCES or (required and not raw):
            self.add(
                "mqtt_mapping_option_invalid",
                path,
                f"A target takes 1 to {MAX_SOURCES} sources.",
            )
            return ()
        sources = []
        for index, item in enumerate(raw):
            source = self.source(item, f"{path}[{index}]")
            if source is not None:
                sources.append(source)
        return tuple(sources)

    def source(self, raw, path):
        if not isinstance(raw, dict):
            self.add("mqtt_mapping_document_invalid", path, "A source must be a JSON object.")
            return None
        before = len(self.issues)
        topic = raw.get("topic")
        problem = topic_problem(topic)
        if problem == "mqtt_mapping_topic_native":
            self.add(
                problem,
                f"{path}.topic",
                "This topic belongs to a device the EMS reads directly; add it under Find devices.",
            )
        elif problem:
            self.add(
                problem,
                f"{path}.topic",
                "A source topic is exact, without + or #, not $-prefixed, not in "
                f"{OUTPUT_PREFIX}/ and at most {MAX_TOPIC_LENGTH} characters.",
            )
        payload_format = raw.get("format")
        payload_path = raw.get("path")
        if payload_format not in PAYLOAD_FORMATS:
            self.add("mqtt_mapping_path_invalid", f"{path}.format", "format is number or json.")
        elif payload_format == "json" and not valid_json_path(payload_path):
            self.add(
                "mqtt_mapping_path_invalid",
                f"{path}.path",
                "A json source needs a dotted path of at most 8 segments.",
            )
        elif payload_format == "number" and payload_path not in (None, ""):
            self.add("mqtt_mapping_path_invalid", f"{path}.path", "A number source takes no path.")
        factor = raw.get("factor", 1)
        if not _is_number(factor) or factor == 0 or abs(factor) > MAX_FACTOR:
            self.add(
                "mqtt_mapping_option_invalid",
                f"{path}.factor",
                f"The factor is a number other than 0, at most {MAX_FACTOR} either way.",
            )
        invert = raw.get("invert", False)
        if not isinstance(invert, bool):
            self.add("mqtt_mapping_option_invalid", f"{path}.invert", "invert is true or false.")
        negative = raw.get("negative", "keep")
        if negative not in NEGATIVE_MODES:
            self.add("mqtt_mapping_option_invalid", f"{path}.negative", "negative is keep or zero.")
        max_age = raw.get("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)
        if not _is_int(max_age) or not (MIN_MAX_AGE_SECONDS <= max_age <= MAX_MAX_AGE_SECONDS):
            self.add(
                "mqtt_mapping_option_invalid",
                f"{path}.max_age_seconds",
                f"The maximum age is a whole number of seconds from {MIN_MAX_AGE_SECONDS} "
                f"to {MAX_MAX_AGE_SECONDS}.",
            )
        if len(self.issues) != before:
            return None
        return Source(
            topic=topic,
            format=payload_format,
            path=payload_path if payload_format == "json" else None,
            factor=factor,
            invert=invert,
            negative=negative,
            max_age_seconds=max_age,
        )

    def limits(self, mappings):
        subscriptions = mappings.subscriptions()
        if len(subscriptions) > MAX_BROKERS:
            self.add(
                "mqtt_mapping_limit_exceeded",
                "devices",
                f"Mappings may read from at most {MAX_BROKERS} brokers.",
            )
        if sum(len(topics) for topics in subscriptions.values()) > MAX_SUBSCRIPTIONS:
            self.add(
                "mqtt_mapping_limit_exceeded",
                "devices",
                f"Mappings may read at most {MAX_SUBSCRIPTIONS} topics.",
            )

    def shared_sources(self, mappings):
        """The same topic and path counted twice would count one device twice."""

        owners = {}
        for index, device in enumerate(mappings.devices):
            for target in device.targets:
                seen_in_target = set()
                for source in target.all_sources():
                    key = (device.broker_ref, source.topic, source.path)
                    owner = owners.setdefault(key, device.id)
                    if owner != device.id or key in seen_in_target:
                        self.add(
                            "mqtt_mapping_source_shared",
                            f"devices[{index}].values.{target.key}",
                            f"The source {source.topic} is already counted elsewhere.",
                        )
                    seen_in_target.add(key)


def parse_mappings(document, *, broker_refs=None):
    """``(Mappings, [])`` for a valid document, or ``(None, issues)``.

    ``broker_refs`` names the brokers a mapping may use; ``None`` checks only
    that a reference is well formed.
    """

    parser = _Parser(broker_refs)
    mappings = parser.document(document)
    if parser.issues:
        return None, parser.issues
    return mappings, []


def validate_mappings(document, *, broker_refs=None) -> list[dict]:
    """Every rule a mapping document breaks, as ``{code, path, message}``."""

    return parse_mappings(document, broker_refs=broker_refs)[1]


def load_mappings_file(path, *, broker_refs=None):
    """``(Mappings, issues)`` from the file; no file is no mappings, not an error."""

    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return Mappings(), []
    except OSError as exc:
        return None, [_issue("mqtt_mapping_document_invalid", "", f"The file cannot be read: {exc.strerror}.")]
    try:
        document = json.loads(raw)
    except ValueError:
        return None, [_issue("mqtt_mapping_document_invalid", "", "The file is no valid JSON.")]
    return parse_mappings(document, broker_refs=broker_refs)


def mappings_file_signature(path):
    """What tells one version of the file from the next, or ``None`` without a file."""

    try:
        return file_signature(os.stat(path))
    except OSError:
        return None
