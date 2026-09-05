// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Benchmark page for the energy pipe study. One candidate, one scene, and a
// window.__lab record the shared Playwright driver reads.

(function () {
  "use strict";

  var CANDIDATE_IDS = window.PipeStudy.CANDIDATES.map(function (c) { return c.id; });

  function params() {
    var q = new URLSearchParams(window.location.search);
    var candidate = q.get("candidate") || "capsule";
    if (CANDIDATE_IDS.indexOf(candidate) < 0) candidate = "capsule";
    var scenario = q.get("scenario") || "aggregate";
    var flows = parseInt(q.get("flows") || "12", 10);
    if (scenario === "devices") {
      var devices = parseInt(q.get("devices") || "4", 10);
      flows = Math.max(1, (isNaN(devices) ? 4 : devices)) * 3;
    } else if (scenario === "single") {
      flows = 1;
    }
    var wattsRaw = q.get("watts") || "690";
    var tokens = parseInt(q.get("tokens") || "2", 10);
    var tile = parseInt(q.get("tile") || "1", 10);
    var pad = parseInt(q.get("pad") || "0", 10);
    return {
      candidate: candidate,
      scenario: scenario,
      flows: Math.max(1, Math.min(600, isNaN(flows) ? 12 : flows)),
      watts: wattsRaw === "mixed" ? "mixed" : Number(wattsRaw),
      motion: q.get("motion") === "off" ? "off" : "on",
      speeds: q.get("speeds") === "mixed" ? "mixed" : "single",
      reverse: q.get("reverse") || "mixed",
      tokens: [1, 2, 4, 8, 16].indexOf(tokens) >= 0 ? tokens : 2,
      texture: q.get("texture") === "rich" ? "rich" : "simple",
      shape: window.PipeStudy.SHAPES.indexOf(q.get("shape")) >= 0 ? q.get("shape") : "normal",
      glow: window.PipeStudy.GLOWS.indexOf(q.get("glow")) >= 0 ? q.get("glow") : "none",
      anim: q.get("anim") === "waapi" ? "waapi" : "var",
      tile: [1, 4, 16].indexOf(tile) >= 0 ? tile : 1,
      pad: isNaN(pad) ? 0 : Math.max(0, Math.min(200, pad)),
    };
  }

  function run() {
    var opts = params();
    var scene = window.PipeStudy.buildScene(opts);
    var stage = document.getElementById("stage");
    var overlay = document.getElementById("overlay");
    var w = Math.round(scene.layout.w * scene.layout.scale);
    var h = Math.round(scene.layout.h * scene.layout.scale);
    stage.style.width = w + "px";
    stage.style.height = h + "px";

    var counts = window.PipeStudy.paint(scene, {
      overlay: overlay,
      candidate: opts.candidate,
      motion: opts.motion,
      tokens: opts.tokens,
      texture: opts.texture,
      tile: opts.tile,
      pad: opts.pad,
      glow: opts.glow,
      anim: opts.anim,
    });

    if (opts.motion === "off") {
      var style = document.createElement("style");
      style.textContent = "*, *::before, *::after { animation-play-state: paused !important; }";
      document.head.appendChild(style);
    }

    var animations = null;
    try {
      animations = document.getAnimations ? document.getAnimations().length : null;
    } catch (error) {
      animations = null;
    }

    window.__lab = {
      study: "energy-pipe",
      candidate: opts.candidate,
      scenario: opts.scenario,
      flows: opts.flows,
      devices: opts.scenario === "devices" ? opts.flows / 3 : null,
      watts: opts.watts,
      motion: opts.motion,
      speeds: opts.speeds,
      reverse: opts.reverse,
      tokens: opts.tokens,
      texture: opts.texture,
      shape: opts.shape,
      glow: opts.glow,
      anim: opts.anim,
      tile: opts.tile,
      pad: opts.pad,
      reference: scene.reference,
      maxWatts: scene.maxWatts,
      widths: scene.pipes.slice(0, 8).map(function (p) { return p.width; }),
      seconds: scene.pipes.length ? scene.pipes[0].seconds : null,
      cols: scene.layout.cols,
      rows: scene.layout.rows,
      scale: Number(scene.layout.scale.toFixed(4)),
      stageW: w,
      stageH: h,
      stagePx: w * h,
      animatedElements: counts.animated,
      paintedElements: counts.painted,
      staticBars: counts.staticBars,
      movingTokens: counts.tokens,
      overlayElements: overlay.getElementsByTagName("*").length,
      cssAnimations: animations,
      ready: true,
    };

    document.getElementById("labInfo").textContent =
      opts.candidate + " | glow " + opts.glow + " | " + opts.scenario + " " + opts.flows +
      " | motion " + opts.motion +
      " | animated " + counts.animated +
      " | painted " + counts.painted +
      " | anim " + animations +
      " | " + w + "x" + h;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run, { once: true });
  } else {
    run();
  }
})();
