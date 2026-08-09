# Raspberry Pi Appliance Manager — Architecture

The Appliance Manager is host management and recovery for an EMS SolarFlow
appliance. It runs **directly on Raspberry Pi OS as systemd services**, outside
Docker, so it stays reachable when the EMS Admin container is stopped, an Admin
update fails, the Admin image will not start, the Compose deployment is
incomplete, or EMS itself is down.

It is a separate product surface from the EMS Admin Console. It never edits EMS
configuration, never touches device discovery and never defines a second EMS
backup format.

## Who owns what

| Layer | Owns |
|---|---|
| **Appliance Manager** | Raspberry Pi OS status, OS package updates, host reboot/shutdown, network and WLAN state, hostname and mDNS, Docker service state, EMS Admin container installation/restart/version/reinstall/rollback, Admin image and container diagnostics, SSH service and public keys, storage and temperature, host logs, appliance-level recovery |
| **EMS Admin Console** | EMS configuration, device discovery, grid-meter and inverter configuration, control parameters, EMS runtime state, EMS diagnostics, EMS backup/restore semantics, Guided Setup, Guided Upgrade, application-level maintenance |
| **EMS** | Energy-management logic, runtime control, device communication, configuration validation, EMS backup/restore logic, runtime safety and reconciliation |

Explicit non-goals: no arbitrary shell execution, no browser terminal, no
general Linux package manager, no free-form systemd editing, no manual editing
of arbitrary host files, no EMS configuration editing, no inverter or
grid-meter configuration, no duplicated EMS backup format, no unrestricted
Docker container management, no unrestricted Docker image execution.

## Two host services

```text
Browser
  │
  ▼
ems-appliance-web.service          unprivileged (user ems-appliance-web)
  · authentication, sessions, CSRF, rate limiting
  · rendering and the JSON API
  · no root, no Docker socket, no host command
  │
  │  local Unix socket /run/ems-appliance-manager/agent.sock  (0750 dir, 0660 socket)
  ▼
ems-appliance-agent.service        privileged (root)
  · fixed operation allowlist, typed fields
  · re-validates every request
  · owns the durable operation store
  │
  ├── systemd            (allowlisted units only)
  ├── Docker Engine      (typed argv, never a shell string)
  ├── apt / dpkg         (packages parsed by the appliance itself)
  ├── NetworkManager     (nmcli, passphrase on stdin)
  ├── OpenSSH            (authorized_keys, atomic writes)
  └── local filesystem   (canonical paths only)
```

The socket is never a network listener. The agent checks the peer's credentials
(`SO_PEERCRED`) and serves only `root` and the web service account.

A request names an operation and typed fields:

```json
{ "operation": "admin.install", "target_tag": "v0.8.0" }
```

It can never carry a command, a path, an image reference or a repository:

```json
{ "command": "docker pull ..." }
```

is refused as `invalid_request` before any handler runs.

## Module map (`appliance/`)

| Module | Responsibility |
|---|---|
| `paths.py` | The canonical appliance layout and path-boundary validation |
| `config.py` | `/etc` host configuration and the image allowlist |
| `validation.py` | Every typed input validator and its stable error code |
| `redaction.py` | Secret redaction and log bounding |
| `protocol.py` | The fixed agent operation allowlist |
| `operations.py` | The durable operation model and the single-mutation lock |
| `commands.py` | The only place a host process is started (tool allowlist, no shell) |
| `docker_backend.py`, `systemd.py`, `hostprobe.py` | Typed host access |
| `admin_deployment.py`, `known_good.py`, `releases.py` | Admin deployment files, known-good history, release channels |
| `admin_lifecycle.py` | Transactional Admin install, rollback and repair |
| `packages.py` | OS update state, installation and package-manager recovery |
| `network.py` | Network overview, WLAN with automatic revert, hostname |
| `ssh_service.py`, `sshkeys.py`, `backup_access.py` | SSH service, public keys, read-only backup access |
| `status.py`, `support_archive.py` | Fault-isolated status collection, bounded logs, support archive |
| `agent.py`, `agent_client.py`, `services.py` | The privileged agent, its client and the service graph |
| `auth.py`, `web.py`, `static/` | Authentication and the unprivileged web interface |
| `cli.py` | The `ems-appliance` host CLI |

## Operation model

Every mutating action is a durable operation record, not a request or a thread:

```text
planned → awaiting_confirmation → running → verifying → succeeded
                                        ↘ failed_recoverable
                                        ↘ rolling_back → rolled_back
                                        ↘ failed_terminal
                              ↘ cancelled
```

- Only one conflicting host mutation runs at a time; read-only status calls stay
  available throughout.
- A browser reload restores the current progress from the record.
- An agent restart turns an interrupted `running` operation into a visible
  `failed_recoverable`, and expires an unconfirmed plan, so the lock is never
  stuck.
- Confirmation and cancellation require the operation ID; execution
  additionally requires the confirmation token issued with that plan.
- Terminal results stay visible until they are acknowledged.

Every mutation follows: **plan → preview → confirmation → execution →
verification → result**.

## Network endpoints

```text
http://ems-solarflow.local:8080   Raspberry Pi Appliance Manager
http://ems-solarflow.local:8090   EMS Admin Console
```

The recovery UI deliberately does not depend on a reverse-proxy container. A
proxy may later add `/system` and `/admin` paths in front of both.

## Related documents

- [installation.md](installation.md) — install, layout and first-run setup
- [admin-recovery.md](admin-recovery.md) — Admin install, rollback and repair
- [os-updates.md](os-updates.md) — OS updates and package recovery
- [ssh-backup-access.md](ssh-backup-access.md) — SSH keys and file backup access
- [network-recovery.md](network-recovery.md) — WLAN, hostname and lockout recovery
- [security-model.md](security-model.md) — the privilege boundary in detail
- [troubleshooting.md](troubleshooting.md) — symptom-driven recovery
