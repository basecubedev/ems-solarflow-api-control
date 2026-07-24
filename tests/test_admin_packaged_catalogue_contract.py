# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packaged browser fixtures must exercise the production catalogue path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_SUPPORT = ROOT / "admin" / "test_support.py"


def test_packaged_runtime_uses_real_release_manager_and_production_labels():
    source = TEST_SUPPORT.read_text(encoding="utf-8")

    assert "class _TestReleaseManager" not in source
    assert 'item["selection_label"] = "Development"' not in source
    assert "release_manager = ReleaseManager(" in source
    assert "development_source=development_source" in source
