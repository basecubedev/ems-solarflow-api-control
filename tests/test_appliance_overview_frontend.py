# SPDX-License-Identifier: AGPL-3.0-or-later
"""The overview answers three questions before it shows a single fact.

It used to open with six tiles of raw readings and end with a bullet list whose
first word was a machine code. Whether an entry was serious was decided in the
browser by a regular expression over that code -- a second severity judgement
next to the one the health check had already made.

Now the page says what this appliance is (one verdict), what needs doing (the
findings, worst first, each with a way to get there), and only then what the
box currently reads. The severity comes from the backend; this layer maps it
onto a sentence and never re-derives it.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.appliance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "appliance_overview_runner.js")


def _render(
    status, view="overview", durations=(), sizes=(), percentages=(), plan=None, expert=False
):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps(
            {
                "status": status,
                "view": view,
                "durations": list(durations),
                "sizes": list(sizes),
                "percentages": list(percentages),
                "plan": plan,
                "expert": expert,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _finding(code="admin_unhealthy", severity="error", section="admin", **overrides):
    payload = {
        "code": code,
        "severity": severity,
        "section": section,
        "title": "EMS Admin is not answering",
        "message": "the EMS Admin container is not healthy",
        "next_step": "Open Admin and restart it.",
    }
    payload.update(overrides)
    return payload


def _status(level="healthy", findings=(), **overrides):
    payload = {"health": {"level": level, "warnings": list(findings)}}
    payload.update(overrides)
    return payload


# --- the verdict -----------------------------------------------------------


def test_a_healthy_appliance_is_told_so_in_one_sentence():
    view = _render(_status())["verdict"]
    assert view["tone"] == "ok"
    assert view["text"].endswith(".")
    assert "healthy" in view["text"].lower()


def test_a_degraded_appliance_leads_with_the_bad_news():
    view = _render(_status("degraded", [_finding()]))["verdict"]
    assert view["tone"] == "bad"


def test_an_appliance_that_needs_attention_is_not_reported_as_broken():
    view = _render(_status("attention", [_finding(severity="warning")]))["verdict"]
    assert view["tone"] == "warn"


def test_a_status_that_could_not_be_read_is_never_reported_as_healthy():
    """The old page printed the tiles anyway, every value an em dash, and said
    underneath them that the appliance looked healthy.

    A status call that failed is itself the finding, so the page reports an
    appliance nobody could ask rather than an empty one.
    """

    view = _render({"error": "agent_unavailable"})
    assert view["verdict"]["tone"] == "bad"
    assert "could not be read" in view["verdict"]["text"].lower()
    assert "healthy" not in view["verdict"]["text"].lower()
    assert view["findings"]["hidden"] is False
    assert view["findings"]["findings"][0]["severity"] == "error"
    assert "agent_unavailable" in view["findings"]["findings"][0]["message"]


def test_the_verdict_is_spoken_only_when_it_changes():
    """The verdict line is rebuilt by the two-second poll.

    Marking that node as a live region would have it read out again on every
    rebuild -- the same sentence, every two seconds. The shell's one live region
    speaks instead, and only when the sentence itself is new.
    """

    spoken = _render(_status())["announcement"]
    assert spoken["first"] is None, "arriving on the page is not a change"
    assert spoken["unchanged"] is None
    assert spoken["changed"] == "Something on this appliance is not working."


# --- the findings ----------------------------------------------------------


def test_findings_are_ranked_worst_first():
    view = _render(
        _status(
            "degraded",
            [
                _finding(code="security_updates_pending", severity="warning", section="updates"),
                _finding(code="admin_unhealthy", severity="error", section="admin"),
                _finding(code="reboot_required", severity="info", section="overview"),
            ],
        )
    )
    assert [item["severity"] for item in view["findings"]["findings"]] == [
        "error",
        "warning",
        "info",
    ]


def test_the_severity_is_the_one_the_backend_sent():
    """app.js used to re-derive it with /unhealthy|not_running|.../ over the code.

    A backend that downgrades a finding has to be able to say so, and a code it
    has never seen must not be silently ranked by its spelling.
    """

    view = _render(_status("attention", [_finding(code="admin_unhealthy", severity="info")]))
    assert view["findings"]["findings"][0]["severity"] == "info"
    assert view["findings"]["status"] == "info"


def test_the_headline_counts_what_is_waiting():
    one = _render(_status("attention", [_finding(severity="warning")]))
    two = _render(_status("attention", [_finding(severity="warning"), _finding()]))
    assert "1 thing" in one["findings"]["headline"]
    assert "2 things" in two["findings"]["headline"]


def test_a_clean_appliance_hides_the_findings_panel():
    view = _render(_status())["findings"]
    assert view["hidden"] is True
    assert view["findings"] == []


def test_no_finding_is_shown_as_its_code():
    view = _render(_status("degraded", [_finding()]))["findings"]["findings"][0]
    assert view["title"] == "EMS Admin is not answering"
    assert "admin_unhealthy" not in json.dumps(
        {"title": view["title"], "message": view["message"], "next_step": view["next_step"]}
    )


# --- getting there ---------------------------------------------------------


def test_a_finding_offers_the_page_that_can_act_on_it():
    action = _render(_status("degraded", [_finding(section="admin")]))["actions"][0]
    assert action["view"] == "admin"
    assert action["label"] == "Open Admin"


def test_a_finding_does_not_offer_the_page_it_is_already_on():
    """An "Open Overview" button on the overview is a button that does nothing."""

    action = _render(
        _status("attention", [_finding(code="reboot_required", section="overview")]),
        view="overview",
    )["actions"][0]
    assert action is None


def test_an_unknown_destination_is_dropped_rather_than_guessed():
    action = _render(_status("degraded", [_finding(section="does-not-exist")]))["actions"][0]
    assert action is None


def test_the_navigation_marks_the_sections_that_need_attention():
    view = _render(
        _status(
            "degraded",
            [
                _finding(section="admin"),
                _finding(section="updates", severity="warning"),
                _finding(section="updates", severity="warning"),
            ],
        )
    )
    assert view["attention"] == {"admin": "error", "updates": "warning"}


def test_a_session_limit_is_shown_in_the_unit_a_person_thinks_in():
    """The configuration file holds seconds; "1800 s" is a number to convert
    before it answers "how long until I am signed out".

    Only a unit the value divides into exactly is used. Rounding 5400 seconds
    to "2 hours" would hide half an hour of a session that ends at 90 minutes,
    and a limit is the one number nobody should have to distrust.
    """

    seconds = [1, 45, 90, 1800, 5400, 43200, 172800, 0, None, 1.5]
    assert _render(_status(), durations=seconds)["durations"] == [
        "1 second",
        "45 seconds",
        "90 seconds",
        "30 minutes",
        "90 minutes",
        "12 hours",
        "2 days",
        None,
        None,
        None,
    ]


def test_a_size_reads_the_same_on_every_page():
    """The overview divided by 1024 inline and Diagnostics printed raw
    megabytes, so one filesystem was 149 GB on one page and 152473 MB on the
    next."""

    assert _render(_status(), sizes=[152473, 5859, 512, 0, None])["sizes"] == [
        "149 GB",
        "5.7 GB",
        "0.5 GB",
        None,
        None,
    ]


def test_a_percentage_says_what_it_is_a_percentage_of():
    """ "27.8 %" on a storage card answers neither how full nor how free."""

    assert _render(_status(), percentages=[27.8, 0, None])["percentages"] == [
        "27.8 % used",
        "0 % used",
        "—",
    ]


# --- the confirmation dialog -----------------------------------------------


def _plan(**overrides):
    payload = {
        "type": "admin.install",
        "bootstrap": False,
        "repository": "ghcr.io/example/ems-solarflow-admin",
        "target_tag": "v1.1.0",
        "target_channel": "stable",
        "target_digest": "sha256:" + "c" * 64,
        "target_reference": "ghcr.io/example/admin@sha256:" + "c" * 64,
        "target_revision": "",
        "target_source": "",
        "target_architecture": "",
        "legacy_labels_accepted": "",
        "reinstall": False,
        "current_version": "v1.0.0",
        "current_digest": "sha256:" + "d" * 64,
    }
    payload.update(overrides)
    return payload


def _fields(plan, expert=False):
    return _render(_status(), plan=plan, expert=expert)["plan"]


def test_a_plan_field_with_no_value_is_not_a_row():
    """The dialog listed every scalar the plan carried, empty ones included.

    "target revision —", "target source —", "legacy labels accepted —": four of
    the thirteen rows in an install plan said nothing at all, in the one place
    an operator is asked to read carefully before agreeing to a change.
    """

    labels = [field["label"] for field in _fields(_plan(), expert=True)]
    assert "target revision" not in labels
    assert "Source revision" not in labels
    assert all(field["value"] not in ("", "—") for field in _fields(_plan(), expert=True))


def test_a_plan_names_its_fields_in_words():
    fields = {field["key"]: field["label"] for field in _fields(_plan(), expert=True)}
    assert fields["target_tag"] == "Version to install"
    assert fields["current_version"] == "Installed now"
    assert fields["target_reference"] == "Exact image"
    assert "_" not in " ".join(fields.values())


def test_a_plan_leads_with_what_happens_not_with_image_identity():
    keys = [field["key"] for field in _fields(_plan(), expert=True)]
    assert keys.index("target_tag") < keys.index("target_digest")
    assert keys.index("current_version") < keys.index("current_digest")


def test_the_plan_fingerprint_is_named_and_kept_with_the_other_machine_detail():
    """It seals the plan so a confirmation can only apply what was shown.

    That makes it a mechanism rather than a statement about this appliance, so
    it belongs where the digests are -- but it must still be reachable, because
    it is what binds the button to the page.
    """

    assert "authority" not in [field["key"] for field in _fields(_plan(authority="a" * 64))]
    expert = {
        field["key"]: field["label"] for field in _fields(_plan(authority="a" * 64), expert=True)
    }
    assert expert["authority"] == "Plan fingerprint"


def test_image_identity_stays_an_expert_detail():
    """Unchanged from before: Basic mode shows the version, not the digest."""

    basic = [field["key"] for field in _fields(_plan())]
    assert "target_digest" not in basic
    assert "target_reference" not in basic
    assert "target_tag" in basic


def test_a_plan_size_is_shown_in_gigabytes_like_everywhere_else():
    fields = {field["key"]: field["value"] for field in _fields(_plan(free_megabytes=152473))}
    assert fields["free_megabytes"] == "149 GB"


def test_a_plan_still_shows_a_field_this_build_has_no_label_for():
    """A new backend field must appear, badly named, rather than vanish."""

    fields = {field["key"]: field["label"] for field in _fields(_plan(surprise_field="yes"))}
    assert fields["surprise_field"] == "surprise field"
