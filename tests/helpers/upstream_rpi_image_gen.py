# SPDX-License-Identifier: AGPL-3.0-or-later
"""Access to the pinned rpi-image-gen contract, as fixture or as real source.

``tests/fixtures/rpi_image_gen`` holds exact bytes copied from the pinned
release, so the upstream tier runs everywhere. When a real pinned source tree is
available the same assertions run against it, and the fixture is proven to be
that tree rather than a convenient copy of it.

The generator runner is deliberate: ``slot-shared-generator`` writes to a fixed
``/run/systemd/generator``, so it is executed inside a private mount namespace
with tmpfs over ``/etc`` and ``/run/systemd``. The script itself is untouched —
a rewritten copy would be testing this project's idea of upstream.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "rpi_image_gen"
MANIFEST = FIXTURE / "source-manifest.json"

SHARED_GENERATOR = (
    "system-generators/slot-shared-generator"
)
PERSIST_GENERATOR = (
    "system-generators/slot-perst-generator"
)

DEVICE_LAYERS = (
    "device/pi3/device.yaml",
    "device/pi4/device.yaml",
    "device/pi5/device.yaml",
)

# Named separately as well, because image-rota does not accept its device class:
# the Pi 3 builds only the single-slot image, and that refusal is proven against
# real pinned bytes rather than an absence.
PI3_DEVICE_LAYER = "device/pi3/device.yaml"
DOCKER_LAYERS = (
    "layer/app-container/docker/engine-trixie.yaml",
    "layer/app-container/docker/engine-bookworm.yaml",
)
UPSTREAM_AB_CONFIG = "config/trixie-minbase-ab.yaml"

# The non-A/B counterparts: one MBR boot partition, one root, and the udev
# rules that give that root the /dev/disk/by-slot/system name the kernel
# command line and fstab are written against.
IMAGE_RPIOS = "image/mbr/simple_dual/image.yaml"
IMAGE_RPIOS_GENIMAGE = "image/mbr/simple_dual/genimage.cfg.in.ext4"
IMAGE_RPIOS_SETUP = "image/mbr/simple_dual/setup.sh"
IMAGE_RPIOS_SLOT_RULES = (
    "image/mbr/simple_dual/device/rootfs-overlay/etc/udev/rules.d/99-rpi-05-image.rules"
)
UPSTREAM_SINGLE_CONFIG = "config/trixie-minbase.yaml"

SOURCE_ENV = "EMS_RPI_IMAGE_GEN"


def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def fixture_files():
    return manifest()["files"]


def read(relative):
    return (FIXTURE / relative).read_text(encoding="utf-8")


def real_source_tree():
    """A real pinned checkout, when the environment names one."""

    candidate = os.environ.get(SOURCE_ENV, "").strip()
    if not candidate:
        return None
    path = Path(candidate)
    return path if (path / "LICENSE").is_file() else None


def namespaces_available():
    """Can the upstream generator be run against a disposable root here?"""

    if shutil.which("unshare") is None or shutil.which("systemd-escape") is None:
        return False
    probe = subprocess.run(
        ["unshare", "-rm", "sh", "-c", "mount -t tmpfs tmpfs /etc && echo ok"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and "ok" in probe.stdout


def run_slot_shared_generator(conf_files, output_dir, *, source=None):
    """Run the pinned generator over ``conf_files`` and collect what it wrote.

    ``conf_files`` maps a file name in ``/etc/rpi-image-gen/slot-shared.d`` to
    its exact content. Returns the generated unit names and the units that were
    linked into ``local-fs.target.wants``.
    """

    source = Path(source) if source is not None else FIXTURE
    generator = source / SHARED_GENERATOR
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    staging = output.parent / f"{output.name}-conf"
    staging.mkdir(parents=True, exist_ok=True)
    for name, content in conf_files.items():
        (staging / name).write_text(content, encoding="utf-8")

    script = (
        "set -e\n"
        "mount -t tmpfs tmpfs /etc\n"
        "mkdir -p /etc/rpi-image-gen/slot-shared.d\n"
        f"cp {staging}/* /etc/rpi-image-gen/slot-shared.d/\n"
        "mount -t tmpfs tmpfs /run/systemd\n"
        "mkdir -p /run/systemd/generator\n"
        f"{generator}\n"
        f"cp -r /run/systemd/generator/. {output}/\n"
    )
    result = subprocess.run(
        ["unshare", "-rm", "sh", "-c", script], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"the upstream generator failed: {result.stderr.strip()}")

    units = sorted(item.name for item in output.iterdir() if item.suffix == ".mount")
    wants_dir = output / "local-fs.target.wants"
    wants = sorted(item.name for item in wants_dir.iterdir()) if wants_dir.is_dir() else []
    return {"units": units, "wants": wants, "directory": output}


def escaped_mount_unit(path):
    """The unit name systemd gives a mount at ``path``."""

    result = subprocess.run(
        ["systemd-escape", "--path", str(path)], capture_output=True, text=True, check=True
    )
    return result.stdout.strip() + ".mount"


def layer_field(text, field):
    """One ``X-Env-Layer-<field>`` value from an upstream metadata header."""

    key = f"X-Env-Layer-{field}:"
    for raw in str(text).splitlines():
        line = raw.strip().lstrip("#").strip()
        if line.startswith(key):
            return line[len(key) :].strip()
    return ""


def layer_list(text, field):
    value = layer_field(text, field)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _header_field(text, key):
    for raw in str(text).splitlines():
        line = raw.strip().lstrip("#").strip()
        if line.startswith(key):
            return line[len(key) :].strip()
    return ""


def var_requires_rules(text):
    """The externally-required variables of a layer, paired with their rules.

    ``X-Env-VarRequires`` and ``X-Env-VarRequires-Valid`` are two comma-separated
    lists matched by position. The split is upstream's own — see
    ``site/pipeline.py`` — so a rule read here is the rule a build enforces.
    """

    names = [item.strip() for item in _header_field(text, "X-Env-VarRequires:").split(",")]
    rules = [item.strip() for item in _header_field(text, "X-Env-VarRequires-Valid:").split(",")]
    return {
        name: (rules[index] if index < len(rules) else "")
        for index, name in enumerate(names)
        if name
    }


def rule_accepts(rule, value):
    """Does one ``X-Env-...-Valid`` rule accept ``value``?

    Only the rule forms this project's layers actually use are implemented, and
    an unknown form raises rather than defaulting to "accepted": a rule this
    helper silently passed would turn a refused board into a supported one.
    """

    import re

    if rule == "string":
        return True
    if rule.startswith("regex:"):
        return re.match(rule[len("regex:") :], str(value)) is not None
    if rule.startswith("keywords:"):
        return str(value) in {item.strip() for item in rule[len("keywords:") :].split(",")}
    raise NotImplementedError(f"unsupported validation rule: {rule!r}")


def site_tooling(source):
    """Upstream's own config loader and layer manager, or why not.

    Driving the real resolver is the only way to prove a project profile
    resolves: this project's reading of a layer name is exactly the thing under
    test, so it cannot also be the thing doing the checking.
    """

    import importlib
    import sys

    site = Path(source) / "site"
    if not site.is_dir():
        return None, "the source tree carries no site/ tooling"
    for module in ("yaml", "debian", "jsonschema"):
        try:
            importlib.import_module(module)
        except ImportError:
            return None, f"upstream's loader needs python3-{module.replace('_', '-')}"
    inserted = str(site) not in sys.path
    if inserted:
        sys.path.insert(0, str(site))
    try:
        config_loader = importlib.import_module("config_loader")
        layer_manager = importlib.import_module("layer_manager")
    except Exception as exc:
        return None, f"upstream's tooling could not be imported: {exc}"
    return (config_loader, layer_manager), ""


def layer_index(source, layer_manager, *, extra_paths=()):
    """Every layer upstream exposes, loaded the way a build loads them."""

    import tempfile

    paths = [f"DYNlayer={tempfile.mkdtemp()}"]
    paths.extend(str(Path(source) / name) for name in ("layer", "device", "image"))
    paths.extend(str(path) for path in extra_paths)
    return layer_manager.LayerManager(search_paths=paths)


def load_config(source, config_loader, config_path):
    """Resolve one project profile through upstream's own include handling."""

    loader = config_loader.ConfigLoader(
        str(config_path),
        expand_vars=False,
        search_paths=[str(Path(source) / "config")],
    )
    loader.load_all()
    return loader.data
