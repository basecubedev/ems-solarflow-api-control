# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pinned rpi-image-gen contract, and whether a checkout satisfies it.

``image-rota`` owns the partition table, the slot identities and the mechanism
that shares state between slots. That makes the generator's revision part of
this appliance's definition rather than a build-host detail, so it is pinned in
``rpi-image-gen.lock`` and a checkout is verified against it before anything is
built.

Everything here is read-only. The checkout directory comes from a build
operator's command line or the build environment, never from a request: nothing
in the agent or web API accepts a generator path.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = "rpi-image-gen.lock"

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"

REASON_UNAVAILABLE = "rpi_image_gen_unavailable"
REASON_INCOMPATIBLE = "rpi_image_gen_incompatible"
REASON_DEPENDENCIES = "rpi_image_gen_dependencies_missing"

LAYER_NAME_KEY = "X-Env-Layer-Name:"
LAYER_VERSION_KEY = "X-Env-Layer-Version:"


class ImageGenError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Lock:
    repository: str
    release: str
    commit: str
    executable: str
    image_layer: str
    image_layer_version: str
    image_layer_path: str
    shared_slot_mechanism: str
    shared_slot_conf_dir: str
    shared_root: str
    persistent_mountpoint: str
    boot_mountpoint: str
    bootconfig_mountpoint: str
    machine_id_source: str
    slot_device_prefix: str
    update_archive: str
    update_members: tuple
    partition_labels: dict
    required_paths: tuple = ()
    refused_paths: tuple = ()
    host_dependencies_file: str = "depends"

    def to_dict(self):
        return {
            "repository": self.repository,
            "release": self.release,
            "commit": self.commit,
            "executable": self.executable,
            "image_layer": self.image_layer,
            "image_layer_version": self.image_layer_version,
            "shared_slot_mechanism": self.shared_slot_mechanism,
            "shared_root": self.shared_root,
            "persistent_mountpoint": self.persistent_mountpoint,
            "update_archive": self.update_archive,
            "update_members": list(self.update_members),
        }


