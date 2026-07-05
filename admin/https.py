# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Console HTTPS defaults and install-root resolution.

Thin wrapper only: certificate generation and the ``SSLContext`` build live in
the shared ``dashboard.https`` helper so there is exactly one copy of that code.
This module adds Admin-specific default paths and resolves them relative to the
real EMS install root (never the Admin container's ``/app``), mirroring how the
shared password file is handled in ``admin.auth``.
"""

import os

from admin.auth import install_dir_available
from admin.install_context import detect_install_context
from dashboard.https import _resolve_path, coerce_bool, ensure_ssl_context

DEFAULT_ADMIN_SSL_CERT_FILE = os.path.join("config", "admin.crt")
DEFAULT_ADMIN_SSL_KEY_FILE = os.path.join("config", "admin.key")
DEFAULT_ADMIN_HTTPS_PORT = 8091


def ensure_admin_ssl_context(config, base_dir=None):
    """Build the Admin ``SSLContext`` against the real EMS install root.

    Relative cert/key paths resolve against ``<EMS_INSTALL_DIR>`` so any
    generated files land at ``config/admin.crt`` / ``config/admin.key`` on the
    mounted host install, never inside the read-only container image. Existing
    readable cert/key files load even when that root is not writable; only
    *generating* a missing pair requires a writable, mounted install root.
    """

    context = detect_install_context(base_dir=base_dir)
    install_root = str(context.install_root)

    cert_file = _resolve_path(
        install_root, config.get("ssl_cert_file", DEFAULT_ADMIN_SSL_CERT_FILE)
    )
    key_file = _resolve_path(
        install_root, config.get("ssl_key_file", DEFAULT_ADMIN_SSL_KEY_FILE)
    )

    have_pair = os.path.exists(cert_file) and os.path.exists(key_file)
    auto_generate = coerce_bool(config.get("ssl_auto_generate"), default=True)
    if not have_pair and auto_generate and not install_dir_available(base_dir=base_dir):
        # Missing cert/key and generation is on: without a mounted install root
        # the only writable target is the container image dir. Refuse rather than
        # write /app/config/admin.crt. (Missing pair with auto-generate off falls
        # through to ensure_ssl_context's clear "certificate or key is missing".)
        raise RuntimeError("admin HTTPS needs a mounted EMS install directory")

    return ensure_ssl_context(
        config,
        install_root,
        default_cert_file=DEFAULT_ADMIN_SSL_CERT_FILE,
        default_key_file=DEFAULT_ADMIN_SSL_KEY_FILE,
        common_name="EMS Admin Console",
        service_label="admin",
    )
