// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Visual gallery for the energy pipe study: every candidate animating at once
// against the same scene, with the controls the study's scenarios need.
//
// Per-card frame rates are measured one card at a time with every other card
// paused. A frame rate read while nine animations share one main thread is a
// property of the page, not of the candidate, so the gallery never prints one.

(function () {
  "use strict";

  var PS = window.PipeStudy;
  var POWERS = [-2000, -690, -100, 0].concat(PS.WATT_SAMPLES);
  var CARD_W = 1148;

  var state = {
    powerIndex: 8,
    scenario: "aggregate",
    devices: 4,
    shape: "normal",
    glow: "blur",
    direction: "mixed",
    motion: "on",
    sweep: false,
  };

  var cards = [];
  var sweepTimer = null;

  function flowsFor() {
    if (state.scenario === "single") return 1;
    if (state.scenario === "devices") return state.devices * 3;
    return 4;
  }

  function cardHeight(flows, cols) {
    var rows = Math.ceil(flows / cols);
    return Math.max(96, Math.min(420, rows * 120 * (CARD_W / (cols * 260))));
  }

  function colsFor(flows) {
    return Math.max(1, Math.min(flows, Math.floor(CARD_W / 200)));
  }

  function build() {
    var host = document.getElementById("cards");
    PS.CANDIDATES.forEach(function (candidate) {
      var card = document.createElement("section");
      card.className = "card";
      card.innerHTML =
        '<header><span class="letter">' + candidate.letter + '</span>' +
        '<span class="name">' + candidate.name + "</span>" +
        '<span class="blurb">' + candidate.blurb + "</span></header>" +
        '<div class="metrics">' +
        '<span>power <b class="m-power">-</b></span>' +
        '<span>thickness <b class="m-width">-</b></span>' +
        '<span>animated <b class="m-animated">-</b></span>' +
        '<span>painted <b class="m-painted">-</b></span>' +
        '<span>CSS animations <b class="m-anims">-</b></span>' +
        '<span>fps <b class="m-fps">not measured</b></span>' +
        '<span>frame p95 <b class="m-p95">-</b></span>' +
        "</div>" +
        '<div class="viewport"><div class="ps-host"></div></div>';
      host.appendChild(card);
      cards.push({
        id: candidate.id,
        tokens: candidate.id === "tokens" ? 4 : 1,
        root: card,
        host: card.querySelector(".ps-host"),
        viewport: card.querySelector(".viewport"),
        m: {
          power: card.querySelector(".m-power"),
          width: card.querySelector(".m-width"),
          animated: card.querySelector(".m-animated"),
          painted: card.querySelector(".m-painted"),
          anims: card.querySelector(".m-anims"),
          fps: card.querySelector(".m-fps"),
          p95: card.querySelector(".m-p95"),
        },
      });
    });
  }

  function render() {
    var watts = POWERS[state.powerIndex];
    var flows = flowsFor();
    var cols = colsFor(flows);
    var height = cardHeight(flows, cols);

    cards.forEach(function (card) {
      card.viewport.style.height = Math.round(height) + "px";
      var scene = PS.buildScene({
        flows: flows,
        watts: watts,
        speeds: "mixed",
        reverse: state.direction,
        shape: state.shape,
        cols: cols,
        stageW: CARD_W,
        stageH: height,
      });
      card.host.textContent = "";
      var counts = PS.paint(scene, {
        overlay: card.host,
        candidate: card.id,
        motion: state.motion,
        glow: state.glow,
        tokens: card.tokens,
        texture: "simple",
        tile: 1,
        pad: 0,
      });
      var pipe = scene.pipes[0];
      card.m.power.textContent = watts === 0 ? "0 W (idle)"
        : watts + " W" + (watts < 0 ? " (reversed)" : "");
      card.m.width.textContent = pipe.width.toFixed(1) + " px";
      card.m.animated.textContent = String(counts.animated);
      card.m.painted.textContent = String(counts.painted);
      card.m.anims.textContent = "-";
      card.scene = scene;
    });

    var total = null;
    try {
      total = document.getAnimations ? document.getAnimations().length : null;
    } catch (error) {
      total = null;
    }
    cards.forEach(function (card) {
      card.m.anims.textContent = total === null ? "n/a" : "";
    });
    if (total !== null) {
      cards.forEach(function (card) {
        card.m.anims.textContent = String(
          card.host.querySelectorAll(".ps-move, .ps-token").length
        );
      });
    }

    document.getElementById("powerOut").textContent =
      watts === 0 ? "0 W" : watts + " W";
    window.__galleryShape = state.shape;
    window.__gallery = {
      ready: true,
      glow: state.glow,
      power: watts,
      scenario: state.scenario,
      devices: state.devices,
      direction: state.direction,
      motion: state.motion,
      flows: flows,
      candidates: cards.map(function (c) { return c.id; }),
      documentAnimations: total,
    };
  }

  function setPaused(card, paused) {
    var nodes = card.host.querySelectorAll(".ps-move, .ps-token");
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].classList.toggle("paused", paused);
    }
  }

  function sampleFps(ms) {
    return new Promise(function (resolve) {
      var deltas = [];
      var previous = performance.now();
      var stop = previous + ms;
      var step = function (now) {
        deltas.push(now - previous);
        previous = now;
        if (now < stop) requestAnimationFrame(step);
        else {
          var frames = deltas.slice(2);
          if (!frames.length) { resolve({ fps: null, p95: null }); return; }
          var mean = frames.reduce(function (a, b) { return a + b; }, 0) / frames.length;
          var sorted = frames.slice().sort(function (a, b) { return a - b; });
          resolve({
            fps: 1000 / mean,
            p95: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))],
          });
        }
      };
      requestAnimationFrame(step);
    });
  }

  async function measureEach() {
    var status = document.getElementById("status");
    var wasMotion = state.motion;
    if (wasMotion === "off") {
      status.textContent = "turn the animation on first";
      return;
    }
    for (var i = 0; i < cards.length; i += 1) {
      status.textContent = "measuring " + cards[i].id + " (" + (i + 1) + "/" + cards.length + ")";
      cards.forEach(function (card, index) { setPaused(card, index !== i); });
      cards[i].root.scrollIntoView({ block: "center" });
      await new Promise(function (r) { setTimeout(r, 250); });
      var sample = await sampleFps(1600);
      cards[i].m.fps.textContent = sample.fps ? sample.fps.toFixed(1) : "n/a";
      cards[i].m.p95.textContent = sample.p95 ? sample.p95.toFixed(1) + " ms" : "n/a";
      cards[i].measured = sample;
    }
    cards.forEach(function (card) { setPaused(card, false); });
    status.textContent = "measured one at a time, others paused";
    window.__galleryMeasured = cards.map(function (card) {
      return { candidate: card.id, fps: card.measured ? card.measured.fps : null,
               frameP95Ms: card.measured ? card.measured.p95 : null };
    });
  }

  function bind() {
    var power = document.getElementById("power");
    power.max = String(POWERS.length - 1);
    power.value = String(state.powerIndex);
    power.addEventListener("input", function () {
      state.powerIndex = Number(power.value);
      render();
    });

    document.getElementById("scenario").addEventListener("change", function (event) {
      state.scenario = event.target.value;
      render();
    });
    document.getElementById("devices").addEventListener("change", function (event) {
      state.devices = Number(event.target.value);
      if (state.scenario === "devices") render();
    });
    document.getElementById("glow").addEventListener("change", function (event) {
      state.glow = event.target.value;
      render();
    });
    document.getElementById("shape").addEventListener("change", function (event) {
      state.shape = event.target.value;
      render();
    });
    document.getElementById("direction").addEventListener("change", function (event) {
      state.direction = event.target.value;
      render();
    });

    var motion = document.getElementById("motion");
    motion.addEventListener("click", function () {
      state.motion = state.motion === "on" ? "off" : "on";
      motion.setAttribute("aria-pressed", String(state.motion === "on"));
      motion.textContent = state.motion === "on" ? "Animation on" : "Animation off";
      render();
    });

    var sweep = document.getElementById("sweep");
    sweep.addEventListener("click", function () {
      state.sweep = !state.sweep;
      sweep.setAttribute("aria-pressed", String(state.sweep));
      if (state.sweep) {
        sweepTimer = setInterval(function () {
          state.powerIndex = (state.powerIndex + 1) % POWERS.length;
          power.value = String(state.powerIndex);
          render();
        }, 1400);
      } else if (sweepTimer) {
        clearInterval(sweepTimer);
        sweepTimer = null;
      }
    });

    document.getElementById("measure").addEventListener("click", measureEach);
  }

  function start() {
    build();
    bind();
    render();
    window.__lab = { study: "energy-pipe-gallery", ready: true };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
