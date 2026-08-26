# ADR: the Raspberry Pi 3 gets the single-slot image, and no A/B one

- Status: accepted
- Date: 2026-08-14; narrowed 2026-08-26
- Scope: which appliance image a Raspberry Pi 3 is built. Not the Docker
  deployment, not EMS control behaviour.

> **2026-08-26.** The original decision was "no `rpi3` build profile at all".
> It was correct about the A/B image and wider than its own evidence: all three
> findings below are properties of `image-rota`, and the single-slot image is
> built by `image-rpios`, which has none of them. The decision is narrowed
> rather than reversed — the A/B refusal stands unchanged, and the three
> findings are still what holds it in place.

## Context

The Raspberry Pi 3 Model B+ is a 64-bit board — BCM2837B0, Cortex-A53, ARMv8-A —
and it is common, cheap and often the board somebody already has. It is a fair
question whether it should join `rpi4` and `rpi5` as a third build profile, and
"the CPU is arm64" is a genuine argument for asking.

It is not an argument for shipping. What decides the answer is whether a Pi 3
can boot *this image*, and that is a property of the boot chain rather than of
the instruction set.

The A/B layout is not this project's invention. It comes from upstream
`rpi-image-gen`'s `image-rota` layer, pinned in
[`packaging/appliance/image/rpi-image-gen.lock`](../../../packaging/appliance/image/rpi-image-gen.lock)
and explained in [rpi-image-gen-image-rota.md](rpi-image-gen-image-rota.md).
`image-rota` owns the whole GPT, the slot labels, the persistent partition and
the boot selector. A Pi 3 profile would have to work with that layout, not
beside it.

## Decision

**Build a `rpi3` profile for the single-slot image only.** There is a
`rpi3-single.yaml` and there is deliberately no `rpi3-ab.yaml`; the profile
declares `variants = ("single",)` and every build entry point refuses the pair
`rpi3` + `ab` at the identifier, before a generator runs.

