// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { applianceNow, managerActions, managerOptions, releaseStamp, riskyDirection } from "../src/lib/manager.js";
import { confinementGaps, exportSetupReport, rescueState, rescueSsh, shellAccess } from "../src/lib/access.js";

test("a deadline in flight blocks both install and revert; an expired one blocks neither", () => {
  const armed = { verify: { armed: true, deadline_epoch: 1000 }, can_revert: true };
  assert.deepEqual(managerActions(armed, 500), { armed: true, inFlight: true, expiredUnjudged: false, unreadable: false, canUpdate: false, canRevert: false });
  assert.deepEqual(managerActions(armed, 1000), { armed: true, inFlight: false, expiredUnjudged: true, unreadable: false, canUpdate: true, canRevert: true });
});

test("an unreadable clock keeps the lock, and so does a deadline record this manager cannot read", () => {
  assert.equal(managerActions({ verify: { armed: true, deadline_epoch: 1000 } }, 0).inFlight, true);
  const unreadable = managerActions({ verify: { armed: false, unreadable: "schema 9" }, can_revert: true }, 5);
  assert.equal(unreadable.canUpdate, false);
  assert.equal(unreadable.canRevert, false);
  assert.equal(managerActions({ verify: {}, can_revert: false }, 5).canRevert, false);
});

test("the appliance's own clock judges the deadline, not the browser's", () => {
  assert.equal(applianceNow({ system: { time: { epoch: 1800000006 } } }), 1800000006);
  assert.equal(applianceNow({}), 0);
});

test("a release stamp keeps its date alone without a time of day", () => {
  assert.equal(releaseStamp("2026-09-01"), "2026-09-01");
  assert.match(releaseStamp("2026-09-01T00:00:00Z"), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
});

test("two builds of one version in one minute are told apart by their release id", () => {
  const entry = (id, version, direction = "upgrade") => ({ release_id: id, described: { release_version: version, created_at: "2026-09-01T00:00:00Z" }, direction });
  const options = managerOptions([entry("a", "0.2.0"), entry("b", "0.2.0"), entry("c", "0.1.0"), { release_id: "bare" }]);
  assert.deepEqual(options.map((item) => [item.id, item.ambiguous]), [["a", true], ["b", true], ["c", false], ["bare", false]]);
  assert.equal(options[3].version, "");
});

test("an older or uncomparable package is a risky step", () => {
  assert.equal(riskyDirection({ direction: "downgrade" }), true);
  assert.equal(riskyDirection({ direction: "unknown" }), true);
  assert.equal(riskyDirection({ direction: "upgrade" }), false);
});

test("the rescue account is judged network login first, and an unread password is unknown", () => {
  assert.equal(rescueState({}).key, "rescue.unknown");
  assert.equal(rescueState(undefined).key, "rescue.unknown");
  assert.equal(rescueState({ present: false }).key, "rescue.absent");
  assert.equal(rescueState({ present: false, unreadable: "passwd unreadable" }).key, "rescue.unknown");
  assert.equal(rescueState({ present: true, password_is_default: false, ssh: { state: "accepted" } }).key, "rescue.network");
  assert.equal(rescueState({ present: true, password_is_default: null }).key, "rescue.unknown");
  assert.equal(rescueState({ present: true, password_is_default: null, password_set: false }).key, "rescue.noPassword");
  assert.equal(rescueState({ present: true, unreadable: true, password_set: false }).key, "rescue.unknown");
  assert.equal(rescueState({ present: true, password_is_default: true }).key, "rescue.shipped");
  assert.equal(rescueState({ present: true, password_is_default: false }).key, "rescue.changed");
  assert.equal(rescueSsh({ state: "odd" }), "unknown");
});

test("confinement names what sshd does not apply, and an unasked sshd is unverified", () => {
  assert.deepEqual(confinementGaps({ confinement: { available: false } }), { unverified: true, violations: [] });
  assert.deepEqual(confinementGaps({ confinement: { violations: ["allowtcpforwarding"] } }), { unverified: false, violations: ["allowtcpforwarding"] });
  assert.equal(exportSetupReport({ export_access: { reported: { status: "configured" } } }), null);
  assert.deepEqual(exportSetupReport({ export_access: { reported: { status: "failed", detail: "symlink" } } }), { status: "failed", detail: "symlink" });
});

test("shell access shows both gates", () => {
  assert.deepEqual(shellAccess({ shell_access: { account: "ems-shell", enabled: false, effectively_disabled: true, daemon: "refused", key_deployment_allowed: false, home_writable: false } }), {
    account: "ems-shell", enabled: false, confirmedOff: true, daemonAccepts: false, keysAllowed: false, homeBroken: true,
  });
});
