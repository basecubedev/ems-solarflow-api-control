# Appliance Manager troubleshooting

## Quick answers

| Question | Answer |
|---|---|
| What does the Appliance Manager manage? | The Raspberry Pi host: OS status and updates, reboot/shutdown, network and WLAN, hostname and mDNS, Docker service state, the EMS Admin container lifecycle, SSH access, storage and temperature, host logs, appliance recovery. See [architecture.md](architecture.md). |
| What does the EMS Admin Console manage? | EMS configuration, device discovery, grid meter and inverters, control parameters, EMS runtime state, EMS diagnostics, EMS backup/restore, Guided Setup and Guided Upgrade. |
| How do I recover a failed Admin update? | [admin-recovery.md](admin-recovery.md) — the appliance rolls back automatically; otherwise use Repair or *Install version → Previous known-good*. |
| How do I install a specific Admin version? | [admin-recovery.md](admin-recovery.md) — Expert mode, *Exact release tag*. |
| How do I add an SSH key? | [ssh-backup-access.md](ssh-backup-access.md) |
| How do I back up files with rsync? | [ssh-backup-access.md](ssh-backup-access.md) |
| How do I install OS updates? | [os-updates.md](os-updates.md) |
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

```bash
systemctl status ems-appliance-agent.service
systemctl restart ems-appliance-agent.service
ls -l /run/ems-appliance-manager/
```

The socket must be `srw-rw---- root ems-appliance`, and the web account must be
in the `ems-appliance` group.

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
| Compose file or Admin service missing | Regenerate the Admin section |
| Bind path missing | Recreate the directory after confirmation |
| Port 8090 occupied | Stop the shown process yourself; the appliance never kills it |

## "target_identical"

The requested version is already installed. Tick **Reinstall the same version**
if you want to install it again.

## "image_version_mismatch" / "image_source_mismatch" / "image_labels_missing"

The pulled image is not the release you asked for, or it is not from the
project's source. The install is refused on purpose. Pick another tag; do not
work around the check.

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
