# SPDX-License-Identifier: AGPL-3.0-or-later
"""An analytics schema that was never created has to say so.

Found on a live Raspberry Pi 3B+ appliance. Analytics had been empty since the
system was installed, and every layer reported health:

* InfluxDB answered `/health` with "ready for queries and writes"
* `/api/analytics/status` answered `available: true`
* the 1h and 6h ranges returned real data

Only the default 24h range was empty, because it reads the downsampled bucket
`ems_1m`, and that bucket had never been created. InfluxDB answers a query
against a missing bucket with the same 404 it uses for an unknown org, the
dashboard turned that into a 503, and the browser renders a 503 as an empty
chart with no explanation.

The buckets were missing because `influx sync` ran while InfluxDB was still
starting. On that hardware InfluxDB took 35 seconds to listen; the readiness
wait gave it 15. On a PC it is ready well inside that, which is why the same
code path had always worked.

Two properties follow, and this module pins both: the readiness budget is set
for the slowest hardware the project supports rather than the fastest, and a
missing bucket reaches the operator as a sentence they can act on.
"""

import logging
import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytestmark = [
    pytest.mark.integration,
]


# What the appliance measured, and the floor any budget here has to clear.
# InfluxDB 2.7 on a Pi 3B+ with an SD card: container start 19:52:45, listening
# on 8086 at 19:53:19. A first start also runs the setup wrapper, which creates
# the org, bucket, user and token, and takes longer still.
MEASURED_PI_STARTUP_SECONDS = 35


# --- the readiness budget fits the hardware, and its caller fits the budget --


def test_the_readiness_budget_covers_the_slowest_supported_hardware():
    from ems.influx_setup import INFLUX_READY_TIMEOUT_SECONDS

    assert INFLUX_READY_TIMEOUT_SECONDS >= 2 * MEASURED_PI_STARTUP_SECONDS


_TEMPLATE_LEVELS = 3


def _counted_schema_requests(action, existing=("ems_raw",), levels=None):
    """How many HTTP requests one schema operation makes, counted by running it.

    Derived rather than assumed: the ceilings that contain these operations are
    arithmetic over this number, and it is not obvious from the call sites --
    `ensure_bucket_retention` looks up the bucket again and resolves the org per
    creation, so a fresh install costs far more than one request per bucket.
    """

    import json

    from ems.history import schema
    from ems.history.influx_client import HistoryInfluxClient

    with open("config/config.template.json", encoding="utf-8") as handle:
        influx = json.load(handle)["influxdb"]

    if levels is not None:
        chain = ["1m", "5m", "1h", "6h", "12h", "1d", "1w"][:levels]
        source = "raw"
        entries = []
        for target in chain:
            entries.append({"source": source, "target": target, "window": target})
            source = target
        influx = dict(influx, downsampling=entries)

    present = set(existing)
    calls = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Session:
        def get(self, url, **kwargs):
            calls.append(url)
            if "buckets" in url:
                name = (kwargs.get("params") or {}).get("name")
                if name in present:
                    return _Response(
                        {
                            "buckets": [
                                {
                                    "id": "b",
                                    "name": name,
                                    "retentionRules": [{"everySeconds": 0}],
                                }
                            ]
                        }
                    )
                return _Response({"buckets": []})
            if "orgs" in url:
                return _Response({"orgs": [{"id": "org", "name": "ems"}]})
            if "tasks" in url:
                return _Response({"tasks": []})
            return _Response({})

        def post(self, url, **kwargs):
            calls.append(url)
            return _Response({"id": "new"})

        def patch(self, url, **kwargs):
            calls.append(url)
            return _Response({"id": "b"})

    client = HistoryInfluxClient("http://influxdb:8086", "ems", "t", session=_Session())
    getattr(schema, action)(client, influx)

    return len(calls)


