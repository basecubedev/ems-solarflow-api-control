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

  /* The browser's choice between Classic and Modern (/modern/), decided here
     before the first paint, the way the dashboard's theme.js decides it.
     ?ui=classic or ?ui=modern changes the choice. Classic stays the default
     while Modern does not yet cover every workflow. An address that names a
     Classic page (#maintenance-settings and the like) is a hand-off from
     Modern to a page it does not have yet, so it opens here whatever the
     choice. A browser too old for Modern stays on Classic; the Popover API is
     the newest thing Modern needs. */
  var UI_KEY = "ems-admin-ui";
  var DEFAULT_UI = "classic";

  function chosenUi() {
    var asked = /(?:^|[?&])ui=(classic|modern)(?:&|$)/.exec(window.location.search);
    try {
      if (asked) window.localStorage.setItem(UI_KEY, asked[1]);
      var stored = window.localStorage.getItem(UI_KEY);
      if (stored === "classic" || stored === "modern") return stored;
    } catch (exc) {
      if (asked) return asked[1];
    }
    return asked ? asked[1] : DEFAULT_UI;
  }

  var ui = chosenUi();
  var canRunModern = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;
  var handOff = window.location.hash.length > 1;
  if (ui === "modern" && canRunModern && !handOff) {
    window.location.replace("/modern/");
  }
})();