Do not work around the constraints below with a hybrid MBR, a patched
`image-rota`, or a second image layout. The reasoning is in
[Why not work around it](#why-not-work-around-it).

## The three findings

Each was reproduced against the pinned upstream release or a real built image,
and each is now held in place by `tests/test_appliance_pi3_support.py`.

### 1. `image-rota` refuses the `pi3` device class

Upstream *does* define a Pi 3 board layer — `device/pi3/device.yaml`, layer name
`rpi3`, device class `pi3`. The refusal comes from the image layer instead. Its
metadata declares:

```text
X-Env-VarRequires: IGconf_device_storage_type,IGconf_device_class
X-Env-VarRequires-Valid: string,regex:^(cm4|pi4|cm5|pi5)$
```

This is enforced, not advisory: `site/pipeline.py` fails the build when a
required variable does not satisfy its rule. Against the pinned v2.7.0 tree:

```text
IGconf_device_class=pi3 → [FAIL] (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
IGconf_device_class=pi4 → [OK]
```

So a `rpi3-ab.yaml` profile would not produce a Pi 3 image. It would produce a
build that stops at variable validation.

### 2. The first partition carries no second-stage bootloader

The layout's first partition, `bootconfig`, holds the A/B selector and nothing
else. On a real `rpi4` image its entire root directory is:

```text
BOOTCONF.IG    (volume label)
AUTOBOOT.TXT   64 bytes
```

For a Pi 4 or Pi 5 that is complete, because those boards load their bootloader
from EEPROM and only read `autoboot.txt` to learn which partition to boot. A
Pi 3 has no EEPROM bootloader: its SoC ROM has to find `bootcode.bin` on the
card. There is none here, so a Pi 3 would stop before any part of this project
ran.

### 3. The layout is GPT, and the Pi 3 boot ROM reads an MBR

`image-rota` builds a GPT (`partition-table-type = "gpt"`). A real image
therefore carries a *protective* MBR — one entry, type `0xEE`, covering the
disk:

```text
MBR entry 0: type=0xee startLBA=1 sectors=34684967
GPT header: EFI PART, 6 partitions
```

The Pi 3 boot ROM reads the MBR and looks for a FAT partition to load
`bootcode.bin` from. A protective MBR presents no FAT partition, so the board
does not reach a bootloader at all.

## Why the single-slot image is a different answer

Each of the three findings is a property of `image-rota`. The same three
questions, asked of `image-rpios` and pinned in the same test module:

| Finding | A/B (`image-rota`) | Single-slot (`image-rpios`) |
|---|---|---|
| Device-class rule | `regex:^(cm4\|pi4\|cm5\|pi5)$`, enforced | no rule on the device class at all |
| First partition | a file list holding only `autoboot.txt` | `mountpoint = "/boot/firmware"` — the whole firmware directory, which is where `bootcode.bin` lives |
| Partition table | `gpt`, so a protective MBR | `mbr`, with the boot partition typed `0xC` |

Upstream's `rpi3` and `rpi4` device layers both require `rpi-generic64`, so this
is the same kernel family rather than a second one to maintain. A Pi 5 requires
`rpi-linux-2712` instead, which is why it cannot be folded in the same way.

None of that is a claim that the image *boots*. It is a claim that the three
reasons it could not have booted are gone. What is unproven stays unproven —
see [Consequences](#consequences).

## Why not work around it

Findings 2 and 3 have a known workaround shape: a *hybrid* MBR that exposes the
FAT boot partition to the ROM alongside the GPT, plus `bootcode.bin` and
`start.elf` staged on it. It is deliberately not taken.

- **It contradicts the pinned layout owner.** `image-rota` would have to be
  patched, and this project verifies its generator checkout against the lock and
  refuses to build against anything else. A locally patched image layout is a
  second partition-table authority — exactly the shape the project rules forbid.
- **The appliance's own first boot would break it.** The persistent partition is
  grown to the medium on first boot with `growpart`, which rewrites the GPT and
  its backup header and writes a plain protective MBR. Any hybrid entry that
  made the board bootable is gone the first time the appliance starts. An
  appliance that boots once and then never again is worse than one that never
  claimed to boot.
- **The benefit does not carry the risk.** This is an appliance that controls
  real power hardware, on a board with 1 GB of RAM and a 100 Mbit/s Ethernet
  port behind USB 2.0. A fragile boot chain is the wrong thing to add for it.

## Consequences

- The release matrix gains one image: `rpi3-single`. It does not gain an
  `rpi3-ab`, and no gate is weakened or skipped to produce the one it does gain.
- A Raspberry Pi 3 now resolves to the board class `pi3`, so an operator is
  told what their appliance is. `board_is_installable()` stays false for it,
  because that question is about an A/B update artefact and there is none — as
  it is also false for a Pi 4 running the single-slot image, which is patched
  by apt and has no update archive at all.
- **The image has never been booted on a Pi 3.** Building it is not evidence
  that it starts, and this project does not treat it as any. What has and has
  not been proven is recorded in
  [../ab-hardware-validation.md](../ab-hardware-validation.md), and nothing
  here may be promoted there without the evidence that document names.
- 1 GB of RAM against Docker, Admin, EMS and InfluxDB is **unmeasured**. The
  memory table says 1 GB suffices for EMS with InfluxDB; nobody has run it on
  this board, and 100 Mbit/s Ethernet behind USB 2.0 is a second unmeasured
  difference.
- The media requirement is not the A/B one. That figure came from two slot
  roots plus a persistent partition; a single-slot image has neither. See
  `appliance/media_sizing.py`.
- If upstream widens `image-rota`'s device-class rule, the first test in
  `tests/test_appliance_pi3_support.py` fails and says so. That is the intended
  trigger to revisit the A/B half of this decision, and it should be revisited
  then rather than assumed still true.
