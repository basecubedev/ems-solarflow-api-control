// SPDX-License-Identifier: AGPL-3.0-or-later
// Docs-preview driver for the Admin Console screenshots.
//
// Injected after admin.js by scripts/serve_admin_docs_preview.py. Classic
// scripts share one global lexical scope, so the Admin SPA's top-level
// functions and state (enterSetup, setActiveStep, authState, upgradeState, …)
// are reachable here by name. This reads ?screen=<name> and navigates the
// already-authenticated SPA to the documented view, then re-renders a few times
// so async fetches have settled before the screenshot is taken.
//
// It only navigates and re-renders; it never mutates config, runtime state or
// hardware, and all demo data comes from the preview server fixtures.
(function () {
  var screen = new URLSearchParams(window.location.search).get("screen") || "landing";

  function ready() {
    return typeof authState !== "undefined" && authState && authState.authenticated;
  }

  function releaseIsReady() {
    return typeof releaseReady === "function" && releaseReady();
  }

  // Poll until cond() is truthy (or timeout), then run act().
  function when(cond, act, tries) {
    tries = tries === undefined ? 120 : tries;
    if (cond()) {
      act();
      return;
    }
    if (tries <= 0) {
      act();
      return;
    }
    window.setTimeout(function () {
      when(cond, act, tries - 1);
    }, 40);
  }

  // Deep-link straight to a maintenance panel. Setting the final hash keeps the
  // async hashchange handler (applyHashRoute) from resetting the panel back to
  // the hub the way enterMaintenance() would.
  function openMaintenance(path) {
    if (typeof revealWorkspace === "function") revealWorkspace();
    window.location.hash = path === "hub" ? "maintenance" : "maintenance-" + path;
    if (typeof setAdminView === "function") setAdminView("maintenance");
    if (typeof setMaintenancePath === "function") setMaintenancePath(path);
  }

  function expandMaintenanceCards() {
    ["maintenance-layout", "maintenance-containers", "maintenance-versions"].forEach(
      function (id) {
        var card = document.getElementById(id);
        if (card && card.getAttribute("data-open") !== "true" &&
            typeof toggleMaintenanceCard === "function") {
          toggleMaintenanceCard(id);
        }
      }
    );
  }

  var drivers = {
    landing: function () {
      // The authenticated start gate is the default surface; nothing to do.
    },
    "guided-setup-start": function () {
      enterSetup();
    },
    discovery: function () {
      // Block the live scan so the deterministic demo devices (served from
      // /api/discovery/devices) are what render.
      if (typeof devicesDiscoveryStarted !== "undefined") devicesDiscoveryStarted = true;
      enterSetup();
      when(releaseIsReady, function () {
        setActiveStep("devices");
        if (typeof renderAggregate === "function") renderAggregate();
        window.setTimeout(function () {
          if (typeof renderAggregate === "function") renderAggregate();
        }, 300);
      });
    },
    "config-preview": function () {
      enterSetup();
      when(releaseIsReady, function () {
        setActiveStep("config");
      });
    },
    "maintenance-overview": function () {
      openMaintenance("manual");
      window.setTimeout(expandMaintenanceCards, 500);
    },
    "backup-restore": function () {
      openMaintenance("backup");
    },
    "guided-upgrade": function () {
      openMaintenance("upgrade");
      // Once the release list has settled, plan the upgrade so the full step
      // list is visible (the mutating "Upgrade EMS" button stays guarded).
      when(
        function () {
          return typeof upgradeState !== "undefined" && upgradeState.selected;
        },
        function () {
          upgradeState.planned = true;
          if (typeof renderUpgradePlan === "function") renderUpgradePlan();
        }
      );
    },
    "admin-update-reconnect": function () {
      // The reconnect overlay is a full-screen modal, so it does not depend on a
      // loaded panel underneath — show it directly for a deterministic capture.
      openMaintenance("upgrade");
      if (typeof showReconnectOverlay === "function") {
        showReconnectOverlay(
          "Admin Console update started. This page will reconnect automatically."
        );
      }
    },
  };

  function drive() {
    var run = drivers[screen] || drivers.landing;
    try {
      run();
    } catch (err) {
      /* preview driving is best-effort */
    }
    document.body.dataset.docsReady = "1";
  }

  // Re-assert the target screen a few times across the hold window. Every driver
  // is idempotent (navigate + re-render), so repeating defeats races where the
  // SPA's own async bootstrap would otherwise reset the view after the first
  // drive. Driving starts immediately (not on "load"); the preview server holds
  // the load event open so the final state renders before Firefox captures it.
  function driveLoop(remaining) {
    if (!ready()) {
      window.setTimeout(function () {
        driveLoop(remaining);
      }, 40);
      return;
    }
    drive();
    if (remaining > 0) {
      window.setTimeout(function () {
        driveLoop(remaining - 1);
      }, 300);
    }
  }
  driveLoop(8);
})();