def test_the_admin_sync_call_outlives_the_work_it_contains():
    """The subprocess timeout has to leave room for the work after the wait.

    These numbers live in different files and drift apart silently: a wait
    longer than the call that contains it turns a slow start into a killed
    process, which is the same empty Analytics tab by another route. So does a
    call killed while it is still creating tasks.

    Sized for a sync that is slow, not for one where every request runs into its
    own timeout: that worst case is about twenty minutes, it cannot be held
    inside a synchronous Admin request, and waiting it out would not change the
    outcome of a sync whose every request has already failed. What the ceiling
    must not do is kill a sync that is merely slow, and that is what this pins.
    """

    from admin.container_actions import (
        INFLUX_SYNC_REQUEST_ALLOWANCE,
        INFLUX_SYNC_SLOW_REQUEST_SECONDS,
        INFLUX_SYNC_TIMEOUT_SECONDS,
    )
    from ems.influx_setup import (
        INFLUX_READY_TIMEOUT_SECONDS,
        INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
    )

    work = INFLUX_SYNC_REQUEST_ALLOWANCE * INFLUX_SYNC_SLOW_REQUEST_SECONDS

    assert INFLUX_SYNC_TIMEOUT_SECONDS > INFLUX_READY_TIMEOUT_SECONDS
    assert INFLUX_SYNC_TIMEOUT_SECONDS >= INFLUX_READY_TIMEOUT_SECONDS + work

    # "Slow" has to stay well inside what a single request is allowed before it
    # is treated as stuck, or the two numbers are describing the same thing.
    assert INFLUX_SYNC_SLOW_REQUEST_SECONDS < INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS

    # The count comes from the operator's downsampling chain, so the allowance
    # has to hold for a longer one than we ship -- one level, which the comment
    # on the constant states and this is what checks it.
    assert INFLUX_SYNC_REQUEST_ALLOWANCE >= _counted_schema_requests(
        "sync", levels=_TEMPLATE_LEVELS + 1
    )


def _recorded_wait_for(action, **kwargs):
    from unittest.mock import patch

    recorded = {}

    def fake_wait(client, timeout_s=None, **kwargs):
        recorded["timeout_s"] = timeout_s

    config = {
        "org": "ems",
        "token": "t",
        "token_env": "INFLUXDB_TOKEN",
        "bucket_prefix": "ems",
        "url": "http://influxdb:8086",
        "host_url": "http://127.0.0.1:8086",
    }

    import emsctl

    with patch("ems.history.influx_client.wait_for_influx_ready", fake_wait), patch(
        "ems.history.schema.sync", return_value={"buckets": [], "tasks": []}
    ), patch(
        "ems.history.schema.status", return_value={"buckets": [], "tasks": []}
    ), patch.object(emsctl, "resolve_influx_token_with_secret_file", return_value="t"):
        emsctl.execute_influx_schema_op(config, action, **kwargs)

    return recorded["timeout_s"]


def test_the_sync_path_waits_with_the_full_budget():
    from ems.influx_setup import INFLUX_READY_TIMEOUT_SECONDS

    assert _recorded_wait_for("sync") == INFLUX_READY_TIMEOUT_SECONDS


def test_a_status_read_does_not_wait_like_a_freshly_started_container():
    """A status read defaults to the diagnostic budget.

    It runs inside the Admin checks and the guided-upgrade health probe.
    Waiting ninety seconds there for an InfluxDB that is simply stopped is a
    hang, and the upgrade's own reconnect poller gives up first. A caller that
    knows better overrides it; see the test below.
    """

    from ems.influx_setup import (
        INFLUX_PROBE_READY_TIMEOUT_SECONDS,
        INFLUX_READY_TIMEOUT_SECONDS,
    )

    waited = _recorded_wait_for("status")

    assert waited == INFLUX_PROBE_READY_TIMEOUT_SECONDS
    assert waited < INFLUX_READY_TIMEOUT_SECONDS


def test_a_caller_that_just_started_the_container_asks_for_patience():
    """`influx init` probes readiness by running `status` right after starting
    the container -- the one case where the long budget is the point."""

    from ems.influx_setup import INFLUX_READY_TIMEOUT_SECONDS

    assert (
        _recorded_wait_for("status", ready_timeout_s=INFLUX_READY_TIMEOUT_SECONDS)
        == INFLUX_READY_TIMEOUT_SECONDS
    )


