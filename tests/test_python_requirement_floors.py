# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin image allows no older release of a package than the EMS does.

``deploy/admin/requirements.txt`` repeats packages of ``requirements.txt``.
Dependabot kept only the root manifest, so the Admin's floors stayed at
releases with published advisories (requests 2.32, zeroconf 0.132) while the
EMS's moved on.
"""

from pathlib import Path, PurePosixPath

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tools.check_third_party_licenses import PYTHON_DEV_MANIFESTS, PYTHON_RUNTIME_MANIFESTS

pytestmark = [pytest.mark.contract, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]


def _floors(path):
    floors = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement = Requirement(line)
        lows = [Version(spec.version) for spec in requirement.specifier if spec.operator in (">=", "==", "~=")]
        floors[canonicalize_name(requirement.name)] = max(lows) if lows else None
    return floors


def test_the_admin_image_allows_no_older_release_of_a_shared_package_than_the_ems():
    ems = _floors(ROOT / "requirements.txt")
    admin = _floors(ROOT / "deploy" / "admin" / "requirements.txt")
    shared = sorted(set(ems) & set(admin))

    assert {"requests", "zeroconf"} <= set(shared)
    behind = {
        name: {"admin": str(admin[name]), "ems": str(ems[name])}
        for name in shared
        if ems[name] is not None and (admin[name] is None or admin[name] < ems[name])
    }
    assert behind == {}


def test_dependabot_keeps_every_directory_that_holds_a_python_manifest():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    kept = set()
    for update in config["updates"]:
        if update["package-ecosystem"] == "pip":
            kept.update(update.get("directories") or [update["directory"]])

    needed = set()
    for manifest in (*PYTHON_RUNTIME_MANIFESTS, *PYTHON_DEV_MANIFESTS):
        parent = PurePosixPath(manifest).parent
        needed.add("/" if str(parent) == "." else f"/{parent}")

    assert needed <= kept, sorted(needed - kept)
