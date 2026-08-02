# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser holds no physical-identity policy of its own.

Physical equivalence — "are these two observations the same hardware?" — is
answered by ``ems/device_identity.py``, published as keyed tokens by
``admin/observation_identity.py`` and turned into keep/replace/add/block by
``admin/connection_planner.py``. The browser renders that answer.

These contracts are deliberately *behavioral*: they feed the surviving admin.js
helpers payloads that carry only raw hardware fields (serial, device id, host,
broker) and assert that nothing physical is concluded from them. A helper that
disappears passes trivially; a helper that stays but keeps a raw-field fallback
fails. Renaming the helpers therefore cannot make the suite green.

The structural half asserts the same thing from the other side: an identity
decision helper's own source must not read a hardware field at all.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)

# Every helper that has ever decided physical equivalence, connection
# replacement, priority selection or conflict state in the browser. Absent is
# the preferred outcome; present-but-token-only is allowed.
IDENTITY_DECISION_HELPERS = (
    "physicalInverterIdentity",
    "inverterVisibleSerial",
    "inverterIdentitySet",
    "inverterIdentitySetOf",
    "inverterIdentityTokens",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "inverterIdentityRefMatches",
    "inverterHasIdentity",
    "mconfigFindInverterMatch",
    "mconfigSameMqttConnection",
    "mconfigDraftHasProposal",
    "mconfigMqttProposalState",
    "configuredInverterConnection",
    "concreteMqttConnectionKey",
    "sameMqttConnectionScope",
    "serialSelectedOverMqtt",
    "dismissalStorageKey",
    "dismissalKeysForInverter",
    "resolveSelectedDeviceSource",
    "reconcileTransportSelection",
    # The surviving projection: these read issued ids and nothing else.
    "issuedPhysicalIdentity",
    "issuedIdentityTokens",
    "issuedConnectionId",
    "sameIssuedDevice",
    "inverterCandidateConnectionState",
)

# Raw hardware evidence. None of it may appear in an identity decision helper:
# a redacted view renders a serial as a placeholder several unrelated devices
# share, and an MQTT route id is an account-scoped write target, not identity.
RAW_IDENTITY_FIELDS = (
    "serial_number",
    "product_key",
    "broker_ref",
    "hardware_model",
)

RAW_IDENTITY_PROPERTIES = ("sn", "ip", "host", "hostname", "device_id", "broker")

REAL_SERIAL = "EOD1AAA111"
OTHER_SERIAL = "EOD1BBB222"