def test_the_patient_wait_hangs_off_the_action_because_admin_cannot_ask_for_it():
    """Why the budget defaults by action rather than by caller.

    The one caller that must be patient starts the container and syncs straight
    after -- and it does so as a subprocess, with no parameter to pass. Should
    that ever become an in-process call or grow a flag, the patient default
    belongs back on the caller and `sync` should be as impatient as the rest.
    """

    from admin.container_actions import _default_run_influx_sync

    recorded = {}

    class _Compose:
        def run_oneoff(self, workspace, service, argv, **kwargs):
            recorded["argv"] = list(argv)
            return 0, ""

    _default_run_influx_sync(_Compose(), "/workspace")

    assert recorded["argv"][:4] == ["python3", "emsctl.py", "influx", "sync"]
    assert not [arg for arg in recorded["argv"] if arg.startswith("--wait")]


def test_the_admin_check_outlives_the_work_it_runs():
    """It runs `influx status`, which waits and then makes its requests.

    One per planned bucket plus the task list, each on the client's own budget,
    all behind a docker exec and an interpreter start. Killing it early reports
    "timed out" where the reachability error was the answer worth having -- and
    a killed check is reported as a hard failure, which is the opposite of what
    `warn_on_fail` marks this check for.

    Derived from the config template so an added downsampling level, which adds
    a bucket lookup and a task, cannot quietly outgrow the ceiling.
    """

    from admin.ems_cli import CHECKS, INFLUX_STATUS_CHECK_REQUEST_ALLOWANCE
    from ems.influx_setup import (
        INFLUX_PROBE_READY_TIMEOUT_SECONDS,
        INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS,
    )

    # Twice the budget per request: requests spends it on connect and again on
    # read, and here the budget is small enough that the pathological case is
    # worth covering rather than writing off.
    counted = _counted_schema_requests("status", levels=_TEMPLATE_LEVELS + 3)
    work = (
        INFLUX_STATUS_CHECK_REQUEST_ALLOWANCE
        * 2
        * INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS
    )

    assert CHECKS["influx_status"]["timeout"] >= (
        INFLUX_PROBE_READY_TIMEOUT_SECONDS + work + 25
    )
    # The count comes from the operator's downsampling list, not from the
    # template, so the allowance has to hold for a longer chain than we ship.
    assert INFLUX_STATUS_CHECK_REQUEST_ALLOWANCE >= counted


def _recorded_request_timeouts(*calls):
    """Build the client for each (action, kwargs) call and record its budget."""

    from unittest.mock import patch

    seen = []

    class _Client:
        def __init__(self, url, org, token, timeout=None, **kwargs):
            seen.append(timeout)

    config = {
        "org": "ems",
        "token": "t",
        "token_env": "INFLUXDB_TOKEN",
        "bucket_prefix": "ems",
        "url": "http://influxdb:8086",
        "host_url": "http://127.0.0.1:8086",
    }

    import emsctl

    with patch("ems.history.influx_client.HistoryInfluxClient", _Client), patch(
        "ems.history.influx_client.wait_for_influx_ready"
    ), patch("ems.history.schema.sync", return_value={}), patch(
        "ems.history.schema.status", return_value={}
    ), patch.object(emsctl, "resolve_influx_token_with_secret_file", return_value="t"):
        for action, kwargs in calls:
            emsctl.execute_influx_schema_op(config, action, **kwargs)

    return seen


def test_a_status_read_bounds_its_requests_and_a_sync_does_not():
    """The two ceilings are arithmetic, and this is what makes them true.

    Left on the client default a status read's worst case is a multiple of the
    wait it follows, and the Admin runs its checks one after another. A sync
    keeps the generous budget: it creates buckets and Flux tasks, which is real
    work rather than a lookup, and a diagnostic budget there aborts it halfway.
    """

    from ems.influx_setup import (
        INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS,
        INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
    )

    assert _recorded_request_timeouts(("status", {}), ("sync", {})) == [
        INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS,
        INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
    ]


