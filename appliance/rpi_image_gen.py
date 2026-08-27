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

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from appliance import image_shape

LOCK_NAME = "rpi-image-gen.lock"
SOURCE_IDENTITY_NAME = ".rpi-image-gen-source.json"

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"

REASON_UNAVAILABLE = "rpi_image_gen_unavailable"
REASON_INCOMPATIBLE = "rpi_image_gen_incompatible"
REASON_DEPENDENCIES = "rpi_image_gen_dependencies_missing"
REASON_SOURCE_UNVERIFIED = "rpi_image_gen_source_unverified"
REASON_SOURCE_MODIFIED = "rpi_image_gen_source_modified"

# Directories a build reads its definition from. An untracked file under any of
# them can shadow a build input without changing a single tracked byte, which is
# indistinguishable from the pinned upstream by any check that only reads HEAD.
BUILD_CRITICAL_ROOTS = ("bin", "config", "device", "image", "layer", "site")

# git(1) is a build-host tool. It is deliberately not on the agent's allowlist —
# nothing in the agent or the web API resolves a source tree — so the build path
# gets its own runner rather than widening the runtime's.
GIT_EXECUTABLES = ("/usr/bin/git", "/bin/git", "/usr/local/bin/git")

# Written by tools that read the tree, never read by a build as an input.
TREE_IGNORED_NAMES = ("__pycache__", ".git", ".mypy_cache", ".pytest_cache")
TREE_IGNORED_SUFFIXES = (".pyc", ".pyo")

LAYER_NAME_KEY = "X-Env-Layer-Name:"
LAYER_VERSION_KEY = "X-Env-Layer-Version:"

SOURCE_GIT = "git"
SOURCE_TARBALL = "tarball"
SOURCE_UNVERIFIED = "unverified"

# "ask this host's package database" as distinct from "there is none to ask".
AUTO_PACKAGE_QUERY = object()


@dataclass(frozen=True)
class HardwareProfile:
    """One board this project builds a distinct image for.

    ``device_layer`` is upstream's layer name -- ``rpi5``, not the ``pi5``
    directory it lives in. ``device_class`` is what that layer sets
    ``IGconf_device_class`` to. The compatible board classes are derived from
    the device layer rather than declared beside it, so an artefact cannot claim
    hardware its kernel and firmware were not built for.
    """

    name: str
    device_layer: str
    device_class: str
    compatible_board_classes: tuple
    description: str

    def to_dict(self):
        return {
            "name": self.name,
            "device_layer": self.device_layer,
            "device_class": self.device_class,
            "compatible_board_classes": list(self.compatible_board_classes),
            "description": self.description,
        }


HARDWARE_PROFILES = {
    "rpi3": HardwareProfile(
        name="rpi3",
        device_layer="rpi3",
        device_class="pi3",
        compatible_board_classes=("pi3",),
        description="Raspberry Pi 3 Model B / B+",
    ),
    "rpi4": HardwareProfile(
        name="rpi4",
        device_layer="rpi4",
        device_class="pi4",
        compatible_board_classes=("pi4",),
        description="Raspberry Pi 4 Model B",
    ),
    "rpi5": HardwareProfile(
        name="rpi5",
        device_layer="rpi5",
        device_class="pi5",
        compatible_board_classes=("pi5",),
        description="Raspberry Pi 5",
    ),
}

# The device-tree ``compatible`` tokens each board class answers to. A board
# that is not in here is not one this project has an image for, and an unknown
# board must block an update rather than be guessed at: a Pi 4 kernel written to
# a Pi 5 does not boot, and the appliance would be recoverable only by reflashing.
BOARD_CLASSES = {
    "raspberrypi,3-model-b": "pi3",
    "raspberrypi,3-model-b-plus": "pi3",
    "raspberrypi,4-model-b": "pi4",
    "raspberrypi,400": "pi4",
    "raspberrypi,4-compute-module": "cm4",
    "raspberrypi,5-model-b": "pi5",
    "raspberrypi,5-compute-module": "cm5",
}

BOARD_UNKNOWN = ""


# An arm64 image built on anything else needs the kernel to hand aarch64
# binaries to an emulator. That registration is host-wide and belongs to the
# build host, so it is reported rather than arranged.
TARGET_ARCHITECTURE = "arm64"
NATIVE_MACHINES = ("aarch64", "arm64")
BINFMT_HANDLER = "/proc/sys/fs/binfmt_misc/qemu-aarch64"


