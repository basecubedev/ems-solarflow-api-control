# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Setup device plans this Admin process actually issued.

``plan_id`` is a keyed token, so a browser cannot invent one — but an
unforgeable id still only proves *some* plan was issued once. Turning a plan
into mutation authority needs the whole contract it was issued under, and all of
it lives here:

*which run decided it, and in which state*
    A plan is a decision inside one Guided Setup run. Presented in the run that
    replaced it — or in the same run after it was completed or abandoned — it is
    a decision about a session that is no longer an owner. The workflow id
    answers the first half, the workflow revision the second.

*what it was planned over*
    The candidate authority fingerprint, so Preview can recompute it from
    current discovery state. A route keeps its observation id when the hardware
    behind it is replaced, so handles alone would not notice. The draft revision
    joins it: the whole persisted Setup state the plan read, dismissals
    included. The live-config baseline deliberately stays out — it is owned by
    the exact preview record, which re-reads it under the apply transaction.

*what it decided, and what it is still waiting for*
    The planner's per-device verdict, the operations it authorized and the
    fingerprint of its outstanding confirmations. A plan that reached a
    different verdict over the same candidates is different authority, and one
    with an unanswered switch is no authority at all.

*what it authorized*
    The draft its executable operations produce. Without it a valid plan is a
    permission slip for whatever draft the browser posts next.

The two derived values — :func:`plan_fingerprint` over what the planner decided
and :func:`mutation_authority_fingerprint` over the full contract — are
recomputed on every read, so an entry whose parts do not reproduce them is
refused rather than trusted in part.

