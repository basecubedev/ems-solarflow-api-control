# Appliance — step-by-step guides

The **EMS SolarFlow Appliance** turns a Raspberry Pi into a dedicated box that
runs your energy management and nothing else. You flash one card, plug it in,
and manage it from a browser. There is no shell to learn and no operating
system to maintain by hand.

> **Not confirmed on physical hardware.** The image is built and tested
> automatically, but nobody has yet run one on a Raspberry Pi and reported back.
> See [what that means](#what-not-confirmed-means) below before you rely on it.

## Choose your path

| You want to | Start here |
| --- | --- |
| Put the appliance on a card for the first time | [Flashing the card](install.md) |
| Find the box on your network and log in | [First start](first-start.md) |
| Understand what the main page is telling you | [Overview page](overview.md) |
| Update the operating system | [Updates](updates.md#the-operating-system) |
| Update the Appliance Manager itself | [Updates](updates.md#the-appliance-manager) |
| Move it onto WLAN, or rename it | [Network](network.md) |
| Copy your configuration and data off it | [Backups](backup.md) |
| Something is wrong | [When it stops working](recovery.md) |

## What you need

| | |
| --- | --- |
| **Board** | Raspberry Pi 3, 3B+, 4 or 5. Anything older will not run it |
| **Card** | 16 GB or larger. The image is about 8.25 GiB and grows into whatever is left on the card |
| **Network** | An Ethernet cable **for the first start**. WLAN can only be set up afterwards, from the appliance itself |
| **Power** | The official supply for your board. An underpowered Pi corrupts cards |
| **A second computer** | To write the card and to open the browser |

## What it is, and what it is not

It **is** a complete system: operating system, the EMS containers, an update
mechanism, and a small web interface to drive all of it.

There is one image and one board-specific file per Raspberry Pi model. The
operating system is patched in place by `apt`, the way an ordinary Raspberry Pi
is, so a bad operating-system update is undone by you, at the machine — or by
writing the card again and restoring a backup. That is the one thing worth
knowing before you start, and it is why [Backups](backup.md) comes before
anything goes wrong rather than after.

It is **not** a way to run other software. A package you install by hand
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

- whether the image boots at all on a board — it has not
- whether the first boot grows the root partition to fill a real card
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
| **Board and storage** | which Pi, how much RAM, and whether you booted from SD, USB SSD or NVMe |
| **Image** | the file name you flashed, and its `.sha256` |
| **How far it got** | no LED, LED but never on the network, web page reached, or fully working |
| **If it worked** | say so — a plain "Pi 5, NVMe, came up in three minutes" is the report that moves this to a supported tier |
| **If it did not** | the serial capture, and the three FAT files described under [read the card](recovery.md#read-the-card-on-your-computer) |
| **If it worked and then an update failed** | the **Support archive** from the appliance itself; it redacts secrets and carries the package and Appliance Manager state |

Open it as a
[compatibility report](../supported-setups.md#help-improve-compatibility).

## Related

- [Appliance architecture](../../appliance/architecture.md) — for maintainers
- [Installing the manager on your own Pi OS](../../appliance/installation.md)
- [Admin Console guides](../admin/index.md) — the EMS management UI that runs
  *on* the appliance
