# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the first-boot growth actually grew anything before it said so.

The helper ran ``growpart ... || true`` and ``resize2fs ... || true`` and then
wrote ``/persistent/.grown`` either way. The marker is what stops the appliance
ever trying again, so a card whose filesystem never grew was permanently marked
as finished: the operator got a persistent partition the size of the image on a
medium several times larger, and nothing ever reported it.

Growth is a transaction now. Everything is measured first, each step has to
succeed and be *observed* to have succeeded — the kernel re-reading the
partition table, the filesystem actually being larger — and only then is the
marker staged, synced and renamed into place. A failure or a power cut leaves
no marker, so the next boot retries.

The second defect these cover is geometric. "Already grown" was
``disk_bytes - partition_bytes <= slack``, and the persistent partition is the
*last* of six: subtracting its size from the disk's counted the entire occupied
prefix as unused tail, so a partition already grown to the end of the medium
looked gigabytes short. The harness therefore models a real six-partition card
through a sysfs tree and lets the production geometry reader answer from it —
the numbers under test are the ones a booted appliance would read.

The partitioning tools are substituted: repartitioning a real medium is not
something a unit test may do. ``test_appliance_grow_persistent_real.py`` runs
the same script against real growpart and resize2fs on a loop device.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging/appliance/bin/grow-persistent.sh"

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

# A 32 GB card holding an image whose six partitions occupy the first 17 GiB.
# The persistent partition is the last one, and every number below is the one a
# real image-rota medium of that size would present.
DISK_SECTORS = 32 * 1024 * MEBIBYTE // SECTOR
PREFIX_SECTORS = 17 * 1024 * MEBIBYTE // SECTOR
IMAGED_SECTORS = 8 * 1024 * MEBIBYTE // SECTOR
GPT_RESERVE_SECTORS = 33


def grown_sectors(start=PREFIX_SECTORS, disk=DISK_SECTORS):
    """What growpart would leave: the tail, aligned down to a mebibyte."""

    end = (disk - GPT_RESERVE_SECTORS) // ALIGNMENT * ALIGNMENT
    return end - start