class ImageGenError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LockedLayer:
    """One upstream image layer, as the lock pins it."""

    slug: str
    name: str
    path: str
    version: str


@dataclass(frozen=True)
class Lock:
    repository: str
    release: str
    commit: str
    executable: str
    image_layers: dict
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
    update_member_format: str = ""
    tree_sha256: str = ""
    tarball: dict = None

    def layer(self, slug):
        """The pinned upstream image layer the image is built from.

        Declared in the lock rather than derived from ``image_shape``: the lock
        is the file that gets reviewed, and a contract test keeps the two from
        disagreeing.
        """

        entry = self.image_layers[slug]
        return LockedLayer(
            slug=slug,
            name=str(entry["name"]),
            path=str(entry["path"]),
            version=str(entry["version"]),
        )

    @property
    def image_layer(self):
        return self.layer(image_shape.IMAGE.profile_suffix).name

    @property
    def image_layer_version(self):
        return self.layer(image_shape.IMAGE.profile_suffix).version

    @property
    def image_layer_path(self):
        return self.layer(image_shape.IMAGE.profile_suffix).path

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
            "update_member_format": self.update_member_format,
            "tarball": dict(self.tarball or {}),
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
            tree_sha256=str(payload.get("tree_sha256") or ""),
            executable=str(payload["executable"]),
            image_layers={
                str(slug): {
                    "name": str(entry["name"]),
                    "path": str(entry["path"]),
                    "version": str(entry["version"]),
                }
                for slug, entry in dict(payload["image_layers"]).items()
            },
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
            update_member_format=str(payload.get("update_member_format") or ""),
            tarball=dict(payload["tarball"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ImageGenError("lock_invalid", f"the rpi-image-gen lock is incomplete: {exc}")


def default_lock_path():
    return image_dir() / LOCK_NAME


def image_dir():
    return Path(__file__).resolve().parents[1] / "packaging" / "appliance" / "image"


def default_profile_dir():
    return image_dir() / "profiles"


def file_sha256(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


# --- hardware build profiles -------------------------------------------------


@dataclass(frozen=True)
class BuildProfile:
    """One rpi-image-gen config this project builds, and what it is for.

    A profile names a board. The image layer it is built from is one level up,
    in the shared config it includes, because naming it twice is how two files
    come to disagree about which image they build -- so that file is read and
    checked rather than trusted.
    """

    path: Path
    hardware: HardwareProfile
    image_layer: str
    image_name: str

    @property
    def artifact_suffix(self):
        return image_shape.IMAGE.artifact_suffix(self.hardware.name)

    @property
    def name(self):
        return self.path.stem

    @property
    def device_layer(self):
        return self.hardware.device_layer

    @property
    def device_class(self):
        return self.hardware.device_class

    @property
    def compatible_board_classes(self):
        return self.hardware.compatible_board_classes

    def artifact_basename(self, version):
        return f"ems-solarflow-appliance-{version}-{self.artifact_suffix}"

    def to_dict(self):
        return {
            "name": self.name,
            "config": str(self.path),
            "image_name": self.image_name,
            "artifact_suffix": self.artifact_suffix,
            **self.hardware.to_dict(),
        }


def _config_values(text):
    """The ``section.key`` scalars of a build profile.

    The profiles are two levels deep, scalar-valued and generated by this
    project, so they are read without a YAML dependency the appliance package
    does not ship. Anything else in the file is refused rather than guessed at.
    """

    values = {}
    section = ""
    for raw in str(text).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            head, _, tail = line.partition(":")
            section = head.strip()
            if tail.strip():
                values[section] = tail.strip()
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            raise ImageGenError("profile_invalid", f"{line.strip()!r} is not a key: value pair")
        values[f"{section}.{key.strip()}"] = value.strip()
    return values


def read_profile(path):
    """Load one build profile and resolve the hardware it is for."""

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageGenError("profile_unreadable", f"{target} could not be read: {exc}")

    values = _config_values(text)
    layer = values.get("device.layer", "")
    if not layer:
        raise ImageGenError("profile_invalid", f"{target.name} declares no device layer")
    hardware = next(
        (item for item in HARDWARE_PROFILES.values() if item.device_layer == layer), None
    )
    if hardware is None:
        raise ImageGenError(
            "profile_hardware_unknown",
            f"{target.name} selects device layer {layer!r}, which this project has no profile for",
        )
    return BuildProfile(
        path=target,
        hardware=hardware,
        image_layer=_profile_image_layer(target, values),
        image_name=values.get("image.name", ""),
    )


def _profile_image_layer(target, values):
    """The image layer this profile builds, read from the config it includes.

    The profile itself names only a board. The image layer is one level up, in
    the shared config, which is also the file that has to be right for the
    build to be the image it claims -- so that is where the answer is taken
    from, and an unrecognised one is refused rather than defaulted.
    """

    included = values.get("include.file", "")
    if not included:
        raise ImageGenError("profile_invalid", f"{target.name} includes no shared configuration")
    shared = (target.parent / included).resolve()
    try:
        text = shared.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageGenError(
            "profile_unreadable", f"{target.name} includes {included}, which could not be read: {exc}"
        )
    layer = _config_values(text).get("image.layer", "")
    if not image_shape.image_layer_matches(layer):
        raise ImageGenError(
            "profile_image_layer_unknown",
            f"{shared.name} builds image layer {layer or 'nothing'!r}, "
            f"and this project builds {image_shape.IMAGE.image_layer}",
        )
    return layer


def profiles(directory=None):
    """Every build profile this project ships, by name."""

    root = Path(directory) if directory is not None else default_profile_dir()
    if not root.is_dir():
        return {}
    found = {}
    for candidate in sorted(root.glob("*.yaml")):
        profile = read_profile(candidate)
        found[profile.name] = profile
    return found


def board_class(compatible):
    """The bounded board class a device-tree ``compatible`` string names.

    ``compatible`` is NUL-separated, most specific first. An unrecognised board
    resolves to nothing, which is what blocks an update: guessing here would
    write an image built for another SoC.
    """

    for token in str(compatible or "").replace("\x00", "\n").splitlines():
        entry = token.strip()
        if entry in BOARD_CLASSES:
            return BOARD_CLASSES[entry]
    return BOARD_UNKNOWN


def detect_board_class(root="/"):
    """This host's board class, read from the device tree only."""

    try:
        raw = (Path(root) / "proc/device-tree/compatible").read_text(
            encoding="utf-8", errors="replace"
        )
    except (OSError, ValueError):
        return BOARD_UNKNOWN
    return board_class(raw)


def board_has_an_image(board):
    """Is there any appliance image at all for this board?

    Distinct from installability: a Raspberry Pi 3 has an image and no way to
    be updated with one, and the difference is what an operator is told.
    """

    wanted = str(board or "")
    return bool(wanted) and any(
        wanted in profile.compatible_board_classes for profile in HARDWARE_PROFILES.values()
    )


# --- upstream host dependencies ---------------------------------------------


@dataclass(frozen=True)
class Dependency:
    """One ``category:binary:package`` entry of upstream's ``depends``."""

    category: str
    binary: str
    package: str

    @property
    def package_only(self):
        return not self.binary


def parse_dependencies(text):
    """Every entry upstream declares, including the ones with no binary.

    ``all::python3`` and ``build::python3-jsonschema`` name a package with
    nothing to look for on ``PATH``. Eleven of the pinned release's entries have
    that shape, and skipping them is what lets a host that cannot build report
    that it can.
    """

    entries = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        category = parts[0].strip()
        binary = parts[1].strip() if len(parts) > 1 else ""
        package = parts[2].strip() if len(parts) > 2 else ""
        if not binary and not package:
            continue
        entries.append(
            Dependency(category=category, binary=binary, package=package or binary)
        )
    return tuple(entries)


def _installed(package, *, package_query):
    if package_query is None:
        return None
    try:
        return bool(package_query(package))
    except Exception:
        return None


def default_package_query(runner=None):
    """``dpkg-query`` with a fixed argv, or nothing on a host without dpkg.

    Routed through the one allowlisted command runner rather than a bare
    subprocess, so this module starts no host process of its own.
    """

    from appliance.commands import CommandRunner

    runner = runner or CommandRunner()
    if not runner.available("dpkg-query"):
        return None

    def query(package):
        result = runner.run(
            "dpkg-query",
            ["-W", "-f=${db:Status-Status}", "--", str(package)],
            timeout=15,
        )
        return result.ok and result.stdout.strip() == "installed"

    return query


@dataclass(frozen=True)
class DependencyReport:
    """What this host is missing, split so a NOT RUN is actionable."""

    resolved: tuple = ()
    missing_binaries: tuple = ()
    missing_packages: tuple = ()
    unverified_packages: tuple = ()

    @property
    def missing(self):
        return tuple(sorted(set(self.missing_binaries) | set(self.missing_packages)))

    @property
    def satisfied(self):
        return not self.missing_binaries and not self.missing_packages

    def to_dict(self):
        return {
            "satisfied": self.satisfied,
            "resolved": list(self.resolved),
            "missing_binaries": list(self.missing_binaries),
            "missing_packages": list(self.missing_packages),
            "unverified_packages": list(self.unverified_packages),
        }


def probe_dependencies(directory, lock, *, which=None, package_query=None):
    """Resolve every upstream dependency entry: binaries and packages alike."""

    which = which or shutil.which
    path = Path(directory) / lock.host_dependencies_file
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return DependencyReport()

    resolved, missing_binaries, missing_packages, unverified = [], [], [], []
    for entry in parse_dependencies(raw):
        if entry.binary:
            if which(entry.binary):
                resolved.append(entry.binary)
            else:
                missing_binaries.append(entry.package)
            continue
        installed = _installed(entry.package, package_query=package_query)
        if installed is True:
            resolved.append(entry.package)
        elif installed is False:
            missing_packages.append(entry.package)
        else:
            # No package database to ask. Reported rather than assumed present:
            # a build that needs python3-debian fails long after it started.
            unverified.append(entry.package)
    return DependencyReport(
        resolved=tuple(resolved),
        missing_binaries=tuple(sorted(set(missing_binaries))),
        missing_packages=tuple(sorted(set(missing_packages))),
        unverified_packages=tuple(sorted(set(unverified))),
    )


def host_dependencies(directory, lock, *, which=None, package_query=AUTO_PACKAGE_QUERY):
    """The upstream ``depends`` entries this host cannot satisfy."""

    if package_query is AUTO_PACKAGE_QUERY:
        package_query = default_package_query()
    report = probe_dependencies(
        directory, lock, which=which, package_query=package_query
    )
    missing = set(report.missing) | set(report.unverified_packages)
    return report.resolved, tuple(sorted(missing))


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
    source_identity: str = SOURCE_UNVERIFIED
    dependencies: DependencyReport = None
    tree_digest: str = ""

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
            "source_identity": self.source_identity,
            "tree_digest": self.tree_digest,
            "dependencies": (self.dependencies or DependencyReport()).to_dict(),
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




@dataclass(frozen=True)
class BuildHost:
    """Whether this host can cross-build the appliance image, and what is missing.

    The classes are separate because the fixes are: a missing binary is a
    package, a missing binfmt registration is a host-wide kernel setting, and an
    unsupported architecture is neither. A NOT RUN that says which one it is can
    be acted on; "dependencies missing" cannot.
    """

    machine: str = ""
    missing_binaries: tuple = ()
    missing_packages: tuple = ()
    unverified_packages: tuple = ()
    missing_binfmt: tuple = ()
    unsupported_architecture: str = ""

    @property
    def buildable(self):
        return not (
            self.missing_binaries
            or self.missing_packages
            or self.unverified_packages
            or self.missing_binfmt
            or self.unsupported_architecture
        )

    def to_dict(self):
        return {
            "buildable": self.buildable,
            "machine": self.machine,
            "missing_binaries": list(self.missing_binaries),
            "missing_packages": list(self.missing_packages),
            "unverified_packages": list(self.unverified_packages),
            "missing_binfmt": list(self.missing_binfmt),
            "unsupported_architecture": self.unsupported_architecture,
        }


def build_host_state(dependencies=None, *, machine=None, binfmt_path=BINFMT_HANDLER):
    """Everything between this host and a real image, in separable classes."""

    import platform

    machine = machine if machine is not None else platform.machine()
    dependencies = dependencies or DependencyReport()
    missing_binfmt = ()
    unsupported = ""
    if machine not in NATIVE_MACHINES:
        # Cross-building is supported; it just needs the emulator registered.
        if not Path(binfmt_path).exists():
            missing_binfmt = (f"qemu-aarch64 ({binfmt_path} is not registered)",)
        if machine not in ("x86_64", "amd64"):
            unsupported = (
                f"{machine} can neither build {TARGET_ARCHITECTURE} natively nor "
                "emulate it through a registered binfmt handler"
            )
    return BuildHost(
        machine=machine,
        missing_binaries=dependencies.missing_binaries,
        missing_packages=dependencies.missing_packages,
        unverified_packages=dependencies.unverified_packages,
        missing_binfmt=missing_binfmt,
        unsupported_architecture=unsupported,
    )


def probe_checkout(
    directory, lock=None, *, which=None, package_query=AUTO_PACKAGE_QUERY, runner=None
):
    """Check one source tree against the pinned contract.

    Nothing of the generator is executed. ``git`` is, for a git checkout: only
    the repository can answer whether its own tree is clean and whether it holds
    the pinned commit object.
    """

    lock = lock or read_lock()
    if package_query is AUTO_PACKAGE_QUERY:
        package_query = default_package_query()
    root = Path(directory)
    findings = []

    if not root.is_dir():
        return Compatibility(
            findings=(Finding("checkout", FAIL, f"{root} is not a directory"),),
            reason=REASON_UNAVAILABLE,
            dependencies=DependencyReport(),
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

    for slug in sorted(lock.image_layers):
        pinned = lock.layer(slug)
        layer = root / pinned.path
        if not layer.is_file():
            findings.append(
                Finding(f"image_layer:{slug}", FAIL, f"{pinned.path} is missing")
            )
            findings.append(
                Finding(f"image_layer_version:{slug}", NOT_RUN, "the layer file is missing")
            )
            continue
        name, version = layer_metadata(layer.read_text(encoding="utf-8", errors="replace"))
        findings.append(
            Finding(
                f"image_layer:{slug}",
                PASS if name == pinned.name else FAIL,
                f"{name or 'unnamed'} (expected {pinned.name})",
            )
        )
        findings.append(
            Finding(
                f"image_layer_version:{slug}",
                PASS if version == pinned.version else FAIL,
                f"{version or 'unversioned'} (expected {pinned.version})",
            )
        )

    findings.append(_shared_slot_finding(root, lock))
    findings.append(_update_finding(root, lock))

    identity, revision, identity_finding, digest, identity_reason = _source_identity(
        root, lock, runner=runner
    )
    findings.append(identity_finding)

    dependencies = probe_dependencies(
        root, lock, which=which, package_query=package_query
    )
    missing = tuple(sorted(set(dependencies.missing) | set(dependencies.unverified_packages)))
    reason = ""
    if identity_finding.result == FAIL:
        # A tree that named itself correctly and then changed is a different
        # failure from a tree that could never say what it was.
        reason = identity_reason or REASON_SOURCE_UNVERIFIED
    elif any(finding.result == FAIL for finding in findings):
        reason = REASON_INCOMPATIBLE
    elif missing:
        reason = REASON_DEPENDENCIES
    return Compatibility(
        findings=tuple(findings),
        missing_dependencies=missing,
        reason=reason,
        revision=revision,
        source_identity=identity,
        dependencies=dependencies,
        tree_digest=digest,
    )


def _source_identity(root, lock, *, runner=None):
    """Prove which upstream source this tree is, in either supported form.

    A git checkout proves itself through git: the pinned commit object exists,
    HEAD is that commit, the working tree and the index are clean, and nothing
    untracked shadows a build input. Reading forty characters out of
    ``.git/HEAD`` proves none of that, and a hand-written file has exactly that
    shape.

    A release tarball has no ``.git``, so it is proven by the identity record
    ``appliance-fetch-rpi-image-gen.sh`` writes after verifying the download's
    SHA-256 — plus the tree manifest recorded beside it, recomputed here. The
    archive hash is a statement about a download; the manifest is a statement
    about the tree a build is about to read.

    Anything else is unverified, and unverified is a refusal rather than a NOT
    RUN: a source nobody can name is not the source this appliance is defined by.
    """

    if (Path(root) / ".git").exists():
        state = git_state(root, lock, runner=runner)
        if state.ok:
            return (
                SOURCE_GIT,
                state.revision,
                Finding("source_identity", PASS, f"git {state.revision} (pinned {lock.commit})"),
                "",
                "",
            )
        return (
            SOURCE_UNVERIFIED,
            state.revision,
            Finding("source_identity", FAIL, "; ".join(state.problems)),
            "",
            REASON_SOURCE_UNVERIFIED,
        )

    recorded = _recorded_identity(root)
    if not recorded:
        return (
            SOURCE_UNVERIFIED,
            "",
            Finding(
                "source_identity",
                FAIL,
                "this tree carries neither git metadata nor a verified source record; "
                "fetch it with scripts/appliance-fetch-rpi-image-gen.sh",
            ),
            "",
            REASON_SOURCE_UNVERIFIED,
        )

    expected = lock.tarball or {}
    problems = []
    if recorded.get("sha256") != expected.get("sha256"):
        problems.append("the recorded tarball digest is not the pinned one")
    if recorded.get("commit") != lock.commit:
        problems.append(f"the record names commit {recorded.get('commit')!r}")
    if recorded.get("release") != lock.release:
        problems.append(f"the record names release {recorded.get('release')!r}")
    if recorded.get("top_level_directory") != expected.get("top_level_directory"):
        problems.append("the record names another extracted directory")
    if problems:
        return (
            SOURCE_UNVERIFIED,
            str(recorded.get("commit") or ""),
            Finding("source_identity", FAIL, "; ".join(problems)),
            "",
            REASON_SOURCE_UNVERIFIED,
        )

    # The record beside the tree was written by the same fetch that extracted
    # it, so it cannot be the authority for it. The lock is reviewed and
    # committed; that is what makes it one. The record is still compared, so a
    # tree that satisfies the lock but disagrees with its own record is caught
    # too.
    declared = str(lock.tree_sha256 or recorded.get("tree_sha256") or "")
    observed = tree_digest(root)
    recorded_digest = str(recorded.get("tree_sha256") or "")
    if lock.tree_sha256 and recorded_digest and recorded_digest != lock.tree_sha256:
        return (
            SOURCE_UNVERIFIED,
            lock.commit,
            Finding(
                "source_identity",
                FAIL,
                "the source record disagrees with the pinned tree hash in "
                "rpi-image-gen.lock; fetch it again with "
                "scripts/appliance-fetch-rpi-image-gen.sh",
            ),
            observed,
            REASON_SOURCE_UNVERIFIED,
        )
    if not declared:
        return (
            SOURCE_UNVERIFIED,
            lock.commit,
            Finding(
                "source_identity",
                FAIL,
                "the source record carries no tree hash, so the extracted tree cannot be "
                "proven unmodified; fetch it again with "
                "scripts/appliance-fetch-rpi-image-gen.sh",
            ),
            observed,
            REASON_SOURCE_UNVERIFIED,
        )
    if declared != observed:
        return (
            SOURCE_UNVERIFIED,
            lock.commit,
            Finding(
                "source_identity",
                FAIL,
                f"the extracted tree hashes to {observed}, the source record declares "
                f"{declared}",
            ),
            observed,
            REASON_SOURCE_MODIFIED,
        )
    return (
        SOURCE_TARBALL,
        lock.commit,
        Finding(
            "source_identity",
            PASS,
            f"tarball {expected.get('sha256')} ({lock.release}) tree {observed}",
        ),
        observed,
        "",
    )


def _recorded_identity(root):
    try:
        payload = json.loads(
            (Path(root) / SOURCE_IDENTITY_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# --- the git source form -----------------------------------------------------


def git_runner(runner=None):
    """A command runner that may run ``git``, and nothing the agent may not."""

    if runner is not None:
        return runner
    from appliance.commands import EXECUTABLES, CommandRunner

    return CommandRunner(executables={**EXECUTABLES, "git": GIT_EXECUTABLES})


@dataclass(frozen=True)
class GitSourceState:
    """What git says about a checkout, or why git could not be asked."""

    revision: str = ""
    problems: tuple = ()

    @property
    def ok(self):
        return not self.problems


def git_state(root, lock, *, runner=None):
    """Prove a checkout against the pinned commit, using git's own answers."""

    runner = git_runner(runner)
    root = str(root)
    if not runner.available("git"):
        return GitSourceState(
            problems=(
                "git is not installed, so this checkout's identity cannot be proven",
            )
        )

    def git(*args, timeout=120):
        try:
            return runner.run("git", ["-C", root, *args], timeout=timeout)
        except Exception as exc:
            return _failure(str(exc))

    if not git("rev-parse", "--git-dir").ok:
        return GitSourceState(
            problems=(f"{root} carries .git metadata but is not a git repository",)
        )

    head = git("rev-parse", "HEAD")
    revision = (head.stdout or "").strip() if head.ok else ""
    problems = []
    if not head.ok or not revision:
        problems.append("this checkout has no resolvable HEAD")
    elif revision != lock.commit:
        problems.append(f"HEAD is {revision}, the lock pins {lock.commit}")
    if not git("cat-file", "-e", f"{lock.commit}^{{commit}}").ok:
        problems.append(f"the pinned commit object {lock.commit} is not in this repository")
    if not git("diff", "--quiet").ok:
        problems.append("tracked files are modified in the working tree")
    if not git("diff", "--cached", "--quiet").ok:
        problems.append("changes are staged in the index")

    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    if not untracked.ok:
        problems.append("untracked files could not be enumerated")
    else:
        shadowed = _shadowed_build_inputs(untracked.stdout, lock)
        if shadowed:
            problems.append(
                "untracked files shadow build inputs: " + ", ".join(shadowed[:5])
            )
    return GitSourceState(revision=revision, problems=tuple(problems))


def _failure(message):
    from appliance.commands import CommandResult

    return CommandResult("git", (), 1, "", message)


def _shadowed_build_inputs(stdout, lock):
    roots = set(BUILD_CRITICAL_ROOTS)
    files = {str(lock.executable), str(lock.host_dependencies_file)}
    found = []
    for entry in str(stdout or "").split("\0"):
        path = entry.strip()
        if not path:
            continue
        head = path.split("/", 1)[0]
        if head in roots or path in files:
            found.append(path)
    return sorted(found)


# --- the tarball source form -------------------------------------------------


def tree_manifest(root, *, exclude=(SOURCE_IDENTITY_NAME,)):
    """Every file in a source tree as path, type, mode and content identity.

    Deterministic and ordered, so the same tree always produces the same
    manifest. Symlinks are recorded by target rather than followed: a build
    input replaced by a link to somewhere else is a different tree.

    Bytecode caches and version-control metadata are outside it. Nothing reads
    them as a build input, and upstream's own tooling writes ``__pycache__``
    into ``site/`` the first time it runs — an authority that counted those
    would refuse the second build on a tree the first build was fine with.
    """

    base = Path(root)
    skipped = {str(item) for item in exclude}
    entries = []
    for path in sorted(base.rglob("*"), key=lambda item: str(item.relative_to(base))):
        relative = str(path.relative_to(base))
        if _outside_the_tree_manifest(relative, skipped):
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            entries.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "mode": mode,
                    "target": os.readlink(path),
                }
            )
        elif stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "type": "directory", "mode": mode})
        elif stat.S_ISREG(info.st_mode):
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": mode,
                    "sha256": file_sha256(path),
                }
            )
        else:
            entries.append({"path": relative, "type": "other", "mode": mode})
    return entries


