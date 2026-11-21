// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CHANNELS, CHANNEL_FILTERS, CHANNEL_TONES, RELEASE_GROUPS, UPGRADE_STATES, PAIR_STATES, channelChoices, groupedReleases, noteView, pairView, recommendationNote, releaseList, releasesInChannel, shownChannel, updateMark, updateWaits } from "../src/lib/updates.js";

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

test("the channel filter offers all and each listed group, counts what is shown, and keeps the release chosen for an update", () => {
  const releases = releaseList({
    releases: [
      { tag: "v2", channel: "stable", upgrade_state: "upgrade_available" },
      { tag: "v1", channel: "stable", active: true, selectable: false, upgrade_state: "already_current" },
      { tag: "latest", channel: "latest", upgrade_state: "upgrade_available" },
      { tag: "v3-rc1", channel: "rc", selectable: false, upgrade_state: "downgrade_blocked" },
      { tag: "x", channel: "nightly" },
    ],
  });
  const installable = releases.filter((release) => release.installable);
  const choices = channelChoices(releases, installable);
  assert.deepEqual(choices.map((choice) => [choice.id, choice.count]), [["all", 3], ["stable", 1], ["latest", 1], ["rc", 0], ["other", 1]]);
  assert.deepEqual(choices.map((choice) => choice.tone), [null, CHANNEL_TONES.stable, CHANNEL_TONES.latest, CHANNEL_TONES.rc, CHANNEL_TONES.legacy]);
  assert.deepEqual(choices.map((choice) => choice.id).filter((id) => !CHANNEL_FILTERS.includes(id)), []);
  assert.deepEqual(channelChoices(releases, releases).map((choice) => choice.count), [5, 2, 1, 1, 1]);

  assert.equal(shownChannel(choices, "stable"), "stable");
  assert.equal(shownChannel(choices, "development"), "all");
  assert.equal(shownChannel([], "stable"), "all");
  assert.deepEqual(releasesInChannel(installable, "stable", null).map((release) => release.tag), ["v2"]);
  assert.deepEqual(releasesInChannel(installable, "stable", "latest").map((release) => release.tag), ["v2", "latest"]);
  assert.deepEqual(releasesInChannel(installable, "rc", null), []);
  assert.deepEqual(releasesInChannel(installable, "all", null), installable);
  assert.deepEqual(releasesInChannel(installable, "other", null).map((release) => release.tag), ["x"]);
});

test("a catalogue of one channel offers nothing to filter", () => {
  const releases = releaseList({ releases: [{ tag: "v2", channel: "stable" }, { tag: "v1", channel: "stable" }] });
  assert.deepEqual(channelChoices(releases, releases), []);
  assert.deepEqual(channelChoices([], []), []);
  assert.deepEqual(CHANNEL_FILTERS, ["all", ...RELEASE_GROUPS.map((group) => group.id)]);
});

test("nothing proposed says why: the rolling build is chosen by hand, a stable release is current", () => {
  const rolling = { running_rolling: true, default_release: null, active_release: null, releases: [
    { tag: "latest", channel: "latest", selectable: true },
    { tag: "v1", channel: "stable", selectable: true, upgrade_state: "rollback_available" },
  ] };
  const stable = { running_rolling: false, default_release: null, active_release: "v1", releases: [
    { tag: "latest", channel: "latest", selectable: true },
    { tag: "v1", channel: "stable", active: true, selectable: false },
  ] };
  const proposed = { running_rolling: true, default_release: "v2", releases: [
    { tag: "latest", channel: "latest" },
    { tag: "v2", channel: "stable" },
  ] };
  const unknown = { running_rolling: false, default_release: null, releases: [{ tag: "v1", channel: "stable" }] };
  const both = { running_rolling: true, default_release: null, releases: [{ tag: "v1", channel: "stable", active: true }] };
  for (const [catalogue, note] of [[rolling, "rolling"], [stable, "stable"], [proposed, null], [unknown, null], [both, "rolling"]]) {
    assert.equal(recommendationNote(catalogue, releaseList(catalogue)), note);
  }
});

test("the menu's Updates entry waits on a release the server still proposes or an InfluxDB the running release ships", () => {
  const influx = (state) => ({ ok: true, state, current_image: "influxdb:2.7", target_image: "influxdb:2.9.1" });
  const known = (state) => ({ ok: true, release: { state } });
  assert.equal(updateWaits(known("available"), influx("current")), true);
  assert.equal(updateWaits(known("current"), influx("available")), true);
  assert.equal(updateWaits(known("current"), influx("current")), false);
  assert.equal(updateWaits(known("unknown"), influx("no_way_back")), false);
  assert.equal(updateWaits(known("unknown"), influx("low_memory")), true);
  assert.equal(updateWaits(known("unknown"), { ...influx("available"), target_image: null }), false);
  assert.equal(updateWaits(null, null), false);
});

test("the menu marks Updates as a warning while an InfluxDB run needs looking at, else as a waiting update", () => {
  const plan = (state, record) => ({ ok: true, state, current_image: "influxdb:2.7", target_image: "influxdb:2.9.1", last: record ? { state: record } : null });
  const known = (state) => ({ ok: true, release: { state } });
  assert.equal(updateMark(known("available"), plan("current", "failed")), "warning");
  assert.equal(updateMark(known("current"), plan("available")), "info");
  assert.equal(updateMark(known("current"), plan("low_memory")), "info");
  assert.equal(updateMark(known("available"), plan("current")), "info");
  assert.equal(updateMark(known("current"), plan("current", "upgraded")), "");
  assert.equal(updateMark(known("current"), plan("busy", "running")), "");
  assert.equal(updateMark(known("current"), plan("available", "failed")), "info");
});
