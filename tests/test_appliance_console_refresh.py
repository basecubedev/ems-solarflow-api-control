# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Appliance Manager console re-reads once the appliance has changed.

Most of the console is live: `/api/status` and `/api/manager` are read again
every fifth tick, so the installed version, the container state and the
deadline all follow the appliance. Four views are not. The package index, the
release catalogue, the backup account and the settings are fetched lazily on
the first render that needs them and then held in `state.data` for the rest of
the session -- a tick may not re-read them, because one is a network round trip
to a remote index and another costs a `statvfs` per exported path.

Held for the session was the bug. After a manager install the index still
reported the version that had just been replaced, so the release just installed
was still offered as "newer" and going back to it was still marked as the
dangerous direction. After adding a public key the table listed two keys while
the card beside it said one. Nothing but reloading the page in the browser put
either right, and the explicit Refresh button did not either.

So the rule these tests hold: an operation that settled changed the appliance,
and a view derived from the appliance is dropped rather than shown. Dropped,
not emptied -- the lazy guard fires on an absent key, while a key set to null
is the console's own "still loading" and would stick there forever.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

from appliance import operations

pytestmark = [
    pytest.mark.appliance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "appliance_refresh_runner.js")
APP = open(
    os.path.join(ROOT, "appliance", "static", "app.js"), encoding="utf-8"
).read()

SETTLED = sorted(operations.SETTLED_STATES)
IN_FLIGHT = sorted(set(operations.ALL_STATES) - operations.SETTLED_STATES)

# The views the console fetches lazily and then holds. The wifi scan and the log
# are deliberately absent: both are per-click reads, and an operation settling
# is not a reason to start a radio scan.
DERIVED = ["releases", "managerSources", "backup", "settings"]


# Every view fetched through loadInto. `wifi` is a radio scan started by its own
# button and is deliberately not dropped behind the operator's back; the log has
# its own loader. Derived from the source so a fifth held view cannot be added
# without this file and DERIVED_VIEWS agreeing on what it is.
PER_CLICK = {"wifi"}


def held_view_keys():
    return set(re.findall(r'loadInto\("([a-zA-Z]+)"', APP))


def drive(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["steps"]


def operation(state, operation_id="op-1", also_unacknowledged=(), finished_at=0):
    """An operations payload as `/api/operations` reports it.

    The agent's own classification decides where a record goes: a settled one is
    unacknowledged until the operator clears the banner, anything else is the
    active operation. `also_unacknowledged` carries records settled earlier that
    the operator has not cleared yet, as `(id, state)` or `(id, state, finished)`;
    the list is newest first, as the agent sorts it.
    """

    record = {
        "operation_id": operation_id, "state": state, "finished_at": finished_at
    }
    older = [
        {
            "operation_id": item[0],
            "state": item[1],
            "finished_at": item[2] if len(item) > 2 else 0,
        }
        for item in also_unacknowledged
    ]
    if state in operations.SETTLED_STATES:
        return {"active": None, "unacknowledged": [record] + older}
    return {"active": record, "unacknowledged": older}


def test_a_settled_operation_drops_the_views_it_may_have_changed():
    """The reported bug, driven through the shipped poller.

    A manager install runs and succeeds. The next poll sees a settled
    operation, and from that moment the console may not go on showing an index
    that was read before the package changed.
    """

    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [operation("running"), operation("succeeded")],
    })

    assert steps[0]["held"] == DERIVED, "a running operation changed nothing yet"
    assert steps[1]["held"] == [], "a settled operation leaves no view behind"


def test_a_view_is_dropped_and_not_emptied():
    """`null` is the console's own "still loading" and never re-reads."""

    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [operation("succeeded")],
    })

    assert steps[0]["held"] == []


@pytest.mark.parametrize("settled", SETTLED)
def test_every_settled_state_counts_as_a_change(settled):
    """A failed install wrote to this appliance too; only the outcome differs.

    `failed_recoverable` is in here for the same reason the agent counts it as
    settled: the operation is over and the appliance is not what it was. The
    parameters are the agent's own set, so a state added on the backend arrives
    here rather than being forgotten.
    """

    steps = drive({"mode": "poll", "seed": DERIVED, "ticks": [operation(settled)]})

    assert steps[0]["held"] == []


