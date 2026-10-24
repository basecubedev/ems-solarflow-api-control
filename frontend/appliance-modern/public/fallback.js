/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* The deprecated Classic Appliance Manager answers under /classic/. This page
   sends a reader there before its first paint in the two cases Modern does not
   serve: ?ui=classic from a link made before Modern moved to the root, and a
   browser too old for Modern, which would land on a blank page or behind
   panels it cannot close. The Popover API is the newest thing Modern needs.
   It is a file of its own because the CSP is `script-src 'self'`. */
(function () {
  "use strict";

  function wantsClassic(search) {
    var parts = String(search || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < parts.length; i += 1) {
      var pair = parts[i].split("=");
      try {
        var key = decodeURIComponent(pair[0] || "").toLowerCase();
        var value = decodeURIComponent(pair[1] || "").toLowerCase();
        if (key === "ui" && value === "classic") return true;
      } catch (exc) {
        continue;
      }
    }
    return false;
  }

  var canRunModern = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;

  if (!canRunModern || wantsClassic(window.location.search)) {
    window.location.replace("/classic/" + window.location.search + window.location.hash);
  }
})();
