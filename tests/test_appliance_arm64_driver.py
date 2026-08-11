# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ARM64 smoke driver, before it is ever pointed at a real VM.

The ARM64 run is the last validation step before an appliance image is built,
so the driver must be reproducible: a known image, a verified checksum, the
firmware variable store the installed firmware actually expects, and a guest
that proves it is aarch64 before anything is installed. A driver that silently
downloads a moving ``latest`` image, or that boots with a blank variable store,
produces a result that means nothing.

``qemu`` itself is stubbed here. This module proves the driver's decisions, not
that a guest boots — that is the separate VM validation step.
"""

import os
import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

from pathlib import Path  # noqa: E402  (after pytestmark on purpose)

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "appliance-smoke-arm64.sh"
GUEST = ROOT / "scripts" / "appliance-guest-smoke.sh"

EXIT_NOT_RUN = 3
EXIT_USAGE = 2

QEMU_STUB = """#!/bin/sh
console=""
evidence=""
for argument in "$@"; do
    case "$argument" in
        file:*) console=${argument#file:} ;;
        file,id=evidence,path=*) evidence=${argument#file,id=evidence,path=} ;;
    esac
done
[ -n "$console" ] || exit 1
cat "${EMS_TEST_CONSOLE_FIXTURE:-/dev/null}" > "$console"
# A guest that reached the dedicated port delivers its record there. The
# console keeps whatever the guest also printed, exactly as on real hardware.
if [ "${EMS_TEST_EVIDENCE_CHANNEL:-dedicated}" = dedicated ] && [ -n "$evidence" ]; then
    cat "${EMS_TEST_EVIDENCE_FIXTURE:-${EMS_TEST_CONSOLE_FIXTURE:-/dev/null}}" > "$evidence"
fi
exit 0
"""

QEMU_IMG_STUB = """#!/bin/sh
if [ "$1" = "info" ]; then
    # The shape qemu-img 9 and later actually print: the protocol node comes
    # first and its own format is "file", so a driver that takes the first
    # "format" in the document reads every qcow2 as unusable.
    printf '{\\n'
    printf '  "children": [ { "name": "file", "info": {\\n'
    printf '      "virtual-size": 336855040, "format": "file" } } ],\\n'
    printf '  "virtual-size": 2147483648,\\n'
    printf '  "format": "%s",\\n' "${EMS_TEST_IMAGE_FORMAT:-qcow2}"
    printf '  "format-specific": { "type": "%s" }\\n' "${EMS_TEST_IMAGE_FORMAT:-qcow2}"
    printf '}\\n'
    exit "${EMS_TEST_IMAGE_INFO_RC:-0}"
fi
for argument in "$@"; do
    case "$argument" in
        */guest.qcow2) : > "$argument" ;;
    esac
done
exit 0
"""

QEMU_VERSION_STUB_SUFFIX = """
if [ "$1" = "--version" ]; then
    echo "QEMU emulator version 9.2.0 (Debian 1:9.2.0+ds-1)"
    exit 0
fi
"""

CURL_STUB = """#!/bin/sh
output=""
url=""
previous=""
for argument in "$@"; do
    [ "$previous" = "-o" ] && output=$argument
    case "$argument" in
        http*) url=$argument ;;
    esac
    previous=$argument
done
name=$(basename "$url")
source="$EMS_TEST_MIRROR/$name"
[ -f "$source" ] || exit 22
cp "$source" "$output"
exit 0
"""

GPGV_STUB = """#!/bin/sh
exit "${EMS_TEST_GPGV_RC:-0}"
"""

TOUCH_LAST_ARGUMENT = """#!/bin/sh
for argument in "$@"; do
    case "$argument" in
        *.iso) : > "$argument" ;;
    esac
done
exit 0
"""

XORRISO_STUB = """#!/bin/sh
previous=""
for argument in "$@"; do
    [ "$previous" = "-o" ] && : > "$argument"
    previous=$argument
done
exit 0
"""

PASSING_CONSOLE = """== architecture ==
guest: aarch64 Debian GNU/Linux 13 (trixie)
  PASS  the package installs and its postinst reports success
RESULT: PASS
APPLIANCE_SMOKE_EXIT: 0
"""

FAILING_CONSOLE = """== architecture ==
guest: aarch64 Debian GNU/Linux 13 (trixie)
  FAIL  verify-install reports an unusable appliance
