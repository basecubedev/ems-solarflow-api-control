# SPDX-License-Identifier: AGPL-3.0-or-later
import datetime
import ipaddress
import os
import socket
import ssl


def ensure_dashboard_ssl_context(config, base_dir):
    cert_file = _resolve_path(
        base_dir,
        config.get("ssl_cert_file", os.path.join("config", "dashboard.crt")),
    )
    key_file = _resolve_path(
        base_dir,
        config.get("ssl_key_file", os.path.join("config", "dashboard.key")),
    )
    auto_generate = bool(config.get("ssl_auto_generate", True))

    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        if not auto_generate:
            raise RuntimeError(
                "dashboard HTTPS is enabled but certificate or key is missing"
            )
        generate_self_signed_certificate(
            cert_file,
            key_file,
            host=str(config.get("host", "localhost")),
        )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


def generate_self_signed_certificate(cert_file, key_file, host="localhost"):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError(
            "dashboard HTTPS auto-generation requires the cryptography package"
        ) from exc

    parent = os.path.dirname(cert_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    key_parent = os.path.dirname(key_file)
    if key_parent:
        os.makedirs(key_parent, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "EMS Dashboard"),
    ])
    now = datetime.datetime.now(datetime.UTC)
    names = _subject_alt_names(host)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(key_file, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass

    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _subject_alt_names(host):
    values = [
        x509_dns("localhost"),
        x509_ip("127.0.0.1"),
        x509_ip("::1"),
    ]

    for candidate in {host, socket.gethostname()}:
        if not candidate or candidate == "0.0.0.0":
            continue
        ip_name = x509_ip(candidate)
        values.append(ip_name if ip_name is not None else x509_dns(candidate))

    return [value for value in values if value is not None]


def x509_dns(value):
    from cryptography import x509

    return x509.DNSName(str(value))


def x509_ip(value):
    from cryptography import x509

    try:
        return x509.IPAddress(ipaddress.ip_address(str(value)))
    except ValueError:
        return None


def _resolve_path(base_dir, path):
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)