Transient on purpose, exactly like the Setup lifecycle coordinator: a restart
holds no plans, so a browser simply re-plans. Losing an entry fails closed.
The entry holds fingerprints and ids only — never a secret, never raw identity
evidence. Every stored input is itself an opaque keyed token or a digest, so the
composite digests below carry nothing their parts do not.
"""

import hashlib
import json
import threading

DEFAULT_LIMIT = 64

CONTRACT_VERSION = 1

# The authority facts a recorded plan is made of. Every one is required at
# record time: a caller that cannot name one has not established it, and a plan
# recorded without it would validate against nothing.
CONTRACT_FIELDS = (
    "workflow_id",
    "workflow_revision",
    "draft_revision",
    "candidate_authority_fingerprint",
    "confirmation_fingerprint",
    "decision_fingerprint",
    "executable_operations_fingerprint",
    "expected_draft_fingerprint",
)

# What a recorded plan can no longer be. Callers map these onto their own wire
# conflicts; the registry never names an HTTP status or an error string.
REASON_UNKNOWN = "unknown_plan"
REASON_CONTRACT = "contract_mismatch"
REASON_WORKFLOW = "workflow_moved"
REASON_CANDIDATES = "candidates_moved"
REASON_CONFIRMATION = "confirmation_pending"
REASON_DRAFT = "draft_mismatch"


def _digest(label, components):
    encoded = json.dumps(
        [label, components], ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def plan_fingerprint(entry):
    """Digest of what the planner decided, independent of where it decided it."""

    return _digest(
        "setup-device-plan-v1",
        {
            "contract_version": CONTRACT_VERSION,
            "plan_id": entry.get("plan_id"),
            "candidate_authority_fingerprint": entry.get(
                "candidate_authority_fingerprint"
            ),
            "draft_revision": entry.get("draft_revision"),
            "confirmation_fingerprint": entry.get("confirmation_fingerprint"),
            "decision_fingerprint": entry.get("decision_fingerprint"),
            "executable_operations_fingerprint": entry.get(
                "executable_operations_fingerprint"
            ),
            "expected_draft_fingerprint": entry.get("expected_draft_fingerprint"),
        },
    )


def mutation_authority_fingerprint(entry):
    """Digest of the complete contract a plan must still satisfy to mutate."""

    return _digest(
        "setup-device-plan-authority-v1",
        {
            "contract_version": CONTRACT_VERSION,
            "plan_fingerprint": entry.get("plan_fingerprint"),
            "workflow_id": entry.get("workflow_id"),
            "workflow_revision": entry.get("workflow_revision"),
        },
    )


def device_plan_conflict(
    entry,
    *,
    workflow_id,
    workflow_revision,
    candidate_authority_fingerprint,
    settled_confirmation_fingerprint,
    submitted_draft_fingerprint,
):
    """The first authority fact ``entry`` no longer satisfies, else ``None``.

    Fail-closed throughout: a missing entry, a contract that does not reproduce
    its own digests, and every mismatch are all refusals. The caller passes
    values it recomputed from current server state — never values a request
    supplied — except ``submitted_draft_fingerprint``, which is the whole point
    of the comparison and is canonicalized before it gets here.
    """

    if not isinstance(entry, dict):
        return REASON_UNKNOWN
    if entry.get("plan_fingerprint") != plan_fingerprint(entry):
        return REASON_CONTRACT
    if entry.get("mutation_authority_fingerprint") != mutation_authority_fingerprint(
        entry
    ):
        return REASON_CONTRACT
    if entry.get("workflow_id") != workflow_id or workflow_id is None:
        return REASON_WORKFLOW
    if entry.get("workflow_revision") != workflow_revision or workflow_revision is None:
        return REASON_WORKFLOW
    if entry.get("candidate_authority_fingerprint") != candidate_authority_fingerprint:
        return REASON_CANDIDATES
    if entry.get("confirmation_fingerprint") != settled_confirmation_fingerprint:
        return REASON_CONFIRMATION
    expected = entry.get("expected_draft_fingerprint")
    if expected is None or expected != submitted_draft_fingerprint:
        return REASON_DRAFT
    return None


class DevicePlanRegistry:
    """Bounded, thread-safe record of issued device plans, newest last."""

    def __init__(self, limit=DEFAULT_LIMIT):
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._plans = {}

    def record(self, plan_id, **contract):
        """Record one plan's complete authority contract.

        Every field of :data:`CONTRACT_FIELDS` is required; the two derived
        fingerprints are computed here so no caller can record a contract whose
        parts and digests disagree.
        """

        missing = [name for name in CONTRACT_FIELDS if name not in contract]
        unknown = [name for name in contract if name not in CONTRACT_FIELDS]
        if missing or unknown:
            raise TypeError(
                "device plan contract mismatch: "
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        if not isinstance(plan_id, str) or not plan_id:
            return None
        entry = dict(contract, plan_id=plan_id)
        entry["plan_fingerprint"] = plan_fingerprint(entry)
        entry["mutation_authority_fingerprint"] = mutation_authority_fingerprint(entry)
        with self._lock:
            self._plans.pop(plan_id, None)
            self._plans[plan_id] = entry
            while len(self._plans) > self._limit:
                self._plans.pop(next(iter(self._plans)))
        return dict(entry)

    def get(self, plan_id):
        if not isinstance(plan_id, str) or not plan_id:
            return None
        with self._lock:
            entry = self._plans.get(plan_id)
        return dict(entry) if entry is not None else None

    def forget_workflow(self, workflow_id):
        """Drop every plan issued inside one workflow. Returns how many."""

        if not isinstance(workflow_id, str) or not workflow_id:
            return 0
        with self._lock:
            stale = [
                plan_id
                for plan_id, entry in self._plans.items()
                if entry["workflow_id"] == workflow_id
            ]
            for plan_id in stale:
                self._plans.pop(plan_id, None)
        return len(stale)


__all__ = [
    "CONTRACT_FIELDS",
    "CONTRACT_VERSION",
    "REASON_CANDIDATES",
    "REASON_CONFIRMATION",
    "REASON_CONTRACT",
    "REASON_DRAFT",
    "REASON_UNKNOWN",
    "REASON_WORKFLOW",
    "DevicePlanRegistry",
    "device_plan_conflict",
    "mutation_authority_fingerprint",
    "plan_fingerprint",
]
