# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release listing's warnings stay beside the System Build selector.

The listing names what it could not read (GitHub releases, the development
catalogue, the baseline) in ``warnings``. Guided Setup and Guided Upgrade each
render every one of them as text on a line of its own, whether or not a build
is selectable, and a later status change does not take them away: the missing
Experimental group stays explained after the operator changes the selection
or verifies a build.
"""

import json
import os
import re

import pytest

from tests.test_admin_frontend import (
    STATIC_DIR,
    _read,
    _release_stage,
    _run_node,
    _upgrade_panel,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

_LISTING_WARNINGS = [
    "GitHub releases are unavailable: timed out",
    "The development build catalogue could not be read, so no development "
    "builds are listed.",
]

_RELEASE_LOADER_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(__ADMIN_JS__, "utf8");
function extractFunction(name) {
  const start = source.indexOf("function " + name + "(");
  if (start === -1) throw new Error("missing " + name);
  let depth = 0;
  let cursor = source.indexOf("(", start);
  for (let i = cursor; i < source.length; i++) {
    if (source[i] === "(") depth++;
    else if (source[i] === ")" && --depth === 0) { cursor = i; break; }
  }
  depth = 0;
  for (let i = source.indexOf("{", cursor); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("unterminated " + name);
}
const innerHtmlWrites = [];
function element() {
  const node = {
    children: [], dataset: {}, disabled: false, hidden: true, value: "",
    classList: { toggle() {}, add() {}, remove() {} },
    appendChild(child) { this.children.push(child); },
  };
  let text = "";
  let html = "";
  Object.defineProperty(node, "textContent", {
    get: () => text, set: (value) => { text = String(value); },
  });
  Object.defineProperty(node, "innerHTML", {
    get: () => html,
    set: (value) => { html = String(value); if (html) innerHtmlWrites.push(html); },
  });
  return node;
}
const document = { createElement: () => element() };
function listing(warnings, selectable) {
  return {
    releases: [{ tag: "v0.8.14", channel: "stable", selectable, prepared: false }],
    default_release: selectable ? "v0.8.14" : null,
    warnings,
  };
}
function respond(data) {
  return async () => ({ ok: true, json: async () => data });
}
function shown(node) {
  return node.hidden ? null : node.textContent;
}
"""


def _release_loader_script(body):
    harness = _RELEASE_LOADER_HARNESS.replace(
        "__ADMIN_JS__", json.dumps(os.path.join(STATIC_DIR, "admin.js"))
    )
    return harness + "\nconst WARNINGS = " + json.dumps(_LISTING_WARNINGS) + ";\n" + body


_UPGRADE_LOADER = r"""
async function loadUpgrade(data) {
  const upgradeEls = {
    select: element(), releaseStatus: element(), releaseError: element(),
    releaseWarnings: element(), prepareBtn: element(),
  };
  const upgradeState = {
    status: null, error: null, selected: null, releases: [], releaseWarnings: [],
  };
  const run = new Function(
    "document", "upgradeEls", "upgradeState", "fetch",
    '"use strict";\n' +
    "const UPGRADE_RELEASE_STATUS_TEXT = { idle: 'Select a System Build, then verify it.' };\n" +
    "function humanErrorText(data, fallback) { return (data && (data.error || data.message)) || fallback; }\n" +
    "function groupSetupReleaseOptions(releases) { return [{ label: 'Stable', releases }]; }\n" +
    "function releaseOptionLabel(release) { return release.tag; }\n" +
    "function selectUpgradeReleaseTag(releases, data) {\n" +
    "  const hit = releases.find((r) => r.tag === data.default_release && r.selectable !== false);\n" +
    "  return hit ? hit.tag : null;\n" +
    "}\n" +
    "function upgradeVerificationSurvivesReload() { return false; }\n" +
    "function clearUpgradeVerification() { upgradeState.status = 'idle'; }\n" +
    "function renderUpgradeBadges() {}\n" +
    "function upgradeSelectedRelease() { return upgradeState.releases.find((r) => r.tag === upgradeState.selected); }\n" +
    "function upgradeTargetPrepared() { return false; }\n" +
    "function updateUpgradeActionButtons() {}\n" +
    extractFunction("renderReleaseListingWarnings") + "\n" +
    extractFunction("setUpgradeReleaseStatus") + "\n" +
    "async " + extractFunction("loadUpgradeReleases") + "\n" +
    "return loadUpgradeReleases();"
  );
  await run(document, upgradeEls, upgradeState, respond(data));
  return {
    selected: upgradeState.selected,
    status: upgradeState.status,
    error: upgradeEls.releaseError.hidden ? null : upgradeEls.releaseError.textContent,
    statusText: upgradeEls.releaseStatus.textContent,
    warnings: shown(upgradeEls.releaseWarnings),
  };
}
"""


