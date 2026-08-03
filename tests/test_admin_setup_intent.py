# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot Fresh Setup intent lifecycle: claim, prune, bound, fingerprint."""

import hashlib
import threading

import pytest

from admin.setup_intent import (
    SetupIntentError,
    SetupIntentStore,
    installation_state_fingerprint,
    sha256_file,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]

RUNTIME_DOWN = {"available": False}


def _seed(base, *, config=None, legacy=None, compose=None, data=False):
    if config is not None:
        target = base / "config" / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(config, encoding="utf-8")
    if legacy is not None:
        (base / "config.json").write_text(legacy, encoding="utf-8")
    if compose is not None:
        (base / "docker-compose.yml").write_text(compose, encoding="utf-8")
    if data:
        (base / "data").mkdir(parents=True, exist_ok=True)


def _fingerprint(base, *, runtime=RUNTIME_DOWN):
    return installation_state_fingerprint(
        base_dir=str(base), runtime_provider=lambda: dict(runtime)
    )


WORKFLOW = "wf-" + "0" * 20


def _store(**kwargs):
    kwargs.setdefault("state_fingerprint", lambda: "fp-stable")
    return SetupIntentStore(**kwargs)


def _issue(store, *, session_id="sess", workflow_id=WORKFLOW):
    return store.issue(session_id=session_id, workflow_id=workflow_id)


def _claim(store, intent_id, *, session_id="sess", workflow_id=WORKFLOW):
    return store.claim(intent_id, session_id=session_id, workflow_id=workflow_id)


def test_claim_consumes_an_intent_exactly_once():
    store = _store()
    record = _issue(store)
    claimed = _claim(store, record.intent_id)
    assert claimed.intent_id == record.intent_id

    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id)
    assert exc.value.reason == "setup_intent_consumed"
    assert exc.value.status == 409


def test_claim_requires_the_same_session():
    store = _store()
    record = _issue(store)
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id, session_id="other")
    assert exc.value.reason == "setup_intent_required"


def test_claim_rejects_a_missing_or_empty_intent():
    store = _store()
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, "")
    assert exc.value.reason == "setup_intent_required"


def test_claim_rejects_expired_records():
    clock = [100.0]
    store = _store(ttl_seconds=60, time_fn=lambda: clock[0])
    record = _issue(store)
    clock[0] += 61
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id)
    assert exc.value.reason == "setup_intent_expired"


def test_claim_rejects_a_changed_installation_fingerprint():
    current = ["fp-a"]
    store = _store(state_fingerprint=lambda: current[0])
    record = _issue(store)
    current[0] = "fp-b"
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id)
    assert exc.value.reason == "setup_state_changed"


# --- workflow binding --------------------------------------------------------


def test_issue_requires_a_workflow_id():
    store = _store()
    with pytest.raises(ValueError):
        store.issue(session_id="sess", workflow_id=None)


def test_an_intent_carries_the_workflow_it_was_issued_for():
    store = _store()
    record = _issue(store, workflow_id="wf-a")
    assert record.workflow_id == "wf-a"


def test_claim_rejects_a_foreign_workflow():
    store = _store()
    record = _issue(store, workflow_id="wf-a")
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id, workflow_id="wf-b")
    assert exc.value.reason == "setup_intent_workflow_mismatch"
    assert exc.value.status == 409
    # The refused claim did not consume it: its own workflow can still use it.
    assert _claim(store, record.intent_id, workflow_id="wf-a").workflow_id == "wf-a"


def test_validate_rejects_a_foreign_workflow_without_consuming():
    store = _store()
    record = _issue(store, workflow_id="wf-a")
    with pytest.raises(SetupIntentError) as exc:
        store.validate(record.intent_id, session_id="sess", workflow_id="wf-b")
    assert exc.value.reason == "setup_intent_workflow_mismatch"
    assert store.validate(
        record.intent_id, session_id="sess", workflow_id="wf-a"
    ).intent_id == record.intent_id


