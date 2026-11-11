# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HomeWizard P1 Meter over its local API v2.

The meter answers HTTPS only, with a certificate HomeWizard's Appliance Access
CA issued for ``appliance/p1dongle/<serial>``. A connection trusts that CA and
nothing else, and checks that the certificate names the serial the config was
paired with, so a DHCP address that moved to another meter is refused rather
than read. Reads carry the Bearer token the meter hands out once its button
was pressed. Import-side-effect-free.
"""

import http.client
import json
import re
import secrets
import ssl
import time

from ems.config import (
    HOMEWIZARD_P1_DEFAULT_PORT,
    HOMEWIZARD_P1_PAIRING_NAME_PATTERN,
    HOMEWIZARD_P1_SERIAL_PATTERN,
    HOMEWIZARD_P1_TOKEN_PATTERN,
)

HOMEWIZARD_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDITCCAgkCFDn7cwYLioTM3VxdAygLl/Px9ovFMA0GCSqGSIb3DQEBCwUAME0x
CzAJBgNVBAYTAk5MMQswCQYDVQQIDAJaSDETMBEGA1UECgwKSG9tZVdpemFyZDEc
MBoGA1UEAwwTQXBwbGlhbmNlIEFjY2VzcyBDQTAeFw0yMTEyMTgxOTEyMTJaFw0z
MTEyMTYxOTEyMTJaME0xCzAJBgNVBAYTAk5MMQswCQYDVQQIDAJaSDETMBEGA1UE
CgwKSG9tZVdpemFyZDEcMBoGA1UEAwwTQXBwbGlhbmNlIEFjY2VzcyBDQTCCASIw
DQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAPBIvW8NRffqdvzHZY0M32fQHiGm
pJgNGhiaQmpJfRDhT9yihM0S/hYcN8IqnfrMqoCQb/56Ub0+dZizmtfcGsE+Lpm1
K1znkWqSDlpnuTNOb70TrsxBmbFuNOZQEi/xOjzT2j98wT0GSfxz1RVq6lZhDRRz
xoe08+Xo4+ttUGanfOggJi0BXygeFEVBpbctVVJ9EgqeEE9itjcMlcxMe1QN14f8
hCcOnId+9PSsdmyUCLrTB0FVYrbNfbJPk/vMU57fu6swBjWhYBxPx9ZhFy+7WnPR
9BFg4seHNVQIqZNrf1YwBXlmZQIL32SRPaiH/+AVNMrYGXBvncY0Km6ZHIMCAwEA
ATANBgkqhkiG9w0BAQsFAAOCAQEA6ybM8xm0PCXg8Rr/q0v1vPxQy44PmwXTDj0e
r2vW4ZMiEwXZCp0Kk2K16KJYz4iJyfiQk8ikAIMiRSbyXzmyQ7XmL1O4l4d8E1Pg
8EImvcyoBxFhd0Lq7VKriLc8Bw8SXbahPMGT+Y8Yz0uIsLAYVwlkLfgppVPmBaLD
QautcQnI8WxPvCIQf5anyzgAyJC5ac6/CkB+iyPcuWcG3RMYvXnC0QoTlRa5YMlE
FweVDlT2C/MdDyOxiAD/H1EP/eaySnU0zsxyD0yNFRKsQfQ+UJEPd2GS1AGA1lTy
CGdyYj/Gghrusw0hM4rYXQSERWGF0mpEnuJ+7bHDolHu0rzgTQ==
-----END CERTIFICATE-----
"""

P1_CERTIFICATE_PREFIX = "appliance/p1dongle/"
PAIRING_NAME_PREFIX = "local/ems_solarflow#"
OWN_PAIRING_NAME_PATTERN = re.compile(r"local/ems_solarflow#[0-9a-f]{6}")
PAIRING_WINDOW_S = 60.0
PAIRING_RETRY_S = 2.0
MAX_ANSWER_BYTES = 64 * 1024

REASON_UNREACHABLE = "unreachable"
REASON_NOT_A_P1_METER = "not_a_p1_meter"
REASON_OTHER_METER = "other_meter"
REASON_UNAUTHORIZED = "unauthorized"
REASON_BUTTON_NOT_PRESSED = "button_not_pressed"
REASON_NO_ROOM = "no_room"
REASON_HTTP_STATUS = "http_status"
REASON_INVALID_ANSWER = "invalid_answer"


