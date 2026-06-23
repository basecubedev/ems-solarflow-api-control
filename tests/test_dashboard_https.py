# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import ssl

import pytest

from dashboard.https import ensure_dashboard_ssl_context, generate_self_signed_certificate


pytest.importorskip("cryptography")


def test_self_signed_certificate_generation_restricts_private_key(tmp_path):
    cert_file = tmp_path / "dashboard.crt"
    key_file = tmp_path / "dashboard.key"

    generate_self_signed_certificate(cert_file, key_file, host="127.0.0.1")

    assert cert_file.exists()
    assert key_file.exists()
    assert "BEGIN CERTIFICATE" in cert_file.read_text()
    assert "BEGIN RSA PRIVATE KEY" in key_file.read_text()
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600


def test_ssl_context_autogenerates_missing_files(tmp_path):
    context = ensure_dashboard_ssl_context(
        {
            "host": "localhost",
            "ssl_cert_file": "dashboard.crt",
            "ssl_key_file": "dashboard.key",
            "ssl_auto_generate": True,
        },
        str(tmp_path),
    )

    assert isinstance(context, ssl.SSLContext)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert (tmp_path / "dashboard.crt").exists()
    assert (tmp_path / "dashboard.key").exists()


def test_ssl_context_fails_when_auto_generate_is_disabled(tmp_path):
    with pytest.raises(RuntimeError, match="certificate or key is missing"):
        ensure_dashboard_ssl_context(
            {
                "host": "localhost",
                "ssl_cert_file": "dashboard.crt",
                "ssl_key_file": "dashboard.key",
                "ssl_auto_generate": False,
            },
            str(tmp_path),
        )
