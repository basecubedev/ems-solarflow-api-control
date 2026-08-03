# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import ssl

import pytest

from dashboard.https import (
    coerce_bool,
    ensure_dashboard_ssl_context,
    ensure_ssl_context,
    generate_self_signed_certificate,
)


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", " On "])
def test_coerce_bool_truthy_strings(value):
    assert coerce_bool(value, default=False) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " Off "])
def test_coerce_bool_falsey_strings(value):
    assert coerce_bool(value, default=True) is False


def test_coerce_bool_passes_through_real_bools():
    assert coerce_bool(True, default=False) is True
    assert coerce_bool(False, default=True) is False


def test_coerce_bool_none_uses_default():
    assert coerce_bool(None, default=True) is True
    assert coerce_bool(None, default=False) is False


def test_coerce_bool_unknown_string_uses_default():
    assert coerce_bool("maybe", default=True) is True
    assert coerce_bool("maybe", default=False) is False


def test_ssl_context_treats_string_false_auto_generate_as_disabled(tmp_path):
    # A JSON/env value of "false" must not read as truthy and silently generate.
    with pytest.raises(RuntimeError, match="is enabled but certificate or key is missing"):
        ensure_ssl_context(
            {"host": "localhost", "ssl_auto_generate": "false"},
            str(tmp_path),
            default_cert_file="config/x.crt",
            default_key_file="config/x.key",
            common_name="X",
            service_label="x",
        )


pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

pytestmark = [
    pytest.mark.integration,
]


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


def test_dashboard_certificate_common_name_stays_dashboard(tmp_path):
    cert_file = tmp_path / "dashboard.crt"
    key_file = tmp_path / "dashboard.key"

    generate_self_signed_certificate(cert_file, key_file, host="localhost")

    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "EMS Dashboard"


def test_self_signed_certificate_allows_custom_common_name(tmp_path):
    cert_file = tmp_path / "admin.crt"
    key_file = tmp_path / "admin.key"

    generate_self_signed_certificate(
        cert_file, key_file, host="localhost", common_name="EMS Admin Console"
    )

    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "EMS Admin Console"


def test_generic_ssl_context_uses_custom_default_paths(tmp_path):
    context = ensure_ssl_context(
        {"host": "localhost", "ssl_auto_generate": True},
        str(tmp_path),
        default_cert_file="config/admin.crt",
        default_key_file="config/admin.key",
        common_name="EMS Admin Console",
        service_label="admin",
    )

    assert isinstance(context, ssl.SSLContext)
    assert (tmp_path / "config" / "admin.crt").exists()
    assert (tmp_path / "config" / "admin.key").exists()


def test_generic_ssl_context_error_mentions_service_label(tmp_path):
    with pytest.raises(RuntimeError, match="admin HTTPS is enabled"):
        ensure_ssl_context(
            {"host": "localhost", "ssl_auto_generate": False},
            str(tmp_path),
            default_cert_file="config/admin.crt",
            default_key_file="config/admin.key",
            common_name="EMS Admin Console",
            service_label="admin",
        )
