/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Applies the stored palette, object style and density before the stylesheet
   paints, so the cockpit does not show the defaults first and visibly change a
   moment later. Three axes, three keys, three attributes, and none of them
   knows about the others. It is a file of its own rather than three lines
   inline because the CSP is `script-src 'self'`, and it is requested ahead of
   styles.css for the same reason it exists.

   This matters more here than on the other two surfaces: the cockpit is the one
   that gets left on a screen, and a flash on every reload is something a room
   notices.

   A stored name this build has no rules for simply matches nothing and the page
   keeps :root, so the shape check below guards the attribute value rather than
   the vocabulary. */
(function () {
  "use strict";

  var THEME_KEY = "ems-dashboard-theme";
  var STYLE_KEY = "ems-dashboard-style";
  var DENSITY_KEY = "ems-dashboard-density";

  function restore(key, attribute) {
    var stored = window.localStorage.getItem(key);
    if (stored && /^[a-z0-9-]{1,32}$/.test(stored)) {
      document.documentElement.setAttribute(attribute, stored);
    }
  }

  try {
    restore(THEME_KEY, "data-theme");
    restore(STYLE_KEY, "data-style");
    restore(DENSITY_KEY, "data-density");
  } catch (exc) {
    /* Private mode and blocked site data both throw here. The default palette is
       the right answer, not a failure before the first paint. */
  }
})();
