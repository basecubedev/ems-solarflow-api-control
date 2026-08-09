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
from pathlib import Path

from appliance.agent import AgentHandlers, AgentServer, operation_names
from appliance.agent_client import AgentCallError, AgentClient, AgentUnavailableError, InProcessAgentClient
from appliance.auth import AuthError, AuthStore
from appliance.config import load_config
from appliance.migration import migrate_state, write_report
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
    _record_password_reset(paths)
    print("appliance password updated; all existing sessions were invalidated")
    return EXIT_OK


def _record_password_reset(paths):
    """The CLI is privileged, so it writes the audit entry itself."""

    from appliance.audit import AuditLog

    try:
        AuditLog(paths.audit_log).record("password.reset", user="root", target="cli")
    except OSError:
        print("warning: the password reset could not be written to the audit log", file=sys.stderr)


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
    previous = paths.packages_dir / "previous.deb"
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


def command_migrate_state(args):
    paths = resolve_paths()
    if os.geteuid() != 0:
        print("error: state migration must run as root", file=sys.stderr)
        return EXIT_ERROR
    report = migrate_state(paths)
    write_report(paths, report)
    if not args.quiet or not report.ok:
        _print(report.to_dict() if args.json else _migration_summary(report), args.json)
    for entry in report.conflicts:
        print(
            f"warning: {entry.source} needs a decision: {entry.detail}",
            file=sys.stderr,
        )
    # A conflict preserves both copies and waits for an operator; only a move
    # that did not arrive makes the layout unusable.
    return EXIT_ERROR if report.fatal else EXIT_OK


def _migration_summary(report):
    return {
        "ok": report.ok,
        "migrated": [entry.source for entry in report.migrated],
        "conflicts": [f"{entry.source} ({entry.detail})" for entry in report.conflicts],
        "fatal": [f"{entry.result}: {entry.source} ({entry.detail})" for entry in report.fatal],
    }


def command_verify_install(args):
    """Post-install check: is this installation actually usable?"""

    from appliance.install_check import verify_installation

    report = verify_installation(resolve_paths(), live=None if args.live is None else args.live)
    if args.json:
        _print(report, True)
    else:
        for entry in report["checks"]:
            print(f"{entry['status']:>12}  {entry['check']}: {entry['detail']}")
        if report["failures"]:
            print("\nfailed:", file=sys.stderr)
            for failure in report["failures"]:
                print(f"  {failure}", file=sys.stderr)
    return EXIT_OK if report["ok"] else EXIT_ERROR


def command_host_config(args):
    """Show — or regenerate — the derived host-path files."""

    from appliance.commands import CommandRunner
    from appliance.host_config import (
        HostConfigError,
        apply_host_config,
        describe,
        live_activation,
    )

    paths = resolve_paths()
    config = load_config(paths)
    runner = CommandRunner()
    if not args.apply:
        _print(describe(paths, config, runner=runner), args.json)
        return EXIT_OK
    if os.geteuid() != 0:
        print("error: writing the host configuration needs root", file=sys.stderr)
        return EXIT_ERROR
    try:
        report = apply_host_config(paths, config, activation=live_activation(runner=runner))
    except HostConfigError as exc:
        if args.json:
            _print({"applied": False, "error": exc.code, "message": exc.message, **exc.rollback}, True)
            return EXIT_ERROR
        print(f"error: {exc.message}", file=sys.stderr)
        rollback = exc.rollback
        if rollback:
            print(
                f"  files: {rollback.get('disk_rollback')}, "
                f"runtime: {rollback.get('runtime_rollback')}, "
                f"backup authentication disabled: {rollback.get('authentication_disabled')}",
                file=sys.stderr,
            )
            differences = rollback.get("differences") or {}
            for item in rollback.get("remaining_drift") or []:
                print(f"  still not restored: {item}", file=sys.stderr)
                detail = differences.get(item) or {}
                if "expected" in detail:
                    detail = {item: detail}
                for name, values in sorted(detail.items()):
                    if not isinstance(values, dict):
                        continue
                    print(
                        f"    {name}: expected {values.get('expected')!r}, "
                        f"observed {values.get('observed')!r}",
                        file=sys.stderr,
                    )
        return EXIT_ERROR
    _print(report, args.json)
    return EXIT_OK