class HomeWizardError(Exception):
    """A failed exchange with the meter; ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def tls_context(ca_pem=HOMEWIZARD_CA_PEM):
    """Trust HomeWizard's CA alone; the identity is the certificate's serial.

    The device has no DNS name, so the name check is the serial comparison in
    :class:`HomeWizardP1Api` instead of the hostname match. HomeWizard's root is
    an X.509 v1 certificate without extensions, which the strict chain check of
    current Python versions refuses although it is the only anchor trusted here.
    """

    context = ssl.create_default_context(cadata=ca_pem)
    context.check_hostname = False
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def pairing_name(stored=None):
    """The name this installation pairs under: the stored one, else a new one.

    The meter revokes the token of a name that pairs again. Each installation
    therefore keeps a name of its own in its config and only ever replaces its
    own pairing, so several installations can read one meter.
    """

    text = str(stored or "").strip()
    if HOMEWIZARD_P1_PAIRING_NAME_PATTERN.fullmatch(text):
        return text
    return PAIRING_NAME_PREFIX + secrets.token_hex(3)


def certificate_serial(certificate):
    """The serial a validated P1 Meter certificate names, or ``None``."""

    for rdn in (certificate or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName" and isinstance(value, str) and value.startswith(P1_CERTIFICATE_PREFIX):
                serial = value[len(P1_CERTIFICATE_PREFIX):].lower()
                return serial if HOMEWIZARD_P1_SERIAL_PATTERN.fullmatch(serial) else None
    return None


def _error_code(body):
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, str) else ""


class HomeWizardP1Api:
    """One P1 Meter at ``host``; the TLS connection is kept between requests."""

    def __init__(
        self,
        host,
        *,
        serial=None,
        token=None,
        port=HOMEWIZARD_P1_DEFAULT_PORT,
        timeout=3.0,
        context=None,
    ):
        self.host = host
        self.serial = serial
        self.token = token
        self.port = port
        self.timeout = timeout
        self.connected_serial = None
        self._context = context or tls_context()
        self._connection = None

    def identify(self):
        """Connect and return the serial the meter's certificate names."""

        self.close()
        self._connect()
        return self.connected_serial

    def measurement(self):
        """The meter's latest measurement as a JSON object."""

        status, body = self._request("GET", "/api/measurement", authorized=True)
        if status == 401:
            raise HomeWizardError(REASON_UNAUTHORIZED, "the P1 Meter refused the token; pair it again")
        if status != 200:
            raise HomeWizardError(REASON_HTTP_STATUS, f"HTTP {status} from /api/measurement")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise HomeWizardError(REASON_INVALID_ANSWER, "the P1 Meter answered no JSON") from exc
        if not isinstance(data, dict):
            raise HomeWizardError(REASON_INVALID_ANSWER, "the P1 Meter answered no JSON object")
        return data

    def pair(self, name):
        """Ask for a token for ``name`` once; the meter grants it only shortly after its button was pressed."""

        payload = json.dumps({"name": name}).encode("utf-8")
        status, body = self._request("POST", "/api/user", authorized=False, body=payload)
        if _error_code(body) == "user:no-storage":
            raise HomeWizardError(REASON_NO_ROOM, "the P1 Meter has no room for another pairing")
        if status == 403 and _error_code(body) == "user:creation-not-enabled":
            raise HomeWizardError(REASON_BUTTON_NOT_PRESSED, "press the button on the P1 Meter")
        if status != 200:
            raise HomeWizardError(REASON_HTTP_STATUS, f"HTTP {status} from /api/user")
        try:
            token = json.loads(body).get("token")
        except (ValueError, AttributeError) as exc:
            raise HomeWizardError(REASON_INVALID_ANSWER, "the P1 Meter answered no token") from exc
        if not isinstance(token, str) or not HOMEWIZARD_P1_TOKEN_PATTERN.fullmatch(token):
            raise HomeWizardError(REASON_INVALID_ANSWER, "the P1 Meter answered no valid token")
        return token

    def close(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _connect(self):
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=self._context
        )
        try:
            connection.connect()
        except ssl.SSLCertVerificationError as exc:
            connection.close()
            raise HomeWizardError(
                REASON_NOT_A_P1_METER, "the device's certificate was not issued by HomeWizard"
            ) from exc
        except OSError as exc:
            connection.close()
            raise HomeWizardError(REASON_UNREACHABLE, f"cannot reach the P1 Meter: {exc}") from exc
        serial = certificate_serial(connection.sock.getpeercert())
        if serial is None:
            connection.close()
            raise HomeWizardError(REASON_NOT_A_P1_METER, "the HomeWizard device is not a P1 Meter")
        if self.serial and serial != self.serial:
            connection.close()
            raise HomeWizardError(
                REASON_OTHER_METER, "the address answers as another P1 Meter than the paired one"
            )
        self.connected_serial = serial
        self._connection = connection

    def _request(self, method, path, *, authorized, body=None):
        headers = {"X-Api-Version": "2", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authorized:
            headers["Authorization"] = f"Bearer {self.token}"
        reused = self._connection is not None
        if not reused:
            self._connect()
        try:
            return self._exchange(method, path, body, headers)
        except TimeoutError as exc:
            self.close()
            raise HomeWizardError(REASON_UNREACHABLE, f"the P1 Meter did not answer: {exc}") from exc
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            if not reused:
                raise HomeWizardError(REASON_UNREACHABLE, f"the P1 Meter dropped the request: {exc}") from exc
        self._connect()
        try:
            return self._exchange(method, path, body, headers)
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            raise HomeWizardError(REASON_UNREACHABLE, f"the P1 Meter dropped the request: {exc}") from exc

    def _exchange(self, method, path, body, headers):
        self._connection.request(method, path, body=body, headers=headers)
        response = self._connection.getresponse()
        answer = response.read(MAX_ANSWER_BYTES + 1)
        if len(answer) > MAX_ANSWER_BYTES:
            self.close()
            raise HomeWizardError(REASON_INVALID_ANSWER, "the P1 Meter's answer is too large")
        if response.will_close:
            self.close()
        return response.status, answer


def pair_until_pressed(
    api,
    name,
    *,
    window_s=PAIRING_WINDOW_S,
    retry_s=PAIRING_RETRY_S,
    sleep=time.sleep,
    clock=time.monotonic,
    waiting=None,
):
    """Ask for a token until the button is pressed or ``window_s`` has passed."""

    deadline = clock() + window_s
    while True:
        try:
            return api.pair(name)
        except HomeWizardError as exc:
            if exc.reason != REASON_BUTTON_NOT_PRESSED or clock() + retry_s > deadline:
                raise
        if waiting is not None:
            waiting()
        sleep(retry_s)
