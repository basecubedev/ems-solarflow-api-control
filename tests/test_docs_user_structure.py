# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the audience-split documentation structure.

These tests protect the split between user, technical and developer docs, the
compact-router shape of the root README, the Admin Console user-facing naming,
FAQ coverage, and the preservation of technical knowledge after the move. They
check for headings/links and short copy snippets, not exact long paragraphs.
"""
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return path.read_text(encoding="utf-8")


# --- Shared Admin/Dashboard password ---------------------------------------


def test_user_docs_document_shared_admin_dashboard_password():
    text = read(ROOT / "docs" / "user" / "admin-console.md")
    assert "same password as the EMS Dashboard" in text
    assert "config/dashboard-auth.json" in text
    assert "first" in text.lower()


def test_readme_mentions_first_start_password_without_becoming_security_manual():
    text = read(ROOT / "README.md")
    assert "shared EMS/Admin password" in text or "Admin Console password" in text
    assert len(text.splitlines()) <= 130


# --- README is a compact router, not a manual -----------------------------


def test_readme_is_router_not_manual():
    text = read(ROOT / "README.md")
    assert "install-admin-console.sh" in text
    # Docker Bootstrap and Developer Setup are links only in the README.
    assert "install-docker.sh" not in text
    assert "git clone" not in text
    assert "docker compose exec ems python3 emsctl.py diagnose" not in text
    assert '"dry_run"' not in text


def test_readme_is_router_sized():
    # Router target: short. 110 lines is the hard ceiling (top-of-file status
    # badges included).
    lines = read(ROOT / "README.md").splitlines()
    assert len(lines) <= 110, len(lines)


def test_readme_routes_to_three_setup_paths():
    text = read(ROOT / "README.md")
    assert "Admin Console" in text
    assert "Docker Bootstrap" in text
    assert "Developer Setup" in text
    assert "docs/user/admin-console.md" in text
    assert "docs/user/docker-bootstrap.md" in text
    assert "docs/developer/developer-setup.md" in text
    # Stale naming must not come back.
    assert "Admin Tool" not in text


def test_readme_has_copy_paste_admin_console_start():
    text = read(ROOT / "README.md")
    assert "install-admin-console.sh" in text
    assert "http://127.0.0.1:8090" in text


def test_readme_admin_console_start_requires_no_git_checkout():
    text = read(ROOT / "README.md")
    admin_section = text.split("## Get started", 1)[1].split("##", 1)[0]
    assert "install-admin-console.sh" in admin_section
    assert "git clone" not in admin_section


def test_readme_admin_console_defaults_to_host_networking():
    text = read(ROOT / "README.md")
    admin_section = text.split("## Get started", 1)[1].split("##", 1)[0]
    # Normal users are never told to pass --hostnet; host networking is the
    # documented default and --bridge is the opt-in.
    assert "--hostnet" not in admin_section
    assert "host networking" in admin_section.lower()
    assert "--bridge" in admin_section


def test_readme_links_the_three_audience_doc_areas():
    text = read(ROOT / "README.md")
    assert "docs/user/" in text
    assert "docs/technical/" in text
    assert "docs/developer/" in text
    assert "docs/README.md" in text


# --- Supported hardware at a glance ---------------------------------------


def test_readme_has_supported_hardware_summary():
    text = read(ROOT / "README.md")
    assert "## Supported hardware at a glance" in text
    # The README carries a compact grouped summary, not the full compatibility
    # matrix: important hardware families stay discoverable by exact name, and
    # the detailed table is owned by docs/user/supported-setups.md.
    for model in (
        "SolarFlow 800 Pro 2",
        "SolarFlow 2400 Pro",
        "Hyper 2000",
        "Smart Meter 3CT",
        "Smart Meter D0",
        "Shelly Pro",
    ):
        assert model in text, model
    # The four-tier vocabulary is summarised (defined in full in the linked page).
    assert "Validated" in text
    assert "Family-supported" in text
    assert (
        "docs/user/supported-setups.md" in text
        or "docs/supported-setups.md" in text
    )
    # The real-hardware safety warning must stay in the README.
    assert "real power hardware" in text


def test_readme_marks_mqtt_device_control_as_roadmap():
    text = read(ROOT / "README.md")
    section = text.split("## Supported hardware at a glance", 1)[1].split("##", 1)[0]
    assert "Roadmap" in section
    assert "MQTT" in section
    assert "device" in section or "inverter" in section


def test_supported_setups_page_lives_under_user_docs():
    page = ROOT / "docs" / "user" / "supported-setups.md"
    assert page.is_file()
    text = read(page)
    # Known ZenSDK-compatible models and the roadmap/not-supported guards.
    assert "SolarFlow 800" in text
    assert "SolarFlow 2400 Pro" in text
    assert "Roadmap" in text
    assert "Not Supported" in text or "not supported" in text
    # The page has one home under docs/user/; no stale top-level copy.
    assert not (ROOT / "docs" / "supported-setups.md").exists()


def _validated_devices_rows(text):
    # Return the data rows of the "## Validated devices" ledger table as lists
    # of trimmed cell strings.
    section = text.split("## Validated devices", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue  # separator row
        rows.append(cells)
    return rows


def test_validated_devices_ledger_ties_each_row_to_evidence():
    text = read(ROOT / "docs" / "user" / "supported-setups.md")
    rows = _validated_devices_rows(text)
    assert rows, "Validated devices ledger table not found"
    header = [h.lower() for h in rows[0]]
    assert "validated by" in header
    assert "evidence" in header
    evidence_col = header.index("evidence")
    validated_by_col = header.index("validated by")
    # Every Validated claim must carry a non-empty "Validated by" and "Evidence"
    # statement — a device only earns a ledger row with real evidence behind it.
    for row in rows[1:]:
        device = row[0]
        assert row[validated_by_col], f"{device}: missing 'Validated by'"
        assert row[evidence_col], f"{device}: missing 'Evidence'"


# --- Docs are split by audience -------------------------------------------


def test_docs_are_split_by_audience():
    assert (ROOT / "docs" / "user").is_dir()
    assert (ROOT / "docs" / "technical").is_dir()
    assert (ROOT / "docs" / "developer").is_dir()


def test_docs_index_routes_by_audience():
    text = read(ROOT / "docs" / "README.md")
    assert "## User documentation" in text
    assert "## Technical reference" in text
    assert "## Developer documentation" in text
    # The map links into the audience folders.
    assert "user/admin-console.md" in text
    assert "technical/admin-discovery.md" in text
    assert "developer/developer-setup.md" in text


def test_docs_index_names_three_operating_models():
    text = read(ROOT / "docs" / "README.md")
    assert "Admin Console" in text
    assert "Docker Bootstrap" in text
    assert "Developer Setup" in text


# --- Technical knowledge is preserved at its new home ---------------------


def test_technical_docs_are_preserved():
    for path in [
        "docs/technical/admin-discovery.md",
        "docs/technical/admin-architecture.md",
        "docs/technical/architecture.md",
        "docs/technical/control-logic.md",
        "docs/technical/control-flow.md",
        "docs/technical/runtime-state.md",
        "docs/technical/configuration.md",
        "docs/technical/backup-restore.md",
        "docs/technical/influxdb.md",
    ]:
        assert (ROOT / path).exists(), path


def test_developer_docs_are_preserved():
    for path in [
        "docs/developer/developer-setup.md",
        "docs/developer/development.md",
        "docs/developer/developer.md",
        "docs/developer/testing.md",
        "docs/developer/ci-release.md",
        "docs/developer/dashboard-style-guide.md",
        "docs/developer/design-notes/develop-tool-influxdb-telemetry.md",
    ]:
        assert (ROOT / path).exists(), path


def test_admin_architecture_reference_exists():
    text = read(ROOT / "docs" / "technical" / "admin-architecture.md")
    assert "EMS remains the source of truth" in text
    assert "Admin Console" in text
    assert "UI" in text and "orchestration" in text
    assert "config/config.json" in text
    assert "data/admin" in text


def test_redirect_stubs_point_to_new_paths():
    # High-value old paths keep short redirect stubs so external links survive.
    for old, new in [
        ("docs/admin.md", "user/admin-console.md"),
        ("docs/admin-discovery.md", "technical/admin-discovery.md"),
        ("docs/configuration.md", "technical/configuration.md"),
        ("docs/backup-restore.md", "technical/backup-restore.md"),
        ("docs/influxdb.md", "technical/influxdb.md"),
        ("docs/troubleshooting.md", "user/troubleshooting.md"),
        ("docs/development.md", "developer/development.md"),
    ]:
        stub = ROOT / old
        assert stub.exists(), old
        text = read(stub)
        assert "Moved" in text
        assert new in text


# --- Naming and stale copy ------------------------------------------------


def test_stale_admin_user_copy_removed():
    combined = "\n".join(
        read(ROOT / path)
        for path in [
            "README.md",
            "docs/README.md",
            "docs/user/admin-console.md",
            "docs/user/admin-setup.md",
            "docs/user/admin-maintenance.md",
            "docs/user/admin-backup-restore.md",
            "docs/user/faq.md",
        ]
        if (ROOT / path).exists()
    )
    assert "Admin Tool" not in combined
    assert "Admin (MVP)" not in combined
    assert "Admin discovery (MVP)" not in combined
    assert "Planned upgrade workflow" not in combined


# --- FAQ coverage (now under docs/user/) ----------------------------------


def test_faq_answers_admin_console_user_questions():
    text = read(ROOT / "docs" / "user" / "faq.md")
    for expected in [
        "What is the Admin Console?",
        "Should I choose Setup or Maintenance?",
        "Should I use the Admin Console or Docker Bootstrap?",
        "What is Developer Setup?",
        "Are Admin Console backups normal EMS backups?",
        "Is the Admin Console safe to expose to the internet?",
    ]:
        assert expected in text


def test_faq_documents_admin_shared_dashboard_password():
    text = read(ROOT / "docs" / "user" / "faq.md")
    assert "same password file as the EMS Dashboard" in text
    assert "config/dashboard-auth.json" in text
    # The old "no built-in authentication" wording must be gone.
    assert "no built-in authentication" not in text.lower()


def test_faq_has_copy_paste_troubleshooting_commands():
    text = read(ROOT / "docs" / "user" / "faq.md")
    assert "The dashboard is not reachable. What should I check?" in text
    assert "Device discovery does not find my devices. What should I try?" in text
    assert "docker compose ps" in text
    assert "sh install-admin-console.sh --bridge" in text


def test_faq_explains_host_networking_default_and_bridge():
    text = read(ROOT / "docs" / "user" / "faq.md")
    assert "Why does the Admin Console use host networking by default?" in text
    assert "Can I run the Admin Console in bridge mode?" in text
    assert "sh install-admin-console.sh --bridge" in text
    start_answer = text.split(
        "### How do I start the Admin Console?", 1
    )[1].split("###", 1)[0]
    assert "--hostnet" not in start_answer


def test_faq_start_path_uses_installer_not_source_launcher():
    text = read(ROOT / "docs" / "user" / "faq.md")
    start_answer = text.split(
        "### How do I start the Admin Console?", 1
    )[1].split("###", 1)[0]
    assert "install-admin-console.sh" in start_answer
    assert "deploy/admin/start-admin-setup.sh" not in start_answer


# --- Positive, user-facing install copy (no "No Git checkout") -------------


def test_readme_recommended_admin_console_uses_positive_instruction():
    text = read(ROOT / "README.md")
    section = text.split("## Get started", 1)[1].split("##", 1)[0]
    assert "Start with the Admin Console" in section
    assert "Install and start it" in section
    assert "No Git checkout" not in section


def test_user_docs_do_not_lead_with_no_git_checkout_copy():
    paths = [
        ROOT / "README.md",
        ROOT / "docs" / "user" / "admin-console.md",
        ROOT / "docs" / "user" / "admin-setup.md",
        ROOT / "docs" / "user" / "faq.md",
    ]
    combined = "\n".join(read(path) for path in paths)
    assert "No Git checkout is required" not in combined


def test_user_docs_do_not_show_old_setup_path_labels():
    combined = "\n".join(
        read(path)
        for path in (ROOT / "docs" / "user").glob("*.md")
    )
    assert "[setup/" not in combined


def test_faq_update_guidance_is_admin_first():
    text = read(ROOT / "docs" / "user" / "faq.md")
    section = text.split(
        "### What should I do before updating?", 1
    )[1].split("###", 1)[0]
    assert "Admin Console" in section
    assert "Maintenance" in section
    assert "Guided upgrade" in section
    assert "For Docker Bootstrap or advanced shell use" in section
    assert section.index("Admin Console") < section.index("docker compose")


def test_supported_setups_next_step_uses_operating_models():
    text = read(ROOT / "docs" / "user" / "supported-setups.md")
    assert "## Next step" in text
    assert "[Admin Console](admin-console.md)" in text
    assert "[Docker Bootstrap](docker-bootstrap.md)" in text
    assert "[Developer Setup](../developer/developer-setup.md)" in text
    assert "../quickstart.md" not in text.split("## Next step", 1)[1]


def test_readme_stays_compact_router():
    text = read(ROOT / "README.md")
    assert "install-admin-console.sh" in text
    assert "install-docker.sh" not in text
    assert "git clone" not in text
    assert "docker compose exec ems python3 emsctl.py diagnose" not in text
    assert '"dry_run"' not in text
    assert len(text.splitlines()) <= 110


# --- User docs stay simple; technical detail keeps its home ---------------


def test_user_troubleshooting_is_admin_first_and_not_huge():
    text = read(ROOT / "docs" / "user" / "troubleshooting.md")
    assert text.startswith("# Troubleshooting")
    assert "Admin Console" in text
    assert "Maintenance" in text
    assert "technical troubleshooting reference" in text
    assert len(text.splitlines()) <= 400
    # Docker is only a fallback, so Admin Console must lead.
    if "docker compose" in text:
        assert text.find("Admin Console") < text.find("docker compose")


def test_technical_troubleshooting_reference_exists():
    path = ROOT / "docs" / "technical" / "troubleshooting-reference.md"
    assert path.exists()
    text = read(path)
    assert "Technical" in text or "technical" in text
    assert "docker compose" in text
    assert "emsctl" in text
    assert "../user/troubleshooting.md" in text


def test_user_safety_is_checklist_not_runtime_reference():
    text = read(ROOT / "docs" / "user" / "safety.md")
    assert text.startswith("# Safety")
    assert "Before enabling live writes" in text
    assert "technical safety model" in text
    assert len(text.splitlines()) <= 250


def test_technical_safety_model_exists():
    path = ROOT / "docs" / "technical" / "safety-model.md"
    assert path.exists()
    text = read(path)
    assert "Write" in text or "write" in text
    assert "outputLimit" in text
    assert "gridOffMode" in text or "acMode" in text
    assert "../user/safety.md" in text


def test_user_admin_maintenance_is_workflow_oriented():
    text = read(ROOT / "docs" / "user" / "admin-maintenance.md")
    assert "Guided upgrade" in text
    assert "Choose the target" in text or "Choose the target version" in text
    assert "Review the plan" in text
    assert "Create a backup" in text
    assert "../technical/admin-discovery.md" in text
    forbidden = [
        "ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES",
        "SemVer fallback",
        "build identity",
        "release cache",
    ]
    for term in forbidden:
        assert term not in text


def test_docs_index_links_new_references():
    text = read(ROOT / "docs" / "README.md")
    assert "technical/troubleshooting-reference.md" in text
    assert "technical/safety-model.md" in text


# --- Final polish: audience routing and user-facing wording ---------------


def test_docs_index_marks_developer_setup_as_developer_path():
    text = read(ROOT / "docs" / "README.md")
    assert "Developer Setup" in text
    assert "developer" in text.lower()
    assert "contributor" in text.lower() or "source checkout" in text.lower()

    # Normal users are routed to user docs first.
    assert "Normal users should start" in text
    assert "User documentation" in text
    assert "Developer documentation" in text


def test_docs_index_operating_models_mark_developer_setup_not_for_normal_users():
    text = read(ROOT / "docs" / "README.md")
    section = text.split("## Operating models", 1)[1].split("##", 1)[0]
    assert "Admin Console" in section
    assert "Docker Bootstrap" in section
    assert "Developer Setup" in section
    assert (
        "Developers" in section
        or "contributors" in section
        or "source checkout" in section
    )


def test_user_docs_hide_release_cache_internals():
    combined = "\n".join(
        read(path)
        for path in (ROOT / "docs" / "user").glob("*.md")
    )
    assert "release cache" not in combined.lower()


def test_admin_console_files_table_uses_simple_admin_state_wording():
    text = read(ROOT / "docs" / "user" / "admin-console.md")
    assert "data/admin/" in text
    assert (
        "temporary files and logs" in text
        or "temporary setup data and logs" in text
    )
    assert "release cache" not in text.lower()
