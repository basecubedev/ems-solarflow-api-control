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

A name is not an identity, and neither is a device and inode pair. When a
directory is removed the filesystem is free to hand its inode to the very next
creation, so a replacement home can present exactly the pair the record was
written with — on ext4 that is routine. The durable half of the home identity is
therefore a **root-owned ownership marker** the package writes inside the home it
created, carrying a random secret that is also stored in the root-only record:

```text
/var/lib/ems-backup                          root:root, not writable by ems-backup
/var/lib/ems-backup/.ems-appliance-backup-home   root:root 0400, the marker
/var/lib/ems-backup/.ssh                     root:root 0700
```

The record binds the account name, uid, primary gid, home path, the home's
device and inode *and* the marker's secret. All of them have to match before
anything is moved, quarantined, expired or deleted:

| Situation | What purge does |
|---|---|
| the record says this package created the account and its home | removes the account, the home and the managed key files |
| the record says the home already existed | removes the managed key files only |
| the recorded home was replaced, removed or is a symbolic link | touches nothing — the account, the directory and its key material stay, and the mismatch is reported |
| the home carries no marker, a marker naming another home, or another secret | touches nothing and reports the mismatch |
| the account's uid, gid or home path changed | touches nothing and reports the conflict |
| there is no record | removes the managed key files only; the account and its home stay |
| an account exists at install time without a record | the installation fails with a named conflict |

The account identity and the home identity are judged separately, because a
fail-closed step still needs somewhere to act. `disable` follows the same rule
in both implementations, the packaged shell and the Python service: with the
exact package-owned account *and* home the key file is moved out of sshd's reach
and preserved; with the account but a home this package cannot prove is its own,
the key file in that home is not read, moved, renamed or rewritten and the
account is expired instead; without even the account, nothing on the host is
this package's to change. Neither path claims more than it achieved: the
packaged `backup-account.sh disable` fails when the account cannot be expired,
and the service reports `authentication_disabled: false` rather than a
withdrawal that did not happen.

### Records written before the marker existed

A record from an older schema carries no marker, so it cannot prove that the
home it names is the one this package created. Nothing upgrades one by itself.
The fields such a record does carry — `created_by_package`, an account name, a
home path, a device and an inode — are all reproducible by whatever wrote them,
and an inode is reproducible by the filesystem the moment it is handed out
again, so none of them establishes ownership. Until an administrator resolves
it, backup access stays disabled and purge leaves the account and home alone.

`backup-account.sh ownership-state`, mirrored by
`ems-appliance backup-account status`, reports one closed set of states:

| State | Meaning |
|---|---|
| `current` | the record is schema 3 and the marker in the home verifies |
| `legacy_manual_migration_required` | the record predates the marker; an administrator has to decide |
| `ownership_conflict` | the account or the recorded home is not the one the record describes |
| `marker_missing` | the marker the record names is not there |
| `marker_mismatch` | a file is at the marker path but it is not this package's marker |
| `record_corrupt` | the record cannot be read, or carries a schema nothing can interpret |
| `no_ownership_record` | there is no record |

An install never resolves any of these. A **schema-2** record — one written
before the marker but with the account and home identity fields — is adopted
only by the explicit, root-only

```bash
ems-appliance backup-account migrate-ownership
```

which takes no path from anywhere but the record and refuses unless *every* one
of the following is independently proven: the account's uid, gid and home path
still match the record; the recorded home is still that exact directory; nothing
already claims it with a marker; the home is root-owned and closed to other
writers; it holds nothing but `.ssh` and the marker path, and `.ssh` holds
nothing but the key files this package writes; and every key in it is one this
package recorded. It prints what it is about to adopt, writes the marker and the
record as one step, and re-verifies the result afterwards. Device and inode
equality is a necessary condition there and never a sufficient one, and there is
no force-adopt flag.

A **schema-less** record is not adoptable at all, by this command or any other.
It is reported, and an operator reviews the record and the directory by hand.

