/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Applies the stored palette, object style and density before the stylesheet
   paints, so the console does not show the defaults first and visibly change a
   moment later, and sends a browser that chose Modern there first. Three axes,
   three keys, three attributes, and none of them knows about the others. It is
   a file of its own rather than three lines inline because the Manager's CSP is
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

  var THEME_KEY = "ems-appliance-theme";
  var STYLE_KEY = "ems-appliance-style";
  var DENSITY_KEY = "ems-appliance-density";

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
     before the first paint, the way the Admin console's admin-theme.js decides
     it. ?ui=classic or ?ui=modern changes the choice; Modern is the default,
     as on the dashboard and the Admin console. A browser too old for Modern
     stays on Classic: the Popover API is the newest thing Modern needs. */
  var UI_KEY = "ems-appliance-ui";
  var DEFAULT_UI = "modern";

  /* Once stored, ?ui= has done its work and leaves the address, so coming back
     to this page later does not choose again what the reader has changed
     since. Where storage is blocked it stays, as the only carrier of the
     choice. */
  function forgetAsked() {
    var search = window.location.search.replace(/([?&])ui=(?:classic|modern)(&|$)/, function (match, lead, tail) {
      return tail ? lead : "";
    });
    window.history.replaceState(window.history.state, "", window.location.pathname + (search === "?" ? "" : search) + window.location.hash);
  }

  function chosenUi() {
    var asked = /(?:^|[?&])ui=(classic|modern)(?:&|$)/.exec(window.location.search);
    try {
      if (asked) {
        window.localStorage.setItem(UI_KEY, asked[1]);
        forgetAsked();
      }
      var stored = window.localStorage.getItem(UI_KEY);
      if (stored === "classic" || stored === "modern") return stored;
    } catch (exc) {
      if (asked) return asked[1];
    }
    return asked ? asked[1] : DEFAULT_UI;
  }

  var canRunModern = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;
  /* The page the reader was on travels along: a Modern page that reloads
     itself from here after a Manager update comes back to it. */
  if (chosenUi() === "modern" && canRunModern) {
    window.location.replace("/modern/" + window.location.hash);
  }
})();