class Harness:
    """A fake medium: a sysfs tree, and tools that read and rewrite it."""

    def __init__(
        self,
        tmp_path,
        *,
        disk_sectors=DISK_SECTORS,
        start_sector=PREFIX_SECTORS,
        sectors=IMAGED_SECTORS,
        filesystem=None,
        disk_name="fakedisk",
    ):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.persistent = tmp_path / "persistent"
        self.persistent.mkdir()
        self.layout = tmp_path / "ab-layout.json"
        self.layout.write_text("{}")
        self.calls = tmp_path / "calls.log"
        self.calls.write_text("")
        self.disk_name = disk_name
        self.partition_name = Path(BLOCK_DEVICE).name

        self.sysfs = tmp_path / "sys"
        self.disk_dir = self.sysfs / "block" / disk_name
        self.partition_dir = self.disk_dir / self.partition_name
        (self.partition_dir / "..").resolve()
        self.partition_dir.mkdir(parents=True)
        (self.disk_dir / "queue").mkdir()
        (self.disk_dir / "queue" / "logical_block_size").write_text("512\n")
        (self.disk_dir / "size").write_text(f"{disk_sectors}\n")
        (self.partition_dir / "partition").write_text("6\n")
        (self.partition_dir / "start").write_text(f"{start_sector}\n")
        (self.partition_dir / "size").write_text(f"{sectors}\n")

        class_block = self.sysfs / "class" / "block"
        class_block.mkdir(parents=True)
        (class_block / self.partition_name).symlink_to(self.partition_dir)
        (class_block / disk_name).symlink_to(self.disk_dir)

        if filesystem is None:
            filesystem = sectors * SECTOR
        (self.state / "filesystem").write_text(str(filesystem))

        self._write_tools()

    # --- the fake medium -------------------------------------------------

    @property
    def partition_sectors(self):
        return int((self.partition_dir / "size").read_text())

    @property
    def disk_sectors(self):
        return int((self.disk_dir / "size").read_text())

    @property
    def start_sector(self):
        return int((self.partition_dir / "start").read_text())

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
            f'case "$2" in\n'
            f"  persistent-device) echo {BLOCK_DEVICE} ;;\n"
            f"  persistent-geometry)\n"
            f"    device=$(cat {state}/device 2>/dev/null || echo {BLOCK_DEVICE})\n"
            f'    PYTHONPATH={ROOT} python3 -c "\n'
            f"import sys\n"
            f"from appliance import ab_geometry\n"
            f"try:\n"
            f"    geometry = ab_geometry.read_geometry(sys.argv[1], sysfs=sys.argv[2])\n"
            f"except ab_geometry.GeometryError as error:\n"
            f"    sys.exit(error.code)\n"
            f"print(chr(10).join(geometry.to_lines()))\n"
            f'" "$device" {self.sysfs}\n'
            f"    ;;\n"
            f"  *) exit 1 ;;\n"
            f"esac",
        )
        self._tool(
            "dumpe2fs",
            f"size=$(cat {state}/filesystem)\n"
            f'echo "Block size:               4096"\n'
            f'echo "Block count:              $((size / 4096))"',
        )
        self._tool("mountpoint", "exit 0")
        self._tool(
            "growpart",
            f"if [ -f {state}/growpart-fails ]; then exit 1; fi\n"
            f"if [ -f {state}/growpart-nochange ]; then exit 2; fi\n"
            f"start=$(cat {self.partition_dir}/start)\n"
            f"disk=$(cat {self.disk_dir}/size)\n"
            f"end=$(( (disk - {GPT_RESERVE_SECTORS}) / {ALIGNMENT} * {ALIGNMENT} ))\n"
            f"echo $((end - start)) > {self.partition_dir}/size",
        )
        self._tool(
            "resize2fs",
            f"if [ -f {state}/resize2fs-fails ]; then exit 1; fi\n"
            f"sectors=$(cat {self.partition_dir}/size)\n"
            f"echo $((sectors * {SECTOR})) > {state}/filesystem",
        )

    # --- scripting -------------------------------------------------------

    def break_tool(self, name):
        (self.state / f"{name}-fails").write_text("")

    def growpart_reports_no_change(self):
        (self.state / "growpart-nochange").write_text("")

    def stall_growpart(self):
        """growpart exits 0 but the kernel keeps reporting the old size."""

        self._tool("growpart", "exit 0")

    def stall_resize2fs(self):
        self._tool("resize2fs", "exit 0")

    def hide_the_partition(self):
        """The block layer no longer describes the partition at all."""

        (self.state / "device").write_text("/dev/loop-does-not-exist")

    def run(self):
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "EMS_GROW_APPLIANCE_BIN": str(self.bin / "ems-appliance"),
                "EMS_GROW_PERSISTENT_ROOT": str(self.persistent),
                "EMS_GROW_LAYOUT": str(self.layout),
            }
        )
        return subprocess.run(
            ["sh", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    @property
    def marker(self):
        return self.persistent / ".grown"

    def ran(self, name):
        return any(
            line.startswith(f"{name} ") for line in self.calls.read_text().splitlines()
        )


@pytest.fixture
def medium(tmp_path):
    """A 32 GB card whose persistent partition is still the image's 8 GiB."""

    return Harness(tmp_path)


@pytest.fixture
def already_grown(tmp_path):
    """The same card after a growth whose marker a power cut never reached.

    ``disk_bytes - partition_bytes`` is roughly 17 GB here — the occupied
    prefix — so the old check called this medium ungrown on every boot.
    """

    return Harness(tmp_path, sectors=grown_sectors())


@requires_block_device
def test_a_medium_that_grows_is_measured_grown_and_verified(medium):
    result = medium.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert medium.partition_sectors == grown_sectors()
    assert medium.filesystem_bytes == grown_sectors() * SECTOR
    assert "outcome=grown" in medium.marker.read_text()


@requires_block_device
def test_a_partition_that_already_reaches_the_end_is_not_repartitioned(already_grown):
    """The geometry regression: a grown *last* partition must read as grown."""

    result = already_grown.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert not already_grown.ran("growpart")
    assert not already_grown.ran("resize2fs")
    assert "outcome=already_filled" in already_grown.marker.read_text()


@requires_block_device
def test_the_marker_records_the_measured_tail_rather_than_a_difference(already_grown):
    already_grown.run()

    marker = already_grown.marker.read_text()

    # The unused tail is alignment and the GPT backup, not the 17 GiB prefix
    # a subtraction of the two sizes would have reported.
    tail = int([line for line in marker.splitlines() if line.startswith("tail_bytes=")][0][11:])
    assert tail < 2 * MEBIBYTE


@requires_block_device
def test_a_power_cut_between_growpart_and_the_marker_completes_on_the_next_boot(medium):
    """The failure mode the geometry fix exists for.

    growpart succeeds, the power goes before the marker lands, and the next
    boot finds a partition that already reaches the end of the medium. The old
    check called it ungrown, ran growpart again, and got NOCHANGE — for ever.
    Here the second boot never reaches growpart at all.
    """

    medium.break_tool("resize2fs")
    first = medium.run()
    assert first.returncode == 1 and not medium.marker.exists()
    assert medium.partition_sectors == grown_sectors()

    (medium.state / "resize2fs-fails").unlink()
    medium.growpart_reports_no_change()
    second = medium.run()

    assert second.returncode == 0, second.stdout + second.stderr
    assert not second.stderr
    assert medium.filesystem_bytes == grown_sectors() * SECTOR
    assert medium.marker.exists()


@requires_block_device
def test_growpart_refusing_an_unused_tail_is_reported_rather_than_marked(medium):
    """A medium with 14 GiB free that growpart will not take is a defect."""

    medium.growpart_reports_no_change()

    result = medium.run()

    assert result.returncode == 1
    assert "growpart_refused" in result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_a_failed_growpart_leaves_no_marker(medium):
    medium.break_tool("growpart")

    result = medium.run()

    assert result.returncode == 1
    assert "growpart_failed" in result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_a_failed_resize2fs_leaves_no_marker(medium):
    medium.break_tool("resize2fs")

    result = medium.run()

    assert result.returncode == 1
    assert "resize2fs_failed" in result.stderr
    assert not medium.marker.exists()
    # The partition did grow. The marker is about the whole transaction.
    assert medium.partition_sectors == grown_sectors()


@requires_block_device
def test_a_partition_table_the_kernel_never_reread_is_a_failure(medium):
    """resize2fs would otherwise grow the filesystem to the old partition."""

    medium.stall_growpart()

    result = medium.run()

    assert result.returncode == 1
    assert "partition_table_not_reread" in result.stderr
    assert not medium.ran("resize2fs")
    assert not medium.marker.exists()


@requires_block_device
def test_a_filesystem_that_did_not_grow_is_a_failure(medium):
    medium.stall_resize2fs()

    result = medium.run()

    assert result.returncode == 1
    assert "filesystem_not_grown" in result.stderr
    assert not medium.marker.exists()


@requires_block_device
def test_the_next_boot_retries_and_completes_a_partial_growth(medium):
    """The reproduction: a card left half-grown must not stay half-grown."""

    medium.break_tool("resize2fs")
    first = medium.run()
    assert first.returncode == 1 and not medium.marker.exists()

    (medium.state / "resize2fs-fails").unlink()
    second = medium.run()

    assert second.returncode == 0
    assert medium.filesystem_bytes == grown_sectors() * SECTOR
    assert medium.marker.exists()


@requires_block_device
def test_a_medium_that_was_already_grown_is_never_touched_again(medium):
    medium.run()
    calls_before = medium.calls.read_text()

    again = medium.run()

    assert again.returncode == 0
    assert medium.calls.read_text() == calls_before


@requires_block_device
def test_a_geometry_that_cannot_be_measured_is_a_failure_rather_than_a_silent_skip(medium):
    medium.hide_the_partition()

    result = medium.run()

    assert result.returncode == 1
    assert "persistent_geometry_unknown" in result.stderr
    assert not medium.ran("growpart")
    assert not medium.marker.exists()


@requires_block_device
def test_an_unmounted_persistent_partition_is_never_marked(medium):
    medium._tool("mountpoint", "exit 1")

    result = medium.run()

    assert result.returncode == 1
    assert "persistent_not_mounted" in result.stderr
    assert not medium.marker.exists()


def test_a_medium_that_is_not_an_ab_image_is_left_alone(tmp_path):
    harness = Harness(tmp_path)
    harness.layout.unlink()

    result = harness.run()

    assert result.returncode == 0
    assert not harness.ran("growpart")
    assert not harness.marker.exists()


@requires_block_device
def test_the_marker_is_published_by_rename_and_nothing_is_left_staged(medium):
    medium.run()

    assert medium.marker.exists()
    assert not (medium.persistent / ".grown.staged").exists()


def test_the_helper_never_ignores_the_result_of_a_growth_tool():
    text = SCRIPT.read_text(encoding="utf-8")

    executable = [
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    ]

    assert any("growpart " in line for line in executable)
    assert not any("|| true" in line for line in executable)
    assert any(line.strip().startswith("sync ") for line in executable)


def test_the_helper_never_decides_growth_from_a_difference_of_two_sizes():
    """The exact shape of the defect, kept out by a test rather than by care."""

    text = SCRIPT.read_text(encoding="utf-8")

    assert "DISK_BYTES - PARTITION_BYTES" not in text
    assert "TAIL_SLACK" not in text
    assert "fills_disk" in text