def test_invalidate_workflow_clears_every_session_holding_it():
    store = _store()
    first = _issue(store, session_id="a", workflow_id="wf-a")
    second = _issue(store, session_id="b", workflow_id="wf-a")
    other = _issue(store, session_id="c", workflow_id="wf-b")

    store.invalidate_workflow("wf-a")

    for record in (first, second):
        with pytest.raises(SetupIntentError) as exc:
            _claim(store, record.intent_id, session_id=record.session_id,
                   workflow_id="wf-a")
        # A retired workflow's intent reports the workflow, not a bare unknown
        # id: the browser has to rejoin the current setup, not just re-confirm.
        assert exc.value.reason == "setup_intent_workflow_mismatch"
    assert _claim(
        store, other.intent_id, session_id="c", workflow_id="wf-b"
    ).intent_id == other.intent_id


def test_issuing_for_another_session_keeps_the_first_sessions_intent():
    """The pre-existing per-session invalidation is unchanged by the binding."""

    store = _store()
    first = _issue(store, session_id="a", workflow_id="wf-a")
    _issue(store, session_id="b", workflow_id="wf-a")
    assert _claim(
        store, first.intent_id, session_id="a", workflow_id="wf-a"
    ).intent_id == first.intent_id


def test_only_one_of_two_parallel_claims_succeeds():
    # A deliberately slow fingerprint keeps both threads inside claim() at once so
    # the single-lock atomic claim is what serializes them, not luck of timing.
    gate = threading.Barrier(2)

    def slow_fingerprint():
        try:
            gate.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return "fp-stable"

    store = _store(state_fingerprint=slow_fingerprint)
    record = _issue(store)

    outcomes = []
    start = threading.Barrier(2)

    def attempt():
        start.wait()
        try:
            _claim(store, record.intent_id)
            outcomes.append("claimed")
        except SetupIntentError as exc:
            outcomes.append(exc.reason)

    workers = [threading.Thread(target=attempt) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert sorted(outcomes) == ["claimed", "setup_intent_consumed"]


def test_a_claimed_intent_cannot_be_reissued_by_a_stale_copy():
    store = _store()
    record = _issue(store)
    _claim(store, record.intent_id)
    # A second attempt on the same id reports it consumed, never still-usable.
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id)
    assert exc.value.reason == "setup_intent_consumed"


# --- installation fingerprint (content + runtime, digest only) --------------


def test_sha256_file_streams_and_matches_hashlib(tmp_path):
    target = tmp_path / "big.json"
    payload = b"secret-token-value\n" * 10000
    target.write_bytes(payload)
    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_raises_on_a_missing_path(tmp_path):
    with pytest.raises(OSError):
        sha256_file(tmp_path / "does-not-exist.json")


def test_fingerprint_changes_when_config_content_changes(tmp_path):
    _seed(tmp_path, config='{"a": 1}', compose="services: {}")
    before = _fingerprint(tmp_path)
    _seed(tmp_path, config='{"a": 2}', compose="services: {}")
    assert _fingerprint(tmp_path) != before


def test_fingerprint_changes_when_compose_content_changes(tmp_path):
    _seed(tmp_path, config='{"a": 1}', compose="services: {}")
    before = _fingerprint(tmp_path)
    _seed(tmp_path, config='{"a": 1}', compose="services: {changed: true}")
    assert _fingerprint(tmp_path) != before


def test_fingerprint_changes_when_legacy_config_content_changes(tmp_path):
    _seed(tmp_path, legacy='{"legacy": 1}')
    before = _fingerprint(tmp_path)
    _seed(tmp_path, legacy='{"legacy": 2}')
    assert _fingerprint(tmp_path) != before


def test_fingerprint_changes_when_runtime_image_changes(tmp_path):
    _seed(tmp_path, config='{"a": 1}', compose="services: {}")
    running_a = {"available": True, "container_running": True, "image_id": "sha256:a"}
    running_b = {"available": True, "container_running": True, "image_id": "sha256:b"}
    assert _fingerprint(tmp_path, runtime=running_a) != _fingerprint(
        tmp_path, runtime=running_b
    )


def test_fingerprint_changes_when_container_starts_running(tmp_path):
    _seed(tmp_path, config='{"a": 1}', compose="services: {}")
    stopped = {"available": True, "container_running": False, "image_id": "sha256:a"}
    running = {"available": True, "container_running": True, "image_id": "sha256:a"}
    assert _fingerprint(tmp_path, runtime=stopped) != _fingerprint(
        tmp_path, runtime=running
    )


