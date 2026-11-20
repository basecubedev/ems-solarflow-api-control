/* SPDX-FileCopyrightText: 2026 basecubedev */
/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Runs before the first paint and sends a browser too old for the app to a
   page that says so, instead of a blank page or panels it cannot close. The
   Popover API is the newest thing the app needs; no engine shipped it before
   the syntax and the modal dialogs the app also relies on, and the CSP rules
   out a syntax probe. It is a file of its own because the CSP is
   `script-src 'self'`, and plain ES5 so the oldest browser can run it. */
(function () {
  "use strict";
  var canRun = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;
  if (!canRun) window.location.replace("/unsupported.html");
})();