def test_the_sync_budget_and_the_client_default_stay_the_same_number():
    """Two copies of one number, because they cannot be one.

    `ems/influx_setup.py` is one of the few `ems` modules the Admin image
    carries; the client it would import from pulls in `requests`, which that
    image has no reason to hold. So the pair is walked here instead.
    """

    from ems.influx_setup import INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS
    from scripts.influx_utils import DEFAULT_REQUEST_TIMEOUT_SECONDS

    assert INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS == DEFAULT_REQUEST_TIMEOUT_SECONDS


def test_the_admin_image_does_not_have_to_carry_the_influx_client():
    """The reason the number above is written out twice.

    If this ever stops being true the duplication should go, not the test.
    """

    dockerfile = open("deploy/admin/Dockerfile", encoding="utf-8").read()

    assert "COPY ems/influx_setup.py" in dockerfile
    assert "COPY scripts/influx_utils.py" not in dockerfile


def test_the_budget_is_what_one_phase_may_take_not_what_both_may():
    """Passed as a scalar on purpose.

    `requests` applies a scalar to connect and to read separately, so it is the
    tolerance per phase. Halving it to cap the sum would make every individual
    phase less tolerant than before -- and a bucket lookup that takes three
    seconds on a loaded board is slow, not broken.
    """

    spent = _recorded_request_timeouts(("status", {}))

    assert all(isinstance(value, (int, float)) for value in spent)


def test_failing_fast_on_the_url_does_not_rush_the_work_behind_it():
    """The wait and the per-request budget are separate questions.

    External InfluxDB is user-managed, so `influx init` fails fast on a wrong
    URL -- but once the URL answers, the sync behind it still creates four
    buckets and three Flux tasks, and must not be held to a lookup's budget.
    """

    from ems.influx_setup import (
        INFLUX_PROBE_READY_TIMEOUT_SECONDS,
        INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
    )

    assert _recorded_request_timeouts(
        ("sync", {"ready_timeout_s": INFLUX_PROBE_READY_TIMEOUT_SECONDS})
    ) == [INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS]


def test_the_readiness_wait_accepts_the_budget_the_status_path_sets():
    """The wait does arithmetic on the client's own timeout.

    `requests` accepts a (connect, read) pair wherever it accepts a number, and
    the wait used to assume a scalar -- a TypeError for any caller that bounds
    a client that way. Recording the value in a test double cannot show that;
    only the real client through the real wait does.
    """

    from ems.influx_setup import INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS
    from scripts.influx_utils import InfluxHTTPClient, wait_for_influx_ready

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "pass"}

    class _Session:
        def __init__(self):
            self.timeouts = []

        def get(self, url, headers=None, timeout=None):
            self.timeouts.append(timeout)
            return _Response()

    half = INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS / 2
    session = _Session()
    client = InfluxHTTPClient(
        "http://influxdb:8086", "ems", "t", session=session, timeout=(half, half)
    )

    wait_for_influx_ready(client, timeout_s=5, interval_s=2)

    assert session.timeouts
    for spent in session.timeouts:
        assert isinstance(spent, (int, float))
        assert 0 < spent <= INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS


def test_the_caller_that_just_started_the_container_is_not_rushed_either():
    """`influx init` probes with `status` right after starting the container.

    On the hardware this whole change is about, that is the worst moment to hold
    each request to a diagnostic budget: the board is loaded, and a short read
    would report `ready: false` after an init that in fact worked. So it asks
    for both -- the patient wait and the generous requests.
    """

    from ems.influx_setup import (
        INFLUX_READY_TIMEOUT_SECONDS,
        INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
    )

    assert _recorded_request_timeouts(
        (
            "status",
            {
                "ready_timeout_s": INFLUX_READY_TIMEOUT_SECONDS,
                "request_timeout_s": INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
            },
        )
    ) == [INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS]


def test_both_surfaces_send_the_operator_to_the_same_command():
    """Two texts on two surfaces for one fault, and they must not drift.

    They cannot share a constant: `dashboard.server` deliberately imports no
    `ems` module at import time, and every ems import in it is local to a
    function. So the pair is walked here instead.
    """

    from dashboard.server import ANALYTICS_SCHEMA_HINT
    from ems.history.influx_writer import SCHEMA_SYNC_HINT

    command = "python3 emsctl.py influx sync"

    assert command in ANALYTICS_SCHEMA_HINT
    assert command in SCHEMA_SYNC_HINT