def test_fingerprint_is_stable_for_unchanged_files_and_runtime(tmp_path):
    _seed(tmp_path, config='{"a": 1}', compose="services: {}", data=True)
    running = {"available": True, "container_running": True, "image_id": "sha256:a"}
    assert _fingerprint(tmp_path, runtime=running) == _fingerprint(
        tmp_path, runtime=running
    )


def test_fingerprint_is_deterministic_when_docker_is_unavailable(tmp_path):
    _seed(tmp_path, config='{"a": 1}', compose="services: {}")
    # Issue-time and validate-time both see an unavailable Docker: the fingerprint
    # must be identical so the intent is not spuriously invalidated.
    assert _fingerprint(tmp_path, runtime=RUNTIME_DOWN) == _fingerprint(
        tmp_path, runtime=RUNTIME_DOWN
    )


def test_intent_record_stores_only_a_digest_not_secret_file_content(tmp_path):
    secret = "SUPER-SECRET-API-KEY-42"
    _seed(tmp_path, config='{"apiKey": "%s"}' % secret, compose="services: {}")
    store = _store(
        state_fingerprint=lambda: _fingerprint(tmp_path),
    )
    record = _issue(store, session_id="sess")
    assert secret not in record.install_state_fingerprint
    assert len(record.install_state_fingerprint) == 64
    int(record.install_state_fingerprint, 16)  # a pure hex digest


def test_claim_fails_closed_when_a_security_file_cannot_be_read():
    mode = ["ok"]

    def fingerprint():
        if mode[0] == "boom":
            raise OSError("config exists but cannot be read")
        return "fp-stable"

    store = _store(state_fingerprint=fingerprint)
    record = _issue(store, session_id="sess")
    mode[0] = "boom"
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id, session_id="sess")
    assert exc.value.reason == "setup_state_changed"


# --- pruning and bounded storage --------------------------------------------


def test_expired_intents_are_removed_on_issue():
    clock = [0.0]
    store = _store(ttl_seconds=10, time_fn=lambda: clock[0])
    stale = _issue(store, session_id="old")
    clock[0] += 11
    _issue(store, session_id="new")
    # The expired record is gone, not merely unusable.
    assert stale.intent_id not in store._records


def test_store_never_exceeds_its_size_limit():
    store = _store(max_records=4)
    issued = [_issue(store, session_id=f"sess-{i}").intent_id for i in range(20)]
    assert len(store._records) <= 4
    # The most recent issue always survives the eviction of older records.
    assert issued[-1] in store._records


def test_consumed_tombstone_expires_and_stops_reporting_consumed():
    clock = [0.0]
    store = _store(consumed_ttl_seconds=30, time_fn=lambda: clock[0])
    record = _issue(store, session_id="sess")
    _claim(store, record.intent_id, session_id="sess")
    clock[0] += 31
    _issue(store, session_id="other")  # prunes the expired tombstone
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, record.intent_id, session_id="sess")
    # Once the short-lived tombstone expires the id is simply unknown again.
    assert exc.value.reason == "setup_intent_required"


def test_consumed_tombstones_are_bounded_by_the_limit():
    store = _store(max_records=4)
    for i in range(10):
        record = _issue(store, session_id=f"sess-{i}")
        _claim(store, record.intent_id, session_id=f"sess-{i}")
    assert len(store._tombstones) <= 4


def test_logout_removes_open_intents_and_tombstones_for_the_session():
    store = _store()
    open_intent = _issue(store, session_id="sess")
    claimed = _issue(store, session_id="sess")
    _claim(store, claimed.intent_id, session_id="sess")
    store.invalidate_session("sess")
    assert open_intent.intent_id not in store._records
    assert claimed.intent_id not in store._tombstones
    # The cleared tombstone means a stale copy is now merely unknown, not consumed.
    with pytest.raises(SetupIntentError) as exc:
        _claim(store, claimed.intent_id, session_id="sess")
    assert exc.value.reason == "setup_intent_required"
