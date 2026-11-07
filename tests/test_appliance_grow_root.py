# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the single-slot root really grew before it said so.

The A/B image grows its persistent partition on first boot; the single-slot
image has no persistent partition and grows its root instead. Same transaction,
and it has to be: measure, mutate, verify, and only then write the marker that
stops the appliance ever trying again. A failure or a power cut leaves no
marker, so the next boot retries.

The two properties that are new here, and that these cover:

*It must not touch a medium this project did not write.* The .deb installs onto
somebody else's Raspberry Pi OS, and the installation guide promises that this
appliance never resizes, moves or repartitions a running installation's
storage. The helper therefore acts only on a *positive* statement that this
medium was flashed from a single-slot appliance image.

*The table is an MBR.* The A/B geometry reader was written against a GPT; here
there is none, and the numbers a booted appliance would read come from the
production reader answering against this harness's sysfs tree.

The partitioning tools are substituted: repartitioning a real medium is not
something a unit test may do.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging/appliance/bin/grow-root.sh"

# Never written to: every tool that could touch it is a fake, and the test
# asserts which fakes ran. It has to be a real block device because refusing
# anything else is one of the properties under test.
BLOCK_DEVICE = "/dev/loop0"

requires_block_device = pytest.mark.skipif(
    not Path(BLOCK_DEVICE).exists(), reason=f"{BLOCK_DEVICE} is needed as an inert block device"
)

MEBIBYTE = 1024 * 1024
SECTOR = 512
ALIGNMENT = 2048

# A 32 GB card holding a single-slot image: 8 MiB of alignment, a 256 MiB boot
# partition, and a root the build sized at 8 GiB. The root is the last
# partition, which is what makes growing it possible at all.
DISK_SECTORS = 32 * 1024 * MEBIBYTE // SECTOR
ROOT_START_SECTORS = (8 + 256) * MEBIBYTE // SECTOR
IMAGED_SECTORS = 8 * 1024 * MEBIBYTE // SECTOR


# An MBR addresses 2^32 sectors; cloud-utils growpart stops a dos partition there.
MBR_LIMIT_SECTORS = 2**32

# What mke2fs gives an 8 GiB root with 4 KiB blocks.
BLOCKS_PER_GROUP = 32768
INODE_BLOCKS_PER_GROUP = 512
RESERVED_GDT_BLOCKS = 1024


def grown_sectors(start=ROOT_START_SECTORS, disk=DISK_SECTORS):
    """What growpart would leave: the tail, aligned down to a mebibyte."""

    return disk // ALIGNMENT * ALIGNMENT - start


