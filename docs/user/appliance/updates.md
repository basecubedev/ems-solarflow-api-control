# Updates

Two different things get updated on an appliance, and they behave differently.

| What | Where |
| --- | --- |
| The operating system underneath | **System Updates** |
| The Appliance Manager, the console you are looking at | **System Updates → Appliance Manager** |
| The EMS and Admin containers | the Admin console, not here |

## The operating system

The appliance runs Raspberry Pi OS, and its packages are patched in place by
`apt`. **System Updates** shows what is pending: security updates, other package
updates, whether a kernel or firmware upgrade is among them, whether a reboot is
required afterwards, and whether the package manager is healthy. The check
itself changes nothing.

- **Install security updates** is the basic action. Only the packages the
  appliance itself found are upgraded — your browser never names a package.
- **Install all available OS updates** is the same path in Expert mode, for
  everything that is upgradable rather than only the security archive.

![A plan dialog naming what will be installed, waiting for confirmation](../../assets/screenshots/appliance/appliance-update-plan.png)

Confirm, and the page follows along — a banner names the stage it is in, and it
survives a reload or a closed browser.

![An operation in flight, with a banner naming the stage it has reached](../../assets/screenshots/appliance/appliance-update-running.png)

Kernel and firmware upgrades are **not** held back or singled out for a separate
approval. If `apt` offers one, installing updates installs it. That is
deliberate: an appliance that quietly skips kernel security fixes is worse than
one that occasionally needs you at the machine.

### There is nothing to fall back to

If an update leaves the board unable to start, the way back is a keyboard and
screen at the appliance
([when it stops working](recovery.md#the-web-page-does-not-load)), and failing
that, writing the card again and restoring a backup.

This is the one thing worth understanding about this appliance's updates, and it
is why the backup matters more than the update does: **keep a backup somewhere
other than the card.** [SSH & Backup Access](backup.md) is how you get one off
the box.

A major OS generation change (for example Bookworm → Trixie) is not offered as
an update at all. Back up, flash the newer image, restore.

## The Appliance Manager

The Appliance Manager is the software this console *is*. It is updated on its
own, from **System Updates → Appliance Manager**, and nowhere else — `apt` does
not offer it, because it is not in any package archive.

> Like the rest of the appliance, this has never run on hardware. No appliance
> has fetched and installed a manager package over a real network, and the
> deadline described below has never expired on a board. It is tested in full
> offline; that is not the same claim. See
> [what "not confirmed" means](index.md#what-not-confirmed-means).

Nothing here happens on a schedule. There is no automatic update, no nightly
check that installs something, and no way for a newer version to arrive because
time passed. It moves when you press the button.

### Doing it

1. Open **System Updates** and scroll to **Appliance Manager**.
2. Pick a version and press **Install**. The plan names the version, says
   whether it moves forward or back, and lists everything that could refuse.
3. Confirm. The console goes briefly unreachable while the package is unpacked —
   the update restarts the very services answering your browser. Reload after a
   minute.

Before anything is installed, the appliance fetches the package over HTTPS and
checks it against the signing keyring it ships. An unsigned package, one whose
contents do not match its signed description, one built for another
architecture, or one whose manager could not read the settings already on this
appliance, is refused *before* the install begins.

### Installing an older version is allowed

Deliberately. The same control installs an older package as readily as a newer
one, and the plan tells you which direction it is going.

Reinstalling the previous manager **is** the recovery — refusing to go
backwards would take it away. What is refused instead is a version that could
not read the state already on the disk, which is the question "is this number
bigger" never answered.

### If the new one does not come up

**Doing nothing here does not undo anything** — an installed package stays
installed. So the appliance sets itself a deadline before it unpacks
anything.

Once a minute, it checks whether the version now installed is the one the update
promised, and whether the manager's two services are running. The window is
fifteen minutes, and it survives a reboot inside it — rebooting is exactly what
you would try when a console stops answering, so a deadline a reboot cancelled
would be no deadline at all.

| What happens | What the appliance does |
| --- | --- |
| Those checks pass | The deadline is retired and the new version stays. |
| The deadline expires first | The previous package is installed again, by itself, and the page reports it. |
| There is no previous package to go back to | It says so, and waits for you. A first install has nothing behind it. |
| Even the previous package refuses to install | It says so, and waits for you. |

The undo is a copy taken out of the package being *replaced*, saved before the
new one is unpacked, so the thing deciding whether to keep the update is not
part of the update.

**This is a timer, not a safety net in firmware.** It covers the Appliance
Manager and nothing else: not the kernel, not the firmware, not the operating
system. If a manager update somehow leaves the machine unable to boot, the
deadline never gets to run. The reasoning, and what it does not buy you, is in
[the decision record](../../appliance/adr/manager-self-update.md).

### After a power cut, wait before downloading

The Raspberry Pi has no battery-backed clock. After a cold start it believes it
is somewhere in the past until it reaches a time server. Certificate checks and
signature validity both depend on the time, so a download started too early
fails with errors that mention everything except the clock. The appliance
refuses to start a manager download until the time is confirmed.

## Related

- [When it stops working](recovery.md)
- [SSH & Backup Access](backup.md) — getting a backup off the box
- [Operating-system and manager updates, in technical detail](../../appliance/os-updates.md)
