# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the build and gate scripts are allowed to put multi-gigabyte staging.

Several of them defaulted to ``/tmp``, which is a tmpfs on many hosts -- 12G on
the maintainer's. A builder VM growing to 60G plus collected artefacts, an 8.5G
sparse crosscheck and 9.5G of firmware fixtures all landed there, filling RAM
rather than a disk. One of them went the other way and hardcoded the
maintainer's own absolute path, which is a portability bug for everyone else.

One resolver decides this now, so a host is configured once.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "scripts" / "lib" / "workdir.sh"

# Every script that stages more than a handful of megabytes.
STAGING_SCRIPTS = (
    "appliance-builder-vm.sh",
    "appliance-crosscheck-sparse.sh",
    "appliance-test-ab-layout.sh",
    "appliance-build-rpi-ab-image.sh",
    "appliance-build-rpi-ab-update.sh",
)


def script(name):
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_the_resolver_exists_and_is_sourceable():
    assert LIBRARY.is_file()
    assert "ems_work_root()" in LIBRARY.read_text(encoding="utf-8")


def test_the_default_work_root_is_not_tmp():
    """A tmpfs default is what turns a build into an out-of-memory kill."""

    resolved = subprocess.run(
        ["sh", "-c", f". {LIBRARY}; ems_work_root"],
        capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/nobody"},
    )

    assert resolved.returncode == 0, resolved.stderr
    assert not resolved.stdout.strip().startswith("/tmp"), resolved.stdout


def test_the_work_root_is_configurable():
    resolved = subprocess.run(
        ["sh", "-c", f". {LIBRARY}; ems_work_root"],
        capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": "/usr/bin:/bin", "EMS_APPLIANCE_WORK_DIR": "/somewhere/with/room"},
    )

    assert resolved.stdout.strip() == "/somewhere/with/room"


def test_a_request_that_does_not_fit_is_refused_with_the_way_out(tmp_path):
    """Filling the filesystem and failing later is the worse diagnosis."""

    refused = subprocess.run(
        ["sh", "-c", f". {LIBRARY}; ems_work_dir enormous 999999999999999"],
        capture_output=True, text=True, check=False, timeout=60,
        env={"PATH": "/usr/bin:/bin", "EMS_APPLIANCE_WORK_DIR": str(tmp_path)},
    )

    assert refused.returncode != 0
    assert "EMS_APPLIANCE_WORK_DIR" in refused.stderr


@pytest.mark.parametrize("name", STAGING_SCRIPTS)
def test_no_staging_script_defaults_to_tmp(name):
    text = script(name)

    assert not re.search(r"\$\{TMPDIR:-/tmp\}", text), f"{name} falls back to /tmp"


@pytest.mark.parametrize("name", STAGING_SCRIPTS)
def test_no_staging_script_hardcodes_a_maintainer_path(name):
    """appliance-test-ab-layout.sh defaulted to /zfs/tmp/tmp, which exists on
    exactly one machine."""

    text = script(name)

    assert "/zfs/tmp" not in text, f"{name} hardcodes a maintainer-only path"


# The image build's work root is inside the release output directory and is
# claimed through the build authority, so it is not scratch the resolver may
# place. Its own finding is that it is never cleared, which is checked below.
RESOLVER_SCRIPTS = tuple(name for name in STAGING_SCRIPTS if "ab-image" not in name)


@pytest.mark.parametrize("name", RESOLVER_SCRIPTS)
def test_every_staging_script_uses_the_shared_resolver(name):
    text = script(name)

    assert "lib/workdir.sh" in text, f"{name} resolves its own work directory"


@pytest.mark.parametrize("name", STAGING_SCRIPTS)
def test_every_staging_script_cleans_up_on_the_failure_path_too(name):
    """A gate that fails halfway must not leave gigabytes behind unannounced."""

    text = script(name)

    assert re.search(r"^trap .* EXIT", text, re.M), f"{name} has no EXIT trap"


def test_a_finished_image_build_does_not_keep_its_fifteen_gigabytes():
    """Every artefact is copied out, so a successful build has no use for it."""

    text = script("appliance-build-rpi-ab-image.sh")

    assert 'rm -rf "$WORK"' in text


def test_a_failed_image_build_keeps_the_tree_and_says_where():
    """It is the only place the failure can be examined -- but silently keeping
    it is how a dist directory grows by 15G per attempt."""

    text = script("appliance-build-rpi-ab-image.sh")

    assert "kept for diagnosis" in text
    assert "du -sh" in text


def test_an_atomic_write_flushes_the_directory_entry_too():
    """The rename is a directory operation. Without flushing the parent, a power
    cut can leave the entry pointing at nothing while the bytes are durable --
    which is how a zero-length state file appears on a headless appliance."""

    import inspect

    from appliance import paths

    source = inspect.getsource(paths.atomic_write)

    assert "_sync_parent" in source
    assert "os.fsync" in inspect.getsource(paths._sync_parent)


def _extract_shell_function(name, script_name):
    """The function on its own, so the test runs the real one and not a copy."""

    text = script(script_name)
    start = text.index(f"{name}() {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"{name} is not a closed function")


def test_a_successful_build_is_not_failed_by_a_cleanup_it_cannot_finish(tmp_path):
    """The verdict belongs to the build, not to the tidying after it.

    The chroot holds directories owned by the accounts the package creates, so
    ``rm -rf`` returns non-zero for a build that produced and verified every
    artefact. Under ``set -e`` the trap then aborts before its own
    ``return "$status"``, and the wrapper prints RESULT: FAIL over a good build
    -- a gate reporting a result it did not earn.
    """

    work = tmp_path / "work"
    (work / "kept").mkdir(parents=True)
    work.chmod(0o500)
    try:
        harness = tmp_path / "harness.sh"
        harness.write_text(
            "set -eu\n"
            f'WORK="{work}"\n'
            + _extract_shell_function(
                "release_work_cleanup", "appliance-build-rpi-ab-image.sh"
            )
            + "\ntrap release_work_cleanup EXIT\nexit 0\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["sh", str(harness)], capture_output=True, text=True, timeout=60
        )
    finally:
        work.chmod(0o700)

    assert result.returncode == 0, f"a successful build was failed by cleanup: {result.stderr}"


def test_the_build_publishes_a_compressed_image_with_its_own_checksum():
    """A 16.5 GiB image is not a download anyone completes, and no common
    release host accepts it: GitHub caps a release asset at 2 GB. Compressed it
    is roughly 486 MB, and Raspberry Pi Imager and balenaEtcher write .img.xz
    straight to the card, so the operator unpacks nothing.

    The checksum has to cover the file that was actually downloaded. A digest
    over the raw image cannot verify the compressed one, which is how a good
    download gets discarded as corrupt -- the defect the RC review recorded.
    """

    text = script("appliance-build-rpi-ab-image.sh")

    assert "xz" in text, "the build produces no compressed image"
    assert '$NAME.img.xz.sha256' in text
    assert '$NAME.img.sha256' in text, "the raw image keeps its own checksum"


def test_the_build_refuses_up_front_if_it_cannot_compress():
    """A missing tool must not surface after the image was already built.

    The compression step runs at the end of a build that takes roughly
    twenty-five minutes. Discovering there that xz is absent spends all of it
    and, by the script's own rule, a host that cannot build reports NOT RUN
    rather than a failure it did not earn.
    """

    text = script("appliance-build-rpi-ab-image.sh")
    preflight = text.split("cp \"$BUILT\"", 1)[0]

    assert 'command -v xz' in preflight, "xz is used but never verified up front"
    assert "required_tool_missing" in preflight
