# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the appliance was proven to do at runtime, as evidence rather than prose.

A release gate can prove that an image is structurally what it claims. It
cannot prove that an operator can log in over SFTP, that removing the package
leaves a foreign account alone, or that the runtime reconstructs its containers
from a seed with no registry in reach. Those are answered by running the thing,
and the answers used to live in a paragraph a human wrote afterwards.

So each one is a record: a result, the prerequisite it is waiting on if it did
not run, the digest of the evidence it was derived from, and the environment
that produced it. ``not_run`` is a first-class result — the whole point is that
"nobody ran it" and "it works" stop looking alike — and a required gate that did
not run is never a pass.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"
RESULTS = (PASS, FAIL, NOT_RUN)

UNREADABLE = "runtime_gates_unreadable"
UNSUPPORTED = "runtime_gates_unsupported"

# The runtime behaviour a release is responsible for, and whether a release may
# be cut without it. The ARM64 generic guest is a cross-check on a machine that
# is not the target: a Raspberry Pi boots its own firmware and its own kernel,
# so a generic UEFI guest can raise confidence and can never be the proof.
REQUIRED_GATES = (
    "sftp",
    "package_lifecycle",
    "docker_reconstruction",
)
OPTIONAL_GATES = ("arm64_guest",)
GATES = REQUIRED_GATES + OPTIONAL_GATES

# What each gate establishes, and where it stops. A bare "pass" is read as a
# claim about the whole subject; these travel with the evidence so a release
# reader sees the same limits the gate was written with.
GATE_SCOPES = {
    "sftp": (
        "the chrooted SFTP policy confines the backup account to the export "
        "root, against a real sshd"
    ),
    "package_lifecycle": (
        "install, upgrade, remove and purge of the built package against real "
        "dpkg and systemd in a container"
    ),
    "docker_reconstruction": (
        "Docker save, load and digest-pinned pull mechanics against contract "
        "stand-in images built by this test. It does not exercise the project's "
        "own Admin, EMS or InfluxDB images, which are arm64 and run on hardware "
        "this gate does not have"
    ),
    "arm64_guest": (
        "the package installs and its services start on a booted aarch64 guest "
        "under emulation. Not a Raspberry Pi: no Pi firmware"
    ),
}


class RuntimeGateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def file_sha256(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True)
class GateRecord:
    name: str = ""
    result: str = NOT_RUN
    reason: str = ""
    evidence_sha256: str = ""
    environment: str = ""
    scope: str = ""

    @property
    def required(self):
        return self.name in REQUIRED_GATES

    def to_dict(self):
        return {
            "result": self.result,
            "reason": self.reason,
            "evidence_sha256": self.evidence_sha256,
            "environment": self.environment,
            "scope": self.scope or GATE_SCOPES.get(self.name, ""),
        }


@dataclass(frozen=True)
class RuntimeGates:
    gates: dict = field(default_factory=dict)
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "gates": {name: self.gates[name].to_dict() for name in sorted(self.gates)},
            "required": list(REQUIRED_GATES),
            "optional": list(OPTIONAL_GATES),
            "result": PASS if self.required_pass else FAIL,
        }

    @property
    def required_pass(self):
        return all(
            self.gates.get(name) is not None and self.gates[name].result == PASS
            for name in REQUIRED_GATES
        )

    @property
    def unmet(self):
        return tuple(
            name
            for name in REQUIRED_GATES
            if self.gates.get(name) is None or self.gates[name].result != PASS
        )

    def summary(self):
        return {name: record.result for name, record in sorted(self.gates.items())}


def record(name, result, *, reason="", evidence=None, environment=""):
    """One gate's answer, with the evidence it was read out of.

    ``not_run`` carries the exact prerequisite rather than an apology: the next
    run has to be able to act on it without re-deriving why it was skipped.
    """

    if name not in GATES:
        raise RuntimeGateError(UNSUPPORTED, f"{name!r} is not a runtime gate")
    if result not in RESULTS:
        raise RuntimeGateError(UNSUPPORTED, f"{result!r} is not one of {', '.join(RESULTS)}")
    if result == NOT_RUN and not reason:
        raise RuntimeGateError(
            UNREADABLE, f"{name}: a gate that did not run has to name its prerequisite"
        )
    digest = ""
    if evidence:
        target = Path(evidence)
        if not target.is_file():
            raise RuntimeGateError(UNREADABLE, f"{name}: the evidence {target} is not a file")
        digest = file_sha256(target)
    return GateRecord(
        name=name,
        result=result,
        reason=reason,
        evidence_sha256=digest,
        environment=environment,
        scope=GATE_SCOPES.get(name, ""),
    )


def build(records, *, created_at=""):
    return RuntimeGates(
        gates={item.name: item for item in records},
        created_at=created_at,
    )


def parse(payload):
    if not isinstance(payload, dict):
        raise RuntimeGateError(UNREADABLE, "the runtime gate evidence is not an object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RuntimeGateError(
            UNSUPPORTED, f"runtime gate schema {version!r} is not schema {SCHEMA_VERSION}"
        )
    raw = payload.get("gates")
    if not isinstance(raw, dict):
        raise RuntimeGateError(UNREADABLE, "the runtime gate evidence names no gate")
    gates = {}
    for name, entry in raw.items():
        if name not in GATES or not isinstance(entry, dict):
            raise RuntimeGateError(UNSUPPORTED, f"{name!r} is not a runtime gate")
        result = str(entry.get("result") or "")
        if result not in RESULTS:
            raise RuntimeGateError(UNSUPPORTED, f"{name}: {result!r} is not a runtime gate result")
        gates[name] = GateRecord(
            name=name,
            result=result,
            reason=str(entry.get("reason") or ""),
            evidence_sha256=str(entry.get("evidence_sha256") or ""),
            environment=str(entry.get("environment") or ""),
        )
    return RuntimeGates(gates=gates, created_at=str(payload.get("created_at") or ""))


def read(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeGateError(UNREADABLE, f"{path} could not be read: {exc}")
    except ValueError:
        raise RuntimeGateError(UNREADABLE, f"{path} is not valid JSON")
    return parse(payload)


def write(path, gates):
    target = Path(path)
    target.write_text(json.dumps(gates.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