def test_the_console_keeps_no_copy_of_the_settled_set():
    """Which states are settled is the agent's decision and stays there.

    The console reads it from where the agent already applied it -- a record in
    the unacknowledged list is a settled one -- rather than from a list of state
    names of its own, which would drift the moment the backend gained one.

    Naming a single settled state is not that copy: `CANCELLABLE_STATES` names
    `failed_recoverable` to answer a different question, which of the states a
    plan may still be withdrawn from. A list carrying two or more of them is a
    second settled set, whatever it is called.
    """

    for declaration in re.finditer(r"var ([A-Z_]+) = \[(.*?)\];", APP, re.S):
        listed = set(re.findall(r'"([a-z_]+)"', declaration.group(2)))
        overlap = sorted(listed & set(SETTLED))
        assert len(overlap) < 2, (
            "var " + declaration.group(1) + " is a second settled set: "
            + ", ".join(overlap)
        )


@pytest.mark.parametrize("pending", IN_FLIGHT)
def test_an_operation_that_is_still_in_flight_is_not_a_reason_to_refetch(pending):
    """Every tick would otherwise read a remote index: one round trip per tick."""

    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [operation(pending), operation(pending)],
    })

    assert [step["held"] for step in steps] == [DERIVED, DERIVED]


def test_a_settle_the_banner_never_showed_counts_too():
    """The banner shows one operation, and not always the one that settled.

    An operation settles and the operator starts another inside the same
    two-second poll, so the console never sees the first one on the banner at
    all: it goes straight from "the first is running" to "the second is
    running". Reading the state of the operation being shown missed that
    entirely. Counting the settled records the agent reports does not -- one
    more of them is the whole signal, whichever operation the banner is on.
    """

    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [
            operation("running", "op-1"),
            operation("running", "op-2", also_unacknowledged=[("op-1", "failed_recoverable")]),
            operation("running", "op-2", also_unacknowledged=[("op-1", "failed_recoverable")]),
        ],
    })

    assert steps[0]["held"] == DERIVED
    assert steps[1]["held"] == [], "the settle the banner skipped left a stale view"
    assert "/api/status" in steps[1]["fetched"]


def test_two_settled_operations_in_a_row_each_count():
    """An operator who does not clear the banner leaves both records in the
    payload, and the second settle has to be noticed as well.

    The first poll counts the record that was already settled when the page
    opened -- the operator may have signed in after the fact -- so the run below
    starts there and the second settle is the third tick.
    """

    settled_earlier = [("op-1", "succeeded")]
    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [
            operation("running", "op-2", also_unacknowledged=settled_earlier),
            operation("running", "op-2", also_unacknowledged=settled_earlier),
            operation("succeeded", "op-2", also_unacknowledged=settled_earlier),
            operation("succeeded", "op-2", also_unacknowledged=settled_earlier),
        ],
    })

    assert steps[0]["held"] == [], "the first poll learns of a settle it missed"
    assert steps[1]["held"] == DERIVED, "nothing new settled"
    assert steps[2]["held"] == [], "the second settled operation was not counted"
    assert "/api/status" in steps[2]["fetched"]


def test_a_retry_that_settles_again_counts_although_the_list_did_not_grow():
    """The unacknowledged list does not only grow.

    A recoverable failure can be retried in place: the record leaves the list
    while it runs and comes back when it settles again. If a retry settles inside
    one two-second poll the list is the same length as before, so length alone
    would report nothing happened -- on an appliance that had just been written
    to twice. The record's own finish time is what says otherwise.
    """

    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [
            operation("failed_recoverable", "op-1", finished_at=100),
            operation("failed_recoverable", "op-1", finished_at=100),
            operation("succeeded", "op-1", finished_at=140),
        ],
    })

    assert steps[0]["held"] == [], "the first settle"
    assert steps[1]["held"] == DERIVED, "nothing changed in between"
    assert steps[2]["held"] == [], "the retry settled and was not counted"


def test_the_same_settled_operation_is_only_acted_on_once():
    """The banner stays up until the operator clears it.

    Acting on every poll for as long as it is there would read the remote package
    index every two seconds. Nothing is held after the first of those polls, so
    the cost is read from what each one asked for.
    """

    settled = operation("succeeded", finished_at=100)
    steps = drive({"mode": "poll", "seed": DERIVED, "ticks": [settled] * 3})

    assert "/api/status" in steps[0]["fetched"]
    assert steps[1]["fetched"] == ["/api/operations"]
    assert steps[2]["fetched"] == ["/api/operations"]


