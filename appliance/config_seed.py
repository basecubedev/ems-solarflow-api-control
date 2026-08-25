# SPDX-License-Identifier: AGPL-3.0-or-later
"""Give an appliance its operator-owned configuration once, and never again.

``appliance.conf`` and ``allowed-images.conf`` belong to whoever runs the
appliance. They live under ``/etc/ems-appliance-manager``, which is a declared
shared path, so an edit made on one slot is visible from the other and survives
a slot switch.

What it does not survive, if the package ships its own copy to the same place,
is a reboot. Upstream's ``rpi-persistent-shared-init`` runs before the shared
binds are active and pushes the booting slot's own ``/etc`` into
``/persistent/shared/etc`` with ``rsync -av --checksum`` — every boot, no
``--delete``, and ``--checksum`` transfers precisely the files that differ. An
operator's edit is the difference, so the packaged copy wins and the edit is
gone. The appliance's ``timezone`` file has always been safe from this for the
one reason that matters here: no slot root contains a copy of it.

So the package ships templates outside ``/etc`` and this module seeds a missing
file from one. Seeding is the only write: a file that exists is never touched,
which is what makes running this on every boot the same as running it once.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from appliance import paths as paths_module

TEMPLATES = ("appliance.conf", "allowed-images.conf")

SEEDED = "seeded"
PRESENT = "present"
TEMPLATE_MISSING = "template_missing"

FILE_MODE = 0o644
DIRECTORY_MODE = 0o755


@dataclass(frozen=True)
class SeedResult:
    """What became of one operator-owned file."""

    name: str
    outcome: str
    target: str
    detail: str = ""

    @property
    def ok(self):
        return self.outcome in (SEEDED, PRESENT)


def _install(template, target):
    """Copy a template into place so no reader can observe a partial file.

    The rename is what makes this safe to run while the agent is starting: a
    reader either sees no file and falls back to the compiled defaults, or sees
    a complete one.
    """

    payload = template.read_bytes()
    handle, staging = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, FILE_MODE)
        os.replace(staging, target)
    except BaseException:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise


def seed_config(paths, *, templates=TEMPLATES):
    """Create every operator-owned file this appliance is missing.

    Returns one :class:`SeedResult` per name. An existing file is reported as
    ``present`` without being read, compared or rewritten.
    """

    config_dir = Path(paths.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, DIRECTORY_MODE)
    except OSError:
        pass

    results = []
    for name in templates:
        target = config_dir / name
        if target.exists():
            results.append(SeedResult(name=name, outcome=PRESENT, target=str(target)))
            continue
        template = paths_module.packaged_template(name)
        if not template.is_file():
            results.append(
                SeedResult(
                    name=name,
                    outcome=TEMPLATE_MISSING,
                    target=str(target),
                    detail=f"{template} is not installed",
                )
            )
            continue
        _install(template, target)
        results.append(
            SeedResult(
                name=name,
                outcome=SEEDED,
                target=str(target),
                detail=f"from {template}",
            )
        )
    return results
