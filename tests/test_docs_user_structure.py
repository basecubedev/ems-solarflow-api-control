# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the user-facing documentation structure.

These tests protect the split between user docs and technical/developer docs,
the Admin Console user-facing naming and FAQ coverage, and the removal of stale
Admin wording. They check for the presence of headings/links and short copy
snippets, not exact long paragraphs.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return path.read_text(encoding="utf-8")


def test_readme_is_user_focused_and_links_admin_guides():
    text = read(ROOT / "README.md")
    assert "Most users" in text
    assert "Admin Console" in text
    assert "docs/setup/admin-setup.md" in text
    assert "docs/setup/admin-maintenance.md" in text
    assert "docs/faq.md" in text


def test_readme_names_three_operating_models():
    text = read(ROOT / "README.md")
    assert "Admin Console" in text
    assert "Docker Bootstrap" in text
    assert "Developer Setup" in text
    assert "Admin Tool" not in text


def test_readme_has_copy_paste_admin_console_start():
    text = read(ROOT / "README.md")
    assert "deploy/admin/start-admin-setup.sh" in text
    assert "http://127.0.0.1:8090" in text


def test_readme_has_copy_paste_docker_bootstrap_start():
    text = read(ROOT / "README.md")
    assert "sh install-docker.sh" in text


def test_docs_index_separates_user_technical_and_developer_docs():
    text = read(ROOT / "docs" / "README.md")
    assert "## For users" in text
    assert "## Technical reference" in text
    assert "## Developer and maintainer docs" in text
    assert "setup/admin-setup.md" in text
    assert "setup/admin-maintenance.md" in text
    assert "admin-discovery.md" in text


def test_docs_index_names_three_operating_models():
    text = read(ROOT / "docs" / "README.md")
    assert "Admin Console" in text
    assert "Docker Bootstrap" in text
    assert "Developer Setup" in text


def test_stale_admin_user_copy_removed():
    combined = "\n".join(
        read(ROOT / path)
        for path in [
            "README.md",
            "docs/README.md",
            "docs/admin.md",
            "docs/setup/admin-setup.md",
            "docs/setup/admin-maintenance.md",
            "docs/setup/admin-backup-restore.md",
            "docs/faq.md",
        ]
        if (ROOT / path).exists()
    )
    assert "Admin Tool" not in combined
    assert "Admin (MVP)" not in combined
    assert "Admin discovery (MVP)" not in combined
    assert "Planned upgrade workflow" not in combined


def test_technical_docs_are_preserved():
    for path in [
        "docs/admin-discovery.md",
        "docs/architecture.md",
        "docs/control-logic.md",
        "docs/control-flow.md",
        "docs/runtime-state.md",
        "docs/configuration.md",
        "docs/development.md",
        "docs/developer.md",
    ]:
        assert (ROOT / path).exists(), path


def test_faq_answers_admin_console_user_questions():
    text = read(ROOT / "docs" / "faq.md")
    for expected in [
        "What is the Admin Console?",
        "Should I choose Setup or Maintenance?",
        "Should I use the Admin Console or Docker Bootstrap?",
        "What is Developer Setup?",
        "Are Admin Console backups normal EMS backups?",
        "Is the Admin Console safe to expose to the internet?",
    ]:
        assert expected in text


def test_faq_has_copy_paste_troubleshooting_commands():
    text = read(ROOT / "docs" / "faq.md")
    assert "The dashboard is not reachable. What should I check?" in text
    assert "Device discovery does not find my devices. What should I try?" in text
    assert "docker compose ps" in text
    assert "deploy/admin/start-admin-setup.sh --hostnet" in text
