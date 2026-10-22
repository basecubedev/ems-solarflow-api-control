// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminSend } from "./auth.svelte.js";
import { system, reload } from "./system.svelte.js";
import { changeCount, fieldIndex, liveEdits, overrideTargets, sameAsSaved } from "../lib/settings.js";

const EDITS_KEY = "ems-admin.settings-edits";

const PATHS = {
  preview: "/api/admin/maintenance/config/preview",
  apply: "/api/admin/maintenance/config/apply",
  reset: "/api/admin/maintenance/config/reset-runtime",
};

function mapping(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function readEdits() {
  try {
    return mapping(JSON.parse(globalThis.sessionStorage?.getItem(EDITS_KEY) || "null"));
  } catch {
    return {};
  }
}

function storeEdits(edits) {
  try {
    if (Object.keys(edits).length) globalThis.sessionStorage?.setItem(EDITS_KEY, JSON.stringify(edits));
    else globalThis.sessionStorage?.removeItem(EDITS_KEY);
  } catch {
    return;
  }
}

/**
 * The settings the reader changed and has not applied: config paths and their
 * new values, laid over whatever config the server last answered. They are
 * held here rather than by the page, so changing pages keeps them, and in the
 * tab's session storage, so a reload keeps them too. Because they are laid
 * over the newest answer, a config another console changed meanwhile is the
 * base of the next preview rather than a conflict at apply.
 */
class SettingsEditor {
  edits = $state.raw(readEdits());
  preview = $state.raw(null);
  previewedFor = $state("");
  previewing = $state(false);
  previewError = $state.raw(null);
  applying = $state(false);
  applied = $state.raw(null);
  applyError = $state.raw(null);
  resetting = $state(false);
  resetError = $state.raw(null);

  saved = $derived(system.config.data?.status === "ok" && system.config.data.draft ? system.config.data : null);
  fields = $derived(fieldIndex(this.saved));
  live = $derived(this.saved ? liveEdits(this.edits, this.saved.draft.features, this.fields) : {});
  draft = $derived(this.saved ? { ...this.saved.draft, features: { ...mapping(this.saved.draft.features), ...this.live } } : null);
  changes = $derived(this.saved ? changeCount(this.draft, this.saved.draft) : 0);
  fresh = $derived(
    Boolean(this.preview) && this.previewedFor === JSON.stringify(this.draft) && (this.preview.revision === undefined || this.preview.revision === this.saved?.revision),
  );
  reviewed = $derived(this.fresh && this.preview?.status === "ok" && Boolean(this.preview?.revision) && Boolean(this.preview?.validation?.ok));
  overrides = $derived(overrideTargets(this.saved?.overrides));
}

export const settings = new SettingsEditor();

function setEdits(edits) {
  settings.edits = edits;
  storeEdits(edits);
}

/**
 * An edit a newer config made pointless is forgotten, not merely hidden: kept,
 * it would come back as an unsaved change once the config moved on again.
 */
$effect.root(() => {
  $effect(() => {
    if (settings.saved && JSON.stringify(settings.live) !== JSON.stringify(settings.edits)) setEdits(settings.live);
  });
});

/**
 * One field's new value. A value that is what the config already says is no
 * change, so typing a number back to what it was leaves nothing to apply.
 */
export function setValue(field, value) {
  const edits = { ...settings.live };
  if (sameAsSaved(field, value, settings.saved?.draft?.features)) delete edits[field.path];
  else edits[field.path] = value;
  setEdits(edits);
  settings.applied = null;
}

/** Back to the saved config. Nothing on the system changes. */
export function discard() {
  setEdits({});
  settings.preview = null;
  settings.previewedFor = "";
  settings.previewError = null;
  settings.applyError = null;
}

/**
 * The server's preview of the draft: what would change, and whether the
 * result is a valid config. A draft the server refuses answers with its
 * validation, which is the preview to show, not a failure to read.
 */
export async function previewDraft() {
  if (settings.previewing || !settings.draft) return;
  const draft = settings.draft;
  settings.previewing = true;
  settings.previewError = null;
  settings.applyError = null;
  const current = () => JSON.stringify(settings.draft) === JSON.stringify(draft);
  try {
    const answer = await adminSend(PATHS.preview, { body: { draft } });
    if (!current()) return;
    settings.preview = answer;
    settings.previewedFor = JSON.stringify(draft);
    if (answer?.revision && answer.revision !== settings.saved?.revision) reload("config");
  } catch (error) {
    if (!current()) return;
    if (error?.payload?.validation) {
      settings.preview = error.payload;
      settings.previewedFor = JSON.stringify(draft);
    } else {
      settings.preview = null;
      settings.previewError = error;
    }
  } finally {
    settings.previewing = false;
  }
}

/**
 * Writes the reviewed draft to the config, with a backup first unless the
 * reader turned it off. A draft changed after its preview is refused here:
 * what is applied is what was reviewed.
 */
export async function applyDraft({ backup = true } = {}) {
  if (settings.applying || !settings.reviewed || !settings.saved) return;
  const draft = settings.draft;
  const changes = settings.preview?.diff;
  const revision = settings.preview.revision;
  settings.applying = true;
  settings.applyError = null;
  try {
    const result = await adminSend(PATHS.apply, { body: { draft, revision, confirm: true, backup } });
    if (result?.ok === false) {
      settings.applyError = result;
      return;
    }
    settings.applied = { result, diff: changes, at: Date.now() };
    setEdits({});
    settings.preview = null;
    settings.previewedFor = "";
  } catch (error) {
    settings.applyError = error;
    if (error?.payload?.validation) {
      settings.preview = error.payload;
      settings.previewedFor = JSON.stringify(draft);
    }
  } finally {
    settings.applying = false;
    reload("config", "overview", "plan");
  }
}

/** Puts the dashboard's runtime overrides back to the saved config values, at once. */
export async function resetOverrides() {
  const targets = settings.overrides;
  if (settings.resetting || !targets.length) return;
  settings.resetting = true;
  settings.resetError = null;
  try {
    await adminSend(PATHS.reset, { body: { targets, confirm: true } });
  } catch (error) {
    settings.resetError = error;
  } finally {
    settings.resetting = false;
    reload("config");
  }
}
