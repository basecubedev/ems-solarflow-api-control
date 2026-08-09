# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSH service state and public-key management.

Only accounts the host configuration lists may be touched. The appliance never
enables password authentication and never handles a private key: enabling SSH
enables the service, nothing else.
"""

import time
from dataclasses import dataclass

from appliance.operations import STATE_FAILED_TERMINAL, STATE_SUCCEEDED
from appliance.ssh_policy import parse_sshd_config
from appliance.sshkeys import AuthorizedKeysStore, validate_public_key
from appliance.systemd import UNIT_SSH
from appliance.validation import ValidationError

TYPE_SSH_SERVICE = "ssh.service"
TYPE_SSH_KEY_ADD = "ssh.key_add"
TYPE_SSH_KEY_REMOVE = "ssh.key_remove"
TYPE_SSH_REVOKE_ALL = "ssh.revoke_all"

RECOMMENDED_DEFAULTS = {
    "permitrootlogin": "no",
    "passwordauthentication": "no",
    "pubkeyauthentication": "yes",
}


class SshServiceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Account:
    name: str
    exists: bool
    home: str = ""
    uid: int = None
    gid: int = None
    shell: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "exists": self.exists,
            "home": self.home,
            "shell": self.shell,
        }


def parse_passwd_entry(name, text):
    for line in (text or "").splitlines():
        fields = line.strip().split(":")
        if len(fields) >= 7 and fields[0] == name:
            return Account(
                name=name,
                exists=True,
                home=fields[5],
                uid=int(fields[2]) if fields[2].isdigit() else None,
                gid=int(fields[3]) if fields[3].isdigit() else None,
                shell=fields[6],
            )
    return Account(name=name, exists=False)


class SshService:
    def __init__(
        self,
        *,
        runner,
        systemd,
        config,
        operations,
        paths=None,
        time_fn=None,
        operation_log=None,
    ):
        self.runner = runner
        self.systemd = systemd
        self.config = config
        self.operations = operations
        self.paths = paths
        self._time = time_fn or time.time
        self._operation_log = operation_log

    def _attribute(self, account, blobs, *, managed):
        """Record which keys this package wrote, so purge removes only those."""

        if self.paths is None or account != self.config.backup_user:
            return False
        from appliance import backup_ownership

        if managed:
            return backup_ownership.record_managed_keys(self.paths, blobs)
        return backup_ownership.forget_managed_keys(self.paths, blobs)

    # --- read-only -------------------------------------------------------

    def account(self, name):
        if name not in tuple(self.config.ssh_key_accounts):
            raise SshServiceError("account_not_allowed", f"{name} is not an appliance account")
        if not self.runner.available("getent"):
            return Account(name=name, exists=False)
        result = self.runner.run("getent", ["passwd", name], timeout=15)
        return parse_passwd_entry(name, result.stdout if result.ok else "")

    def keystore(self, name):
        account = self.account(name)
        if not account.exists or not account.home:
            raise SshServiceError("account_missing", f"the host account {name} does not exist")
        return AuthorizedKeysStore(account.home, owner_uid=account.uid, owner_gid=account.gid)

    def effective_config(self, user=None):
        """The configuration sshd would apply, optionally for one account.

        Without a connection specification ``sshd -T`` skips every ``Match``
        block, so the backup account's chroot and forced command are only
        visible when the user is named.
        """

        if not self.runner.available("sshd"):
            return {}
        arguments = ["-T"]
        if user:
            arguments += ["-C", f"user={user},host=localhost,addr=127.0.0.1"]
        result = self.runner.run("sshd", arguments, timeout=20)
        if not result.ok and user:
            result = self.runner.run("sshd", ["-T"], timeout=20)
        return parse_sshd_config(result.stdout if result.ok else "")

    def status(self):
        unit = self.systemd.unit_state(UNIT_SSH)
        effective = self.effective_config()
        accounts = []
        for name in self.config.ssh_key_accounts:
            account = self.account(name)
            keys = []
            if account.exists and account.home:
                keys = [key.to_dict() for key in AuthorizedKeysStore(account.home).list()]
            entry = account.to_dict()
            entry["keys"] = keys
            entry["key_count"] = len(keys)
            accounts.append(entry)

        hardening = {}
        for option, expected in RECOMMENDED_DEFAULTS.items():
            actual = effective.get(option, "")
            hardening[option] = {
                "value": actual,
                "recommended": expected,
                "compliant": actual.lower() == expected if actual else None,
            }

        return {
            "service": unit,
            "enabled": unit["running"],
            "accounts": accounts,
            "hardening": hardening,
            "password_authentication": effective.get("passwordauthentication", "unknown"),
        }

    # --- planning --------------------------------------------------------

    def plan_service(self, operation, enabled):
        unit = self.systemd.unit_state(UNIT_SSH)
        values = {"enabled": bool(enabled)}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_SSH_SERVICE,
            "enabled": bool(enabled),
            "current": unit,
            "note": "Password authentication stays disabled; only key-based logins are enabled.",
        }

    def plan_key_add(self, operation, *, account, public_key):
        store = self.keystore(account)
        try:
            key = validate_public_key(public_key)
        except ValidationError as exc:
            raise SshServiceError(exc.code, exc.message)
        if any(item.fingerprint == key.fingerprint for item in store.list()):
            raise SshServiceError("duplicate_public_key", "this key is already authorized")

        # The public key is stored with the operation so the execution survives
        # an agent restart; ``Operation.to_dict`` truncates the key body before
        # it is displayed or logged.
        values = {"account": account, "public_key": key.line, "fingerprint": key.fingerprint}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_SSH_KEY_ADD,
            "account": account,
            "key": key.to_dict(),
            "authorized_keys": str(store.path),
        }

    def plan_key_remove(self, operation, *, account, fingerprint):
        store = self.keystore(account)
        match = next((item for item in store.list() if item.fingerprint == fingerprint), None)
        if match is None:
            raise SshServiceError("unknown_public_key", "no authorized key with that fingerprint")
        values = {"account": account, "fingerprint": fingerprint}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {"type": TYPE_SSH_KEY_REMOVE, "account": account, "key": match.to_dict()}

    def plan_revoke_all(self, operation, account):
        store = self.keystore(account)
        keys = [key.to_dict() for key in store.list()]
        values = {"account": account}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_SSH_REVOKE_ALL,
            "account": account,
            "keys": keys,
            "key_count": len(keys),
            "warning": "Every SSH key of this account is removed. Remote access through this "
            "account stops immediately.",
        }

    # --- execution -------------------------------------------------------

    def execute(self, operation):
        if operation.type == TYPE_SSH_SERVICE:
            return self._execute_service(operation)
        if operation.type == TYPE_SSH_KEY_ADD:
            return self._execute_key_add(operation)
        if operation.type == TYPE_SSH_KEY_REMOVE:
            return self._execute_key_remove(operation)
        if operation.type == TYPE_SSH_REVOKE_ALL:
            return self._execute_revoke_all(operation)
        raise SshServiceError("unknown_operation_type", f"{operation.type} is not executable")

    def _execute_service(self, operation):
        enabled = bool(operation.requested_target.get("enabled"))
        self._advance(operation, "enabling_ssh" if enabled else "disabling_ssh")
        result = self.systemd.enable(UNIT_SSH) if enabled else self.systemd.disable(UNIT_SSH)
        if not result.ok:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="ssh_service_failed",
                error={"code": "ssh_service_failed", "message": "systemctl reported an error"},
            )
            raise SshServiceError("ssh_service_failed", "the SSH service could not be changed")
        payload = {"enabled": enabled, "service": self.systemd.unit_state(UNIT_SSH)}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _execute_key_add(self, operation):
        account = operation.requested_target["account"]
        public_key = operation.requested_target["public_key"]
        store = self.keystore(account)
        self._advance(operation, "writing_authorized_keys")
        try:
            key = store.add(public_key)
        except ValidationError as exc:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="key_add_failed",
                error={"code": exc.code, "message": exc.message},
            )
            raise SshServiceError(exc.code, exc.message)
        self._attribute(account, [key.blob], managed=True)
        payload = {"account": account, "key": key.to_dict(), "key_count": len(store.list())}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _execute_key_remove(self, operation):
        account = operation.requested_target["account"]
        fingerprint = operation.requested_target["fingerprint"]
        store = self.keystore(account)
        self._advance(operation, "writing_authorized_keys")
        withdrawn = [key.blob for key in store.list() if key.fingerprint == fingerprint]
        try:
            removed = store.remove(fingerprint)
        except ValidationError as exc:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="key_remove_failed",
                error={"code": exc.code, "message": exc.message},
            )
            raise SshServiceError(exc.code, exc.message)
        self._attribute(account, withdrawn, managed=False)
        payload = {"account": account, "removed": removed, "key_count": len(store.list())}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _execute_revoke_all(self, operation):
        account = operation.requested_target["account"]
        store = self.keystore(account)
        self._advance(operation, "writing_authorized_keys")
        withdrawn = [key.blob for key in store.list()]
        removed = store.revoke_all()
        self._attribute(account, withdrawn, managed=False)
        payload = {"account": account, "removed": removed, "key_count": 0}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _advance(self, operation, stage, *, state=None, detail=None):
        self.operations.advance(operation.operation_id, stage, state=state, detail=detail)
        if self._operation_log is not None:
            self._operation_log.record(
                operation.operation_id, stage, operation_type=operation.type, detail=detail
            )
