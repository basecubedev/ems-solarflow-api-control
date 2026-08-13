# Raspberry Pi 3B+ A/B compatibility investigation — 2026-08-14

```
Branch              feat/appliance-manager
Revision at start   07aa2eb6fef530f6c10d4e53790f3cc69fd76a6f
Generator pinned    rpi-image-gen v2.7.0 (a7b6d4806183195f3efadb533f58c8e46393d057)
Image layer         image-rota 5.5.1
Verdict             PI3_AB_NOT_SUPPORTED
```

The question was whether the A/B appliance image can gain a third build profile
for the Raspberry Pi 3B+, which is the only physical Raspberry Pi available for
hardware testing. It cannot. Three independent facts block it, two of them in
the board's silicon, and each was reproduced rather than reasoned about.

The user-facing conclusion is in
[`docs/user/hardware-requirements.md`](../../../docs/user/hardware-requirements.md);
the decision and its consequences are in
[`docs/appliance/adr/raspberry-pi-3-ab-support.md`](../../../docs/appliance/adr/raspberry-pi-3-ab-support.md).
This file is the evidence those two rest on.

## A. `image-rota` refuses the `pi3` device class

Upstream *does* ship a Pi 3 board layer — `device/pi3/device.yaml`, layer name
`rpi3`, `X-Env-Var-class: pi3`, requires `rpi-generic64`. The A/B image layer is
what refuses it, through an enforced variable rule
(`X-Env-VarRequires-Valid: string,regex:^(cm4|pi4|cm5|pi5)$`). Driven against
the pinned checkout with upstream's own validator:

```
$ IGconf_device_class=<class> IGconf_device_storage_type=sd \
    ./rpi-image-gen metadata --validate image/gpt/ab_userdata/image.yaml

[FAIL] IGconf_device_class=pi3    (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
[OK]   IGconf_device_class=pi4    (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
[OK]   IGconf_device_class=pi5    (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
[OK]   IGconf_device_class=cm4    (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
[OK]   IGconf_device_class=cm5    (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
[FAIL] IGconf_device_class=zero2w (required, rule: regex:^(cm4|pi4|cm5|pi5)$)
```

`site/pipeline.py:_validate_resolved` sets `ok = False` on a failed rule, so
this is a build that stops at variable validation, not a warning.

## B. The layout is GPT; the Pi 3 boot ROM reads an MBR

Read from a real `rpi4` image built by this project
(`ems-solarflow-appliance-0.1.0-rpi4-arm64-ab.img`, 17 758 703 616 bytes):

```
MBR entry 0: type=0xee startLBA=1 sectors=34684967
GPT header: EFI PART

p1: bootconfig    32.0 MiB
p2: boot_a       256.0 MiB
p3: boot_b       256.0 MiB
p4: system_a    4096.0 MiB
p5: system_b    4096.0 MiB
p6: persistent  8192.0 MiB
```

The MBR is purely protective — one `0xEE` entry spanning the disk, no FAT
partition. The BCM2837 boot ROM reads the MBR and looks for a FAT partition to
load `bootcode.bin` from, so it finds nothing to boot.

## C. Partition 1 carries no second-stage bootloader

The partition a Pi 3 ROM would have to boot from contains only the A/B selector:

```
BOOTCONF.IG    attr=0x08 size=0      (volume label)
AUTOBOOT.TXT   attr=0x20 size=64
```

Pi 4 and Pi 5 load their bootloader from EEPROM and read `autoboot.txt` only to
learn which partition to boot, so for them this is complete. A Pi 3 has no
EEPROM bootloader and would need `bootcode.bin` and `start.elf` here.

Note that `bootcode.bin` itself is not the obstacle: the current
`raspberrypi/firmware` build does contain `autoboot.txt`, `tryboot_a_b`,
`tryboot` and `boot_partition` strings, so the Pi 3 second-stage bootloader does
appear to implement selector parsing. That was checked precisely because it
would have changed the answer. It does not, because the ROM never gets far
enough to load it (B), and it is not on the partition anyway (C).

## Why the known workaround was refused

A hybrid MBR exposing the FAT boot partition, plus `bootcode.bin` staged on it,
is the standard way to boot a Pi 3 from a GPT medium. It was rejected for
reasons recorded in the ADR, the decisive one being operational rather than
theoretical: the appliance grows its persistent partition to the medium on first
boot using `growpart`, which rewrites the GPT and writes a plain protective MBR.
The hybrid entry that made the board bootable would not survive the appliance's
own first boot.

## The pipeline already refuses a Pi 3 profile

No half-support is left behind. All three release entry points refuse at the
identifier, before a generator is resolved or an output directory is created:

```
appliance-build-rpi-ab-image:  exit=2  build_identifier_invalid: 'rpi3' is not a supported profile (rpi4, rpi5)
appliance-release-gates:       exit=2  build_identifier_invalid: 'rpi3' is not a supported profile (rpi4, rpi5)
appliance-build-rpi-ab-update: exit=2  build_identifier_invalid: 'rpi3' is not a supported profile (rpi4, rpi5)
```

A running Pi 3 is equally fail-closed: `raspberrypi,3-model-b-plus` and
`raspberrypi,3-model-b` resolve to no board class, so `board_is_installable()`
is false and an OS update is refused with `hardware_not_supported` rather than
being offered an image built for another SoC.

## Runtime container availability (checked, not a Pi 3 claim)

Every runtime image this project deploys publishes a `linux/arm64` manifest, so
the container layer is not what excludes any 64-bit board:

| Image | Platforms |
| --- | --- |
| `ghcr.io/basecubedev/ems-solarflow-api-control:v0.7.0` | linux/amd64, linux/arm64 |
| `ghcr.io/basecubedev/ems-solarflow-admin:latest` | linux/amd64, linux/arm64 |
| `influxdb:2.7` | linux/amd64, linux/arm64v8 |

This says nothing about whether EMS runs well on a Pi 3B+. Nobody has run it
there, and the documentation says so rather than inferring it from this table.

## Not run, and why

| Activity | Status | Reason |
| --- | --- | --- |
| Build a real `rpi3` image | NOT RUN | Refused at A. There is no artefact to build. |
| Pi 3 image inspection, slot pairing, update artefact | NOT APPLICABLE | Depends on an image that cannot exist. |
| ARM64 QEMU boot of a Pi 3 image | NOT RUN | No `qemu-system-aarch64` on this host, and no image to boot. |
| Physical Pi 3B+ boot / A/B / power-loss testing | NOT RUN | The board cannot reach a bootloader from this layout (B, C). Flashing it would test nothing and is not a recoverable use of the only test hardware. |
| `rpi4` / `rpi5` rebuild | NOT RUN | No production code changed. This work is tests and documentation. |

The Pi 3B+ hardware-test preparation asked for in the task is therefore not
provided: preparing a flash procedure, recovery medium and serial-console plan
for an image the board provably cannot start would be documentation of a
procedure that cannot succeed.

## Regression protection

`tests/test_appliance_pi3_support.py` holds all of this in place: the device
class rule is evaluated the way a build evaluates it, the first partition's
contents and the GPT choice are read from the pinned upstream configuration, and
the fail-closed board handling is asserted from the real device-tree
`compatible` strings. If upstream widens the rule, the first test fails and says
that the decision needs revisiting.
