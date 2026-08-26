# When it stops working

Start at the top and work down. Each step is safe to try.

## The web page does not load

**Give it three minutes first.** The appliance reboots once during its first
start, and again after a system update. A page that is missing for two minutes
is usually a box that is coming back.

Then, in order:

1. **Try the address instead of the name.** Look up `ems-solarflow` in your
   router's list of connected devices and open `http://<address>:8088`. If that
   works, the name lookup is the problem, not the appliance — see
   [Network](network.md).
2. **Check the lights.** No activity LED at all means power or the card, not
   software. Try the official power supply; try re-seating the card.
3. **Check the cable.** A different switch port, or a different cable.
4. **Power-cycle it — but shut it down first if you can reach it at all.**
   Pulling power from a running appliance is how cards get corrupted.

If none of that helps: **plug in a keyboard and a screen.** The appliance has a
rescue account for exactly this — user `ems-rescue`, password `ems-rescue` — and
it can become root with `sudo`. That password is written down here and in the
source, so it is public knowledge: fine on a home network, and worth changing
with `sudo passwd ems-rescue` if your appliance can be reached from outside one.
The console reports which of the two it is; it does not insist.

The full order of attempts, down to reading the card in another computer, is in
[the console recovery guide](../../appliance/console-recovery.md).

Two things are also readable without logging in, and between them they usually
say what happened.

### Read the card on your computer

Power the appliance off, take the card out, and put it in your computer. Three
of its six partitions are FAT, so Windows, macOS and Linux all open them without
extra software. You may be asked to format the others — **say no**; that is only
your computer failing to read Linux filesystems, not a damaged card.

| File | What it tells you |
| --- | --- |
| `autoboot.txt` on the small first partition | which system slot the firmware was told to start, and whether a trial boot was pending |
| `cmdline.txt` on a `boot` partition | which root the kernel was asked to find |
| `config.txt` beside it | the board settings the firmware applied |

Nothing there needs interpreting to be useful — copying the three files into an
issue is enough.

### Watch it boot

The appliance narrates its whole start-up on a serial line, from the firmware
onwards, and it does so whether or not the network ever comes up. This is the
only way to see *why* a boot failed rather than that it did.

You need a USB-to-serial adapter for 3.3 V (about €10, sold as "USB TTL" or
"FTDI"). Connect **GND, RX and TX only — never the power pin**, and open the
port at **115200 baud, 8N1**:

| Board | Where | Notes |
| --- | --- | --- |
| Raspberry Pi 5 | the 3-pin **UART** connector next to the power socket | a JST-SH debug cable, the connector the board is designed for |
| Raspberry Pi 4 | GPIO header: GND = pin 6, TXD = pin 8, RXD = pin 10 | the adapter's RX goes to the Pi's TX |

Then power the appliance on and copy everything the terminal prints. A line
beginning `FATAL: AB` is the A/B start-up refusing to guess which system to
start, and it names exactly what it could not find.

You cannot log in this way — there is no account to log in to. The line is for
reading, which is also why leaving an adapter attached grants nobody anything.

If the boot never gets far enough to say anything, what is left is re-flashing
the card, which does not erase your configuration and data. The paths are listed
in
[network recovery](../../appliance/network-recovery.md#whether-you-have-a-shell-at-all).

![The Admin section, where restart and repair are offered](../../assets/screenshots/appliance/appliance-recovery.png)

## The page loads but EMS is not running

The appliance manager and EMS are separate. A working manager page with a
stopped EMS means the box is fine and the application is not.

1. Open the **Admin** section.
2. If Admin is not installed, install it — the page offers a single action.
3. If Admin is installed but unhealthy, press **Restart Admin**.
4. If it stays unhealthy, press **Preview repair**. It inspects the deployment
   and shows what it would change before changing anything.

## An update failed

You are already back where you started — that is what the second slot is for.
The Updates page reports what happened and waits for you to acknowledge it.

Read the reason before retrying. An update that failed because the card is full
or the download was truncated will fail again the same way.

## "The Admin console is replacing itself"

Two things can manage the Admin container, and they refuse to fight. If the
Admin Console is in the middle of updating itself, the appliance declines to
touch it and says which stage it reached.

Wait for it to finish. If it never does, the message names the file holding that
state and you can remove it.

## The password is lost

There is no email recovery and no reset button in the browser. There is a
console login: sign in as `ems-rescue` at a keyboard and run
`sudo ems-appliance password-reset`.

If that is not available to you either, write the card again. Your EMS
configuration is on the shared area and is **not** erased by re-flashing — but
do take a [backup](backup.md) first if you can still reach the box at all.

## What not to do

- **Do not pull power to "reset" it.** Shut it down from the page.
- **Do not install packages on it by hand.** The system area is read-only, and
  anything that did stick would disappear at the next update.
- **Do not run a second controller against the same inverter.** Two things
  writing an output limit is worse than either alone.

## Getting help

Open an issue with: what you were doing, what the page said, and the model of
your Pi. If the appliance is reachable, its **Support archive** collects the
relevant logs and state with secrets redacted, and attaching that answers most
questions in one round.
