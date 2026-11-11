# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HomeWizard API v2 client against the local meter of ``homewizard_meter``."""

import shutil
import ssl
import subprocess
from contextlib import ExitStack

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.x509 import Version, load_pem_x509_certificate

from ems import homewizard
from ems.clients import HomeWizardP1Client
from tests.homewizard_meter import MEASUREMENT, SERIAL, TOKEN, authority, pem, serving, serving_files

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.integration,
]


@pytest.fixture
def served(tmp_path):
    with ExitStack() as stack:
        def serve(common_name=f"appliance/p1dongle/{SERIAL}", issuer=None):
            return stack.enter_context(serving(tmp_path, common_name, issuer))

        yield serve


def _api(port, ca_pem, **kwargs):
    return homewizard.HomeWizardP1Api(
        "127.0.0.1", port=port, timeout=2.0, context=homewizard.tls_context(ca_pem), **kwargs
    )


def test_the_measurement_is_read_with_the_token_and_the_api_version(served):
    meter, port, ca_pem = served()
    api = _api(port, ca_pem, serial=SERIAL, token=TOKEN)

    assert api.measurement() == MEASUREMENT
    method, path, headers = meter.requests[0]
    assert (method, path) == ("GET", "/api/measurement")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Api-Version"] == "2"
    api.close()


def test_the_connection_is_kept_between_reads(served):
    meter, port, ca_pem = served()
    api = _api(port, ca_pem, serial=SERIAL, token=TOKEN)

    api.measurement()
    api.measurement()

    assert meter.connections == 1
    api.close()


def test_a_connection_the_meter_dropped_after_its_answer_is_opened_again_for_the_next_read(served):
    meter, port, ca_pem = served()
    meter.drop_after_answer = True
    api = _api(port, ca_pem, serial=SERIAL, token=TOKEN)

    first = api.measurement()
    second = api.measurement()

    assert first == second == MEASUREMENT
    assert meter.connections == 2
    api.close()


