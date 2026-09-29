# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Updates page of the Appliance Manager is allowed to decide.

Style family: Control / Energy stage for the summary and the version choices,
Aggregate / Device for the Manager's status cards; both reuse the existing
action-card, status-card and tone classes.

The page groups versions and offers the newest stable one as a button. Which
track a version is on, and which one is the newest stable, are the backend's
answers; the page only sorts what it was given into the order Admin uses.
"""

import json
import re
import shutil
import subprocess

import pytest

from tests.test_appliance_manager_frontend import APP, APP_JS, evaluate, extract

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

TRACKS = "var RELEASE_TRACKS = " + APP.split("var RELEASE_TRACKS = ", 1)[1].split("];", 1)[0] + "];\n"


def test_updates_is_the_second_section_so_it_is_found_without_searching():
    views = re.findall(r'\{ id: "([a-z]+)", label: "([^"]+)"', APP.split("var VIEWS = [", 1)[1])

    assert views[0] == ("overview", "Overview")
    assert views[1] == ("updates", "Updates")


def test_the_tracks_use_the_admin_consoles_words_in_its_order():
    labels = re.findall(r'label: "([A-Za-z]+)", help:', TRACKS)

    assert labels == ["Stable", "Unstable", "Experimental"]


@requires_node
def test_versions_are_grouped_in_track_order_and_empty_groups_are_left_out():
    groups = evaluate(
        "groupByTrack",
        [
            {"tag": "v1.2.0-dev.1", "track": "experimental"},
            {"tag": "v1.1.0", "track": "stable"},
            {"tag": "v1.0.0", "track": "stable"},
        ],
        preamble=TRACKS,
    )

    assert [group["track"]["id"] for group in groups] == ["stable", "experimental"]
    assert [item["tag"] for item in groups[0]["items"]] == ["v1.1.0", "v1.0.0"]


@requires_node
def test_a_version_without_a_track_is_never_shown_as_stable():
    groups = evaluate("groupByTrack", [{"tag": "v9.9.9"}], preamble=TRACKS)

    assert [group["track"]["id"] for group in groups] == ["experimental"]


@requires_node
@pytest.mark.parametrize(
    "direction,verdict",
    [
        ("upgrade", ["warn", "update available"]),
        ("unknown", ["idle", "installed version cannot be compared"]),
        ("reinstall", ["ok", "up to date"]),
        ("downgrade", ["idle", "newer than the latest stable"]),
        (None, ["idle", "installed version cannot be compared"]),
        ("revert", ["idle", "installed version cannot be compared"]),
    ],
)
def test_the_summary_verdict_is_the_backends_direction(direction, verdict):
    assert evaluate("latestVerdict", {"direction": direction}) == verdict


def test_only_an_upgrade_gets_a_one_click_button():
    """An uncomparable install may be the running image again, which the agent
    refuses without a reinstall flag the summary does not have."""

    body = extract("latestButton")

    assert 'if (latest.direction !== "upgrade") return null;' in body


def test_the_manager_button_waits_for_a_readable_manager_state():
    body = extract("managerSummaryCard")

    assert "var managerKnown = !!state.data.manager && !manager.error;" in body
    assert "!managerKnown || !managerActions(manager, applianceNow()).canUpdate" in body


def test_the_manager_plan_is_titled_with_the_release_id_and_not_the_index_claim():
    assert "entry.release_id" in extract("planManagerInstall")
    assert "managerLabel" not in extract("planManagerInstall")


def test_the_one_click_updates_install_the_entry_the_backend_named():
    """A channel would be resolved again at planning time and could name a
    release published after the button was drawn."""

    assert 'planAdminInstall("exact", latest.tag, false)' in extract("adminSummaryCard")
    assert "planManagerInstall(latest)" in extract("managerSummaryCard")
    assert "latest_stable" in extract("managerSummaryCard")
    assert "if (channel === \"exact\") body.tag = tag;" in extract("planAdminInstall")


def test_every_install_from_this_page_goes_through_a_plan():
    for name in ("planAdminInstall", "planSecurityUpdates", "planManagerInstall"):
        assert "planOperation(" in extract(name), name
    assert "planSecurityUpdates" in extract("systemSummaryCard")
    assert "planAdminInstall(" in extract("renderInstallForm")
    assert APP.count('scope: "security"') == 1, "the security plan is built in one place"


def _system_card(updates, warnings):
    preamble = "".join(
        "var " + name + " = " + APP.split("var " + name + " = ", 1)[1].split(";", 1)[0] + ";\n"
        for name in ("FINDING_SEVERITY_ORDER",)
    ) + "".join(
        extract(name) + "\n"
        for name in (
            "rankSeverity", "rankedFindings", "format", "updatesFindings", "updatesInDoubt",
            "securityUpdatesOffered",
        )
    ) + (
        "var state = { data: { status: "
        + json.dumps({"health": {"warnings": warnings}, "updates": updates}) + " } };\n"
        "function el() { return {}; }\n"
        "function fact() { return {}; }\n"
        "function planSecurityUpdates() {}\n"
        "function summaryCard(title, subtitle, facts, verdict, action) {"
        " return { verdict: verdict, action: !!action }; }\n"
    )
    return evaluate("systemSummaryCard", updates, preamble=preamble)


@requires_node
def test_os_counts_that_are_not_an_answer_are_not_called_up_to_date():
    warnings = [{
        "code": "update_check_failed", "severity": "warning", "section": "updates",
        "title": "The update check did not finish",
    }]

    card = _system_card(
        {"security_count": 0, "normal_count": 0, "error": "update_check_failed"}, warnings
    )

    assert card["verdict"] == ["warn", "The update check did not finish"]


@requires_node
def test_counts_the_backend_doubts_are_not_offered_for_install():
    warnings = [{
        "code": "update_check_failed", "severity": "warning", "section": "updates",
        "title": "The update check did not finish",
    }]

    card = _system_card(
        {"security_count": 3, "normal_count": 0, "error": "update_check_failed"}, warnings
    )

    assert card["action"] is False


@requires_node
def test_a_stale_index_still_offers_the_security_updates_it_lists():
    """An appliance nobody refreshes is the common case; its security updates
    are real, and the overview offers them too."""

    warnings = [{
        "code": "package_index_stale", "severity": "warning", "section": "updates",
        "title": "The package index is out of date",
    }]

    pending = _system_card({"security_count": 3, "normal_count": 0}, warnings)
    quiet = _system_card({"security_count": 0, "normal_count": 0}, warnings)

    assert pending == {"verdict": ["warn", "security updates waiting"], "action": True}
    assert quiet == {"verdict": ["warn", "The package index is out of date"], "action": False}


@requires_node
def test_a_package_manager_that_refuses_installs_is_not_offered_one():
    warnings = [{
        "code": "package_manager_unhealthy", "severity": "error", "section": "updates",
        "title": "The package manager needs recovery",
    }]
    updates = {"security_count": 2, "normal_count": 0, "package_manager": {"healthy": False}}

    card = _system_card(updates, warnings)

    assert card["verdict"] == ["bad", "The package manager needs recovery"]
    assert card["action"] is False


@requires_node
def test_pending_security_updates_alone_are_offered_in_one_click():
    warnings = [{
        "code": "security_updates_pending", "severity": "warning", "section": "updates",
        "title": "Security updates are waiting",
    }]

    card = _system_card({"security_count": 2, "normal_count": 0}, warnings)

    assert card["verdict"] == ["warn", "security updates waiting"]
    assert card["action"] is True


def test_the_version_choice_lives_on_the_updates_page_and_not_on_the_admin_page():
    assert 'renderInstallForm(state.data.releases, { blocked: gate === "live" })' in extract(
        "renderAdminVersions"
    )
    admin = extract("renderAdmin")
    assert "renderInstallForm(" not in admin
    assert 'viewButton("updates"' in admin


def test_a_live_admin_replacement_blocks_the_version_choice_with_a_reason():
    versions = extract("renderAdminVersions")

    assert 'if (adminTransitionLive()) return "live";' in extract("adminGate")
    assert 'adminTransitionNotice("admin-versions-transition-live")' in versions
    assert 'blocked: gate === "live"' in versions
    assert "disabled: opts.blocked === true" in extract("renderInstallForm")
    assert 'gate === "live"' in extract("adminSummaryCard")


def test_an_open_disclosure_and_a_picked_version_survive_the_poll():
    assert "state.disclosures[key]" in extract("disclosure")
    assert "state.choices[key]" in extract("rememberChoice")
    assert "rememberChoice(select" in extract("renderManagerSources")
    assert "rememberChoice(channelSelect" in extract("renderInstallForm")
    form = extract("renderInstallForm")
    assert 'choiceKey + "-tag", "value"' in form
    assert 'choiceKey + "-reinstall", "checked"' in form


def test_no_dynamic_value_reaches_innerhtml():
    for name in (
        "renderUpdates",
        "managerSummaryCard",
        "adminSummaryCard",
        "systemSummaryCard",
        "renderAdminVersions",
        "renderManagerSources",
        "latestButton",
        "rememberInput",
        "trackHelp",
        "disclosure",
    ):
        assert "innerHTML" not in extract(name), name


def test_a_failed_index_read_is_not_reported_as_an_unconfigured_one():
    """A failed call carries an error and no configured flag; checking the flag
    first sent the operator to set a manager_index_url that was already set."""

    body = extract("renderManagerSources")

    assert body.index("if (sources.error)") < body.index("if (!sources.configured)")
    summary = extract("managerSummaryCard")
    assert summary.index("sources.error") < summary.index("!sources.configured")


def test_a_missing_admin_container_is_sent_to_repair_not_called_uncomparable():
    body = extract("adminSummaryCard")

    assert 'if (!admin.installed) return "missing";' in extract("adminGate")
    assert body.index('gate === "missing"') < body.index("latestVerdict(latest)")


@requires_node
def test_the_running_version_keeps_its_marker_when_it_is_also_refused():
    releases = {"available": [{
        "tag": "v1.2.0-rc1", "track": "unstable", "direction": "reinstall",
        "installable": False, "reason": "candidates are not enabled",
    }]}
    script = (
        TRACKS
        + extract("groupByTrack") + "\n"
        + extract("installOption") + "\n"
        + extract("appendReleaseGroups") + "\n"
        + "var made = [];\n"
        + "function el(tag, attrs) { if (tag === 'option') made.push(attrs.text);"
        + " return { appendChild: function () {} }; }\n"
        + "appendReleaseGroups({ appendChild: function () {} }, " + json.dumps(releases) + ");\n"
        + "console.log(JSON.stringify(made));\n"
    )
    result = subprocess.run(
        [shutil.which("node"), "-"], input=script, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["v1.2.0-rc1 \u00b7 installed \u2014 candidates are not enabled"]


def _admin_card(status, releases):
    preamble = (
        "".join(extract(name) + "\n" for name in (
            "adminVersionText", "adminTransitionLive", "latestVerdict", "latestButton", "format",
            "viewButton", "dockerDaemonState", "dockerCondition", "dockerStopped", "dockerStateText",
            "adminGate",
        ))
        + "var state = { data: { status: " + json.dumps(status)
        + ", releases: " + json.dumps(releases) + " } };\n"
        + "function el(tag, attrs) { return { test: (attrs || {})['data-test'] }; }\n"
        + "function fact() { return {}; }\n"
        + "function selectView() {}\nfunction planAdminInstall() {}\n"
        + "function summaryCard(title, subtitle, facts, verdict, action) {"
        + " return { verdict: verdict, action: action ? action.test : null }; }\n"
    )
    return _run_admin_card(preamble)


def _run_admin_card(preamble):
    script = preamble + extract("adminSummaryCard") + "\nconsole.log(JSON.stringify(adminSummaryCard()));\n"
    result = subprocess.run(
        [shutil.which("node"), "-"], input=script, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


LATEST = {"available": [], "latest_stable": {"tag": "v1.1.0", "direction": "upgrade"}}


@requires_node
@pytest.mark.parametrize("status", [{"error": "agent_unavailable"}, {"admin": {"status": "unavailable"}}])
def test_an_unread_admin_state_is_not_reported_as_a_missing_container(status):
    card = _admin_card(status, LATEST)

    assert card == {"verdict": ["idle", "Admin state unavailable"], "action": None}


RUNNING = {"daemon": {"state": "running"}}


@requires_node
def test_a_stopped_docker_is_named_and_not_called_a_missing_container():
    card = _admin_card(
        {"docker": {"daemon": {"state": "stopped"}}, "admin": {"installed": False}}, LATEST
    )

    assert card == {"verdict": ["bad", "Docker is not running"], "action": None}


@requires_node
def test_a_read_admin_without_its_container_is_sent_to_repair():
    card = _admin_card(
        {"docker": RUNNING, "admin": {"installed": False, "bootstrap_required": False}}, LATEST
    )

    assert card == {"verdict": ["bad", "container missing"], "action": "update-admin-repair"}


@requires_node
def test_a_running_admin_behind_the_latest_stable_is_offered_the_update():
    card = _admin_card({"docker": RUNNING, "admin": {"installed": True, "version": "v1.0.0"}}, LATEST)

    assert card == {"verdict": ["warn", "update available"], "action": "update-admin-latest"}


def test_the_admin_version_choice_is_withheld_while_its_state_is_unread():
    body = extract("renderAdminVersions")

    assert "unread:" in body.split("}[gate];", 1)[0]
    assert 'if (admin.installed === undefined) return "unread";' in extract("adminGate")


def test_a_disclosure_can_keep_keyboard_focus_through_the_poll():
    """focusAnchor re-finds the focused element by its test id; the summary is
    what takes focus, so it is the one that needs one."""

    assert '"data-test": key + "-summary"' in extract("disclosure")


def test_a_reinstall_tick_is_spent_by_any_settled_operation_but_a_picked_version_is_not():
    body = extract("invalidateDerivedViews")

    assert "/-reinstall$/.test(choice)" in body
    assert "state.choices = {};" not in body


def test_an_install_of_unknown_direction_is_confirmed_as_a_caution():
    assert 'entry.direction === "unknown"' in extract("planManagerInstall")


@requires_node
def test_an_unread_admin_state_is_not_called_not_installed():
    preamble = "".join(extract(n) + "\n" for n in (
        "dockerDaemonState", "dockerCondition", "dockerStopped"
    )) + "var state = { data: { status: {} } };\n"

    assert evaluate("adminVersionText", {}, preamble=preamble) == "unknown"
    assert evaluate("adminVersionText", {"installed": False}, preamble=preamble) == "not installed"
    assert evaluate(
        "adminVersionText", {"installed": True, "version": "v1.0.0"}, preamble=preamble
    ) == "v1.0.0"


def test_a_missing_container_still_reaches_the_version_list_after_the_repair_hint():
    """With the container gone, installing a version is the way back when
    repair cannot pull the recorded image; the recovery checklist relies on it."""

    body = extract("renderAdminVersions")
    withheld = body.split("var withheld = {", 1)[1].split("}[gate];", 1)[0]

    assert "missing:" not in withheld and "live:" not in withheld
    assert body.index('if (gate === "missing")') < body.index("renderInstallForm(")


def test_a_version_change_nobody_operated_drops_the_listings_that_compared_against_it():
    """Admin replacing itself and the Manager's deadline reverting a package
    change the installed version without an operation settling here."""

    assert "dropOutdatedListings();" in extract("readHostState")
    reconcile = extract("reconcileListing")
    assert "if (!now) return;" in reconcile
    assert "if (before && before !== now) clearChoices(prefix);" in reconcile
    assert "state.readAgainst[key] !== now" in reconcile
    sources = extract("loadUpdateSources")
    assert 'state.readAgainst.releases = installedVersion("releases");' in sources
    assert 'state.readAgainst.managerSources = installedVersion("managerSources");' in sources
    drop = extract("dropListing")
    assert "viewGeneration" not in drop, "the global generation strands every other read"
    assert "listing !== listingGeneration(key)" in extract("loadInto")

def test_navigation_buttons_are_built_in_one_place():
    assert APP.count('onclick: function () { selectView("') == 0


def test_the_admin_listing_is_read_only_for_an_admin_that_is_there():
    body = extract("loadUpdateSources")

    assert '["missing", "live", "ready"].indexOf(adminGate())' in body


def test_an_uncomparable_manager_option_says_so():
    assert 'unknown: "cannot be compared"' in APP.split("var MANAGER_DIRECTIONS = ", 1)[1].split("};", 1)[0]


@requires_node
def test_an_unread_docker_state_does_not_withhold_the_admin_versions():
    card = _admin_card({"admin": {"installed": True, "version": "v1.0.0"}}, LATEST)

    assert card == {"verdict": ["warn", "update available"], "action": "update-admin-latest"}


@requires_node
def test_docker_that_is_not_installed_is_called_that():
    card = _admin_card(
        {"docker": {"daemon": {"state": "unavailable"}}, "admin": {"installed": False}}, LATEST
    )

    assert card["verdict"] == ["bad", "Docker is not installed"]


def test_the_summary_and_the_version_list_read_the_admin_state_in_one_place():
    assert "adminGate()" in extract("adminSummaryCard")
    assert "adminGate()" in extract("renderAdminVersions")
    assert 'adminTransitionNotice("admin-transition-live")' in extract("renderAdmin")


def test_the_disclosure_arrow_is_drawn_where_webkit_draws_it_too():
    css = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")

    assert ".disclosure > summary::-webkit-details-marker { display: none; }" in css
    assert ".disclosure > summary::before" in css
    assert "summary::marker" not in css


def test_the_track_explanations_are_the_admin_consoles_words():
    """A copy across two deployables is one source only with a test comparing
    them (agent-rules §2); the appliance cannot link the Admin page."""

    admin = (APP_JS.parents[2] / "admin" / "static" / "index.html").read_text(encoding="utf-8")
    help_block = admin.split('class="system-build-channel-help"', 1)[1].split("</dl>", 1)[0]
    pairs = re.findall(r"<dt>([^<]+)</dt>\s*<dd>\s*(.*?)\s*</dd>", help_block, re.S)
    admin_words = {term: " ".join(text.split()) for term, text in pairs}
    ours = dict(re.findall(r'label: "([A-Za-z]+)", help: "([^"]+)"', TRACKS))

    assert ours == {term: admin_words[term] for term in ours}


def test_the_overview_tile_and_the_updates_gate_read_docker_in_one_place():
    assert "dockerCondition(" in extract("dockerTone")
    assert "dockerCondition(" in extract("dockerStopped")
    assert "dockerCondition(" in extract("dockerStateText")


def test_every_security_update_button_follows_one_rule():
    quick = extract("quickActions")
    assert "disabled: !securityUpdatesOffered()," in quick
    assert '"data-test": "quick-security-reason"' in quick
    assert "disabled: !securityUpdatesOffered()," not in extract("renderPackageUpdates"), (
        "the section's plan names the blockers; greying it out hides them"
    )
    assert "securityUpdatesOffered()" in extract("systemSummaryCard")
    offered = extract("securityUpdatesOffered")
    assert "Number(updates.security_count) > 0 && !updatesInDoubt(updates)" in offered
    doubt = extract("updatesInDoubt")
    assert 'updates.error === "update_check_failed"' in doubt
    assert "(updates.package_manager || {}).healthy === false" in doubt


def test_the_manager_index_is_read_only_once_the_manager_state_is():
    body = extract("loadUpdateSources")

    assert "sources === undefined && manager && !manager.error" in body
    assert '"manager state unavailable"' in extract("managerSummaryCard")


def _reconcile(data, steps, choices=None, against=None):
    """Run dropOutdatedListings over successive host reads and report what is left."""

    script = (
        "".join(extract(name) + "\n" for name in (
            "listingGeneration", "dropListing", "clearChoices", "reconcileListing",
            "installedVersion", "dropOutdatedListings",
        ))
        + "var state = { listingGenerations: {}, seenVersions: {}, readAgainst: "
        + json.dumps(against or {}) + ", choices: " + json.dumps(choices or {})
        + ", data: " + json.dumps(data) + " };\n"
        + "var dropped = [];\n"
        + "var steps = " + json.dumps(steps) + ";\n"
        + "steps.forEach(function (step) {\n"
        + "  state.data.status = step.status; state.data.manager = step.manager;\n"
        + "  Object.keys(step.listings || {}).forEach(function (k) {\n"
        + "    state.data[k] = step.listings[k].listing; state.readAgainst[k] = step.listings[k].against;\n"
        + "  });\n"
        + "  var before = Object.keys(state.data).filter(function (k) { return k === 'releases' || k === 'managerSources'; });\n"
        + "  dropOutdatedListings();\n"
        + "  before.forEach(function (k) { if (!(k in state.data)) dropped.push(k); });\n"
        + "});\n"
        + "console.log(JSON.stringify({ dropped: dropped, choices: state.choices }));\n"
    )
    result = subprocess.run(
        [shutil.which("node"), "-"], input=script, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _read(admin, manager):
    return {"status": {"admin": {"installed": True, "version": admin}}, "manager": {"installed_version": manager}}


def _loaded(against):
    return {"listing": {"available": []}, "against": against}


@requires_node
def test_a_listing_is_dropped_by_the_version_it_was_requested_against():
    data = {"releases": {"available": []}, "managerSources": {"releases": []}}
    result = _reconcile(data, [_read("v1.0.0", "0.3.8")],
                        against={"releases": "v1.0.0", "managerSources": "0.3.9"})

    assert result["dropped"] == ["managerSources"]


@requires_node
def test_a_listing_requested_against_no_version_is_re_read_once_one_is_known():
    """An inspect that failed while the listing was requested left every
    direction unknown; the first real version re-reads it, and the re-read
    records that version, so a flapping read does not re-read it again."""

    steps = [
        {**_read("v0.8.11", "0.3.8"), "listings": {"releases": _loaded("")}},
        {**_read("v0.8.11", "0.3.8"), "listings": {"releases": _loaded("v0.8.11")}},
        _read("", "0.3.8"),
        _read("v0.8.11", "0.3.8"),
    ]

    assert _reconcile({}, steps)["dropped"] == ["releases"]


@requires_node
def test_a_version_that_comes_back_is_reconciled_again():
    """A to B outside this console, back to A by an install here, then to B
    again: each move drops the listing requested against the other version."""

    steps = [
        {**_read("B", "0.3.8"), "listings": {"releases": _loaded("A")}},
        {**_read("A", "0.3.8"), "listings": {"releases": _loaded("A")}},
        _read("B", "0.3.8"),
    ]

    assert _reconcile({}, steps)["dropped"] == ["releases", "releases"]


@requires_node
def test_choices_go_when_the_installed_version_moves_and_survive_an_empty_read():
    choices = {"admin-version": "v0.8.10", "admin-version-tag": "v0.8.10", "manager-version": "x"}
    steps = [_read("v0.8.9", "0.3.8"), _read("", "0.3.8"), _read("v0.8.9", "0.3.8")]

    kept = _reconcile({}, steps, choices=choices)
    moved = _reconcile({}, [_read("v0.8.9", "0.3.8"), _read("v0.8.10", "0.3.8")], choices=choices)

    assert kept["choices"] == choices
    assert moved["choices"] == {"manager-version": "x"}


def test_a_host_without_docker_is_not_promised_a_repair_that_starts_it():
    body = extract("renderAdminVersions")

    assert '"No Admin version can be installed on a host without Docker."' in body


@requires_node
def test_a_fresh_host_whose_docker_is_down_is_told_about_docker_first():
    card = _admin_card(
        {"docker": {"daemon": {"state": "stopped"}}, "admin": {"installed": False, "bootstrap_required": True}},
        LATEST,
    )

    assert card["verdict"] == ["bad", "Docker is not running"]


@requires_node
def test_a_stopped_docker_does_not_make_the_admin_read_as_not_installed():
    card = _admin_card(
        {"docker": {"daemon": {"state": "stopped"}}, "admin": {"installed": False}}, LATEST
    )

    assert card["verdict"] == ["bad", "Docker is not running"]
    status = {"docker": {"daemon": {"state": "stopped"}}}
    preamble = "".join(extract(n) + "\n" for n in (
        "dockerDaemonState", "dockerCondition", "dockerStopped"
    )) + "var state = { data: { status: " + json.dumps(status) + " } };\n"
    assert evaluate("adminVersionText", {"installed": False}, preamble=preamble) == "unknown"


@requires_node
def test_a_failed_listing_read_is_retried_on_the_next_host_read():
    data = {"releases": {"error": "release_index_unreachable"}}
    result = _reconcile(data, [_read("v1.0.0", "0.3.8")], against={"releases": "v1.0.0"})

    assert result["dropped"] == ["releases"]


def test_the_install_button_waits_while_the_version_list_is_re_read():
    """Re-read after any settled operation, the list briefly lacks the picked
    version and the select stands on its first option."""

    assert "disabled: opts.blocked === true || releases === null," in extract("renderInstallForm")
