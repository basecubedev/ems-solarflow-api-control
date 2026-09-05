// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Flow rendering lab. One synthetic scene -- a grid of energy pipes with the
// dashboard's exact geometry, colours and speed buckets -- rendered by a
// selectable technique. Everything except the animated energy layer is held
// constant, so a difference between two runs is a difference between
// techniques and nothing else.
//
// Geometry note that makes several candidates possible at all: every pipe in
// the dashboard is axis-aligned (M, H, V only). There is no curve to follow,
// so a moving dash can be a translated tile instead of a changing dash offset.
//
//   ?renderer=dashoffset|svg-transform|svg-pattern|svg-mask|dom-tiles
//            |motion-path|canvas|none
//   ?flows=N&motion=on|off&active=0..1&speeds=mixed|single

(function () {
  "use strict";

  var CELL_W = 260;
  var CELL_H = 120;
  var STAGE_W = 1440;
  var STAGE_H = 820;
  var DASH_ON = 34;
  var DASH_PERIOD = 52;
  var SVG_NS = "http://www.w3.org/2000/svg";

  // The pipe inside one cell: H 88, V 67, H 80. Same shape as the dashboard's
  // device rows (`M204 91 H292 V158 H372`).
  var SHAPE = [
    { x1: 20, y1: 30, x2: 108, y2: 30 },
    { x1: 108, y1: 30, x2: 108, y2: 97 },
    { x1: 108, y1: 97, x2: 188, y2: 97 },
  ];

  var KINDS = ["pv", "battery", "output", "grid"];
  var SPEEDS = ["low", "medium", "high", "idle"];
  var SPEED_SECONDS = { idle: 1.85, low: 1.7, medium: 1.38, high: 1.1 };
  var SPEED_STYLE = {
    idle: { alpha: 0.12, width: 3, glow: 0.08 },
    low: { alpha: 0.48, width: 4, glow: 0.16 },
    medium: { alpha: 0.68, width: 5, glow: 0.26 },
    high: { alpha: 0.9, width: 6, glow: 0.4 },
  };
  var COLORS = {
    pv: "#ffd166",
    battery: "#39e58c",
    output: "#38d5ff",
    grid: "#d87cff",
    idle: "#64748b",
  };

  var GLOW_MODES = ["both", "static-only", "energy-only", "none"];

  function params() {
    var q = new URLSearchParams(window.location.search);
    var flows = parseInt(q.get("flows") || "4", 10);
    var activeFraction = parseFloat(q.get("active") || "1");
    return {
      renderer: q.get("renderer") || "dashoffset",
      flows: Math.max(1, Math.min(400, isNaN(flows) ? 4 : flows)),
      motion: q.get("motion") === "off" ? "off" : "on",
      activeFraction: isNaN(activeFraction) ? 1 : Math.max(0, Math.min(1, activeFraction)),
      speeds: q.get("speeds") === "single" ? "single" : "mixed",
      // Diagnostic axis. Two different filters sit on a pipe: the halo on the
      // layer that animates, and the static blur on .pipe-glow beside it.
      // Removing them together cannot say which one costs, so each can be
      // removed on its own.
      //   both        production
      //   static-only the animated layer is unfiltered, .pipe-glow stays
      //   energy-only .pipe-glow is unfiltered, the animated layer keeps its halo
      //   none        neither
      glow: GLOW_MODES.indexOf(q.get("glow")) >= 0 ? q.get("glow") : "both",
      metaphor: METAPHORS.indexOf(q.get("metaphor")) >= 0 ? q.get("metaphor") : "dash",
    };
  }

  function grid(flows) {
    var target = STAGE_W / STAGE_H;
    var rows = Math.ceil(Math.sqrt((flows * CELL_W) / (target * CELL_H)));
    rows = Math.max(1, Math.min(rows, flows));
    var cols = Math.ceil(flows / rows);
    var scale = Math.min(STAGE_W / (cols * CELL_W), STAGE_H / (rows * CELL_H));
    return { rows: rows, cols: cols, scale: scale, w: cols * CELL_W, h: rows * CELL_H };
  }

  function buildPipes(opts, layout) {
    var pipes = [];
    var activeCount = Math.round(opts.flows * opts.activeFraction);
    for (var i = 0; i < opts.flows; i += 1) {
      var col = i % layout.cols;
      var row = Math.floor(i / layout.cols);
      var ox = col * CELL_W;
      var oy = row * CELL_H;
      var active = i < activeCount;
      var speed = active ? (opts.speeds === "single" ? "medium" : SPEEDS[i % 3]) : "idle";
      var kind = KINDS[i % KINDS.length];
      var segments = SHAPE.map(function (s) {
        return { x1: s.x1 + ox, y1: s.y1 + oy, x2: s.x2 + ox, y2: s.y2 + oy };
      });
      var cumulative = 0;
      segments.forEach(function (s) {
        s.length = Math.abs(s.x2 - s.x1) + Math.abs(s.y2 - s.y1);
        s.horizontal = s.y1 === s.y2;
        s.before = cumulative;
        cumulative += s.length;
      });
      pipes.push({
        index: i,
        kind: kind,
        speed: speed,
        active: active,
        reverse: i % 5 === 4,
        segments: segments,
        total: cumulative,
        d:
          "M" + segments[0].x1 + " " + segments[0].y1 +
          " H" + segments[0].x2 +
          " V" + segments[1].y2 +
          " H" + segments[2].x2,
        style: SPEED_STYLE[speed],
        color: active ? COLORS[kind] : COLORS.idle,
      });
    }
    return pipes;
  }

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG_NS, name);
    for (var key in attrs) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function div(className, parent) {
    var node = document.createElement("div");
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  function pipeGroup(pipe, svg) {
    var classes = [
      "energy-pipe",
      pipe.kind,
      pipe.active ? "active" : "idle",
      pipe.reverse ? "reverse" : "",
      "flow-speed-" + pipe.speed,
    ].filter(Boolean).join(" ");
    var g = el("g", { class: classes }, svg);
    g.style.setProperty("--pipe-alpha", String(pipe.style.alpha));
    g.style.setProperty("--pipe-width", pipe.style.width + "px");
    g.style.setProperty("--pipe-glow", String(pipe.style.glow));
    el("path", { class: "pipe-base", d: pipe.d }, g);
    el("path", { class: "pipe-glow", d: pipe.d }, g);
    return g;
  }

  // Travel direction of a segment, and the CSS keyframe that moves a tile
  // along it by exactly one dash period.
  function travel(segment) {
    if (segment.horizontal) return segment.x2 > segment.x1 ? "right" : "left";
    return segment.y2 > segment.y1 ? "down" : "up";
  }

  // ------------------------------------------------------------ metaphors
  //
  // dom-tiles moves ONE div per segment and paints the pattern with a
  // background image. What that pattern looks like therefore costs nothing:
  // the compositor translates the same layer whatever is drawn on it. That
  // makes the visual metaphor a free choice, and this table is the point of
  // the ?metaphor= axis -- it separates "how it looks" from "what it costs" so
  // the two can be judged independently.
  //
  // Every stop uses an explicit rgba(r,g,b,0) rather than `transparent`,
  // because `transparent` is transparent BLACK and CSS interpolates through
  // it, which puts a dark fringe on the trailing edge of every dash.

  var METAPHORS = [
    "dash", "capsule", "particles", "comet", "chevron", "pulse", "sweep",
  ];

  function hexRgb(hex) {
    var value = String(hex).replace("#", "");
    if (value.length === 3) {
      value = value[0] + value[0] + value[1] + value[1] + value[2] + value[2];
    }
    var n = parseInt(value, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function rgba(rgb, alpha) {
    return "rgba(" + rgb[0] + ", " + rgb[1] + ", " + rgb[2] + ", " + alpha + ")";
  }

  function svgUrl(markup) {
    return 'url("data:image/svg+xml;utf8,' + encodeURIComponent(markup) + '")';
  }

  // Returns the background for one segment plus the distance the layer must
  // travel for the pattern to repeat seamlessly.
  //
  // `thick` is the painted layer's thickness in px. Shape-based metaphors need
  // it: an SVG tile built in a square viewBox and then stretched to a 52x6
  // strip turns rounded rects into lenses and arrowheads into slivers, so the
  // tile is authored at the aspect it will actually be drawn at.
  function metaphorBackground(metaphor, horizontal, color, on, period, thick) {
    var rgb = hexRgb(color);
    var solid = rgba(rgb, 1);
    var clear = rgba(rgb, 0);
    var axis = horizontal ? "to right" : "to bottom";
    var span = period;
    var t = Math.max(2, thick || 6);
    var size = function (length) {
      return horizontal ? length + "px 100%" : "100% " + length + "px";
    };
    var repeat = horizontal ? "repeat-x" : "repeat-y";
    // The tile is authored along the direction of travel and then rotated by
    // swapping width and height, so one piece of geometry serves both axes.
    var tile = function (w, h, body) {
      var vb = horizontal ? "0 0 " + w + " " + h : "0 0 " + h + " " + w;
      var inner = horizontal
        ? body
        : '<g transform="rotate(90) translate(0,-' + h + ')">' + body + "</g>";
      return svgUrl(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="' + vb +
        '" preserveAspectRatio="none">' + inner + "</svg>"
      );
    };

    if (metaphor === "capsule") {
      // The production SVG strokes with round linecaps, so its dashes have
      // rounded ends. A tiled rounded rect reproduces that on one layer.
      var r = t / 2;
      var body = '<rect x="0" y="0" width="' + on.toFixed(2) + '" height="' + t.toFixed(2) +
        '" rx="' + r.toFixed(2) + '" ry="' + r.toFixed(2) + '" fill="' + solid + '"/>';
      return { image: tile(period, t, body), size: size(span), repeat: repeat, step: span };
    }

    if (metaphor === "particles") {
      // Discrete round dots. Direction is carried only by their motion, so in
      // a still frame this is the weakest of the set for reading direction.
      var radius = Math.max(18, Math.min(46, (on / period) * 60));
      return {
        image:
          "radial-gradient(circle at 50% 50%, " + solid + " 0%, " +
          rgba(rgb, 0.85) + " " + radius + "%, " + clear + " " + (radius + 22) + "%)",
        size: size(span),
        repeat: repeat,
        step: span,
      };
    }

    if (metaphor === "comet") {
      // A bright head with a tail fading out behind it. The asymmetry states
      // the direction even in a still frame, which the dash pattern does not.
      return {
        image:
          "linear-gradient(" + axis + ", " + clear + " 0%, " +
          rgba(rgb, 0.10) + " 34%, " + rgba(rgb, 0.45) + " 68%, " +
          rgba(rgb, 0.95) + " 92%, " + solid + " 100%)",
        size: size(span),
        repeat: repeat,
        step: span,
      };
    }

    if (metaphor === "chevron") {
      // Arrowheads: the strongest direction cue of the set, and the only one
      // that still states direction when the animation is paused. It needs a
      // thicker pipe than today's 4-6px to read, which metaphorThickness pays.
      var head = Math.min(period * 0.42, t * 0.9);
      var sw = Math.max(1.4, t * 0.24);
      var midY = t / 2;
      var x0 = (period - head) / 2;
      var body2 =
        '<polyline points="' + x0.toFixed(2) + "," + (midY - head * 0.62).toFixed(2) + " " +
        (x0 + head).toFixed(2) + "," + midY.toFixed(2) + " " +
        x0.toFixed(2) + "," + (midY + head * 0.62).toFixed(2) +
        '" fill="none" stroke="' + solid + '" stroke-width="' + sw.toFixed(2) +
        '" stroke-linecap="round" stroke-linejoin="round"/>';
      return { image: tile(period, t, body2), size: size(span), repeat: repeat, step: span };
    }

    if (metaphor === "pulse") {
      // A sparse travelling packet on an otherwise dim line: "something went
      // past", rather than "a texture is scrolling". The bright core is kept
      // narrow, because a wide one just reads as an evenly lit pipe.
      var wide = span * 2;
      return {
        image:
          "linear-gradient(" + axis + ", " + rgba(rgb, 0.10) + " 0%, " +
          rgba(rgb, 0.10) + " 58%, " + rgba(rgb, 0.30) + " 72%, " +
          rgba(rgb, 0.85) + " 84%, " + solid + " 90%, " +
          rgba(rgb, 0.70) + " 95%, " + rgba(rgb, 0.10) + " 100%)",
        size: size(wide),
        repeat: repeat,
        step: wide,
      };
    }

    if (metaphor === "sweep") {
      // No discrete objects at all: a continuous luminance wave. The calmest
      // option, and the only one with nothing to strobe or alias.
      return {
        image:
          "linear-gradient(" + axis + ", " + rgba(rgb, 0.22) + " 0%, " +
          rgba(rgb, 0.30) + " 18%, " + solid + " 50%, " +
          rgba(rgb, 0.30) + " 82%, " + rgba(rgb, 0.22) + " 100%)",
        size: size(span),
        repeat: repeat,
        step: span,
      };
    }

    // dash: what ships today.
    return {
      image:
        "linear-gradient(" + axis + ", " + solid + " 0px, " + solid + " " + on + "px, " +
        clear + " " + on + "px, " + clear + " " + period + "px)",
      size: size(span),
      repeat: repeat,
      step: span,
    };
  }

  // Chevrons and particles are shapes, not stripes: squeezed into a 5px strip
  // they turn into mush. Widening the painted layer is a design cost that the
  // dash pattern does not pay, and it is recorded here rather than hidden.
  function metaphorThickness(metaphor) {
    if (metaphor === "chevron") return 2.6;
    if (metaphor === "particles") return 1.5;
    return 1;
  }
  // ------------------------------------------------------------ renderers

  var renderers = {};

  renderers.none = function () {};

  renderers["canvas-bloom"] = function (pipes, ctx) {
    var withBloom = {};
    for (var key in ctx) withBloom[key] = ctx[key];
    withBloom.bloom = true;
    renderers.canvas(pipes, withBloom);
  };

  renderers.dashoffset = function (pipes, ctx) {
    pipes.forEach(function (pipe) {
      el("path", { class: "pipe-energy", d: pipe.d }, ctx.group(pipe));
    });
  };

  // A repeating dash tile per segment, clipped to that segment and moved with
  // a CSS `transform`. The tile's own dash offset carries the phase the pipe
  // has accumulated before the segment, so the pattern stays continuous
  // across the corners.
  renderers["svg-transform"] = function (pipes, ctx) {
    pipes.forEach(function (pipe) {
      var group = ctx.group(pipe);
      var tiles = el("g", { class: "flow-tiles energy-halo" }, group);
      pipe.segments.forEach(function (segment, k) {
        var id = "clip-" + pipe.index + "-" + k;
        var pad = pipe.style.width / 2 + 1;
        var clip = el("clipPath", { id: id, clipPathUnits: "userSpaceOnUse" }, ctx.defs);
        el("rect", {
          x: Math.min(segment.x1, segment.x2) - (segment.horizontal ? 0 : pad),
          y: Math.min(segment.y1, segment.y2) - (segment.horizontal ? pad : 0),
          width: segment.horizontal ? segment.length : pad * 2,
          height: segment.horizontal ? pad * 2 : segment.length,
        }, clip);

        var holder = el("g", { "clip-path": "url(#" + id + ")" }, tiles);
        var tile = el("g", { class: "flow-tile dir-" + travel(segment) }, holder);
        // Extended one period beyond both ends so the translation never
        // exposes an uncovered stretch.
        var ext = DASH_PERIOD;
        var d;
        if (segment.horizontal) {
          var sx = segment.x2 > segment.x1 ? segment.x1 - ext : segment.x1 + ext;
          var ex = segment.x2 > segment.x1 ? segment.x2 + ext : segment.x2 - ext;
          d = "M" + sx + " " + segment.y1 + " H" + ex;
        } else {
          var sy = segment.y2 > segment.y1 ? segment.y1 - ext : segment.y1 + ext;
          var ey = segment.y2 > segment.y1 ? segment.y2 + ext : segment.y2 - ext;
          d = "M" + segment.x1 + " " + sy + " V" + ey;
        }
        el("path", { d: d, "stroke-dashoffset": segment.before % DASH_PERIOD }, tile);
      });
    });
  };

  // One SVG <pattern> per (orientation, speed, direction), shared by every
  // pipe that needs it, animated with SMIL. The number of running animations
  // is therefore constant no matter how many pipes exist.
  renderers["svg-pattern"] = function (pipes, ctx) {
    var made = {};
    function pattern(horizontal, speed, reverse, color) {
      var id = "pat-" + (horizontal ? "h" : "v") + "-" + speed + "-" +
        (reverse ? "rev" : "fwd") + "-" + color.replace("#", "");
      if (made[id]) return id;
      made[id] = true;
      var node = el("pattern", {
        id: id,
        patternUnits: "userSpaceOnUse",
        width: horizontal ? DASH_PERIOD : 1,
        height: horizontal ? 1 : DASH_PERIOD,
      }, ctx.defs);
      el("rect", {
        x: 0, y: 0,
        width: horizontal ? DASH_ON : 1,
        height: horizontal ? 1 : DASH_ON,
        fill: color,
      }, node);
      var from = "0 0";
      var to = horizontal ? DASH_PERIOD + " 0" : "0 " + DASH_PERIOD;
      el("animateTransform", {
        attributeName: "patternTransform",
        type: "translate",
        from: reverse ? to : from,
        to: reverse ? from : to,
        dur: SPEED_SECONDS[speed] + "s",
        repeatCount: "indefinite",
      }, node);
      return id;
    }

    pipes.forEach(function (pipe) {
      var group = ctx.group(pipe);
      var paint = el("g", { class: "energy-halo" }, group);
      paint.style.opacity = String(pipe.active ? pipe.style.alpha : 0.1);
      pipe.segments.forEach(function (segment) {
        var width = pipe.active ? pipe.style.width : 1.5;
        var half = width / 2;
        var id = pattern(segment.horizontal, pipe.speed, pipe.reverse, pipe.color);
        el("rect", {
          x: Math.min(segment.x1, segment.x2) - (segment.horizontal ? half : half),
          y: Math.min(segment.y1, segment.y2) - (segment.horizontal ? half : half),
          width: segment.horizontal ? segment.length + width : width,
          height: segment.horizontal ? width : segment.length + width,
          fill: "url(#" + id + ")",
        }, paint);
      });
    });
  };

  // The same idea expressed as a mask: a solid pipe, revealed through a
  // striped layer that is moved with a CSS transform. Masks are shared the
  // same way the patterns are.
  renderers["svg-mask"] = function (pipes, ctx) {
    var made = {};
    function mask(horizontal, speed, reverse) {
      var id = "mask-" + (horizontal ? "h" : "v") + "-" + speed + "-" + (reverse ? "rev" : "fwd");
      if (made[id]) return id;
      made[id] = true;
      var stripeId = "stripe-" + (horizontal ? "h" : "v");
      if (!made[stripeId]) {
        made[stripeId] = true;
        var pat = el("pattern", {
          id: stripeId,
          patternUnits: "userSpaceOnUse",
          width: horizontal ? DASH_PERIOD : 1,
          height: horizontal ? 1 : DASH_PERIOD,
        }, ctx.defs);
        el("rect", {
          x: 0, y: 0,
          width: horizontal ? DASH_ON : 1,
          height: horizontal ? 1 : DASH_ON,
          fill: "#fff",
        }, pat);
      }
      var node = el("mask", {
        id: id,
        maskUnits: "userSpaceOnUse",
        x: -DASH_PERIOD, y: -DASH_PERIOD,
        width: ctx.layout.w + DASH_PERIOD * 2,
        height: ctx.layout.h + DASH_PERIOD * 2,
      }, ctx.defs);
      var mover = el("g", {
        class: "flow-tile dir-" + (horizontal ? "right" : "down") + (reverse ? " reverse" : ""),
      }, node);
      mover.style.animationDuration = SPEED_SECONDS[speed] + "s";
      if (reverse) mover.style.animationDirection = "reverse";
      el("rect", {
        x: -DASH_PERIOD, y: -DASH_PERIOD,
        width: ctx.layout.w + DASH_PERIOD * 2,
        height: ctx.layout.h + DASH_PERIOD * 2,
        fill: "url(#" + stripeId + ")",
      }, mover);
      return id;
    }

    pipes.forEach(function (pipe) {
      var group = ctx.group(pipe);
      var paint = el("g", { class: "energy-halo" }, group);
      paint.style.opacity = String(pipe.active ? pipe.style.alpha : 0.1);
      var horizontal = [];
      var vertical = [];
      pipe.segments.forEach(function (segment) {
        var d = segment.horizontal
          ? "M" + segment.x1 + " " + segment.y1 + " H" + segment.x2
          : "M" + segment.x1 + " " + segment.y1 + " V" + segment.y2;
        (segment.horizontal ? horizontal : vertical).push(d);
      });
      if (horizontal.length) {
        el("path", {
          class: "pipe-paint",
          d: horizontal.join(" "),
          mask: "url(#" + mask(true, pipe.speed, pipe.reverse) + ")",
        }, paint);
      }
      if (vertical.length) {
        el("path", {
          class: "pipe-paint",
          d: vertical.join(" "),
          mask: "url(#" + mask(false, pipe.speed, pipe.reverse) + ")",
        }, paint);
      }
    });
  };

  // The energy layer leaves SVG entirely: one HTML element per segment,
  // clipping a repeating-gradient strip that is moved with translate3d.
  renderers["dom-tiles"] = function (pipes, ctx) {
    var scale = ctx.layout.scale;
    var metaphor = ctx.metaphor || "dash";
    var thickness = metaphorThickness(metaphor);
    pipes.forEach(function (pipe) {
      ctx.group(pipe);
      var width = (pipe.active ? pipe.style.width : 1.5) * thickness;
      pipe.segments.forEach(function (segment) {
        var horizontal = segment.horizontal;
        var period = DASH_PERIOD * scale;
        var on = DASH_ON * scale;
        var paint = metaphorBackground(metaphor, horizontal, pipe.color, on, period, width * scale);
        var box = div("dom-pipe energy-halo" + (pipe.reverse ? " reverse" : "") +
          (ctx.motion === "off" || !pipe.active ? " paused" : ""), ctx.overlay);
        box.style.left = (Math.min(segment.x1, segment.x2) - (horizontal ? 0 : width / 2)) * scale + "px";
        box.style.top = (Math.min(segment.y1, segment.y2) - (horizontal ? width / 2 : 0)) * scale + "px";
        box.style.width = (horizontal ? segment.length : width) * scale + "px";
        box.style.height = (horizontal ? width : segment.length) * scale + "px";
        box.style.color = pipe.color;
        box.style.opacity = String(pipe.active ? pipe.style.alpha : 0.1);
        box.style.setProperty("--tile-step", paint.step + "px");
        box.style.setProperty("--tile-speed", SPEED_SECONDS[pipe.speed] + "s");

        var inner = div("dom-pipe-inner dir-" + travel(segment), box);
        // One whole pattern of overhang at each end, so the layer can be
        // translated by exactly one step and wrap without a visible seam.
        var phase = -((segment.before % DASH_PERIOD) * scale);
        if (horizontal) {
          inner.style.left = -paint.step + "px";
          inner.style.right = -paint.step + "px";
        } else {
          inner.style.top = -paint.step + "px";
          inner.style.bottom = -paint.step + "px";
        }
        inner.style.backgroundImage = paint.image;
        inner.style.backgroundSize = paint.size;
        inner.style.backgroundRepeat = paint.repeat;
        if (horizontal) inner.style.backgroundPositionX = phase + "px";
        else inner.style.backgroundPositionY = phase + "px";
      });
    });
  };

  // Canvas 2D drawn on a worker thread through OffscreenCanvas.
  //
  // The previous investigation rejected canvas partly because its per-frame
  // repaint cost Firefox everything: 5.2 fps with the loop running against
  // 60.1 with it stopped. That measurement was of MAIN-THREAD work. If the
  // raster happens on a worker the main thread never sees it, which is the
  // only version of the canvas idea that can survive that result -- so it is
  // worth measuring rather than assuming.
  //
  // Workers have no requestAnimationFrame (it is not exposed on
  // DedicatedWorkerGlobalScope in either engine), so the loop self-schedules
  // and takes its phase from performance.now(). Jittered ticks therefore
  // change smoothness but never drift.
  renderers["canvas-worker"] = function (pipes, ctx) {
    pipes.forEach(function (pipe) { ctx.group(pipe); });
    var canvas = ctx.canvas;
    var scale = ctx.layout.scale;
    canvas.width = Math.round(ctx.layout.w * scale);
    canvas.height = Math.round(ctx.layout.h * scale);
    canvas.style.width = canvas.width + "px";
    canvas.style.height = canvas.height + "px";

    if (typeof canvas.transferControlToOffscreen !== "function") {
      window.__labError = "OffscreenCanvas unavailable";
      renderers.canvas(pipes, ctx);
      return;
    }

    var payload = pipes.map(function (pipe) {
      return {
        active: pipe.active,
        reverse: !!pipe.reverse,
        color: pipe.color,
        alpha: pipe.active ? pipe.style.alpha : 0.1,
        width: (pipe.active ? pipe.style.width : 1.5) * scale,
        seconds: SPEED_SECONDS[pipe.speed],
        points: [
          [pipe.segments[0].x1 * scale, pipe.segments[0].y1 * scale],
          [pipe.segments[0].x2 * scale, pipe.segments[0].y2 * scale],
          [pipe.segments[1].x2 * scale, pipe.segments[1].y2 * scale],
          [pipe.segments[2].x2 * scale, pipe.segments[2].y2 * scale],
        ],
      };
    });

    var source = [
      "let pipes = [], W = 0, H = 0, c2d = null, on = 34, period = 52;",
      "let running = false, glow = true, scale = 1;",
      "function draw(t) {",
      "  c2d.clearRect(0, 0, W, H);",
      "  c2d.lineCap = 'round'; c2d.lineJoin = 'round';",
      "  for (const p of pipes) {",
      "    const travelled = ((t / 1000) % p.seconds) / p.seconds * period;",
      "    c2d.save();",
      "    c2d.globalAlpha = p.alpha; c2d.strokeStyle = p.color; c2d.lineWidth = p.width;",
      "    if (p.active) { c2d.setLineDash([on * scale, (period - on) * scale]);",
      "      c2d.lineDashOffset = (p.reverse ? travelled : -travelled) * scale; }",
      "    else c2d.setLineDash([]);",
      "    c2d.beginPath(); c2d.moveTo(p.points[0][0], p.points[0][1]);",
      "    for (let i = 1; i < p.points.length; i += 1) c2d.lineTo(p.points[i][0], p.points[i][1]);",
      "    if (p.active && glow) {",
      "      for (const layer of [[5.0,0.05],[3.0,0.09],[1.8,0.16]]) {",
      "        c2d.lineWidth = p.width * layer[0]; c2d.globalAlpha = p.alpha * layer[1]; c2d.stroke(); }",
      "      c2d.lineWidth = p.width; c2d.globalAlpha = p.alpha;",
      "    }",
      "    c2d.stroke(); c2d.restore();",
      "  }",
      "}",
      "function tick() { if (!running) return; draw(performance.now()); setTimeout(tick, 16); }",
      "self.onmessage = (event) => {",
      "  const d = event.data;",
      "  if (d.type === 'init') {",
      "    c2d = d.canvas.getContext('2d'); W = d.canvas.width; H = d.canvas.height;",
      "    pipes = d.pipes; glow = d.glow; scale = d.scale; on = d.on; period = d.period;",
      "    if (d.motion === 'on') { running = true; tick(); } else { draw(0); }",
      "    self.postMessage({ type: 'ready' });",
      "  }",
      "};",
    ].join("\n");

    var blob = new Blob([source], { type: "text/javascript" });
    var worker = new Worker(URL.createObjectURL(blob));
    window.__labWorker = worker;
    var offscreen = canvas.transferControlToOffscreen();
    worker.postMessage({
      type: "init",
      canvas: offscreen,
      pipes: payload,
      glow: ctx.glow === "both" || ctx.glow === "energy-only",
      scale: scale,
      on: DASH_ON,
      period: DASH_PERIOD,
      motion: ctx.motion,
    }, [offscreen]);
  };

  // WebGL: every segment in the scene as one instanced draw call.
  //
  // The dash and its glow are the same signed distance field, so the glow
  // needs no blur pass, no second target and no shadowBlur -- it is an
  // exponential falloff on a distance the fragment shader already computed.
  // That is the property that makes GPU rendering interesting here: the glow
  // is what made every other technique expensive, and this is the only one
  // where it is nearly free.
  var VERT = [
    "#version 300 es",
    "in vec2 a_corner;",
    "in vec2 i_p0;",
    "in vec2 i_p1;",
    "in vec4 i_params;",   // width, seconds, before, flags
    "in vec4 i_color;",
    "uniform vec2 u_resolution;",
    "uniform float u_glowPad;",
    "out vec2 v_local;",
    "flat out float v_len;",
    "flat out vec4 v_color;",
    "flat out vec4 v_params;",
    "void main() {",
    "  vec2 d = i_p1 - i_p0;",
    "  float len = length(d);",
    "  vec2 dir = len > 0.0 ? d / len : vec2(1.0, 0.0);",
    "  vec2 nrm = vec2(-dir.y, dir.x);",
    "  float halfW = i_params.x * 0.5;",
    "  float pad = halfW * u_glowPad;",
    "  float along = a_corner.x * (len + 2.0 * pad) - pad;",
    "  float across = (a_corner.y - 0.5) * 2.0 * (halfW + pad);",
    "  vec2 pos = i_p0 + dir * along + nrm * across;",
    "  v_local = vec2(along, across);",
    "  v_len = len;",
    "  v_color = i_color;",
    "  v_params = i_params;",
    "  vec2 clip = (pos / u_resolution) * 2.0 - 1.0;",
    "  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);",
    "}",
  ].join("\n");

  var FRAG = [
    "#version 300 es",
    "precision highp float;",
    "in vec2 v_local;",
    "flat in float v_len;",
    "flat in vec4 v_color;",
    "flat in vec4 v_params;",
    "uniform float u_time;",
    "uniform float u_dashOn;",
    "uniform float u_dashPeriod;",
    "uniform float u_glowStrength;",
    "out vec4 outColor;",
    "float capsule(vec2 p, float halfLen, float radius) {",
    "  vec2 q = vec2(max(abs(p.x) - halfLen, 0.0), p.y);",
    "  return length(q) - radius;",
    "}",
    "void main() {",
    "  float halfW = v_params.x * 0.5;",
    "  float seconds = v_params.y;",
    "  float before = v_params.z;",
    "  float flags = v_params.w;",
    "  bool isReverse = mod(flags, 2.0) >= 0.5;",
    "  bool isActive = flags >= 2.0;",
    "  float along = v_local.x;",
    "  float across = v_local.y;",
    "  float d;",
    "  if (isActive) {",
    "    float travelled = fract(u_time / seconds) * u_dashPeriod;",
    "    float s = along + before + (isReverse ? travelled : -travelled);",
    "    float ph = mod(s, u_dashPeriod);",
    "    float halfDash = u_dashOn * 0.5;",
    "    float radius = min(halfW, halfDash);",
    // A dash straddling the period boundary is evaluated twice, once for this
    // period and once for the previous one, so the wrap is not a visible cut.
    "    float d0 = capsule(vec2(ph - halfDash, across), halfDash - radius, radius);",
    "    float d1 = capsule(vec2(ph - u_dashPeriod - halfDash, across), halfDash - radius, radius);",
    "    d = min(d0, d1);",
    "  } else {",
    "    d = abs(across) - halfW;",
    "  }",
    // Clip to the segment so the padded quad does not paint past the ends.
    "  float endDist = max(-along, along - v_len);",
    "  d = max(d, endDist - halfW);",
    "  float core = 1.0 - smoothstep(-1.0, 1.0, d);",
    "  float glow = exp(-max(d, 0.0) / max(halfW * 1.6, 1.0)) * u_glowStrength;",
    "  float a = clamp(core + glow, 0.0, 1.0) * v_color.a;",
    "  if (a <= 0.002) discard;",
    "  outColor = vec4(v_color.rgb * a, a);",
    "}",
  ].join("\n");

  function compile(gl, type, source) {
    var shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      window.__labError = "shader: " + gl.getShaderInfoLog(shader);
      return null;
    }
    return shader;
  }

  renderers.webgl = function (pipes, ctx) {
    pipes.forEach(function (pipe) { ctx.group(pipe); });
    var canvas = ctx.canvas;
    var scale = ctx.layout.scale;
    canvas.width = Math.round(ctx.layout.w * scale);
    canvas.height = Math.round(ctx.layout.h * scale);
    canvas.style.width = canvas.width + "px";
    canvas.style.height = canvas.height + "px";

    var gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false });
    if (!gl) {
      // A 24/7 wall panel will meet a lost or refused context eventually. The
      // renderer must degrade to something that still shows energy moving.
      window.__labError = "webgl2 unavailable";
      renderers["dom-tiles"](pipes, ctx);
      return;
    }

    var vs = compile(gl, gl.VERTEX_SHADER, VERT);
    var fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) { renderers["dom-tiles"](pipes, ctx); return; }
    var program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      window.__labError = "link: " + gl.getProgramInfoLog(program);
      renderers["dom-tiles"](pipes, ctx);
      return;
    }
    gl.useProgram(program);

    var instances = [];
    pipes.forEach(function (pipe) {
      var rgb = hexRgb(pipe.color);
      var width = (pipe.active ? pipe.style.width : 1.5) * scale;
      var flags = (pipe.reverse ? 1 : 0) + (pipe.active ? 2 : 0);
      pipe.segments.forEach(function (segment) {
        instances.push(
          segment.x1 * scale, segment.y1 * scale,
          segment.x2 * scale, segment.y2 * scale,
          width, SPEED_SECONDS[pipe.speed], segment.before * scale, flags,
          rgb[0] / 255, rgb[1] / 255, rgb[2] / 255,
          pipe.active ? pipe.style.alpha : 0.1
        );
      });
    });
    var count = instances.length / 12;

    var quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
    var aCorner = gl.getAttribLocation(program, "a_corner");
    gl.enableVertexAttribArray(aCorner);
    gl.vertexAttribPointer(aCorner, 2, gl.FLOAT, false, 0, 0);

    var buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(instances), gl.STATIC_DRAW);
    var stride = 12 * 4;
    [["i_p0", 2, 0], ["i_p1", 2, 8], ["i_params", 4, 16], ["i_color", 4, 32]].forEach(function (spec) {
      var loc = gl.getAttribLocation(program, spec[0]);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, spec[1], gl.FLOAT, false, stride, spec[2]);
      gl.vertexAttribDivisor(loc, 1);
    });

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.viewport(0, 0, canvas.width, canvas.height);

    var uRes = gl.getUniformLocation(program, "u_resolution");
    var uTime = gl.getUniformLocation(program, "u_time");
    var uOn = gl.getUniformLocation(program, "u_dashOn");
    var uPeriod = gl.getUniformLocation(program, "u_dashPeriod");
    var uGlow = gl.getUniformLocation(program, "u_glowStrength");
    var uPad = gl.getUniformLocation(program, "u_glowPad");
    gl.uniform2f(uRes, canvas.width, canvas.height);
    gl.uniform1f(uOn, DASH_ON * scale);
    gl.uniform1f(uPeriod, DASH_PERIOD * scale);
    gl.uniform1f(uGlow, ctx.glow === "both" || ctx.glow === "energy-only" ? 0.55 : 0.0);
    gl.uniform1f(uPad, 6.0);

    window.__labGl = { instances: count, renderer: "webgl2" };

    function draw(nowMs) {
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform1f(uTime, nowMs / 1000);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, count);
    }

    if (ctx.motion === "off") { draw(0); return; }
    var raf = function (now) {
      draw(now);
      window.__labRaf = requestAnimationFrame(raf);
    };
    window.__labRaf = requestAnimationFrame(raf);
  };

  // CSS motion-path: a handful of capsules per pipe travelling the whole path.
  // The dash spacing becomes total/N rather than 52 because the path length is
  // not a whole number of periods; that is a fidelity compromise, not a bug.
  renderers["motion-path"] = function (pipes, ctx) {
    var scale = ctx.layout.scale;
    pipes.forEach(function (pipe) {
      ctx.group(pipe);
      var count = Math.max(1, Math.round(pipe.total / DASH_PERIOD));
      var travelSeconds = (pipe.total / DASH_PERIOD) * SPEED_SECONDS[pipe.speed];
      var host = div("motion-host" + (ctx.motion === "off" || !pipe.active ? " paused" : ""), ctx.overlay);
      var s = pipe.segments;
      var path = "path('M" + s[0].x1 * scale + " " + s[0].y1 * scale +
        " H" + s[0].x2 * scale +
        " V" + s[1].y2 * scale +
        " H" + s[2].x2 * scale + "')";
      var width = pipe.active ? pipe.style.width : 1.5;
      for (var k = 0; k < count; k += 1) {
        var dash = div("motion-dash energy-halo", host);
        dash.style.width = DASH_ON * scale + "px";
        dash.style.height = width * scale + "px";
        dash.style.borderRadius = (width * scale) / 2 + "px";
        dash.style.background = pipe.color;
        dash.style.opacity = String(pipe.active ? pipe.style.alpha : 0.1);
        dash.style.offsetPath = path;
        dash.style.setProperty("--travel-speed", travelSeconds + "s");
        var offset = (k * travelSeconds) / count;
        dash.style.animationDelay = (pipe.reverse ? offset : -offset) + "s";
        if (pipe.reverse) dash.style.animationDirection = "reverse";
      }
    });
  };

  // The halo without a per-frame blur: the same dash stroked a few times, wider
  // and fainter each time. A canvas shadowBlur is a gaussian blur recomputed
  // every frame and Firefox charges for it; stacked strokes are ordinary fill.
  var BLOOM_LAYERS = [
    { width: 5.0, alpha: 0.05 },
    { width: 3.0, alpha: 0.09 },
    { width: 1.8, alpha: 0.16 },
  ];

  // One canvas, one requestAnimationFrame, every pipe drawn per frame. The
  // number of running animations is one regardless of how many pipes exist.
  renderers.canvas = function (pipes, ctx) {
    pipes.forEach(function (pipe) { ctx.group(pipe); });
    var canvas = ctx.canvas;
    var scale = ctx.layout.scale;
    canvas.width = Math.round(ctx.layout.w * scale);
    canvas.height = Math.round(ctx.layout.h * scale);
    canvas.style.width = canvas.width + "px";
    canvas.style.height = canvas.height + "px";
    var c2d = canvas.getContext("2d");
    var bloom = ctx.bloom;

    function draw(nowMs) {
      var t = nowMs / 1000;
      c2d.clearRect(0, 0, canvas.width, canvas.height);
      c2d.lineCap = "round";
      c2d.lineJoin = "round";
      for (var i = 0; i < pipes.length; i += 1) {
        var pipe = pipes[i];
        var seconds = SPEED_SECONDS[pipe.speed];
        var travelled = ((t % seconds) / seconds) * DASH_PERIOD;
        var offset = pipe.reverse ? travelled : -travelled;
        c2d.save();
        c2d.globalAlpha = pipe.active ? pipe.style.alpha : 0.1;
        c2d.strokeStyle = pipe.color;
        c2d.lineWidth = (pipe.active ? pipe.style.width : 1.5) * scale;
        if (pipe.active) {
          c2d.setLineDash([DASH_ON * scale, (DASH_PERIOD - DASH_ON) * scale]);
          c2d.lineDashOffset = offset * scale;
        } else {
          c2d.setLineDash([]);
        }
        var s = pipe.segments;
        var width = c2d.lineWidth;
        var alpha = c2d.globalAlpha;
        c2d.beginPath();
        c2d.moveTo(s[0].x1 * scale, s[0].y1 * scale);
        c2d.lineTo(s[0].x2 * scale, s[0].y2 * scale);
        c2d.lineTo(s[1].x2 * scale, s[1].y2 * scale);
        c2d.lineTo(s[2].x2 * scale, s[2].y2 * scale);
        var haloed = pipe.active && (ctx.glow === "both" || ctx.glow === "energy-only");
        if (haloed && bloom) {
          for (var b = 0; b < BLOOM_LAYERS.length; b += 1) {
            c2d.lineWidth = width * BLOOM_LAYERS[b].width;
            c2d.globalAlpha = alpha * BLOOM_LAYERS[b].alpha;
            c2d.stroke();
          }
          c2d.lineWidth = width;
          c2d.globalAlpha = alpha;
        } else if (haloed) {
          c2d.shadowColor = pipe.color;
          c2d.shadowBlur = 15 * scale;
          c2d.stroke();
          c2d.shadowBlur = 6 * scale;
        }
        c2d.stroke();
        c2d.restore();
      }
    }

    if (ctx.motion === "off") {
      draw(0);
      return;
    }
    var raf = function (now) {
      draw(now);
      window.__labRaf = requestAnimationFrame(raf);
    };
    window.__labRaf = requestAnimationFrame(raf);
  };

  // `?renderer=` names one of the entries above. It is matched against the
  // table's own keys instead of indexing the table with it, because every
  // object answers to `constructor`, `toString` and `valueOf`: indexing finds
  // one of those, it is truthy, and it gets called in place of the fallback.
  function pickRenderer(table, name) {
    var names = Object.keys(table);
    for (var i = 0; i < names.length; i++) {
      if (names[i] === name) return table[names[i]];
    }
    return table.dashoffset;
  }

  // ------------------------------------------------------------------ main

  function run() {
    var opts = params();
    var layout = grid(opts.flows);
    var pipes = buildPipes(opts, layout);

    var stage = document.getElementById("stage");
    var svg = document.getElementById("flowSvg");
    var overlay = document.getElementById("overlay");
    var canvas = document.getElementById("flowCanvas");

    stage.className = "renderer-" + (opts.renderer === "canvas-bloom" ? "canvas" : opts.renderer);
    if (opts.glow === "static-only" || opts.glow === "none") {
      document.body.classList.add("no-energy-halo");
    }
    if (opts.glow === "energy-only" || opts.glow === "none") {
      document.body.classList.add("no-static-glow");
    }
    stage.style.width = layout.w * layout.scale + "px";
    stage.style.height = layout.h * layout.scale + "px";
    svg.setAttribute("viewBox", "0 0 " + layout.w + " " + layout.h);
    svg.setAttribute("width", layout.w * layout.scale);
    svg.setAttribute("height", layout.h * layout.scale);

    var defs = el("defs", {}, svg);
    var context = {
      defs: defs,
      overlay: overlay,
      canvas: canvas,
      layout: layout,
      motion: opts.motion,
      glow: opts.glow,
      metaphor: opts.metaphor,
      group: function (pipe) { return pipeGroup(pipe, svg); },
    };

    var render = pickRenderer(renderers, opts.renderer);
    render(pipes, context);

    if (opts.motion === "off") {
      var style = document.createElement("style");
      style.textContent =
        "*, *::before, *::after { animation-play-state: paused !important; }";
      document.head.appendChild(style);
      // SMIL ignores animation-play-state; the SVG element pauses it.
      if (typeof svg.pauseAnimations === "function") svg.pauseAnimations();
    }

    window.__lab = {
      renderer: opts.renderer,
      flows: opts.flows,
      motion: opts.motion,
      glow: opts.glow,
      metaphor: opts.metaphor,
      cols: layout.cols,
      rows: layout.rows,
      scale: Number(layout.scale.toFixed(4)),
      stageW: Math.round(layout.w * layout.scale),
      stageH: Math.round(layout.h * layout.scale),
      paintedPx: Math.round(layout.w * layout.scale) * Math.round(layout.h * layout.scale),
      svgElements: svg.getElementsByTagName("*").length,
      overlayElements: overlay.getElementsByTagName("*").length,
      webgl: (function () {
        try {
          var probe = document.createElement("canvas");
          var gl = probe.getContext("webgl2") || probe.getContext("webgl");
          if (!gl) return { available: false };
          var info = gl.getExtension("WEBGL_debug_renderer_info");
          return {
            available: true,
            version: gl.getParameter(gl.VERSION),
            renderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null,
          };
        } catch (error) {
          return { available: false, error: String(error) };
        }
      })(),
      ready: true,
    };

    document.getElementById("labInfo").textContent =
      opts.renderer + " | flows " + opts.flows + " | motion " + opts.motion +
      " | " + window.__lab.stageW + "x" + window.__lab.stageH +
      " | svg " + window.__lab.svgElements +
      " | dom " + window.__lab.overlayElements;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run, { once: true });
  } else {
    run();
  }
})();