Anything less than a full proof is left unowned and reported, because adopting
an uncertain home is how a package ends up deleting somebody else's directory.

A key file that appears next to an already preserved one is never discarded:
both are kept and authentication stays disabled until an operator resolves it.
Purge reports every mount and account it could not withdraw instead of claiming
a clean removal.

### ACL entries

The export setup records a versioned manifest of the ACL entries it granted:
one line per object *and* ACL scope, with the object's identity, whether the
entry predated the package, its exact previous permissions and the exact
permissions this package left behind. A recursive grant is expanded during the
ACL walk, because a subtree root cannot say which descendants were changed.

An object identity is not a device and inode either, and it has two halves.

```text
mandatory:  device : inode : file type : uid : gid : generation
optional:                                                       [ : inode version ]
```

The **mandatory** half comes entirely from `stat`, so it is readable wherever
this package runs, and it carries every match. The generation is the
filesystem's birth time when it keeps one and the status-change time otherwise;
type, owner and group are part of it because an ACL entry means something
different on an object whose ownership changed.

The **optional** half is the kernel's inode generation number where it is
exposed (ext4 and relatives) — the one signal that sees a reuse two creations in
the same clock tick hid. It needs `lsattr`, which this package does not depend
on and which no filesystem is obliged to answer, so it only ever *strengthens* a
match or *refuses* one: two generations that disagree describe two objects and
the entry is preserved, while a generation recorded on one host and unreadable
on another leaves the mandatory comparison in charge. Whether `lsattr` is
installed is a fact about the host, not about the object that was granted an
ACL, and a cleanup that became impossible when a tool was removed would leave
this package's own entries behind for good. The manifest header declares which
optional signals the run could read. A mandatory identity that cannot be
reproduced exactly is treated as *unknown*, which preserves the entry.

The whole grant is one transaction with durable, explicit states — `staging`,
`rollback_required`, `rollback_complete`, `recovery_required`, `committed` —
recorded in `acl-transaction.state` beside the manifest. The staging manifest is
created and flushed and the complete pre-state is captured **before** the first
`setfacl`; the read-back, the manifest write, the flush, the atomic rename, the
parent-directory flush and the state commit all have to succeed. The manifest is
authoritative only while the state says `committed`; presence alone is not that
statement, and purge refuses to act on a manifest whose transaction did not
commit.

**The manifest and its transaction state are one authority, restored as one
pair.** A run snapshots both before it stages anything: the manifest bytes with
their hash, file mode and owner, and the exact transaction state beside them.
When a failed run puts the previous manifest back, it puts that state back with
it and re-reads both to prove it — a previously committed manifest left under
`rollback_complete` would be a pair no later purge may act on, so the grants
would stay on the host with nothing allowed to remove them. The restore verifies
content, hash, file type, mode, owner and the state file's content, and a failed
`chmod`, `chown` or flush fails the restore instead of being ignored. The
previous state is only claimed again when the rollback put **every** ACL back;
after an incomplete rollback `recovery_required` stands.

The same applies to a run that failed before it ever renamed a manifest: the
authoritative manifest was untouched, but the transaction state moved to
`staging` when the run opened, so the state is put back and verified there too.

If the parent-directory flush fails after the rename, the previous manifest is
restored and verified by hash, or — when there was none — the slot is emptied.
If neither is possible the new manifest is moved off the authoritative name to
`acl-manifest.tsv.uncommitted` and reported, so nothing reads a manifest
describing grants the rollback withdrew.

The grant, the ownership record and the home marker all name the same
`installation_id`: `backup-account.sh ensure` generates it once and writes it
into the record and the marker, the export setup reads it back out of the record
for the manifest header, and ownership verification refuses a marker that names a
different installation.

A failure at any point after the first `setfacl` restores every entry from the
captured pre-state and verifies the restoration. What could not be put back is
written to a root-only `acl-recovery.tsv` carrying the installation and
operation id, the transaction state, the last step reached, the error, the
opened roots, the expected pre-state and the observed state. Every step of that
write — staging, flush, rename, parent flush — is checked, and a rollback whose
evidence could not be written fails with a message naming both losses rather
than reporting a clean cleanup.

