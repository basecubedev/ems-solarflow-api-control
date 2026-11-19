# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The appliance's swap: zram first, a swap file behind it, run against stubs.

packaging/appliance/bin/swap-setup.sh is a shell script the units run as root
at boot. Here it runs with /proc and the swap directory moved into a temporary
tree and every tool it calls replaced by a stub that records its arguments, so
what it would do to a board is read back without touching this host's swap.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "appliance" / "bin" / "swap-setup.sh"
SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"


class Board:
    """A fake /proc, a root with a given amount of free space, and recording tools."""

    def __init__(self, tmp_path):
        self.proc = tmp_path / "proc"
        (self.proc / "sys" / "vm").mkdir(parents=True)
        (self.proc / "sys" / "vm" / "swappiness").write_text("60\n")
        (self.proc / "meminfo").write_text("MemTotal:         924804 kB\nMemAvailable:     540000 kB\n")
        self.swaps = []
        self.root = tmp_path / "root" / "var" / "lib"
        self.root.mkdir(parents=True)
        self.swap_dir = self.root / "ems-appliance-swap"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.calls = tmp_path / "calls"
        self.calls.write_text("")
        self.free_mb = 10000
        self.labels = {}
        for name in ("modprobe", "mkswap", "swapon", "swapoff"):
            self._tool(name)
        self._tool("dd", 'for arg; do case "$arg" in of=*) : > "${arg#of=}" ;; esac; done')
        self._tool("zramctl", 'case "$1" in --find) echo /dev/zram0 ;; esac')
        self._tool("fallocate", 'for last; do :; done; : > "$last"')

    def _tool(self, name, body=""):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n{body}\n')
        path.chmod(0o755)

    def run(self, command):
        (self.proc / "swaps").write_text(SWAPS_HEADER + "".join(
            f"{name}                              {kind}\t\t462\t\t0\t\t100\n" for name, kind in self.swaps
        ))
        self._tool("df", f'printf "Avail\\n{self.free_mb}M\\n"')
        environment = dict(os.environ)
        environment.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "EMS_SWAP_PROC": str(self.proc),
            "EMS_SWAP_DIR": str(self.swap_dir),
        })
        return subprocess.run(["sh", str(SCRIPT), command], capture_output=True, text=True, env=environment,
                              check=False)

    def ran(self):
        return self.calls.read_text().splitlines()

    @property
    def swappiness(self):
        return (self.proc / "sys" / "vm" / "swappiness").read_text().strip()


@pytest.fixture
def board(tmp_path):
    return Board(tmp_path)


def test_compressed_swap_takes_half_the_memory_at_the_first_priority(board):
    result = board.run("zram")

    assert result.returncode == 0, result.stderr
    assert board.ran() == [
        "modprobe zram",
        "zramctl --find --size 451M --algorithm zstd",
        "mkswap --label ems-zram /dev/zram0",
        "swapon --priority 100 /dev/zram0",
    ]
    assert board.swappiness == "100"


def test_compressed_swap_that_is_on_is_left_alone(board):
    board.swaps = [("/dev/zram0", "partition")]

    result = board.run("zram")

    assert result.returncode == 0 and board.ran() == [] and board.swappiness == "60"


def test_a_kernel_without_zstd_for_zram_gets_its_default_algorithm(board):
    board._tool("zramctl", 'case "$*" in *--algorithm*) exit 1 ;; --find*) echo /dev/zram1 ;; esac')

    result = board.run("zram")

    assert result.returncode == 0, result.stderr
    assert board.ran()[1:] == [
        "zramctl --find --size 451M --algorithm zstd",
        "zramctl --find --size 451M",
        "mkswap --label ems-zram /dev/zram1",
        "swapon --priority 100 /dev/zram1",
    ]


def test_a_kernel_without_zram_fails_the_unit_and_still_keeps_swap_for_pressure(board):
    board._tool("modprobe", "exit 1")

    result = board.run("zram")

    assert result.returncode == 1 and "no zram" in result.stdout
    assert board.ran() == ["modprobe zram"] and board.swappiness == "100"


def test_a_memory_size_that_cannot_be_read_adds_no_compressed_swap(board):
    (board.proc / "meminfo").write_text("MemAvailable: 1 kB\n")

    result = board.run("zram")

    assert result.returncode == 1 and board.ran() == []


@pytest.mark.parametrize(("free_mb", "size"), [(10000, 2048), (4100, 2048), (4000, 1024), (3000, 512)])
def test_the_swap_file_is_sized_for_the_card_and_keeps_2_gb_clear(board, free_mb, size):
    board.free_mb = free_mb

    result = board.run("file")

    assert result.returncode == 0, result.stderr
    partial = board.swap_dir / "swapfile.partial"
    assert board.ran()[1:] == [
        f"fallocate --length {size}M {partial}",
        f"mkswap --label ems-swapfile {partial}",
        f"swapon --priority 10 {board.swap_dir / 'swapfile'}",
    ]
    assert (board.swap_dir / "swapfile").exists() and not partial.exists()
    assert board.swappiness == "100"
    assert oct((board.swap_dir / "swapfile").stat().st_mode & 0o777) == "0o600"
    assert oct(board.swap_dir.stat().st_mode & 0o777) == "0o700"


def test_a_card_without_room_gets_no_swap_file(board):
    board.free_mb = 2500

    result = board.run("file")

    assert result.returncode == 0 and "no swap file is made" in result.stdout
    assert board.ran() == [f"df --output=avail -BM {board.root}"]
    assert not board.swap_dir.exists()


def test_a_swap_the_owner_set_up_stays_theirs(board):
    board.swaps = [("/dev/zram0", "partition"), ("/dev/sda2", "partition")]

    result = board.run("file")

    assert result.returncode == 0 and "owner" in result.stdout and board.ran() == []


def test_a_swap_file_made_at_an_earlier_boot_is_switched_on_again(board):
    board.swap_dir.mkdir()
    (board.swap_dir / "swapfile").write_text("")

    result = board.run("file")

    assert result.returncode == 0
    assert board.ran() == [f"swapon --priority 10 {board.swap_dir / 'swapfile'}"]


def test_a_swap_file_that_is_on_is_left_alone(board):
    board.swaps = [("/dev/zram0", "partition"), (str(board.swap_dir / "swapfile"), "file")]

    result = board.run("file")

    assert result.returncode == 0 and board.ran() == []


def test_a_filesystem_that_cannot_allocate_gets_the_file_written_out(board):
    board._tool("fallocate", "exit 1")

    result = board.run("file")

    assert result.returncode == 0, result.stderr
    assert f"dd if=/dev/zero of={board.swap_dir / 'swapfile.partial'} bs=1M count=2048 status=none" in board.ran()


def test_an_unknown_command_is_refused(board):
    assert board.run("off").returncode == 2


def test_the_purge_removes_the_directory_the_script_makes():
    """Two copies of one path, in the script and in the postrm that deletes it."""

    import re

    script = SCRIPT.read_text(encoding="utf-8")
    postrm = (ROOT / "packaging" / "appliance" / "debian" / "postrm").read_text(encoding="utf-8")
    made = re.search(r"SWAP_DIR=\$\{EMS_SWAP_DIR:-([^}]+)\}", script).group(1)
    assert re.search(rf"^SWAP_DIR={re.escape(made)}$", postrm, re.MULTILINE)
