# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed input validation for the Appliance Manager.

Everything a browser can influence passes through these validators before it
reaches a host command, so each rejection here is a security boundary.
"""

import pytest

from appliance import validation
from appliance.config import load_allowed_images
from appliance.paths import AppliancePaths, PathBoundaryError, ensure_within
from appliance.redaction import bounded_redacted_log, clamp_log, redact_mapping, redact_text
from appliance.validation import ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ALLOWED = ("ghcr.io/basecubedev/ems-solarflow-admin",)
SOURCE = "https://github.com/basecubedev/ems-solarflow-api-control"


def labels(**overrides):
    values = {
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.version": "v0.8.0",
        "org.opencontainers.image.revision": "abc1234",
        "org.opencontainers.image.created": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return values


# --- release tags ----------------------------------------------------------


@pytest.mark.parametrize("tag", ["v0.8.0", "0.8.0", "v1.2.3-rc1", "v10.20.30"])
def test_valid_release_tags(tag):
    assert validation.validate_release_tag(tag) == tag


@pytest.mark.parametrize(
    "tag",
    [
        "latest",
        "main",
        "v0.8",
        "v0.8.0; rm -rf /",
        "v0.8.0 && reboot",
        "../../etc/passwd",
        "ghcr.io/evil/image:v1.0.0",
        "v0.8.0\nv0.9.0",
        "",
        None,
        42,
    ],
)
def test_rejected_release_tags(tag):
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_release_tag(tag)
    assert excinfo.value.code == "invalid_release_tag"


def test_mutable_channel_names_are_not_tags():
    # "latest stable" is a channel that must resolve server-side to a concrete
    # tag; it must never reach a docker pull as a tag by itself.
    with pytest.raises(ValidationError):
        validation.validate_release_tag("latest_stable")


def test_prerelease_detection():
    assert validation.is_prerelease_tag("v1.0.0-rc1")
    assert not validation.is_prerelease_tag("v1.0.0")


def test_normalize_version_ignores_v_prefix():
    assert validation.normalize_version("v1.2.3") == validation.normalize_version("1.2.3")


# --- image allowlist -------------------------------------------------------


def test_allowlisted_repository_is_accepted():
    assert validation.validate_image_repository(ALLOWED[0], ALLOWED) == ALLOWED[0]


@pytest.mark.parametrize(
    "repository",
    ["ghcr.io/attacker/ems-solarflow-admin", "docker.io/library/alpine", "localhost:5000/evil"],
)
def test_repository_outside_the_allowlist_is_refused(repository):
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_image_repository(repository, ALLOWED)
    assert excinfo.value.code in ("image_repository_not_allowed", "invalid_image_repository")


def test_allowed_images_file_is_the_authority(tmp_path):
    conf = tmp_path / "allowed-images.conf"
    conf.write_text(
        "# comment\n"
        "ghcr.io/basecubedev/ems-solarflow-admin\n"
        f"expected_source = {SOURCE}\n"
        "allow_prerelease = false\n",
        encoding="utf-8",
    )
    images = load_allowed_images(conf)
    assert images.repositories == ("ghcr.io/basecubedev/ems-solarflow-admin",)
    assert images.expected_source == SOURCE
    assert images.allow_prerelease is False


def test_missing_allowed_images_file_falls_back_to_the_packaged_default(tmp_path):
    images = load_allowed_images(tmp_path / "absent.conf")
    assert images.repositories == ("ghcr.io/basecubedev/ems-solarflow-admin",)


# --- OCI labels ------------------------------------------------------------


def test_matching_labels_are_accepted():
    result = validation.validate_oci_labels(
        labels(), requested_tag="v0.8.0", expected_source=SOURCE
    )
    assert result == {"legacy_exempt": False, "missing_labels": []}


def test_version_label_conflict_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_oci_labels(
            labels(**{"org.opencontainers.image.version": "v0.9.0"}),
            requested_tag="v0.8.0",
            expected_source=SOURCE,
        )
    assert excinfo.value.code == "image_version_mismatch"


def test_foreign_source_label_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_oci_labels(
            labels(**{"org.opencontainers.image.source": "https://github.com/attacker/evil"}),
            requested_tag="v0.8.0",
            expected_source=SOURCE,
        )
    assert excinfo.value.code == "image_source_mismatch"


def test_missing_labels_fail_closed():
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_oci_labels({}, requested_tag="v0.8.0", expected_source=SOURCE)
    assert excinfo.value.code == "image_labels_missing"


def test_legacy_exempt_tag_is_the_only_way_past_missing_labels():
    result = validation.validate_oci_labels(
        {}, requested_tag="v0.5.0", expected_source=SOURCE, legacy_exempt_tags=("v0.5.0",)
    )
    assert result["legacy_exempt"] is True


def test_uninspectable_image_labels_fail_closed():
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_oci_labels(None, requested_tag="v0.8.0", expected_source=SOURCE)
    assert excinfo.value.code == "image_labels_missing"


# --- architecture ----------------------------------------------------------


def test_supported_architecture_is_accepted():
    assert validation.validate_architecture("arm64", ("arm64",)) == "arm64"


def test_foreign_architecture_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_architecture("amd64", ("arm64",))
    assert excinfo.value.code == "architecture_mismatch"


# --- digests and references -----------------------------------------------


def test_digest_shape_is_enforced():
    digest = "sha256:" + "a" * 64
    assert validation.validate_digest(digest) == digest
    with pytest.raises(ValidationError):
        validation.validate_digest("sha256:short")


def test_digest_reference_is_built_from_validated_parts():
    reference = validation.build_digest_ref(ALLOWED[0], "sha256:" + "b" * 64)
    assert reference == f"{ALLOWED[0]}@sha256:{'b' * 64}"


# --- paths -----------------------------------------------------------------


def test_path_inside_the_boundary_resolves(tmp_path):
    assert ensure_within(tmp_path, tmp_path / "operations" / "a.json").parent.name == "operations"


@pytest.mark.parametrize("candidate", ["../escape", "../../etc/passwd", "/etc/shadow"])
def test_path_outside_the_boundary_is_refused(tmp_path, candidate):
    with pytest.raises(PathBoundaryError):
        ensure_within(tmp_path, candidate)


def test_symlinked_state_entry_cannot_redirect_a_write(tmp_path):
    base = tmp_path / "state"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "link").symlink_to(outside)
    with pytest.raises(PathBoundaryError):
        ensure_within(base, base / "link")


def test_appliance_layout_is_centrally_defined(tmp_path):
    paths = AppliancePaths(
        install_root=tmp_path / "opt",
        config_dir=tmp_path / "etc",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "log",
        runtime_dir=tmp_path / "run",
    )
    assert paths.agent_socket.name == "agent.sock"
    assert paths.appliance_conf.name == "appliance.conf"
    assert paths.allowed_images_conf.name == "allowed-images.conf"
    assert set(paths.export_paths()) == {"config", "backups", "data"}


# --- hostnames, accounts, scopes ------------------------------------------


@pytest.mark.parametrize("hostname", ["ems-solarflow", "pi5", "a", "ems-solarflow-01"])
def test_valid_hostnames(hostname):
    assert validation.validate_hostname(hostname) == hostname


@pytest.mark.parametrize(
    "hostname", ["-leading", "trailing-", "with space", "with_underscore", "a" * 64, "UPPER!"]
)
def test_invalid_hostnames(hostname):
    if hostname == "UPPER!":
        with pytest.raises(ValidationError):
            validation.validate_hostname(hostname)
        return
    with pytest.raises(ValidationError):
        validation.validate_hostname(hostname)


def test_hostname_is_lowercased():
    assert validation.validate_hostname("EMS-SolarFlow") == "ems-solarflow"


def test_account_must_be_allowlisted():
    assert validation.validate_account("ems-backup", ("ems-backup",)) == "ems-backup"
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_account("root", ("ems-backup",))
    assert excinfo.value.code == "account_not_allowed"


def test_container_must_be_appliance_managed():
    with pytest.raises(ValidationError) as excinfo:
        validation.validate_container_name("some-other-container", ("ems-solarflow-admin",))
    assert excinfo.value.code == "container_not_allowed"


def test_update_scope_and_repair_action_are_closed_sets():
    assert validation.validate_update_scope("security") == "security"
    with pytest.raises(ValidationError):
        validation.validate_update_scope("dist-upgrade")
    assert validation.validate_package_repair_action("fix_broken") == "fix_broken"
    with pytest.raises(ValidationError):
        validation.validate_package_repair_action("--allow-downgrades")


def test_log_source_is_a_closed_set():
    assert validation.validate_log_source("audit") == "audit"
    with pytest.raises(ValidationError):
        validation.validate_log_source("/var/log/secrets")


def test_line_count_is_clamped():
    assert validation.validate_line_count(None) == validation.DEFAULT_LOG_LINES
    assert validation.validate_line_count(10_000) == validation.MAX_LOG_LINES
    with pytest.raises(ValidationError):
        validation.validate_line_count(0)


def test_wifi_passphrase_length_bounds():
    assert validation.validate_wifi_passphrase("") == ""
    assert validation.validate_wifi_passphrase("a" * 12) == "a" * 12
    with pytest.raises(ValidationError):
        validation.validate_wifi_passphrase("short")
    with pytest.raises(ValidationError):
        validation.validate_wifi_passphrase("a" * 64)


# --- redaction -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password=supersecret",
        "PASSWORD: supersecret",
        'token="supersecret"',
        "Authorization: Bearer supersecretvalue",
        "mqtt://user:supersecret@broker.local",
        '{"api_key": "supersecret"}',
    ],
)
def test_secret_values_are_redacted(text):
    assert "supersecret" not in redact_text(text)


def test_public_key_body_is_not_logged_in_full():
    line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf u@h"
    redacted = redact_text(line)
    assert "AAAAC3NzaC1lZDI1NTE5" not in redacted
    assert "ssh-ed25519" in redacted


def test_private_key_block_is_redacted():
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecretbytes\n-----END OPENSSH PRIVATE KEY-----"
    assert "secretbytes" not in redact_text(block)


def test_mapping_redaction_is_recursive():
    payload = {"outer": {"password": "hunter2", "safe": "value"}, "list": [{"token": "abc"}]}
    redacted = redact_mapping(payload)
    assert redacted["outer"]["password"] == "***"
    assert redacted["outer"]["safe"] == "value"
    assert redacted["list"][0]["token"] == "***"


@pytest.mark.parametrize(
    "text",
    [
        "csrf_token=supersecret",
        "api_token: supersecret",
        "current_password=supersecret",
        'ha_device_list_token="supersecret"',
        '{"registry_token": "supersecret"}',
    ],
)
def test_a_qualified_secret_name_is_still_a_secret(text):
    """Only the bare name was redacted, so every qualified one leaked.

    ``\\b`` cannot match between ``_`` and ``t``, so ``csrf_token=`` never
    reached the rule that redacts ``token=``. The support archive is the last
    thing standing between an operator's session and a bug report, and these
    are the names this project actually uses.
    """

    assert "supersecret" not in redact_text(text)


@pytest.mark.parametrize(
    "key", ["csrf_token", "registry_token", "current_password", "client_secret_id"]
)
def test_a_qualified_secret_key_is_redacted_in_a_mapping(key):
    """Mapping redaction compared the whole key, so a prefix defeated it."""

    assert redact_mapping({key: "hunter2"})[key] == "***"


@pytest.mark.parametrize("key", ["device_key", "public_key", "host_key_fingerprint"])
def test_public_material_stays_readable(key):
    """Over-redaction would empty the bundle of what it is collected for.

    A fingerprint and a public key are the evidence an operator compares
    against the appliance, so ``key`` is deliberately not a secret name.
    """

    assert redact_mapping({key: "SHA256:abc"})[key] == "SHA256:abc"


def test_log_output_is_bounded_by_lines_and_bytes():
    clamped = clamp_log("\n".join(str(index) for index in range(5000)), max_lines=100)
    assert clamped["lines"] <= 100
    assert clamped["truncated"] is True

    big = clamp_log("x" * 10000, max_bytes=1000)
    assert len(big["text"].encode("utf-8")) <= 1000


def test_bounded_log_is_redacted_as_well():
    bounded = bounded_redacted_log("line one\npassword=supersecret\n", max_lines=10)
    assert "supersecret" not in bounded["text"]
