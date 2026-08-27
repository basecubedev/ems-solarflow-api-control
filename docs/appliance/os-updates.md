# Operating-system updates

Open **System Updates**. The operating system is patched in place with `apt`,
and a major OS generation change means writing the card again.

There is one update path and no mode to be in. What the page adds beside the
package list is the Appliance Manager's own package, which is updated
separately and is the one thing on this appliance that keeps a way back of its
own.

## What the check reports

The update check is read-only: it never modifies a package or a package index.

| Item | Meaning |
|---|---|
| Security updates | Packages whose candidate comes from a security archive |
| Normal package updates | Everything else that is upgradable |
| Held packages | Packages pinned with `dpkg` hold |
| Kernel update | A `linux-image*` / `raspberrypi-kernel` upgrade is pending |
| Firmware update | A `raspi-firmware` / bootloader / `firmware-*` upgrade is pending |
| Reboot required | `/var/run/reboot-required` exists, with the packages that set it |
| Package-manager health | dpkg consistency and whether another package manager holds the lock |

## Install security updates

Basic mode offers **Install security updates**. Only the packages the appliance
itself parsed out of a simulated apt run are upgraded — the browser never sends
a package name.

Before installing, the plan shows:

```text
01 Free disk space
02 dpkg state
03 apt lock state
04 any appliance operation already running
05 the package summary
06 an explicit confirmation
```

Blockers stop the confirmation: an active package-manager lock, an interrupted
dpkg run, or insufficient free space.

During installation the operation reports its stage, captures bounded output and
prevents a second package operation. Afterwards it runs a dpkg consistency
check, detects the reboot requirement, reports the changed package count and
shows failures explicitly.

## Install all updates (Expert mode)

Expert mode adds **Install all available OS updates**. It uses the same plan,
confirmation and verification path.

## Package-manager recovery (Expert mode)

Three strictly defined actions:

| Action | What it runs |
|---|---|
| Complete pending package configuration | `dpkg --configure -a` |
| Repair package dependencies | `apt-get -y -f install` |
| Refresh package indexes | `apt-get update` |

There are no free-form apt arguments. **A real active package-manager lock is
never removed** — the operation refuses with `package_lock_held` and asks you to
wait for the other package manager to finish.

## Major OS upgrades

Unattended distribution upgrades (for example Bookworm → Trixie) are
deliberately not supported. For a major OS generation change:

1. Create or export an EMS backup (EMS Admin Console).
2. Flash the new supported appliance image.
3. Restore the EMS backup.

## Updating the Appliance Manager itself

The Appliance Manager is the package this console runs from. `apt` does not
offer it, because it is not in any Debian archive this appliance trusts, so it
is updated here and nowhere else.

**System Updates → Appliance Manager** is where it is updated, and only there.

Where the packages come from is
[manager-releases.md](manager-releases.md): each version is published at its own
release tag, and one index — at a tag that never moves — names every version
that was ever published, oldest included. That the old ones stay listed is not
tidiness; it is what makes the paragraph below true.

### What happens, in order

1. The configured index (`manager_index_url` in `appliance.conf`) is fetched.
   Nothing in it is trusted: an entry may name a candidate and three `https`
   URLs, and that is all it is allowed to decide.
2. The manifest and its detached signature are fetched, and the signature is
   verified against the keyring the appliance already ships. One trust anchor,
   root-owned, and never reachable from a request.
3. Only then is the manifest read as an authority: what the package is called,
   how large it is and what it must hash to.
4. The package is downloaded under exactly that declared size and hashed
   against the verified manifest.
5. Everything that can refuse has now refused: signature, digest, architecture,
   and whether that package's manager can read the state already on this
   appliance's disk. Refusals happen here, before dpkg runs, while the code
   deciding is still the code that started.
6. The running package is retained as `previous.deb`.
7. A deadline is armed — see below.
8. `dpkg` runs from its own systemd unit, not from the agent. The package's own
   postinst restarts the agent and the web service, so the console is briefly
   unreachable. That is expected.

### Going backwards is not an error

The same control installs an older package as readily as a newer one, and the
plan says which direction it moves. This is deliberate: reinstalling the
previous package is the whole recovery. Refusing a downgrade would take it
away.

What *is* refused is a package whose manager could not read the state already
written on this appliance — which is the question a version comparison was never
able to answer.

### What happens when it fails

**Doing nothing does not confirm an install here.** An appliance that cannot
answer must end up back where it was, and a deadline is what makes silence mean
that rather than mean consent.

A repeating timer asks, once a minute, whether the package dpkg reports is the
one the install promised and whether the agent and the web service are running.
That gate is narrow and is not a functional test of the manager.

| Outcome | What the appliance does |
|---|---|
| The gate passes | The deadline is retired and the install stands. |
| The gate has not passed when the deadline expires | `previous.deb` is installed again, and the console reports *reverted*. |
| There is no `previous.deb` | The console reports *revert unavailable*, and the appliance is left to a person. |
| `dpkg` refuses the previous package too | The console reports *revert failed*. |

The reverter is a copy taken out of the *outgoing* package before anything is
unpacked, so the code deciding keep-or-undo is not code the install brought with
it.

The deadline is software rather than firmware, and what that is worth is
written down rather than glossed:
[adr/manager-self-update.md](adr/manager-self-update.md).

### What it does not cover

`previous.deb` covers the Appliance Manager. It does not cover the kernel, the
firmware or the operating system — see
[console-recovery.md](console-recovery.md).

## Reboot and shutdown

**Overview → Power** offers *Restart Raspberry Pi* and *Shut down*. Before
either, the plan shows the running host operations, the EMS and Admin state and
warns when a package installation is active. An active package operation blocks
the confirmation. After a reboot request the UI shows a reconnect screen and
checks periodically whether the appliance is reachable again.

## From the console

```bash
sudo ems-appliance status        # includes the security-update count and reboot flag
```
