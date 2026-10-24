/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Applies the stored palette, object style and density before the stylesheet
   paints, so the page does not show the defaults first and visibly change a
   moment later. It is a file of its own rather than a handful of lines inline
   because the Admin CSP is `script-src 'self'`, and it is requested ahead of
   admin.css for the same reason it exists.

   Three axes, three keys, three attributes, and none of them knows about the
   others -- that is what lets any palette be worn with any object style at
   any density.

   A stored name this build has no rules for simply matches nothing and the page
   keeps :root, so the shape check below guards the attribute value rather than
   the vocabulary. */
(function () {
  "use strict";

  var THEME_KEY = "ems-admin-theme";
  var STYLE_KEY = "ems-admin-style";
  var DENSITY_KEY = "ems-admin-density";

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
    /* Private mode and blocked site data both throw here. The default theme is
       the right answer, not a failure before the first paint. */
  }
})();
