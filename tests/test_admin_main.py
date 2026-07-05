# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin CLI: HTTPS flag/env parsing and the HTTP/HTTPS port-equality guard."""

import pytest

from admin import __main__ as admin_main

pytestmark = pytest.mark.simulation


def test_https_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EMS_ADMIN_HTTPS_ENABLED", raising=False)
    args = admin_main.parse_args([])
    assert args.https is False
    assert args.https_port == 8091
    assert args.https_cert_file == "config/admin.crt"
    assert args.https_key_file == "config/admin.key"
    assert args.https_auto_generate is True


def test_env_enables_https(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_ENABLED", "true")
    assert admin_main.parse_args([]).https is True


def test_env_disables_https(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_ENABLED", "false")
    assert admin_main.parse_args([]).https is False


def test_cli_flag_overrides_default_false(monkeypatch):
    monkeypatch.delenv("EMS_ADMIN_HTTPS_ENABLED", raising=False)
    assert admin_main.parse_args(["--https"]).https is True


def test_cli_no_https_overrides_env_true(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_ENABLED", "true")
    args = admin_main.parse_args(["--no-https"])
    assert args.https is False


def test_cli_https_overrides_env_false(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_ENABLED", "false")
    assert admin_main.parse_args(["--https"]).https is True


def test_invalid_boolean_env_raises_system_exit(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_ENABLED", "maybe")
    with pytest.raises(SystemExit):
        admin_main.parse_args([])


def test_invalid_port_env_raises_system_exit(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_PORT", "not-a-number")
    with pytest.raises(SystemExit):
        admin_main.parse_args([])


def test_env_seeds_https_paths_and_port(monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_HTTPS_PORT", "9443")
    monkeypatch.setenv("EMS_ADMIN_HTTPS_CERT_FILE", "config/custom.crt")
    monkeypatch.setenv("EMS_ADMIN_HTTPS_KEY_FILE", "config/custom.key")
    monkeypatch.setenv("EMS_ADMIN_HTTPS_AUTO_GENERATE", "false")
    args = admin_main.parse_args([])
    assert args.https_port == 9443
    assert args.https_cert_file == "config/custom.crt"
    assert args.https_key_file == "config/custom.key"
    assert args.https_auto_generate is False


def test_no_https_auto_generate_flag(monkeypatch):
    monkeypatch.delenv("EMS_ADMIN_HTTPS_AUTO_GENERATE", raising=False)
    assert admin_main.parse_args(["--no-https-auto-generate"]).https_auto_generate is False
    assert admin_main.parse_args([]).https_auto_generate is True


def test_equal_http_and_https_ports_are_rejected(monkeypatch):
    monkeypatch.delenv("EMS_ADMIN_HTTPS_ENABLED", raising=False)
    # Port 0 binds an ephemeral HTTP port; equal HTTP/HTTPS ports must be
    # rejected before any HTTPS listener starts. The HTTP server is closed first.
    with pytest.raises(SystemExit, match="must differ"):
        admin_main.main(
            ["--host", "127.0.0.1", "--port", "0", "--https", "--https-port", "0"]
        )
