// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Per-browser appearance of the Modern front ends. Every key carries the
 * "ems-modern." prefix: Modern never touches Classic's appearance keys, so
 * switching back finds Classic exactly as it was left. The dashboard and the
 * Admin console are separate origins, so each keeps its own copy.
 */

export const THEMES = Object.freeze(["graphite", "midnight"]);
/** A scheme's name is a name, the same in every language. */
export const THEME_NAMES = Object.freeze({ graphite: "Graphite", midnight: "Midnight" });
export const LANGUAGES = Object.freeze(["en", "de", "nl", "fr"]);
export const DENSITIES = Object.freeze(["normal", "compact"]);
export const MOTIONS = Object.freeze(["on", "off"]);
export const NAV_MODES = Object.freeze(["auto", "labels", "icons"]);
const NARROW_SIDEBAR = "(max-width: 1023px)";

const SPEC = {
  theme: { key: "ems-modern.theme", allowed: THEMES, fallback: "graphite" },
  lang: { key: "ems-modern.lang", allowed: LANGUAGES, fallback: "en" },
  density: { key: "ems-modern.density", allowed: DENSITIES, fallback: "normal" },
  motion: { key: "ems-modern.motion", allowed: MOTIONS, fallback: "on" },
  nav: { key: "ems-modern.nav", allowed: NAV_MODES, fallback: "auto" },
};

/** A stored choice, or the fallback when it is missing, unknown or storage is blocked. */
export function readStored(key, allowed, fallback) {
  try {
    const stored = globalThis.localStorage?.getItem(key);
    return allowed.includes(stored) ? stored : fallback;
  } catch {
    return fallback;
  }
}

export function readStoredText(key) {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writeStored(key, value) {
  try {
    globalThis.localStorage?.setItem(key, value);
  } catch {
    return false;
  }
  return true;
}

const read = (name) => readStored(SPEC[name].key, SPEC[name].allowed, SPEC[name].fallback);

class Prefs {
  theme = $state(read("theme"));
  lang = $state(read("lang"));
  density = $state(read("density"));
  motion = $state(read("motion"));
  nav = $state(read("nav"));
  narrowScreen = $state(false);
  serverAnimation = $state("normal");
  systemReducedMotion = $state(false);

  get animate() {
    return this.motion === "on" && this.serverAnimation !== "off" && !this.systemReducedMotion;
  }

  /** "auto" keeps the text beside the icons on a desktop and drops it below 1024 px. */
  get navIconsOnly() {
    return this.nav === "icons" || (this.nav === "auto" && this.narrowScreen);
  }
}

export const prefs = new Prefs();

export function setPref(name, value) {
  const spec = SPEC[name];
  if (!spec || !spec.allowed.includes(value)) return;
  prefs[name] = value;
  writeStored(spec.key, value);
  applyDocumentPrefs();
}

export function applyDocumentPrefs() {
  const root = document.documentElement;
  root.dataset.mTheme = prefs.theme;
  root.dataset.density = prefs.density;
  root.dataset.motion = prefs.animate ? "on" : "off";
  root.lang = prefs.lang;
}

export function watchScreenWidth() {
  const query = globalThis.matchMedia?.(NARROW_SIDEBAR);
  if (!query) return;
  const update = () => {
    prefs.narrowScreen = query.matches;
  };
  update();
  query.addEventListener?.("change", update);
}

export function watchSystemMotion() {
  const query = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)");
  if (!query) return;
  const update = () => {
    prefs.systemReducedMotion = query.matches;
    applyDocumentPrefs();
  };
  update();
  query.addEventListener?.("change", update);
}
