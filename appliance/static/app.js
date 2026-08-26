/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Appliance Manager UI.
   Every dynamic value is written with textContent or a created node; nothing
   from the host ever reaches innerHTML. Basic and Expert mode render the same
   backend state and differ only in which facts and actions are shown. */
(function () {
  "use strict";

  var MODE_KEY = "ems-appliance-mode";
  var VIEW_KEY = "ems-appliance-view";
  var POLL_INTERVAL = 2000;

  /* Cancel is only a legal transition out of a plan that has not started.
     Offering it during a running image write produced an internal-state alert
     rather than a cancellation; interrupting those needs a cooperative flag the
     executors poll, not a state flip. */
  var CANCELLABLE_STATES = ["planned", "awaiting_confirmation", "failed_recoverable"];

  var state = {
    authenticated: false,
    passwordConfigured: false,
    csrf: "",
    mode: "basic",
    view: "overview",
    data: {},
    operation: null,
    pending: null,
    pollTimer: null,
    busy: false,
    securityAudit: null
  };

  /* ---------------------------------------------------------------- DOM */

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = String(value);
        else if (key === "onclick") node.addEventListener("click", value);
        else if (key === "onsubmit") node.addEventListener("submit", value);
        else if (key === "onchange") node.addEventListener("change", value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, String(value));
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function announce(message) {
    document.getElementById("live-region").textContent = String(message || "");
  }

  function toneFor(level) {
    if (level === "ok" || level === "healthy" || level === true) return "tone tone-ok";
    if (level === "warn" || level === "attention") return "tone tone-warn";
    if (level === "bad" || level === "degraded" || level === false) return "tone tone-bad";
    return "tone tone-idle";
  }

  /* Status is never communicated by colour alone: every tone pill carries a
     text label as well. */
  function tone(level, label) {
    return el("span", { class: toneFor(level) }, [String(label)]);
  }

  function fact(label, value, options) {
    var opts = options || {};
    return el("div", { class: "fact-row" }, [
      el("span", { class: "fact-label", text: label }),
      el("span", { class: "fact-value" + (opts.mono ? " mono" : ""), text: format(value) })
    ]);
  }

  function format(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (value === true) return "yes";
    if (value === false) return "no";
    return String(value);
  }

  function card(title, children, testId) {
    return el("section", { class: "status-card", "data-test": testId || null }, [
      el("h3", { text: title })
    ].concat(children.filter(Boolean)));
  }

  function stage(step, title, subtitle, children, testId) {
    return el("section", { class: "control-stage", "data-test": testId || null }, [
      el("div", { class: "control-stage-head" }, [
        el("span", { class: "control-stage-step", "aria-hidden": "true", text: String(step) }),
        el("div", {}, [
          el("h3", { class: "control-stage-title", text: title }),
          subtitle ? el("p", { class: "control-stage-subtitle", text: subtitle }) : null
        ])
      ])
    ].concat(children.filter(Boolean)));
  }

  function expert() {
    return state.mode === "expert";
  }

  /* ---------------------------------------------------------------- API */

  function api(path, options) {
    var opts = options || {};
    var headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (state.csrf && opts.method && opts.method !== "GET") {
      headers["X-Appliance-CSRF"] = state.csrf;
    }
    if (opts.account) headers["X-Appliance-Account"] = opts.account;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      credentials: "same-origin",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var error = new Error(payload.message || "request failed");
          error.code = payload.error || String(response.status);
          error.status = response.status;
          if (response.status === 401 && state.authenticated) sessionLost();
          throw error;
        }
        return payload;
      });
    });
  }

  /* ------------------------------------------------------------ session */

  function showGate(firstRun) {
    document.getElementById("shell").hidden = true;
    document.getElementById("reconnect").hidden = true;
    var gate = document.getElementById("gate");
    gate.hidden = false;
    document.getElementById("gate-confirm-field").hidden = !firstRun;
    document.getElementById("gate-intro").textContent = firstRun
      ? "No appliance password exists yet. Create one to finish the first-run setup. It is independent from the EMS Admin password."
      : "Sign in to manage this Raspberry Pi appliance.";
    document.getElementById("gate-submit").textContent = firstRun ? "Create password" : "Sign in";
    document.getElementById("gate-password-label").textContent = firstRun
      ? "New appliance password"
      : "Appliance password";
    document.getElementById("gate-password").setAttribute(
      "autocomplete", firstRun ? "new-password" : "current-password"
    );
    document.getElementById("gate-password").focus();
  }

  function showShell() {
    document.getElementById("gate").hidden = true;
    document.getElementById("reconnect").hidden = true;
    document.getElementById("shell").hidden = false;
  }

  function loadSession() {
    return api("/api/session").then(function (payload) {
      state.authenticated = !!payload.authenticated;
      state.passwordConfigured = !!payload.password_configured;
      state.csrf = payload.csrf_token || "";
      state.securityAudit = payload.security_audit || null;
      return payload;
    });
  }

  /* The appliance must never imply an authentication event reached the
     authoritative audit log when the agent could not record it. */
  function renderAuditNotice() {
    var audit = state.securityAudit;
    if (!audit || !audit.degraded) return null;
    return el("div", { class: "warning-item severe", "data-test": "audit-degraded", role: "status" }, [
      el("strong", { text: "Security audit degraded: " }),
      el("span", {
        text: audit.message ||
          "Authentication events could not be written to the audit log."
      }),
      el("span", {
        class: "fact-value",
        text: " " + format(audit.unrecorded_events) + " event(s) unrecorded" +
          (audit.last_error ? " (" + audit.last_error + ")" : "")
      })
    ]);
  }

  function submitGate(event) {
    event.preventDefault();
    var error = document.getElementById("gate-error");
    error.hidden = true;
    var password = document.getElementById("gate-password").value;
    var confirmation = document.getElementById("gate-confirm").value;
    var firstRun = !state.passwordConfigured;
    var path = firstRun ? "/api/session/setup" : "/api/session/login";
    var body = firstRun ? { password: password, confirmation: confirmation } : { password: password };

    api(path, { method: "POST", body: body }).then(function (payload) {
      state.authenticated = true;
      state.passwordConfigured = true;
      state.csrf = payload.csrf_token || "";
      state.securityAudit = payload.security_audit || null;
      document.getElementById("gate-form").reset();
      showShell();
      renderNav();
      refresh();
    }).catch(function (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    });
  }

  function sessionLost() {
    state.authenticated = false;
    state.csrf = "";
    state.operation = null;
    stopPolling();
    showGate(false);
  }

  function logout() {
    api("/api/session/logout", { method: "POST", body: {} }).then(function () {
      state.authenticated = false;
      state.csrf = "";
      stopPolling();
      showGate(false);
    });
  }

  /* ------------------------------------------------------------- views */

  var VIEWS = [
    { id: "overview", label: "Overview", render: renderOverview },
    { id: "admin", label: "Admin", render: renderAdmin },
    { id: "updates", label: "System Updates", render: renderUpdates },
    { id: "network", label: "Network", render: renderNetwork },
    { id: "access", label: "SSH & Backup Access", render: renderAccess },
    { id: "diagnostics", label: "Diagnostics", render: renderDiagnostics },
    { id: "settings", label: "Settings", render: renderSettings }
  ];

  function renderNav() {
    var list = document.getElementById("nav-list");
    clear(list);
    VIEWS.forEach(function (view) {
      var button = el("button", {
        type: "button",
        class: "nav-button",
        "data-test": "nav-" + view.id,
        text: view.label,
        onclick: function () { selectView(view.id); }
      });
      if (view.id === state.view) button.setAttribute("aria-current", "page");
      list.appendChild(el("li", {}, [button]));
    });
  }

  function selectView(id) {
    state.view = id;
    try { window.localStorage.setItem(VIEW_KEY, id); } catch (exc) { /* private mode */ }
    renderNav();
    render();
  }

  function setMode(mode) {
    state.mode = mode;
    try { window.localStorage.setItem(MODE_KEY, mode); } catch (exc) { /* private mode */ }
    document.getElementById("mode-basic").setAttribute("aria-pressed", String(mode === "basic"));
    document.getElementById("mode-expert").setAttribute("aria-pressed", String(mode === "expert"));
    announce(mode === "expert" ? "Expert mode enabled" : "Basic mode enabled");
    render();
  }

  function render() {
    var main = document.getElementById("main");
    clear(main);
    var view = VIEWS.filter(function (item) { return item.id === state.view; })[0] || VIEWS[0];
    main.appendChild(renderOperationBanner());
    view.render(main);
  }

  /* --------------------------------------------------------- operations */

  function renderOperationBanner() {
    var wrapper = el("div", { "data-test": "operation-banner" });
    var operation = state.operation;
    if (!operation) return wrapper;

    var isTerminal = !!operation.terminal;
    var OUTCOMES = {
      succeeded: { level: "ok", label: "completed" },
      rolled_back: { level: "warn", label: "rolled back" },
      manual_action_required: { level: "warn", label: "manual action required" },
      failed_recoverable: { level: "bad", label: "incomplete" },
      failed_terminal: { level: "bad", label: "failed" },
      cancelled: { level: "idle", label: "cancelled" }
    };
    var outcome = OUTCOMES[operation.state] ||
      { level: isTerminal ? "bad" : "warn", label: operation.state.replace(/_/g, " ") };
    var level = outcome.level;

    var progress = el("ol", { class: "progress-list" },
      (operation.progress || []).slice(-6).map(function (entry) {
        return el("li", { class: "progress-item" }, [
          el("span", { text: entry.stage }),
          el("span", { text: entry.detail || "" })
        ]);
      }));

    var actions = [];
    if (isTerminal && !operation.acknowledged) {
      actions.push(el("button", {
        type: "button", class: "primary-button compact", "data-test": "acknowledge-operation",
        text: "Acknowledge", onclick: acknowledgeOperation
      }));
    }
    if (CANCELLABLE_STATES.indexOf(operation.state) !== -1) {
      actions.push(el("button", {
        type: "button", class: "ghost-button compact", "data-test": "cancel-operation",
        text: "Cancel", onclick: cancelOperation
      }));
    } else if (!isTerminal) {
      actions.push(el("p", {
        class: "control-result", "data-test": "operation-uninterruptible",
        text: "This operation cannot be interrupted."
      }));
    }

    wrapper.appendChild(stage(
      "!",
      "Current operation",
      operation.type + " · " + operation.stage,
      [
        el("div", { "data-test": "operation-outcome" }, [tone(level, outcome.label)]),
        operation.error ? el("p", { class: "control-result", text: operation.error.message }) : null,
        renderOperationDetails(operation),
        progress,
        el("div", { class: "control-stage-actions" }, actions)
      ],
      "operation-stage"
    ));
    return wrapper;
  }

  var VERIFICATION_LABELS = {
    container_missing: "the Admin container does not exist",
    container_not_running: "the Admin container is not running",
    container_still_running: "the Admin container is still running",
    container_unhealthy: "the Docker health check reports unhealthy",
    image_mismatch: "a different image than expected is running",
    api_unreachable: "the Admin web interface did not answer",
    version_unreadable: "the Admin version could not be read",
    version_mismatch: "the running Admin reports a different version"
  };

  /* A Docker command that returned 0 is not a verified Admin, so the failing
     fact is named instead of a generic "unhealthy". */
  function renderVerification(verification) {
    if (!verification || verification.verified) return null;
    var reasons = (verification.failures || []).map(function (code) {
      return el("li", { class: "warning-item", text: VERIFICATION_LABELS[code] || code });
    });
    var facts = [
      fact("Container", verification.container_state),
      fact("Admin endpoint", verification.api_reachable ? "answered" : "no answer"),
      fact("Reported version", verification.version)
    ];
    if (expert()) {
      facts.push(fact("Running digest", verification.active_digest, { mono: true }));
      facts.push(fact("Expected digest", verification.expected_digest, { mono: true }));
    }
    return el("div", { "data-test": "verification-failure" }, [
      el("p", { class: "control-stage-subtitle", text: "Verification failed:" }),
      el("ul", { class: "warning-list", "data-test": "verification-reasons" }, reasons)
    ].concat(facts));
  }

  function renderOperationDetails(operation) {
    var result = operation.result || {};
    var manual = result.manual_actions || [];
    var remaining = result.remaining_findings || [];
    var unverified = result.unverified_actions || [];
    var verification = renderVerification(result.verification);
    var untouched = result.admin_untouched === true;
    var replan = result.replan_required === true;
    var bootstrapFailed = result.bootstrap_failed === true;
    if (!manual.length && !remaining.length && !unverified.length && !verification && !untouched
        && !replan && !bootstrapFailed) {
      return null;
    }

    var wrapper = el("div", { "data-test": "operation-details" }, []);
    /* A preflight failure costs no downtime; saying so stops an operator from
       hunting for an outage that never happened. */
    if (untouched) {
      wrapper.appendChild(el("p", {
        class: "warning-item",
        "data-test": "admin-untouched",
        text: "Preflight failed before anything was changed. The Admin that was running is still running and the deployment files are unchanged."
      }));
    }
    /* A first installation failed. There was no Admin to keep running, so the
       one thing the operator needs is whether the deployment files now exist:
       that decides whether a retry installs or creates. */
    if (bootstrapFailed) {
      wrapper.appendChild(el("p", {
        class: "warning-item",
        "data-test": "admin-bootstrap-failed",
        text: result.deployment_created
          ? "The deployment files were created but Admin did not come up. This appliance still has no working Admin; installing again updates the deployment that now exists."
          : "Nothing was written. This appliance still has no Admin installation and no deployment files were created."
      }));
    }
    /* An OS update refused before the first destructive byte. The operator
       needs to know that nothing was written and that this plan is spent. */
    if (replan) {
      wrapper.appendChild(el("p", {
        class: "warning-item",
        "data-test": "ab-replan-required",
        text: "Nothing was written: the inactive slot is untouched and the boot default is "
          + "unchanged. The appliance is no longer the one this update was planned against, "
          + "so create a new update plan."
      }));
    }
    if (verification) wrapper.appendChild(verification);
    if (unverified.length) {
      wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: "Repair actions that could not be verified:" }));
      wrapper.appendChild(el("ul", { class: "warning-list", "data-test": "unverified-actions" },
        unverified.map(function (item) {
          return el("li", { class: "warning-item severe" }, [
            el("strong", { text: item.action + ": " }),
            el("span", { text: VERIFICATION_LABELS[item.result] || item.result })
          ]);
        })));
    }
    if (manual.length) {
      wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: "Do this yourself:" }));
      wrapper.appendChild(el("ul", { class: "warning-list", "data-test": "manual-actions" },
        manual.map(function (item) {
          return el("li", { class: "warning-item", text: String(item) });
        })));
    }
    if (remaining.length && expert()) {
      wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: "Still failing:" }));
      wrapper.appendChild(renderFindings(remaining));
    } else if (remaining.length) {
      wrapper.appendChild(el("p", {
        class: "warning-item severe",
        "data-test": "remaining-summary",
        text: remaining.length + " check(s) still report a problem."
      }));
    }
    return wrapper;
  }

  function pollOperations() {
    return api("/api/operations").then(function (payload) {
      var active = payload.active;
      var unacked = (payload.unacknowledged || [])[0] || null;
      state.operation = active || unacked;
      return payload;
    }).catch(function () { return null; });
  }

  /* The poll keeps an operation's progress live. Rebuilding the whole view for
     that destroys whatever the operator is typing, so while a field of theirs
     has focus only the banner is replaced. */
  function isEditing() {
    var active = document.activeElement;
    var main = document.getElementById("main");
    if (!active || !main || !main.contains(active)) return false;
    return /^(input|select|textarea)$/i.test(active.tagName);
  }

  function refreshBanner() {
    var main = document.getElementById("main");
    var existing = main && main.querySelector('[data-test="operation-banner"]');
    if (existing) main.replaceChild(renderOperationBanner(), existing);
  }

  function renderPolled() {
    if (isEditing()) {
      refreshBanner();
      return;
    }
    render();
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = window.setInterval(function () {
      pollOperations().then(renderPolled);
    }, POLL_INTERVAL);
  }

  function stopPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function acknowledgeOperation() {
    if (!state.operation) return;
    api("/api/operations/acknowledge", {
      method: "POST", body: { operation_id: state.operation.operation_id }
    }).then(function () {
      state.operation = null;
      announce("Operation acknowledged");
      refresh();
    }).catch(showToastError);
  }

  function cancelOperation() {
    if (!state.operation) return;
    api("/api/operations/cancel", {
      method: "POST", body: { operation_id: state.operation.operation_id }
    }).then(function () {
      announce("Operation cancelled");
      refresh();
    }).catch(showToastError);
  }

  function showToastError(exc) {
    announce("Error: " + exc.message);
    window.alert(exc.message);
  }

  /* Plan -> preview -> confirmation -> execution -> verification -> result */
  function planOperation(options) {
    if (state.busy) return;
    state.busy = true;
    api(options.endpoint, {
      method: "POST", body: options.body || {}, account: options.account
    }).then(function (payload) {
      state.busy = false;
      state.pending = payload;
      openDialog(options.title, payload, options.confirmLabel, options.danger);
    }).catch(function (exc) {
      state.busy = false;
      showToastError(exc);
      refresh();
    });
  }

  function openDialog(title, payload, confirmLabel, danger) {
    var backdrop = document.getElementById("dialog-backdrop");
    var body = document.getElementById("dialog-body");
    var error = document.getElementById("dialog-error");
    document.getElementById("dialog-title").textContent = title;
    error.hidden = true;
    clear(body);

    var plan = payload.plan || {};
    body.appendChild(renderPlan(plan));

    var confirm = document.getElementById("dialog-confirm");
    confirm.textContent = confirmLabel || "Confirm";
    confirm.className = danger ? "danger-button" : "primary-button";
    confirm.disabled = (plan.blockers || []).length > 0;

    backdrop.hidden = false;
    confirm.focus();
  }

  function renderPlan(plan) {
    var wrapper = el("div", {}, []);
    var rows = [];

    Object.keys(plan).forEach(function (key) {
      var value = plan[key];
      if (key === "blockers" || key === "findings" || key === "packages" || key === "keys") return;
      if (value === null || typeof value === "object") return;
      if (!expert() && (key === "target_digest" || key === "target_architecture" ||
        key === "current_digest" || key === "legacy_labels_accepted" ||
        key === "target_reference" || key === "target_source")) return;
      if (key === "bootstrap") return;
      rows.push(fact(key.replace(/_/g, " "), value, { mono: /digest|revision/.test(key) }));
    });
    wrapper.appendChild(el("div", { class: "control-result" }, rows));

    /* The only plan that creates files rather than editing them, so it says
       which files, before the operator confirms it. */
    if (plan.creates_deployment) {
      wrapper.appendChild(el("div", { class: "control-result", "data-test": "plan-creates-deployment" }, [
        el("p", { class: "control-stage-subtitle", text: "This creates the Admin deployment; nothing exists to replace." }),
        fact("compose file", plan.creates_deployment.compose_file, { mono: true }),
        fact("environment file", plan.creates_deployment.environment_file, { mono: true }),
        expert() ? fact("installer", plan.creates_deployment.installer, { mono: true }) : null,
        expert() ? fact("owner", plan.creates_deployment.owner_uid + ":" + plan.creates_deployment.owner_gid) : null
      ].filter(Boolean)));
    }

    (plan.blockers || []).forEach(function (blocker) {
      wrapper.appendChild(el("p", { class: "warning-item severe" }, [
        el("strong", { text: blocker.code + ": " }),
        el("span", { text: blocker.message })
      ]));
    });

    if ((plan.findings || []).length) {
      wrapper.appendChild(renderFindings(plan.findings));
    }

    if ((plan.packages || []).length) {
      wrapper.appendChild(renderPackageTable(plan.packages, 12));
    }

    if (plan.key) {
      wrapper.appendChild(el("div", { class: "control-result", "data-test": "plan-key" }, [
        fact("key type", plan.key.key_type),
        fact("comment", plan.key.comment),
        fact("fingerprint", plan.key.fingerprint, { mono: true })
      ]));
    }

    if ((plan.target || {}).admin_version) {
      wrapper.appendChild(el("div", { class: "control-result" }, [
        fact("target version", plan.target.admin_version),
        expert() ? fact("target digest", plan.target.admin_digest, { mono: true }) : null
      ].filter(Boolean)));
    }

    if (plan.warning) {
      wrapper.appendChild(el("p", { class: "warning-item", text: plan.warning }));
    }
    if ((plan.preserves || []).length) {
      wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: "Preserved: " + plan.preserves.join(", ") }));
    }
    return wrapper;
  }

  function closeDialog() {
    document.getElementById("dialog-backdrop").hidden = true;
    state.pending = null;
  }

  function confirmDialog() {
    var pending = state.pending;
    if (!pending) return;
    var error = document.getElementById("dialog-error");
    api("/api/operations/confirm", {
      method: "POST",
      body: {
        operation_id: pending.operation.operation_id,
        confirmation_token: pending.confirmation_token
      }
    }).then(function (payload) {
      state.operation = payload.operation;
      closeDialog();
      announce("Operation started");
      var type = (pending.plan || {}).type || "";
      if (type === "system.reboot" || type === "system.shutdown") showReconnect(type);
      render();
    }).catch(function (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    });
  }

  /* -------------------------------------------------------- reconnect */

  function showReconnect(type) {
    stopPolling();
    document.getElementById("shell").hidden = true;
    document.getElementById("reconnect").hidden = false;
    document.getElementById("reconnect-detail").textContent = type === "system.shutdown"
      ? "The appliance is shutting down. This page stays open; power the Raspberry Pi back on to continue."
      : "The appliance is restarting. This page checks automatically whether it is reachable again.";
    window.setTimeout(pollReconnect, 8000);
  }

  function pollReconnect() {
    if (document.getElementById("reconnect").hidden) return;
    api("/api/session").then(function () {
      document.getElementById("reconnect").hidden = true;
      boot();
    }).catch(function () {
      window.setTimeout(pollReconnect, 5000);
    });
  }

  /* ----------------------------------------------------------- refresh */

  function refresh() {
    if (!state.authenticated) return Promise.resolve();
    return Promise.all([
      api("/api/status").catch(function (exc) { return { error: exc.code }; }),
      pollOperations()
    ]).then(function (results) {
      state.data.status = results[0];
      render();
      startPolling();
    });
  }

  function loadInto(key, path) {
    return api(path).then(function (payload) {
      state.data[key] = payload;
      render();
      return payload;
    }).catch(function (exc) {
      state.data[key] = { error: exc.code, message: exc.message };
      render();
    });
  }

  function sectionOk(section) {
    return section && section.status === "ok";
  }

  /* ------------------------------------------------------------ views */

  function renderOverview(main) {
    var status = state.data.status || {};
    var health = status.health || {};
    var system = status.system || {};
    var docker = status.docker || {};
    var admin = status.admin || {};
    var updates = status.updates || {};
    var network = status.network || {};

    main.appendChild(el("h2", { class: "section-title", text: "Appliance overview" }));
    main.appendChild(el("p", {
      class: "section-hint",
      text: "Host management for this Raspberry Pi. EMS configuration and devices stay in the EMS Admin Console."
    }));

    var cards = el("div", { class: "card-grid" }, [
      card("Raspberry Pi", [
        el("p", { class: "status-value" }, [tone(health.level === "healthy" ? "ok" : (health.level === "degraded" ? "bad" : "warn"), health.level || "unknown")]),
        fact("Model", (system.hardware || {}).model),
        fact("OS", (system.operating_system || {}).name),
        fact("Uptime", (system.uptime || {}).days !== undefined ? (system.uptime.days + " days") : null),
        fact("Temperature", (system.temperature || {}).celsius ? system.temperature.celsius + " °C" : null),
        fact("Free storage", ((system.storage || {}).root || {}).free_mb ? Math.round(system.storage.root.free_mb / 1024) + " GB" : null)
      ], "card-host"),

      card("Docker", [
        el("p", { class: "status-value" }, [
          /* "unavailable" means Docker is not installed at all: an optional
             host feature, not a broken appliance. */
          tone(dockerTone(docker), (docker.daemon || {}).state || "unknown")
        ]),
        (docker.daemon || {}).state === "unavailable"
          ? el("p", { class: "control-stage-subtitle", text: "Docker is not installed; Admin container management is unavailable." })
          : null,
        expert() ? fact("Engine", (docker.daemon || {}).version) : null
      ], "card-docker"),

      card("EMS Admin", [
        el("p", { class: "status-value", text: admin.version || (admin.installed ? "unknown version" : "not installed") }),
        el("div", {}, [tone(admin.healthy ? "ok" : (admin.installed ? "bad" : "warn"), admin.installed ? (admin.healthy ? "healthy" : "unhealthy") : "missing")]),
        fact("Container", (admin.container || {}).state),
        expert() ? fact("Digest", admin.digest, { mono: true }) : null
      ], "card-admin"),

      card("EMS", [
        el("p", { class: "status-value" }, [tone(emsState(docker) === "running" ? "ok" : "warn", emsState(docker))]),
        fact("Container", emsContainerName(docker))
      ], "card-ems"),

      card("Updates", [
        el("p", { class: "status-value", text: String(updates.security_count === undefined ? "—" : updates.security_count) }),
        fact("Security updates", updates.security_count),
        fact("Other updates", updates.normal_count),
        fact("Reboot required", updates.reboot_required)
      ], "card-updates"),

      card("Network", [
        el("p", { class: "status-value", text: (network.hostname || "—") }),
        fact("mDNS", network.mdns),
        fact("Connectivity", network.connectivity),
        fact("Active connection", network.active_connection)
      ], "card-network")
    ]);
    main.appendChild(cards);

    main.appendChild(el("h2", { class: "section-title", text: "Warnings" }));
    var auditNotice = renderAuditNotice();
    if (auditNotice) main.appendChild(el("ul", { class: "warning-list" }, [auditNotice]));
    var warnings = health.warnings || [];
    if (status.error) {
      main.appendChild(el("p", {
        class: "empty-state", "data-test": "status-unavailable",
        text: "Appliance status is unavailable (" + format(status.error) + "). "
          + "Nothing below has been read from the appliance."
      }));
    } else if (!warnings.length && !auditNotice) {
      main.appendChild(el("p", { class: "empty-state", text: "No warnings. The appliance looks healthy." }));
    } else if (warnings.length) {
      main.appendChild(el("ul", { class: "warning-list", "data-test": "warnings" },
        warnings.map(function (warning) {
          var severe = /unhealthy|not_running|not_installed|storage_low|package_manager/.test(warning.code);
          return el("li", { class: "warning-item" + (severe ? " severe" : "") }, [
            el("strong", { text: warning.code.replace(/_/g, " ") + ": " }),
            el("span", { text: warning.message })
          ]);
        })));
    }

    main.appendChild(quickActions());
  }

  function dockerTone(docker) {
    var state = (docker.daemon || {}).state;
    if (state === "running") return "ok";
    if (state === "unavailable") return "warn";
    return "bad";
  }

  function emsState(docker) {
    var containers = docker.containers || [];
    for (var i = 0; i < containers.length; i += 1) {
      if (/ems-solarflow$|api-control/.test(containers[i].name)) return containers[i].state;
    }
    return "unknown";
  }

  function emsContainerName(docker) {
    var containers = docker.containers || [];
    for (var i = 0; i < containers.length; i += 1) {
      if (/ems-solarflow$|api-control/.test(containers[i].name)) return containers[i].name;
    }
    return null;
  }

  function quickActions() {
    /* Nothing is deployed yet: restarting and repairing have no target, so the
       one action offered is the one that applies. */
    var bootstrap = ((state.data.status || {}).admin || {}).bootstrap_required === true;
    return el("div", { class: "stage-grid" }, [
      stage(1, "Admin recovery", bootstrap
        ? "No EMS Admin is installed on this appliance yet"
        : "Restart, repair or update the EMS Admin container", [
        el("div", { class: "control-stage-actions" }, bootstrap ? [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "quick-install-admin",
            text: "Install Admin",
            onclick: function () { selectView("admin"); }
          })
        ] : [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "quick-restart-admin",
            text: "Restart Admin",
            onclick: function () {
              planOperation({ endpoint: "/api/admin/restart", title: "Restart the EMS Admin container" });
            }
          }),
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "quick-repair-admin",
            text: "Repair Admin",
            onclick: function () {
              planOperation({ endpoint: "/api/admin/repair", title: "Repair the EMS Admin deployment" });
            }
          }),
          el("button", {
            type: "button", class: "ghost-button compact", text: "Open Admin section",
            onclick: function () { selectView("admin"); }
          })
        ])
      ], "quick-admin"),

      stage(2, "Operating system", "Install pending security updates", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "quick-security-updates",
            text: "Install security updates",
            onclick: function () {
              planOperation({
                endpoint: "/api/updates/plan", body: { scope: "security" },
                title: "Install security updates", confirmLabel: "Install"
              });
            }
          })
        ])
      ], "quick-updates"),

      stage(3, "Power", "Restart or shut down the Raspberry Pi", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "quick-reboot",
            text: "Restart Raspberry Pi",
            onclick: function () {
              planOperation({ endpoint: "/api/system/reboot", title: "Restart the Raspberry Pi", confirmLabel: "Restart", danger: true });
            }
          }),
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "quick-shutdown",
            text: "Shut down",
            onclick: function () {
              planOperation({ endpoint: "/api/system/shutdown", title: "Shut down the Raspberry Pi", confirmLabel: "Shut down", danger: true });
            }
          })
        ])
      ], "quick-power")
    ]);
  }

  /* ------------------------------------------------------------- Admin */

  function renderAdmin(main) {
    var admin = (state.data.status || {}).admin || {};
    var releases = state.data.releases;
    if (releases === undefined) {
      state.data.releases = null;
      loadInto("releases", "/api/admin/releases");
    }

    main.appendChild(el("h2", { class: "section-title", text: "EMS Admin" }));

    if ((admin.transition || {}).state === "live") {
      main.appendChild(el("p", { class: "empty-state", "data-test": "admin-transition-live" }, [
        el("strong", { text: "The Admin console is replacing itself right now. " }),
        el("span", {
          text: "Installing, rolling back, repairing, starting, stopping and restarting are "
            + "refused until that finishes, because both would write the same deployment files. "
            + "Stage: " + (admin.transition.stage || "unnamed") + "."
        })
      ]));
    }

    if (admin.bootstrap_required) {
      renderAdminBootstrap(main, releases);
      return;
    }

    main.appendChild(el("p", {
      class: "section-hint",
      text: "Install, reinstall, restart, repair and roll back the EMS Admin container. The Appliance Manager stays reachable throughout."
    }));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Installed version", [
        el("p", { class: "status-value", text: admin.version || (admin.installed ? "unknown" : "not installed") }),
        el("div", {}, [tone(admin.healthy ? "ok" : (admin.installed ? "bad" : "warn"),
          admin.installed ? (admin.healthy ? "healthy" : "unhealthy") : "missing")]),
        fact("Container state", (admin.container || {}).state),
        fact("Health check", (admin.container || {}).health)
      ], "admin-version"),
      expert() ? card("Image", [
        fact("Repository", ((admin.image || {}).reference || "").split(":")[0], { mono: true }),
        fact("Digest", admin.digest, { mono: true }),
        fact("Revision", admin.revision, { mono: true }),
        fact("Architecture", (admin.image || {}).architecture),
        fact("Container ID", (admin.container || {}).container_id, { mono: true })
      ], "admin-image") : null,
      card("Known-good versions", [
        fact("Current", ((admin.known_good || {}).current || {}).admin_version),
        fact("Previous", ((admin.known_good || {}).previous || {}).admin_version),
        expert() ? fact("Previous digest", ((admin.known_good || {}).previous || {}).admin_digest, { mono: true }) : null
      ], "admin-known-good")
    ].filter(Boolean)));

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "Lifecycle", "Start, stop or restart the running container", [
        el("div", { class: "control-stage-actions" }, [
          lifecycleButton("start", "Start"),
          lifecycleButton("stop", "Stop"),
          lifecycleButton("restart", "Restart")
        ])
      ], "admin-lifecycle"),

      stage(2, "Install version", "Pull, validate and replace the Admin image", [
        renderInstallForm(releases)
      ], "admin-install"),

      stage(3, "Repair", "Inspect the deployment and preview the repair", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "admin-repair",
            text: "Preview repair",
            onclick: function () {
              planOperation({ endpoint: "/api/admin/repair", title: "Repair the EMS Admin deployment" });
            }
          })
        ])
      ], "admin-repair-stage"),

      stage(4, "Rollback", "Restore the previous known-good version", [
        el("p", { class: "control-stage-subtitle", text: "Rollback restores the recorded digest, not just a tag." }),
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "admin-rollback",
            text: "Roll back",
            disabled: !((admin.known_good || {}).previous),
            onclick: function () {
              planOperation({ endpoint: "/api/admin/rollback", title: "Roll back to the previous known-good Admin", confirmLabel: "Roll back" });
            }
          })
        ])
      ], "admin-rollback-stage")
    ]));

    main.appendChild(logPanel("admin_container", "Admin container log"));
  }

  /* No deployment exists yet: a freshly flashed appliance has an empty
     /opt/ems-solarflow. Lifecycle, repair and rollback have nothing to act on,
     so the page offers the one thing that applies — creating it. */
  function renderAdminBootstrap(main, releases) {
    main.appendChild(el("p", {
      class: "section-hint",
      text: "No EMS Admin installation was found on this appliance. Installing it creates the deployment under /opt/ems-solarflow and starts the version you choose. EMS itself is set up afterwards from Admin's own guided setup."
    }));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Installed version", [
        el("p", { class: "status-value", text: "not installed" }),
        el("div", {}, [tone("warn", "no deployment")]),
        fact("Deployment", "will be created on install")
      ], "admin-bootstrap-state")
    ]));

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "Install Admin", "Choose a version, then review the plan before anything is written", [
        renderInstallForm(releases, { bootstrap: true })
      ], "admin-bootstrap-install")
    ]));

    main.appendChild(logPanel("admin_container", "Admin container log"));
  }

  function lifecycleButton(action, label) {
    return el("button", {
      type: "button", class: "ghost-button compact", "data-test": "admin-" + action,
      text: label,
      onclick: function () {
        planOperation({ endpoint: "/api/admin/" + action, title: label + " the EMS Admin container" });
      }
    });
  }

  function renderInstallForm(releases, options) {
    var opts = options || {};
    var wrapper = el("div", { class: "inline-form" });
    /* A first installation has no current version and no known-good history,
       so only the channels that can resolve without one are offered. */
    var channelSelect = el("select", { id: "install-channel", "data-test": "install-channel" },
      opts.bootstrap
        ? [el("option", { value: "latest_stable", text: "Latest stable" })]
        : [
          el("option", { value: "latest_stable", text: "Latest stable" }),
          el("option", { value: "current", text: "Current stable (reinstall)" }),
          el("option", { value: "previous_known_good", text: "Previous known-good" })
        ]);
    if (expert()) {
      channelSelect.appendChild(el("option", { value: "exact", text: "Exact release tag" }));
    }

    var tagField = el("div", { class: "field", hidden: true }, [
      el("label", { for: "install-tag", text: "Release tag" }),
      el("input", { id: "install-tag", type: "text", "data-test": "install-tag", placeholder: "v0.8.0" })
    ]);
    channelSelect.addEventListener("change", function () {
      tagField.hidden = channelSelect.value !== "exact";
    });

    var reinstall = el("input", { id: "install-reinstall", type: "checkbox", "data-test": "install-reinstall" });

    wrapper.appendChild(el("div", { class: "field" }, [
      el("label", { for: "install-channel", text: "Version" }), channelSelect
    ]));
    wrapper.appendChild(tagField);
    if (!opts.bootstrap) {
      wrapper.appendChild(el("div", { class: "fact-row" }, [
        el("label", { for: "install-reinstall", text: "Reinstall the same version" }), reinstall
      ]));
    }

    if (releases && releases.error) {
      wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: "Release list unavailable: " + releases.error }));
    } else if (releases && (releases.available || []).length && expert()) {
      wrapper.appendChild(el("p", {
        class: "control-stage-subtitle",
        text: "Available: " + releases.available.slice(0, 8).map(function (item) { return item.tag; }).join(", ")
      }));
    }

    wrapper.appendChild(el("div", { class: "control-stage-actions" }, [
      el("button", {
        type: "button", class: "primary-button compact",
        "data-test": opts.bootstrap ? "admin-bootstrap-plan" : "install-plan",
        text: opts.bootstrap ? "Install Admin" : "Plan installation",
        onclick: function () {
          var body = { channel: channelSelect.value, reinstall: opts.bootstrap ? false : reinstall.checked };
          if (channelSelect.value === "exact") {
            body.tag = document.getElementById("install-tag").value.trim();
          }
          planOperation({ endpoint: "/api/admin/plan-install", body: body, title: "Install EMS Admin", confirmLabel: "Install" });
        }
      })
    ]));
    return wrapper;
  }

  /* ----------------------------------------------------------- Updates */

  function renderUpdates(main) {
    var updates = (state.data.status || {}).updates || {};
    var ab = updates.ab || {};

    renderManagerUpdates(main);

    if (updates.update_mode === "ab_image" || ab.ab_supported) {
      renderAbUpdates(main, ab);
      return;
    }
    renderPackageUpdates(main, updates, ab);
  }

  /* An A/B image-managed appliance stages host images into the inactive slot.
     Running apt through the UI there would create slot drift and could vanish
     after a rollback, so package installation is not the normal path. */
  /* Every state the backend can actually prove, and no state it cannot. The
     label comes from backend authority alone; nothing here infers progress. */
  function abLifecycle(ab) {
    var abState = ab.ab_state || {};
    var pending = abState.pending_trial;
    var fallback = abState.last_fallback;

    if (ab.mode === "single_slot") {
      /* A supported shape working as designed, not a degraded A/B one: this is
         either a package installation on an existing system or a single-slot
         appliance image, and both are patched with apt on purpose. What it
         genuinely does not have is a slot to fall back to, and that is what
         the hint says instead of calling the appliance incomplete. */
      return { tone: "ok", label: "Single-slot appliance",
        hint: "One root filesystem, patched with apt. There is no second slot to fall back "
          + "to, so keep a backup." };
    }
    if ((ab.drift || []).length || !ab.ab_supported) {
      return { tone: "bad", label: "Manual action required",
        hint: "The A/B layout could not be proven, so every OS mutation is disabled." };
    }
    if (fallback && !fallback.acknowledged) {
      return { tone: "bad", label: "Fallback observed",
        hint: "A trial slot did not commit and this appliance returned to its previous slot." };
    }
    if (ab.tryboot && pending && pending.committed) {
      return { tone: "ok", label: "Committed",
        hint: "The trial slot proved itself and is now the default." };
    }
    if (ab.tryboot && pending) {
      return { tone: "warn", label: "Trial boot active — health checking",
        hint: "This slot is running as a one-shot trial and is verifying itself." };
    }
    if (ab.tryboot) {
      return { tone: "bad", label: "Manual action required",
        hint: "This slot booted as a trial but no A/B operation is pending." };
    }
    if (pending && !pending.committed) {
      return { tone: "warn", label: "Trial reboot pending",
        hint: "An update is staged in the inactive slot and armed for a one-shot trial boot." };
    }
    return { tone: "ok", label: "A/B appliance ready",
      hint: "Both slots are proven and no operation is in flight." };
  }

  /* Every production prerequisite the backend can prove, in the order an
     operator would fix them. The plan action stays disabled while any of them
     is false: an update that cannot be decoded, written or recovered from is
     not one to offer. */
  var AB_READINESS = [
    ["hardware_supported", "Hardware", "This board is not one this appliance has an image for."],
    ["layout_ready", "A/B layout", "The layout could not be proven."],
    ["persistence_ready", "Persistent data", "A shared path is not backed by the persistent partition."],
    ["artifact_decoder_ready", "Artifact decoder", "zstd is missing, so a .tar.zst artifact cannot be read."],
    ["sparse_decoder_ready", "Sparse decoder", "Update members cannot be expanded."],
    ["host_identity_ready", "Host identity", "The persistent SSH host keys could not be proven."],
    ["release_keyring_ready", "Release keyring", "No OS release keyring is installed, so no update artifact can be verified and every one of them is refused."]
  ];

  /* Reported by the backend and shown on the EMS deployment card, but never a
     plan precondition: planning is what writes the runtime record both of them
     describe, so gating the plan action on them leaves a freshly flashed
     appliance unable to ever record one. The server still refuses a plan it
     cannot bind to a running Admin container, and drift is still a new plan. */
  var AB_INFORMATIONAL_READINESS = [
    "docker_reconstruction_ready",
    "deployment_authority_ready"
  ];

  /* The bounded deployment states the agent reports. Each one names what an
     operator does about it; there is deliberately no bypass in the browser,
     because a deployment the plan was not made against is a new plan. */
  var AB_DEPLOYMENT_STATES = {
    deployment_authority_ready: { tone: "ok", label: "recorded",
      hint: "The compose file and environment this update was planned against are unchanged." },
    deployment_authority_drift: { tone: "bad", label: "changed",
      hint: "The EMS deployment changed after this OS update was planned. Create a new update plan before continuing." },
    deployment_authority_missing: { tone: "warn", label: "not recorded",
      hint: "No EMS deployment has been recorded yet, so a trial slot could not rebuild it." },
    runtime_seed_ready: { tone: "ok", label: "staged",
      hint: "Every recorded image is staged on the persistent partition." },
    runtime_seed_incomplete: { tone: "warn", label: "incomplete",
      hint: "An image is missing from the staged set; the trial slot would have to reach a registry." },
    application_reconstruction_ready: { tone: "ok", label: "ready",
      hint: "A trial slot can rebuild this appliance without a network." },
    application_reconstruction_incomplete: { tone: "warn", label: "not ready",
      hint: "A trial slot could not rebuild every recorded service." }
  };

  function deploymentState(value) {
    return AB_DEPLOYMENT_STATES[value] || { tone: "warn", label: format(value), hint: "" };
  }

  var AB_SERVICE_STATES = {
    running: "running",
    stopped_clean: "stopped",
    absent: "not deployed",
    failed: "failed",
    restarting: "restarting",
    created: "never started",
    unknown: "unknown"
  };

  function abReadiness(ab) {
    var readiness = ab.readiness || {};
    var missing = AB_READINESS.filter(function (entry) {
      return AB_INFORMATIONAL_READINESS.indexOf(entry[0]) === -1
        && readiness[entry[0]] === false;
    });
    return { ready: missing.length === 0, missing: missing, readiness: readiness };
  }

  /* What the configured index offers. An index entry is a candidate and
     nothing more: the signature over the release manifest is what decides
     whether the download may be kept, so nothing here is presented as
     trustworthy. */
  function releaseLabel(entry) {
    // What an operator picks a release by. The index is never trusted for a
    // decision, but choosing between several published releases needs something
    // to choose by, and a column of identifiers is not it. Falls back to the
    // identifier when the index says nothing, which is what it used to show.
    var described = entry.described || entry;
    var version = described.release_version;
    if (!version) {
      return entry.release_id;
    }
    var parts = [version];
    var day = String(described.created_at || "").slice(0, 10);
    if (day) {
      parts.push(day);
    }
    if (described.board) {
      parts.push(described.board);
    }
    return parts.join(" · ");
  }

  function renderAbSources(sources, ab) {
    if (sources === null || sources === undefined) {
      return el("p", { class: "control-stage-subtitle", text: "Reading the release index\u2026" });
    }
    if (!sources.configured) {
      return el("p", { class: "control-stage-subtitle", "data-test": "ab-sources-unconfigured" }, [
        el("span", {
          text: "No release index is configured, so this appliance cannot download an OS image. "
            + "Set os_release_index_url in appliance.conf, or place a signed release in the "
            + "release directory yourself."
        })
      ]);
    }
    if (sources.error) {
      return el("p", { class: "control-stage-subtitle", "data-test": "ab-sources-error" }, [
        el("strong", { text: "The release index could not be read: " }),
        el("span", { text: sources.error })
      ]);
    }
    var offered = (sources.releases || []).filter(function (entry) { return !entry.present; });
    if (!offered.length) {
      return el("p", { class: "control-stage-subtitle", "data-test": "ab-sources-empty",
        text: "The release index offers nothing this appliance does not already have." });
    }
    return el("div", {}, [
      el("div", { class: "control-stage-actions" }, offered.map(function (entry) {
        return el("button", {
          type: "button", class: "primary-button compact",
          "data-test": "ab-plan-fetch",
          "data-release": entry.release_id,
          title: entry.release_id,
          text: "Download " + releaseLabel(entry),
          disabled: !ab.may_mutate,
          onclick: function () {
            planOperation({
              endpoint: "/api/ab/plan-fetch",
              body: { release_id: entry.release_id },
              title: "Download the OS release " + entry.release_id,
              confirmLabel: "Download"
            });
          }
        });
      })),
      el("p", {
        class: "control-stage-subtitle",
        text: "The download size is only known once the release manifest has been fetched and "
          + "its signature verified against this appliance's keyring. A release that fails that "
          + "check is discarded and never appears in the list above."
      })
    ]);
  }

  var MANAGER_DIRECTIONS = {
    upgrade: "newer",
    downgrade: "older",
    reinstall: "same version",
    revert: "the kept package"
  };

  var MANAGER_VERDICTS = {
    confirmed: ["ok", "the install proved itself and the deadline was retired"],
    reverted: ["warn", "the install did not prove itself in time, so the previous package was put back"],
    revert_failed: ["bad", "the install did not prove itself and the previous package could not be installed either"],
    revert_unavailable: ["bad", "the install did not prove itself and this appliance had kept no earlier package"]
  };

  function managerLabel(entry) {
    var described = entry.described || entry;
    var version = described.release_version;
    if (!version) {
      return entry.release_id;
    }
    var parts = [version];
    var day = String(described.created_at || "").slice(0, 10);
    if (day) {
      parts.push(day);
    }
    var direction = MANAGER_DIRECTIONS[entry.direction];
    if (direction) {
      parts.push(direction);
    }
    return parts.join(" \u00b7 ");
  }

  /* Every enable/disable decision this section makes, in one place and derived
     from the backend payload alone. A deadline in flight is the one state that
     blocks both buttons: a second install would replace the package whose
     verdict the appliance is still waiting for. */
  function managerActions(manager) {
    var verify = (manager || {}).verify || {};
    var armed = verify.armed === true;
    return {
      armed: armed,
      canUpdate: !armed,
      canRevert: (manager || {}).can_revert === true && !armed
    };
  }

  function renderManagerSources(sources, manager) {
    if (sources === null || sources === undefined) {
      return el("p", { class: "control-stage-subtitle", text: "Reading the package index\u2026" });
    }
    if (!sources.configured) {
      return el("p", { class: "control-stage-subtitle", "data-test": "manager-sources-unconfigured",
        text: "No manager package index is configured, so this appliance cannot download one. "
          + "Set manager_index_url in appliance.conf, or install the package by hand." });
    }
    if (sources.error) {
      return el("p", { class: "control-stage-subtitle", "data-test": "manager-sources-error" }, [
        el("strong", { text: "The package index could not be read: " }),
        el("span", { text: sources.error })
      ]);
    }
    var offered = sources.releases || [];
    if (!offered.length) {
      return el("p", { class: "control-stage-subtitle", "data-test": "manager-sources-empty",
        text: "The package index offers nothing." });
    }
    return el("div", {}, [
      el("div", { class: "control-stage-actions" }, offered.map(function (entry) {
        return el("button", {
          type: "button", class: "primary-button compact",
          "data-test": "manager-plan-update",
          "data-release": entry.release_id,
          "data-direction": entry.direction || "",
          title: entry.release_id,
          text: "Install " + managerLabel(entry),
          disabled: !managerActions(manager).canUpdate,
          onclick: function () {
            planOperation({
              endpoint: "/api/manager/plan-update",
              body: { release_id: entry.release_id },
              title: "Install the Appliance Manager package " + entry.release_id,
              confirmLabel: "Install",
              danger: entry.direction === "downgrade"
            });
          }
        });
      })),
      el("p", {
        class: "control-stage-subtitle",
        text: "An older package installs as readily as a newer one: going back is the recovery "
          + "this path provides. The size and digest come from the release manifest after its "
          + "signature has been verified against this appliance's keyring."
      })
    ]);
  }

  /* The Appliance Manager is the package the console itself runs from, so it
     is shown on every appliance -- A/B image-managed or single-slot. It never
     updates on its own: an automatic update would distribute an untested
     package to every appliance at once. */
  function renderManagerUpdates(main) {
    var manager = state.data.manager;
    if (manager === undefined) {
      state.data.manager = null;
      loadInto("manager", "/api/manager");
      manager = null;
    }
    if (manager === null) {
      main.appendChild(el("h2", { class: "section-title", text: "Appliance Manager" }));
      main.appendChild(el("p", { class: "section-hint", text: "Reading the manager state\u2026" }));
      return;
    }
    if (manager.error) {
      main.appendChild(el("h2", { class: "section-title", text: "Appliance Manager" }));
      main.appendChild(el("p", { class: "empty-state", "data-test": "manager-unavailable" }, [
        el("strong", { text: "The manager state is unavailable: " }),
        el("span", { text: format(manager.error) })
      ]));
      return;
    }

    var retention = manager.retention || {};
    var kept = retention.previous || {};
    var verify = manager.verify || {};
    var verdict = manager.verdict || {};

    main.appendChild(el("h2", { class: "section-title", text: "Appliance Manager" }));
    main.appendChild(el("p", {
      class: "section-hint",
      text: "The package this console runs from. It updates only when you ask it to, and the "
        + "same control installs an older package as readily as a newer one."
    }));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Installed", [
        el("p", { class: "status-value", text: format(manager.installed_version) }),
        fact("Package index", manager.configured ? "configured" : "not configured")
      ], "manager-installed"),
      card("Kept package", [
        el("p", { class: "status-value" }, [
          manager.can_revert ? tone("ok", format(kept.version)) : tone("warn", "none kept")
        ]),
        fact("Build", kept.build_id),
        expert() ? fact("Digest", kept.sha256, { mono: true }) : null
      ], "manager-kept"),
      card("Last install", [
        el("p", { class: "status-value" }, [
          verify.armed
            ? tone("warn", "waiting for the deadline")
            : (verdict.settled
                ? tone(MANAGER_VERDICTS[verdict.verdict] ? MANAGER_VERDICTS[verdict.verdict][0] : "warn",
                       format(verdict.verdict))
                : tone("ok", "nothing in flight"))
        ]),
        fact("Result", (manager.outcome || {}).outcome),
        verify.armed ? fact("Expecting", verify.expected_version) : null
      ], "manager-verify")
    ]));

    if (verify.armed) {
      main.appendChild(el("p", { class: "empty-state", "data-test": "manager-deadline" }, [
        el("strong", { text: "An install is being judged. " }),
        el("span", {
          text: "This appliance is waiting for " + format(verify.expected_version)
            + " to prove itself. If no healthy result appears before the deadline, the previous "
            + "package is installed again. Nothing else can be started until it settles."
        })
      ]));
    } else if (verdict.settled && verdict.verdict !== "confirmed") {
      main.appendChild(el("p", { class: "empty-state", "data-test": "manager-verdict" }, [
        el("strong", { text: "The last manager install " }),
        el("span", {
          text: (MANAGER_VERDICTS[verdict.verdict] || ["warn", format(verdict.verdict)])[1] + "."
        })
      ]));
    }

    var sources = state.data.managerSources;
    if (sources === undefined) {
      state.data.managerSources = null;
      loadInto("managerSources", "/api/manager/sources");
    }

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "Update the Appliance Manager", "Fetched over HTTPS, then verified against the appliance keyring", [
        renderManagerSources(sources, manager)
      ], "manager-stage-update"),
      stage(2, "Go back to the kept package", "The package this appliance ran before the last install", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "manager-plan-revert",
            text: "Reinstall " + (manager.can_revert ? format(kept.version) : "\u2014"),
            disabled: !managerActions(manager).canRevert,
            onclick: function () {
              planOperation({
                endpoint: "/api/manager/plan-revert", body: {},
                title: "Go back to Appliance Manager " + format(kept.version),
                confirmLabel: "Reinstall",
                danger: true
              });
            }
          })
        ]),
        el("p", {
          class: "control-stage-subtitle",
          text: "Every install arms a deadline first. If the new package does not report itself "
            + "healthy in time, this happens by itself -- doing nothing does not confirm an "
            + "install here."
        })
      ], "manager-stage-revert")
    ]));
  }

  function renderAbUpdates(main, ab) {
    var abState = ab.ab_state || {};
    var selector = ab.selector || {};
    var pending = abState.pending_trial;
    var fallback = abState.last_fallback;
    var releases = ab.releases || [];
    var lifecycle = abLifecycle(ab);
    var readiness = abReadiness(ab);

    main.appendChild(el("h2", { class: "section-title", text: "Operating-system image" }));
    main.appendChild(el("p", { class: "section-hint", "data-test": "ab-lifecycle" }, [
      tone(lifecycle.tone, lifecycle.label),
      el("span", { text: " " + lifecycle.hint })
    ]));
    main.appendChild(el("p", {
      class: "section-hint",
      text: "This appliance uses fail-safe A/B OS images. Host updates are staged into the "
        + "inactive slot and tested before becoming active."
    }));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Current slot", [
        el("p", { class: "status-value", text: format(ab.active_slot) }),
        fact("Current OS build", (ab.os_build || {}).build_id),
        fact("Current OS version", (ab.os_build || {}).release_version)
      ], "ab-current-slot"),
      card("Inactive slot", [
        el("p", { class: "status-value", text: format(ab.inactive_slot) }),
        fact("Last known-good slot", abState.known_good_slot),
        fact("Rollback candidate", abState.previous_slot)
      ], "ab-inactive-slot"),
      card("Trial status", [
        el("p", { class: "status-value" }, [
          ab.tryboot
            ? tone("warn", "trial boot running")
            : (pending ? tone("warn", "trial pending") : tone("ok", "no trial in flight"))
        ]),
        fact("Trial target slot", pending ? pending.target_slot : "—"),
        fact("Committed", pending ? pending.committed : "—")
      ], "ab-trial"),
      card("Persistent data", [
        el("p", { class: "status-value" }, [
          (ab.persistence || {}).ok ? tone("ok", "shared") : tone("bad", "not shared")
        ]),
        fact("Layout", ab.may_mutate ? "proven" : "not proven"),
        expert() ? fact("Default boot partition", selector.default_partition) : null
      ], "ab-persistence"),
      card("Update readiness", [
        el("p", { class: "status-value" }, [
          readiness.ready
            ? tone("ok", "ready")
            : tone("bad", readiness.missing.length + " prerequisite" + (readiness.missing.length === 1 ? "" : "s") + " missing")
        ])
      ].concat(AB_READINESS.map(function (entry) {
        var value = readiness.readiness[entry[0]];
        return fact(entry[1], value === undefined ? "—" : (value ? "ready" : "missing"));
      })), "ab-readiness")
    ]));

    var deployment = ab.deployment || {};
    var authority = deploymentState(deployment.authority);
    main.appendChild(el("div", { class: "card-grid" }, [
      card("EMS deployment", [
        el("p", { class: "status-value" }, [tone(authority.tone, authority.label)]),
        fact("Image staging", deploymentState(deployment.seed).label),
        fact("Trial recovery", deploymentState(deployment.reconstruction).label),
        expert() && deployment.seed_bytes
          ? fact("Staged image size", Math.round(deployment.seed_bytes / (1024 * 1024)) + " MB")
          : null
      ].concat((deployment.services || []).map(function (entry) {
        return fact(format(entry.role), AB_SERVICE_STATES[entry.state] || format(entry.state));
      })), "ab-deployment")
    ]));

    if (deployment.authority === "deployment_authority_drift") {
      main.appendChild(el("p", { class: "empty-state", "data-test": "ab-deployment-drift" }, [
        el("strong", { text: "The EMS deployment changed after this OS update was planned. " }),
        el("span", { text: "Create a new update plan before continuing." })
      ]));
    }

    if (!readiness.ready) {
      main.appendChild(el("p", { class: "empty-state", "data-test": "ab-not-ready" }, [
        el("strong", { text: "OS updates are unavailable: " }),
        el("span", { text: readiness.missing.map(function (entry) { return entry[2]; }).join(" ") })
      ]));
    }

    if ((ab.drift || []).length) {
      main.appendChild(el("p", { class: "empty-state", "data-test": "ab-drift" }, [
        el("strong", { text: "OS updates are disabled: " }),
        el("span", { text: (ab.drift || []).join("; ") })
      ]));
    }

    if (fallback && !fallback.acknowledged) {
      main.appendChild(el("p", { class: "empty-state", "data-test": "ab-fallback" }, [
        el("strong", { text: "Automatic fallback: " }),
        el("span", {
          text: "slot " + fallback.target_slot + " did not commit, so this appliance returned to "
            + "slot " + fallback.source_slot + ". Nothing is retried automatically."
        }),
        el("button", {
          type: "button", class: "ghost-button compact", "data-test": "ab-acknowledge",
          text: "Acknowledge",
          onclick: function () {
            api("/api/ab/acknowledge", {
              method: "POST", body: { operation_id: fallback.operation_id }
            }).then(function () { refresh(); });
          }
        })
      ]));
    }

    var stages = [];
    var step = 0;
    function nextStep() { step += 1; return step; }

    /* Nothing can be installed that is not here yet. The download comes first
       because on a fresh appliance the release list below is empty and the
       operator would otherwise have no way to fill it. */
    var sources = state.data.abSources;
    if (sources === undefined) {
      state.data.abSources = null;
      loadInto("abSources", "/api/ab/sources");
    }
    stages.push(stage(nextStep(), "Download an OS release", "Fetched over HTTPS, then verified against the appliance keyring", [
      renderAbSources(sources, ab)
    ], "ab-stage-fetch"));

    var installable = releases.filter(function (release) { return release.signed; });
    stages.push(stage(nextStep(), "OS image update", "Staged into the inactive slot, then trial-booted", [
      installable.length
        ? el("div", { class: "control-stage-actions" }, installable.map(function (release) {
            return el("button", {
              type: "button", class: "primary-button compact",
              "data-test": "ab-plan-update",
              "data-release": release.release_id,
              title: release.release_id,
              text: "Plan " + releaseLabel(release),
              disabled: !ab.may_mutate || !readiness.ready,
              onclick: function () {
                planOperation({
                  endpoint: "/api/ab/plan-update",
                  body: { release_id: release.release_id },
                  title: "Update the operating system to " + release.release_version,
                  confirmLabel: "Stage and trial-boot"
                });
              }
            });
          }))
        : el("p", { class: "control-stage-subtitle", text: "No signed OS image is available." }),
      el("p", {
        class: "control-stage-subtitle",
        text: "The trial boot is one-shot. If the new slot does not prove itself, the next "
          + "ordinary boot returns to the current slot with nothing changed."
      })
    ], "ab-stage-update"));

    stages.push(stage(nextStep(), "Roll back the operating system", "Only the recorded previous known-good slot", [
      el("div", { class: "control-stage-actions" }, [
        el("button", {
          type: "button", class: "ghost-button compact", "data-test": "ab-plan-rollback",
          text: "Plan rollback to slot " + (abState.previous_slot || "—"),
          disabled: !ab.may_mutate || !abState.previous_slot || !readiness.ready,
          onclick: function () {
            planOperation({
              endpoint: "/api/ab/plan-rollback", body: {},
              title: "Roll back to the previous known-good slot",
              confirmLabel: "Trial-boot the previous slot"
            });
          }
        })
      ]),
      el("p", {
        class: "control-stage-subtitle",
        text: "A rollback trial-boots the previous slot and commits it only when it proves itself."
      })
    ], "ab-stage-rollback"));

    if (expert()) {
      stages.push(stage(nextStep(), "Package-manager recovery", "Recovery only, not the normal update path", [
        el("div", { class: "control-stage-actions" }, [
          repairButton("configure_pending", "Complete pending configuration"),
          repairButton("fix_broken", "Repair dependencies")
        ]),
        el("p", {
          class: "control-stage-subtitle",
          text: "On an image-managed appliance a live package upgrade creates slot drift and can "
            + "disappear after a rollback. Use this only to repair a broken active slot."
        })
      ], "ab-stage-recovery"));
    }

    main.appendChild(el("div", { class: "stage-grid" }, stages));

    main.appendChild(el("p", { class: "empty-state" }, [
      el("strong", { text: "Host OS versus applications: " }),
      el("span", {
        text: "A/B applies to the Raspberry Pi operating system. The EMS Admin container keeps its "
          + "own image rollback, and EMS data keeps backup and restore."
      })
    ]));
  }

  function renderPackageUpdates(main, updates, ab) {
    var manager = updates.package_manager || {};

    main.appendChild(el("h2", { class: "section-title", text: "System updates" }));
    main.appendChild(el("p", {
      class: "section-hint",
      text: "Raspberry Pi OS packages. Major distribution upgrades are not performed here."
    }));
    main.appendChild(el("p", { class: "section-hint", "data-test": "ab-single-slot" }, [
      el("span", {
        text: "This appliance has a single root filesystem and is patched with apt. That is a "
          + "supported shape, not a missing feature — but there is no second slot to fall back "
          + "to, so a failed OS upgrade is recovered by reflashing and restoring a backup. "
          + "Fail-safe rollback needs an A/B-capable appliance image, which no installation is "
          + "converted to in place."
      })
    ]));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Security updates", [
        el("p", { class: "status-value", text: format(updates.security_count) }),
        el("div", {}, [tone(updates.security_count ? "warn" : "ok", updates.security_count ? "action recommended" : "up to date")])
      ], "updates-security"),
      card("Other updates", [
        el("p", { class: "status-value", text: format(updates.normal_count) }),
        fact("Kernel update", updates.kernel_update),
        fact("Firmware update", updates.firmware_update),
        fact("Held packages", (updates.held || []).length)
      ], "updates-normal"),
      card("Reboot", [
        el("p", { class: "status-value" }, [tone(updates.reboot_required ? "warn" : "ok", updates.reboot_required ? "required" : "not required")]),
        expert() ? fact("Triggered by", (updates.reboot_packages || []).join(", ")) : null
      ], "updates-reboot"),
      card("Package manager", [
        el("p", { class: "status-value" }, [tone(manager.healthy ? "ok" : "bad", manager.healthy ? "healthy" : "needs recovery")]),
        fact("Lock", manager.lock_state),
        expert() ? fact("dpkg issues", (manager.dpkg_issues || []).length) : null
      ], "updates-manager")
    ]));

    var stages = [
      stage(1, "Install security updates", "Only packages from a security archive", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "updates-install-security",
            text: "Plan security updates",
            onclick: function () {
              planOperation({ endpoint: "/api/updates/plan", body: { scope: "security" }, title: "Install security updates", confirmLabel: "Install" });
            }
          })
        ])
      ], "updates-stage-security")
    ];

    if (expert()) {
      stages.push(stage(2, "Install all updates", "Every available package upgrade", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "updates-install-all",
            text: "Plan all updates",
            onclick: function () {
              planOperation({ endpoint: "/api/updates/plan", body: { scope: "all" }, title: "Install all OS updates", confirmLabel: "Install" });
            }
          })
        ])
      ], "updates-stage-all"));

      stages.push(stage(3, "Package-manager recovery", "Strictly defined repair actions", [
        el("div", { class: "control-stage-actions" }, [
          repairButton("configure_pending", "Complete pending configuration"),
          repairButton("fix_broken", "Repair dependencies"),
          repairButton("refresh_index", "Refresh package indexes")
        ]),
        el("p", { class: "control-stage-subtitle", text: "An active package-manager lock is never removed." })
      ], "updates-stage-repair"));
    }

    main.appendChild(el("div", { class: "stage-grid" }, stages));

    if (expert() && (updates.security_updates || []).length) {
      main.appendChild(el("h2", { class: "section-title", text: "Pending security packages" }));
      main.appendChild(renderPackageTable(updates.security_updates, 100));
    }

    main.appendChild(el("p", { class: "empty-state" }, [
      el("strong", { text: "Major OS upgrade: " }),
      el("span", {
        text: "Create or export an EMS backup, flash the new supported appliance image, then restore the EMS backup."
      })
    ]));
  }

  function repairButton(action, label) {
    return el("button", {
      type: "button", class: "ghost-button compact", "data-test": "updates-repair-" + action,
      text: label,
      onclick: function () {
        planOperation({
          endpoint: "/api/updates/repair", body: { action: action },
          title: label, confirmLabel: "Run repair"
        });
      }
    });
  }

  function renderPackageTable(packages, limit) {
    var rows = packages.slice(0, limit).map(function (item) {
      return el("tr", {}, [
        el("td", { class: "mono", text: item.name }),
        el("td", { class: "mono", text: item.current_version || "—" }),
        el("td", { class: "mono", text: item.new_version || "—" }),
        el("td", {}, [tone(item.security ? "warn" : "idle", item.security ? "security" : "normal")])
      ]);
    });
    return el("div", { class: "table-wrap" }, [
      el("table", { class: "data", "data-test": "package-table" }, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "Package" }), el("th", { text: "Installed" }),
          el("th", { text: "Candidate" }), el("th", { text: "Origin" })
        ])]),
        el("tbody", {}, rows)
      ])
    ]);
  }

  /* ----------------------------------------------------------- Network */

  function renderNetwork(main) {
    var network = (state.data.status || {}).network || {};

    main.appendChild(el("h2", { class: "section-title", text: "Network" }));
    main.appendChild(el("p", {
      class: "section-hint",
      text: "A WLAN change can disconnect this session. The previous profile is kept and restored automatically when the new network fails."
    }));

    var cards = [
      card("Hostname", [
        el("p", { class: "status-value", text: network.hostname || "—" }),
        fact("mDNS name", network.mdns),
        fact("Connectivity", network.connectivity)
      ], "network-hostname")
    ];

    (network.interfaces || []).forEach(function (item) {
      var facts = [
        el("p", { class: "status-value" }, [tone(/connected/.test(item.state) ? "ok" : "idle", item.state)]),
        fact("Type", item.type),
        fact("Addresses", (item.addresses || []).join(", "))
      ];
      if (item.type === "wifi") facts.push(fact("SSID", item.ssid), fact("Signal", item.signal));
      if (expert()) facts.push(fact("Gateway", item.gateway), fact("DNS", (item.dns || []).join(", ")));
      cards.push(card(item.device, facts, "network-interface"));
    });

    main.appendChild(el("div", { class: "card-grid" }, cards));

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "WLAN", "Scan, select and apply with automatic revert", [renderWifiForm()], "network-wifi"),
      stage(2, "Hostname", "Changes the appliance and Admin URLs", [renderHostnameForm()], "network-hostname-stage"),
      stage(3, "Timezone", "Decides when the EMS opens an hour-based control window",
        [renderTimezoneForm()], "network-timezone-stage")
    ]));
  }

  function renderWifiForm() {
    var wrapper = el("div", { class: "inline-form" });
    var scan = state.data.wifi;

    var ssidInput = el("input", { id: "wifi-ssid", type: "text", "data-test": "wifi-ssid" });
    var passInput = el("input", { id: "wifi-pass", type: "password", "data-test": "wifi-pass", autocomplete: "new-password" });
    var hidden = el("input", { id: "wifi-hidden", type: "checkbox", "data-test": "wifi-hidden" });

    wrapper.appendChild(el("div", { class: "control-stage-actions" }, [
      el("button", {
        type: "button", class: "ghost-button compact", "data-test": "wifi-scan", text: "Scan networks",
        onclick: function () { loadInto("wifi", "/api/network/wifi/scan"); }
      })
    ]));

    if (scan && (scan.networks || []).length) {
      var select = el("select", { id: "wifi-select", "data-test": "wifi-select" },
        [el("option", { value: "", text: "Choose a network" })].concat(
          scan.networks.map(function (item) {
            return el("option", {
              value: item.ssid,
              text: item.ssid + " · " + (item.signal === null ? "?" : item.signal) + "% · " + item.security
            });
          })));
      select.addEventListener("change", function () { ssidInput.value = select.value; });
      wrapper.appendChild(el("div", { class: "field" }, [
        el("label", { for: "wifi-select", text: "Visible networks" }), select
      ]));
    }

    wrapper.appendChild(el("div", { class: "field" }, [el("label", { for: "wifi-ssid", text: "SSID" }), ssidInput]));
    wrapper.appendChild(el("div", { class: "field" }, [el("label", { for: "wifi-pass", text: "Passphrase" }), passInput]));
    wrapper.appendChild(el("div", { class: "fact-row" }, [
      el("label", { for: "wifi-hidden", text: "Hidden network" }), hidden
    ]));
    wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: "Stored passphrases are never shown again." }));
    wrapper.appendChild(el("div", { class: "control-stage-actions" }, [
      el("button", {
        type: "button", class: "primary-button compact", "data-test": "wifi-plan", text: "Plan WLAN change",
        onclick: function () {
          planOperation({
            endpoint: "/api/network/wifi/plan",
            body: { ssid: ssidInput.value, passphrase: passInput.value, hidden: hidden.checked },
            title: "Change the WLAN connection", confirmLabel: "Apply", danger: true
          });
        }
      })
    ]));
    return wrapper;
  }

  function renderHostnameForm() {
    var input = el("input", { id: "hostname-input", type: "text", "data-test": "hostname-input" });
    return el("div", { class: "inline-form" }, [
      el("div", { class: "field" }, [el("label", { for: "hostname-input", text: "New hostname" }), input]),
      el("div", { class: "control-stage-actions" }, [
        el("button", {
          type: "button", class: "primary-button compact", "data-test": "hostname-plan", text: "Plan hostname change",
          onclick: function () {
            planOperation({
              endpoint: "/api/network/hostname", body: { hostname: input.value.trim() },
              title: "Change the appliance hostname", confirmLabel: "Apply"
            });
          }
        })
      ])
    ]);
  }

  function renderTimezoneForm() {
    var current = ((state.data.status || {}).system || {}).timezone || "UTC";
    var input = el("input", {
      id: "timezone-input", type: "text", "data-test": "timezone-input", value: current
    });
    return el("div", { class: "inline-form" }, [
      el("p", { class: "section-hint", "data-test": "timezone-current",
        text: "The EMS runs its control windows in this zone. Currently " + current + "." }),
      el("div", { class: "field" }, [
        el("label", { for: "timezone-input", text: "Timezone (IANA name)" }), input
      ]),
      el("div", { class: "control-stage-actions" }, [
        el("button", {
          type: "button", class: "primary-button compact",
          "data-test": "timezone-plan", text: "Plan timezone change",
          onclick: function () {
            planOperation({
              endpoint: "/api/system/timezone", body: { timezone: input.value.trim() },
              title: "Change the appliance timezone", confirmLabel: "Apply"
            });
          }
        })
      ])
    ]);
  }

  /* ------------------------------------------------------------ Access */

  /* Three states, and "unreadable" is one of them. The console reports which
     one the appliance is in; it never demands a change and never renders a
     state it could not read as one it could. */
  function rescueState(rescue) {
    if (!rescue || !rescue.present) {
      return { tone: "warn", label: "not present", hint: "This appliance has no rescue account. A console login is the only way back in when the web console does not answer." };
    }
    if (rescue.unreadable || rescue.password_is_default === null) {
      return { tone: "idle", label: "unknown", hint: "This appliance could not read whether the password is still the shipped one." };
    }
    if (rescue.password_is_default) {
      return { tone: "warn", label: "shipped password", hint: "The password is the documented default, which is public knowledge. That is fine on a private network and a login for anyone who reaches this appliance from outside one. Change it with 'sudo passwd " + rescue.account + "' if that describes yours." };
    }
    return { tone: "ok", label: "changed", hint: "The password is no longer the shipped one." };
  }

  function renderAccess(main) {
    var ssh = (state.data.status || {}).ssh || {};
    var backup = state.data.backup;
    if (backup === undefined) {
      state.data.backup = null;
      loadInto("backup", "/api/backup");
    }

    main.appendChild(el("h2", { class: "section-title", text: "SSH & backup access" }));
    /* The hint states the design; what is actually in force is reported by the
       export card below, from the policy sshd applies. */
    main.appendChild(el("p", {
      class: "section-hint",
      text: "Key-based SSH only. The backup account is meant to be chrooted into the read-only export root and restricted to SFTP. The Appliance Manager never enables password logins and never handles private keys."
    }));

    var hardening = ssh.hardening || {};
    main.appendChild(el("div", { class: "card-grid" }, [
      card("SSH service", [
        el("p", { class: "status-value" }, [tone(ssh.enabled ? "ok" : "idle", ssh.enabled ? "running" : "stopped")]),
        fact("Unit state", (ssh.service || {}).active),
        fact("Password login", (hardening.passwordauthentication || {}).value || ssh.password_authentication)
      ], "ssh-service"),
      rescueCard(),
      card("Backup account", [
        el("p", { class: "status-value", text: ((backup || {}).account || {}).name || "—" }),
        el("div", {}, [tone(((backup || {}).account || {}).exists ? "ok" : "warn",
          ((backup || {}).account || {}).exists ? "available" : "not created")]),
        fact("Authorized keys", ((backup || {}).account || {}).key_count),
        fact("Protocol", ((backup || {}).protocol || "").toUpperCase()),
        fact("Shell access", (backup || {}).shell_access),
        fact("Write access", (backup || {}).write_access)
      ], "backup-account"),
      card("Export access", [
        el("p", { class: "status-value" }, [
          tone(exportTone(((backup || {}).export_access || {}).status),
            ((backup || {}).export_access || {}).status || "unknown")
        ]),
        el("div", {}, [tone((backup || {}).confined ? "ok" : "warn",
          (backup || {}).confined ? "confined to the export root" : "not confined")]),
        fact("Detail", ((backup || {}).export_access || {}).detail),
        confinementGaps(backup),
        exportSetupReport(backup),
        fact("Export root", (backup || {}).export_root, { mono: true }),
        (((backup || {}).unmanaged_entries || []).length
          ? fact("Unmanaged entries", (backup.unmanaged_entries || []).join(", "))
          : null),
        expert() ? fact("sshd chroot", ((backup || {}).chroot || {}).configured, { mono: true }) : null,
        expert() ? fact("Forced command", ((backup || {}).chroot || {}).force_command, { mono: true }) : null
      ], "backup-export")
    ]));

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "SSH service", "Enable or disable key-based remote access", [
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "ssh-enable", text: "Enable SSH",
            onclick: function () { planOperation({ endpoint: "/api/ssh/enable", title: "Enable the SSH service" }); }
          }),
          el("button", {
            type: "button", class: "ghost-button compact", "data-test": "ssh-disable", text: "Disable SSH",
            onclick: function () { planOperation({ endpoint: "/api/ssh/disable", title: "Disable the SSH service", danger: true }); }
          })
        ])
      ], "ssh-stage-service"),
      stage(2, "Add public key", "Paste an OpenSSH public key", [renderKeyForm(ssh)], "ssh-stage-add")
    ]));

    (ssh.accounts || []).forEach(function (account) {
      main.appendChild(el("h2", { class: "section-title", text: "Keys for " + account.name }));
      if (!account.exists) {
        main.appendChild(el("p", { class: "empty-state", text: "The host account " + account.name + " does not exist yet." }));
        return;
      }
      if (!(account.keys || []).length) {
        main.appendChild(el("p", { class: "empty-state", text: "No authorized keys." }));
        return;
      }
      main.appendChild(el("div", { class: "table-wrap" }, [
        el("table", { class: "data", "data-test": "ssh-key-table" }, [
          el("thead", {}, [el("tr", {}, [
            el("th", { text: "Type" }), el("th", { text: "Comment" }),
            el("th", { text: "Fingerprint" }), el("th", { text: "Action" })
          ])]),
          el("tbody", {}, account.keys.map(function (key) {
            return el("tr", {}, [
              el("td", { text: key.key_type }),
              el("td", { text: key.comment || "—" }),
              el("td", { class: "mono", text: key.fingerprint }),
              el("td", {}, [el("button", {
                type: "button", class: "ghost-button compact", text: "Remove",
                "data-test": "ssh-remove-key",
                onclick: function () {
                  planOperation({
                    endpoint: "/api/ssh/keys/remove-plan", title: "Remove SSH key",
                    body: { account: account.name, fingerprint: key.fingerprint },
                    confirmLabel: "Remove", danger: true
                  });
                }
              })])
            ]);
          }))
        ])
      ]));
    });

    if (backup && (backup.paths || []).length) {
      main.appendChild(el("h2", { class: "section-title", text: "Readable paths" }));
      main.appendChild(el("div", { class: "table-wrap" }, [
        el("table", { class: "data", "data-test": "backup-paths" }, [
          el("thead", {}, [el("tr", {}, [
            el("th", { text: "Name" }), el("th", { text: "Path" }),
            el("th", { text: "Access" }), el("th", { text: "Export state" }),
            el("th", { text: "Size" })
          ])]),
          el("tbody", {}, backup.paths.map(function (item) {
            return el("tr", {}, [
              el("td", { text: item.name }),
              el("td", { class: "mono", text: expert() ? item.path : "/" + item.name }),
              el("td", { text: item.access }),
              el("td", {}, [tone(item.state === "mounted" ? "ok" : (item.exists ? "bad" : "warn"),
                exportStateLabel(item))]),
              el("td", { text: item.exists ? item.size_mb + " MB" : "missing" })
            ]);
          }))
        ])
      ]));

      main.appendChild(el("h2", { class: "section-title", text: "Copy files from this appliance" }));
      main.appendChild(el("p", {
        class: "section-hint",
        text: "The backup account accepts SFTP only; rsync and scp need a remote shell it does not have."
      }));
      main.appendChild(el("div", { class: "stage-grid" },
        (backup.examples || []).map(function (example, index) {
          return stage(index + 1, example.title, "Run this on your own computer", [
            el("pre", { class: "log-view", text: example.command })
          ], "backup-example");
        })));
    }
  }

  /* Which promised SSH restrictions the running daemon does not apply. Named
     one by one, because "degraded" does not tell an operator what to fix. */
  function confinementGaps(backup) {
    var confinement = (backup || {}).confinement || {};
    if (confinement.available === false) {
      return fact("SSH confinement", "not verified — sshd could not be asked");
    }
    if (!(confinement.violations || []).length) return null;
    return fact("Not enforced by sshd", confinement.violations.join(", "));
  }

  /* What the export setup last reported: a refused export source or a failed
     watcher is only visible here. */
  function exportSetupReport(backup) {
    var reported = ((backup || {}).export_access || {}).reported || {};
    if (!reported.status || reported.status === "configured") return null;
    var detail = reported.detail ? reported.status + " — " + reported.detail : reported.status;
    return fact("Export setup", detail);
  }

  // A mount is only "read-only export" when the kernel says it publishes the
  // configured EMS directory; ro alone is not the same claim.
  var EXPORT_STATE_LABELS = {
    mounted: "read-only export",
    foreign: "not the configured directory",
    writable: "exported read-write",
    not_mounted: "not published",
    missing: "missing"
  };

  function exportStateLabel(item) {
    return EXPORT_STATE_LABELS[item.state] || item.state;
  }

  function exportTone(status) {
    if (status === "configured") return "ok";
    if (status === "failed" || status === "unavailable" || status === "degraded") return "bad";
    return "warn";
  }

  function rescueCard() {
    var rescue = (state.data.status || {}).system || {};
    rescue = rescue.rescue || {};
    var verdict = rescueState(rescue);
    return card("Console rescue account", [
      el("p", { class: "status-value", text: rescue.account || "ems-rescue" }),
      el("div", {}, [tone(verdict.tone, verdict.label)]),
      fact("Can log in", rescue.can_log_in),
      expert() ? fact("Shell", rescue.shell) : null,
      el("p", { class: "control-stage-subtitle", text: verdict.hint })
    ], "rescue-account");
  }

  function renderKeyForm(ssh) {
    var accounts = (ssh.accounts || []).map(function (item) { return item.name; });
    var select = el("select", { id: "key-account", "data-test": "key-account" },
      accounts.map(function (name) { return el("option", { value: name, text: name }); }));
    var textarea = el("textarea", {
      id: "key-value", "data-test": "key-value", rows: "4",
      placeholder: "ssh-ed25519 AAAA... user@laptop"
    });
    return el("div", { class: "inline-form" }, [
      el("div", { class: "field" }, [el("label", { for: "key-account", text: "Account" }), select]),
      el("div", { class: "field" }, [el("label", { for: "key-value", text: "Public key" }), textarea]),
      el("p", { class: "control-stage-subtitle", text: "Never paste a private key. Only public keys are accepted." }),
      el("div", { class: "control-stage-actions" }, [
        el("button", {
          type: "button", class: "primary-button compact", "data-test": "key-add", text: "Add key",
          onclick: function () {
            planOperation({
              endpoint: "/api/ssh/keys",
              body: { account: select.value, public_key: textarea.value },
              title: "Add an SSH public key", confirmLabel: "Add key"
            });
          }
        })
      ])
    ]);
  }

  /* ------------------------------------------------------- Diagnostics */

  function renderDiagnostics(main) {
    var status = state.data.status || {};
    var system = status.system || {};

    main.appendChild(el("h2", { class: "section-title", text: "Diagnostics" }));
    main.appendChild(el("p", { class: "section-hint", text: "Bounded, redacted host information." }));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Temperature", [
        el("p", { class: "status-value", text: (system.temperature || {}).celsius ? system.temperature.celsius + " °C" : "—" })
      ], "diag-temperature"),
      card("Memory", [
        el("p", { class: "status-value", text: (system.memory || {}).used_percent !== null && (system.memory || {}).used_percent !== undefined ? system.memory.used_percent + " %" : "—" }),
        fact("Total", (system.memory || {}).total_mb ? system.memory.total_mb + " MB" : null),
        fact("Available", (system.memory || {}).available_mb ? system.memory.available_mb + " MB" : null)
      ], "diag-memory"),
      card("Root filesystem", [
        el("p", { class: "status-value", text: ((system.storage || {}).root || {}).used_percent !== undefined ? system.storage.root.used_percent + " %" : "—" }),
        fact("Free", ((system.storage || {}).root || {}).free_mb ? system.storage.root.free_mb + " MB" : null)
      ], "diag-storage"),
      card("EMS data", [
        el("p", { class: "status-value", text: ((system.storage || {}).ems_data || {}).used_percent !== undefined ? system.storage.ems_data.used_percent + " %" : "—" }),
        fact("Path", ((system.storage || {}).ems_data || {}).path, { mono: true })
      ], "diag-ems-data"),
      expert() ? card("Kernel", [
        el("p", { class: "status-value", text: (system.operating_system || {}).kernel || "—" }),
        fact("Architecture", (system.hardware || {}).architecture),
        fact("OS version", (system.operating_system || {}).version)
      ], "diag-kernel") : null,
      card("Services", [
        el("div", {}, ((system.services || []).map(function (unit) {
          return fact(unit.unit, unit.active);
        })))
      ], "diag-services")
    ].filter(Boolean)));

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "Support archive", "Bounded, redacted diagnostics for support", [
        el("p", { class: "control-stage-subtitle", text: "Passwords, tokens, private keys and EMS secrets are excluded." }),
        el("div", { class: "control-stage-actions" }, [
          el("button", {
            type: "button", class: "primary-button compact", "data-test": "support-archive",
            text: "Create support archive",
            onclick: function () { planOperation({ endpoint: "/api/support/archive", title: "Create a support archive", confirmLabel: "Create" }); }
          })
        ])
      ], "diag-support")
    ]));

    var sources = expert()
      ? ["appliance_web", "appliance_agent", "operations", "audit", "admin_container", "ems_container", "docker_daemon", "boot", "packages"]
      : ["admin_container", "operations", "audit"];
    main.appendChild(logPanel(state.data.logSource || sources[0], "Logs", sources));
  }

  function logPanel(source, title, sources) {
    var wrapper = el("section", { class: "control-stage", "data-test": "log-panel" }, [
      el("div", { class: "control-stage-head" }, [
        el("span", { class: "control-stage-step", "aria-hidden": "true", text: "L" }),
        el("h3", { class: "control-stage-title", text: title })
      ])
    ]);

    if (sources) {
      var select = el("select", { id: "log-source", "data-test": "log-source" },
        sources.map(function (item) { return el("option", { value: item, text: item.replace(/_/g, " ") }); }));
      select.value = source;
      select.addEventListener("change", function () {
        state.data.logSource = select.value;
        loadLog(select.value);
      });
      wrapper.appendChild(el("div", { class: "field" }, [
        el("label", { for: "log-source", text: "Source" }), select
      ]));
    }

    wrapper.appendChild(el("div", { class: "control-stage-actions" }, [
      el("button", {
        type: "button", class: "ghost-button compact", "data-test": "log-load", text: "Load log",
        onclick: function () { loadLog(source); }
      })
    ]));

    var log = state.data.log;
    if (log && log.source === source) {
      wrapper.appendChild(el("p", { class: "control-stage-subtitle", text: log.lines + " lines" + (log.truncated ? " (truncated)" : "") }));
      wrapper.appendChild(el("pre", { class: "log-view", "data-test": "log-output", text: log.text || "(empty)" }));
    } else {
      wrapper.appendChild(el("p", { class: "empty-state", text: "No log loaded." }));
    }
    return wrapper;
  }

  function loadLog(source) {
    api("/api/logs/" + encodeURIComponent(source) + "?lines=200").then(function (payload) {
      state.data.log = payload;
      render();
    }).catch(showToastError);
  }

  /* --------------------------------------------------------- Settings */

  function renderSettings(main) {
    var settings = state.data.settings;
    if (settings === undefined) {
      state.data.settings = null;
      loadInto("settings", "/api/settings");
    }
    settings = settings || {};
    var auditState = settings.security_audit || state.securityAudit || {};

    main.appendChild(el("h2", { class: "section-title", text: "Settings" }));
    main.appendChild(el("p", { class: "section-hint", text: "Host settings live in the appliance configuration file and are read-only here." }));

    main.appendChild(el("div", { class: "card-grid" }, [
      card("Appliance", [
        el("p", { class: "status-value", text: settings.appliance_version || "—" }),
        fact("Web port", settings.web_port),
        fact("Admin port", settings.admin_port),
        fact("Configuration", settings.configuration_file, { mono: true })
      ], "settings-appliance"),
      card("Sessions", [
        fact("Idle timeout", settings.session_timeout_seconds ? settings.session_timeout_seconds + " s" : null),
        fact("Absolute maximum", settings.session_absolute_max_seconds ? settings.session_absolute_max_seconds + " s" : null)
      ], "settings-sessions"),
      card("Updates", [
        fact("Automatic security updates", settings.automatic_security_updates),
        fact("Admin repository", settings.admin_repository, { mono: true }),
        fact("Prereleases allowed", settings.allow_prerelease)
      ], "settings-updates"),
      card("Security audit", [
        el("p", { class: "status-value" }, [
          tone(auditState.degraded ? "bad" : "ok", auditState.degraded ? "degraded" : "healthy")
        ]),
        fact("Written by", "the privileged appliance agent"),
        fact("Recorded events", auditState.recorded_events),
        fact("Unrecorded events", auditState.unrecorded_events),
        auditState.last_error ? fact("Last error", auditState.last_error, { mono: true }) : null
      ], "settings-audit")
    ]));

    main.appendChild(el("div", { class: "stage-grid" }, [
      stage(1, "Detail level", "Basic hides host internals, Expert shows them", [
        el("p", { class: "control-stage-subtitle", text: "The preference is stored in this browser only." }),
        el("div", { class: "control-stage-actions" }, [
          el("button", { type: "button", class: "ghost-button compact", text: "Use Basic mode", onclick: function () { setMode("basic"); } }),
          el("button", { type: "button", class: "ghost-button compact", text: "Use Expert mode", onclick: function () { setMode("expert"); } })
        ])
      ], "settings-mode"),
      stage(2, "Appliance password", "Independent from the EMS Admin password", [renderPasswordForm()], "settings-password")
    ]));
  }

  function renderPasswordForm() {
    var current = el("input", { id: "pw-current", type: "password", autocomplete: "current-password", "data-test": "pw-current" });
    var next = el("input", { id: "pw-new", type: "password", autocomplete: "new-password", "data-test": "pw-new" });
    var confirmation = el("input", { id: "pw-confirm", type: "password", autocomplete: "new-password", "data-test": "pw-confirm" });
    var message = el("p", { class: "control-result", hidden: true, "data-test": "pw-message" });

    return el("form", {
      class: "inline-form",
      onsubmit: function (event) {
        event.preventDefault();
        api("/api/settings/password", {
          method: "POST",
          body: {
            current_password: current.value,
            password: next.value,
            confirmation: confirmation.value
          }
        }).then(function () {
          message.textContent = "Password changed. All sessions were signed out.";
          message.hidden = false;
          announce("Password changed");
          window.setTimeout(function () {
            state.authenticated = false;
            state.csrf = "";
            stopPolling();
            showGate(false);
          }, 1500);
        }).catch(function (exc) {
          message.textContent = exc.message;
          message.hidden = false;
        });
      }
    }, [
      el("div", { class: "field" }, [el("label", { for: "pw-current", text: "Current password" }), current]),
      el("div", { class: "field" }, [el("label", { for: "pw-new", text: "New password" }), next]),
      el("div", { class: "field" }, [el("label", { for: "pw-confirm", text: "Repeat new password" }), confirmation]),
      message,
      el("div", { class: "control-stage-actions" }, [
        el("button", { type: "submit", class: "primary-button compact", "data-test": "pw-submit", text: "Change password" })
      ])
    ]);
  }

  function renderFindings(findings) {
    return el("div", { class: "table-wrap" }, [
      el("table", { class: "data", "data-test": "repair-findings" }, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "Check" }), el("th", { text: "State" }),
          el("th", { text: "Detail" }), el("th", { text: "Suggestion" })
        ])]),
        el("tbody", {}, findings.map(function (item) {
          /* A check that could not run is neither a pass nor a problem. */
          var level = item.indeterminate ? "warn" : (item.ok ? "ok" : "bad");
          var label = item.indeterminate ? "not checked" : (item.ok ? "ok" : "problem");
          return el("tr", {}, [
            el("td", { text: item.check.replace(/_/g, " ") }),
            el("td", {}, [tone(level, label)]),
            el("td", { text: item.detail }),
            el("td", { text: item.suggestion || "—" })
          ]);
        }))
      ])
    ]);
  }

  /* ------------------------------------------------------------- boot */

  function boot() {
    loadSession().then(function () {
      if (!state.authenticated) {
        showGate(!state.passwordConfigured);
        return;
      }
      showShell();
      renderNav();
      refresh();
    }).catch(function () {
      showGate(false);
    });
  }

  function init() {
    try {
      state.mode = window.localStorage.getItem(MODE_KEY) === "expert" ? "expert" : "basic";
      var storedView = window.localStorage.getItem(VIEW_KEY);
      if (storedView) state.view = storedView;
    } catch (exc) { /* private mode */ }

    document.getElementById("gate-form").addEventListener("submit", submitGate);
    document.getElementById("logout-button").addEventListener("click", logout);
    document.getElementById("refresh-button").addEventListener("click", function () { refresh(); });
    document.getElementById("mode-basic").addEventListener("click", function () { setMode("basic"); });
    document.getElementById("mode-expert").addEventListener("click", function () { setMode("expert"); });
    document.getElementById("dialog-cancel").addEventListener("click", closeDialog);
    document.getElementById("dialog-confirm").addEventListener("click", confirmDialog);
    document.getElementById("reconnect-retry").addEventListener("click", pollReconnect);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !document.getElementById("dialog-backdrop").hidden) closeDialog();
    });

    document.getElementById("mode-basic").setAttribute("aria-pressed", String(state.mode === "basic"));
    document.getElementById("mode-expert").setAttribute("aria-pressed", String(state.mode === "expert"));
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