def _read():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    """The full source of ``name``, or None when the helper no longer exists."""

    marker = "function " + name + "("
    idx = js.find(marker)
    if idx < 0:
        return None
    body = js[idx:]
    depth = 0
    for position, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[: position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _present(js, names):
    return {name: _extract_fn(js, name) for name in names if _extract_fn(js, name)}


def _constants(js):
    """The module-level token patterns the extracted helpers close over."""

    return "\n".join(
        line
        for line in js.split("\n")
        if line.startswith("const ") and "PATTERN = /" in line
    )


def _run(js, names, script):
    """Evaluate ``script`` with the named helpers, skipping absent ones."""

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the frontend identity authority contract")
    sources = [_extract_fn(js, name) for name in names]
    helpers = "\n".join(source for source in sources if source)
    stubs = "\n".join(
        f"function {name}() {{ return undefined; }}"
        for name, source in zip(names, sources)
        if not source
    )
    result = subprocess.run(
        [node, "-e", _constants(js) + "\n" + helpers + "\n" + stubs + "\n" + script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _has(js, name):
    return _extract_fn(js, name) is not None


# --- structural: an identity decision never reads a hardware field -----------
def test_no_identity_decision_helper_reads_raw_hardware_evidence():
    js = _read()
    offenders = {}
    for name, source in _present(js, IDENTITY_DECISION_HELPERS).items():
        hits = [field for field in RAW_IDENTITY_FIELDS if field in source]
        hits += [
            prop
            for prop in RAW_IDENTITY_PROPERTIES
            if re.search(r"\.\s*" + prop + r"\b", source)
        ]
        if hits:
            offenders[name] = sorted(set(hits))

    assert offenders == {}, (
        "these admin.js helpers still derive physical identity from raw hardware "
        f"fields: {offenders}. Physical equivalence belongs to "
        "ems/device_identity.py, published through admin/observation_identity.py."
    )


def test_browser_cannot_mint_an_authoritative_identity_token():
    """Only a well-formed server-issued token is accepted, never constructed."""

    js = _read()
    if not _has(js, "issuedPhysicalIdentity"):
        pytest.skip("issuedPhysicalIdentity has been removed")
    payload = _run(
        js,
        ("issuedPhysicalIdentity",),
        """
console.log(JSON.stringify([
  issuedPhysicalIdentity({ serial_number: "%s" }),
  issuedPhysicalIdentity({ sn: "%s" }),
  issuedPhysicalIdentity({ physical_identity_token: "opaque:v1:aaa" }),
  issuedPhysicalIdentity({ physical_identity_token: "not-a-token" }),
  issuedPhysicalIdentity({ physical_device_id: "opaque:v1:bbb" }),
]));
"""
        % (REAL_SERIAL, REAL_SERIAL),
    )

    assert payload == ["", "", "opaque:v1:aaa", "", "opaque:v1:bbb"]


# --- behavioral: raw fields never establish physical equivalence -------------
def test_a_shared_raw_serial_is_not_browser_side_physical_identity():
    js = _read()
    if not _has(js, "physicalInverterIdentity"):
        pytest.skip("physicalInverterIdentity has been removed")
    payload = _run(
        js,
        (
            "normalizeSerial",
            "usableSerialValue",
            "issuedPhysicalIdentity",
            "physicalInverterIdentity",
        ),
        """
console.log(JSON.stringify([
  physicalInverterIdentity({ serial_number: "%s" }),
  physicalInverterIdentity({ sn: "%s" }),
]));
"""
        % (REAL_SERIAL, REAL_SERIAL),
    )

    assert payload == ["", ""], (
        "physicalInverterIdentity still returns a raw serial as identity; a "
        "device without a server-issued token has no browser-side identity"
    )


def test_two_observations_never_match_on_raw_evidence_alone():
    js = _read()
    if not _has(js, "inverterIdentitiesMatch"):
        pytest.skip("inverterIdentitiesMatch has been removed")
    payload = _run(
        js,
        (
            "issuedPhysicalIdentity",
            "issuedIdentityTokens",
            "isConfirmedIdentity",
            "inverterIdentityConflict",
            "inverterIdentitiesMatch",
        ),
        """
const bySerial = inverterIdentitiesMatch(
  { serial_number: "%s" }, { serial_number: "%s" });
const byHost = inverterIdentitiesMatch(
  { ip: "10.0.0.7" }, { ip: "10.0.0.7" });
const byRoute = inverterIdentitiesMatch(
  { mqtt: { device_id: "ROUTE_1" } }, { mqtt: { device_id: "ROUTE_1" } });
const byToken = inverterIdentitiesMatch(
  { physical_device_id: "opaque:v1:same", identity_status: "confirmed" },
  { physical_device_id: "opaque:v1:same", identity_status: "confirmed" });
console.log(JSON.stringify({ bySerial, byHost, byRoute, byToken }));
"""
        % (REAL_SERIAL, REAL_SERIAL),
    )

    assert payload["bySerial"] is False, "a raw serial still merges two observations"
    assert payload["byHost"] is False
    assert payload["byRoute"] is False
    assert payload["byToken"] is True, "issued tokens must still compare equal"


def test_identity_conflict_is_not_inferred_from_visible_serials():
    js = _read()
    if not _has(js, "inverterIdentityConflict"):
        pytest.skip("inverterIdentityConflict has been removed")
    payload = _run(
        js,
        (
            "issuedPhysicalIdentity",
            "issuedIdentityTokens",
            "isConfirmedIdentity",
            "inverterIdentityConflict",
        ),
        """
console.log(JSON.stringify(inverterIdentityConflict(
  { serial_number: "%s", identity_status: "confirmed",
    physical_device_id: "opaque:v1:route" },
  { serial_number: "%s", identity_status: "confirmed",
    physical_device_id: "opaque:v1:route" }
)));
"""
        % (REAL_SERIAL, OTHER_SERIAL),
    )

    assert payload is False, (
        "the browser still decides identity conflict from two visible serials; "
        "that verdict is admin/connection_planner.py's"
    )


def test_a_bare_serial_is_never_a_persisted_dismissal_key():
    js = _read()
    if not _has(js, "dismissalStorageKey"):
        pytest.skip("dismissalStorageKey has been removed")
    payload = _run(
        js,
        ("normalizeSerial", "usableSerialValue", "dismissalStorageKey"),
        """
console.log(JSON.stringify([
  dismissalStorageKey("%s"),
  dismissalStorageKey("serial:%s"),
  dismissalStorageKey("opaque:v1:token"),
]));
"""
        % (REAL_SERIAL, REAL_SERIAL.lower()),
    )

    assert payload[0] == "", "a bare serial is still upgraded to an identity key"
    assert payload[1] == "", "a serial sentinel is still accepted as an identity key"
    assert payload[2] == "opaque:v1:token"


def test_no_javascript_reconciler_decides_transport_priority():
    """Batch selection across Local API / Local MQTT / Zendure MQTT is backend work."""

    js = _read()
    assert not _has(js, "reconcileTransportSelection"), (
        "reconcileTransportSelection() is still a complete second transport "
        "planner in JavaScript; Setup must call the backend batch planner"
    )
    assert not _has(js, "resolveSelectedDeviceSource"), (
        "resolveSelectedDeviceSource() still owns source priority in the browser"
    )
    assert not _has(js, "serialSelectedOverMqtt"), (
        "serialSelectedOverMqtt() still compares raw serials across transports"
    )


def test_transport_switch_cannot_mutate_before_an_authoritative_plan():
    """``switchInverterTransport`` must await a backend plan, not search locally."""

    js = _read()
    source = _extract_fn(js, "switchInverterTransport")
    if source is None:
        pytest.skip("switchInverterTransport has been removed")
    mutations = ("configDraftItems", "zendureMqttPreviewProposals", "saveConfigDraft")
    plan_boundary = "await " in source
    assert plan_boundary, (
        "switchInverterTransport() still selects a candidate and mutates the "
        "draft without ever awaiting a backend ConnectionPlan"
    )
    first_mutation = min(
        (source.index(name) for name in mutations if name in source),
        default=len(source),
    )
    assert source.index("await ") < first_mutation, (
        "switchInverterTransport() mutates Setup state before the backend plan "
        "arrives; an unresolved or blocked plan must leave the draft untouched"
    )
