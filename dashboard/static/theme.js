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

  /* The browser's choice between Classic and Modern (/modern/). Decided here,
     before the first paint, so a reader who chose Modern never sees Classic
     flash past; ?ui=classic or ?ui=modern changes the choice. Two cases stay on
     Classic whatever the choice: demo mode, which only Classic has (read the
     way app.js reads it), and a browser too old for Modern, which would land on
     a blank page or behind panels it cannot close, with no way back. The
     Popover API is the newest thing Modern needs; no engine shipped it before
     the syntax and the modal dialogs Modern also relies on, and the CSP rules
     out a syntax probe. */
  var UI_KEY = "ems-dashboard-ui";
  var DEFAULT_UI = "modern";

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

  function demoMode(search) {
    var parts = String(search || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < parts.length; i += 1) {
      var pair = parts[i].split("=");
      try {
        var key = decodeURIComponent(pair[0] || "").toLowerCase();
        var value = decodeURIComponent(pair[1] || "").toLowerCase();
        if (key === "demo" && (value === "1" || value === "true")) return true;
      } catch (exc) {
        continue;
      }
    }
    return false;
  }

  var demo = demoMode(window.location.search);
  var canRunModern = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;

  if (window.location.pathname === "/" && !demo && canRunModern && chosenUi() === "modern") {
    window.location.replace("/modern/");
  }
})();
