# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ems-appliance`` — the local recovery CLI.

The web interface must never be the only way back into an appliance. This CLI
runs as root on the console or over SSH and can reset the password, inspect the
host, repair the Admin deployment and roll the Appliance Manager itself back to
the previously installed package.
"""

import argparse
import json
import os
import sys

from appliance.agent import AgentHandlers, AgentServer, operation_names
from appliance.agent_client import AgentCallError, AgentClient, AgentUnavailableError, InProcessAgentClient
from appliance.auth import AuthError, AuthStore
from appliance.paths import ensure_directories, resolve_paths
from appliance.services import build_services
from appliance.version import APPLIANCE_VERSION, PACKAGE_NAME
from appliance.web import serve as serve_web

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 2


def _print(payload, as_json):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    _print_human(payload, 0)


def _print_human(payload, depth):
    pad = "  " * depth
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{pad}{key}:")
                _print_human(value, depth + 1)
            else:
                print(f"{pad}{key}: {value}")
    elif isinstance(payload, list):
        for item in payload:
            _print_human(item, depth)
    else:
        print(f"{pad}{payload}")


def _client(paths, *, local=False):
    """Prefer the running agent; fall back to an in-process privileged client."""

    if not local:
        client = AgentClient(paths.agent_socket)
        if client.available():
            return client
    if os.geteuid() != 0:
        raise SystemExit(
            "the appliance agent is not running and this command needs root; "
            "run it with sudo or start ems-appliance-agent.service"
        )
    return InProcessAgentClient(AgentHandlers(build_services(paths=paths)))


def command_status(args):
    paths = resolve_paths()
    try:
        result = _client(paths, local=args.local).call("status.get")
    except (AgentUnavailableError, AgentCallError) as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    _print(result if args.json else _status_summary(result), args.json)
    return EXIT_OK


def _status_summary(status):
    health = status.get("health") or {}
    system = status.get("system") or {}
    admin = status.get("admin") or {}
    docker = status.get("docker") or {}
    updates = status.get("updates") or {}
    return {
        "appliance_version": status.get("appliance_version"),
        "health": health.get("level"),
        "model": (system.get("hardware") or {}).get("model"),
        "os": (system.get("operating_system") or {}).get("name"),
        "uptime_days": (system.get("uptime") or {}).get("days"),
        "temperature_c": (system.get("temperature") or {}).get("celsius"),
        "docker": (docker.get("daemon") or {}).get("state"),
        "admin_version": admin.get("version"),
        "admin_healthy": admin.get("healthy"),
        "security_updates": updates.get("security_count"),
        "reboot_required": updates.get("reboot_required"),
        "warnings": [item.get("code") for item in health.get("warnings", [])],
    }


def command_repair(args):
    paths = resolve_paths()
    client = _client(paths, local=args.local)
    try:
        planned = client.call("admin.plan_repair")
    except (AgentUnavailableError, AgentCallError) as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR

    plan = planned.get("plan") or {}
    _print({"findings": plan.get("findings", []), "actions": plan.get("actions", [])}, args.json)

    operation_id = (planned.get("operation") or {}).get("operation_id")
    if not args.apply:
        print("\nRun with --apply to execute the listed repair actions.")
        client.call("operations.cancel", operation_id=operation_id)
        return EXIT_OK

    try:
        client.call(
            "operations.execute",
            operation_id=operation_id,
            confirmation_token=planned.get("confirmation_token"),
        )
    except AgentCallError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    print(f"repair operation {operation_id} started")
    return EXIT_OK


def command_password_reset(args):
    paths = resolve_paths()
    ensure_directories(paths)
    store = AuthStore(paths.auth_file)
    if os.geteuid() != 0 and not os.access(str(paths.auth_file.parent), os.W_OK):
        print("error: run this command as root", file=sys.stderr)
        return EXIT_ERROR

    import getpass

    password = args.password or getpass.getpass("New appliance password: ")
    confirmation = args.password or getpass.getpass("Repeat password: ")
    try:
        store.reset(password, confirmation)
    except AuthError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    _chown_web_user(paths)
    print("appliance password updated; all existing sessions were invalidated")
    return EXIT_OK


def _chown_web_user(paths):
    import pwd

    try:
        entry = pwd.getpwnam("ems-appliance-web")
    except KeyError:
        return
    try:
        os.chown(paths.auth_file, entry.pw_uid, entry.pw_gid)
    except OSError:
        pass


def command_operations(args):
    paths = resolve_paths()
    try:
        result = _client(paths, local=args.local).call("operations.list")
    except (AgentUnavailableError, AgentCallError) as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    _print(result, args.json)
    return EXIT_OK


def command_rollback_manager(args):
    """Reinstall the previously installed Appliance Manager package."""

    paths = resolve_paths()
    previous = paths.state_dir / "packages" / "previous.deb"
    if not previous.is_file():
        print(
            "error: no previous Appliance Manager package is retained at "
            f"{previous}; reinstall it with apt",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if os.geteuid() != 0:
        print("error: run this command as root", file=sys.stderr)
        return EXIT_ERROR

    from appliance.commands import CommandRunner

    runner = CommandRunner()
    result = runner.run("dpkg", ["--install", str(previous)], timeout=600)
    print(result.stdout or result.stderr)
    return EXIT_OK if result.ok else EXIT_ERROR


def command_agent(args):
    paths = resolve_paths()
    ensure_directories(paths)
    if os.geteuid() != 0:
        print("error: the appliance agent must run as root", file=sys.stderr)
        return EXIT_ERROR
    services = build_services(paths=paths)
    recovered = services.operations.recover_interrupted()
    if recovered:
        print(f"recovered {len(recovered)} interrupted operation(s)")
    server = AgentServer(services, socket_path=args.socket or paths.agent_socket)
    print(f"appliance agent listening on {server.socket_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


def command_web(args):
    paths = resolve_paths()
    address = (args.address, args.port) if args.address or args.port else None
    if address:
        config = None
        return serve_web(paths=paths, config=config, address=(args.address or "0.0.0.0", args.port or 8080))
    return serve_web(paths=paths)


def command_operations_list(args):
    _print({"operations": operation_names()}, args.json)
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ems-appliance",
        description="EMS SolarFlow Raspberry Pi Appliance Manager host CLI",
    )
    parser.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {APPLIANCE_VERSION}")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument(
        "--local",
        action="store_true",
        help="bypass the agent socket and run privileged code in this process (root only)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="show the appliance overview").set_defaults(
        handler=command_status
    )

    repair = subparsers.add_parser("repair", help="inspect and optionally repair the Admin deployment")
    repair.add_argument("--apply", action="store_true", help="execute the listed repair actions")
    repair.set_defaults(handler=command_repair)

    reset = subparsers.add_parser("password-reset", help="set a new Appliance Manager password")
    reset.add_argument("--password", help="non-interactive password (avoid on shared hosts)")
    reset.set_defaults(handler=command_password_reset)

    subparsers.add_parser("operations", help="list recent appliance operations").set_defaults(
        handler=command_operations
    )

    subparsers.add_parser(
        "rollback-manager", help="reinstall the previous Appliance Manager package"
    ).set_defaults(handler=command_rollback_manager)

    subparsers.add_parser("allowlist", help="print the agent operation allowlist").set_defaults(
        handler=command_operations_list
    )

    agent = subparsers.add_parser("agent", help="run the privileged agent (systemd entry point)")
    agent.add_argument("--socket", help="override the agent socket path")
    agent.set_defaults(handler=command_agent)

    web = subparsers.add_parser("web", help="run the web service (systemd entry point)")
    web.add_argument("--address", help="bind address")
    web.add_argument("--port", type=int, help="bind port")
    web.set_defaults(handler=command_web)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
