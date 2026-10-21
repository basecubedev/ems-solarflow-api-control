// SPDX-License-Identifier: AGPL-3.0-or-later
import { getContext, setContext } from "svelte";

const SHELL = Symbol("shell");

/**
 * What the shared menu, top bar and phone bar need from the app that hosts
 * them, handed down once from its root component:
 *
 *   pageSet   createPageSet(...) of the app's pages
 *   nav       createRouter(pageSet): { router, href, setPinned }
 *   short     the word under the mark when the menu is folded
 *   locked    (page) => true when the page needs a sign-in first
 *   session   { authenticated, label, icon, pill, signIn(), signOut() }; pill
 *             false keeps the label out of the top bar (the phone sheet still
 *             shows it)
 *   classic   (pageId) => void, the switch to the Classic view
 *   links     [{ href, icon, label, testid }], opened in place
 */
export function provideShell(shell) {
  setContext(SHELL, shell);
  return shell;
}

export function useShell() {
  return getContext(SHELL);
}
