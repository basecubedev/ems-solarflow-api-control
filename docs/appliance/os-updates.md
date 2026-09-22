# Operating-system updates

Open **System Updates**. The operating system is patched in place with `apt`,
and a major OS generation change means writing the card again.

There is one update path and no mode to be in. What the page adds beside the
package list is the Appliance Manager's own package, which is updated
separately and is the one thing on this appliance that keeps a way back of its
own.

## What the check reports

The update check is read-only: it never modifies a package or a package index.

That has a consequence worth stating plainly: **an empty list is only as old as
the last refresh.** Nothing on this appliance refreshes the package index on a
schedule, so "0 security updates" means "none had been published when somebody
last ran `apt-get update` here" — which may have been weeks ago. A live Pi 3B+
was found reporting an empty list against an index nobody had touched for
twenty-three days.

The index age is shown beside the counts, and an index older than seven days
raises a warning that names the number of days. Refreshing it is the
**Refresh package indexes** action below; it is an operator's decision, because
a status poll that changed the machine it reports on would not be read-only any
more.

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
dpkg run, insufficient free space — and free space that could not be measured
at all, because not knowing how much room there is is not proof that there is
enough. A filesystem with nothing left on it reads as zero, which used to be
indistinguishable from "the probe did not run" and let the update through at
exactly the worst moment: `apt` then dies inside the dpkg transaction, and the
documented repair for a broken package manager here is re-flashing.

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

A repeating timer asks, once a minute, whether dpkg calls the manager
*installed* at the version the install promised, and whether the agent and the
web service are running. Both halves of the first question matter: dpkg reports
a version for a package it unpacked and never configured, and for one it has
only config files left for, and those are the states this exists to catch. That
gate is narrow and is not a functional test of the manager.

| Outcome | What the appliance does |
|---|---|
| The gate passes | The deadline is retired and the install stands. |
| The gate has not passed when the deadline expires | `previous.deb` is installed again, and the console reports *reverted*. |
| There is no `previous.deb` | The console reports *revert unavailable*, and the appliance is left to a person. |
| `dpkg` refuses the previous package too | The next tick tries again, up to five times, and only then does the console report *revert failed*. |

The retries are there because the commonest reason dpkg refuses is a frontend
lock another `apt` run holds — often the operator repairing the package manager
the console just told them to repair. Settling at the first refusal would spend
the only automatic way back on a condition that clears itself a minute later.

The reverter is a copy taken out of the *outgoing* package before anything is
unpacked, so the code deciding keep-or-undo is not code the install brought with
it. It goes back to the archive the deadline kept, checked by digest rather than
by the name of the slot: `previous.deb` is rewritten by every install, so a
deadline that trusted the path alone could be made to reinstall the very package
it was armed to undo.

While a deadline is armed and has not been judged, the appliance refuses a
second install or revert -- the console showed both buttons disabled, but only
the browser was enforcing it, and each acceptance rotated the archive that
deadline would restore out of the way-back slot and overwrote the deadline
itself. Once the window has run out without a verdict both become available
again, which is deliberate: taking them away on a board whose only alternative
is a keyboard would be the failure the deadline exists to prevent.

A revert that one of the two paths without Python performed -- the installer's
own fallback, or the armed reverter -- is folded back into the retained record
before the next install is planned. Neither can amend it, so the record went on
naming the package `dpkg` refused as the current one, and the next update
rotated *that* into the way-back slot.

Execution is bound to the release the plan showed, by digest and by version,
not merely to its release id: the index is read again at confirmation time, and
an asset republished under the same id would otherwise install a different
package than the one that was agreed to.

An install offering the package already current keeps the way back it has rather
than rotating it away — both slots holding one package is a revert that leads
nowhere, and the console stops offering it.

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