def command_backup_access(args):
    """Activate or disable the confined backup account, fail-closed."""

    from appliance.backup_confinement import STATE_ACTIVE, STATE_UNAVAILABLE, build_activation

    paths = resolve_paths()
    config = load_config(paths)
    service = build_activation(paths=paths, config=config)
    if args.action == "status":
        # A read-only revalidation: the same evidence activation uses, without
        # changing anything. Support and package verification both need it.
        report = dict(service.observe())
        report["exports"] = service.export_state()
        report["policy"] = service.effective_policy()
        _print(report, args.json)
        return EXIT_OK
    if os.geteuid() != 0:
        print("error: changing backup access needs root", file=sys.stderr)
        return EXIT_ERROR
    report = service.disable(reason="requested") if args.action == "disable" else service.activate()
    _print(report, args.json)
    if args.action == "disable":
        return EXIT_OK
    return EXIT_OK if report["state"] in (STATE_ACTIVE, STATE_UNAVAILABLE) else EXIT_ERROR


PACKAGE_LIBDIR = "/usr/lib/ems-appliance-manager"


def _account_helper():
    """The packaged shell that owns the record; the same one ``postinst`` calls."""

    libdir = os.environ.get("EMS_APPLIANCE_LIBDIR") or PACKAGE_LIBDIR
    return os.path.join(libdir, "backup-account.sh")


def command_backup_account(args):
    """Report what the ownership record proves, and adopt a legacy one on request.

    Adoption is never automatic and never takes a path from anywhere but the
    record: the helper validates the configured account and home itself, prints
    what it is about to adopt, and refuses key material it cannot attribute.
    """

    import subprocess

    from appliance import backup_ownership

    paths = resolve_paths()
    config = load_config(paths)
    if args.action == "status":
        report = {
            "account": config.backup_user,
            "state": backup_ownership.ownership_state(paths, config.backup_user),
            "record": backup_ownership.read_record(paths).to_dict(),
        }
        _print(report, args.json)
        return EXIT_OK

    if os.geteuid() != 0:
        print("error: migrating the backup ownership record needs root", file=sys.stderr)
        return EXIT_ERROR
    helper = _account_helper()
    if not os.path.isfile(helper):
        print(f"error: the packaged account helper {helper} is not installed", file=sys.stderr)
        return EXIT_ERROR
    completed = subprocess.run(  # noqa: S603 - fixed packaged path, no caller input
        [helper, "migrate-ownership"], check=False, timeout=300
    )
    return EXIT_OK if completed.returncode == 0 else EXIT_ERROR


def command_agent(args):
    paths = resolve_paths()
    ensure_directories(paths, role="agent")
    if os.geteuid() == 0:
        write_report(paths, migrate_state(paths))
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
    ensure_directories(paths, role="web")
    if not (args.address or args.port):
        return serve_web(paths=paths)
    config = load_config(paths)
    address = (args.address or config.web_address, args.port or config.web_port)
    return serve_web(paths=paths, config=config, address=address)


def command_operations_list(args):
    _print({"operations": operation_names()}, args.json)
    return EXIT_OK


def _shared_flags():
    """``--json`` and ``--local`` work before and after the subcommand.

    ``SUPPRESS`` keeps the subparser from overwriting a value the top-level
    parser already stored when the flag was given before the subcommand.
    """

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="print raw JSON"
    )
    shared.add_argument(
        "--local",
        action="store_true",
        default=argparse.SUPPRESS,
        help="bypass the agent socket and run privileged code in this process (root only)",
    )
    return shared


# --- A/B operating-system updates -------------------------------------------


def _ab_services(args):
    """The privileged A/B services. Root only: this path touches block devices."""

    if os.geteuid() != 0:
        print("error: this command must run as root", file=sys.stderr)
        return None
    return build_services(root=getattr(args, "root", "/") or "/")


def command_ab_status(args):
    paths = resolve_paths()
    try:
        result = _client(paths, local=args.local).call("ab.status")
    except (AgentUnavailableError, AgentCallError) as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    _print(result if args.json else _ab_summary(result), args.json)
    return EXIT_OK


def _ab_summary(status):
    state = status.get("ab_state") or {}
    selector = status.get("selector") or {}
    return {
        "mode": status.get("mode"),
        "ab_supported": status.get("ab_supported"),
        "reason": status.get("reason"),
        "active_slot": status.get("active_slot"),
        "inactive_slot": status.get("inactive_slot"),
        "tryboot": status.get("tryboot"),
        "known_good_slot": state.get("known_good_slot"),
        "previous_slot": state.get("previous_slot"),
        "default_boot_partition": selector.get("default_partition"),
        "trial_boot_partition": selector.get("tryboot_partition"),
        "pending_trial": bool(state.get("pending_trial")),
        "persistence": (status.get("persistence") or {}).get("state"),
        "drift": status.get("drift"),
    }


