# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ephemeral Mosquitto container helper for optional real-broker integration.

Docker-only and self-contained: no external network service, a random host port,
and guaranteed container cleanup. Skips cleanly when Docker is unavailable.
"""

import contextlib
import os
import socket
import subprocess
import threading
import time
import uuid
from shutil import which

MOSQUITTO_IMAGE = "eclipse-mosquitto:2"

REQUIRE_REAL_MQTT_ENV = "EMS_REQUIRE_REAL_MQTT_TESTS"


def docker_available() -> bool:
    if not which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def real_mqtt_tests_required() -> bool:
    return os.environ.get(REQUIRE_REAL_MQTT_ENV) == "1"


def require_real_broker_environment(*required_imports) -> None:
    """Module-level gate: skip locally, fail closed when release CI requires.

    With ``EMS_REQUIRE_REAL_MQTT_TESTS=1`` a missing broker environment must
    fail the run — a release gate must never go green by skipping. Extra
    ``required_imports`` are gated the same way, so a module cannot slip past
    the gate through a plain ``importorskip``.
    """

    import importlib

    import pytest

    reason = None
    for module in ("paho.mqtt.client", *required_imports):
        try:
            importlib.import_module(module)
        except ImportError:
            reason = f"{module} is not installed"
            break
    if reason is None and not which("docker"):
        reason = "the Docker CLI is not installed"
    if reason is None and (
        subprocess.run(["docker", "info"], capture_output=True).returncode != 0
    ):
        reason = "the Docker daemon is not reachable"
    if reason is None:
        return
    if real_mqtt_tests_required():
        pytest.fail(
            f"{REQUIRE_REAL_MQTT_ENV}=1: real Mosquitto tests are required"
            f" but {reason}",
            pytrace=False,
        )
    pytest.skip(f"Real broker unavailable: {reason}", allow_module_level=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(host, port, timeout=15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            with socket.create_connection((host, port), timeout=1.0):
                return True
        time.sleep(0.1)
    return False


def _wait_for_tls(host, port, timeout=15.0) -> bool:
    """Wait until the broker completes a TLS handshake, not just accepts TCP.

    An open port precedes TLS readiness, so a client connecting in that window
    sees ``SSLEOFError`` when the broker drops the half-open handshake. The
    handshake here is unverified — it only proves the TLS listener is live.
    """

    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0) as raw:
                with context.wrap_socket(raw, server_hostname=host):
                    return True
        except (OSError, ssl.SSLError):
            time.sleep(0.1)
    return False


@contextlib.contextmanager
def mosquitto_broker(
    tmp_path, *, username=None, password=None, include_container_name=False
):
    """Start a throwaway Mosquitto broker; yield ``(host, port)``.

    With ``username``/``password`` the broker requires authentication and rejects
    anonymous connections; otherwise it allows anonymous access. The container is
    always removed on exit.
    """

    host = "127.0.0.1"
    port = free_port()
    conf = tmp_path / "mosquitto.conf"
    lines = ["listener 1883 0.0.0.0", "persistence false"]
    mounts = ["-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro"]
    if username:
        pwfile = tmp_path / "passwd"
        # mosquitto_passwd is available inside the image; build the file with it.
        pwfile.write_text("")
        lines += ["allow_anonymous false", "password_file /mosquitto/config/passwd"]
        mounts += ["-v", f"{pwfile}:/mosquitto/config/passwd"]
    else:
        lines.append("allow_anonymous true")
    conf.write_text("\n".join(lines) + "\n")

    name = f"ems-mosq-{uuid.uuid4().hex[:8]}"
    run = ["docker", "run", "-d", "--rm", "--name", name,
           "-p", f"{port}:1883", *mounts, MOSQUITTO_IMAGE]
    result = subprocess.run(run, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed to start mosquitto: {result.stderr}")
    try:
        if username:
            # Populate the password file inside the running container, then reload.
            subprocess.run(
                ["docker", "exec", name, "mosquitto_passwd", "-b",
                 "/mosquitto/config/passwd", username, password],
                capture_output=True, text=True, check=True,
            )
            subprocess.run(["docker", "kill", "-s", "HUP", name],
                           capture_output=True, text=True)
        if not _wait_for_port(host, port):
            logs = subprocess.run(["docker", "logs", name], capture_output=True,
                                  text=True)
            raise RuntimeError(f"mosquitto never became ready: {logs.stderr}")
        if include_container_name:
            yield host, port, name
        else:
            yield host, port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _new_paho_client():
    import paho.mqtt.client as mqtt

    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        return mqtt.Client()


class PublishReadinessError(RuntimeError):
    """A publish that never reached a connected broker.

    The message carries only non-secret diagnostics (endpoint, topic and the
    connect/publish reason codes) — never MQTT usernames or passwords.
    """


def _connect_reason_is_success(reason_code):
    """Map an ``on_connect`` reason code to a success boolean.

    Paho v2 (``CallbackAPIVersion.VERSION2``) delivers a ``ReasonCode`` exposing
    ``is_failure``; the legacy callback delivers an integer where ``0`` means a
    successful CONNACK.
    """

    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return not is_failure
    return reason_code == 0


def _reason_code_text(reason_code):
    if reason_code is None:
        return "none"
    return str(getattr(reason_code, "value", reason_code))


def _client_is_connected(client):
    is_connected = getattr(client, "is_connected", None)
    if callable(is_connected):
        with contextlib.suppress(Exception):
            return bool(is_connected())
    return True


def _readiness_error(reason, *, host, port, topic, connect_rc=None, publish_rc=None):
    details = [
        f"host={host}",
        f"port={port}",
        f"topic={topic}",
        f"connect_rc={_reason_code_text(connect_rc)}",
    ]
    if publish_rc is not None:
        details.append(f"publish_rc={publish_rc}")
    return PublishReadinessError(f"{reason} ({', '.join(details)})")


def _await_connack_and_publish(client, *, host, port, topic, payload, timeout):
    """Publish exactly once, but only after a successful CONNACK.

    The network loop delivers CONNACK asynchronously, so publishing straight
    after ``connect()`` can race the connection and fail with
    ``MQTT_ERR_NO_CONN``. Connection readiness is gated on the ``on_connect``
    reason code through an event — never a fixed sleep — and the loop is always
    stopped and the client disconnected. ``client`` is injectable so the
    readiness contract can be exercised against a fake Paho client.
    """

    import paho.mqtt.client as mqtt

    settled = threading.Event()
    state = {"connect_rc": None, "connected": False}

    def _on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        state["connect_rc"] = reason_code
        state["connected"] = _connect_reason_is_success(reason_code)
        settled.set()

    client.on_connect = _on_connect
    client.connect(host, port, keepalive=10)
    client.loop_start()
    try:
        if not settled.wait(timeout):
            raise _readiness_error(
                "broker did not acknowledge the connection before timeout",
                host=host, port=port, topic=topic, connect_rc=state["connect_rc"],
            )
        if not state["connected"]:
            raise _readiness_error(
                "broker refused the connection",
                host=host, port=port, topic=topic, connect_rc=state["connect_rc"],
            )
        if not _client_is_connected(client):
            raise _readiness_error(
                "broker disconnected before publish",
                host=host, port=port, topic=topic, connect_rc=state["connect_rc"],
            )
        info = client.publish(topic, payload, qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise _readiness_error(
                "client rejected the publish",
                host=host, port=port, topic=topic,
                connect_rc=state["connect_rc"], publish_rc=info.rc,
            )
        info.wait_for_publish(timeout)
        if not info.is_published():
            raise _readiness_error(
                "publish did not complete before timeout",
                host=host, port=port, topic=topic,
                connect_rc=state["connect_rc"], publish_rc=info.rc,
            )
    finally:
        client.loop_stop()
        client.disconnect()


def publish_once(host, port, topic, payload, *, username=None, password=None,
                 tls_ca=None, tls_insecure=False, timeout=10.0):
    """Publish a single retained-free message to a broker and disconnect.

    Waits for a successful CONNACK before publishing so a publish never races
    ahead of the connection on a slow runner.
    """

    client = _new_paho_client()
    if username:
        client.username_pw_set(username, password)
    if tls_ca is not None or tls_insecure:
        client.tls_set(ca_certs=str(tls_ca) if tls_ca else None)
        if tls_insecure:
            client.tls_insecure_set(True)
    _await_connack_and_publish(
        client, host=host, port=port, topic=topic, payload=payload, timeout=timeout
    )


def wait_until(predicate, *, timeout=10.0, message="condition not met", interval=0.02):
    """Poll ``predicate`` until it returns a truthy value or ``timeout`` elapses.

    Returns the truthy value; raises ``AssertionError`` with ``message`` on
    timeout. A bounded, primitive-based readiness wait — used instead of a fixed
    sleep so a slow subscription can't turn into a spurious failure.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"{message} (waited {timeout:.1f}s)")