def test_the_dashboard_still_imports_no_ems_module_eagerly():
    """The reason the hint above is duplicated rather than shared.

    If this ever stops being true the duplication should go, not the test.
    """

    import subprocess
    import sys

    probe = (
        "import sys; before = set(sys.modules); import dashboard.server; "
        "print([m for m in set(sys.modules) - before if m.split('.')[0] == 'ems'])"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "[]"


# --- a missing bucket is reported, not swallowed ----------------------------


START = datetime(2026, 9, 24, tzinfo=timezone.utc)
END = datetime(2026, 9, 25, tzinfo=timezone.utc)


class _Provider:
    """Stands in for the InfluxDB analytics provider."""

    name = "influxdb"

    def __init__(self, missing=(), raises=True, needed="ems_1m", planned=True):
        self._missing = list(missing)
        self._raises = raises
        self._needed = needed
        self.asked_about = "never asked"
        self._planned = planned

    def bucket_for_range(self, start, end):
        return self._needed

    def available(self):
        return True

    def query(self, *args, **kwargs):
        if self._raises:
            raise RuntimeError("404 Client Error: Not Found for url: .../query")
        raise AssertionError("not reached")

    def missing_buckets(self, first=None):
        self.asked_about = first
        return list(self._missing)

    def is_planned_bucket(self, name):
        return self._planned


def test_a_missing_bucket_becomes_an_actionable_answer():
    from dashboard.server import analytics_schema_gap_payload

    payload = analytics_schema_gap_payload(
        _Provider(missing=["ems_1m", "ems_5m"], needed="ems_1m"), START, END
    )

    assert payload["available"] is False
    assert payload["reason"] == "schema_incomplete"
    assert payload["bucket"] == "ems_1m"
    assert payload["missing_buckets"] == ["ems_1m", "ems_5m"]
    assert "influx sync" in payload["hint"]


def test_a_bucket_no_sync_would_create_gets_the_other_sentence():
    """Same symptom, opposite fix.

    A query profile naming a bucket that no downsampling entry produces is a
    config disagreeing with itself. Sending the operator to `influx sync` there
    is a loop: the command runs, reports success, and changes nothing.
    """

    from dashboard.server import analytics_schema_gap_payload

    payload = analytics_schema_gap_payload(
        _Provider(missing=["ems_10m"], needed="ems_10m", planned=False), START, END
    )

    assert payload["reason"] == "schema_incomplete"
    assert "influx sync" not in payload["hint"]
    assert "query_profiles" in payload["hint"]


def test_a_bucket_the_sync_would_create_keeps_the_command():
    from dashboard.server import analytics_schema_gap_payload

    payload = analytics_schema_gap_payload(
        _Provider(missing=["ems_1m"], needed="ems_1m"), START, END
    )

    assert "python3 emsctl.py influx sync" in payload["hint"]


def test_a_provider_that_cannot_say_keeps_the_common_answer(caplog):
    """The plan check is a refinement, not a precondition for the diagnosis."""

    class NoPlan(_Provider):
        def is_planned_bucket(self, name):
            raise RuntimeError("no config")

    from dashboard.server import analytics_schema_gap_payload

    with caplog.at_level(logging.ERROR):
        payload = analytics_schema_gap_payload(
            NoPlan(missing=["ems_1m"], needed="ems_1m"), START, END
        )

    assert "python3 emsctl.py influx sync" in payload["hint"]
    assert "dashboard_analytics_schema_plan_check_failed" in caplog.text


def test_the_gap_check_asks_about_the_bucket_that_decides_the_answer():
    """The probe is budgeted, so it has to know which lookup is the one that
    matters -- otherwise the deciding bucket can be the one left undecided."""

    from dashboard.server import analytics_schema_gap_payload

    provider = _Provider(missing=["ems_1m"], needed="ems_1m")
    analytics_schema_gap_payload(provider, START, END)

    assert provider.asked_about == "ems_1m"


def test_a_check_that_cannot_run_leaves_a_trace(caplog):
    """Silence here is the unexplained empty chart this change removes."""

    class Broken(_Provider):
        def bucket_for_range(self, start, end):
            raise KeyError("bucket_prefix")

    from dashboard.server import analytics_schema_gap_payload

    with caplog.at_level(logging.ERROR):
        assert analytics_schema_gap_payload(Broken(), START, END) is None

    assert "dashboard_analytics_schema_check_failed" in caplog.text


def test_a_partial_schema_does_not_explain_an_unrelated_failure():
    """The live fault left the raw bucket working while the rest were missing.

    A short range failing for its own reason -- an auth error, a restart, a bad
    query -- must not be answered with "run influx sync", which would not fix
    it and would swallow the real error.
    """

    from dashboard.server import analytics_schema_gap_payload

    unrelated = _Provider(missing=["ems_1m", "ems_5m"], needed="ems_raw")

    assert analytics_schema_gap_payload(unrelated, START, END) is None


def test_a_complete_schema_is_not_blamed_for_an_outage():
    from dashboard.server import analytics_schema_gap_payload

    assert (
        analytics_schema_gap_payload(_Provider(missing=[]), START, END) is None
    )


def test_a_provider_that_cannot_be_asked_reports_nothing():
    class Unreachable(_Provider):
        def missing_buckets(self, first=None):
            raise OSError("connection refused")

    from dashboard.server import analytics_schema_gap_payload

    assert analytics_schema_gap_payload(Unreachable(), START, END) is None


class _Client:
    """Counts lookups so the cache can be shown to bound them."""

    def __init__(self, present=("ems_raw",)):
        self.present = set(present)
        self.lookups = 0
        self.probed = []
        self.timeouts = []

    def find_bucket(self, name, timeout=None):
        self.lookups += 1
        self.probed.append(name)
        self.timeouts.append(timeout)
        return {"id": "1"} if name in self.present else None


def _provider(client):
    """Built through the constructor seam, so the real accessor is exercised.

    Patching the attribute instead would hide whether ``client`` is reached the
    way every other call site reaches it -- which is exactly where this feature
    was silently dead once.
    """

    from ems.history.influx_provider import InfluxHistoryProvider

    return InfluxHistoryProvider(
        {
            "enabled": True,
            "url": "http://influxdb:8086",
            "org": "ems",
            "token": "t",
            "bucket_prefix": "ems",
            "downsampling": [
                {"source": "raw", "target": "1m", "window": "1m"},
                {"source": "1m", "target": "5m", "window": "5m"},
                {"source": "5m", "target": "1h", "window": "1h"},
            ],
        },
        client=client,
    )


def test_the_provider_lists_the_buckets_its_config_expects():
    assert _provider(_Client()).missing_buckets() == ["ems_1m", "ems_5m", "ems_1h"]


def test_the_answer_is_held_so_a_poll_cannot_multiply_the_lookups():
    """One lookup per planned bucket, against an InfluxDB that may be timing
    out, on every failed request is how a request thread fills up."""

    client = _Client()
    provider = _provider(client)

    provider.missing_buckets()
    after_first = client.lookups
    provider.missing_buckets()

    assert after_first == 4
    assert client.lookups == after_first


def test_a_complete_schema_lists_nothing():
    client = _Client(present=("ems_raw", "ems_1m", "ems_5m", "ems_1h"))

    assert _provider(client).missing_buckets() == []


def test_a_probe_that_fails_is_not_remembered_as_a_complete_schema():
    """"Could not tell" and "every bucket exists" are different answers.

    Answering an unreadable schema with an empty list would serve a definite
    "nothing is missing" for the next minute and hide the hint in exactly the
    boot race this exists for. The hold pays for a bounded number of probes, so
    a hanging InfluxDB cannot be probed once per poll either.
    """

    import ems.history.influx_provider as provider_module

    class Hanging(_Client):
        def find_bucket(self, name, timeout=None):
            self.lookups += 1
            raise OSError("connection reset")

    client = Hanging()
    provider = _provider(client)

    for _ in range(20):
        assert provider.missing_buckets() is None

    assert client.lookups == provider_module.MISSING_BUCKETS_PROBE_ROUNDS


def test_a_bucket_outside_the_downsampling_plan_is_still_diagnosed():
    """A query profile may name a bucket no downsampling entry creates.

    Nothing cross-checks the two halves of the config, and that bucket is
    exactly the one that will never exist. Skipping it because the plan does not
    list it would leave its range permanently unexplained -- the empty chart
    this whole path replaces, for the one range that is actually broken.
    """

    client = _Client()
    provider = _provider(client)

    assert provider.missing_buckets("ems_10m") == [
        "ems_10m",
        "ems_1m",
        "ems_5m",
        "ems_1h",
    ]
    assert client.probed[0] == "ems_10m"


def test_the_bucket_the_caller_asks_about_is_looked_up_first():
    """Pipeline order puts the downsampled buckets last, and the budget can run
    out before them -- on the very ranges they are the answer for."""

    client = _Client()
    provider = _provider(client)

    provider.missing_buckets("ems_1h")

    assert client.probed[0] == "ems_1h"


def test_a_lookup_that_fails_does_not_discard_what_is_already_known():
    """A half-read schema that names the failing range is still the answer.

    Discarding it because a later, irrelevant lookup timed out hands the
    browser back the unexplained failure this whole path replaces.
    """

    class FailsAfterFirst(_Client):
        def find_bucket(self, name, timeout=None):
            self.lookups += 1
            self.probed.append(name)
            if self.lookups > 1:
                raise OSError("connection reset")
            return None

    provider = _provider(FailsAfterFirst())

    assert provider.missing_buckets("ems_1m") == ["ems_1m"]


def test_a_bucket_that_was_not_reached_is_not_reported_as_present():
    """A held answer covers the buckets it settled, and no others.

    The hold is shared by every range the dashboard asks about. One that got as
    far as the raw bucket must not later tell the 24h range that *its* bucket
    exists, because nobody ever looked -- the answer there is "could not tell",
    which falls back to the plain failure instead of a wrong all-clear.
    """

    import ems.history.influx_provider as provider_module

    class Stubborn(_Client):
        """Answers for the raw bucket and refuses every other lookup."""

        def find_bucket(self, name, timeout=None):
            self.lookups += 1
            self.probed.append(name)
            if name != "ems_raw":
                raise OSError("connection reset")
            return {"id": "1"}

    client = Stubborn()
    provider = _provider(client)

    assert provider.missing_buckets("ems_raw") == []
    assert provider.missing_buckets("ems_1m") is None

    for _ in range(20):
        provider.missing_buckets("ems_1m")

    # Bounded: twenty polls against an InfluxDB that refuses cost the hold's
    # allowance of probes, not one each.
    assert client.probed.count("ems_1m") <= provider_module.MISSING_BUCKETS_PROBE_ROUNDS


def test_a_range_whose_answer_is_known_does_not_pay_for_another_probe():
    """A probe round costs this request thread ten seconds.

    Paying that to fill in the rest of the list, for a payload whose one
    decisive bucket was settled on the first round, is the wrong trade on the
    hardware this exists for.
    """

    import ems.history.influx_provider as provider_module

    clock = {"now": 0.0}

    class Slow(_Client):
        def find_bucket(self, name, timeout=None):
            self.lookups += 1
            self.probed.append(name)
            clock["now"] += provider_module.MISSING_BUCKETS_PROBE_SECONDS
            return None

    client = Slow()
    provider = _provider(client)

    with patch.object(provider_module, "_now", lambda: clock["now"]):
        assert provider.missing_buckets("ems_1m") == ["ems_1m"]
        settled = clock["now"]

        assert provider.missing_buckets("ems_1m") == ["ems_1m"]

    assert clock["now"] == settled
    assert client.probed == ["ems_1m"]


def test_a_later_request_picks_up_what_the_budget_did_not_reach():
    """A truncated probe must not blind every other range for the whole hold.

    The bucket a range reads is probed first, so a probe cut short settles the
    one it was asked about and leaves the rest. Another range asking a moment
    later has to be able to get its own answer inside the same hold.
    """

    import ems.history.influx_provider as provider_module

    clock = {"now": 0.0}

    class Slow(_Client):
        def find_bucket(self, name, timeout=None):
            self.lookups += 1
            self.probed.append(name)
            clock["now"] += provider_module.MISSING_BUCKETS_PROBE_SECONDS
            return None

    client = Slow()
    provider = _provider(client)

    with patch.object(provider_module, "_now", lambda: clock["now"]):
        assert provider.missing_buckets("ems_raw") == ["ems_raw"]
        assert provider.missing_buckets("ems_1h") == ["ems_raw", "ems_1h"]

    assert client.probed == ["ems_raw", "ems_1h"]


def test_two_requests_arriving_together_cost_one_probe():
    """The dashboard runs on a threading HTTP server, so this is the normal case.

    Interleaving: A claims the hold and enters the first lookup; B arrives while
    A is still inside it. B must neither start a second probe nor queue behind
    a ten-second one on a request thread -- it says "could not tell" until A's
    answer lands, which is a momentary fallback, not a wrong answer.
    """

    entered = threading.Event()
    release = threading.Event()

    class Blocking(_Client):
        def find_bucket(self, name, timeout=None):
            entered.set()
            assert release.wait(timeout=5)
            return super().find_bucket(name, timeout=timeout)

    client = Blocking()
    provider = _provider(client)
    answer = {}

    first = threading.Thread(
        target=lambda: answer.update(value=provider.missing_buckets("ems_1m"))
    )
    first.start()
    try:
        assert entered.wait(timeout=5)
        before = client.lookups

        assert provider.missing_buckets("ems_1m") is None
        assert client.lookups == before
    finally:
        release.set()
        first.join(timeout=5)

    assert answer["value"] == ["ems_1m", "ems_5m", "ems_1h"]
    assert client.lookups == 4


def test_a_probe_gives_up_before_it_holds_the_request_thread():
    """It runs after a query that already failed, on a request thread, and the
    dashboard polls faster than four client timeouts take."""

    import ems.history.influx_provider as provider_module

    clock = {"now": 0.0}

    class Slow(_Client):
        def find_bucket(self, name, timeout=None):
            self.lookups += 1
            self.probed.append(name)
            clock["now"] += provider_module.MISSING_BUCKETS_PROBE_SECONDS
            return None

    client = Slow()
    provider = _provider(client)

    with patch.object(provider_module, "_now", lambda: clock["now"]):
        assert provider.missing_buckets("ems_1m") == ["ems_1m"]

    assert client.lookups == 1


def test_the_probe_is_as_tolerant_as_the_rest_of_the_project():
    """The same lookups against the same InfluxDB on the same board.

    Halving what is left each round makes every lookup after the first stricter
    than the one before it, so the tail buckets time out on exactly the hardware
    the budgets elsewhere are sized for.
    """

    import ems.history.influx_provider as provider_module
    from ems.influx_setup import INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS

    client = _Client()
    provider = _provider(client)

    with patch.object(provider_module, "_now", lambda: 0.0):
        provider.missing_buckets("ems_1m")

    assert client.timeouts[0] == (
        min(
            INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS,
            provider_module.MISSING_BUCKETS_PROBE_SECONDS / 2,
        ),
    ) * 2


def test_each_lookup_is_given_the_time_that_is_left():
    """A budget the request never sees bounds nothing."""

    import ems.history.influx_provider as provider_module

    client = _Client()
    _provider(client).missing_buckets()

    # Split between connect and read, because a scalar would be spent twice.
    assert all(isinstance(t, tuple) and len(t) == 2 for t in client.timeouts)
    budgets = [sum(t) for t in client.timeouts]
    assert budgets[0] <= provider_module.MISSING_BUCKETS_PROBE_SECONDS
    # Never more than what is left, so the sequence cannot outrun its deadline.
    assert budgets == sorted(budgets, reverse=True)
