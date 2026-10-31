# SPDX-License-Identifier: AGPL-3.0-or-later
"""The running service: connections, the publish loop, reloads and the status file.

The mapping file and the broker file are re-read when their signature changes,
so an applied mapping takes effect without a restart, and a connection is only
replaced when its broker or its topics change. A file that does not validate
leaves the last good mappings running and is reported in the status.
"""

import logging
import threading
import time
from dataclasses import dataclass

from ems.logging_utils import log_event
from mqtt_mapping.brokers import PahoConnection, load_broker_file
from mqtt_mapping.config import Mappings, load_mappings_file, mappings_file_signature
from mqtt_mapping.publisher import MappingPublisher
from mqtt_mapping.status import build_status, write_status

TICK_SECONDS = 0.25
RELOAD_CHECK_SECONDS = 2.0
STATUS_WRITE_SECONDS = 5.0


@dataclass(frozen=True)
class ServicePaths:
    mappings: str
    brokers: str
    status: str


class MappingService:
    def __init__(self, paths: ServicePaths, *, connection_factory=PahoConnection, clock=time.monotonic):
        self.paths = paths
        self._connection_factory = connection_factory
        self._clock = clock
        self._signatures = None
        self._connections = {}
        self._endpoints = {}
        self._issues = []
        self._lock = threading.Lock()
        self.publisher = MappingPublisher(Mappings(), self._publish, clock=clock)

    def _publish(self, broker_ref, topic, payload, expiry_seconds):
        connection = self._connections.get(broker_ref)
        return bool(connection and connection.publish(topic, payload, expiry_seconds))

    def _observer(self, broker_ref):
        def observe(topic, payload):
            with self._lock:
                self.publisher.observe(broker_ref, topic, payload)

        return observe

    def reload_if_changed(self) -> bool:
        signatures = (
            mappings_file_signature(self.paths.mappings),
            mappings_file_signature(self.paths.brokers),
        )
        if signatures == self._signatures:
            return False
        self._signatures = signatures
        endpoints, broker_issues = load_broker_file(self.paths.brokers)
        mappings, issues = load_mappings_file(self.paths.mappings, broker_refs=endpoints)
        self._issues = broker_issues + issues
        if mappings is None:
            log_event(logging.WARNING, "mqtt_mapping_config_rejected", issues=len(issues))
            return False
        with self._lock:
            self.publisher.replace_mappings(mappings)
        self._reconnect(endpoints, mappings.subscriptions())
        log_event(
            logging.INFO,
            "mqtt_mapping_config_loaded",
            devices=len(mappings.devices),
            active=len(mappings.active_devices()),
            brokers=len(endpoints),
        )
        return True

    def _reconnect(self, endpoints, subscriptions):
        for ref in list(self._connections):
            connection = self._connections[ref]
            if endpoints.get(ref) != connection.endpoint or subscriptions.get(ref) != connection.topics:
                connection.stop()
                del self._connections[ref]
        for ref, topics in subscriptions.items():
            if ref in self._connections or ref not in endpoints:
                continue
            connection = self._connection_factory(endpoints[ref], topics, self._observer(ref))
            self._connections[ref] = connection
            connection.start()
        self._endpoints = endpoints

    def tick(self):
        with self._lock:
            return self.publisher.tick()

    def status(self):
        brokers = {ref: connection.state() for ref, connection in self._connections.items()}
        with self._lock:
            return build_status(publisher=self.publisher, brokers=brokers, issues=self._issues)

    def write_status(self):
        write_status(self.paths.status, self.status())

    def stop(self):
        for connection in self._connections.values():
            connection.stop()
        self._connections = {}

    def run(self, stop_event: threading.Event):
        next_reload = next_status = 0.0
        try:
            while not stop_event.is_set():
                now = self._clock()
                if now >= next_reload:
                    self.reload_if_changed()
                    next_reload = now + RELOAD_CHECK_SECONDS
                self.tick()
                if now >= next_status:
                    self.write_status()
                    next_status = now + STATUS_WRITE_SECONDS
                stop_event.wait(TICK_SECONDS)
        finally:
            self.stop()
            self.write_status()
