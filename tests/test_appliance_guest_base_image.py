# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which base image a disposable guest is allowed to boot.

The builder guest runs as root, installs build dependencies, receives the
project source and the pinned generator, and produces the images a release is
cut from. It was booting ``cloud/trixie/latest/debian-13-genericcloud-amd64
.qcow2`` with no digest check at all, and the smoke guest verified a cached
image only when a ``SHA512SUMS`` file happened to be lying next to it — so a
missing checksum file, or a cache entry nobody wrote, was silently accepted.

Both now go through one script and one lock. The digest recorded in the lock is
the authority: fetching the expectation from the same server as the artefact
proves only that the two arrived together. Anything the lock cannot answer is a
failure, never a skipped check.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/appliance-guest-base-image.sh"
LOCK = ROOT / "packaging/appliance/vm/base-images.lock.json"
BUILDER_VM = ROOT / "scripts/appliance-builder-vm.sh"
SMOKE_VM = ROOT / "scripts/appliance-smoke-vm-amd64.sh"

ROLES = ("builder", "smoke-amd64", "guest-arm64")


def lock_data():
    return json.loads(LOCK.read_text(encoding="utf-8"))


def run_helper(*args):
    return subprocess.run(
        ["sh", str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def seeded_cache(tmp_path, content=b"a disposable guest image"):
    cache = tmp_path / "cache"
    cache.mkdir()
    image = cache / "pinned.qcow2"
    image.write_bytes(content)
    return cache, image, hashlib.sha512(content).hexdigest()


def custom_lock(tmp_path, entry):
    lock = tmp_path / "base-images.lock.json"
    lock.write_text(json.dumps({"lock_version": 1, "images": {"builder": entry}}))
    return lock


@pytest.mark.parametrize("role", ROLES)
def test_every_guest_role_is_locked_to_one_immutable_build(role):
    entry = lock_data()["images"][role]

    assert re.fullmatch(r"[0-9a-f]{128}", entry["sha512"])
    assert entry["url"].startswith("https://")
    assert entry["url"].endswith(entry["filename"])
    assert entry["build_id"] and entry["build_id"] != "latest"
    assert "/latest/" not in entry["url"]
    assert entry["build_id"] in entry["filename"]


def test_the_helper_verifies_a_cached_image_before_every_boot(tmp_path):
    cache, image, digest = seeded_cache(tmp_path)
    lock = custom_lock(
        tmp_path,
        {"filename": image.name, "url": "https://example.invalid/pinned.qcow2", "sha512": digest},
    )

    result = run_helper("--role", "builder", "--cache", str(cache), "--lock", str(lock))

    assert result.returncode == 0
    assert result.stdout.strip() == str(image)


def test_a_cached_image_that_is_not_the_locked_one_is_a_failure(tmp_path):
    cache, image, digest = seeded_cache(tmp_path)
    lock = custom_lock(
        tmp_path,
        {"filename": image.name, "url": "https://example.invalid/pinned.qcow2", "sha512": digest},
    )
    image.write_bytes(b"someone else's image")

    result = run_helper("--role", "builder", "--cache", str(cache), "--lock", str(lock))

    assert result.returncode == 1
    assert "base_image_digest_mismatch" in result.stderr
    assert result.stdout.strip() == ""


def test_a_role_with_no_lock_entry_is_a_failure_not_a_skip(tmp_path):
    cache, image, digest = seeded_cache(tmp_path)
    lock = custom_lock(
        tmp_path,
        {"filename": image.name, "url": "https://example.invalid/pinned.qcow2", "sha512": digest},
    )

    result = run_helper("--role", "guest-arm64", "--cache", str(cache), "--lock", str(lock))

    assert result.returncode == 1
    assert "base_image_not_locked" in result.stderr


@pytest.mark.parametrize(
    "entry",
    [
        {"filename": "pinned.qcow2", "url": "https://example.invalid/pinned.qcow2"},
        {"filename": "pinned.qcow2", "url": "https://example.invalid/pinned.qcow2", "sha512": ""},
        {"filename": "pinned.qcow2", "url": "https://example.invalid/pinned.qcow2", "sha512": "ab"},
        {"filename": "pinned.qcow2", "url": "http://example.invalid/pinned.qcow2", "sha512": "0" * 128},
        {"filename": "../escape.qcow2", "url": "https://example.invalid/x", "sha512": "0" * 128},
    ],
)
def test_an_entry_that_cannot_prove_an_image_is_refused(tmp_path, entry):
    cache = tmp_path / "cache"
    cache.mkdir()
    lock = custom_lock(tmp_path, entry)

    result = run_helper("--role", "builder", "--cache", str(cache), "--lock", str(lock))

    assert result.returncode == 1
    assert "base_image_not_locked" in result.stderr


def test_an_uncached_image_is_not_run_rather_than_unverified(tmp_path):
    cache = tmp_path / "cache"
    lock = custom_lock(
        tmp_path,
        {
            "filename": "pinned.qcow2",
            "url": "https://example.invalid/pinned.qcow2",
            "sha512": "0" * 128,
        },
    )

    result = run_helper(
        "--role", "builder", "--cache", str(cache), "--lock", str(lock), "--offline"
    )

    assert result.returncode == 3
    assert "base_image_uncached" in result.stderr


def test_a_failed_download_leaves_no_partial_image_behind(tmp_path):
    cache = tmp_path / "cache"
    lock = custom_lock(
        tmp_path,
        {
            "filename": "pinned.qcow2",
            "url": "https://ems-appliance.invalid/pinned.qcow2",
            "sha512": "0" * 128,
        },
    )

    result = run_helper("--role", "builder", "--cache", str(cache), "--lock", str(lock))

    assert result.returncode == 3
    assert list(cache.iterdir()) == []


@pytest.mark.parametrize("script", [BUILDER_VM, SMOKE_VM])
def test_no_guest_script_boots_a_floating_base_image(script):
    text = script.read_text(encoding="utf-8")

    assert "cloud/trixie/latest" not in text
    assert "appliance-guest-base-image.sh" in text
    assert "base_image_unverified" in text