class Harness:
    """A fake medium: a sysfs tree, and tools that read and rewrite it."""

    def __init__(self, tmp_path, *, disk_sectors=DISK_SECTORS,
                 start_sector=ROOT_START_SECTORS, sectors=IMAGED_SECTORS,
                 filesystem=None, image_layer="image-rpios", disk_name="fakedisk"):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.marker_root = tmp_path / "var-lib"
        self.marker_root.mkdir()
        self.calls = tmp_path / "calls.log"
        self.calls.write_text("")
        self.partition_name = Path(BLOCK_DEVICE).name
        (self.state / "image_layer").write_text(image_layer)

        self.sysfs = tmp_path / "sys"
        self.disk_dir = self.sysfs / "block" / disk_name
        self.partition_dir = self.disk_dir / self.partition_name
        self.partition_dir.mkdir(parents=True)
        (self.disk_dir / "queue").mkdir()
        (self.disk_dir / "queue" / "logical_block_size").write_text("512\n")
        (self.disk_dir / "size").write_text(f"{disk_sectors}\n")
        (self.partition_dir / "partition").write_text("2\n")
        (self.partition_dir / "start").write_text(f"{start_sector}\n")
        (self.partition_dir / "size").write_text(f"{sectors}\n")

        class_block = self.sysfs / "class" / "block"
        class_block.mkdir(parents=True)
        (class_block / self.partition_name).symlink_to(self.partition_dir)
        (class_block / disk_name).symlink_to(self.disk_dir)

        (self.state / "filesystem").write_text(
            str(sectors * SECTOR if filesystem is None else filesystem)
        )
        self._write_tools()

    @property
    def partition_sectors(self):
        return int((self.partition_dir / "size").read_text())

    @property
    def partition_bytes(self):
        return self.partition_sectors * SECTOR

    @property
    def filesystem_bytes(self):
        return int((self.state / "filesystem").read_text())

    def _tool(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> {self.calls}\n{body}\n')
        path.chmod(0o755)

    def _write_tools(self):
        state = self.state
        # The geometry the helper acts on comes from the production reader,
        # against this harness's sysfs tree. A fake that answered "already
        # grown" itself would prove nothing about the calculation under test.
        self._tool(
            "ems-appliance",
            f'if [ "$1" = image-check ]; then\n'
            f"  layer=$(cat {state}/image_layer)\n"
            # The production verb accepts one layer name and refuses every
            # other answer; a stub that echoed whatever it was given would let
            # this harness pass a gate the appliance would not.
            f"  if [ -f {state}/image-check-crashes ]; then\n"
            f'    echo "Traceback (most recent call last):" >&2\n'
            f"    exit 1\n"
            f"  fi\n"
            f'  [ "$layer" = image-rpios ] || exit 3\n'
            f'  echo "$layer"\n'
            f"  exit 0\n"
            f"fi\n"
            f'case "$1" in\n'
            f"  root-geometry)\n"
            f"    device=$(cat {state}/device 2>/dev/null || echo {BLOCK_DEVICE})\n"
            f'    PYTHONPATH={ROOT} python3 -c "\n'
            f"import sys\n"
            f"from pathlib import Path\n"
            f"from appliance import partition_geometry\n"
            # The disk's first sectors, when a case gives it any; otherwise the
            # disk cannot be opened, as for an unprivileged reader.
            f"header = Path(sys.argv[3])\n"
            f"opener = (lambda path: header.open('rb')) if header.exists() else None\n"
            f"try:\n"
            f"    geometry = partition_geometry.read_geometry(\n"
            f"        sys.argv[1], sysfs=sys.argv[2], opener=opener)\n"
            f"except partition_geometry.GeometryError as error:\n"
            f"    sys.exit(error.code)\n"
            f"print(chr(10).join(geometry.to_lines()))\n"
            f'" "$device" {self.sysfs} {state}/disk-header\n'
            f"    ;;\n"
            f"  *) exit 1 ;;\n"
            f"esac",
        )
        # The fields of a real 4 KiB-block ext4 the slack is worked out from.
        self._tool(
            "dumpe2fs",
            f"size=$(cat {state}/filesystem)\n"
            f'echo "Block count:              $((size / 4096))"\n'
            f'echo "Reserved GDT blocks:      {RESERVED_GDT_BLOCKS}"\n'
            f'echo "Block size:               4096"\n'
            f'echo "Group descriptor size:    64"\n'
            f'echo "Blocks per group:         {BLOCKS_PER_GROUP}"\n'
            f'echo "Inode blocks per group:   {INODE_BLOCKS_PER_GROUP}"',
        )
        self._tool(
            "growpart",
            # cloud-utils growpart: 1 is NOCHANGE, 2 is FAILED, and the reason
            # is on its standard output.
            f"if [ -f {state}/growpart-fails ]; then\n"
            f'  echo "FAILED: failed to resize: sfdisk refused the table"; exit 2\n'
            f"fi\n"
            f"if [ -f {state}/growpart-nochange ]; then\n"
            f'  echo "NOCHANGE: partition 2 could only be grown by 33 [fudge=2048]"; exit 1\n'
            f"fi\n"
            f"start=$(cat {self.partition_dir}/start)\n"
            f"disk=$(cat {self.disk_dir}/size)\n"
            f"end=$(( disk / {ALIGNMENT} * {ALIGNMENT} ))\n"
            # On a dos label it stops where an MBR stops addressing.
            f"if [ -f {state}/disk-header ] && [ $end -gt {MBR_LIMIT_SECTORS + 1} ]; then\n"
            f"  end={MBR_LIMIT_SECTORS + 1}\n"
            f"fi\n"
            f"echo $((end - start)) > {self.partition_dir}/size",
        )
        self._tool(
            "resize2fs",
            f"if [ -f {state}/resize2fs-fails ]; then exit 1; fi\n"
            f"sectors=$(cat {self.partition_dir}/size)\n"
            # ext4 drops a trailing part of a group too small for its metadata.
            f"leaves=$(cat {state}/resize2fs-leaves 2>/dev/null || echo 0)\n"
            f"echo $((sectors * {SECTOR} - leaves)) > {state}/filesystem",
        )

    def break_tool(self, name):
        (self.state / f"{name}-fails").write_text("")

    def crash_image_check(self):
        (self.state / "image-check-crashes").write_text("")

    def resize2fs_leaves(self, byte_count):
        (self.state / "resize2fs-leaves").write_text(str(byte_count))

    def label_disk_dos(self):
        """The first sector of an MBR-labelled disk, as the image writes it."""

        sector = bytearray(SECTOR)
        sector[446 + 4] = 0x0C
        sector[446 + 16 + 4] = 0x83
        sector[510:512] = b"\x55\xaa"
        (self.state / "disk-header").write_bytes(bytes(sector))

    def growpart_reports_no_change(self):
        (self.state / "growpart-nochange").write_text("")

    def stall_growpart(self):
        self._tool("growpart", "exit 0")

    def says_image_layer(self, layer):
        (self.state / "image_layer").write_text(layer)

    def hide_the_partition(self):
        (self.state / "device").write_text("/dev/loop-does-not-exist")

    def run(self):
        environment = dict(os.environ)
        environment.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "EMS_GROW_APPLIANCE_BIN": str(self.bin / "ems-appliance"),
            "EMS_GROW_ROOT_STATE": str(self.marker_root),
        })
        return subprocess.run(
            ["sh", str(SCRIPT)], capture_output=True, text=True,
            env=environment, check=False,
        )

    @property
    def marker(self):
        return self.marker_root / ".root-grown"

    def ran(self, name):
        return any(
            line.startswith(f"{name} ") for line in self.calls.read_text().splitlines()
        )


