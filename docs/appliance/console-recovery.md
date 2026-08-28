# Getting back in when the appliance will not come up

This document covers the case nothing else does: the appliance boots, or half
boots, and the web console at `http://ems-solarflow.local:8088` does not answer.
For a failed Admin update see [admin-recovery.md](admin-recovery.md); for a WLAN
change that cut you off see [network-recovery.md](network-recovery.md).

## What you have, before you need it

**A rescue account, `ems-rescue`, with the password `rescue-me`.** It is
created by the Appliance Manager package on installation, it can become root
with `sudo`, and it is the account you log in with at a keyboard and monitor or
over a serial console.

Changing that password is **optional**. The appliance runs on a private home
network and this project does not think a forced password change is worth the
friction of an owner who cannot get in. The Admin console reports which state
your appliance is in — *"still the shipped password"* or *"changed"* — and does
not insist.

What it does not do is pretend the trade is free. **The default credentials are
public knowledge**: they are in this document, in the source and in every
published package. Anything that later exposes the appliance beyond that private
network — a port forward, a flat guest VLAN, a compromised device on the same
subnet — is a login for whoever finds it. If any of those describe your network,
change it:

```bash
sudo passwd ems-rescue
```

An upgrade never resets it. The package creates the account if it is missing and
leaves an existing one exactly as it is.

## Order of attempts

Work down this list. Each step assumes the one above it failed.

### 1. The web console

`http://ems-solarflow.local:8088`. It runs outside Docker and outside the EMS
containers, so it answers even when everything it manages is down.

### 2. SSH

Only if you enabled it and added a key. `ems-rescue` is a password account and
the shipped sshd policy refuses it a password — by `Match User ems-rescue`, and
by refusing keyboard-interactive too, which is the path that otherwise still
asks for it. Its password is published in this document, so it is a console
credential and nothing else. This is a key login for whatever account you
configured:

```bash
ssh <your-account>@ems-solarflow.local
```

### 3. A keyboard and a monitor

Log in as `ems-rescue`. This is the first step that works when the network does
not, and the first one that works when the web service will not start.

```bash
sudo ems-appliance status
sudo ems-appliance verify-install
sudo journalctl -u ems-appliance-web -u ems-appliance-agent -b --no-pager
```

If the **Update** and **Revert** controls are both greyed out, a verification
deadline is armed and nothing is judging it — normally
`ems-appliance-manager-verify.timer` does, within its window. Run the check by
hand and it resolves the deadline the same way the timer would, confirming a
healthy manager or putting the previous one back:

```bash
sudo systemctl start ems-appliance-manager-verify.service
sudo /usr/lib/ems-appliance-manager/verify-manager.sh   # if the unit is gone
```

To put back the Appliance Manager package the appliance was running before its
last update:

```bash
sudo ems-appliance rollback-manager
```

Check first whether the appliance already did it. An install through the manager
arms a deadline before `dpkg` runs, and an install that never reports itself
healthy is undone by that deadline on its own:

```bash
systemctl status ems-appliance-manager-verify.timer
cat /var/lib/ems-appliance-manager/agent/packages/verify-verdict.json
```

`confirmed` means the install stands. `reverted` means it was already put back.
`revert_unavailable` or `revert_failed` means the appliance stopped and is
waiting for a person — you. An install done by hand with `dpkg` arms nothing, so
there is no verdict at all and the command above is the only route.

### 4. A serial console

A Pi that does not reach a login prompt shows why only here. Both images already
ship `console=serial0,115200` on the kernel command line and ask the firmware
for the serial line as well, so there is nothing to prepare: connect a 3.3 V
USB-serial adapter to GPIO 14 (TXD), 15 (RXD) and a ground pin, and open it at
115200 baud. On a Raspberry Pi 5 use the dedicated 3-pin UART connector instead
of the GPIO header.

If a card was written by something other than these images, or `cmdline.txt` was
edited, check that the line is still there before concluding the board is dead.

### 5. `init=/bin/sh`

The last resort, and the one that works when nothing on the appliance starts at
all. Take the card out, put it in another machine, and append to the single line
in `cmdline.txt` on the boot partition:

```text
init=/bin/sh
```

Boot it with a keyboard attached. You land at a root shell with the root
filesystem mounted read-only:

```bash
mount -o remount,rw /
# ... repair ...
mount -o remount,ro /
sync
```

Then remove `init=/bin/sh` from `cmdline.txt` again before the next boot.

> On this appliance the root is writable, so the remount above is only needed
> when a filesystem error forced it read-only. The root is
> already writable.

A serial console is a login as well as a log: the getty on that line accepts the
same `ems-rescue` account as a keyboard does. Anyone who can clip an adapter
onto the GPIO header is holding the appliance in their hands, which is the
threshold this account is written for.

### 6. Re-flash

Write the image again and restore a backup. This is the step the rest of this
document exists to avoid, and it is why
[the backup](../user/admin-backup-restore.md) is worth having before you need it.

## What the manager cannot get you out of

`previous.deb` covers the **Appliance Manager package**, and nothing else. It
does not cover:

- **a kernel or firmware that does not boot.** `apt` on this appliance is
  unrestricted — anything an upgrade offers may install, kernel and firmware
  included — and there is nothing to fall back into automatically
  appliance. Recovery is steps 3 to 5 above, and failing those, step 6.
- **the operating system.** It is patched in place. There
  is no OS-level revert.

That is a deliberate decision rather than an oversight, and it is recorded with
its reasoning in
[adr/manager-self-update.md](adr/manager-self-update.md).

## Status

**The procedure above has NOT been executed on physical hardware.** It is
written from the shipped configuration and from what each step does on a
Raspberry Pi, not from a run. When it is executed, the result belongs in
[hardware-validation.md](hardware-validation.md) with the evidence that
document requires — not here.
