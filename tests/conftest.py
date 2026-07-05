# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pytest fixtures for the EMS test suite."""

import pytest

from ems import paths


@pytest.fixture
def isolated_install_root(tmp_path_factory, monkeypatch):
    """Point EMS path resolution at an empty temporary install root.

    Admin config preview/export/server code resolves the active EMS config
    through ``ems.paths`` (``paths.BASE_DIR`` when no explicit install root is
    given). In a real developer checkout that root holds a gitignored
    ``config/config.json`` and ``data/`` left behind by running EMS locally, so
    the default resolution would read the developer's real runtime files and let
    the outcome depend on the working tree.

    Isolating ``BASE_DIR`` to an empty directory (and clearing any ambient EMS
    path env overrides) keeps those tests deterministic without requiring a
    clean checkout or ``git clean -fdX``. Tests that intentionally validate path
    resolution override ``BASE_DIR``/``EMS_INSTALL_DIR`` themselves, which layers
    cleanly on top of this baseline.
    """

    root = tmp_path_factory.mktemp("isolated_install_root")
    for var in ("EMS_INSTALL_DIR", "EMS_CONFIG_FILE", "EMS_TEMPLATE_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paths, "BASE_DIR", str(root))
    return root