def command_ab_verify_persistence(args):
    from appliance import ab_layout, ab_persistence

    services = _ab_services(args)
    if services is None:
        return EXIT_ERROR
    layout = ab_layout.discover(services.ab_probe)
    report = ab_persistence.verify(layout, services.ab_probe.mounts())
    if not args.quiet:
        _print(report.to_dict(), args.json)
    if not report.ok:
        for problem in report.problems:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def command_host_identity(args):
    """Establish and prove the identity both slots share.

    Idempotent: an existing host key is validated and left exactly as it is, so
    the fingerprint an operator verified on first boot survives every slot
    switch. Only public fingerprints are ever printed.
    """

    from appliance.host_identity import HostIdentityService

    services = _ab_services(args)
    if services is None:
        return EXIT_ERROR
    service = HostIdentityService(runner=services.runner, root=services.ab_probe.root)
    report = service.ensure() if args.ensure else service.verify()
    payload = report.to_dict()
    if report.ok:
        # sshd has the last word: a configuration it refuses must not start.
        sshd = service.validate_sshd()
        payload["sshd_config"] = sshd.to_dict()
        if not sshd.ok:
            payload["ok"] = False
            payload["problems"] = [*payload["problems"], sshd.detail]
    if not args.quiet or not payload["ok"]:
        _print(payload, args.json)
    if not payload["ok"]:
        for problem in payload.get("problems") or []:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def command_ab_write_layout(args):
    """Write the layout descriptor into a rootfs being built.

    Labels only. image-rota generates the partition identities per build, so a
    descriptor that named them would be a second authority over the same disk.
    """

    from appliance import ab_layout, ab_persistence

    descriptor = {
        "schema_version": ab_layout.LAYOUT_SCHEMA_VERSION,
        "layout_id": args.layout_id,
        "slot_schema_version": ab_layout.LAYOUT_SCHEMA_VERSION,
        "persistent_schema_version": ab_persistence.PERSISTENT_SCHEMA_VERSION,
        "image_layer": "image-rota",
        "image_layer_version": args.image_layer_version,
        "slot_device_prefix": "/dev/disk/by-slot",
        "bootconfig": {"label": "bootconfig", "fstype": "vfat"},
        "persist": {"label": "persistent", "fstype": "ext4"},
        "selector_mountpoint": "/bootfs",
        "persist_mountpoint": ab_persistence.PERSISTENT_MOUNTPOINT,
        "boot_mountpoint": "/boot/firmware",
        "shared_root": ab_persistence.SHARED_ROOT,
        "machine_id_source": ab_persistence.MACHINE_ID_SOURCE,
        "slots": {
            "A": {
                "boot": {"label": "boot_a", "fstype": "vfat"},
                "root": {"label": "system_a", "fstype": "ext4"},
            },
            "B": {
                "boot": {"label": "boot_b", "fstype": "vfat"},
                "root": {"label": "system_b", "fstype": "ext4"},
            },
        },
    }
    # Parsing it back is the check: the build fails rather than shipping a
    # descriptor the runtime would reject as drift on first boot.
    ab_layout.parse_layout_manifest(descriptor)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(target))
    return EXIT_OK


def _ab_persistent(args, attribute):
    """Read-only discovery for the one-shot growth unit. Nothing here mutates."""

    from appliance import ab_layout

    services = _ab_services(args)
    if services is None:
        return None
    layout = ab_layout.discover(services.ab_probe)
    if layout.persist_device is None or layout.manifest is None:
        print("error: the persistent partition could not be identified", file=sys.stderr)
        return None
    return {
        "device": layout.persist_device.path,
        "disk": layout.persist_device.parent,
        "number": layout.persist_device.number,
        "mountpoint": layout.manifest.persist_mountpoint,
    }.get(attribute)


def _print_ab_persistent(args, attribute):
    value = _ab_persistent(args, attribute)
    if value is None:
        return EXIT_ERROR
    print(value)
    return EXIT_OK


def command_ab_persistent_device(args):
    return _print_ab_persistent(args, "device")


def command_ab_persistent_disk(args):
    return _print_ab_persistent(args, "disk")


def command_ab_persistent_partition_number(args):
    return _print_ab_persistent(args, "number")