@pytest.fixture
def medium(tmp_path):
    """A 32 GB card whose root is still the image's 8 GiB."""

    return Harness(tmp_path)


# --- the medium this project did not write -----------------------------------


@pytest.mark.parametrize("image_layer", ["", "image-elsewhere"])
def test_a_medium_this_project_did_not_image_is_left_alone(tmp_path, image_layer):
    """The .deb installs onto somebody else's Raspberry Pi OS.

    docs/appliance/installation.md promises that this appliance never resizes,
    moves or repartitions a running installation's storage. Only a positive
    "single" may start a repartition, so an absent marker, an unreadable one
    and one naming the other image all leave the medium untouched.
    """

    host = Harness(tmp_path, image_layer=image_layer)

    result = host.run()

    assert result.returncode == 0
    assert not host.ran("growpart")
    assert not host.ran("resize2fs")
    assert not host.marker.exists()


# --- the transaction ---------------------------------------------------------


@requires_block_device
def test_a_medium_that_grows_is_measured_grown_and_verified(medium):
    result = medium.run()

    assert result.returncode == 0, result.stderr
    assert "RESULT: PASS" in result.stdout
    assert medium.partition_sectors == grown_sectors()
    assert medium.filesystem_bytes == medium.partition_bytes
    assert "outcome=grown" in medium.marker.read_text()


@requires_block_device
def test_a_root_that_already_reaches_the_end_is_not_repartitioned(tmp_path):
    host = Harness(tmp_path, sectors=grown_sectors())

    result = host.run()

    assert result.returncode == 0
    assert not host.ran("growpart")
    assert "outcome=already_filled" in host.marker.read_text()


