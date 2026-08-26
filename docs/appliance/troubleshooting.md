# Appliance Manager troubleshooting

## Quick answers

| Question | Answer |
|---|---|
| What does the Appliance Manager manage? | The Raspberry Pi host: OS status and updates, reboot/shutdown, network and WLAN, hostname and mDNS, Docker service state, the EMS Admin container lifecycle, SSH access, storage and temperature, host logs, appliance recovery. See [architecture.md](architecture.md). |
| What does the EMS Admin Console manage? | EMS configuration, device discovery, grid meter and inverters, control parameters, EMS runtime state, EMS diagnostics, EMS backup/restore, Guided Setup and Guided Upgrade. |
| How do I recover a failed Admin update? | [admin-recovery.md](admin-recovery.md) — the appliance rolls back automatically; otherwise use Repair or *Install version → Previous known-good*. |
| The appliance will not come up at all | [console-recovery.md](console-recovery.md) — log in as `ems-rescue` at a keyboard, then work down the list. |
| How do I install a specific Admin version? | [admin-recovery.md](admin-recovery.md) — Expert mode, *Exact release tag*. |
| How do I add an SSH key? | [ssh-backup-access.md](ssh-backup-access.md) |
| How do I back up files with rsync? | rsync is not available: the backup account is SFTP-only by design. Use the `sftp` commands in [ssh-backup-access.md](ssh-backup-access.md). |
| How do I install OS updates? | [os-updates.md](os-updates.md) |
| Does my appliance use A/B OS images? | `sudo ems-appliance ab status` — `mode=ab` or `mode=single_slot`. See [ab-os-updates.md](ab-os-updates.md). |
| An OS update booted back into the old slot. Why? | The trial slot did not prove itself, so the one-shot trial boot expired. Nothing was changed and nothing is retried; see below. |
| How do I recover access after a WLAN change? | [network-recovery.md](network-recovery.md) |
| How do I reset the Appliance Manager password locally? | `sudo ems-appliance password-reset` — see [installation.md](installation.md). |

## The Appliance Manager itself is unreachable

```bash
systemctl status ems-appliance-web.service
systemctl status ems-appliance-agent.service
journalctl -u ems-appliance-web -n 200
sudo ems-appliance status
```

If the web service fails to start after a package upgrade:

```bash
sudo ems-appliance rollback-manager
```

## `agent_unavailable` in the browser

The web process cannot reach `/run/ems-appliance-manager/agent.sock`.

**Signing in fails with this too, and that is not a wrong password.** The shared
password is checked by the agent, so while the socket is unreachable the login
page answers `503 agent_unavailable` rather than refusing the password. Restart
the agent; nothing about the password needs changing.

```bash
systemctl status ems-appliance-agent.service
systemctl restart ems-appliance-agent.service
ls -l /run/ems-appliance-manager/
```

The socket must be `srw-rw---- root ems-appliance`, and the web account must be
in the `ems-appliance` group.

## "Security audit degraded" is shown after signing in

Authentication worked, but the appliance could not hand the event to the agent,
so it is **not** in `/var/log/ems-appliance-manager/audit/audit.log`. The banner
names how many events were lost; the appliance refuses to claim an entry it did
not write. You are signed in because the agent answered at login — if it stops
answering afterwards, the next sign-in will fail with `agent_unavailable` until
it is back.

```bash
systemctl status ems-appliance-agent.service
sudo tail -n 20 /var/log/ems-appliance-manager/web/appliance.log   # audit_unavailable
systemctl restart ems-appliance-agent.service
```

The flag stays set until the web service restarts, because a lost entry never
appears in the authoritative trail afterwards.

## The package refused to install

On a live host the package fails rather than report success over a broken
appliance. The message names the step:

| Message | What to do |
|---|---|
| `agent failed to start` / `web service failed to start` | `journalctl -u ems-appliance-agent.service -n 200` |
| `the installation is not usable` | `sudo ems-appliance verify-install` lists which check failed |
| `state migration failed` | `sudo ems-appliance migrate-state` and resolve the reported findings |
| `the read-only SFTP export root could not be configured` | `sudo /usr/lib/ems-appliance-manager/setup-export-root.sh` |

A migration **conflict** is not a failure: both copies were kept, the old one as
`<name>.migrated-conflict`. Compare them and delete the one you do not want.

`unavailable  docker: docker is not installed` is a report, not an error. Docker,
NetworkManager, OpenSSH and `acl` are optional; the features that need them are
shown as unavailable.

## Backup access shows "degraded"

**SSH & Backup Access → Export access** reports what it observes, not what was
intended. `degraded` means an export is missing from the export root, is mounted
read-write, or sshd does not chroot the account.

```bash
sudo systemctl start ems-appliance-export.service
findmnt /srv/ems-appliance-export/config
sshd -T -C user=ems-backup,host=localhost,addr=127.0.0.1 | grep -i chroot
```

`pending` simply means `/opt/ems-solarflow` has no exportable directory yet; the
export root is built as soon as one appears.

## "Sign in to use the Appliance Manager" keeps coming back

Every session was invalidated. That happens after a password change or a
`sudo ems-appliance password-reset`. Sign in again with the new password.

## Login says "too many failed attempts"

Rate limiting engaged after five failures from your address. Wait for the window
to pass (the message states the remaining seconds), or reset the password on the
console.

## An operation is stuck