def _openssl_v1_meter(directory):
    """HomeWizard's root is X.509 v1; cryptography cannot build one, the OpenSSL CLI can."""

    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("the openssl command is required to build an X.509 v1 root")
    config = directory / "min.cnf"
    config.write_text("[req]\ndistinguished_name = dn\n[dn]\n")

    def run(*args):
        result = subprocess.run([openssl, *args], cwd=directory, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"this openssl cannot build the certificates: {result.stderr.strip()[:200]}")

    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key")
    run("req", "-x509", "-x509v1", "-new", "-key", "ca.key", "-subj", "/CN=Test v1 CA", "-config", "min.cnf", "-days", "1", "-out", "ca.pem")
    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "meter.key")
    run("req", "-new", "-key", "meter.key", "-subj", f"/CN=appliance\\/p1dongle\\/{SERIAL}", "-config", "min.cnf", "-out", "meter.csr")
    run("x509", "-req", "-in", "meter.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "1", "-out", "meter.pem")
    ca_pem = (directory / "ca.pem").read_text()
    if load_pem_x509_certificate(ca_pem.encode("ascii")).version is not Version.v1:
        pytest.skip("this openssl does not build X.509 v1 certificates")
    return directory / "meter.pem", directory / "meter.key", ca_pem


def test_a_meter_under_an_x509_v1_root_like_homewizards_is_accepted(tmp_path):
    cert_path, key_path, ca_pem = _openssl_v1_meter(tmp_path)
    strict = homewizard.tls_context(ca_pem)
    strict.verify_flags |= ssl.VERIFY_X509_STRICT

    with serving_files(cert_path, key_path) as (_meter, port):
        with pytest.raises(homewizard.HomeWizardError) as refused:
            homewizard.HomeWizardP1Api("127.0.0.1", port=port, timeout=2.0, context=strict).identify()
        assert refused.value.reason == homewizard.REASON_NOT_A_P1_METER
        assert _api(port, ca_pem).identify() == SERIAL


def test_a_meter_certificate_without_authority_key_identifier_needs_the_relaxed_chain_check(served):
    _meter, port, ca_pem = served()
    strict = homewizard.tls_context(ca_pem)
    strict.verify_flags |= ssl.VERIFY_X509_STRICT

    with pytest.raises(homewizard.HomeWizardError) as refused:
        homewizard.HomeWizardP1Api("127.0.0.1", port=port, timeout=2.0, context=strict).identify()
    assert refused.value.reason == homewizard.REASON_NOT_A_P1_METER
    assert _api(port, ca_pem).identify() == SERIAL


def test_a_certificate_from_another_authority_is_refused(served):
    _meter, port, _ca_pem = served()
    _other_key, other_ca = authority("Someone else")

    with pytest.raises(homewizard.HomeWizardError) as refused:
        _api(port, pem(other_ca), serial=SERIAL, token=TOKEN).measurement()

    assert refused.value.reason == homewizard.REASON_NOT_A_P1_METER


def test_another_meter_at_the_paired_address_is_refused_before_the_token_is_sent(served):
    meter, port, ca_pem = served(common_name="appliance/p1dongle/ffeeddccbbaa")

    with pytest.raises(homewizard.HomeWizardError) as refused:
        _api(port, ca_pem, serial=SERIAL, token=TOKEN).measurement()

    assert refused.value.reason == homewizard.REASON_OTHER_METER
    assert "ffeeddccbbaa" not in str(refused.value)
    assert SERIAL not in str(refused.value)
    assert meter.requests == []


@pytest.mark.parametrize("common_name", ["appliance/energysocket/0a1b2c3d4e5f", "appliance/p1dongle/not-a-serial", "router.local"])
def test_a_homewizard_certificate_that_names_no_p1_meter_is_refused(served, common_name):
    _meter, port, ca_pem = served(common_name=common_name)

    with pytest.raises(homewizard.HomeWizardError) as refused:
        _api(port, ca_pem).identify()

    assert refused.value.reason == homewizard.REASON_NOT_A_P1_METER


def test_a_refused_token_says_to_pair_again(served):
    _meter, port, ca_pem = served()

    with pytest.raises(homewizard.HomeWizardError) as refused:
        _api(port, ca_pem, serial=SERIAL, token="F" * 32).measurement()

    assert refused.value.reason == homewizard.REASON_UNAUTHORIZED
    assert "pair it again" in str(refused.value)


def test_pairing_waits_for_the_button_and_returns_the_token(served):
    meter, port, ca_pem = served()
    api = _api(port, ca_pem)
    name = homewizard.pairing_name()
    waits = []

    def press_on_second_wait():
        waits.append(True)
        meter.pressed = len(waits) >= 2

    token = homewizard.pair_until_pressed(api, name, sleep=lambda _s: None, waiting=press_on_second_wait)

    assert token == meter.users[name]
    assert api.connected_serial == SERIAL
    assert len(waits) == 2
    posts = [request for request in meter.requests if request[0] == "POST"]
    assert {request[3]["name"] for request in posts} == {name}
    assert all(request[2]["X-Api-Version"] == "2" for request in posts)
    api.close()


def test_two_installations_pair_one_meter_and_each_pairing_again_revokes_only_its_own_token(served):
    meter, port, ca_pem = served()
    meter.pressed = True
    first_name, second_name = homewizard.pairing_name(), homewizard.pairing_name()
    assert first_name != second_name

    first = _api(port, ca_pem).pair(first_name)
    second = _api(port, ca_pem).pair(second_name)
    assert _api(port, ca_pem, serial=SERIAL, token=first).measurement() == MEASUREMENT
    assert _api(port, ca_pem, serial=SERIAL, token=second).measurement() == MEASUREMENT

    renewed = _api(port, ca_pem).pair(first_name)
    with pytest.raises(homewizard.HomeWizardError) as refused:
        _api(port, ca_pem, serial=SERIAL, token=first).measurement()
    assert refused.value.reason == homewizard.REASON_UNAUTHORIZED
    assert _api(port, ca_pem, serial=SERIAL, token=renewed).measurement() == MEASUREMENT
    assert _api(port, ca_pem, serial=SERIAL, token=second).measurement() == MEASUREMENT


def test_a_meter_without_room_for_another_pairing_says_so(served):
    meter, port, ca_pem = served()
    meter.pressed = True
    meter.full = True

    with pytest.raises(homewizard.HomeWizardError) as refused:
        homewizard.pair_until_pressed(_api(port, ca_pem), homewizard.pairing_name(), sleep=lambda _s: None)

    assert refused.value.reason == homewizard.REASON_NO_ROOM


def test_pairing_gives_up_when_the_window_closes(served):
    _meter, port, ca_pem = served()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(homewizard.HomeWizardError) as refused:
        homewizard.pair_until_pressed(
            _api(port, ca_pem), homewizard.pairing_name(), window_s=6.0, retry_s=2.0, sleep=sleep, clock=lambda: now[0]
        )

    assert refused.value.reason == homewizard.REASON_BUTTON_NOT_PRESSED
    assert now[0] <= 6.0


def test_the_ems_client_reads_power_through_the_api(served):
    meter, port, ca_pem = served()
    client = HomeWizardP1Client(_api(port, ca_pem, serial=SERIAL, token=TOKEN))

    assert client.get_power() == -678.0
    meter.measurement = {**MEASUREMENT, "power_w": 512.34, "timestamp": "2026-11-12T03:14:28"}
    assert client.get_power() == 512.3
    assert client.health.snapshot()["success_count"] == 2
    client.close()


def test_an_unreachable_meter_is_a_failed_read():
    api = homewizard.HomeWizardP1Api("127.0.0.1", port=1, timeout=0.5, serial=SERIAL, token=TOKEN)

    with pytest.raises(homewizard.HomeWizardError) as refused:
        api.measurement()

    assert refused.value.reason == homewizard.REASON_UNREACHABLE


def test_the_shipped_authority_is_homewizards_appliance_access_ca():
    certificate = load_pem_x509_certificate(homewizard.HOMEWIZARD_CA_PEM.encode("ascii"))

    assert certificate.subject.rfc4514_string() == "CN=Appliance Access CA,O=HomeWizard,ST=ZH,C=NL"
    assert certificate.fingerprint(hashes.SHA256()).hex() == (
        "4ec277e62033fae8bc373e3f2a976676d3f665683873f1ea8e3bdf78ac680003"
    )
    homewizard.tls_context()