def command_ab_slot_bootstrap(args):
    """Rebuild this slot's container runtime from the shared record."""

    services = _ab_services(args)
    if services is None:
        return EXIT_ERROR
    if services.ab_bootstrap is None:
        print("error: no runtime bootstrap is configured", file=sys.stderr)
        return EXIT_ERROR
    report = services.ab_bootstrap.reconstruct()
    _print(report.to_dict(), args.json)
    if not report.ok:
        for problem in report.problems:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def command_ab_trial_health(args):
    """The trial slot judging itself, run by ems-appliance-ab-health.service.

    Only a booted target slot reaches a commit here, and only after every
    required gate passed. Anything else records what it saw and steps aside.
    """

    from appliance import ab_health
    from appliance.ab_health import TrialHealthService

    services = _ab_services(args)
    if services is None:
        return EXIT_ERROR
    layout_manifest = services.ab_probe.manifest()
    if layout_manifest is None:
        print("this appliance has no A/B layout; nothing to verify")
        return EXIT_OK

    selector_path = (
        services.ab_probe.root
        / str(layout_manifest.selector_mountpoint).lstrip("/")
        / "autoboot.txt"
    )
    verifier = TrialHealthService(
        probe=services.ab_probe,
        state=services.ab_state,
        selector_path=selector_path,
        runner=services.runner,
        systemd=services.systemd,
        docker=services.ab_docker_health,
        runtime=services.ab_runtime,
        # The gate compares the fingerprint the trial was planned against; the
        # bootstrap service is what re-reads the files behind it.
        bootstrap=services.ab_bootstrap,
        install_check=lambda: services.status.overview().get("health", {}).get("level") != "error",
        agent_socket=lambda: services.paths.agent_socket.exists(),
        health_window_seconds=services.config.ab_health_window_seconds,
    )
    report = verifier.evaluate()
    _print(report.to_dict(), args.json)

    if report.result == ab_health.RESULT_NOT_A_TRIAL:
        # The selector first: an ordinary boot of the trial slot whose default
        # already names it is a commit whose state write did not survive.
        committed = ab_health.reconcile_boot(
            services.ab_probe, services.ab_state, selector_path
        )
        if committed is not None:
            services.audit.record("ab.commit", target=committed.slot, result="reconciled")
            print(
                f"reconciled: slot {committed.slot} is the boot default and is now recorded "
                "as known-good",
                file=sys.stderr,
            )
            return EXIT_OK
        record = ab_health.classify_fallback(services.ab_probe, services.ab_state)
        if record is not None:
            services.audit.record(
                "ab.fallback_observed", target=record.target_slot, operation_id=record.operation_id
            )
            print(f"fallback observed: slot {record.target_slot} did not commit", file=sys.stderr)
        return EXIT_OK
    if report.result == ab_health.RESULT_MANUAL_ACTION_REQUIRED:
        services.audit.record(
            "ab.trial_health_failed", target=report.slot, result="denied",
            operation_id=report.operation_id,
        )
        print("error: this trial boot cannot be proven; manual action required", file=sys.stderr)
        return EXIT_ERROR
    if not report.healthy:
        services.audit.record(
            "ab.trial_health_failed", target=report.slot, result="failure",
            operation_id=report.operation_id,
        )
        if args.commit:
            verifier.abandon(report)
        return EXIT_ERROR
    if not args.commit:
        return EXIT_OK

    result = verifier.commit(report)
    services.audit.record("ab.commit", target=result["slot"], operation_id=result["operation_id"])
    _print(result, args.json)
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
    shared = _shared_flags()
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "status", parents=[shared], help="show the appliance overview"
    ).set_defaults(handler=command_status)

    repair = subparsers.add_parser(
        "repair", parents=[shared], help="inspect and optionally repair the Admin deployment"
    )
    repair.add_argument("--apply", action="store_true", help="execute the listed repair actions")
    repair.set_defaults(handler=command_repair)

    reset = subparsers.add_parser(
        "password-reset", parents=[shared], help="set a new Appliance Manager password"
    )
    reset.add_argument("--password", help="non-interactive password (avoid on shared hosts)")
    reset.set_defaults(handler=command_password_reset)

    subparsers.add_parser(
        "operations", parents=[shared], help="list recent appliance operations"
    ).set_defaults(handler=command_operations)

    subparsers.add_parser(
        "rollback-manager", parents=[shared], help="reinstall the previous Appliance Manager package"
    ).set_defaults(handler=command_rollback_manager)

    subparsers.add_parser(
        "allowlist", parents=[shared], help="print the agent operation allowlist"
    ).set_defaults(handler=command_operations_list)

    migrate = subparsers.add_parser(
        "migrate-state", parents=[shared], help="move legacy state into the web/agent split"
    )
    migrate.add_argument("--quiet", action="store_true", help="print only findings")
    migrate.set_defaults(handler=command_migrate_state)

    verify = subparsers.add_parser(
        "verify-install", parents=[shared], help="check that this installation is usable"
    )
    verify.add_argument(
        "--live",
        dest="live",
        action="store_const",
        const=True,
        default=None,
        help="assume a running systemd even when none was detected",
    )
    verify.add_argument(
        "--offline",
        dest="live",
        action="store_const",
        const=False,
        help="treat this as an image-build root: service startup is deferred, not failed",
    )
    verify.set_defaults(handler=command_verify_install)

    host_config = subparsers.add_parser(
        "host-config", parents=[shared], help="show or regenerate the derived host-path files"
    )
    host_config.add_argument(
        "--apply",
        action="store_true",
        help="write the environment file and the export path-unit drop-in (root only)",
    )
    host_config.set_defaults(handler=command_host_config)

    backup_access = subparsers.add_parser(
        "backup-access",
        parents=[shared],
        help="activate, disable or inspect the confined SFTP backup account",
    )
    backup_access.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "activate", "disable"),
        help="activate only enables the account once the effective confinement is verified",
    )
    backup_access.set_defaults(handler=command_backup_access)

    backup_account = subparsers.add_parser(
        "backup-account",
        parents=[shared],
        help="report the backup ownership record, or adopt a legacy one explicitly",
    )
    backup_account.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "migrate-ownership"),
        help="migrate-ownership adopts a record that predates the home ownership marker",
    )
    backup_account.set_defaults(handler=command_backup_account)

    ab = subparsers.add_parser(
        "ab", parents=[shared], help="fail-safe A/B operating-system updates"
    )
    ab_commands = ab.add_subparsers(dest="ab_command", required=True)
    ab_commands.add_parser(
        "status", parents=[shared], help="slot, selector and persistence state"
    ).set_defaults(handler=command_ab_status)
    verify_persistence = ab_commands.add_parser(
        "verify-persistence", parents=[shared], help="prove the shared state is really shared"
    )
    verify_persistence.add_argument("--quiet", action="store_true", help="report only failures")
    verify_persistence.set_defaults(handler=command_ab_verify_persistence)
    host_identity = subparsers.add_parser(
        "host-identity",
        parents=[shared],
        help="establish and prove the persistent SSH host keys and machine identity",
    )
    host_identity.add_argument(
        "--ensure", action="store_true", help="create anything missing, never regenerate"
    )
    host_identity.add_argument("--quiet", action="store_true", help="report only failures")
    host_identity.set_defaults(handler=command_host_identity)
    write_layout = ab_commands.add_parser(
        "write-layout", parents=[shared], help="write the layout descriptor (image build)"
    )
    write_layout.add_argument("--layout-id", default="ems-appliance-rota-v1")
    write_layout.add_argument("--image-layer-version", default="5.5.1")
    write_layout.add_argument(
        "--output", default="/etc/ems-appliance-manager/ab-layout.json"
    )
    write_layout.set_defaults(handler=command_ab_write_layout)
    ab_commands.add_parser(
        "persistent-device", parents=[shared], help="print the persistent partition device"
    ).set_defaults(handler=command_ab_persistent_device)
    ab_commands.add_parser(
        "persistent-disk", parents=[shared], help="print the disk the layout lives on"
    ).set_defaults(handler=command_ab_persistent_disk)
    ab_commands.add_parser(
        "persistent-partition-number", parents=[shared], help="print its partition number"
    ).set_defaults(handler=command_ab_persistent_partition_number)
    ab_commands.add_parser(
        "slot-bootstrap", parents=[shared], help="rebuild this slot's container runtime"
    ).set_defaults(handler=command_ab_slot_bootstrap)
    trial_health = ab_commands.add_parser(
        "trial-health", parents=[shared], help="verify a trial boot and optionally commit it"
    )
    trial_health.add_argument(
        "--commit", action="store_true", help="commit this slot when every gate passes"
    )
    trial_health.set_defaults(handler=command_ab_trial_health)

    agent = subparsers.add_parser(
        "agent", parents=[shared], help="run the privileged agent (systemd entry point)"
    )
    agent.add_argument("--socket", help="override the agent socket path")
    agent.set_defaults(handler=command_agent)

    web = subparsers.add_parser(
        "web", parents=[shared], help="run the web service (systemd entry point)"
    )
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
