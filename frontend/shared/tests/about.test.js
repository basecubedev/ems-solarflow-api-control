// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  DEVICE_REPORT,
  DOCS_SITE,
  REPOSITORY,
  SUPPORTED_SETUPS,
  buildFacts,
  knownRevision,
  noticeEntries,
  sourceLinks,
  sourceRef,
  versionText,
} from "../lib/about.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "../../..");

const COMMIT = "8baa635b0123456789abcdef0123456789abcdef";

test("the source links point at the exact commit a build names", () => {
  assert.equal(sourceRef({ revision: COMMIT, version: "v0.9.0" }), COMMIT);
  assert.deepEqual(sourceLinks(COMMIT), {
    tree: `${REPOSITORY}/tree/${COMMIT}`,
    license: `${REPOSITORY}/blob/${COMMIT}/LICENSE`,
    inventory: `${REPOSITORY}/blob/${COMMIT}/THIRD_PARTY_LICENSES.md`,
  });
});

test("without a commit the release tag of the version is the source, with the app's prefix", () => {
  assert.equal(sourceRef({ version: "v0.9.0" }), "v0.9.0");
  assert.equal(sourceRef({ version: "0.5.0", tagPrefix: "appliance-manager-v" }), "appliance-manager-v0.5.0");
  assert.equal(sourceRef({ version: "0.9.0-rc.1" }), "v0.9.0-rc.1");
});

test("a local build points at the main branch rather than at a commit that does not exist", () => {
  assert.equal(knownRevision("0".repeat(40)), "");
  assert.equal(sourceRef({ revision: "0".repeat(40), version: "local" }), "main");
  assert.equal(sourceRef({ revision: "abc123", version: "" }), "main");
  assert.deepEqual(buildFacts({ version: "local", revision: "0".repeat(40) }), { version: "", revision: "", serial: 0 });
  assert.deepEqual(buildFacts(null), { version: "", revision: "", serial: 0 });
});

test("the menu names a release by its version and a latest build by its number and commit", () => {
  const t = (key, values) => ({ "about.latest": "latest", "about.dev": "dev", "about.build": `build ${values?.serial}` })[key];
  const commit = "9168b7a2b2896524c554039547e75e7b4d88fb8a";
  assert.equal(versionText(buildFacts({ version: "v0.10.9", revision: commit, build_serial: "812", channel: "stable" }), t), "v0.10.9");
  assert.equal(versionText(buildFacts({ version: "", revision: commit, build_serial: "812", channel: "latest" }), t), "latest · build 812 · 9168b7a");
  assert.equal(versionText(buildFacts({ version: "latest", revision: commit, build_serial: "812", channel: "latest" }), t), "latest · build 812 · 9168b7a");
  assert.equal(versionText(buildFacts({ version: "latest", revision: commit, build_serial: "0", channel: "latest" }), t), "latest · 9168b7a");
  assert.equal(versionText(buildFacts({ version: "local", revision: "0".repeat(40), build_serial: "0", channel: "development" }), t), "");
  assert.equal(versionText(buildFacts(null), t), "");
  assert.equal(versionText(buildFacts({ version: "dev-feat-x-0123456789-9168b7a-45915815385-1", revision: commit, build_serial: "77", channel: "development" }), t), "dev · 9168b7a");
  assert.equal(buildFacts({ version: "latest", build_serial: "0x10" }).serial, 0);
  assert.equal(buildFacts({ version: "latest", build_serial: "1e3" }).serial, 0);
  assert.equal(buildFacts({ version: "latest", build_serial: 812 }).serial, 812);
});

test("a ref is encoded but keeps its slashes", () => {
  assert.equal(sourceLinks("feature/x y").tree, `${REPOSITORY}/tree/feature/x%20y`);
});

test("the bundled packages come first, by name, then the files beside the bundle", () => {
  const bundled = [
    { name: "svelte", version: "5.57.3", identifier: "MIT", text: "Svelte text" },
    { name: "clsx", version: "2.1.1", identifier: "MIT", text: "clsx text" },
    { version: "1" },
  ];
  const entries = noticeEntries(bundled, [{ name: "Inter", license: "OFL-1.1", file: "/Inter.LICENSE", text: "OFL" }]);
  assert.deepEqual(
    entries.map((entry) => [entry.name, entry.version, entry.license]),
    [["clsx", "2.1.1", "MIT"], ["svelte", "5.57.3", "MIT"], ["Inter", "", "OFL-1.1"]],
  );
  assert.equal(entries[2].text, "OFL");
});

test("a notice list the build did not write still shows the files", () => {
  assert.deepEqual(noticeEntries(null, [{ name: "Lucide", license: "ISC AND MIT", file: "/Lucide.LICENSE" }]).map((entry) => entry.name), ["Lucide"]);
});

test("a development Manager points at the commit its version carries", () => {
  assert.equal(sourceRef({ version: "0.0.0~dev.8baa635b0123", tagPrefix: "appliance-manager-v" }), "8baa635b0123");
  assert.equal(sourceRef({ version: "0.5.0~rc1", tagPrefix: "appliance-manager-v" }), "main");
});

test("a build of a channel names the channel, a local build names nothing", () => {
  assert.deepEqual(buildFacts({ version: "", channel: "latest", revision: COMMIT }), { version: "latest", revision: COMMIT, serial: 0 });
  assert.deepEqual(buildFacts({ version: "", channel: "development" }), { version: "", revision: "", serial: 0 });
  assert.deepEqual(buildFacts({ version: "v0.9.0", channel: "stable" }), { version: "v0.9.0", revision: "", serial: 0 });
});

test("the same package listed twice is shown once", () => {
  const bundled = [
    { name: "svelte", version: "5.57.3", identifier: "MIT" },
    { name: "svelte", version: "5.57.3", identifier: "MIT" },
    { name: "svelte", version: "4.2.0", identifier: "MIT" },
  ];
  const entries = noticeEntries(bundled, [{ name: "Inter", license: "OFL-1.1", file: "/Inter.LICENSE" }, { name: "Inter", license: "OFL-1.1", file: "/Inter.LICENSE" }]);
  assert.deepEqual(entries.map((entry) => `${entry.name}@${entry.version}`), ["svelte@5.57.3", "svelte@4.2.0", "Inter@"]);
});

test("the device report opens the repository's issue form and the setups link the published page", () => {
  const form = new URL(DEVICE_REPORT);
  assert.equal(`${form.origin}${form.pathname}`, `${REPOSITORY}/issues/new`);
  assert.ok(existsSync(join(ROOT, ".github/ISSUE_TEMPLATE", form.searchParams.get("template"))));
  const site = readFileSync(join(ROOT, "zensical.toml"), "utf8").match(/^site_url = "([^"]+)"$/m)[1];
  assert.equal(`${DOCS_SITE}/`, site);
  const page = SUPPORTED_SETUPS.slice(site.length);
  assert.match(page, /\.html$/);
  assert.ok(existsSync(join(ROOT, "docs", page.replace(/\.html$/, ".md"))));
});
