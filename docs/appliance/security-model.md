# Appliance Manager security model

## The privilege boundary

```text
Browser  ──HTTP──▶  ems-appliance-web.service   (user ems-appliance-web)
                      no root
                      no Docker socket
                      no host command
                      │
                      │ typed JSON over a local Unix socket
                      ▼
                    ems-appliance-agent.service (root)
                      fixed operation allowlist
                      re-validates every field
                      owns the durable operation store
```

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
login success and failure, logout, password change,
admin install / update / rollback / repair,
OS update, package recovery,
SSH enable or disable, SSH key added or removed, all keys revoked,
network change, hostname change, reboot, shutdown
```

Passwords, tokens, WLAN passphrases and full SSH keys are never recorded.

## One mutation at a time

The durable operation store allows exactly one conflicting host mutation.
Read-only status calls stay available. A stale browser cannot start a second
operation: execution requires the operation ID plus the confirmation token of
that plan, and a terminal operation cannot be restarted.

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
