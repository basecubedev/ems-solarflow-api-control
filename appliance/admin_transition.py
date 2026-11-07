# SPDX-FileCopyrightText: 2026 basecubedev
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
Which is why the stage has to be read: Admin keeps the record after it is done
and moves its deadline forward with the last write, so a record that is over
looks live to a clock for up to a full window afterwards -- and that window is
exactly when an operator reaches for the repair tools.
"""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from appliance.admin_deployment import read_env, read_service_environment

# Copies of names Admin owns. The appliance runs outside every container and
# cannot import the authority, so a contract test pins these to it.
ENV_ADMIN_DATA_DIR = "EMS_ADMIN_DATA_DIR"
ADMIN_DATA_SUBPATH = ("data", "admin")
STATE_SUBDIR = "state"
TRANSITION_FILE = "pending-transition.json"

MAX_TRANSITION_BYTES = 256 * 1024
# Admin's own DEFAULT_TRANSITION_TTL_SECONDS, pinned by a contract test: it
# renews the expiry this far ahead on every step, so a record that expires
# later than that was not written by Admin, and would block for ever.
MAX_TRANSITION_TTL_SECONDS = 3600

STATE_NONE = "none"
STATE_LIVE = "live"
STATE_EXPIRED = "expired"
STATE_UNREADABLE = "unreadable"
STATE_FINISHED = "finished"

# Admin's own TERMINAL_TRANSITION_STAGES: the two it refuses to advance out of.
# Copied because the appliance runs outside every container and cannot import
# the authority; a contract test pins the pair. Anything this side does not
# recognise is absent from here and therefore keeps blocking.
FINISHED_STAGES = frozenset({"completed", "cancelled"})


def admin_data_dir(paths, deployment=None):
    """Where Admin keeps its state, taken from the deployment, not assumed.

    The installer writes the value into the compose service's environment,
    which is what the container is started with; the environment file is kept
    for reference and answers only for a compose value that interpolates it. A
    deployment that names neither falls back to the layout the shipped
    installer creates.
    """

    if deployment is not None:
        try:
            compose = read_service_environment(
                deployment.compose_file.read_text(encoding="utf-8"), deployment.service
            )
        except OSError:
            compose = {}
        configured = str(compose.get(ENV_ADMIN_DATA_DIR) or "").strip()
        if configured.startswith("/"):
            return Path(configured)
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
        raw = _read_bounded(path)
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
    if record["stage"] in FINISHED_STAGES:
        # Admin renews the expiry on every durable step, the completing one
        # included, so a record it has finished with still names an hour in the
        # future. The stage says the operation is over; the clock only bounds
        # one that is not.
        record["state"] = STATE_FINISHED
        return record

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
    # A minute of slack for a stamp rounded up on the way to disk.
    if expires > current + timedelta(seconds=MAX_TRANSITION_TTL_SECONDS + 60):
        record["state"] = STATE_UNREADABLE
        record["reason"] = (
            f"the transition state expires at {record['expires_at']}, later than Admin "
            "ever sets one"
        )
        return record
    record["state"] = STATE_LIVE if current < expires else STATE_EXPIRED
    return record


def _read_bounded(path):
    """At most one byte past the limit, from a regular file and not through a link.

    The EMS container can write this tree, and the agent reading it is root: a
    FIFO there hung every status read and Admin plan, and a link to /dev/zero
    was read until memory ran out.
    """

    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise OSError("the transition state is not a regular file")
        chunks = []
        remaining = MAX_TRANSITION_BYTES + 1
        while remaining > 0:
            chunk = os.read(handle, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(handle)


def blocks_admin_mutation(record):
    return record.get("state") == STATE_LIVE


def refusal_message(record):
    """Why an Admin operation is refused, and when that ends without a shell."""

    stage = record.get("stage") or "an unnamed stage"
    expires = _expiry(record.get("expires_at"))
    until = (
        f" It stops blocking by itself at {expires.astimezone():%H:%M %Z} at the latest."
        if expires is not None
        else ""
    )
    return (
        "the Admin console is replacing itself right now "
        f"({stage}); this operation would fight it. Wait for that to finish.{until} "
        f"With a shell on the appliance, {record.get('path')} can be removed if it "
        "never will"
    )
