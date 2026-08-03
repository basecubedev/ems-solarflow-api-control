# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text contract: MQTT control is never described as generically experimental.

MQTT output control is a normal EMS transport where a verified write protocol
exists. "Experimental" wording may refer only to a specific unvalidated
hardware generation or protocol variation, never to MQTT control, the cloud
transport or Admin-created MQTT devices as a whole. These checks keep the
release wording from regressing.
"""

import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]

# Documentation, runtime and Admin sources whose user-facing wording must not
# reintroduce a generic MQTT restriction. Every entry must be a repository-owned
# tracked file (see the source contract below): a clean CI checkout never has
# ignored, developer-local files such as CLAUDE.md, so the wording contract must
# not depend on them.
FILES = (
    "README.md",
    "docs/user/connection-types.md",
    "docs/user/supported-setups.md",
    "docs/user/admin-setup.md",
    "docs/user/admin-maintenance.md",
    "docs/technical/configuration.md",
    "docs/technical/safety-model.md",
    "docs/technical/admin-discovery.md",
    "ems/zendure_mqtt/__init__.py",
    "ems/zendure_mqtt/config_entries.py",
    "admin/zendure_mqtt_config_draft.py",
    "admin/config_preview.py",
    "admin/server.py",
    "admin/zendure_mqtt_runtime_status.py",
    "admin/static/admin.js",
    "admin/static/admin.css",
    "admin/static/index.html",
)

# Generic claims that are no longer true for this release. Matching happens on
# whitespace-normalized lowercase text, so wrapped doc lines still match.
DISALLOWED = (
    "mqtt control is experimental",
    "control is experimental",
    "experimental, per-device opt-in",
    "experimental/opt-in",
    "remains experimental",
    "experimental discovery preview",
    "experimental transport",
    "mqtt control is disabled by default",
    "all mqtt devices are telemetry-only",
    "always telemetry-only",
    "always forces write_output_limit=false",
    "manual config.json editing",
    "provisioned manually",
    "not yet provision",
    "fall back to conservative in-memory defaults",
    "conservative in-memory fallbacks stay off",
    # Cloud MQTT discovery feeds real config proposals and apply provisions the
    # runtime credential; no doc may still call it display-only or demand
    # out-of-band credential provisioning.
    "discovery is display-only",
    "discovery display only",
    "discovery-display only",
    "discovery-display-only",
    "today is display-only",
    "display-only: it lists cloud devices",
    "provisioned out of band",
    "out-of-band provisioning",
    "needs a provisioned credential",
    # Zendure MQTT devices are not generically telemetry-only: whether a device
    # is controllable is a per-device capability (verified write method).
    # Telemetry-only wording is allowed only when tied to that capability
    # ("no verified write method", "unsupported topic family"), never as a
    # blanket description of MQTT devices, proposals or broker profiles.
    "zendure mqtt telemetry-only",
    "telemetry-only zendure mqtt",
    "telemetry-only broker profile",
    "all mqtt proposals are telemetry-only",
    "telemetry-only device added",
    "these telemetry-only entries",
    "telemetry-only validation",
    "telemetry-only device(s)",
    "config proposals: telemetry-only",
)


def _normalized(rel_path):
    # Blockquote markers would otherwise split phrases wrapped across quoted
    # markdown lines; none of the checked phrases contain ">".
    text = (ROOT / rel_path).read_text(encoding="utf-8").replace(">", " ")
    return " ".join(text.split()).lower()


def _git_tracked_files():
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return {entry for entry in result.stdout.split("\0") if entry}


def test_terminology_sources_are_tracked_and_present():
    # Every scanned source must be a tracked, repository-owned file so the
    # wording contract holds in a clean CI checkout — never an ignored,
    # developer-local file that is absent there.
    tracked = _git_tracked_files()
    if tracked is None:
        pytest.skip("not a git checkout")

    missing = [rel for rel in FILES if not (ROOT / rel).is_file()]
    assert missing == [], f"terminology sources missing on disk: {missing}"

    untracked = [rel for rel in FILES if rel not in tracked]
    assert untracked == [], (
        "terminology sources are not tracked in git and would be absent from a "
        f"clean checkout: {untracked}"
    )


def test_terminology_contract_holds_in_clean_checkout():
    # Export the committed tree only (ignored/local-only files excluded) and run
    # the same disallowed-phrase scan against it, proving the contract depends on
    # no developer-workspace file.
    tracked = _git_tracked_files()
    if tracked is None:
        pytest.skip("not a git checkout")

    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD"],
        capture_output=True,
    )
    assert archive.returncode == 0, archive.stderr

    import io

    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        names = set(tar.getnames())
        offenders = []
        for rel in FILES:
            assert rel in names, f"{rel} is absent from a clean checkout"
            member = tar.extractfile(rel)
            text = member.read().decode("utf-8").replace(">", " ")
            normalized = " ".join(text.split()).lower()
            offenders += [
                f"{rel}: {phrase!r}"
                for phrase in DISALLOWED
                if phrase in normalized
            ]
    assert offenders == [], "\n".join(offenders)


def test_no_generic_mqtt_experimental_wording():
    offenders = [
        f"{rel_path}: {phrase!r}"
        for rel_path in FILES
        for phrase in DISALLOWED
        if phrase in _normalized(rel_path)
    ]
    assert offenders == [], "\n".join(offenders)


def test_supported_setups_frames_limits_as_hardware_validation():
    text = _normalized("docs/user/supported-setups.md")
    assert (
        "mqtt output control is supported where a verified write protocol exists"
        in text
    )


def test_supported_setups_requires_exact_model_and_both_write_gates():
    # Semantic contract for the MQTT control-authorization chain. Each assertion
    # proves a distinct safety property rather than pinning one exact sentence,
    # so honest rewording of the surrounding prose does not break the contract.
    text = _normalized("docs/user/supported-setups.md")

    # 1. Control requires an EXACT supported hardware model.
    assert "exact supported hardware model" in text
    # 2. On a compatible broker transport, 3. with a verified write protocol.
    assert "compatible broker transport" in text
    assert "verified write protocol" in text
    # 4. The per-device control capability must be enabled.
    assert "enabled control capability" in text
    # 5. The relevant transport write gate must be enabled.
    assert "enabled transport write gate" in text
    # Topic family / hardware generation alone never authorizes a write.
    assert "topic family and generation are evidence and telemetry grouping only" in text
    assert "enabled per device from its observed topic family" not in text
    # Unknown or conflicting model evidence stays telemetry-only.
    assert "telemetry-only" in text
    assert "unknown or conflicting model evidence" in text
    # A local broker seen publishing scalar metrics only is not a verified write
    # carrier, and the page must say which broker source that restriction is
    # about rather than blaming the telemetry family in general.
    assert "local" in text
    assert "scalar" in text
    assert "stays telemetry-only" in text or "stay telemetry-only" in text
    assert "zendure cloud broker" in text
    # Unvalidated legacy hardware is clearly identified as not physically
    # validated. Accept the honest phrasings ("validated"/"confirmed") tied to
    # physical/real hardware, rather than one brittle exact sentence.
    assert "physical hardware" in text or "real hardware" in text
    assert any(
        phrase in text
        for phrase in (
            "not been validated on physical hardware",
            "not validated on physical hardware",
            "not been confirmed on physical hardware",
            "not confirmed on physical hardware",
            "not been confirmed on real hardware",
            "not confirmed on real hardware",
        )
    )


def test_safety_model_documents_runtime_release_gate_defaults():
    text = _normalized("docs/technical/safety-model.md")
    assert "resolves them to the release defaults" in text


def test_connection_types_documents_automatic_cloud_provisioning():
    text = _normalized("docs/user/connection-types.md")
    assert "provisions the runtime mqtt credential" in text


def test_readme_does_not_label_cloud_transport_experimental():
    # A specific unvalidated hardware generation may be flagged, but the cloud
    # MQTT transport as a whole is a supported control transport.
    text = _normalized("README.md")
    assert "zendure cloud mqtt | experimental" not in text


def test_configuration_documents_apply_time_cloud_provisioning():
    text = _normalized("docs/technical/configuration.md")
    assert "setup and maintenance apply provision this record automatically" in text


def test_admin_discovery_documents_cloud_apply_path():
    # Cloud discovery results feed the same trusted proposal set as local
    # discovery; the docs must describe the selection -> apply path instead of
    # calling the source display-only.
    text = _normalized("docs/technical/admin-discovery.md")
    assert "same trusted proposal set" in text