RESULT: FAIL
"""


def qemu_stub(extra=""):
    """The emulator stub, always able to answer ``--version``."""

    body = QEMU_STUB.replace('console=""\n', 'console=""\n' + QEMU_VERSION_STUB_SUFFIX)
    return body if not extra else body.replace("exit 0\n", extra + "exit 0\n")


@pytest.fixture
def sandbox(tmp_path):
    """A PATH with every external tool the driver needs, all stubbed."""

    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (
        ("qemu-system-aarch64", qemu_stub()),
        ("qemu-img", QEMU_IMG_STUB),
        ("cloud-localds", TOUCH_LAST_ARGUMENT),
        ("xorriso", XORRISO_STUB),
        ("curl", CURL_STUB),
        ("gpgv", GPGV_STUB),
    ):
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "AAVMF_CODE.fd").write_bytes(b"\x00" * (64 * 1024 * 1024))
    (firmware / "AAVMF_VARS.fd").write_bytes(b"\xff" * (64 * 1024 * 1024))

    image = tmp_path / "debian-13-arm64.qcow2"
    image.write_bytes(b"QFI\xfb" + b"\x00" * 64)

    console = tmp_path / "console-fixture.txt"
    console.write_text(PASSING_CONSOLE, encoding="utf-8")

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    keyring = tmp_path / "keyring.gpg"
    keyring.write_bytes(b"fake-keyring")

    cache = tmp_path / "vm-cache"
    cache.mkdir()

    return {
        "stubs": stubs,
        "firmware": firmware,
        "image": image,
        "console": console,
        "mirror": mirror,
        "keyring": keyring,
        "cache": cache,
        "tmp": tmp_path,
    }


def run_driver(sandbox, *arguments, **environment):
    env = dict(os.environ)
    env["PATH"] = sandbox.get("path") or f"{sandbox['stubs']}:{env.get('PATH', '')}"
    env["EMS_ARM64_FIRMWARE"] = str(sandbox["firmware"] / "AAVMF_CODE.fd")
    env["EMS_ARM64_FIRMWARE_VARS"] = str(sandbox["firmware"] / "AAVMF_VARS.fd")
    env["EMS_TEST_CONSOLE_FIXTURE"] = str(sandbox["console"])
    env["EMS_TEST_MIRROR"] = str(sandbox["mirror"])
    env["EMS_ARM64_KEYRINGS"] = str(sandbox["keyring"])
    env["TMPDIR"] = str(sandbox["tmp"])
    # The driver's base-image step falls back to the developer's own
    # ~/.cache/ems-appliance-vm. A case that asks what happens when an image
    # cannot be obtained would silently be answered by a real cached image that
    # an earlier VM run had left there, so every run gets an empty cache of its
    # own unless the case seeds one deliberately.
    env["EMS_APPLIANCE_VM_CACHE"] = str(sandbox["cache"])
    env.update({key: str(value) for key, value in environment.items()})
    return subprocess.run(
        [str(DRIVER), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env=env,
    )


# --- static contract --------------------------------------------------------


def text():
    return DRIVER.read_text(encoding="utf-8")


def test_a_downloaded_image_is_checksum_verified():
    driver = text()
    assert "SHA512SUMS" in driver or "SHA256SUMS" in driver
    assert "sha512sum -c" in driver or "sha256sum -c" in driver


def test_a_downloaded_checksum_manifest_is_signature_verified_when_possible():
    driver = text()
    assert "gpgv" in driver
    assert ".sign" in driver


def test_an_unverified_image_is_never_used_silently():
    driver = text()
    assert "unverified" in driver.lower()
    assert "--allow-unverified-image" in driver


def test_the_matching_uefi_variable_store_template_is_preferred():
    driver = text()
    assert "AAVMF_VARS" in driver
    assert "EMS_ARM64_FIRMWARE_VARS" in driver


def test_a_run_that_did_not_happen_still_reports_not_run():
    driver = text()
    assert "RESULT: NOT RUN" in driver
    assert "exit 3" in driver


def test_working_files_are_only_kept_on_request():
    driver = text()
    assert "--keep" in driver
    assert "trap cleanup EXIT" in driver


def test_the_guest_proves_its_architecture_before_installing_the_package():
    guest = GUEST.read_text(encoding="utf-8")
    assert "dpkg --print-architecture" in guest
    assert "uname -m" in guest
    assert "EXPECTED_ARCH" in guest


def test_the_package_architecture_is_asked_of_the_package_not_its_name():
    """This driver stages the package as appliance.deb.

    A file-name test would call the driver's own arm64 build "not a arm64
    build", and would equally believe an amd64 package renamed to end in
    _arm64.deb.
    """

    guest = GUEST.read_text(encoding="utf-8")

    assert "dpkg-deb -f" in guest
    assert "Architecture" in guest
    assert '*_"$EXPECTED_ARCH".deb' not in guest


def test_the_arm64_driver_asks_the_guest_for_arm64():
    assert "guest-smoke.sh /mnt/payload/appliance.deb arm64" in text()


# --- driver behaviour -------------------------------------------------------


def test_a_supplied_image_runs_without_downloading_anything(sandbox):
    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_a_failing_guest_is_reported_as_a_failure_not_as_not_run(sandbox):
    sandbox["console"].write_text(FAILING_CONSOLE, encoding="utf-8")

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "RESULT: FAIL" in result.stdout + result.stderr


def test_a_silent_guest_is_never_reported_as_a_pass(sandbox):
    sandbox["console"].write_text("", encoding="utf-8")

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_a_symlinked_firmware_is_measured_through_the_link(sandbox):
    """Debian ships AAVMF_CODE.fd as a symlink, which is the normal case.

    `stat` without -L measures the link — 24 bytes — so the pflash bound would
    be checked against the link and pass whatever it pointed at, and the run's
    own inputs record would claim a 24-byte firmware.
    """

    firmware = sandbox["firmware"]
    (firmware / "AAVMF_CODE.real.fd").write_bytes(b"\x00" * (64 * 1024 * 1024))
    link = firmware / "AAVMF_CODE.link.fd"
    link.symlink_to("AAVMF_CODE.real.fd")

    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), EMS_ARM64_FIRMWARE=str(link)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{64 * 1024 * 1024} bytes" in result.stdout, result.stdout


def test_a_symlinked_firmware_larger_than_the_slot_is_still_refused(sandbox):
    firmware = sandbox["firmware"]
    (firmware / "AAVMF_CODE.big.fd").write_bytes(b"\x00" * (64 * 1024 * 1024 + 1))
    link = firmware / "AAVMF_CODE.biglink.fd"
    link.symlink_to("AAVMF_CODE.big.fd")

    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), EMS_ARM64_FIRMWARE=str(link)
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "pflash" in result.stdout + result.stderr


def test_missing_firmware_reports_not_run(sandbox):
    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        EMS_ARM64_FIRMWARE=str(sandbox["tmp"] / "absent.fd"),
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_a_missing_emulator_reports_not_run(sandbox):
    (sandbox["stubs"] / "qemu-system-aarch64").unlink()

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == EXIT_NOT_RUN
    assert "RESULT: NOT RUN" in result.stderr


def test_a_missing_image_file_reports_not_run(sandbox):
    result = run_driver(sandbox, "--image", str(sandbox["tmp"] / "absent.qcow2"))

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_the_variable_store_is_taken_from_the_firmware_template(sandbox):
    """A blank variable store is not a substitute for AAVMF_VARS.fd."""

    marker = sandbox["firmware"] / "AAVMF_VARS.fd"
    marker.write_bytes(b"VARS-TEMPLATE".ljust(64 * 1024 * 1024, b"\x00"))
    recorder = sandbox["stubs"] / "qemu-system-aarch64"
    recorder.write_text(
        qemu_stub(
            'for argument in "$@"; do\n'
            '    case "$argument" in\n'
            '        *efi-vars.fd) head -c 13 "${argument#*file=}" > "$EMS_TEST_VARS_PROBE" ;;\n'
            "    esac\n"
            "done\n"
        ),
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    probe = sandbox["tmp"] / "vars-probe"

    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), EMS_TEST_VARS_PROBE=str(probe)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert probe.read_bytes() == b"VARS-TEMPLATE"


# --- deterministic image validation -----------------------------------------


def sha256_of(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_a_raw_image_where_qcow2_is_required_reports_not_run(sandbox):
    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), EMS_TEST_IMAGE_FORMAT="raw"
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr
    assert "qcow2" in result.stderr


def test_an_unreadable_image_reports_not_run(sandbox):
    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), EMS_TEST_IMAGE_INFO_RC="1"
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_a_supplied_checksum_that_matches_is_accepted(sandbox):
    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--image-sha256",
        sha256_of(sandbox["image"]),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_a_supplied_checksum_that_does_not_match_reports_not_run(sandbox):
    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), "--image-sha256", "0" * 64
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_a_checksum_file_is_accepted_for_a_supplied_image(sandbox):
    manifest = sandbox["tmp"] / "image.sha256"
    manifest.write_text(
        f"{sha256_of(sandbox['image'])}  {sandbox['image'].name}\n", encoding="utf-8"
    )

    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), "--image-checksum-file", str(manifest)
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_run_prints_the_inputs_it_used(sandbox):
    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--image-sha256",
        sha256_of(sandbox["image"]),
    )

    combined = result.stdout + result.stderr
    assert sha256_of(sandbox["image"]) in combined, combined
    assert "QEMU emulator version" in combined, combined
    assert str(sandbox["firmware"] / "AAVMF_CODE.fd") in combined, combined
    assert str(sandbox["firmware"] / "AAVMF_VARS.fd") in combined, combined
    assert "package sha256" in combined.lower(), combined


def test_an_unverified_run_is_labelled_and_never_a_release_pass(sandbox):
    result = run_driver(sandbox, "--image", str(sandbox["image"]), "--allow-unverified-image")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "UNVERIFIED INPUT" in combined, combined
    assert "RESULT: PASS (unverified" in combined, combined


def test_a_verified_run_is_not_labelled_unverified(sandbox):
    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--image-sha256",
        sha256_of(sandbox["image"]),
    )

    assert "UNVERIFIED INPUT" not in result.stdout + result.stderr


# --- firmware pairing -------------------------------------------------------


def test_a_missing_variable_store_template_reports_not_run(sandbox):
    (sandbox["firmware"] / "AAVMF_VARS.fd").unlink()

    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        EMS_ARM64_FIRMWARE_VARS=str(sandbox["tmp"] / "absent-vars.fd"),
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_incompatible_firmware_sizes_report_not_run(sandbox):
    (sandbox["firmware"] / "AAVMF_CODE.fd").write_bytes(b"\x00" * (128 * 1024 * 1024))

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


# --- the downloaded-image manifest ------------------------------------------


def seed_pinned_cache(sandbox, *, content=None, digest=None):
    """A cache and a lock the shared acquisition helper can answer from.

    The driver no longer fetches its own image: every disposable guest in this
    project resolves one pinned, digest-verified base image through
    scripts/appliance-guest-base-image.sh. What this seeds is therefore a lock
    and a cache entry, not a mirror.
    """

    import hashlib
    import json

    cache = sandbox["tmp"] / "vm-cache"
    cache.mkdir(exist_ok=True)
    payload = content if content is not None else sandbox["image"].read_bytes()
    image = cache / "debian-13-genericcloud-arm64-testrun.qcow2"
    image.write_bytes(payload)
    lock = sandbox["tmp"] / "base-images.lock.json"
    lock.write_text(
        json.dumps(
            {
                "lock_version": 1,
                "images": {
                    "guest-arm64": {
                        "filename": image.name,
                        "url": "https://example.invalid/images/" + image.name,
                        "sha512": digest or hashlib.sha512(payload).hexdigest(),
                        "build_id": "testrun",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "EMS_APPLIANCE_VM_CACHE": str(cache),
        "EMS_APPLIANCE_VM_BASE_IMAGE_LOCK": str(lock),
    }


def download(sandbox, *arguments, **environment):
    return run_driver(sandbox, *arguments, **environment)


def test_the_pinned_base_image_is_accepted_and_the_driver_runs(sandbox):
    result = download(sandbox, **seed_pinned_cache(sandbox))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "UNVERIFIED INPUT" not in result.stdout + result.stderr


def test_a_cached_image_that_is_not_the_locked_one_is_refused(sandbox):
    result = download(sandbox, **seed_pinned_cache(sandbox, digest="0" * 128))

    assert result.returncode != 0
    assert "base_image_digest_mismatch" in result.stdout + result.stderr


def test_the_driver_never_fetches_a_floating_base_image():
    text = (ROOT / "scripts/appliance-smoke-arm64.sh").read_text(encoding="utf-8")

    assert "appliance-guest-base-image.sh" in text
    assert "base_image_unverified" in text






def test_a_manifest_that_cannot_be_downloaded_is_refused(sandbox):
    image = sandbox["mirror"] / "debian-13-genericcloud-arm64.qcow2"
    image.write_bytes(sandbox["image"].read_bytes())

    result = download(sandbox)

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr




def test_the_unverified_override_cannot_be_combined_with_a_checksum(sandbox):
    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--image-sha256",
        sha256_of(sandbox["image"]),
        "--allow-unverified-image",
    )

    assert result.returncode == 2, result.stdout + result.stderr


# --- guest architecture -----------------------------------------------------


def test_a_guest_that_reports_the_wrong_architecture_is_not_a_pass(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE.replace("aarch64", "x86_64"), encoding="utf-8"
    )

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: PASS (booted" not in result.stdout


def test_both_smoke_drivers_share_the_same_guest_test():
    amd64 = (ROOT / "scripts" / "appliance-smoke-amd64.sh").read_text(encoding="utf-8")
    arm64 = text()
    assert "appliance-guest-smoke.sh" in amd64
    assert "appliance-guest-smoke.sh" in arm64


# --- a PASS marker is evidence, not a verdict -------------------------------

TIMEOUT_STATUS = 124

PANIC_CONSOLE = """== architecture ==
guest: aarch64 Debian GNU/Linux 13 (trixie)
Kernel panic - not syncing: Attempted to kill init!
"""


def qemu_exit(status):
    """A stub emulator that writes the console fixture and then exits ``status``."""

    body = qemu_stub()
    head, _, _ = body.rpartition("exit 0\n")
    return head + f"exit {status}\n"


def install_qemu(sandbox, body):
    path = sandbox["stubs"] / "qemu-system-aarch64"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_pass_marker_after_a_timeout_is_not_a_pass(sandbox):
    install_qemu(sandbox, qemu_exit(TIMEOUT_STATUS))

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "RESULT: PASS (booted" not in result.stdout, result.stdout
    assert "timed out" in (result.stdout + result.stderr).lower(), result.stderr


def test_a_pass_marker_after_a_qemu_failure_is_not_a_pass(sandbox):
    install_qemu(sandbox, qemu_exit(1))

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "RESULT: PASS (booted" not in result.stdout, result.stdout


def test_a_pass_without_the_guest_exit_marker_is_not_a_pass(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE.replace("APPLIANCE_SMOKE_EXIT: 0\n", ""), encoding="utf-8"
    )

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "RESULT: PASS (booted" not in result.stdout, result.stdout


def test_a_non_zero_guest_exit_marker_is_not_a_pass(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE.replace("APPLIANCE_SMOKE_EXIT: 0", "APPLIANCE_SMOKE_EXIT: 1"),
        encoding="utf-8",
    )

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode != 0, result.stdout + result.stderr


def test_a_later_failure_overrides_an_earlier_pass(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE + "RESULT: FAIL (1 check(s))\nAPPLIANCE_SMOKE_EXIT: 1\n",
        encoding="utf-8",
    )

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "RESULT: FAIL" in result.stdout + result.stderr


def test_a_kernel_panic_is_never_a_pass(sandbox):
    sandbox["console"].write_text(PANIC_CONSOLE + "RESULT: PASS\n", encoding="utf-8")

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "panic" in (result.stdout + result.stderr).lower()


def test_the_guest_architecture_marker_is_what_proves_aarch64(sandbox):
    """A stray "aarch64" anywhere in the log is not the guest reporting it."""

    sandbox["console"].write_text(
        "downloading debian-13-genericcloud-aarch64.qcow2\nRESULT: PASS\n"
        "APPLIANCE_SMOKE_EXIT: 0\n",
        encoding="utf-8",
    )

    result = run_driver(sandbox, "--image", str(sandbox["image"]))

    assert result.returncode != 0, result.stdout + result.stderr


# --- input validation -------------------------------------------------------


def test_an_option_without_its_value_fails_with_usage(sandbox):
    result = run_driver(sandbox, "--image")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--image" in result.stderr, result.stderr


def test_a_non_numeric_memory_value_is_refused(sandbox):
    result = run_driver(sandbox, "--image", str(sandbox["image"]), EMS_ARM64_MEMORY="lots")

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_a_non_numeric_timeout_is_refused(sandbox):
    result = run_driver(sandbox, "--image", str(sandbox["image"]), EMS_ARM64_BOOT_TIMEOUT="soon")

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_a_zero_cpu_count_is_refused(sandbox):
    result = run_driver(sandbox, "--image", str(sandbox["image"]), EMS_ARM64_CPUS="0")

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_an_unwritable_output_directory_is_refused(sandbox):
    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--output",
        str(sandbox["tmp"] / "absent" / "logs"),
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_a_package_built_for_another_architecture_is_refused(sandbox):
    """The driver must not smoke-test an amd64 package in an ARM64 guest."""

    build = sandbox["stubs"] / "build-deb.sh"
    build.write_text(
        "#!/bin/sh\n"
        'output=""\n'
        'while [ $# -gt 0 ]; do case "$1" in --output) output=$2; shift 2 ;; *) shift ;; esac; done\n'
        'name="$output/ems-appliance-manager_9.9.9_amd64.deb"\n'
        ': > "$name"\n'
        'cd "$output" && sha256sum "$(basename "$name")" > "$(basename "$name").sha256"\n',
        encoding="utf-8",
    )
    build.chmod(0o755)

    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), EMS_ARM64_BUILD_SCRIPT=str(build)
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "arm64" in result.stderr, result.stderr


# --- cleanup ----------------------------------------------------------------


def test_a_failed_run_still_removes_its_working_directory(sandbox):
    install_qemu(sandbox, qemu_exit(1))
    before = {item.name for item in sandbox["tmp"].iterdir()}

    run_driver(sandbox, "--image", str(sandbox["image"]))

    leftovers = {
        item.name
        for item in sandbox["tmp"].iterdir()
        if item.name.startswith("ems-appliance-arm64.")
    }
    assert not leftovers, leftovers | before


def evidence_runs(output):
    """The per-run evidence directories inside an --output directory."""

    return sorted(item for item in output.iterdir() if item.name.startswith("run-"))


def only_run(output):
    runs = evidence_runs(output)
    assert len(runs) == 1, sorted(item.name for item in output.iterdir())
    return runs[0]


def test_a_failed_run_preserves_the_serial_log_when_asked(sandbox):
    install_qemu(sandbox, qemu_exit(1))
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    result = run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert result.returncode != 0
    run = only_run(output)
    assert (run / "console.log").is_file(), sorted(item.name for item in run.iterdir())


def test_a_failed_run_preserves_the_emulator_verdict_as_evidence(sandbox):
    """The reason a run failed has to survive the working directory."""

    install_qemu(sandbox, qemu_exit(TIMEOUT_STATUS))
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    result = run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert result.returncode == 1, result.stdout + result.stderr
    run = only_run(output)
    assert (run / "qemu-status.txt").read_text().strip() == str(TIMEOUT_STATUS)
    assert "qemu-system-aarch64" in (run / "qemu-command.txt").read_text()
    assert sha256_of(sandbox["image"]) in (run / "inputs.txt").read_text()
    summary = (run / "result.txt").read_text()
    assert "FAIL" in summary and "timed out" in summary, summary


# --- an option that needs a value may not read the next option as one -------


@pytest.mark.parametrize(
    "arguments",
    [
        ("--image", "--keep"),
        ("--output", "--allow-unverified-image"),
        ("--image-sha256", "--output"),
        ("--image-checksum-file", "--keep"),
        ("--image",),
        ("--output",),
        ("--image=",),
        ("--output=",),
    ],
)
def test_an_option_without_a_value_is_a_usage_error(sandbox, arguments):
    result = run_driver(sandbox, *arguments)

    assert result.returncode == EXIT_USAGE, result.stdout + result.stderr
    assert "RESULT: NOT RUN" not in result.stderr, result.stderr
    assert "requires a value" in result.stderr, result.stderr


def test_an_explicit_inline_value_is_accepted(sandbox):
    result = run_driver(sandbox, f"--image={sandbox['image']}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_an_inline_checksum_is_accepted(sandbox):
    result = run_driver(
        sandbox,
        f"--image={sandbox['image']}",
        f"--image-sha256={sha256_of(sandbox['image'])}",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNVERIFIED INPUT" not in result.stdout + result.stderr


# --- evidence belongs to exactly one run ------------------------------------


def test_every_required_artefact_is_preserved(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    result = run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert result.returncode == 0, result.stdout + result.stderr
    run = only_run(output)
    present = sorted(item.name for item in run.iterdir())
    for artifact in (
        "console.log",
        "qemu-command.txt",
        "qemu-status.txt",
        "inputs.txt",
        "result.txt",
        "run.txt",
    ):
        assert artifact in present, present


def test_the_evidence_records_who_produced_it(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    metadata = (only_run(output) / "run.txt").read_text(encoding="utf-8")
    assert "run_id: " in metadata, metadata
    assert "started_at: " in metadata, metadata
    assert "ended_at: " in metadata, metadata
    assert "driver_revision: " in metadata, metadata
    assert sha256_of(sandbox["image"]) in metadata, metadata


def test_two_runs_never_share_an_evidence_directory(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))
    install_qemu(sandbox, qemu_exit(1))
    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    runs = evidence_runs(output)
    assert len(runs) == 2, sorted(item.name for item in output.iterdir())
    verdicts = sorted((item / "result.txt").read_text(encoding="utf-8").splitlines()[0]
                      for item in runs)
    assert verdicts == ["result: FAIL", "result: PASS"], verdicts


def test_stale_files_in_the_output_directory_are_not_this_runs_evidence(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()
    (output / "console.log").write_text("a previous run\n", encoding="utf-8")

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    run = only_run(output)
    assert "a previous run" not in (run / "console.log").read_text(encoding="utf-8")
    assert (output / "console.log").read_text(encoding="utf-8") == "a previous run\n"


def test_evidence_that_cannot_be_copied_is_never_reported_as_preserved(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()
    # Only the copies into the evidence directory fail; everything the run
    # needs to reach a verdict still works, so what is under test is the claim
    # that the evidence was preserved.
    guard = sandbox["stubs"] / "cp"
    guard.write_text(
        "#!/bin/sh\n"
        'for argument in "$@"; do case "$argument" in */run-*/*) exit 1 ;; esac; done\n'
        'exec /bin/cp "$@"\n',
        encoding="utf-8",
    )
    guard.chmod(0o755)

    result = run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "evidence is incomplete" in result.stderr, result.stderr
    assert "RESULT: EVIDENCE INCOMPLETE" in result.stderr, result.stderr


# --- a verified pass and an unverified one are different results ------------


def test_the_result_file_separates_a_release_gate_from_a_functional_pass(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--allow-unverified-image",
        "--output",
        str(output),
    )

    summary = (only_run(output) / "result.txt").read_text(encoding="utf-8")
    assert "result: PASS" in summary, summary
    assert "verification: unverified" in summary, summary
    assert "release_gate: no" in summary, summary


def test_a_verified_pass_is_marked_as_a_release_gate(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--image-sha256",
        sha256_of(sandbox["image"]),
        "--output",
        str(output),
    )

    summary = (only_run(output) / "result.txt").read_text(encoding="utf-8")
    assert "verification: verified" in summary, summary
    assert "release_gate: pass" in summary, summary


def test_a_timeout_is_classified_in_the_result(sandbox):
    install_qemu(sandbox, qemu_exit(TIMEOUT_STATUS))
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    summary = (only_run(output) / "result.txt").read_text(encoding="utf-8")
    assert "result: FAIL" in summary, summary
    assert "timeout: expired" in summary, summary
    assert "release_gate: no" in summary, summary


def test_a_completed_run_is_classified_as_untimed_out(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert "timeout: none" in (only_run(output) / "result.txt").read_text(encoding="utf-8")


# --- a terminal result always leaves a complete record ----------------------


# What the driver itself needs before it reaches its first prerequisite check.
# A test that removes a stub has to remove the tool, and the sandbox PATH still
# falls through to the developer's own /usr/bin — so these runs get a PATH built
# from exactly this list plus the stubs, and nothing else.
DRIVER_OWN_TOOLS = (
    "bash", "sh", "env", "sed", "date", "mkdir", "mktemp", "uname", "stat", "cp", "mv",
    "rm", "cat", "basename", "dirname", "head", "tail", "grep", "awk", "cut", "tr",
    "sort", "chmod", "readlink", "printf", "truncate", "git", "sha256sum", "timeout",
)


def without_tool(sandbox, name):
    """Remove one prerequisite, the way a host that never installed it is."""

    (sandbox["stubs"] / name).unlink()
    shim = sandbox["tmp"] / "minimal-bin"
    shim.mkdir(exist_ok=True)
    for tool in DRIVER_OWN_TOOLS:
        found = shutil.which(tool)
        target = shim / tool
        if found and not target.exists():
            target.symlink_to(found)
    sandbox["path"] = f"{sandbox['stubs']}:{shim}"
    return name


def result_fields(run):
    fields = {}
    for line in (run / "result.txt").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


NOT_RUN_EVIDENCE = (
    "result.txt",
    "inputs.txt",
    "run.txt",
    "environment.txt",
    "missing-requirements.txt",
)


@pytest.mark.parametrize(
    "tool", ["qemu-system-aarch64", "cloud-localds", "xorriso", "qemu-img"]
)
def test_a_missing_tool_still_writes_a_complete_not_run_record(sandbox, tool):
    """An evidence directory that was asked for is never left empty."""

    output = sandbox["tmp"] / "evidence"
    without_tool(sandbox, tool)

    result = run_driver(
        sandbox, "--image", str(sandbox["image"]), "--output", str(output)
    )

    assert result.returncode == 3, result.stdout + result.stderr
    run = only_run(output)
    present = sorted(item.name for item in run.iterdir())
    for artifact in NOT_RUN_EVIDENCE:
        assert artifact in present, (artifact, present)
    fields = result_fields(run)
    assert fields["result"] == "NOT RUN", fields
    assert fields["exit_code"] == "3", fields
    assert fields["verified"] == "false", fields
    assert fields["qemu_started"] == "false", fields
    assert fields["evidence_complete"] == "true", fields
    assert tool in (run / "missing-requirements.txt").read_text(encoding="utf-8")


def test_a_missing_tool_reports_a_stable_reason_code(sandbox):
    output = sandbox["tmp"] / "evidence"
    without_tool(sandbox, "cloud-localds")

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert result_fields(only_run(output))["reason_code"] == "required_tool_missing"


def test_missing_firmware_reports_a_stable_reason_code(sandbox):
    output = sandbox["tmp"] / "evidence"

    run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--output",
        str(output),
        EMS_ARM64_FIRMWARE=str(sandbox["tmp"] / "absent.fd"),
    )

    fields = result_fields(only_run(output))
    assert fields["reason_code"] == "firmware_unavailable", fields
    assert fields["result"] == "NOT RUN", fields


def test_an_early_not_run_never_leaves_an_empty_run_directory(sandbox):
    output = sandbox["tmp"] / "evidence"
    without_tool(sandbox, "qemu-system-aarch64")

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    for run in evidence_runs(output):
        assert sorted(item.name for item in run.iterdir()), run


def test_a_usage_error_after_the_output_was_accepted_still_leaves_a_record(sandbox):
    """The output directory was taken; a run that ends there owes it a result."""

    output = sandbox["tmp"] / "evidence"

    result = run_driver(
        sandbox,
        "--output",
        str(output),
        "--image",
        str(sandbox["image"]),
        "--image-sha256",
        sha256_of(sandbox["image"]),
        "--allow-unverified-image",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    fields = result_fields(only_run(output))
    assert fields["result"] == "USAGE ERROR", fields
    assert fields["exit_code"] == "2", fields
    assert fields["reason_code"], fields


def test_an_invalid_output_path_owes_no_evidence(sandbox):
    """Where the output path itself is the fault there is nowhere to write it."""

    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--output",
        str(sandbox["tmp"] / "absent-parent" / "evidence"),
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert not (sandbox["tmp"] / "absent-parent").exists()


def test_a_passing_run_states_that_its_evidence_is_complete(sandbox):
    output = sandbox["tmp"] / "evidence"

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    fields = result_fields(only_run(output))
    assert fields["result"] == "PASS", fields
    assert fields["evidence_complete"] == "true", fields
    assert fields["qemu_started"] == "true", fields


def test_two_early_not_run_results_never_share_a_directory(sandbox):
    output = sandbox["tmp"] / "evidence"
    without_tool(sandbox, "xorriso")

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))
    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    runs = evidence_runs(output)
    assert len(runs) == 2, sorted(item.name for item in output.iterdir())
    for run in runs:
        assert (run / "result.txt").is_file()


# --- every terminal result after --output was accepted owes a record --------


@pytest.mark.parametrize(
    "arguments",
    [
        ("--image", "--keep"),
        ("--image-sha256", "--allow-unverified-image"),
    ],
    ids=["image_swallows_keep", "checksum_swallows_allow_unverified"],
)
def test_a_parse_error_after_the_output_was_accepted_still_leaves_a_record(
    sandbox, arguments
):
    """The evidence directory is taken as soon as --output parses, not later."""

    output = sandbox["tmp"] / "evidence"

    result = run_driver(sandbox, "--output", str(output), *arguments)

    assert result.returncode == EXIT_USAGE, result.stdout + result.stderr
    run = only_run(output)
    fields = result_fields(run)
    assert fields["result"] == "USAGE ERROR", fields
    assert fields["exit_code"] == "2", fields
    assert fields["reason_code"] == "usage_error", fields
    assert fields["evidence_complete"] == "true", fields
    for artifact in NOT_RUN_EVIDENCE:
        assert (run / artifact).is_file(), sorted(item.name for item in run.iterdir())


def test_a_repeated_output_directory_is_a_recorded_usage_error(sandbox):
    output = sandbox["tmp"] / "evidence"

    result = run_driver(sandbox, "--output", str(output), "--output", str(output))

    assert result.returncode == EXIT_USAGE, result.stdout + result.stderr
    assert result_fields(only_run(output))["result"] == "USAGE ERROR"


def test_a_working_directory_that_cannot_be_created_is_a_complete_record(sandbox):
    """The evidence transaction opens before the temporary directory exists."""

    output = sandbox["tmp"] / "evidence"

    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--output",
        str(output),
        TMPDIR=str(sandbox["tmp"] / "absent" / "deeper"),
    )

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    run = only_run(output)
    fields = result_fields(run)
    assert fields["result"] == "NOT RUN", fields
    assert fields["reason_code"] == "working_directory_unusable", fields
    assert fields["evidence_complete"] == "true", fields
    for artifact in NOT_RUN_EVIDENCE:
        assert (run / artifact).is_file(), sorted(item.name for item in run.iterdir())


# --- the reason code of a terminal result is the truth about it -------------


def reason_code_of(sandbox, *arguments, **environment):
    output = sandbox["tmp"] / "evidence"
    run_driver(sandbox, "--output", str(output), *arguments, **environment)
    return result_fields(only_run(output))["reason_code"]


def test_a_passing_run_records_a_final_reason_code(sandbox):
    assert reason_code_of(sandbox, "--image", str(sandbox["image"])) == "guest_smoke_passed"


def test_a_guest_failure_records_a_final_reason_code(sandbox):
    sandbox["console"].write_text(FAILING_CONSOLE, encoding="utf-8")

    assert reason_code_of(sandbox, "--image", str(sandbox["image"])) == "guest_smoke_failed"


def test_a_timeout_records_a_final_reason_code(sandbox):
    install_qemu(sandbox, qemu_exit(TIMEOUT_STATUS))

    assert reason_code_of(sandbox, "--image", str(sandbox["image"])) == "guest_timeout"


def test_an_abnormal_emulator_exit_records_a_final_reason_code(sandbox):
    install_qemu(sandbox, qemu_exit(1))

    assert reason_code_of(sandbox, "--image", str(sandbox["image"])) == "qemu_failed"


def test_a_kernel_panic_records_a_final_reason_code(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE.replace("RESULT: PASS", "Kernel panic - not syncing"),
        encoding="utf-8",
    )

    assert reason_code_of(sandbox, "--image", str(sandbox["image"])) == "guest_kernel_panic"


def test_a_foreign_architecture_records_a_final_reason_code(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE.replace("guest: aarch64", "guest: x86_64"), encoding="utf-8"
    )

    assert (
        reason_code_of(sandbox, "--image", str(sandbox["image"]))
        == "guest_architecture_mismatch"
    )


def test_a_missing_completion_marker_records_a_final_reason_code(sandbox):
    sandbox["console"].write_text(
        PASSING_CONSOLE.replace("APPLIANCE_SMOKE_EXIT: 0\n", ""), encoding="utf-8"
    )

    assert (
        reason_code_of(sandbox, "--image", str(sandbox["image"]))
        == "guest_completion_missing"
    )


def test_the_latest_pointer_names_the_most_recent_run(sandbox):
    output = sandbox["tmp"] / "evidence"
    without_tool(sandbox, "xorriso")

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))
    second = run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    latest = (output / "latest.txt").read_text(encoding="utf-8").strip()
    assert latest in [run.name for run in evidence_runs(output)], latest
    assert latest in second.stderr + second.stdout or latest, latest


# --- the guest's record travels on a channel nothing else writes to ---------
#
# Two real aarch64 runs failed with no diagnosis because the tier logged to the
# boot console, which agetty claims and revokes. The record now has its own
# virtio-serial port, and these cases hold the driver to reading that.


def test_the_result_names_the_channel_the_record_came_from(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    result = run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "record_channel: dedicated" in (only_run(output) / "result.txt").read_text()


def test_the_record_is_preserved_beside_the_serial_log(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    run_driver(sandbox, "--image", str(sandbox["image"]), "--output", str(output))

    run = only_run(output)
    assert (run / "evidence.log").is_file(), sorted(item.name for item in run.iterdir())
    assert "RESULT: PASS" in (run / "evidence.log").read_text()


def test_a_pass_on_the_shared_console_cannot_override_the_record(sandbox):
    """The console is not the verdict, whatever it happens to contain."""

    evidence = sandbox["tmp"] / "evidence-fixture.txt"
    evidence.write_text(FAILING_CONSOLE, encoding="utf-8")

    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        EMS_TEST_EVIDENCE_FIXTURE=str(evidence),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "RESULT: FAIL" in result.stderr


def test_a_guest_that_never_reached_the_port_is_read_from_the_console_and_said_so(sandbox):
    output = sandbox["tmp"] / "evidence"
    output.mkdir()

    result = run_driver(
        sandbox,
        "--image",
        str(sandbox["image"]),
        "--output",
        str(output),
        EMS_TEST_EVIDENCE_CHANNEL="console",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "record_channel: console" in (only_run(output) / "result.txt").read_text()
    assert "delivered no record" in result.stderr


def test_the_driver_gives_the_guest_a_port_of_its_own():
    assert "virtio-serial-pci" in text()
    assert "virtserialport" in text()
    assert "guest-evidence.sh" in text()


def test_the_guest_tier_is_never_redirected_to_the_login_console():
    for line in text().splitlines():
        if "guest-smoke.sh" in line:
            assert "> /dev/ttyAMA0" not in line, line