def test_guided_upgrade_names_every_listing_warning_beside_a_selectable_build():
    script = _release_loader_script(_UPGRADE_LOADER + """
(async () => {
  const out = {
    selectable: await loadUpgrade(listing(WARNINGS, true)),
    unselectable: await loadUpgrade(listing(WARNINGS, false)),
    clean: await loadUpgrade(listing([], true)),
  };
  out.innerHtmlWrites = innerHtmlWrites;
  console.log(JSON.stringify(out));
})();
""")
    out = _run_node(script)

    selectable = out["selectable"]
    assert selectable["selected"] == "v0.8.14"
    assert selectable["error"] is None
    assert selectable["warnings"] is not None, selectable
    for warning in _LISTING_WARNINGS:
        assert warning in selectable["warnings"]

    unselectable = out["unselectable"]
    assert unselectable["selected"] is None
    assert unselectable["error"] == "No System Builds are available."
    for warning in _LISTING_WARNINGS:
        assert warning in unselectable["warnings"]

    assert out["clean"]["warnings"] is None
    assert not any(
        warning in html for html in out["innerHtmlWrites"] for warning in _LISTING_WARNINGS
    )


def test_guided_upgrade_has_a_listing_warning_line_beside_the_status():
    panel = _upgrade_panel(_read("index.html"))
    stage = panel.split('id="upgrade-release-status"', 1)[1].split("</section>", 1)[0]
    line = re.search(r'<p id="upgrade-release-warnings"[^>]*>', stage)
    assert line is not None
    assert 'role="status"' in line.group(0)
    assert "hidden" in line.group(0)
    assert (
        'releaseWarnings: document.getElementById("upgrade-release-warnings")'
        in _read("admin.js")
    )


_SETUP_LOADER = r"""
async function loadSetup(data) {
  const setupEls = {
    releaseSelect: element(), releaseStatus: element(), releaseError: element(),
    releaseWarnings: element(), releaseStatusVal: element(),
    releaseSelectedVal: element(),
  };
  const setupState = { release: { releases: [], warnings: [] } };
  const run = new Function(
    "document", "setupEls", "setupState", "fetch",
    '"use strict";\n' +
    "let selectedSystemBuildTag = null;\n" +
    "let systemBuildResumeValidationTag = null;\n" +
    "const RELEASE_STATUS_TEXT = {};\n" +
    "function setSummary() {}\n" +
    "function renderStepper() {}\n" +
    "function humanErrorText(data, fallback) { return (data && (data.error || data.message)) || fallback; }\n" +
    "function groupSetupReleaseOptions(releases) { return [{ label: 'Stable', releases }]; }\n" +
    "function releaseOptionLabel(release) { return release.tag; }\n" +
    "function renderReleaseBadges() {}\n" +
    "function preparedResourceStatus() { return null; }\n" +
    "async function loadActiveConfigTemplate() {}\n" +
    "function clearActiveConfigTemplate() {}\n" +
    "function renderReleaseResources() {}\n" +
    "function presentSelectedSystemBuild() {}\n" +
    "function validateSelectedSystemBuild() {}\n" +
    extractFunction("renderReleaseListingWarnings") + "\n" +
    extractFunction("setReleaseStatus") + "\n" +
    "async " + extractFunction("loadReleases") + "\n" +
    "return { loadReleases, setReleaseStatus };"
  );
  const page = run(document, setupEls, setupState, respond(data));
  await page.loadReleases();
  const read = () => ({
    status: setupEls.releaseStatus.textContent,
    error: shown(setupEls.releaseError),
    warnings: shown(setupEls.releaseWarnings),
  });
  return { page, read };
}
"""


def test_guided_setup_names_every_listing_warning_on_its_own_line():
    script = _release_loader_script(_SETUP_LOADER + """
(async () => {
  const out = {
    selectable: (await loadSetup(listing(WARNINGS, true))).read(),
    unselectable: (await loadSetup(listing(WARNINGS, false))).read(),
    clean: (await loadSetup(listing([], true))).read(),
  };
  out.innerHtmlWrites = innerHtmlWrites;
  console.log(JSON.stringify(out));
})();
""")
    out = _run_node(script)

    selectable = out["selectable"]
    assert selectable["error"] is None
    assert selectable["warnings"] is not None, selectable
    for warning in _LISTING_WARNINGS:
        assert warning in selectable["warnings"]
        assert warning not in selectable["status"]

    unselectable = out["unselectable"]
    assert unselectable["error"] == "No System Builds are available."
    for warning in _LISTING_WARNINGS:
        assert warning in unselectable["warnings"]

    assert out["clean"]["warnings"] is None
    assert not any(
        warning in html for html in out["innerHtmlWrites"] for warning in _LISTING_WARNINGS
    )


