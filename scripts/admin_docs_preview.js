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

  // Wait for the SPA's own authenticated workflow resume to finish as well:
  // it re-opens Guided Setup on step 01, so a driver that ran before it would
  // be silently reset and every setup screen would capture the release step.
  function ready() {
    return (
      typeof authState !== "undefined" &&
      authState &&
      authState.authenticated &&
      (typeof authenticatedWorkflowResumeCompleted === "undefined" ||
        authenticatedWorkflowResumeCompleted)
    );
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

  // Like when(), but gives up silently instead of acting on an unmet condition.
  // Used where acting anyway would capture a different screen (a locked setup
  // step silently falls back to step 01) rather than visibly failing.
  function whenReady(cond, act, tries) {
    tries = tries === undefined ? 120 : tries;
    if (cond()) {
      act();
      return;
    }
    if (tries <= 0) return;
    window.setTimeout(function () {
      whenReady(cond, act, tries - 1);
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

  function expandMaintenanceCard(id) {
    var card = document.getElementById(id);
    if (card && card.getAttribute("data-open") !== "true" &&
        typeof toggleMaintenanceCard === "function") {
      toggleMaintenanceCard(id);
    }
  }

  function expandMaintenanceCards() {
    ["maintenance-layout", "maintenance-containers", "maintenance-versions"].forEach(
      expandMaintenanceCard
    );
  }

  // Open a Guided Setup step once the wizard itself reports it unlocked. A
  // locked step silently falls back to "release", which would make the capture a
  // duplicate of the release screen instead of failing.
  function stepScreen(step) {
    // Start only unlocks once the deployment step has loaded its plan, so that
    // step has to be opened first.
    var prerequisite = step === "start" ? "deployment" : null;
    function unlocked(target) {
      return (
        releaseIsReady() &&
        typeof stepLocked === "function" &&
        !stepLocked(target)
      );
    }
    return function () {
      enterSetup();
      if (prerequisite) {
        whenReady(
          function () {
            return unlocked(prerequisite);
          },
          function () {
            if (setupState.activeStep !== step) setActiveStep(prerequisite);
          }
        );
      }
      whenReady(
        function () {
          return unlocked(step);
        },
        function () {
          setActiveStep(step);
        }
      );
    };
  }

  // Open the manual-maintenance panel with exactly one card expanded, so each
  // documented card gets a focused screenshot instead of the whole panel.
  function maintenanceCardScreen(cardId) {
    return function () {
      openMaintenance("manual");
      expandMaintenanceCard(cardId);
      window.setTimeout(function () {
        expandMaintenanceCard(cardId);
      }, 500);
    };
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
    // The auth gate never carries demo credentials: the fields stay empty and
    // the preview only re-renders the gate the SPA already owns.
    "password-setup": function () {
      if (typeof showAuthView === "function") showAuthView("create");
    },
    login: function () {
      if (typeof showAuthView === "function") showAuthView("login");
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
    // Wait for the wizard's own unlock state instead of forcing it: the async
    // deployment plan/status fetches would otherwise overwrite hand-set values
    // and silently drop the capture back to step 01.
    "setup-deployment": stepScreen("deployment"),
    "setup-start-done": stepScreen("start"),
    "maintenance-hub": function () {
      openMaintenance("hub");
    },
    "maintenance-overview": function () {
      openMaintenance("manual");
      window.setTimeout(expandMaintenanceCards, 500);
    },
    "maintenance-diagnostics": maintenanceCardScreen("maintenance-diagnostics"),
    "maintenance-config-hardware": maintenanceCardScreen("maintenance-config-card"),
    "maintenance-mqtt": maintenanceCardScreen("maintenance-zendure-mqtt"),
    "maintenance-recovery": maintenanceCardScreen("maintenance-workflow-recovery"),
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
  // The loop deliberately outlasts HOLD_SECONDS, so a late async callback cannot
  // reset the view between the last drive and the screenshot.
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
  driveLoop(20);
})();