def _outside_the_tree_manifest(relative, skipped):
    parts = relative.split("/")
    if relative in skipped or parts[0] in skipped:
        return True
    if any(part in TREE_IGNORED_NAMES for part in parts):
        return True
    return relative.endswith(TREE_IGNORED_SUFFIXES)


def tree_digest(root, *, exclude=(SOURCE_IDENTITY_NAME,)):
    """One hash over the whole tree manifest."""

    payload = json.dumps(tree_manifest(root, exclude=exclude), sort_keys=True,
                         separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_source_identity(root, *, form, release, commit, url, sha256, top_level_directory):
    """Record what was verified, including the tree the build will read.

    The archive hash proves the download. The tree hash is what proves the
    extraction afterwards, every time, right up to the moment a build starts.
    """

    target = Path(root) / SOURCE_IDENTITY_NAME
    payload = {
        "form": form,
        "release": release,
        "commit": commit,
        "url": url,
        "sha256": sha256,
        "top_level_directory": top_level_directory,
        "tree_sha256": tree_digest(root),
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def assert_buildable(directory, lock=None, *, which=None, package_query=AUTO_PACKAGE_QUERY,
                     runner=None):
    """Prove the tree immediately before the build reads it, or refuse.

    A compatibility check that ran minutes earlier says nothing about the tree
    ``./rpi-image-gen build`` is about to open. This is the call the build
    wrapper makes with the generator's own working directory already chosen.
    """

    lock = lock or read_lock()
    report = probe_checkout(
        directory, lock, which=which, package_query=package_query, runner=runner
    )
    if not report.compatible:
        raise ImageGenError(
            report.reason or REASON_INCOMPATIBLE,
            "; ".join(
                finding.detail for finding in report.findings if finding.result == FAIL
            )
            or "the source tree is not the pinned rpi-image-gen",
        )
    return report


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
