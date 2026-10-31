# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m mqtt_mapping``: run the service, or answer the container health check."""

import argparse
import logging
import signal
import sys
import threading

from mqtt_mapping.service import MappingService, ServicePaths
from mqtt_mapping.status import status_is_fresh

DEFAULT_MAPPINGS_PATH = "config/mqtt/mappings.json"
DEFAULT_BROKERS_PATH = "mqtt/mapping/mapping-credentials.json"
DEFAULT_STATUS_PATH = "data/mqtt-mapping-status.json"


def _parser():
    parser = argparse.ArgumentParser(prog="python -m mqtt_mapping")
    parser.add_argument("--mappings", default=DEFAULT_MAPPINGS_PATH)
    parser.add_argument("--brokers", default=DEFAULT_BROKERS_PATH)
    parser.add_argument("--status", default=DEFAULT_STATUS_PATH)
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="exit 0 when the status file was written recently, else 1",
    )
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.healthcheck:
        return 0 if status_is_fresh(args.status) else 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_args: stop.set())
    MappingService(ServicePaths(args.mappings, args.brokers, args.status)).run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