def publish_until(publish, predicate, *, timeout=10.0, message="condition not met",
                  republish_interval=0.25):
    """Publish repeatedly until ``predicate`` is truthy, bounded by ``timeout``.

    Delivery here is not retained, so a publish that races ahead of a
    subscriber's SUBACK is silently dropped. Re-publishing until the effect is
    observed removes that race deterministically instead of trusting a fixed
    pre-publish sleep. Returns the truthy value; raises ``AssertionError`` with
    ``message`` on timeout.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        publish()
        stop = min(deadline, time.monotonic() + republish_interval)
        while time.monotonic() < stop:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
    raise AssertionError(f"{message} (waited {timeout:.1f}s)")


@contextlib.contextmanager
def mosquitto_acl_broker(tmp_path, accounts):
    """Start a Mosquitto broker enforcing per-account topic ACLs.

    ``accounts`` is a list of ``(username, password, topic_pattern)``; each user
    may only publish/subscribe within its own topic pattern. Anonymous access is
    denied. Yields ``(host, port)`` and always removes the container on exit.
    """

    host = "127.0.0.1"
    port = free_port()
    conf = tmp_path / "mosquitto.conf"
    pwfile = tmp_path / "passwd"
    aclfile = tmp_path / "aclfile"
    pwfile.write_text("")
    acl_lines = []
    for username, _password, pattern in accounts:
        acl_lines += [f"user {username}", f"topic readwrite {pattern}", ""]
    aclfile.write_text("\n".join(acl_lines) + "\n")
    conf.write_text(
        "\n".join(
            [
                "listener 1883 0.0.0.0",
                "persistence false",
                "allow_anonymous false",
                "password_file /mosquitto/config/passwd",
                "acl_file /mosquitto/config/aclfile",
            ]
        )
        + "\n"
    )
    mounts = [
        "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro",
        "-v", f"{pwfile}:/mosquitto/config/passwd",
        "-v", f"{aclfile}:/mosquitto/config/aclfile:ro",
    ]
    name = f"ems-mosq-{uuid.uuid4().hex[:8]}"
    run = ["docker", "run", "-d", "--rm", "--name", name,
           "-p", f"{port}:1883", *mounts, MOSQUITTO_IMAGE]
    result = subprocess.run(run, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed to start mosquitto: {result.stderr}")
    try:
        for username, password, _pattern in accounts:
            subprocess.run(
                ["docker", "exec", name, "mosquitto_passwd", "-b",
                 "/mosquitto/config/passwd", username, password],
                capture_output=True, text=True, check=True,
            )
        subprocess.run(["docker", "kill", "-s", "HUP", name],
                       capture_output=True, text=True)
        if not _wait_for_port(host, port):
            logs = subprocess.run(["docker", "logs", name], capture_output=True,
                                  text=True)
            raise RuntimeError(f"mosquitto never became ready: {logs.stderr}")
        yield host, port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def generate_tls_material(tmp_path, *, common_ip="127.0.0.1", san_dns=None):
    """Create a throwaway CA and a server cert/key inside ``tmp_path``.

    Returns ``(ca_path, cert_path, key_path)``. By default the server certificate
    carries an IP SAN for ``common_ip`` so ordinary hostname verification passes.
    Pass ``san_dns`` to instead issue a CA-valid cert whose SAN deliberately does
    not match the connect address, so only a no-verify client tolerates it.
    Everything stays inside the temporary test directory.
    """

    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def _key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ems-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = _key()
    subject_cn = san_dns or common_ip
    san = (
        x509.DNSName(san_dns)
        if san_dns
        else x509.IPAddress(ipaddress.ip_address(common_ip))
    )
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = tmp_path / "ca.crt"
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


@contextlib.contextmanager
def mosquitto_tls_broker(tmp_path, *, san_dns=None):
    """Start a TLS-only Mosquitto broker with a throwaway CA.

    Yields ``(host, port, ca_path)``. The broker authenticates itself with a cert
    signed by ``ca_path`` and accepts anonymous clients over TLS; clients are not
    asked for a certificate. Pass ``san_dns`` to issue a CA-valid server cert
    whose SAN does not match the connect address. Container is always removed on
    exit.
    """

    host = "127.0.0.1"
    port = free_port()
    ca_path, cert_path, key_path = generate_tls_material(
        tmp_path, common_ip=host, san_dns=san_dns
    )
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        "\n".join(
            [
                "listener 8883 0.0.0.0",
                "persistence false",
                "allow_anonymous true",
                "cafile /mosquitto/config/ca.crt",
                "certfile /mosquitto/config/server.crt",
                "keyfile /mosquitto/config/server.key",
                "require_certificate false",
            ]
        )
        + "\n"
    )
    mounts = [
        "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro",
        "-v", f"{ca_path}:/mosquitto/config/ca.crt:ro",
        "-v", f"{cert_path}:/mosquitto/config/server.crt:ro",
        "-v", f"{key_path}:/mosquitto/config/server.key:ro",
    ]
    name = f"ems-mosq-{uuid.uuid4().hex[:8]}"
    run = ["docker", "run", "-d", "--rm", "--name", name,
           "-p", f"{port}:8883", *mounts, MOSQUITTO_IMAGE]
    result = subprocess.run(run, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed to start mosquitto: {result.stderr}")
    try:
        if not _wait_for_port(host, port) or not _wait_for_tls(host, port):
            logs = subprocess.run(["docker", "logs", name], capture_output=True,
                                  text=True)
            raise RuntimeError(f"mosquitto TLS never became ready: {logs.stderr}")
        yield host, port, ca_path
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
