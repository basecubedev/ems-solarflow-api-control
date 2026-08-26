# Hardware Requirements

Use this page to check whether the machine you have is a good host for EMS. It
covers the computer EMS runs *on*. For the inverters and grid meters EMS talks
*to*, see [supported-setups.md](supported-setups.md).

EMS is small. The usual limit is memory, and the thing that decides how much you
need is whether you want the InfluxDB history database.

## Memory

| RAM | Recommended configuration |
|-----|---------------------------|
| 512 MB | EMS without InfluxDB |
| 1 GB | EMS with InfluxDB |
| >1 GB | Additional headroom |

These are practical sizing figures, not a promise. On a running installation the
three containers were observed at roughly 245 MiB (EMS), 79 MiB (Admin Console)
and 299 MiB (InfluxDB) — about 623 MiB together. Your numbers will differ with
the number of devices, how long the history has been collecting, and what else
the machine is doing.

So read the table as: 512 MB is enough to run and control your system; 1 GB is
enough to also keep history; above 1 GB you have room for the operating system,
Docker and growth rather than running close to the edge.

### Do I need InfluxDB?

No. It stores long-range energy history for the dashboard's analytics. Control
does not depend on it: EMS reads your meter, calculates a target and writes it
to your inverter whether InfluxDB is running or not. On a memory-constrained
machine, leaving it out is a reasonable choice rather than a degraded one.

## Architecture

`arm64` (64-bit ARM) and `amd64` (64-bit x86) are both supported. Every
container EMS deploys is published for both, so there is no emulation and no
32-bit build.

## Storage

Use something you would trust with a database that writes continuously. A decent
SD card is workable; a good USB SSD or NVMe is noticeably better and lasts
longer, especially with InfluxDB enabled.

How much you need depends on which shape you run:

| Shape | Minimum | Why |
|---|---|---|
| Docker deployment | A few GB plus your history | The OS is already there; EMS adds containers and a database |
| A/B appliance image | **32 GB** | Two complete system slots plus a shared data partition. A 16 GB card cannot hold the image at all, and the supported floor is 30 GB. **Nothing checks this for you** — a card between the two flashes and then runs out of room later |
| Single-slot appliance image | **16 GB** | One root filesystem, which grows into whatever is left. The supported floor is 14.5 GB, and nothing on the appliance enforces it either |

## Raspberry Pi compatibility

EMS runs in three shapes, and they do not have the same hardware requirements.

| Shape | What it is | Hardware |
|---|---|---|
| Docker deployment | The normal install — Admin Console or Docker Bootstrap on an existing 64-bit OS | Any 64-bit machine that meets the memory table above |
| A/B appliance image | A prepared image with two system slots, fail-safe host updates and automatic rollback | **Raspberry Pi 4 or Raspberry Pi 5 only** |
| Single-slot appliance image | The same appliance with one writable root, patched by `apt` instead of by whole image | Raspberry Pi 3, 3B+, 4 or 5 |

Both images are the same appliance and the same web interface. The difference is
what happens to the operating system underneath, and it is decided when you
flash: an A/B card can fall back to its previous system by itself, a single-slot
card is patched in place and is recovered by hand.

### Which Raspberry Pi

| Model | Docker deployment | A/B appliance image | Single-slot appliance image |
|---|---|---|---|
| Raspberry Pi 5 | Yes | Yes | Yes |
| Raspberry Pi 4 (2 GB or more recommended) | Yes | Yes | Yes |
| Raspberry Pi 3 / 3B+ | Not tested — see below | **No** | Built for it, never booted on one — see below |
| Raspberry Pi Zero 2 W | Not tested — 512 MB, Wi-Fi only | **No** | **No** — no image is built for it |
| Raspberry Pi 2 and older | No — no 64-bit OS | No | No |

Raspberry Pi OS 64-bit covers the Pi 3, 3B+, 3A+, 4, 400, 5, Zero 2 W and the
Compute Modules, but not the Pi 2 or anything older. "Not tested" above means
exactly that: the board is 64-bit and the containers exist for it, and nobody
has run EMS on one.

### Raspberry Pi 3 and 3B+

**The A/B appliance image does not support the Pi 3 or 3B+, and support is not
planned.** This is a boot-chain limit, not a performance judgement, and there
are three of them:

1. the upstream layer that builds the A/B layout refuses the `pi3` device class
   outright;
2. its first partition carries the boot selector and no second-stage bootloader,
   which is what a Pi 3 boot ROM has to find there;
3. the layout is GPT, and that ROM reads only an MBR.

A Pi 4 or Pi 5 loads its bootloader from EEPROM and needs none of that. There is
no setting that changes any of it.

**The single-slot appliance image is built for it.** That image uses an MBR, and
its boot partition is the ordinary firmware directory a Pi 3 boot ROM knows how
to read, so none of the three reasons above applies. It boots from the SD card
and only from there: the advice above about a USB SSD or an NVMe drive belongs
to the Pi 4 and Pi 5, and no `rpi3` image is built for either. The full reasoning, and the
line between the two answers, is in
[the architecture decision record](../appliance/adr/raspberry-pi-3-ab-support.md).

**It has never been booted on a Pi 3, and it is not listed as supported.** The
image is built and inspected; no board has started it. It is equally not tested
what 1 GB of RAM does with Docker, Admin, EMS and InfluxDB together, or what
100 Mbit/s Ethernet behind USB 2.0 does to a backup. "Built for it" and "known
to work on it" are different claims, and only the first is being made. If you
try it, InfluxDB is the first thing to leave out, and a
[compatibility report](https://github.com/basecubedev/ems-solarflow-api-control/issues)
would be genuinely useful.

## Does A/B need twice the memory?

No. A/B gives you two system slots on the medium, but **only one slot runs at a
time** — the other is a copy sitting on storage. Runtime memory is what one
system needs, not two.

The extra cost is storage, not memory. During an update the appliance does need
more room and more work than usual, because it writes the new slot, prepares
container images and reconstructs containers while the current system keeps
running. Plan the medium for that, not the RAM.

See [A/B OS updates](../appliance/ab-os-updates.md) for how slots, trial boots,
commit and rollback work.

## Related

- [Supported setups](supported-setups.md) — inverters, grid meters and connections
- [Appliance installation](../appliance/installation.md) — the Raspberry Pi appliance
- [A/B OS updates](../appliance/ab-os-updates.md) — slots, rollback and recovery
- [Troubleshooting](troubleshooting.md)