def test_guided_setup_keeps_the_listing_warnings_through_every_status_change():
    """A selection change, a Verify and a failure each set the release status.

    The warnings line is not the status line, so none of them clears it.
    """

    script = _release_loader_script(_SETUP_LOADER + """
(async () => {
  const setup = await loadSetup(listing(WARNINGS, true));
  const out = {};
  for (const [status, error] of [
    ["not_started"], ["downloading"], ["ready"], ["failed", "verification failed"],
  ]) {
    setup.page.setReleaseStatus(status, error);
    out[status] = setup.read();
  }
  console.log(JSON.stringify(out));
})();
""")
    out = _run_node(script)

    assert set(out) == {"not_started", "downloading", "ready", "failed"}
    for status, rendered in out.items():
        assert rendered["warnings"] is not None, (status, rendered)
        for warning in _LISTING_WARNINGS:
            assert warning in rendered["warnings"], (status, rendered)
    assert out["failed"]["error"] == "verification failed"


def test_guided_setup_has_a_listing_warning_line_beside_the_status():
    stage = _release_stage(_read("index.html"))
    beside = stage.split('id="release-status"', 1)[1].split("</section>", 1)[0]
    line = re.search(r'<p id="release-warnings"[^>]*>', beside)
    assert line is not None
    assert 'role="status"' in line.group(0)
    assert "hidden" in line.group(0)
    assert (
        'releaseWarnings: document.getElementById("release-warnings")'
        in _read("admin.js")
    )


_SAME_PAGE_SETUP = _SETUP_LOADER.replace(
    "const page = run(document, setupEls, setupState, respond(data));",
    "let current = respond(data);\n"
    "  const page = run(document, setupEls, setupState, (...a) => current(...a));\n"
    "  page.respondWith = (next) => { current = next; };",
)

_SAME_PAGE_UPGRADE = _UPGRADE_LOADER.replace(
    "async function loadUpgrade(data) {",
    "async function loadUpgrade(data, next) {",
).replace(
    "await run(document, upgradeEls, upgradeState, respond(data));",
    "let current = respond(data);\n"
    "  await run(document, upgradeEls, upgradeState, (...a) => current(...a));\n"
    "  current = next;\n"
    "  await run(document, upgradeEls, upgradeState, (...a) => current(...a));",
)


def test_setup_reload_clears_the_previous_listing_warnings():
    assert "page.respondWith" in _SAME_PAGE_SETUP
    out = _run_node(_release_loader_script(_SAME_PAGE_SETUP + """
(async () => {
  const setup = await loadSetup(listing(WARNINGS, true));
  const first = setup.read();
  setup.page.respondWith(respond(listing([], true)));
  await setup.page.loadReleases();
  const clean = setup.read();
  setup.page.respondWith(async () => ({ ok: false, json: async () => ({ error: "release list unavailable" }) }));
  await setup.page.loadReleases();
  const failed = setup.read();
  console.log(JSON.stringify({ first, clean, failed }));
})();
"""))
    assert out["first"]["warnings"] is not None
    assert out["clean"]["warnings"] is None, out["clean"]
    assert out["failed"]["warnings"] is None, out["failed"]


def test_upgrade_reload_clears_the_previous_listing_warnings():
    assert "current = next;" in _SAME_PAGE_UPGRADE
    out = _run_node(_release_loader_script(_SAME_PAGE_UPGRADE + """
(async () => {
  const clean = await loadUpgrade(listing(WARNINGS, true), respond(listing([], true)));
  console.log(JSON.stringify({ clean }));
})();
"""))
    assert out["clean"]["warnings"] is None, out["clean"]


_FAILED_LISTING = 'async () => ({ ok: false, json: async () => ({ error: "release list unavailable" }) })'


def test_setup_clears_its_warnings_when_the_reload_after_them_fails():
    out = _run_node(_release_loader_script(_SAME_PAGE_SETUP + """
(async () => {
  const setup = await loadSetup(listing(WARNINGS, true));
  const first = setup.read();
  setup.page.respondWith(%s);
  await setup.page.loadReleases();
  console.log(JSON.stringify({ first, failed: setup.read() }));
})();
""" % _FAILED_LISTING))
    assert out["first"]["warnings"] is not None
    assert out["failed"]["warnings"] is None, out["failed"]


def test_upgrade_clears_its_warnings_when_the_reload_after_them_fails():
    out = _run_node(_release_loader_script(_SAME_PAGE_UPGRADE + """
(async () => {
  const failed = await loadUpgrade(listing(WARNINGS, true), %s);
  console.log(JSON.stringify({ failed }));
})();
""" % _FAILED_LISTING))
    assert out["failed"]["warnings"] is None, out["failed"]
