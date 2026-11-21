# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the Admin Console server: ``python -m admin --host ... --port ...``.

Starts the Admin HTTP listener always and, when ``--https`` (or
``EMS_ADMIN_HTTPS_ENABLED``) is set, an additional HTTPS listener on a second
port. Both listeners share one ``AdminRuntime``; only the transport differs.
HTTP is never disabled or redirected, so a browser that distrusts the generated
self-signed certificate can still fall back to HTTP.
"""

import argparse
import json
import os
import threading

from admin.ems_forwarding import shares_host_network
from admin.install_context import detect_install_context
from admin.https import (
    DEFAULT_ADMIN_HTTPS_PORT,
    DEFAULT_ADMIN_SSL_CERT_FILE,
    DEFAULT_ADMIN_SSL_KEY_FILE,
    ensure_admin_ssl_context,
    wrap_listening_socket,
)
from admin.mqtt_broker import follow_builtin_broker_config, reconcile_builtin_broker
from admin.releases import default_admin_data_dir
from admin.server import (
    create_admin_runtime,
    create_server,
    resume_pending_guided_upgrade,
)
from dashboard.console_address import ConsoleLease, console_address, publish_console_address
from ems.config import normalize_dashboard_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise SystemExit(f"{name} must be one of: 1, true, yes, on, 0, false, no, off")


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="EMS Admin Console server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    # Optional parallel HTTPS listener. Env vars seed the defaults for container
    # use; explicit CLI flags override them.
    parser.add_argument(
        "--https",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("EMS_ADMIN_HTTPS_ENABLED", False),
        help="Start an additional HTTPS listener (HTTP stays available).",
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=_env_int("EMS_ADMIN_HTTPS_PORT", DEFAULT_ADMIN_HTTPS_PORT),
    )
    parser.add_argument(
        "--https-cert-file",
        default=os.environ.get("EMS_ADMIN_HTTPS_CERT_FILE", DEFAULT_ADMIN_SSL_CERT_FILE),
    )
    parser.add_argument(
        "--https-key-file",
        default=os.environ.get("EMS_ADMIN_HTTPS_KEY_FILE", DEFAULT_ADMIN_SSL_KEY_FILE),
    )
    parser.add_argument(
        "--https-auto-generate",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("EMS_ADMIN_HTTPS_AUTO_GENERATE", True),
    )
    return parser.parse_args(argv)


def _print_startup(args, https_enabled):
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print("Admin Console:")
    print(f"  http://{display_host}:{args.port}")
    if https_enabled:
        print(f"  https://{display_host}:{args.https_port}")
        print()
        print(
            "HTTPS uses a local self-signed certificate unless custom cert files "
            "are configured."
        )
        print("Your browser may show a certificate warning.")
    print("Read-only discovery; enter a local CIDR to scan. Press Ctrl+C to stop.")


ADMIN_SESSIONS_FILE = "admin-sessions.json"


SESSION_LIMITS = (1800, 43200)


def session_limits():
    """The sign-in's idle and absolute limits, read at start: the installed EMS's (``dashboard.session_*``) where they are shorter.

    The console's session can change the whole installation, so the EMS's
    settings only ever tighten it; ``0`` (never ends) and longer limits keep
    the console's own.
    """

    try:
        config = json.loads(detect_install_context().config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        config = {}
    dashboard = normalize_dashboard_config(config.get("dashboard") if isinstance(config, dict) else None)
    configured = (dashboard["session_idle_timeout_seconds"], dashboard["session_absolute_max_seconds"])
    return tuple(value if 0 < value < limit else limit for value, limit in zip(configured, SESSION_LIMITS))


def main(argv=None):
    args = parse_args(argv)

    if args.host not in LOOPBACK_HOSTS:
        print(
            f"WARNING: binding admin server to {args.host}. Device discovery "
            "scans the local network — expose only on trusted local networks, "
            "never to the internet."
        )

    # A deterministic browser-test runtime, gated behind an env flag that is
    # never set in a normal deployment. It fakes only external effects (Docker,
    # release download, Admin replacement); auth/CSRF/gating stay real.
    if _env_bool("EMS_ADMIN_TEST_MODE"):
        from admin.test_support import build_test_runtime

        runtime = build_test_runtime(data_dir=default_admin_data_dir())
    else:
        runtime = create_admin_runtime(
            auth_session_path=default_admin_data_dir() / "state" / ADMIN_SESSIONS_FILE,
            auth_session_limits=session_limits(),
        )
    runtime.https_configured = bool(args.https)
    runtime.https_port = int(args.https_port)
    publishing = not _env_bool("EMS_ADMIN_TEST_MODE")
    if publishing and args.host not in LOOPBACK_HOSTS and shares_host_network():
        runtime.console_address = console_address(args.port, int(args.https_port) if args.https else None)

    try:
        http_server = create_server(
            args.host, args.port, runtime=runtime, https_active=False
        )
    except OSError as exc:
        raise SystemExit(
            f"could not bind admin server to {args.host}:{args.port} ({exc}). "
            "Is the port already in use? Try a different --port."
        )

    https_server = None
    if args.https:
        if int(args.https_port) == int(args.port):
            http_server.server_close()
            raise SystemExit("Admin HTTPS port must differ from the HTTP port")
        try:
            https_server = create_server(
                args.host, args.https_port, runtime=runtime, https_active=True
            )
        except OSError as exc:
            http_server.server_close()
            raise SystemExit(
                f"could not bind admin HTTPS server to {args.host}:{args.https_port} "
                f"({exc}). Is the port already in use? Try a different --https-port."
            )
        try:
            context = ensure_admin_ssl_context(
                {
                    "host": args.host,
                    "ssl_cert_file": args.https_cert_file,
                    "ssl_key_file": args.https_key_file,
                    "ssl_auto_generate": args.https_auto_generate,
                }
            )
            https_server.socket = wrap_listening_socket(
                context, https_server.socket
            )
        except Exception as exc:
            # HTTPS was explicitly requested and failed: do not pretend it is up.
            https_server.server_close()
            http_server.server_close()
            raise SystemExit(f"could not enable admin HTTPS: {exc}")

    _print_startup(args, https_server is not None)
    if publishing:
        record = getattr(runtime, "console_address", None)
        if record is None:
            publish_console_address(detect_install_context().config_path.parent, None)
        else:
            ConsoleLease(lambda: detect_install_context().config_path.parent, record).start()

    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    if https_server is not None:
        threading.Thread(target=https_server.serve_forever, daemon=True).start()

    # mDNS is shared runtime state; start it exactly once for both listeners.
    mdns_status = runtime.mdns_provider.start()
    print(mdns_status["message"])

    # An Admin installed by a Guided Upgrade finishes it. The durable
    # transition already carries the operator's confirmation, and an open
    # browser is not part of that contract: waiting for one left a completed
    # Admin replacement sitting until its deadline. Off the serving threads,
    # because continuing pulls an image.
    def _continue_pending_upgrade():
        outcome = resume_pending_guided_upgrade(runtime)
        if outcome is not None:
            print(outcome)

    threading.Thread(target=_continue_pending_upgrade, daemon=True).start()

    def _reconcile_builtin_broker():
        compose = getattr(runtime, "mqtt_compose", None)

        def reconcile():
            outcome = reconcile_builtin_broker(runtime.credential_store, compose=compose)
            if outcome is not None:
                print(outcome)

        follow_builtin_broker_config(runtime.credential_store, compose=compose, start=reconcile)

    threading.Thread(target=_reconcile_builtin_broker, daemon=True).start()

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopping admin server.")
    finally:
        for server in (http_server, https_server):
            if server is not None:
                server.shutdown()
                server.server_close()
        runtime.mdns_provider.stop()


if __name__ == "__main__":
    main()
