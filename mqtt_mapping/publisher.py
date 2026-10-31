# SPDX-License-Identifier: AGPL-3.0-or-later
"""When the mapping publishes, and when it stays silent.

A device's ``state`` goes out at the publish interval even when unchanged, and
at once when it changes, at most once a second; the standard asks a bridge for
a value at least every 30 seconds. Each message is retained, so discovery finds
it in its few seconds, and expires after three intervals, so a stopped service
leaves no ghost behind that the EMS would read as fresh after a reconnect. A
device with a stale or unreadable source publishes nothing at all.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from mqtt_mapping.config import Mappings
from mqtt_mapping.mapping import Evaluation, Received, evaluate_device

MIN_EXPIRY_SECONDS = 45
MIN_CHANGE_GAP_SECONDS = 1.0


def expiry_seconds(interval_seconds) -> int:
    return max(MIN_EXPIRY_SECONDS, 3 * int(interval_seconds))


def state_payload(state) -> bytes:
    return json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class _Published:
    payload: bytes
    at: float
    wall: float


class MappingPublisher:
    """Holds the latest payload of every source topic and publishes each device's state.

    ``publish(broker_ref, topic, payload, expiry_seconds)`` sends one retained
    message and returns whether the client accepted it. The clocks are
    injected, so tests decide every interleaving.
    """

    def __init__(
        self,
        mappings: Mappings,
        publish: Callable[[str, str, bytes, int], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._publish = publish
        self._clock = clock
        self._wall_clock = wall_clock
        self._latest: dict[tuple[str, str], Received] = {}
        self._published: dict[str, _Published] = {}
        self._evaluations: dict[str, Evaluation] = {}
        self.mappings = Mappings()
        self.replace_mappings(mappings)

    def replace_mappings(self, mappings: Mappings) -> None:
        """Use new mappings; keep what was received on topics that are still read."""

        wanted = {
            (ref, topic) for ref, topics in mappings.subscriptions().items() for topic in topics
        }
        self._latest = {key: value for key, value in self._latest.items() if key in wanted}
        ids = {device.id for device in mappings.devices}
        self._published = {key: value for key, value in self._published.items() if key in ids}
        self._evaluations = {}
        self.mappings = mappings

    def observe(self, broker_ref: str, topic: str, payload) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._latest[(broker_ref, topic)] = Received(bytes(payload or b""), self._clock())

    def tick(self) -> list[str]:
        """Publish every device that is due; return the ids published."""

        now = self._clock()
        interval = self.mappings.publish_interval_seconds
        published = []
        for device in self.mappings.devices:
            latest = {
                topic: received
                for (ref, topic), received in self._latest.items()
                if ref == device.broker_ref
            }
            evaluation = evaluate_device(device, latest, now)
            self._evaluations[device.id] = evaluation
            if evaluation.state is None:
                continue
            payload = state_payload(evaluation.state)
            last = self._published.get(device.id)
            due = (
                last is None
                or now - last.at >= interval
                or (payload != last.payload and now - last.at >= MIN_CHANGE_GAP_SECONDS)
            )
            if not due:
                continue
            if self._publish(device.broker_ref, device.output_topic, payload, expiry_seconds(interval)):
                self._published[device.id] = _Published(payload, now, self._wall_clock())
                published.append(device.id)
        return published

    def evaluations(self) -> dict[str, Evaluation]:
        return dict(self._evaluations)

    def last_published_wall(self, device_id: str) -> float | None:
        last = self._published.get(device_id)
        return last.wall if last else None
