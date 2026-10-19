// SPDX-License-Identifier: AGPL-3.0-or-later
import { PAGES } from "./pages.js";

function fromHash() {
  const id = (globalThis.location?.hash || "").replace(/^#\/?/, "").split(/[?/]/)[0];
  return PAGES.some((page) => page.id === id) ? id : "overview";
}

class Router {
  page = $state(fromHash());
}

export const router = new Router();

export function watchHash() {
  globalThis.addEventListener("hashchange", () => {
    router.page = fromHash();
    globalThis.scrollTo?.(0, 0);
  });
}

export function href(id) {
  return `#/${id}`;
}