def read_lock(path=None):
    target = Path(path) if path is not None else default_lock_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ImageGenError("lock_unreadable", f"the rpi-image-gen lock could not be read: {exc}")
    except ValueError:
        raise ImageGenError("lock_invalid", "the rpi-image-gen lock is not valid JSON")
    try:
        return Lock(
            repository=str(payload["repository"]),
            release=str(payload["release"]),
            commit=str(payload["commit"]),
            executable=str(payload["executable"]),
            image_layer=str(payload["image_layer"]),
            image_layer_version=str(payload["image_layer_version"]),
            image_layer_path=str(payload["image_layer_path"]),
            shared_slot_mechanism=str(payload["shared_slot_mechanism"]),
            shared_slot_conf_dir=str(payload["shared_slot_conf_dir"]),
            shared_root=str(payload["shared_root"]),
            persistent_mountpoint=str(payload["persistent_mountpoint"]),
            boot_mountpoint=str(payload["boot_mountpoint"]),
            bootconfig_mountpoint=str(payload["bootconfig_mountpoint"]),
            machine_id_source=str(payload["machine_id_source"]),
            slot_device_prefix=str(payload["slot_device_prefix"]),
            update_archive=str(payload["update_archive"]),
            update_members=tuple(payload["update_members"]),
            partition_labels=dict(payload["partition_labels"]),
            required_paths=tuple(payload.get("required_paths") or ()),
            refused_paths=tuple(payload.get("refused_paths") or ()),
            host_dependencies_file=str(payload.get("host_dependencies_file") or "depends"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ImageGenError("lock_invalid", f"the rpi-image-gen lock is incomplete: {exc}")


def default_lock_path():
    return Path(__file__).resolve().parents[1] / "packaging" / "appliance" / "image" / LOCK_NAME


@dataclass
class Finding:
    check: str
    result: str
    detail: str = ""

    def to_dict(self):
        return {"check": self.check, "result": self.result, "detail": self.detail}


@dataclass(frozen=True)
class Compatibility:
    findings: tuple = ()
    missing_dependencies: tuple = ()
    reason: str = ""
    revision: str = ""

    @property
    def compatible(self):
        return not any(finding.result == FAIL for finding in self.findings)

    @property
    def buildable(self):
        return self.compatible and not self.missing_dependencies

    def to_dict(self):
        return {
            "compatible": self.compatible,
            "buildable": self.buildable,
            "reason": self.reason,
            "revision": self.revision,
            "missing_dependencies": list(self.missing_dependencies),
            "findings": [finding.to_dict() for finding in self.findings],
            "counts": _counts(self.findings),
        }


def _counts(findings):
    counts = {PASS: 0, FAIL: 0, NOT_RUN: 0}
    for finding in findings:
        counts[finding.result] = counts.get(finding.result, 0) + 1
    return counts


def layer_metadata(text):
    """The ``X-Env-`` header block of an rpi-image-gen layer file."""

    name = version = ""
    for raw in str(text).splitlines():
        line = raw.strip().lstrip("#").strip()
        if line.startswith(LAYER_NAME_KEY):
            name = line[len(LAYER_NAME_KEY) :].strip()
        elif line.startswith(LAYER_VERSION_KEY):
            version = line[len(LAYER_VERSION_KEY) :].strip()
    return name, version


def host_dependencies(directory, lock, *, which=None):
    """The upstream ``depends`` entries this host cannot satisfy.

    Only executables are resolved. Package-only entries are reported as not
    resolvable here rather than guessed at, because a wrong guess would turn a
    host that cannot build into one that claims it can.
    """

    which = which or shutil.which
    path = Path(directory) / lock.host_dependencies_file
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return (), ()
    resolved, missing = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        binary = parts[1] if len(parts) > 1 else ""
        package = parts[2] if len(parts) > 2 and parts[2] else binary
        if not binary:
            continue
        if which(binary):
            resolved.append(binary)
        else:
            missing.append(package or binary)
    return tuple(resolved), tuple(sorted(set(missing)))


def probe_checkout(directory, lock=None, *, which=None):
    """Check one checkout against the pinned contract. Nothing is executed."""

    lock = lock or read_lock()
    root = Path(directory)
    findings = []

    if not root.is_dir():
        return Compatibility(
            findings=(Finding("checkout", FAIL, f"{root} is not a directory"),),
            reason=REASON_UNAVAILABLE,
        )

    executable = root / lock.executable
    if not executable.is_file():
        findings.append(
            Finding("executable", FAIL, f"{lock.executable} is missing from {root}")
        )
    elif not executable.stat().st_mode & 0o111:
        findings.append(Finding("executable", FAIL, f"{lock.executable} is not executable"))
    else:
        findings.append(Finding("executable", PASS, str(executable)))

    for relative in lock.required_paths:
        target = root / relative
        findings.append(
            Finding(f"path:{relative}", PASS if target.exists() else FAIL, str(target))
        )

    for relative in lock.refused_paths:
        present = (root / relative).exists()
        findings.append(
            Finding(
                f"refused:{relative}",
                FAIL if present else PASS,
                "an interface this project does not build against" if present else "absent",
            )
        )

    layer = root / lock.image_layer_path
    if layer.is_file():
        name, version = layer_metadata(layer.read_text(encoding="utf-8", errors="replace"))
        findings.append(
            Finding(
                "image_layer",
                PASS if name == lock.image_layer else FAIL,
                f"{name or 'unnamed'} (expected {lock.image_layer})",
            )
        )
        findings.append(
            Finding(
                "image_layer_version",
                PASS if version == lock.image_layer_version else FAIL,
                f"{version or 'unversioned'} (expected {lock.image_layer_version})",
            )
        )
    else:
        findings.append(Finding("image_layer", FAIL, f"{lock.image_layer_path} is missing"))
        findings.append(Finding("image_layer_version", NOT_RUN, "the layer file is missing"))

    findings.append(_shared_slot_finding(root, lock))
    findings.append(_update_finding(root, lock))

    revision = _revision(root)
    if not revision:
        findings.append(
            Finding("revision", NOT_RUN, "the checkout carries no git metadata to compare")
        )
    else:
        findings.append(
            Finding(
                "revision",
                PASS if revision == lock.commit else FAIL,
                f"{revision} (pinned {lock.commit})",
            )
        )

    missing = host_dependencies(root, lock, which=which)[1]
    reason = ""
    if any(finding.result == FAIL for finding in findings):
        reason = REASON_INCOMPATIBLE
    elif missing:
        reason = REASON_DEPENDENCIES
    return Compatibility(
        findings=tuple(findings),
        missing_dependencies=missing,
        reason=reason,
        revision=revision,
    )


def _shared_slot_finding(root, lock):
    generator = (
        root
        / "image/gpt/ab_userdata/device/rootfs-overlay/usr/lib/systemd/system-generators"
        / f"{lock.shared_slot_mechanism}-generator"
    )
    if not generator.is_file():
        return Finding(
            "shared_slot", FAIL, f"{lock.shared_slot_mechanism}-generator is missing"
        )
    text = generator.read_text(encoding="utf-8", errors="replace")
    if lock.shared_slot_conf_dir not in text or lock.shared_root not in text:
        return Finding(
            "shared_slot",
            FAIL,
            f"the generator does not read {lock.shared_slot_conf_dir} into {lock.shared_root}",
        )
    return Finding("shared_slot", PASS, str(generator))


def _update_finding(root, lock):
    script = root / "image/gpt/ab_userdata/post-image.sh"
    if not script.is_file():
        return Finding("update_artifact", FAIL, "post-image.sh is missing")
    text = script.read_text(encoding="utf-8", errors="replace")
    if lock.update_archive not in text:
        return Finding(
            "update_artifact", FAIL, f"post-image.sh does not produce {lock.update_archive}"
        )
    missing = [name for name in lock.update_members if f"/{name}" not in text]
    if missing:
        return Finding(
            "update_artifact", FAIL, f"the archive does not carry {', '.join(missing)}"
        )
    return Finding("update_artifact", PASS, f"{lock.update_archive} ({', '.join(lock.update_members)})")


def _revision(root):
    head = Path(root) / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if text.startswith("ref:"):
        reference = (Path(root) / ".git" / text.split(" ", 1)[1].strip()).resolve()
        try:
            return reference.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return text