@requires_block_device
def test_a_failed_growpart_leaves_no_marker(medium):
    medium.break_tool("growpart")

    result = medium.run()

    assert result.returncode == 1
    assert "growpart_failed" in result.stderr
    assert "sfdisk refused the table" in result.stdout + result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_growpart_refusing_an_unused_tail_is_reported_rather_than_marked(medium):
    """The geometry already established that the tail is real."""

    medium.growpart_reports_no_change()

    result = medium.run()

    assert result.returncode == 1
    assert "growpart_refused" in result.stderr
    assert "could only be grown by 33" in result.stdout + result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_a_partition_table_the_kernel_never_reread_is_a_failure(medium):
    """resize2fs would otherwise grow to the size the kernel can still see."""

    medium.stall_growpart()

    result = medium.run()

    assert result.returncode == 1
    assert "partition_table_not_reread" in result.stderr
    assert not medium.ran("resize2fs")
    assert not medium.marker.exists()


@requires_block_device
def test_a_failed_resize2fs_leaves_no_marker(medium):
    medium.break_tool("resize2fs")

    result = medium.run()

    assert result.returncode == 1
    assert "resize2fs_failed" in result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_a_geometry_that_cannot_be_measured_is_a_failure_rather_than_a_silent_skip(medium):
    medium.hide_the_partition()

    result = medium.run()

    assert result.returncode == 1
    assert "root_geometry_unknown" in result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_the_next_boot_retries_and_completes_a_partial_growth(medium):
    """A power cut between growpart and the marker must not be permanent."""

    medium.break_tool("resize2fs")
    assert medium.run().returncode == 1
    assert not medium.marker.exists()

    (medium.state / "resize2fs-fails").unlink()
    result = medium.run()

    assert result.returncode == 0
    assert medium.filesystem_bytes == medium.partition_bytes
    assert medium.marker.exists()


@requires_block_device
def test_a_medium_that_was_already_grown_is_never_touched_again(medium):
    assert medium.run().returncode == 0
    medium.calls.write_text("")

    result = medium.run()

    assert result.returncode == 0
    assert not medium.ran("growpart")
    assert not medium.ran("ems-appliance")


@requires_block_device
def test_the_marker_is_published_by_rename_and_nothing_is_left_staged(medium):
    medium.run()

    assert medium.marker.exists()
    assert not (medium.marker_root / ".root-grown.staged").exists()


