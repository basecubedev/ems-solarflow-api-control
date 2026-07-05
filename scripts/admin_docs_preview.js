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

  // Guided-upgrade live-run steps, mirroring the plan order emitted by
  // admin/guided_upgrade.py with all options enabled. These only feed
  // renderUpgradeSteps so the "04 Upgrade validation" box can be shown
  // progressing through green check-marks; nothing is ever executed.
  var UPGRADE_RUN_STEPS = [
    { key: "verify_image", label: "Verify target image", done: "digest verified" },
    { key: "preflight", label: "Preflight checks", done: "environment OK" },
    { key: "backup", label: "Create backup", done: "snapshot saved" },
    { key: "config_check", label: "Check config", done: "compared to template" },
    { key: "config_write", label: "Update config", done: "3 missing keys added" },
    { key: "pull_image", label: "Pull image", done: "image downloaded" },
    { key: "update_compose", label: "Update compose", done: "image ref updated" },
    { key: "recreate_ems", label: "Recreate EMS", done: "container recreated" },
    { key: "diagnostics", label: "Run diagnostics", done: "health check passed" },
  ];

  // Live job "steps" payload with `doneCount` steps complete and the next one
  // running (rest pending) — the exact shape renderUpgradeSteps expects.
  function buildUpgradeRunSteps(doneCount) {
    return UPGRADE_RUN_STEPS.map(function (step, i) {
      var state = i < doneCount ? "done" : i === doneCount ? "running" : "pending";
      return {
        key: step.key,
        label: step.label,
        state: state,
        message: state === "done" ? step.done : null,
      };
    });
  }

  // Completed-result payload for renderUpgradeResult (all steps ok, green).
  function buildUpgradeResult() {
    return {
      ok: true,
      steps: UPGRADE_RUN_STEPS.map(function (step) {
        return { status: "ok", label: step.label, detail: step.done };
      }),
      warnings: [],
      target_release: "v0.7.0",
      target_image: "ghcr.io/example/ems-solarflow-api-control:v0.7.0",
    };
  }

  // The validation box shows Current/Target facts that renderUpgradePlan would
  // normally fill; once we neutralize that render, set them here so the box still
  // names the upgrade even in the live-run screens.
  function setUpgradeFacts() {
    if (typeof upgradeEls === "undefined") return;
    var cur = (typeof upgradeState !== "undefined" && upgradeState.current) || {};
    if (upgradeEls.factCurrent) {
      upgradeEls.factCurrent.textContent = cur.image || cur.tag || "Current version unknown";
    }
    if (upgradeEls.factTarget) upgradeEls.factTarget.textContent = "v0.7.0";
  }

  // Render the validation box in a live-run (doneCount) or completed (null) state.
  function applyUpgradeRun(doneCount) {
    if (doneCount === null) {
      if (typeof setUpgradeRunning === "function") setUpgradeRunning(false);
      if (typeof renderUpgradeResult === "function") {
        renderUpgradeResult(buildUpgradeResult());
      }
    } else {
      if (typeof setUpgradeRunning === "function") setUpgradeRunning(true);
      if (typeof renderUpgradeSteps === "function") {
        renderUpgradeSteps(buildUpgradeRunSteps(doneCount));
      }
    }
    setUpgradeFacts();
  }

  // Drive the guided-upgrade panel into a live-run state. The panel keeps
  // re-planning asynchronously (each openMaintenance re-runs loadUpgradePlanning),
  // so once the target release has loaded we neutralize renderUpgradePlan and take
  // ownership of the validation box, then keep re-asserting the step list so the
  // screenshot lands on it deterministically.
  function driveUpgradeRun(doneCount) {
    if (!driveUpgradeRun.opened) {
      driveUpgradeRun.opened = true;
      openMaintenance("upgrade");
    }
    when(
      function () {
        return typeof upgradeState !== "undefined" && upgradeState.selected;
      },
      function () {
        if (!driveUpgradeRun.armed) {
          driveUpgradeRun.armed = true;
          upgradeState.planned = true;
          // Stop the async planner from overwriting the live step list. Safe:
          // each screen is a separate page load, so this only affects the run.
          if (typeof renderUpgradePlan === "function") {
            renderUpgradePlan = function () {};
          }
          [300, 700, 1200, 2000, 2800].forEach(function (ms) {
            window.setTimeout(function () {
              applyUpgradeRun(doneCount);
            }, ms);
          });
        }
        applyUpgradeRun(doneCount);
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
        // Expand a few feature rows so the config page shows the feature list
        // opened up (with its settings), not just collapsed rows.
        if (typeof openFeatures !== "undefined" && openFeatures.add) {
          ["winter", "energy_savings", "battery_full_charge_assist"].forEach(function (id) {
            openFeatures.add(id);
          });
        }
        if (typeof renderFeatureSettings === "function") renderFeatureSettings();
      });
    },
    "setup-start-done": function () {
      // Drive the wizard to the final Start step showing the running EMS and the
      // "Open EMS Dashboard" (localhost:8080) success card. The deployment state
      // is set in memory so the Start step unlocks; the mocked deployment
      // plan/status endpoints then render the success state.
      enterSetup();
      when(releaseIsReady, function () {
        if (typeof setupState !== "undefined" && setupState.deployment) {
          var dep = setupState.deployment;
          dep.prepared = true;
          dep.generated_ready = true;
          dep.status = "succeeded";
          dep.docker = { state: "ready" };
          dep.workspace = "/opt/ems";
        }
        setActiveStep("start");
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
    // Live EMS software upgrade: the "04 Upgrade validation" box progressing
    // through green check-marks as each step completes (verify → preflight →
    // backup → config → add missing keys → pull → recreate → diagnostics).
    "upgrade-run-1": function () {
      driveUpgradeRun(2); // backup running
    },
    "upgrade-run-2": function () {
      driveUpgradeRun(4); // "Update config" (add missing keys) running
    },
    "upgrade-run-3": function () {
      driveUpgradeRun(5); // config keys added ✓, pulling image
    },
    "upgrade-run-4": function () {
      driveUpgradeRun(7); // recreating the EMS container
    },
    "upgrade-done": function () {
      driveUpgradeRun(null); // all steps done — upgrade completed
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