Open **Overview**; the current operation banner shows the stage and offers
**Cancel**. After an agent restart an interrupted operation appears as
`failed_recoverable` with `operation_interrupted` — acknowledge or cancel it,
then start again.

```bash
sudo ems-appliance operations
```

## "operation_conflict"

Another host mutation is still active. Only one runs at a time. Wait for it, or
cancel it from the operation banner. Read-only pages keep working meanwhile.

## Admin shows "missing" or "unhealthy"

Use **Admin → Repair** for a preview of what is wrong. Typical findings:

| Finding | Action |
|---|---|
| Docker is stopped | Start Docker |
| Container missing | Reinstall the selected version |
| Container stopped | Start Admin |
| Compose file or Admin service missing | Manual: recreate it with `install-admin-console.sh`; the repair reports `manual_action_required`. On an appliance that never had Admin, use **Admin → Install Admin** instead |
| Bind path missing | Recreate the directory after confirmation |
| Port 8090 occupied | Stop the shown process yourself; the appliance never kills it |

## "target_identical"

The requested version is already installed. Tick **Reinstall the same version**
if you want to install it again.

## "image_version_mismatch" / "image_source_mismatch" / "image_labels_missing"

The pulled image is not the release you asked for, or it is not from the
project's source. The install is refused on purpose. Pick another tag; do not
work around the check.

## "digest_unresolved"

The registry did not return a canonical digest for the requested tag, so the
appliance refuses to deploy a mutable reference. Nothing was changed and the
running Admin is untouched. Retry, or pick another tag.

## "digest_pin_failed"

The immutable reference could not be written into the deployment file. The
saved bytes were restored and the running Admin was never stopped. Check that
the compose file exists and is writable.

## "known_good_image_unavailable"

A rollback target is recorded but its image is gone locally and cannot be
pulled. The rollback stops instead of falling back to a tag. Pull the image, or
install a specific version instead.

## "repair_incomplete"

The repair ran but at least one check still reports a problem. The remaining
findings are listed with the result; Expert mode shows the full table.

## "manual_action_required"

Nothing could be repaired automatically — for example a missing compose or
environment file. The listed steps are yours to perform; the operation is not
reported as a success.

## "release_channel_unresolved"

*Latest stable* needs a release index. Either configure `release_index_url` in
`/etc/ems-appliance-manager/appliance.conf` or use Expert mode and enter an
exact release tag.

## "package_lock_held"

Another package manager (`apt`, `unattended-upgrades`) is running. Wait for it
to finish. The lock is never removed by the appliance.

## "dpkg_incomplete"

A previous package operation was interrupted. Expert mode → **Complete pending
package configuration**, then retry the update.

## WLAN change failed

The previous profile is reactivated automatically. See
[network-recovery.md](network-recovery.md) for the console commands and the
Ethernet fallback.

## Collecting information for support

**Diagnostics → Create support archive**. It is bounded, redacted and ships a
manifest of everything it contains. Check the manifest before sharing it.

## Log sources

| Source | Contents |
|---|---|
| `appliance_web` | The web service journal |
| `appliance_agent` | The privileged agent journal |
| `operations` | Per-stage operation progress |
| `audit` | Sensitive actions with user, source IP and result |
| `admin_container` | EMS Admin container output |
| `ems_container` | EMS container output |
| `docker_daemon` | Docker service journal |
| `boot` | Boot warnings |
| `packages` | Package-manager log |

All log output is bounded and redacted before it reaches the browser.

## A/B operating-system updates

### The appliance returned to the previous slot after an update

That is the mechanism working. The trial boot is one-shot: a slot that does not
reach a successful health check never becomes the default, and the next ordinary
boot returns to the previous one with nothing changed.

```bash
sudo ems-appliance ab status --json
```

`last_fallback` names the target slot, the target build and the last health
result. **Nothing is retried automatically.** Acknowledge the fallback, then
plan a new update; the inactive slot is staged again from scratch.

Common causes, in the order worth checking:

```bash
journalctl -u ems-appliance-ab-health.service -b -1
sudo ems-appliance ab verify-persistence
systemctl status ems-appliance-persistence.service
```

### OS updates are greyed out

```bash
sudo ems-appliance ab status
```

- `mode=single_slot` — this is a normal installation. A/B host updates need an
  A/B appliance image; see [installation.md](installation.md). There is no
  in-place conversion.
- `reason=layout_drift` — the signals that identify the slots disagree. The
  `drift` list names each disagreement. A/B mutation stays disabled until it is
  resolved, deliberately: writing to a partition nobody can identify is how an
  appliance gets destroyed.
- `persistence` is not `ok` — the shared partition is not mounted or a shared
  path fell back to the root filesystem. Run `ab verify-persistence` for the
  exact path.

### The trial slot says manual action is required

The firmware booted a slot under tryboot that no pending operation matches, or
the running slot reports a different build than the trial wrote. Nothing is
committed and nothing is guessed.

```bash
sudo ems-appliance ab status --json
sudo systemctl reboot          # returns to the current default slot
```

After the ordinary reboot the appliance is on its known-good slot again. Report
the `ab-status.json` member of a support archive with the issue.

### Recovering a broken active slot

If the active slot boots but a package is broken, Expert mode still offers
package-manager recovery. It is recovery, not the normal update path: a live
package mutation on an image-managed appliance creates slot drift and can
disappear at the next rollback.
