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
import subprocess

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

from pathlib import Path  # noqa: E402  (after pytestmark on purpose)

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "appliance-smoke-arm64.sh"
GUEST = ROOT / "scripts" / "appliance-guest-smoke.sh"

EXIT_NOT_RUN = 3

QEMU_STUB = """#!/bin/sh
console=""
for argument in "$@"; do
    case "$argument" in
        file:*) console=${argument#file:} ;;
    esac
done
[ -n "$console" ] || exit 1
cat "${EMS_TEST_CONSOLE_FIXTURE:-/dev/null}" > "$console"
exit 0
"""

QEMU_IMG_STUB = """#!/bin/sh
if [ "$1" = "info" ]; then
    printf '{ "virtual-size": 2147483648, "format": "%s" }\\n' "${EMS_TEST_IMAGE_FORMAT:-qcow2}"
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

    return {
        "stubs": stubs,
        "firmware": firmware,
        "image": image,
        "console": console,
        "mirror": mirror,
        "keyring": keyring,
        "tmp": tmp_path,
    }


def run_driver(sandbox, *arguments, **environment):
    env = dict(os.environ)
    env["PATH"] = f"{sandbox['stubs']}:{env.get('PATH', '')}"
    env["EMS_ARM64_FIRMWARE"] = str(sandbox["firmware"] / "AAVMF_CODE.fd")
    env["EMS_ARM64_FIRMWARE_VARS"] = str(sandbox["firmware"] / "AAVMF_VARS.fd")
    env["EMS_TEST_CONSOLE_FIXTURE"] = str(sandbox["console"])
    env["EMS_TEST_MIRROR"] = str(sandbox["mirror"])
    env["EMS_ARM64_KEYRINGS"] = str(sandbox["keyring"])
    env["TMPDIR"] = str(sandbox["tmp"])
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


def seed_mirror(sandbox, *, entry_name=None, checksum=None, with_signature=True):
    """Publish an image plus a checksum manifest the driver can download."""

    image = sandbox["mirror"] / "debian-13-genericcloud-arm64.qcow2"
    image.write_bytes(sandbox["image"].read_bytes())
    import hashlib

    digest = checksum or hashlib.sha512(image.read_bytes()).hexdigest()
    manifest = sandbox["mirror"] / "SHA512SUMS"
    manifest.write_text(f"{digest}  {entry_name or image.name}\n", encoding="utf-8")
    if with_signature:
        (sandbox["mirror"] / "SHA512SUMS.sign").write_bytes(b"signature")
    return image


def download(sandbox, *arguments, **environment):
    return run_driver(
        sandbox,
        *arguments,
        EMS_ARM64_IMAGE_BASE="https://example.invalid/images",
        **environment,
    )


def test_a_signed_manifest_with_a_matching_checksum_runs(sandbox):
    seed_mirror(sandbox)

    result = download(sandbox)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "UNVERIFIED INPUT" not in result.stdout + result.stderr


def test_a_manifest_whose_signature_does_not_verify_is_refused(sandbox):
    seed_mirror(sandbox)

    result = download(sandbox, EMS_TEST_GPGV_RC="1")

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr
    assert "RESULT: NOT RUN" in result.stderr


def test_a_signed_manifest_with_a_wrong_image_checksum_is_refused(sandbox):
    seed_mirror(sandbox, checksum="0" * 128)

    result = download(sandbox)

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_a_manifest_without_an_entry_for_the_image_is_refused(sandbox):
    seed_mirror(sandbox, entry_name="some-other-image.qcow2")

    result = download(sandbox)

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_a_missing_signature_is_refused_without_the_override(sandbox):
    seed_mirror(sandbox, with_signature=False)

    result = download(sandbox)

    assert result.returncode == EXIT_NOT_RUN, result.stdout + result.stderr


def test_a_missing_signature_with_the_override_is_labelled(sandbox):
    seed_mirror(sandbox, with_signature=False)

    result = download(sandbox, "--allow-unverified-image")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "UNVERIFIED INPUT" in combined, combined


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
