# SPDX-License-Identifier: AGPL-3.0-or-later
"""The boot selector: ``autoboot.txt`` and the one-shot tryboot transaction.

This is deliberately not a general Raspberry Pi configuration-file editor. The
selector is a small project-generated file with exactly one meaning — which
partition boots normally and which partition boots once under tryboot — and this
module only understands that form. Anything else is refused rather than
reinterpreted, because a selector nobody can parse exactly is a selector nobody
can prove is safe.

The file is always regenerated in full. A parser that had to round-trip
unrelated directives would be a second, weaker authority over the same file.
"""

import os
import re
from dataclasses import dataclass

SELECTOR_NAME = "autoboot.txt"

SECTION_ALL = "all"
SECTION_TRYBOOT = "tryboot"

HEADER = "# ems-appliance boot selector. Generated; do not edit.\n"

_SECTION = re.compile(r"^\[([A-Za-z0-9_]+)\]$")
_DIRECTIVE = re.compile(r"^([a-z_][a-z0-9_]*)=(.*)$")

# Anything outside this set changes what the firmware does with a partition this
# project believes it controls, so an unknown directive is a refusal.
KNOWN_DIRECTIVES = frozenset({"tryboot_a_b", "boot_partition"})

MIN_PARTITION = 1
MAX_PARTITION = 128


class SelectorError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Selector:
    """The semantic state of ``autoboot.txt``."""

    default_partition: int
    tryboot_partition: int
    tryboot_a_b: bool = True

    def to_dict(self):
        return {
            "default_partition": self.default_partition,
            "tryboot_partition": self.tryboot_partition,
            "tryboot_a_b": self.tryboot_a_b,
        }


def _partition_number(raw, *, directive):
    text = str(raw).strip()
    if not text.isdigit():
        raise SelectorError(
            "selector_partition_invalid", f"{directive} must be a partition number"
        )
    value = int(text)
    if not MIN_PARTITION <= value <= MAX_PARTITION:
        raise SelectorError(
            "selector_partition_invalid", f"{directive}={value} is outside the partition range"
        )
    return value


def parse_selector(text):
    """Return the ``Selector`` a project-generated ``autoboot.txt`` describes."""

    sections = {}
    current = None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section = _SECTION.match(line)
        if section:
            current = section.group(1).lower()
            if current not in (SECTION_ALL, SECTION_TRYBOOT):
                raise SelectorError(
                    "selector_section_unsupported",
                    f"[{current}] is not a section this appliance generates",
                )
            if current in sections:
                raise SelectorError(
                    "selector_section_duplicate", f"[{current}] appears more than once"
                )
            sections[current] = {}
            continue
        directive = _DIRECTIVE.match(line)
        if not directive or current is None:
            raise SelectorError(
                "selector_line_unsupported", "the boot selector contains an unsupported line"
            )
        key, value = directive.group(1), directive.group(2).strip()
        if key not in KNOWN_DIRECTIVES:
            raise SelectorError("selector_directive_unknown", f"{key} is not an allowed directive")
        if key in sections[current]:
            raise SelectorError(
                "selector_directive_duplicate", f"{key} appears twice in [{current}]"
            )
        sections[current][key] = value

    if SECTION_ALL not in sections or SECTION_TRYBOOT not in sections:
        raise SelectorError(
            "selector_sections_missing", "the boot selector needs an [all] and a [tryboot] section"
        )
    if "tryboot_a_b" in sections[SECTION_TRYBOOT]:
        raise SelectorError(
            "selector_directive_unknown", "tryboot_a_b belongs in [all], not in [tryboot]"
        )
    if sections[SECTION_ALL].get("tryboot_a_b") != "1":
        raise SelectorError(
            "selector_not_ab", "the boot selector does not enable tryboot_a_b=1"
        )

    default = _partition_number(
        sections[SECTION_ALL].get("boot_partition"), directive="[all] boot_partition"
    )
    trial = _partition_number(
        sections[SECTION_TRYBOOT].get("boot_partition"), directive="[tryboot] boot_partition"
    )
    if default == trial:
        raise SelectorError(
            "selector_slots_identical",
            "the default and the trial boot partition must not be the same partition",
        )
    return Selector(default_partition=default, tryboot_partition=trial)


def render_selector(selector):
    """The exact bytes this appliance writes for ``selector``."""

    return (
        HEADER
        + "[all]\n"
        + "tryboot_a_b=1\n"
        + f"boot_partition={selector.default_partition}\n"
        + "\n"
        + "[tryboot]\n"
        + f"boot_partition={selector.tryboot_partition}\n"
    )


def read_selector(path):
    try:
        text = open(path, encoding="utf-8", errors="strict").read()
    except OSError as exc:
        raise SelectorError("selector_unreadable", f"the boot selector could not be read: {exc}")
    except UnicodeDecodeError:
        raise SelectorError("selector_unreadable", "the boot selector is not text")
    return parse_selector(text)


