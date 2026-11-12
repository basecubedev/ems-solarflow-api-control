# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local HTTPS stand-in for a HomeWizard P1 Meter, shared by its tests.

A test CA stands in for HomeWizard's: the meter's certificate names
``appliance/p1dongle/<serial>`` and, like the real one, carries no authority
key identifier. The meter speaks HTTP/1.1 keep-alive on 127.0.0.1 only.
"""

import datetime
import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SERIAL = "0a1b2c3d4e5f"
TOKEN = "0123456789ABCDEF0123456789ABCDEF"
MEASUREMENT = {
    "protocol_version": 50,
    "timestamp": "2026-11-12T03:14:27",
    "power_w": -678,
    "power_l1_w": -676,
}


def _name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(subject, issuer, key, signing_key, *, ca):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    return builder.sign(signing_key, hashes.SHA256())


def pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def authority(common_name="Test Appliance Access CA"):
    key = ec.generate_private_key(ec.SECP256R1())
    return key, _certificate(_name(common_name), _name(common_name), key, key, ca=True)


def _leaf(tmp_path, ca_key, ca_cert, common_name):
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = _certificate(_name(common_name), ca_cert.subject, key, ca_key, ca=False)
    cert_path = tmp_path / "meter.crt"
    key_path = tmp_path / "meter.key"
    cert_path.write_text(pem(certificate))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class Meter:
    """What the fake meter answers; tests change it between requests."""

    def __init__(self):
        self.pressed = False
        self.full = False
        self.users = {"local/preset": TOKEN}
        self.issued = 0
        self.measurement = dict(MEASUREMENT)
        self.connections = 0
        self.requests = []
        self.drop_after_answer = False

    def grant(self, name):
        """A new token for ``name``; the one it had before stops working, as on the real meter."""

        self.issued += 1
        self.users[name] = f"{self.issued:032X}"
        return self.users[name]


def _handler(meter):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            meter.connections += 1
            super().setup()

        def _answer(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = meter.drop_after_answer

        def do_GET(self):
            meter.requests.append(("GET", self.path, dict(self.headers)))
            if self.path != "/api/measurement":
                self._answer(404, {"error": "request:not-found"})
            elif self.headers.get("Authorization") not in {f"Bearer {token}" for token in meter.users.values()}:
                self._answer(401, {"error": "user:unauthorized"})
            else:
                self._answer(200, meter.measurement)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            meter.requests.append(("POST", self.path, dict(self.headers), body))
            if not meter.pressed:
                self._answer(403, {"error": "user:creation-not-enabled"})
            elif meter.full and body.get("name") not in meter.users:
                self._answer(403, {"error": "user:no-storage"})
            else:
                self._answer(200, {"token": meter.grant(body.get("name")), "name": body.get("name")})

        def log_message(self, *_args):
            return

    return Handler


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        return


@contextmanager
def serving(directory, common_name=f"appliance/p1dongle/{SERIAL}", issuer=None):
    """A running meter: yields it, its port and the PEM of the authority that signed it."""

    ca_key, ca_cert = issuer or authority()
    cert_path, key_path = _leaf(directory, ca_key, ca_cert, common_name)
    with serving_files(cert_path, key_path) as (meter, port):
        yield meter, port, pem(ca_cert)


@contextmanager
def serving_files(cert_path, key_path):
    """A running meter with the certificate and key in these files: yields it and its port."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    meter = Meter()
    server = QuietServer(("127.0.0.1", 0), _handler(meter))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield meter, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
