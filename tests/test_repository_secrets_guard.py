# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite refuses credential-store writes into the checkout's config/secrets."""

import pytest

from admin.credential_store import CredentialStore, _EncryptedFiles
from tests.conftest import REPOSITORY_SECRETS_DIR

pytestmark = [pytest.mark.unit, pytest.mark.simulation]


def test_a_store_on_the_repository_default_refuses_to_delete():
    files = _EncryptedFiles(REPOSITORY_SECRETS_DIR)

    with pytest.raises(AssertionError, match="repository"):
        files.delete_file("guard-probe-that-never-exists.json")


def test_a_store_on_the_repository_default_refuses_a_credential_save():
    store = CredentialStore(REPOSITORY_SECRETS_DIR.parent)

    with pytest.raises(AssertionError, match="repository"):
        store.forget_mqtt_broker_secret("guard-probe")


def test_a_store_on_tmp_path_is_untouched_by_the_guard(tmp_path):
    store = CredentialStore(tmp_path / "config")

    store.save_mqtt_broker_secret("home", "user", "secret")

    assert store.load_mqtt_broker_secret("home").username == "user"
