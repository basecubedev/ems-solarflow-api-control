# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSH public-key parsing, fingerprints and atomic ``authorized_keys`` writes.

The appliance must never accept a private key, never keep a partially written
``authorized_keys`` and never leave the file world-readable. Fingerprints are
compared against the values OpenSSH itself prints for these throwaway keys.
"""

import os
import stat

import pytest

from appliance.sshkeys import (
    AUTHORIZED_KEYS_MODE,
    SSH_DIR_MODE,
    AuthorizedKeysStore,
    fingerprint_of,
    parse_authorized_keys,
    render_authorized_keys,
    validate_public_key,
)
from appliance.validation import ValidationError

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ED25519 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIl8UiJHP3y4t+H+uVmVWcN/BNvqHg2f6urH8+puRXdf "
    "appliance-test@example.invalid"
)
ED25519_FINGERPRINT = "SHA256:49CipW8FlH8lOK6o3jsEAdmPpX8qEhdzW2S/R0YYQaM"

RSA_BLOB = (
    "AAAAB3NzaC1yc2EAAAADAQABAAABAQDAs4upQU7B3RVlagYe6AZplwEG4985zHZ4bTp8IBjz"
    "R0BejAH6f1R1GXyfWfOPnE+BxVJ9tN+sXu1okeNlW7aXH2sWcbYRx0OGdqIP6B6woXsv9cCZ"
    "1bQoMA+zihSYoF2UUmouc0Pe3CozNBocvN7KS9GgDQa9JsUlRQEgE1zUN8Zxp3qFzk4D5Phb"
    "5GMWuCFTIqKp4KC8SLlR5sMcwDtbdzcSBSqegFoEfLde9Cx+mSoxhJNUfEO72bik1RkQz6QE"
    "k71DYxevMT5k02jeQYBuSymwdBB6ozU7bmrb3MsAhJMbwhvmb2SL9ac/9DZ2ux0539L+g9dg"
    "WG/sdG5PaOoL"
)
RSA = f"ssh-rsa {RSA_BLOB} rsa-test@example.invalid"
RSA_FINGERPRINT = "SHA256:ZeCbcUSHvkpBbqk7uaguZocrPHEv1N4z6DkEGBEa6ZE"

PRIVATE_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
-----END OPENSSH PRIVATE KEY-----"""


# --- parsing ---------------------------------------------------------------


def test_valid_ed25519_key_is_parsed():
    key = validate_public_key(ED25519)
    assert key.key_type == "ssh-ed25519"
    assert key.comment == "appliance-test@example.invalid"
    assert key.fingerprint == ED25519_FINGERPRINT


def test_fingerprint_matches_openssh_format():
    key = validate_public_key(ED25519)
    assert fingerprint_of(key.blob) == ED25519_FINGERPRINT
    assert key.fingerprint.startswith("SHA256:")
    assert not key.fingerprint.endswith("=")


def test_key_without_a_comment_is_accepted():
    key = validate_public_key(ED25519.rsplit(" ", 1)[0])
    assert key.comment == ""
    assert key.fingerprint == ED25519_FINGERPRINT


def test_surrounding_whitespace_is_tolerated():
    assert validate_public_key(f"  {ED25519}  \t").fingerprint == ED25519_FINGERPRINT


def test_private_key_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        validate_public_key(PRIVATE_KEY)
    assert excinfo.value.code == "private_key_rejected"


def test_empty_key_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        validate_public_key("   ")
    assert excinfo.value.code == "empty_public_key"


@pytest.mark.parametrize(
    "value",
    [
        "ssh-ed25519",
        "ssh-ed25519 not-base64!!",
        "ssh-ed25519 AAAA",
        "just-some-text",
    ],
)
def test_malformed_keys_are_refused(value):
    with pytest.raises(ValidationError) as excinfo:
        validate_public_key(value)
    assert excinfo.value.code == "invalid_public_key"


def test_unsupported_key_type_is_refused():
    body = ED25519.split()[1]
    with pytest.raises(ValidationError) as excinfo:
        validate_public_key(f"ssh-dss {body}")
    assert excinfo.value.code == "unsupported_key_type"


def test_declared_type_must_match_the_key_body():
    body = ED25519.split()[1]
    with pytest.raises(ValidationError) as excinfo:
        validate_public_key(f"ssh-rsa {body}")
    assert excinfo.value.code == "invalid_public_key"


def test_oversized_key_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        validate_public_key("ssh-ed25519 " + "A" * 9000)
    assert excinfo.value.code == "public_key_too_large"


def test_multiline_input_is_refused():
    with pytest.raises(ValidationError):
        validate_public_key(ED25519 + "\n" + ED25519)


def test_rsa_key_is_accepted_as_a_compatible_type():
    key = validate_public_key(RSA)
    assert key.key_type == "ssh-rsa"
    assert key.fingerprint == RSA_FINGERPRINT


# --- authorized_keys file --------------------------------------------------


def store_for(tmp_path):
    home = tmp_path / "home" / "ems-backup"
    home.mkdir(parents=True)
    return AuthorizedKeysStore(home)


def test_add_creates_the_file_with_strict_permissions(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)

    assert stat.S_IMODE(os.stat(store.path).st_mode) == AUTHORIZED_KEYS_MODE
    assert stat.S_IMODE(os.stat(store.ssh_dir).st_mode) == SSH_DIR_MODE
    assert store.path.read_text(encoding="utf-8").endswith("\n")


def test_listing_returns_parsed_keys(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)
    keys = store.list()
    assert [key.fingerprint for key in keys] == [ED25519_FINGERPRINT]
    assert keys[0].comment == "appliance-test@example.invalid"


def test_duplicate_key_is_refused(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)
    with pytest.raises(ValidationError) as excinfo:
        store.add(ED25519)
    assert excinfo.value.code == "duplicate_public_key"
    assert len(store.list()) == 1


def test_remove_by_fingerprint(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)
    store.add(RSA)
    assert store.remove(ED25519_FINGERPRINT) == 1
    assert [key.key_type for key in store.list()] == ["ssh-rsa"]


def test_removing_an_unknown_fingerprint_is_refused(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)
    with pytest.raises(ValidationError) as excinfo:
        store.remove("SHA256:" + "A" * 43)
    assert excinfo.value.code == "unknown_public_key"
    assert len(store.list()) == 1


def test_revoke_all_empties_the_file_but_keeps_it(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)
    store.add(RSA)
    assert store.revoke_all() == 2
    assert store.path.is_file()
    assert store.list() == []


def test_write_is_atomic_and_leaves_no_temporary_file(tmp_path):
    store = store_for(tmp_path)
    store.add(ED25519)
    store.add(RSA)
    leftovers = [entry.name for entry in store.ssh_dir.iterdir() if entry.name != "authorized_keys"]
    assert leftovers == []


def test_unparsable_lines_are_skipped_not_propagated(tmp_path):
    store = store_for(tmp_path)
    store.ssh_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text(f"# comment\nnonsense line\n{ED25519}\n", encoding="utf-8")
    assert [key.fingerprint for key in store.list()] == [ED25519_FINGERPRINT]


def test_render_round_trips(tmp_path):
    keys = parse_authorized_keys(f"{ED25519}\n{RSA}\n")
    assert len(parse_authorized_keys(render_authorized_keys(keys))) == 2
