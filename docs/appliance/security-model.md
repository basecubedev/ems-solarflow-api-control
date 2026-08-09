# Appliance Manager security model

## The privilege boundary

```text
Browser  ──HTTP──▶  ems-appliance-web.service   (ems-appliance-web:ems-appliance)
                      no root
                      no Docker socket
                      no host command
                      ProtectSystem=strict, writable only under
                        /var/lib/ems-appliance-manager/web
                        /var/log/ems-appliance-manager/web
                      │
                      │ typed JSON over a local Unix socket
                      ▼
                    ems-appliance-agent.service (root:ems-appliance)
                      fixed operation allowlist
                      re-validates every field
                      owns the durable operation store and the audit trail
```

## Socket ownership

One model, declared identically by the unit and by tmpfiles:

```text
/run/ems-appliance-manager        root:ems-appliance 0750
/run/ems-appliance-manager/agent.sock  root:ems-appliance 0660
agent process                     User=root  Group=ems-appliance
web process                       User=ems-appliance-web  Group=ems-appliance
```

The agent's primary group is what makes systemd create the runtime directory
group-owned; a root:root runtime directory would leave the web account unable
to traverse it whatever the tmpfiles rule said. Nothing is world-traversable
or world-readable, and a local user outside the group cannot connect.

**The shared group grants the socket and nothing else.** It is not a read grant
on state.

## State ownership

```text
/var/lib/ems-appliance-manager          root:ems-appliance 0750  (traverse only)
  web/          ems-appliance-web:ems-appliance 0750   authentication, sessions,
                  auth/      0700                      UI preferences
                  sessions/  0700
  agent/        root:root 0700                         operations, known-good,
                                                       compose backups, package
                                                       state, recovery, ssh-keys
/var/log/ems-appliance-manager          root:ems-appliance 0750  (traverse only)
  web/          ems-appliance-web:ems-appliance 0750
  agent/        root:root 0700
  audit/        root:root 0700
```

Files the agent writes are `root:root 0600`; the agent unit runs with
`UMask=0077` so anything it creates outside that list is root-only too.

The web account can neither write, read nor list the agent tree. That matters
because an operation record carries the live confirmation token of its plan and
a known-good file carries the rollback identity: a group-readable record would
hand a compromised web process both. The web unit additionally declares

```text
InaccessiblePaths=-/var/lib/ems-appliance-manager/agent
                  -/var/log/ems-appliance-manager/agent
                  -/var/log/ems-appliance-manager/audit
```

so the paths are not even present in its mount namespace.

Nothing is lost operationally: the same state is served through the typed agent
API — `operations.list`, `operations.get` (which never returns a confirmation
token), `admin.get` for the known-good records, `backup.get` for export state
and `logs.read` for bounded, redacted log output.

The audit trail is written by the agent only; the appliance does not claim
filesystem-level append-only semantics, because none are enforced.

Upgrading from an installation that used the earlier group-readable layout is
handled by the postinst, which re-owns the agent tree to `root:root` before the
services start.

## Agent sandbox

The agent keeps `ProtectHome`, `PrivateTmp`, `RestrictNamespaces`,
`LockPersonality`, `MemoryDenyWriteExecute` and `RestrictRealtime`. Two
directives are deliberately relaxed, each for a verified reason:

| Directive | Decision | Reason |
|---|---|---|
| `RestrictAddressFamilies` | `AF_UNIX AF_INET AF_INET6 AF_NETLINK` | The Admin health check runs on the loopback address, apt fetches repository metadata and the release index is retrieved over HTTPS. With `AF_UNIX` alone all three fail with `[Errno 97] Address family not supported by protocol`. `AF_NETLINK` is required by `ss(8)`: without it the Repair port inspection cannot run at all, and a check that cannot run must not be reported as a result. |
| `RestrictSUIDSGID` | absent | dpkg restores setuid bits while unpacking; with the restriction a package install aborts with `error setting permissions of './usr/bin/chage': Operation not permitted`. |

Every other address family stays blocked — `AF_PACKET`, `AF_BLUETOOTH`,
`AF_VSOCK` and the rest are not reachable from the agent.

### Failing closed

A security boundary that could not be activated is switched off, not merely
labelled. The backup account is the case where this is visible: its
authentication is enabled only after the *effective* sshd policy for that
account has been read back and matched against every restriction the appliance
promises, and it is disabled again when the package that provides the
confinement is removed. The appliance never keeps an account usable while the
UI says "degraded".

The boundary the account depends on is more than the sshd policy: it is the
policy **plus** an export root that contains only the three managed mount
points, each publishing the configured EMS directory read-only, proven from the
kernel's mount table rather than from a report. Any gap disables the
authentication, and a failed export run takes it away through a bounded
`OnFailure` unit.

The same rule governs host paths: a configured root, an export source or an
export target that is not provably a real directory where it claims to be — or
that is reached through a symbolic link in any existing parent — is refused
before any `mkdir`, `chown`, `chmod`, ACL or mount change, rather than exported
with a warning. Recursive ACLs are applied through an open directory handle for
the object that was validated, so a source swapped mid-run cannot receive them.

Package removal follows the same rule in the other direction. Removal stops if
it cannot revoke the backup account's authentication, because completing it
would leave a usable key without the chroot that confined it.

A compromised web process gains exactly the operations on the allowlist, with
values that pass the same validators the agent applies again. It gains no shell,
no path and no image reference.

## What the agent accepts

Every request names an operation from `appliance/protocol.py` and carries typed
fields only. The agent validates:

- the operation name (unknown → `unknown_operation`)
- every field against its declared kind (unknown key → `unknown_field`)
- the target version (`invalid_release_tag`; mutable names such as `latest` are
  not tags)
- the allowed registry and image repository (host configuration, never a request
  field)
- path boundaries (canonical base, symlinks resolved)
- the current operation state (`operation_conflict` for a second mutation)
- the confirmation token issued with that plan
- the requesting local service identity (`SO_PEERCRED`; only `root` and the web
  account are served)

No operation declares a `command`, `argv`, `shell`, `path`, `file`, `image`,
`repository` or `registry` field. That is asserted by a contract test, so it
cannot regress silently.

## Command execution

`appliance/commands.py` is the only place a host process starts.

- Callers name a tool from a fixed allowlist; there is no caller-supplied
  executable and no `PATH` search of an arbitrary name.
- Arguments are a list, `shell=False`, never a single command string.
- Every argument must be a non-empty string without NUL bytes.
- Docker access is treated as root-equivalent and stays behind the agent. The
  Docker socket is never exposed to the web process.

## Authentication and sessions

| Control | Implementation |
|---|---|
| Password hashing | PBKDF2-SHA256, 600 000 iterations, 32-byte salt |
| Minimum length | 12 characters |
| Default password | none — the first start must create one |
| Independence | separate from the EMS Admin password and store |
| Session cookie | `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` behind TLS |
| CSRF | `X-Appliance-CSRF` must match the session token on every mutation; a foreign `Origin` is refused |
| Rate limiting | 5 failures per source address per 5 minutes, then `429` |
| Expiration | idle timeout plus an absolute maximum lifetime |
| Logout | destroys the session |
| Password reset | rotates a generation marker, invalidating **all** sessions |
| Pre-auth exposure | only the login page and whether a password exists |

There is no unauthenticated network password-reset endpoint. Recovery is
`sudo ems-appliance password-reset` on the console or over SSH.

## Response hardening

Every API response carries `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer` and a Content-Security-Policy with
`default-src 'self'` and `frame-ancestors 'none'`. Static assets are an explicit
two-file allowlist; a traversal attempt returns `404`.

## Secret handling

Redaction runs before anything is logged, archived or returned:

- assignment-shaped secrets (`password=`, `token:`, `api_key`, quoted or bare)
- URL user-info credentials
- `Authorization: Bearer|Basic` values
- private-key blocks
- the body of any SSH public key (the type stays, the key material does not)

Log output is bounded by line count and byte size, and dynamic text is written
with `textContent` — never `innerHTML`.

WLAN passphrases live in the agent's memory between plan and apply only. They
are passed to `nmcli` on stdin, never stored in the operation record, never
written to a log and never displayed again.

## Audit logging

Recorded with timestamp, authenticated user, source IP, operation, target,
result and operation ID:

```text
login success and failure, logout, password change, password reset,
admin install / update / rollback / repair,
OS update, package recovery,
SSH enable or disable, SSH key added or removed, all keys revoked,
network change, hostname change, reboot, shutdown
```

Passwords, tokens, WLAN passphrases and full SSH keys are never recorded.

### Who writes the audit log

`/var/log/ems-appliance-manager/audit/audit.log` has exactly one writer: the
privileged agent. The web service owns authentication but not the audit trail,
so it reports an event instead of appending to the file:

```json
{"operation": "audit.record_web_event", "event": "login.failure",
 "result": "failure", "reason": "invalid_password"}
```

`event`, `result` and `reason` are each validated against a fixed set. There is
no free-form action name, no dictionary, no path and no way to pass a password,
a session cookie, a CSRF token or a public key. The operation does not take the
host mutation lock, so a login during a running Admin install is still audited.

`sudo ems-appliance password-reset` runs as root and writes its own
`password.reset` entry directly.

### When the agent is unreachable

Authentication is a recovery path: it must keep working when the agent is down.
If the agent cannot be reached, the appliance does not pretend the event was
recorded. It:

- completes the authentication request normally (no unhandled exception, no
  lockout after the first password);
- writes a bounded `audit_unavailable` warning to the web-owned log at
  `/var/log/ems-appliance-manager/web/appliance.log`;
- reports `security_audit.degraded` on `/api/session`, `/api/settings` and the
  login response, which the UI shows as **Security audit degraded** with the
  number of unrecorded events.

The degraded flag is sticky for the life of the web process, because a lost
entry never reappears in the authoritative trail.

## One mutation at a time

The durable operation store allows exactly one conflicting host mutation.
Read-only status calls stay available. A stale browser cannot start a second
operation: execution requires the operation ID plus the confirmation token of
that plan, and a terminal operation cannot be restarted.

## What the package may delete

A package may not adopt a host account and later delete it. The backup account
is therefore created **and recorded** by this package, in a root-only ownership
record under `/var/lib/ems-appliance-manager/agent/package-state/`. Every
destructive step is gated on it:

| Situation | What purge does |
|---|---|
| the record says this package created the account and its home | removes the account, the home and the managed key files |
| the record says the home already existed | removes the managed key files only |
| there is no record | removes the managed key files only; the account and its home stay |
| an account exists at install time without a record | the installation fails with a named conflict |

A key file that appears next to an already preserved one is never discarded:
both are kept and authentication stays disabled until an operator resolves it.
Purge reports every mount and account it could not withdraw instead of claiming
a clean removal.

## What is deliberately absent

```text
arbitrary shell execution
a browser-based terminal
a general Linux package manager
free-form systemd service editing
manual editing of arbitrary host files
EMS configuration editing
unrestricted Docker container management
unrestricted Docker image execution
an unauthenticated password-reset endpoint
an unauthenticated configuration access point left running
```
