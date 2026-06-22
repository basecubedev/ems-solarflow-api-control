# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.example.yml"
DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "quickstart.md",
    ROOT / "docs" / "docker.md",
    ROOT / "docs" / "common-commands.md",
    ROOT / "docs" / "backup-restore.md",
]


def read(path):
    return path.read_text(encoding="utf-8")


def test_compose_example_contract_matches_user_docs():
    compose = read(COMPOSE)

    assert "  ems:" in compose
    assert "8080:8080" in compose
    assert "./config:/app/config" in compose
    assert "./data:/app/data" in compose

    combined_docs = "\n".join(read(path) for path in DOCS)
    for expected in (
        "service name `ems`",
        "`8080:8080`",
        "`./config:/app/config`",
        "`./data:/app/data`",
    ):
        assert expected in combined_docs


def test_user_docs_use_current_compose_command_style():
    for path in DOCS:
        text = read(path)
        assert "docker-compose " not in text, path
        assert "docker compose" in text, path


def test_update_docs_keep_backup_upgrade_and_diagnose_sequence_visible():
    update_docs = read(ROOT / "docs" / "common-commands.md")

    backup_index = update_docs.index("backup create --type config")
    pull_index = update_docs.index("docker compose pull")

    assert backup_index < pull_index
    assert "config upgrade --dry-run" in update_docs
    assert "config upgrade --yes --backup" in update_docs
    assert "docker compose exec ems python3 emsctl.py diagnose" in update_docs


def test_backup_docs_name_host_backup_path_and_restore_variants():
    text = read(ROOT / "docs" / "backup-restore.md")
    normalized = " ".join(text.split())

    assert "data/backups/" in text
    assert "data/backups/<file>.tar.gz.enc" in text
    assert "data/backups/<file>.tar.gz" in text
    assert "--on-conflict replace --rollback" in text
    assert "without it the encrypted backup cannot be restored" in normalized