def write_selector(path, selector, *, sync_dir=True):
    """Replace the selector atomically and prove the result by re-reading it.

    FAT has no rename-over-open semantics worth relying on, so this is the
    strongest ordering the filesystem offers: write a temporary file, flush it,
    rename it over the target, flush the directory, then parse the file back and
    compare it to what was asked for. A selector that does not read back as the
    requested state is a failure, never a warning.
    """

    target = str(path)
    directory = os.path.dirname(target) or "."
    staged = os.path.join(directory, f".{os.path.basename(target)}.staged")
    payload = render_selector(selector)
    try:
        handle = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(handle, payload.encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(staged, target)
        if sync_dir:
            _sync_directory(directory)
    except OSError as exc:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise SelectorError("selector_write_failed", f"the boot selector could not be written: {exc}")

    observed = read_selector(target)
    if observed != selector:
        raise SelectorError(
            "selector_readback_mismatch",
            "the boot selector did not read back as the state that was written",
        )
    return observed


def _sync_directory(directory):
    """Flush the directory entry. FAT may refuse the open; that is not fatal."""

    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(handle)
    except OSError:
        return False
    finally:
        os.close(handle)
    return True


# --- the tryboot transaction ------------------------------------------------

REBOOT_TRYBOOT_ARGUMENT = "0 tryboot"
# systemd 253 dropped the positional argument to "systemctl reboot"; the image
# is Debian 13, so only the option form reaches reboot(2).
REBOOT_TRYBOOT_OPTION = f"--reboot-argument={REBOOT_TRYBOOT_ARGUMENT}"


class SelectorTransaction:
    """Arm a one-shot trial boot, and commit a slot that proved itself.

    The two operations are deliberately different shapes. Arming never touches
    the default slot: it only points ``[tryboot]`` at the target, so a trial that
    does not commit simply does not survive the next boot. Committing swaps both
    entries, which makes the slot that was default the rollback candidate.

    The selector lives on its own FAT partition, mounted read-only. Each
    transaction remounts it writable for exactly the write and puts it back, so
    the file that decides which slot boots is not sitting on a writable
    filesystem for the rest of the appliance's life.
    """

    def __init__(self, path, *, runner=None, mountpoint=None, remount=True):
        self.path = str(path)
        self.runner = runner
        self.mountpoint = str(mountpoint or os.path.dirname(self.path))
        self.remount = bool(remount)

    # --- mount handling --------------------------------------------------

    def _mount(self, options):
        if not self.remount:
            return True
        if self.runner is None or not self.runner.available("mount"):
            raise SelectorError(
                "selector_remount_unavailable",
                "mount is not available, so the boot selector cannot be made writable",
            )
        result = self.runner.run(
            "mount", ["-o", f"remount,{options}", self.mountpoint], timeout=30
        )
        if not result.ok:
            raise SelectorError(
                "selector_remount_failed",
                f"the selector partition could not be remounted {options}",
            )
        return True

    def _writable(self):
        return self._mount("rw")

    def _read_only(self):
        try:
            return self._mount("ro")
        except SelectorError:
            # The selector was written and verified; leaving the partition
            # writable is worth reporting but must not undo a correct write.
            return False

    # --- operations ------------------------------------------------------

    def read(self):
        return read_selector(self.path)

    def arm_trial(self, *, default_partition, trial_partition):
        """Point ``[tryboot]`` at the target while ``[all]`` stays where it is."""

        current = self.read()
        if current.default_partition != default_partition:
            raise SelectorError(
                "selector_default_unexpected",
                f"the selector defaults to partition {current.default_partition}, "
                f"the operation was planned against {default_partition}",
            )
        if default_partition == trial_partition:
            raise SelectorError(
                "selector_slots_identical",
                "a trial boot must target the other slot",
            )
        wanted = Selector(
            default_partition=default_partition, tryboot_partition=trial_partition
        )
        self._writable()
        try:
            written = write_selector(self.path, wanted)
        finally:
            self._read_only()
        if written.default_partition != default_partition:
            raise SelectorError(
                "selector_default_changed",
                "arming the trial boot changed the default slot; this must never happen",
            )
        return written

    def commit(self, *, target_partition, previous_partition):
        """Make the trial slot the default and the previous slot the fallback."""

        if target_partition == previous_partition:
            raise SelectorError(
                "selector_slots_identical", "a commit must name two different partitions"
            )
        wanted = Selector(
            default_partition=target_partition, tryboot_partition=previous_partition
        )
        self._writable()
        try:
            written = write_selector(self.path, wanted)
        finally:
            self._read_only()
        # Re-read through a fresh parse rather than trusting the write's own
        # return value: the commit is the moment the appliance stops being able
        # to fall back automatically, so it is verified from the file.
        observed = self.read()
        if observed != wanted or observed != written:
            raise SelectorError(
                "selector_commit_unverified",
                "the committed selector does not read back as the requested state",
            )
        return observed


def request_trial_reboot(runner):
    """Ask the firmware for exactly one trial boot.

    The argument is a fixed constant. No caller, and certainly no browser, ever
    supplies a reboot string.
    """

    if runner is None or not runner.available("systemctl"):
        raise SelectorError(
            "tryboot_reboot_unavailable", "systemctl is not available to request a trial boot"
        )
    result = runner.run("systemctl", ["reboot", REBOOT_TRYBOOT_OPTION], timeout=30)
    if not result.ok:
        raise SelectorError(
            "tryboot_reboot_failed", "the firmware did not accept the one-shot trial boot request"
        )
    return True
