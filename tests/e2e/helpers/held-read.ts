import { type Page } from "@playwright/test";

/**
 * Hold the first request to ``path`` from now on until ``release`` is called.
 *
 * Install it only once the reads that came before have returned: the first
 * matching request is the one held, whichever page made it.
 */
export async function holdRead(page: Page, path: string) {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  let reached: () => void = () => {};
  const requested = new Promise<void>((resolve) => (reached = resolve));
  let first = true;
  await page.route(
    (url) => url.pathname === path,
    async (route) => {
      if (first) {
        first = false;
        reached();
        await held;
      }
      await route.continue();
    },
  );
  return { requested, release };
}
