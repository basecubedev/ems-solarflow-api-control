# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin image must copy every file its startup import chain needs.

`admin/install_context.py` imports `ems.paths`, so the Admin runtime reaches the
EMS package during startup. This test rebuilds the exact file set that
`deploy/admin/Dockerfile` copies into `/app` and asserts `python -m admin`
still imports, catching a Dockerfile that forgets a runtime dependency again.
No Docker, network, ports, or device discovery are required.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "deploy" / "admin" / "Dockerfile"

_COPY = re.compile(r"^COPY\s+(?!--)(\S+)\s+(\S+)\s*$")


def _dockerfile_app_copies():
    """Yield (src, dest) pairs for COPY directives that land under /app."""

    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = _COPY.match(line.strip())
        if not match:
            continue
        src, dest = match.group(1), match.group(2)
        if dest.startswith("./") or dest == ".":
            yield src, dest.lstrip("./")


def _mirror_image_files(app_root):
    copied = []
    for src, dest in _dockerfile_app_copies():
        source = ROOT / src
        target = app_root / (dest or src)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        copied.append(dest)
    return copied


def test_dockerfile_copies_the_ems_path_resolver():
    copies = dict(
        (dest, src) for src, dest in _dockerfile_app_copies()
    )
    assert "ems/__init__.py" in copies
    assert "ems/paths.py" in copies


def test_copied_files_are_sufficient_for_admin_startup(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    _mirror_image_files(app_root)

    # Drop PYTHONPATH so imports resolve only against the mirrored image layout,
    # not the real repo checkout — otherwise a missing COPY would go unnoticed.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-B", "-m", "admin", "--help"],
        cwd=app_root,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, (
        f"admin --help failed inside the mirrored image layout:\n{result.stderr}"
    )
    assert "usage" in result.stdout.lower()
