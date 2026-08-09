# SPDX-License-Identifier: AGPL-3.0-or-later
"""Appliance Manager documentation contract.

The user documentation has to answer a fixed set of operator questions and must
not contradict the security model, so the required documents and the claims
they make are checked here.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.documentation]

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "appliance"

REQUIRED = (
    "architecture.md",
    "installation.md",
    "admin-recovery.md",
    "os-updates.md",
    "ssh-backup-access.md",
    "network-recovery.md",
    "security-model.md",
    "troubleshooting.md",
)


def read(name):
    return (DOCS / name).read_text(encoding="utf-8")


def test_every_required_document_exists():
    for name in REQUIRED:
        assert (DOCS / name).is_file(), name


def test_the_documentation_index_links_the_appliance_documents():
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for name in REQUIRED:
        assert f"appliance/{name}" in index, name


def test_product_boundaries_are_stated():
    architecture = read("architecture.md")
    assert "Appliance Manager" in architecture
    assert "EMS Admin Console" in architecture
    assert "Guided Setup" in architecture
    assert "never edits EMS configuration" in architecture or "never edit" in architecture


def test_the_two_services_and_the_socket_are_documented():
    architecture = read("architecture.md")
    assert "ems-appliance-web.service" in architecture
    assert "ems-appliance-agent.service" in architecture
    assert "/run/ems-appliance-manager/agent.sock" in architecture
    assert "never a network listener" in architecture


def test_supported_hardware_and_os_are_documented():
    installation = read("installation.md")
    assert "Raspberry Pi 4" in installation
    assert "Raspberry Pi 5" in installation
    assert "Raspberry Pi OS 64-bit" in installation
    assert "arm64" in installation


def test_the_local_password_reset_is_documented():
    installation = read("installation.md")
    assert "sudo ems-appliance password-reset" in installation
    assert "no unauthenticated network reset endpoint" in installation


def test_failed_admin_update_recovery_is_documented():
    recovery = read("admin-recovery.md")
    assert "rolled_back" in recovery
    assert "previous known-good" in recovery
    assert "Recovering a failed Admin update" in recovery


def test_installing_a_specific_version_is_documented():
    recovery = read("admin-recovery.md")
    assert "Exact release tag" in recovery
    assert "v0.8.0" in recovery


def test_os_update_documentation_refuses_unattended_distribution_upgrades():
    updates = read("os-updates.md")
    assert "Major OS upgrades" in updates
    assert "not supported" in updates
    assert "never removed" in updates


def test_ssh_and_rsync_instructions_exist():
    access = read("ssh-backup-access.md")
    assert "ssh-ed25519" in access
    assert "rsync -a ems-backup@" in access
    assert "scp -r ems-backup@" in access
    assert "Never paste a private key" in access


def test_wlan_recovery_is_documented():
    network = read("network-recovery.md")
    assert "never deleted" in network
    assert "Recovering access after a WLAN change" in network
    assert "Ethernet" in network


def test_the_security_model_documents_the_privilege_boundary():
    security = read("security-model.md")
    assert "no root" in security
    assert "Docker socket is never exposed to the web process" in security
    assert "PBKDF2-SHA256" in security
    assert "SO_PEERCRED" in security


def test_the_security_model_lists_the_absent_capabilities():
    security = read("security-model.md")
    for absent in (
        "arbitrary shell execution",
        "a browser-based terminal",
        "unrestricted Docker container management",
        "an unauthenticated password-reset endpoint",
    ):
        assert absent in security, absent


def test_troubleshooting_answers_every_required_question():
    troubleshooting = read("troubleshooting.md")
    for question in (
        "What does the Appliance Manager manage?",
        "What does the EMS Admin Console manage?",
        "How do I recover a failed Admin update?",
        "How do I install a specific Admin version?",
        "How do I add an SSH key?",
        "How do I back up files with rsync?",
        "How do I install OS updates?",
        "How do I recover access after a WLAN change?",
        "How do I reset the Appliance Manager password locally?",
    ):
        assert question in troubleshooting, question


def test_documentation_does_not_promise_hardware_validation_it_lacks():
    for name in REQUIRED:
        text = read(name)
        assert "certified" not in text.lower(), name
        assert "guaranteed" not in text.lower(), name
