// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { readStoredText, writeStored } from "../state/prefs.svelte.js";

const MOBILE_PAGES_KEY = "ems-modern.mobilePages";

/**
 * The page in the address (#/<id>), and the phone bar's pages. ``alias``
 * names the page an old address stands for, or null; an unknown id opens the
 * first page, so an old bookmark never lands on nothing.
 */
export function createRouter(pageSet, { alias = () => null } = {}) {
  const home = pageSet.pages[0].id;
  const known = (id) => pageSet.pages.some((page) => page.id === id);

  function fromHash() {
    const id = (globalThis.location?.hash || "").replace(/^#\/?/, "").split(/[?/]/)[0];
    if (known(id)) return id;
    const aliased = alias(id);
    return aliased && known(aliased) ? aliased : home;
  }

  class Router {
    page = $state(fromHash());
    pinned = $state.raw(pageSet.pinnedPages(readStoredText(MOBILE_PAGES_KEY)));
  }

  const router = new Router();

  return {
    router,
    href: (id) => `#/${id}`,
    watchHash() {
      globalThis.addEventListener("hashchange", () => {
        router.page = fromHash();
        globalThis.scrollTo?.(0, 0);
      });
    },
    /** The four pages of a phone's bar, in the order the bar shows them. */
    setPinned(ids) {
      router.pinned = pageSet.pinnedPages(ids);
      writeStored(MOBILE_PAGES_KEY, router.pinned.join(","));
    },
  };
}
