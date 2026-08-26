# Appliance — step-by-step guides

The **EMS SolarFlow Appliance** turns a Raspberry Pi into a dedicated box that
runs your energy management and nothing else. You flash one card, plug it in,
and manage it from a browser. There is no shell to learn and no operating
system to maintain by hand.

> **Not confirmed on physical hardware.** These images are built and tested
> automatically, but nobody has yet run one on a Raspberry Pi and reported back.
> See [what that means](#what-not-confirmed-means) below before you rely on one.

## Choose your path

| You want to | Start here |
| --- | --- |
| Put the appliance on a card for the first time | [Flashing the card](install.md) |
| Find the box on your network and log in | [First start](first-start.md) |
| Understand what the main page is telling you | [Overview page](overview.md) |
| Update the operating system — a signed image on a two-slot appliance, `apt` on a single-slot one | [Updates](updates.md#the-operating-system) |
| Update the Appliance Manager itself | [Updates](updates.md#the-appliance-manager) |
| Move it onto WLAN, or rename it | [Network](network.md) |
| Copy your configuration and data off it | [Backups](backup.md) |
| Something is wrong | [When it stops working](recovery.md) |

## What you need

| | |
| --- | --- |
| **Board** | Raspberry Pi 4 or Raspberry Pi 5 for the self-repairing two-slot image. A Raspberry Pi 3 or 3B+ can only run the single-slot one; anything older runs neither |
| **Card** | 32 GB or larger for the two-slot image — a 16 GB card cannot hold it at all. 16 GB is enough for the single-slot image |
| **Network** | An Ethernet cable **for the first start**. WLAN can only be set up afterwards, from the appliance itself |
| **Power** | The official supply for your board. An underpowered Pi corrupts cards |
| **A second computer** | To write the card and to open the browser |

## What it is, and what it is not

It **is** a complete system: operating system, the EMS containers, an update
mechanism, and a small web interface to drive all of it.

There are two images, and choosing between them is the one decision you cannot
revisit without flashing again. The **two-slot** image keeps a second copy of
the whole system, so a bad operating-system update falls back by itself; it
needs a Pi 4 or a Pi 5. The **single-slot** image has one root that `apt`
patches in place, which is cheaper on the card and is what a Raspberry Pi 3 is
built — but a bad update there is undone by you, at the machine.
[Flashing the card](install.md) walks through the choice.

It is **not** a way to run other software. The card is managed as a whole. On
the two-slot image a package you install by hand disappears at the next system
update, because the update replaces the whole slot; on the single-slot image it
survives, and it is then yours to maintain and yours to blame when an upgrade
goes sideways.

## What "not confirmed" means

This project uses the same words for the appliance as for its inverter support:

| Word | Meaning |
| --- | --- |
| **Validated** | Confirmed on the maintainer's own hardware |
| **Family-supported** | Shares an exact profile with something Validated |
| **Reverse-engineered** | Built and tested, but never confirmed on the physical device |

The appliance is in the third group. Every part of it is exercised
automatically — the package installs and its services start on a booted 64-bit
guest, the update mechanism is tested against a real Docker engine, the
read-only system layout is audited — but none of that runs on a Raspberry Pi.

Concretely, these are the things only real hardware can settle:

- whether either image boots at all on a board — neither has
- whether the firmware's one-shot trial boot behaves as the update path assumes
- whether a slot boots inside the health window on a real card
- how the system behaves when power is cut mid-update
- whether a Raspberry Pi 3's 1 GB of RAM carries Docker, Admin, EMS and
  InfluxDB together, which is unmeasured rather than estimated
- whether an appliance can fetch and install a signed Appliance Manager package
  over HTTPS from a real network
- SD-card wear over time

### If you are the first

A report from one real board closes most of that list, and it is worth doing
properly, because a boot that fails leaves nothing behind on its own.

**Before you power it on**, if you can: attach a serial adapter and start
capturing. It is the only thing that records a start-up that never reaches the
network, and it is described in
[When it stops working](recovery.md#watch-it-boot). Everything else on this list
can be collected afterwards; that one cannot.

Then, whatever happened:

| | What to include |
| --- | --- |
| **Board and storage** | which Pi, how much RAM, which of the two images, and whether you booted from SD, USB SSD or NVMe |
| **Image** | the file name you flashed, and its `.sha256` |
| **How far it got** | no LED, LED but never on the network, web page reached, or fully working |
| **If it worked** | say so — a plain "Pi 5, NVMe, came up in three minutes" is the report that moves this to a supported tier |
| **If it did not** | the serial capture, and the three FAT files described under [read the card](recovery.md#read-the-card-on-your-computer) |
| **If it worked and then an update failed** | the **Support archive** from the appliance itself; it redacts secrets and carries the package and Appliance Manager state, plus the slot state on a two-slot appliance |

Open it as a
[compatibility report](../supported-setups.md#help-improve-compatibility).

## Related

- [Appliance architecture](../../appliance/architecture.md) — for maintainers
- [Installing the manager on your own Pi OS](../../appliance/installation.md)
- [Admin Console guides](../admin/index.md) — the EMS management UI that runs
  *on* the appliance
