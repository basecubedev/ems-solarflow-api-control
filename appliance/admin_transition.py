# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the Admin console is in the middle of replacing itself.

Two management layers can write the same Admin deployment. The appliance edits
``docker-compose.admin.yml`` and ``.env.admin`` to install, roll back or repair
Admin; the Admin console replaces *itself* through System Build and Guided
Upgrade and writes the same keys. Neither knows about the other, and both are
correct in isolation.

One side has to yield, and it is this one: the Admin console is the only one of
the two that can be halfway through its own replacement, with a worker running
and a durable record of where it got to. So the appliance reads that record and
stands back while it is live.

It stands back only while the record is *live*. A transition that has passed
its own expiry is not an operation to protect — it is the wedged state an
operator came to the appliance to fix, and refusing to repair Admin because
Admin's own state file says it is busy would turn the recovery tool into part
of the problem. The same applies to a record this cannot read: a corrupt
transition is not a running one, and Admin's own resume is already broken.
Both cases are reported rather than silently ignored.

Nothing here is written. The appliance never edits the transition file: that
record belongs to Admin, and clearing it is Admin's business or the operator's.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from appliance.admin_deployment import read_env

# Copies of names Admin owns. The appliance runs outside every container and
# cannot import the authority, so a contract test pins these to it.
ENV_ADMIN_DATA_DIR = "EMS_ADMIN_DATA_DIR"
ADMIN_DATA_SUBPATH = ("data", "admin")
STATE_SUBDIR = "state"
TRANSITION_FILE = "pending-transition.json"

MAX_TRANSITION_BYTES = 256 * 1024

STATE_NONE = "none"
STATE_LIVE = "live"
STATE_EXPIRED = "expired"
STATE_UNREADABLE = "unreadable"


def admin_data_dir(paths, deployment=None):
    """Where Admin keeps its state, taken from the deployment, not assumed.

    The environment file is what the running container was started with, so it
    is the honest source. A deployment that does not name one falls back to the
    layout the shipped installer creates.
    """

    if deployment is not None:
        try:
            values = read_env(deployment.env_file.read_text(encoding="utf-8"))
        except OSError:
            values = {}
        configured = str(values.get(ENV_ADMIN_DATA_DIR) or "").strip()
        if configured.startswith("/"):
            return Path(configured)
    return Path(paths.install_root).joinpath(*ADMIN_DATA_SUBPATH)


def transition_path(paths, deployment=None):
    return admin_data_dir(paths, deployment) / STATE_SUBDIR / TRANSITION_FILE


def _expiry(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_transition(path, *, now=None):
    """Classify Admin's transition record without interpreting its contents.

    Only three things are read: that it exists, that it parses, and when it
    expires. Everything else in that file is Admin's own state machine, and
    the appliance has no business acting on it.
    """

    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"state": STATE_NONE, "path": str(path)}
    except OSError as exc:
        return {"state": STATE_UNREADABLE, "path": str(path), "reason": str(exc)}

    if len(raw) > MAX_TRANSITION_BYTES:
        return {
            "state": STATE_UNREADABLE,
            "path": str(path),
            "reason": "the transition state is implausibly large",
        }
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {
            "state": STATE_UNREADABLE,
            "path": str(path),
            "reason": "the transition state is not valid JSON",
        }
    if not isinstance(payload, dict):
        return {
            "state": STATE_UNREADABLE,
            "path": str(path),
            "reason": "the transition state is not an object",
        }

    record = {
        "path": str(path),
        "operation_id": str(payload.get("operation_id") or ""),
        "mode": str(payload.get("mode") or ""),
        "stage": str(payload.get("stage") or ""),
        "expires_at": str(payload.get("expires_at") or ""),
    }
    expires = _expiry(payload.get("expires_at"))
    if expires is None:
        # A transition with no usable expiry can never be classified as over,
        # which is how a stale record would block the appliance forever. It is
        # reported as unreadable so the operator sees the reason.
        record["state"] = STATE_UNREADABLE
        record["reason"] = "the transition state names no usable expiry"
        return record

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    record["state"] = STATE_LIVE if current < expires else STATE_EXPIRED
    return record


def blocks_admin_mutation(record):
    return record.get("state") == STATE_LIVE


def refusal_message(record):
    stage = record.get("stage") or "an unnamed stage"
    return (
        "the Admin console is replacing itself right now "
        f"({stage}); this operation would fight it. Wait for that to finish, or "
        f"clear {record.get('path')} if it never will"
    )
