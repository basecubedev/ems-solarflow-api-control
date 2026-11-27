# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checkout's own ``data/`` refuses a test's writes (``tests/checkout_data_guard.py``).

Each test points the guard at a folder of its own and works from its parent,
so nothing here touches the real ``data/``, and takes what the guard
remembered before it ends. The two runs at the end start pytest on a probe
file against the real ``data/``; the guard refuses their writes before they
happen.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests import checkout_data_guard, conftest

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]

WRITES = {
    "open": lambda: open("data/marker.json", "w"),
    "os.open": lambda: os.open("data/marker.json", os.O_WRONLY | os.O_CREAT, 0o600),
    "mkdir": lambda: os.mkdir("data/state"),
    "makedirs": lambda: os.makedirs("data/state/deeper"),
    "rename out": lambda: os.rename("data/kept.json", "moved.json"),
    "replace in": lambda: os.replace("elsewhere.json", "data/kept.json"),
    "link": lambda: os.link("elsewhere.json", "data/linked.json"),
    "symlink": lambda: os.symlink("../elsewhere.json", "data/pointer.json"),
    "remove": lambda: os.remove("data/kept.json"),
    "rmdir": lambda: os.rmdir("data/sub"),
    "rmtree": lambda: shutil.rmtree("data/sub"),
    "truncate": lambda: os.truncate("data/kept.json", 0),
    "chmod": lambda: os.chmod("data/kept.json", 0o600),
    "chown": lambda: os.chown("data/kept.json", os.getuid(), os.getgid()),
    "utime": lambda: os.utime("data/kept.json"),
    "sqlite": lambda: sqlite3.connect("data/ems_state.sqlite"),
}

SWALLOWED_IN_A_TEST = """
def test_swallows_the_refusal():
    try:
        open({target!r}, "w")
    except Exception:
        pass
"""

SWALLOWED_IN_A_TEARDOWN = """
import pytest


@pytest.fixture(scope="module")
def leaves_a_file_behind():
    yield
    try:
        open({target!r}, "w")
    except Exception:
        pass


def test_uses_it(leaves_a_file_behind):
    pass
"""


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "kept.json").write_text("{}")
    (folder / "sub").mkdir()
    (tmp_path / "elsewhere.json").write_text("{}")
    with monkeypatch.context() as patch:
        patch.setattr(checkout_data_guard, "ROOT", os.path.realpath(folder))
        patch.chdir(tmp_path)
        yield folder
    checkout_data_guard.take()


def test_a_write_into_the_folder_is_refused_before_it_happens(guarded):
    with pytest.raises(checkout_data_guard.CheckoutDataWrite):
        open(guarded / "marker.json", "w")

    assert not (guarded / "marker.json").exists()
    assert checkout_data_guard.take() == [("open", str(guarded / "marker.json"))]


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_every_kind_of_write_is_refused_by_a_relative_name_too(guarded, write):
    before = sorted(path.name for path in guarded.iterdir())

    with pytest.raises(checkout_data_guard.CheckoutDataWrite):
        write()

    assert sorted(path.name for path in guarded.iterdir()) == before
    assert (guarded / "kept.json").read_text() == "{}"
    assert len(checkout_data_guard.take()) == 1


def test_reading_the_folder_and_writing_beside_it_pass(guarded, tmp_path):
    assert (guarded / "kept.json").read_text() == "{}"
    (tmp_path / "beside.json").write_text("{}")

    assert checkout_data_guard.take() == []


def test_a_link_into_the_folder_may_go_but_not_be_written_through(guarded, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link.json").symlink_to(guarded / "kept.json")
    (outside / "other.json").symlink_to(guarded / "kept.json")

    with pytest.raises(checkout_data_guard.CheckoutDataWrite):
        open(outside / "link.json", "w")
    os.remove(outside / "link.json")
    shutil.rmtree(outside)

    assert (guarded / "kept.json").read_text() == "{}"
    assert [event for event, _ in checkout_data_guard.take()] == ["open"]


def test_a_parent_step_after_a_link_is_taken_through_the_link(guarded, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sublink").symlink_to(guarded / "sub")

    with pytest.raises(checkout_data_guard.CheckoutDataWrite):
        open("outside/sublink/../marker.json", "w")
    with pytest.raises(checkout_data_guard.CheckoutDataWrite):
        os.remove("outside/sublink/../kept.json")

    assert not (guarded / "marker.json").exists()
    assert (guarded / "kept.json").exists()
    assert len(checkout_data_guard.take()) == 2


def test_owner_and_times_of_a_link_into_the_folder_may_change(guarded, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link.json").symlink_to(guarded / "kept.json")

    os.utime(outside / "link.json", follow_symlinks=False)
    os.chown(outside / "link.json", os.getuid(), os.getgid(), follow_symlinks=False)

    assert checkout_data_guard.take() == []


def test_a_folder_named_data_elsewhere_is_removed_freely(guarded, tmp_path):
    """``shutil.rmtree`` removes ``data`` relative to its parent's descriptor, not to the working directory."""

    elsewhere = tmp_path / "other" / "data"
    elsewhere.mkdir(parents=True)
    (elsewhere / "x.json").write_text("{}")

    shutil.rmtree(tmp_path / "other")

    assert not elsewhere.exists()
    assert checkout_data_guard.take() == []


def test_a_refusal_the_code_swallowed_still_fails_the_test(guarded):
    def server_thread():
        try:
            open(guarded / "guided-setup-workflow.json", "w")
        except Exception:
            pass

    worker = threading.Thread(target=server_thread)
    worker.start()
    worker.join()

    with pytest.raises(checkout_data_guard.CheckoutDataWrite, match="guided-setup-workflow.json"):
        conftest._refuse_checkout_data_writes("during this test")
    assert checkout_data_guard.take() == []


@pytest.mark.parametrize(
    ("probe", "reported"),
    [(SWALLOWED_IN_A_TEST, "during this test"), (SWALLOWED_IN_A_TEARDOWN, "after the last test of this process")],
    ids=["test", "module teardown"],
)
def test_a_swallowed_write_into_the_real_folder_fails_the_run(tmp_path, probe, reported):
    target = ROOT / "data" / "guard-probe.json"
    probe_file = tmp_path / "test_probe.py"
    probe_file.write_text(probe.format(target=str(target)))

    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider", "-q", str(probe_file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "CheckoutDataWrite" in result.stdout
    assert f"writes into the checkout's data/ {reported}" in result.stdout
    assert not target.exists()
