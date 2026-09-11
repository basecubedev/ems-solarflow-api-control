/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Applies the stored theme before the stylesheet paints, so the console does
   not show the default first and visibly change a moment later. It is a file of
   its own rather than three lines inline because the Manager's CSP is
   `script-src 'self'`, and it is requested ahead of styles.css for the same
   reason it exists.

   It runs on the sign-in gate too, which is the point: the owner reaching this
   console is usually here because something is wrong, and a page that flashes
   is one more thing that looks broken.

   A stored name this build has no rules for simply matches nothing and the page
   keeps :root, so the shape check below guards the attribute value rather than
   the vocabulary. */
(function () {
  "use strict";

  var STORAGE_KEY = "ems-appliance-theme";

  try {
    var stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored && /^[a-z0-9-]{1,32}$/.test(stored)) {
      document.documentElement.setAttribute("data-theme", stored);
    }
  } catch (exc) {
    /* Private mode and blocked site data both throw here. The default theme is
       the right answer, not a failure before the first paint. */
  }
})();
