# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client for the privileged agent socket.

Used by the unprivileged web process and by the host CLI. It only speaks the
operation allowlist; there is no escape hatch that forwards a raw command.
"""

import json
import socket

DEFAULT_TIMEOUT = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class AgentUnavailableError(Exception):
    code = "agent_unavailable"

    def __init__(self, message="the appliance agent is not reachable"):
        super().__init__(message)
        self.message = message


class AgentCallError(Exception):
    def __init__(self, code, message, *, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class AgentClient:
    def __init__(self, socket_path, *, timeout=DEFAULT_TIMEOUT, connect=None):
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self._connect = connect or self._unix_connect

    def _unix_connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self.socket_path)
        except OSError as exc:
            connection.close()
            raise AgentUnavailableError(f"cannot reach the appliance agent: {exc.strerror or exc}")
        return connection

    def call(self, operation, *, actor="", source_ip="", timeout=None, **fields):
        payload = {"operation": operation, **fields}
        if actor:
            payload["actor"] = actor
        if source_ip:
            payload["source_ip"] = source_ip

        connection = self._connect()
        try:
            if timeout is not None:
                connection.settimeout(timeout)
            connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            raw = self._read_line(connection)
        except OSError as exc:
            raise AgentUnavailableError(f"the appliance agent closed the connection: {exc}")
        finally:
            connection.close()

        try:
            response = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AgentCallError("agent_response_invalid", "the agent returned malformed JSON")

        if not isinstance(response, dict):
            raise AgentCallError("agent_response_invalid", "the agent returned an unexpected reply")
        if response.get("ok"):
            return response.get("result")

        error = response.get("error") or {}
        raise AgentCallError(
            str(error.get("code") or "agent_error"),
            str(error.get("message") or "the appliance agent refused the request"),
            field=error.get("field"),
        )

    def _read_line(self, connection):
        chunks, total = [], 0
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise AgentCallError("agent_response_too_large", "the agent reply is too large")
            if chunk.endswith(b"\n"):
                break
        return b"".join(chunks)

    def available(self):
        try:
            connection = self._connect()
        except AgentUnavailableError:
            return False
        connection.close()
        return True


class InProcessAgentClient:
    """Direct in-process client for tests and the single-process CLI."""

    def __init__(self, handlers):
        self.handlers = handlers

    def call(self, operation, *, actor="", source_ip="", timeout=None, **fields):
        from appliance.agent import AgentError
        from appliance.operations import OperationError
        from appliance.protocol import ProtocolError
        from appliance.validation import ValidationError

        try:
            return self.handlers.dispatch(
                {"operation": operation, **fields}, actor=actor, source_ip=source_ip
            )
        except ProtocolError as exc:
            raise AgentCallError(exc.code, exc.message, field=exc.field)
        except (AgentError, ValidationError, OperationError) as exc:
            raise AgentCallError(
                getattr(exc, "code", "operation_failed"), str(getattr(exc, "message", exc))
            )

    def available(self):
        return True