Each export source is opened once and **its descriptor is held for the whole
transaction**. The capture, the mutation, the rollback and the rollback
verification all act on that descriptor, and each object is re-identified
through it immediately before it is restored. A source moved aside mid-run
therefore has its own ACL withdrawn and the directory that took its place at the
configured path is not written to at all. The manifest records the canonical
path an operator recognises; that path is never the mutation authority.

Removal reverts an entry only when the object is still the recorded one and
still carries exactly the granted value — a pre-existing entry is restored to
its previous permissions, an entry this package introduced is removed. An entry
whose permissions changed afterwards, an object that was replaced or re-owned, a
descendant created later through the default ACL and every `setfacl` failure are
preserved and reported as incomplete cleanup. The manifest survives an
incomplete purge so a second attempt stays exact instead of guessing.

## A/B operating-system updates

The block-device write is the most destructive thing this appliance can do, so
its authority is the narrowest.

```text
the browser sends           release_id, and a repair flag
the browser can never send  a device path, a partition identity, a partition number,
                            a URL, a signing key, a checksum, a mount flag,
                            a dd argument or a reboot string,
                            a device layer, a hardware class, a decoder path,
                            an rpi-image-gen source, an expanded output path,
                            a Docker command, a health URL, an SSH key path or
                            a shared persistence path
```

Everything else is derived: the release directory and the signing keyring come
from the root-owned `appliance.conf`, the digests come from a manifest whose
detached signature this appliance's own keyring verified, and the devices come
from layout discovery that cross-checks the firmware, the kernel command line,
the mount table, the block layer and the image-build layout manifest. A
disagreement between those is `layout_drift`, which disables every A/B mutation.

An artifact without a verified signature is refused. A development override
exists for a bench, is reachable only where the root-owned configuration enables
it, and records a distinct verification value that no consumer can read as a
release-gate pass.

Extraction treats a verified archive as still an archive: absolute paths, parent
traversals, nested paths, links, device nodes, unexpected or duplicate members,
oversized members and members that produce more bytes than they declared are all
refused, into a root-owned staging directory, with each member's digest checked
against the manifest before any writer may read it.

A verified member is still not an image. `image-rota` wraps both payloads in an
Android Sparse container, so each member is expanded before anything reaches a
partition, and the manifest signs both identities separately — the encoded
digest extraction checks, and the expanded digest the read-back proves. The
expander is in-process, so there is no decoder executable to allowlist and no
converter output size to trust: every header field, every chunk extent and the
running total are bounded before a byte is produced, and the expanded image must
fit the target partition first. A malformed container fails before any block
device is opened.

Hardware is an authority, not a label. The manifest carries the device layer it
was built from and only the board classes that layer is for; the appliance
normalises its own device tree to a bounded board class and refuses anything
else. A board it cannot identify blocks planning rather than being guessed at.

The confirmed operation record binds the exact physical target — device, both
partitions, both partition identities, both digests, the layout id and the persistent
schema — and that binding is revalidated immediately before the first write. A
recorded target that resolves to the running slot is refused even when the record
says otherwise.

**No feature repartitions a running installation.** The command allowlist
contains no partitioning or filesystem-creation tool, and the only partition
change this project makes at all is growing the persistent partition on a
freshly imaged medium during first boot, before any data exists.

The audit trail carries the plan, the confirmation, the staging, the tryboot
request, the trial boot, a health failure, the commit, an observed fallback and
both rollback steps. It never carries signing material: the detail filter matches
key material as a substring, so `signing_key` is dropped exactly like `key`.

## What is deliberately absent

```text
arbitrary shell execution
in-place conversion of a single-slot installation to A/B
repartitioning a running installation from the browser
EEPROM bootloader firmware-slot writes
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