@requires_block_device
def test_a_filesystem_ext4_ends_short_of_its_last_group_counts_as_grown(medium):
    """resize2fs drops a trailing piece of a group too small for its metadata.

    With 4 KiB blocks that leaves the filesystem a mebibyte or two short of the
    partition, and a second resize2fs has nothing to do. Calling that a failure
    failed the unit on every boot of a card that was grown.
    """

    medium.resize2fs_leaves(3 * MEBIBYTE // 2)

    result = medium.run()

    assert result.returncode == 0, result.stderr
    assert "outcome=grown" in medium.marker.read_text()


@requires_block_device
def test_a_grown_card_whose_filesystem_ends_short_of_its_last_group_is_marked(tmp_path):
    """The next boot after such a growth: nothing to grow, so mark and stop."""

    host = Harness(
        tmp_path,
        sectors=grown_sectors(),
        filesystem=grown_sectors() * SECTOR - 3 * MEBIBYTE // 2,
    )
    host.resize2fs_leaves(3 * MEBIBYTE // 2)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert not host.ran("growpart")
    assert host.marker.exists()


@requires_block_device
def test_a_filesystem_left_further_short_than_a_group_drop_is_still_a_failure(medium):
    medium.resize2fs_leaves(64 * MEBIBYTE)

    result = medium.run()

    assert result.returncode == 1
    assert "filesystem_not_grown" in result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_a_medium_larger_than_an_mbr_addresses_grows_to_the_mbr_limit(tmp_path):
    """The image is MBR-labelled, and an MBR stops at 2^32 sectors.

    growpart stops the partition there. Counting the rest of a 4 TB disk as
    tail failed the helper before resize2fs on every boot, and the root stayed
    at the image's 8 GiB.
    """

    host = Harness(tmp_path, disk_sectors=4 * 1000**4 // SECTOR)
    host.label_disk_dos()

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.marker.exists()
    assert ROOT_START_SECTORS + host.partition_sectors <= MBR_LIMIT_SECTORS + 1
    assert host.filesystem_bytes == host.partition_bytes


@requires_block_device
def test_an_image_check_that_could_not_run_fails_rather_than_passing(medium):
    """Only "not an appliance image" leaves a medium alone with success.

    A check that crashed answered nothing, so the unit fails and the next boot
    asks again, rather than reporting a card that stays at 8 GiB as fine.
    """

    medium.crash_image_check()

    result = medium.run()

    assert result.returncode == 1
    assert "image_check_failed" in result.stderr
    assert not medium.ran("growpart")
    assert not medium.marker.exists()


# --- what the helper may never do --------------------------------------------


def test_the_helper_never_ignores_the_result_of_a_growth_tool():
    """The defect its A/B twin was written to fix: `|| true` on both tools and
    a marker written either way, which marked an ungrown card as finished."""

    text = SCRIPT.read_text(encoding="utf-8")

    # The one `|| true` here is on the image-check probe, where a failure means
    # "not our medium" and is answered by the check below it. Neither growth
    # tool may be invoked that way.
    for line in text.splitlines():
        if "growpart " in line or "resize2fs " in line:
            assert "|| true" not in line, line
    assert "growpart_failed" in text
    assert "resize2fs_failed" in text


def test_the_helper_asks_for_a_positive_statement_rather_than_an_absence():
    text = SCRIPT.read_text(encoding="utf-8")

    assert '"$APPLIANCE" image-check' in text


# --- the two answers the helper asks the appliance for ------------------------


def test_the_image_check_verb_answers_only_from_a_marker_it_recognises():
    """No marker, an empty field or an unknown layer must all be an error.

    The growth helper turns this answer into a decision to repartition, so
    "this host does not say" and "this host says single-slot" must never reach
    it as the same thing.
    """

    from appliance import image_shape

    base = {"build_id": "20260809120000", "release_version": "0.1.0"}
    for marker, expected in (
        ({**base, "image_layer": "image-rpios"}, True),
        ({**base, "image_layer": "image-rota"}, False),
        (base, False),
        ({**base, "image_layer": ""}, False),
        ({**base, "image_layer": "image-elsewhere"}, False),
    ):
        assert image_shape.marker_is_ours(marker) is expected, marker.get("image_layer")


def test_the_root_geometry_verb_asks_the_block_layer_which_partition_is_root():
    """Not /proc: the mount source is whatever string was passed to mount, and
    on this image that is an alias rather than the kernel name sysfs is keyed
    by."""

    source = (ROOT / "appliance" / "cli.py").read_text(encoding="utf-8")
    body = source.split("def command_root_geometry", 1)[1].split("\ndef ", 1)[0]
    probe = (ROOT / "appliance" / "block_probe.py").read_text(encoding="utf-8")

    assert "root_partition()" in body
    assert "read_geometry" in body
    assert 'item.mountpoint == "/"' in probe


def test_the_image_check_verb_keeps_not_ours_apart_from_a_check_that_could_not_run(monkeypatch):
    """The helper leaves a medium alone with success only on the first answer."""

    from types import SimpleNamespace

    from appliance import cli

    def probe(marker):
        return lambda args: SimpleNamespace(os_build=lambda: marker)

    monkeypatch.setattr(cli, "_block_probe", probe({}))
    assert cli.command_image_check(None) == cli.EXIT_NOT_APPLIANCE_IMAGE
    monkeypatch.setattr(cli, "_block_probe", probe({"image_layer": "image-rpios"}))
    assert cli.command_image_check(None) == cli.EXIT_OK
    monkeypatch.setattr(cli, "_block_probe", lambda args: None)
    assert cli.command_image_check(None) == cli.EXIT_ERROR

    script = SCRIPT.read_text(encoding="utf-8")
    assert f"IMAGE_CHECK_NOT_OURS={cli.EXIT_NOT_APPLIANCE_IMAGE}\n" in script
