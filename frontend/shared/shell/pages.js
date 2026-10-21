// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * An app's navigation as the shell reads it: the pages in menu order, the
 * groups they fall into and the four a phone's bar holds until the reader
 * picks their own. Pure, so the node tests can walk it.
 */
export function createPageSet({ pages, groups, mobile }) {
  const ids = pages.map((page) => page.id);
  const slots = mobile.length;

  /**
   * The bar's pages from what the browser stored: known ids, each once, the
   * first ones kept, and the default filling any gap in its own order.
   */
  function pinnedPages(stored) {
    const wanted = typeof stored === "string" ? stored.split(",") : Array.isArray(stored) ? stored : [];
    const chosen = [];
    for (const raw of wanted) {
      const id = String(raw).trim();
      if (chosen.length < slots && !chosen.includes(id) && ids.includes(id)) chosen.push(id);
    }
    for (const id of mobile) {
      if (chosen.length < slots && !chosen.includes(id)) chosen.push(id);
    }
    return chosen;
  }

  function pageById(id) {
    return pages.find((page) => page.id === id) || pages[0];
  }

  return Object.freeze({ pages, groups, mobile: Object.freeze([...mobile]), slots, pinnedPages, pageById });
}

/**
 * Two pages in the bar trade places; a page from More takes the place of the
 * one in the bar it is swapped with. Two pages from More swap nothing: null.
 */
export function swapPinned(pinned, first, second) {
  const a = pinned.indexOf(first);
  const b = pinned.indexOf(second);
  if (first === second || (a < 0 && b < 0)) return null;
  const next = [...pinned];
  if (a >= 0) next[a] = second;
  if (b >= 0) next[b] = first;
  return next;
}
