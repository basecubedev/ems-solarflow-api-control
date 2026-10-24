// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CHANNELS, CHANNEL_TONES, RELEASE_GROUPS, UPGRADE_STATES, PAIR_STATES, groupedReleases, noteView, pairView, releaseList, upgradePage } from "../src/lib/updates.js";

const read = (path) => readFileSync(new URL(`../../../admin/${path}`, import.meta.url), "utf8");

test("upgrade verdicts, channels and running-pair states are the server's", () => {
  const identity = read("image_identity.py");
  const verdicts = ["UPGRADE_AVAILABLE", "ALREADY_CURRENT", "OLDER_THAN_RUNNING_BUILD", "DOWNGRADE_BLOCKED", "ROLLBACK_AVAILABLE", "IDENTITY_UNKNOWN"].map((name) => identity.match(new RegExp(`^${name} = "([a-z_]+)"`, "m"))[1]);
  assert.deepEqual(verdicts.sort(), Object.keys(UPGRADE_STATES).sort());
  const releases = read("releases.py");
  const channels = new Set([...releases.matchAll(/^\s+"(latest|legacy|rc|stable|unknown|development)"$/gm)].map((match) => match[1]));
  channels.add("development");
  assert.deepEqual([...channels].filter((channel) => !CHANNELS.includes(channel)), []);
  const alignment = read("system_alignment.py");
  const pairs = [...alignment.matchAll(/^RUNNING_PAIR_(?!HEALTH)\w+ = "([a-z_]+)"/gm)].map((match) => match[1]);
  assert.deepEqual(pairs.sort(), Object.keys(PAIR_STATES).sort());
});

test("a release says whether it is recommended, running and selectable, and keeps the server's reason", () => {
  const list = releaseList({
    default_release: "v2",
    releases: [
      { tag: "latest", channel: "latest", upgrade_state: "upgrade_available", reason: "Rolling Docker channel" },
      { tag: "v2", channel: "stable", upgrade_state: "identity_unknown", published_at: "2026-01-01T10:00:00Z" },
      { tag: "v1", channel: "stable", active: true, selectable: false, upgrade_state: "already_current" },
      { tag: "x", channel: "nightly", upgrade_state: "sideways" },
    ],
  });
  assert.deepEqual(list.map((release) => [release.tag, release.recommended, release.running, release.selectable]), [["latest", false, false, true], ["v2", true, false, true], ["v1", false, true, false], ["x", false, false, true]]);
  assert.deepEqual(list[0].note, { key: null, text: "Rolling Docker channel" });
  assert.equal(list[1].note, null);
  assert.deepEqual([list[3].channel, list[3].state], ["unknown", null]);
  assert.deepEqual(releaseList(null), []);
});

test("an unknown pair state reads as unverified", () => {
  assert.deepEqual(pairView({ state: "current" }), { state: "current", tone: "c-ok" });
  assert.deepEqual(pairView({ state: "mystery" }), { state: "unverified", tone: "c-idle" });
  assert.equal(pairView(null), null);
});

test("a note the server is known to write is said in the reader's language, anything else as it came", () => {
  assert.deepEqual(noteView("Rolling Docker channel, not a stable release"), { key: "updates.note.latest", text: "Rolling Docker channel, not a stable release" });
  assert.deepEqual(noteView("  Already running this EMS build. "), { key: "updates.note.alreadyCurrent", text: "Already running this EMS build." });
  assert.deepEqual(noteView("Something new"), { key: null, text: "Something new" });
  assert.equal(noteView(""), null);
  assert.equal(noteView(null), null);
});

test("a release that cannot be chosen says why, not a warning about its metadata", () => {
  const [release] = releaseList({ releases: [{ tag: "v1", channel: "stable", reason: "Docker setup resources could not be verified.", upgrade_warning: "Legacy image metadata missing. Upgrade check uses SemVer fallback." }] });
  assert.equal(release.note.key, "updates.note.resourcesUnverified");
});

test("a release the catalogue lets the upgrade choose opens it, chosen; any other opens nothing", () => {
  const list = releaseList({
    releases: [
      { tag: "v0.9.0-rc.1", selectable: true },
      { tag: "latest" },
      { tag: "v0.8.0", selectable: false, upgrade_state: "older_than_running_build" },
      { tag: "", selectable: true },
    ],
  });
  assert.deepEqual(list.map((release) => release.upgrade), ["maintenance-upgrade-v0.9.0-rc.1", "maintenance-upgrade-latest", null, null]);
  assert.equal(upgradePage({ tag: "a b/c", selectable: true }), "maintenance-upgrade-a%20b%2Fc");
  assert.equal(upgradePage(null), null);
});

test("every channel has a tone and lands in exactly one group, stable releases first", () => {
  assert.deepEqual(Object.keys(CHANNEL_TONES).sort(), [...CHANNELS].sort());
  const grouped = RELEASE_GROUPS.flatMap((group) => group.channels);
  assert.deepEqual([...grouped].sort(), [...CHANNELS].sort());
  assert.equal(new Set(grouped).size, grouped.length);
  assert.equal(RELEASE_GROUPS[0].id, "stable");
});

test("the list is grouped by channel, each group in the server's order, empty groups left out", () => {
  const list = releaseList({
    default_release: "v2",
    releases: [
      { tag: "latest", channel: "latest" },
      { tag: "v2", channel: "stable" },
      { tag: "v1", channel: "stable" },
      { tag: "dev-1", channel: "development" },
      { tag: "x", channel: "nightly" },
    ],
  });
  const groups = groupedReleases(list);
  assert.deepEqual(groups.map((group) => [group.id, group.tone, group.releases.map((release) => release.tag)]), [
    ["stable", "c-bat", ["v2", "v1"]],
    ["latest", "c-out", ["latest"]],
    ["development", "c-soc", ["dev-1"]],
    ["other", "c-idle", ["x"]],
  ]);
  assert.deepEqual(groupedReleases(null), []);
});
