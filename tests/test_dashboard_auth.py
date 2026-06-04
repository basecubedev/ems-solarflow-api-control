import json
import os

from dashboard import auth


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


def test_session_expiry_removes_session():
    now = [100.0]
    store = auth.SessionStore(timeout_seconds=30, time_fn=lambda: now[0])
    session = store.create()

    assert store.get(session.session_id) is session

    now[0] = 131.0
    assert store.get(session.session_id) is None
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
