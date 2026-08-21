# Flashing the card

Writing the appliance image onto an SD card. About twenty minutes, most of it
waiting.

> **No appliance image has been published yet.** The steps below are complete
> and the file names are the ones a release will carry, but until the first
> release appears on the
> [Releases page](https://github.com/basecubedev/ems-solarflow-api-control/releases)
> there is nothing to download. If you build your own, the build writes the
> same names into `dist/`.

## Before you start

| | |
| --- | --- |
| **Board** | Raspberry Pi 4 **or** Raspberry Pi 5 — you need the image for *your* board, they are not interchangeable |
| **Card** | 32 GB or larger, and a card reader for your computer |
| **Cable** | Ethernet. The first start needs it; WLAN cannot be configured before the appliance runs |
| **Power** | The official supply for your board |

Everything on the card is erased. There is no undo.

## 1. Download the image for your board

Two files belong together:

| Board | File |
| --- | --- |
| Raspberry Pi 5 | `ems-solarflow-appliance-<version>-rpi5-arm64-ab.img.xz` |
| Raspberry Pi 4 | `ems-solarflow-appliance-<version>-rpi4-arm64-ab.img.xz` |

Download the `.img.xz` **and** the `.sha256` file beside it. The second one is
how you check the first arrived intact.

Not sure which board you have? The Pi 5 has a fan connector next to the USB-C
socket and two camera ports. If in doubt, the model is printed on the board
itself, next to the GPIO pins.

## 2. Check the download

A truncated or corrupted download produces a card that half-boots and fails in
ways that look like broken hardware. This step takes ten seconds.

**Windows** (PowerShell, in the download folder):

```powershell
Get-FileHash ems-solarflow-appliance-*-arm64-ab.img.xz -Algorithm SHA256
Get-Content ems-solarflow-appliance-*-arm64-ab.img.xz.sha256
```

**macOS**:

```bash
shasum -a 256 -c ems-solarflow-appliance-*-arm64-ab.img.xz.sha256
```

**Linux**:

```bash
sha256sum -c ems-solarflow-appliance-*-arm64-ab.img.xz.sha256
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
   **Use custom** — then pick the `.img.xz` you downloaded. Imager unpacks it
   for you; you do not need to extract it first.
4. Under **Storage**, choose your card. *Read this line twice.* Imager lists
   every removable disk, and it will happily erase a backup drive.
5. Press **Write** and confirm. It asks whether to apply OS customisation —
   choose **No**. The appliance configures itself, and Imager's settings do not
   apply to it.
6. Wait. Writing and verifying takes ten to fifteen minutes on a typical card.

When Imager says it is done, eject the card.

> balenaEtcher also works if you already use it. Imager is recommended because
> it handles `.img.xz` directly and is maintained by the board's own vendor.

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
| It was on the network, then vanished | Normal during the first start — it reboots once. Wait two minutes |

More in [When it stops working](recovery.md).
