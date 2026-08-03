# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin HTTPS wrapper: install-root resolution and the /app write guard.

The Admin HTTPS listener reuses ``dashboard.https`` for all certificate work.
These tests only cover the Admin-specific behaviour: cert/key must land under
the mounted EMS install root, custom relative paths are honoured, and a missing
install root must never silently write into the container image directory.
"""

import ssl

import pytest

from ems import paths

pytestmark = [
    pytest.mark.admin,
    pytest.mark.integration,
    pytest.mark.simulation,
]

pytest.importorskip("cryptography")


def test_admin_ssl_context_generates_under_install_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    from admin.https import ensure_admin_ssl_context

    context = ensure_admin_ssl_context(
        {"host": "localhost", "ssl_auto_generate": True}
    )

    assert isinstance(context, ssl.SSLContext)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert (tmp_path / "config" / "admin.crt").exists()
    assert (tmp_path / "config" / "admin.key").exists()


def test_admin_ssl_context_honors_relative_cert_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    from admin.https import ensure_admin_ssl_context

    ensure_admin_ssl_context(
        {
            "host": "localhost",
            "ssl_cert_file": "config/custom-admin.crt",
            "ssl_key_file": "config/custom-admin.key",
            "ssl_auto_generate": True,
        }
    )

    assert (tmp_path / "config" / "custom-admin.crt").exists()
    assert (tmp_path / "config" / "custom-admin.key").exists()


def test_admin_ssl_context_reuses_existing_cert(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    from admin.https import ensure_admin_ssl_context

    ensure_admin_ssl_context({"host": "localhost", "ssl_auto_generate": True})
    cert = tmp_path / "config" / "admin.crt"
    first = cert.read_bytes()

    # A second call with the files already present must not regenerate them.
    ensure_admin_ssl_context({"host": "localhost", "ssl_auto_generate": True})
    assert cert.read_bytes() == first


def test_admin_ssl_context_refuses_missing_install_root(tmp_path, monkeypatch):
    # No EMS_INSTALL_DIR and a resolver root that does not exist: the guard must
    # refuse rather than create /app/config/admin.crt in the container image.
    monkeypatch.delenv("EMS_INSTALL_DIR", raising=False)
    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path / "does-not-exist"))

    from admin.https import ensure_admin_ssl_context

    with pytest.raises(RuntimeError, match="mounted EMS install directory"):
        ensure_admin_ssl_context({"host": "localhost", "ssl_auto_generate": True})


def test_admin_ssl_context_loads_existing_cert_without_write_access(
    tmp_path, monkeypatch
):
    # An existing readable cert/key pair must load even when the install root is
    # not writable (no generation is needed, so no write guard should apply).
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    import admin.https as admin_https

    # First call generates the pair while the root is still writable.
    admin_https.ensure_admin_ssl_context({"host": "localhost", "ssl_auto_generate": True})
    assert (tmp_path / "config" / "admin.crt").exists()

    # Now simulate a read-only install root; loading the existing pair must work.
    monkeypatch.setattr(admin_https, "install_dir_available", lambda **kwargs: False)
    context = admin_https.ensure_admin_ssl_context(
        {"host": "localhost", "ssl_auto_generate": True}
    )
    assert isinstance(context, ssl.SSLContext)


def test_admin_ssl_context_missing_cert_unsafe_root_fails(tmp_path, monkeypatch):
    # Missing cert/key + generation on + a non-writable root must refuse.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    import admin.https as admin_https

    monkeypatch.setattr(admin_https, "install_dir_available", lambda **kwargs: False)
    with pytest.raises(RuntimeError, match="mounted EMS install directory"):
        admin_https.ensure_admin_ssl_context(
            {"host": "localhost", "ssl_auto_generate": True}
        )


def test_admin_ssl_context_missing_cert_without_autogen_reports_missing(
    tmp_path, monkeypatch
):
    # Missing cert/key + generation off: fail with a clear missing-cert error,
    # not the install-root guard.
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    from admin.https import ensure_admin_ssl_context

    with pytest.raises(RuntimeError, match="certificate or key is missing"):
        ensure_admin_ssl_context({"host": "localhost", "ssl_auto_generate": False})