def test_only_the_refresh_button_drops_the_held_views():
    """refresh() is reached from six places and five of them are automatic.

    Signing in, booting, acknowledging a banner, cancelling a plan and a plan the
    appliance refused all reach it, and none of them changed the host -- a refused
    plan is the most common failure there is. Dropping the held views on those
    paths would fetch the remote index after every refusal, so the invalidation
    belongs to the one caller that means it.
    """

    reads = APP.count("invalidateDerivedViews()")
    assert reads == 3, (
        "invalidateDerivedViews is called from " + str(reads) + " places; it is "
        "meant to be its own definition, the settle, and refreshEverything"
    )
    assert "function refreshEverything()" in APP
    assert 'addEventListener("click", function () { refreshEverything(); })' in APP

    body = APP.split("function refresh() {", 1)[1].split("\n  }", 1)[0]
    assert "invalidateDerivedViews" not in body, (
        "refresh() drops the held views, so every automatic caller does too"
    )


def test_a_settled_operation_also_brings_the_host_state_forward():
    """The installed version and the index must not disagree for ten seconds.

    The host state joins every fifth tick because it costs a `docker inspect`
    and a `statvfs`. An operation that just settled is exactly the moment that
    cadence is wrong: the card would name the replaced version while the index
    beside it already named the new one. It is read from the settle itself, not
    left to the next tick, so the two cannot disagree at all.
    """

    steps = drive({
        "mode": "poll",
        "seed": DERIVED,
        "ticks": [operation("running"), operation("succeeded")],
    })

    assert steps[0]["fetched"] == ["/api/operations"], "a plain tick stays cheap"
    assert "/api/status" in steps[1]["fetched"]
    assert "/api/manager" in steps[1]["fetched"]


def test_the_explicit_refresh_reads_everything_the_page_shows():
    """The Refresh button is what an operator presses when a value looks wrong.

    It read the host state and the manager state and left the four held views
    exactly as they were, which made it useless for the one complaint it exists
    to answer.
    """

    steps = drive({"mode": "refresh", "seed": DERIVED})

    assert steps[0]["held"] == []
    assert "/api/status" in steps[0]["fetched"]
    assert "/api/manager" in steps[0]["fetched"]


def test_a_scan_and_a_log_are_not_dropped_behind_the_operators_back():
    """Both are per-click reads. The wifi list is a radio scan that takes
    seconds, and the log is whichever source the operator chose to look at."""

    steps = drive({
        "mode": "poll",
        "seed": ["wifi", "log", "logSource"],
        "ticks": [operation("succeeded")],
    })

    assert steps[0]["held"] == ["wifi", "log", "logSource"]


def test_the_list_of_held_views_is_the_one_the_renders_actually_use():
    """Two registries that must agree, walked.

    `DERIVED_VIEWS` is what the console drops; the `loadInto` call sites are what
    it holds. A fifth held view added to a render and forgotten in the list is
    the reported bug again, for that view, with the suite green -- which is why
    the list is checked against the call sites rather than against itself.
    """

    declared = re.search(r"var DERIVED_VIEWS = \[(.*?)\];", APP, re.S)
    assert declared, "app.js declares no DERIVED_VIEWS"
    listed = set(re.findall(r'"([a-zA-Z]+)"', declared.group(1)))

    assert listed == set(DERIVED), "the test and app.js disagree about the list"
    assert held_view_keys() == listed | PER_CLICK, (
        "a view is fetched through loadInto that is neither dropped on a change "
        "nor declared a per-click read: " + repr(held_view_keys() ^ (listed | PER_CLICK))
    )


@pytest.mark.parametrize("key", DERIVED)
def test_each_held_view_is_re_read_on_an_absent_key_and_not_a_falsy_one(key):
    """The guard that makes dropping work, read from the source.

    While a read is in flight the key holds null. A guard written `if (!value)`
    would fire on that too, so every two-second tick would start another read of
    the remote index -- the per-tick round trip the whole design avoids -- and it
    would look correct from the outside.
    """

    site = APP.index('loadInto("' + key + '"')
    guard = APP.rindex("= state.data." + key + ";", 0, site)

    assert "=== undefined" in APP[guard:site], (
        "the lazy guard for " + key + " does not test for an absent key"
    )


def test_a_read_that_was_already_on_its_way_cannot_put_the_old_answer_back():
    """The bug the fix could have restored to itself.

    Reading the package index is a round trip to a remote host, so an operation
    can settle while one is in flight. Storing that answer afterwards would pin
    the pre-operation index back under a key nothing re-reads -- exactly the
    reported symptom, delivered by the fetch that was already on its way.
    """

    steps = drive({"mode": "race", "key": "managerSources", "path": "/api/manager/sources"})

    assert steps[0]["held"] == [], "the stale answer was stored after the change"
