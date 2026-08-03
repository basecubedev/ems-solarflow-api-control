# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os

import pytest

from dashboard import auth

pytestmark = [
    pytest.mark.integration,
]


def test_password_hash_generation_and_verification(tmp_path):
    record = auth.hash_password("correct horse battery staple")

    assert record["algorithm"] == "pbkdf2-sha256"
    assert record["iterations"] == 600000
    assert "correct horse battery staple" not in json.dumps(record)
    assert auth.verify_password_record("correct horse battery staple", record)
    assert not auth.verify_password_record("wrong", record)


def test_auth_file_missing_is_not_configured(tmp_path):
    path = tmp_path / "dashboard-auth.json"

    assert auth.load_auth_file(path) is None
    assert not auth.auth_configured(path)
    assert not auth.verify_password_file(path, "secret")


def test_password_file_writes_hash_only_and_restricts_permissions(tmp_path):
    path = tmp_path / "config" / "dashboard-auth.json"

    auth.write_password_file(path, "secret-password")

    raw = path.read_text()
    assert "secret-password" not in raw
    assert auth.auth_configured(path)
    assert auth.verify_password_file(path, "secret-password")
    assert not auth.verify_password_file(path, "other")

    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_create_password_file_if_missing_creates_parent_and_restricts_permissions(tmp_path):
    path = tmp_path / "config" / "dashboard-auth.json"

    auth.create_password_file_if_missing(path, "secret-password")

    assert auth.verify_password_file(path, "secret-password")
    assert "secret-password" not in path.read_text()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_create_password_file_if_missing_never_overwrites(tmp_path):
    import pytest

    path = tmp_path / "config" / "dashboard-auth.json"
    auth.write_password_file(path, "first-password")

    with pytest.raises(FileExistsError):
        auth.create_password_file_if_missing(path, "second-password")

    # The original password is untouched by the refused create.
    assert auth.verify_password_file(path, "first-password")
    assert not auth.verify_password_file(path, "second-password")


def test_session_expiry_removes_session():
    now = [100.0]
    store = auth.SessionStore(timeout_seconds=30, time_fn=lambda: now[0])
    session = store.create()

    assert store.get(session.session_id) is session

    now[0] = 131.0
    assert store.get(session.session_id) is None
    assert session.session_id not in store.sessions


def test_session_touch_slides_idle_timeout():
    now = [100.0]
    store = auth.SessionStore(
        timeout_seconds=30,
        absolute_max_seconds=1000,
        time_fn=lambda: now[0],
    )
    session = store.create()
    assert session.expires_at == 130.0

    now[0] = 120.0
    store.touch(session.session_id)
    assert session.expires_at == 150.0

    # Without the touch the session would have expired at 130.
    now[0] = 145.0
    assert store.get(session.session_id) is session

    now[0] = 151.0
    assert store.get(session.session_id) is None


def test_session_touch_cannot_exceed_absolute_max():
    now = [0.0]
    store = auth.SessionStore(
        timeout_seconds=100,
        absolute_max_seconds=150,
        time_fn=lambda: now[0],
    )
    session = store.create()
    assert session.expires_at == 100.0  # min(now+100, created+150)

    now[0] = 90.0
    store.touch(session.session_id)
    assert session.expires_at == 150.0  # capped by absolute max

    now[0] = 140.0
    store.touch(session.session_id)
    assert session.expires_at == 150.0  # still capped, sliding stops

    now[0] = 151.0
    assert store.get(session.session_id) is None  # re-login forced past cap


def test_session_get_is_read_only_and_does_not_slide():
    now = [0.0]
    store = auth.SessionStore(
        timeout_seconds=50,
        absolute_max_seconds=1000,
        time_fn=lambda: now[0],
    )
    session = store.create()
    assert session.expires_at == 50.0

    now[0] = 40.0
    store.get(session.session_id)
    assert session.expires_at == 50.0  # get must not extend the session

    now[0] = 51.0
    assert store.get(session.session_id) is None


def test_disabled_timeouts_never_expire():
    now = [0.0]
    store = auth.SessionStore(
        timeout_seconds=0,
        absolute_max_seconds=0,
        time_fn=lambda: now[0],
    )
    session = store.create()
    assert session.expires_at is None
    assert store.timeout_seconds is None
    assert store.absolute_max_seconds is None

    now[0] = 10**9
    assert store.get(session.session_id) is session
    # touch keeps it non-expiring
    store.touch(session.session_id)
    assert session.expires_at is None


def test_disabled_idle_still_bounded_by_absolute_cap():
    now = [0.0]
    store = auth.SessionStore(
        timeout_seconds=0,
        absolute_max_seconds=100,
        time_fn=lambda: now[0],
    )
    session = store.create()
    assert session.expires_at == 100.0

    now[0] = 50.0
    store.touch(session.session_id)
    assert session.expires_at == 100.0  # idle disabled -> only the cap applies

    now[0] = 101.0
    assert store.get(session.session_id) is None


def test_negative_timeouts_are_treated_as_disabled():
    store = auth.SessionStore(timeout_seconds=-5, absolute_max_seconds=-1)
    assert store.timeout_seconds is None
    assert store.absolute_max_seconds is None


def test_touch_unknown_or_expired_session_returns_none():
    now = [0.0]
    store = auth.SessionStore(timeout_seconds=10, time_fn=lambda: now[0])
    assert store.touch("does-not-exist") is None

    session = store.create()
    now[0] = 11.0
    assert store.touch(session.session_id) is None
    assert session.session_id not in store.sessions


def test_login_rate_limiter_prunes_stale_entries_and_caps_size():
    now = [100.0]
    limiter = auth.LoginRateLimiter(
        max_failures=2,
        window_seconds=10,
        max_entries=2,
        time_fn=lambda: now[0],
    )

    limiter.record_failure("old")
    now[0] = 121.0
    limiter.prune()
    assert "old" not in limiter.failures

    limiter.record_failure("a")
    now[0] = 122.0
    limiter.record_failure("b")
    now[0] = 123.0
    limiter.record_failure("c")
    assert len(limiter.failures) == 2
    assert "a" not in limiter.failures
