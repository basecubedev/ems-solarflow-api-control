# Flashing the card

Writing the appliance image onto an SD card. About twenty minutes, most of it
waiting.

> **Not confirmed on physical hardware.** The appliance image is derived and
> tested in emulation, not on a Raspberry Pi the maintainer owns — see
> [what that means](index.md#what-not-confirmed-means). This page asks you to
> erase a card, so it is worth knowing before you start.

> **Check the [Releases page](https://github.com/basecubedev/ems-solarflow-api-control/releases)
> before you start.** If no appliance image is listed there yet, there is
> nothing to download; the steps below are complete and the file names are the
> ones a release carries. A local build writes the same names into `dist/`.

## Before you start

| | |
| --- | --- |
| **Board** | Raspberry Pi 3, 3B+, 4 **or** 5 — you need the image for *your* board, they are not interchangeable. A Pi 3 or 3B+ has one image rather than two; see below |
| **Card** | 16 GB or larger, and a card reader for your computer |
| **Cable** | Ethernet. The first start needs it; WLAN cannot be configured before the appliance runs |
| **Power** | The official supply for your board |

Everything on the card is erased. There is no undo.

## 1. Download the image for your board

**Where to get it.** On the project's
[Releases page](https://github.com/basecubedev/ems-solarflow-api-control/releases),
open the newest release and scroll to **Assets** — a collapsed list at the
bottom of the release notes. The image files are there.

Not under **Packages** in the sidebar. That holds the EMS and Admin container
images, which the appliance downloads by itself once it runs; you never fetch
those by hand.

**One file per board.** They are not interchangeable: the kernel and the
firmware differ.

| Board | File |
| --- | --- |
| Raspberry Pi 5 | `ems-solarflow-appliance-<version>-rpi5-arm64.img.xz` |
| Raspberry Pi 4 | `ems-solarflow-appliance-<version>-rpi4-arm64.img.xz` |
| Raspberry Pi 3 / 3B+ | `ems-solarflow-appliance-<version>-rpi3-arm64.img.xz` |

A **Raspberry Pi 3 boots from its SD card and nothing else**: booting from a USB
SSD or an NVMe drive is a Pi 4 and Pi 5 arrangement, and no `rpi3` image is
built for it.

Download the `.img.xz` **and** the `.img.xz.sha256` file beside it. The second
one is how you check the first arrived intact. The download is about 240 MB and
expands to 8.3 GiB on the card. Both Imager and balenaEtcher expand it while
they write, so **do not unpack it yourself.**

Not sure which board you have? The Pi 5 has a fan connector next to the USB-C
socket and two camera ports; a Pi 3 has a full-size HDMI socket and is powered
over micro-USB rather than USB-C. If in doubt, the model is printed on the board
itself, next to the GPIO pins.

## 2. Check the download

A truncated or corrupted download produces a card that half-boots and fails in
ways that look like broken hardware. This step takes ten seconds.

**Windows** (PowerShell, in the download folder):

```powershell
Get-FileHash ems-solarflow-appliance-*.img.xz -Algorithm SHA256
Get-Content ems-solarflow-appliance-*.img.xz.sha256
```

**macOS**:

```bash
shasum -a 256 -c ems-solarflow-appliance-*.img.xz.sha256
```

**Linux**:

```bash
sha256sum -c ems-solarflow-appliance-*.img.xz.sha256
```

macOS and Linux print `OK` when it matches. On Windows, compare the two lines
by eye — the long hex string has to be identical.

**If they do not match**, delete the file and download it again. Do not write a
card from a file that failed this check.

## 3. Write the card

Use **Raspberry Pi Imager**. It is the official tool, it is maintained for
Windows, macOS and Linux, and it verifies what it wrote.

1. Install it from [raspberrypi.com/software](https://www.raspberrypi.com/software/).
2. Put the card in the reader.
3. Open Imager. Under **Operating System**, scroll to the bottom and choose
   **Use custom** — then pick the `.img.xz` you downloaded. Imager expands it
   while it writes; there is nothing to unpack first.
4. Under **Storage**, choose your card. *Read this line twice.* Imager lists
   every removable disk, and it will happily erase a backup drive.
5. Press **Write** and confirm. It asks whether to apply OS customisation —
   choose **No**. The appliance configures itself, and Imager's settings do not
   apply to it.
6. Wait. Writing and verifying takes ten to fifteen minutes on a typical card.

When Imager says it is done, eject the card.

> balenaEtcher also works if you already use it. Both are open source. Imager is
> recommended because it verifies what it wrote and is maintained by the board's
> own vendor.

**If your tool cannot read `.xz`** — some older writers, including
Win32DiskImager, only take a plain `.img` — unpack it first and write the
result. You need 8.3 GiB of free space for an unpacked image.

| | |
| --- | --- |
| Windows | [7-Zip](https://www.7-zip.org/): right-click the file, **7-Zip → Extract Here** |
| macOS, Linux | `xz -d <the file you downloaded>.img.xz` |

Note that the `.sha256` file covers the **compressed** download, so check it
before unpacking — afterwards it no longer matches anything you have.

### On a Linux machine with no desktop

If you have no graphical session, write the card from a shell. There is no
undo and no confirmation prompt: the command overwrites whatever you name,
immediately and completely.

```bash
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,MODEL
```

Find your card by **size and model** — not by the letter, which changes between
plugs. It is the whole disk (`/dev/sdX`, `/dev/mmcblk0`), never a partition
(`/dev/sdX1`). Unmount anything the desktop auto-mounted, then:

```bash
IMG=<the file you downloaded>.img.xz   # the exact names are in the table above
xz -dc "$IMG" | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
sudo sync
```

`dd` does not verify. Read the card back and compare it against the image,
which is what Imager does for you. The card is larger than the image, so the
comparison is bounded by the image's own uncompressed length:

```bash
xz -dc "$IMG" | sudo cmp -n "$(xz --robot --list "$IMG" \
        | awk -F'\t' '$1=="file" {print $5}')" - /dev/sdX
```

Silence means the card matches the image. Any output means it does not — write
it again before you boot it.

## 4. Start the appliance

In this order:

1. Card into the Pi.
2. **Ethernet cable** into the Pi and into your router.
3. Power last.

The first start takes two to three minutes: it grows the storage to fill your
card and sets up its identity. The activity LED flickers throughout. Leave it
alone until it settles.

### Why the cable is not optional

There is no way to put WLAN credentials on the card before the first start. The
appliance has to be reachable over Ethernet first, and you set up WLAN from its
web interface afterwards — see [Network](network.md).

## 5. Find it and log in

Continue with [First start](first-start.md).

## If something went wrong

| What you see | What it usually is |
| --- | --- |
| Imager reports a verification error | A failing card or reader. Try another card |
| No activity LED at all | Power supply, or the card is not seated |
| LED flickers, but nothing on the network after five minutes | The cable, or the switch port. Try another port |
| It was on the network, then vanished | The first start brings services up in stages and the page appears only at the end. Wait two minutes |

More in [When it stops working](recovery.md).
