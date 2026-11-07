// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the About page says and where its links point: the project's own
 * copyright and license, the source of exactly the running version, and the
 * notices of everything the page itself was built from. Pure, so the node
 * tests can walk it.
 */

export const REPOSITORY = "https://github.com/basecubedev/ems-solarflow-api-control";
export const COPYRIGHT = "© 2026 basecubedev";
export const PROJECT_LICENSE = "AGPL-3.0-or-later";

/**
 * The notice files every Modern build copies beside its bundle; an app adds
 * its own (the dashboard's uPlot, the Admin console's Vite preload helper).
 */
export const SHARED_NOTICES = Object.freeze([
  Object.freeze({ name: "Inter", license: "OFL-1.1", file: "/Inter.LICENSE" }),
  Object.freeze({ name: "Lucide", license: "ISC AND MIT", file: "/Lucide.LICENSE" }),
]);

/** The file the image build writes beside the bundle, naming the version and commit it was built from. */
export const BUILD_FILE = "/build.json";
/** The notices of every package Vite bundled, written by the build itself. */
export const BUNDLED_FILE = "/licenses.json";

const REVISION = /^[0-9a-f]{40}$/;
const RELEASE = /^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;
const DEVELOPMENT = /~dev\.([0-9a-f]{7,40})$/;
const NO_CHANNEL = new Set(["", "development", "local"]);

/** A full commit id that names a real commit, not the all-zero placeholder of a local build. */
export function knownRevision(value) {
  const revision = String(value || "").trim().toLowerCase();
  return REVISION.test(revision) && /[1-9a-f]/.test(revision) ? revision : "";
}

/**
 * The git ref the source links point at: the exact commit when the build named
 * one, the commit a development version carries (``0.0.0~dev.<sha>``), else
 * the release tag its version belongs to, else the main branch.
 */
export function sourceRef({ revision = "", version = "", tagPrefix = "v" } = {}) {
  const commit = knownRevision(revision);
  if (commit) return commit;
  const release = String(version || "").trim();
  const development = DEVELOPMENT.exec(release);
  if (development) return development[1];
  if (RELEASE.test(release)) return `${tagPrefix}${release.replace(/^v/, "")}`;
  return "main";
}

export function sourceLinks(ref) {
  const encoded = encodeURIComponent(ref).replaceAll("%2F", "/");
  return {
    tree: `${REPOSITORY}/tree/${encoded}`,
    license: `${REPOSITORY}/blob/${encoded}/LICENSE`,
    inventory: `${REPOSITORY}/blob/${encoded}/THIRD_PARTY_LICENSES.md`,
  };
}

/**
 * The version and commit a build file names, or nothing when the build wrote
 * none. A build of a channel rather than a release (``latest``) names the
 * channel as its version.
 */
export function buildFacts(value) {
  if (!value || typeof value !== "object") return { version: "", revision: "" };
  const version = String(value.version || "").trim();
  const channel = String(value.channel || "").trim();
  return {
    version: version && version !== "local" ? version : NO_CHANNEL.has(channel) ? "" : channel,
    revision: knownRevision(value.revision),
  };
}

/**
 * Every notice the page shows, in the order a reader looks for them: the
 * bundled packages as the build listed them, then the files beside the bundle.
 */
export function noticeEntries(bundled, files) {
  const packages = Array.isArray(bundled)
    ? bundled
        .filter((entry) => entry && typeof entry.name === "string")
        .map((entry) => ({
          name: entry.name,
          version: String(entry.version || ""),
          license: String(entry.identifier || entry.license || ""),
          text: String(entry.text || ""),
        }))
        .sort((a, b) => a.name.localeCompare(b.name))
    : [];
  const seen = new Set();
  return [...packages, ...files.map((entry) => ({ version: "", text: "", ...entry }))].filter((entry) => {
    const key = noticeKey(entry);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** One notice per package and version: the same name twice in a build is one entry, not a crash. */
export function noticeKey(entry) {
  return `${entry.name}@${entry.version}`;
}
