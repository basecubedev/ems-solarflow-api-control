# Appliance — step-by-step guides

The **EMS SolarFlow Appliance** turns a Raspberry Pi into a dedicated box that
runs your energy management and nothing else. You flash one card, plug it in,
and manage it from a browser. There is no shell to learn and no operating
system to maintain by hand.

> **Partly confirmed on physical hardware.** One Raspberry Pi 3B+ has been
> running this image since 2026-08-29: it boots, it grew its root partition to
> the card, the console answers, and it has installed signed Appliance Manager
> packages over HTTPS and stepped back from one. What is *not* settled is the
> part that matters most for daily use — that board does not run EMS, Admin or
> InfluxDB, so nothing is known about a Pi 3's 1 GB of RAM carrying them. No Pi
> 4 or Pi 5 has run it at all. See [what that means](#what-not-confirmed-means).

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
guest, the update mechanism is tested against a real Docker engine, the image
layout is audited — and one Pi 3B+ has been running the image since 2026-08-29.

What that one board has settled:

- the image boots, and the first boot grew the root partition to fill the card
- the agent and the web console come up and answer
- an appliance fetches and installs a signed Appliance Manager package over
  HTTPS from a real network, and steps back from one

What only real hardware can settle and nothing has:

- whether a Raspberry Pi 3's 1 GB of RAM carries Docker, Admin, EMS and
  InfluxDB together — **that board runs none of them**, so this is still
  unmeasured rather than estimated
- whether a Pi 4 or Pi 5 boots the image at all; neither has been tried
- how the system behaves when power is cut mid-update
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
