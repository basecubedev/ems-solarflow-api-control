# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the admin discovery server: ``python -m admin --host ... --port ...``.

Kept dependency-free (only ``admin.server``) so the container image needs just
the ``admin/`` package and ``dashboard/static_files.py``.
"""

import argparse

from admin.server import create_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Admin device-discovery server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding admin server to {args.host}. Device discovery "
            "scans the local network — expose only on trusted local networks, "
            "never to the internet."
        )

    try:
        server = create_server(args.host, args.port)
    except OSError as exc:
        raise SystemExit(
            f"could not bind admin server to {args.host}:{args.port} ({exc}). "
            "Is the port already in use? Try a different --port."
        )

    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"Admin discovery: http://{display_host}:{args.port}")
    print("Enter a local CIDR (e.g. 192.168.178.0/24) and start a scan.")
    print("Read-only: no config.json is written. Press Ctrl+C to stop.")

    mdns_status = server.mdns_provider.start()
    print(mdns_status["message"])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping admin server.")
    finally:
        server.mdns_provider.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
