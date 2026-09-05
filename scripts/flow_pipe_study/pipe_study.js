// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Energy pipe study. One scene, one animation mechanism family, nine ways of
// building the pipe and its moving token. Geometry, colours, speed, phase,
// magnitude and viewport are held constant so a difference between two runs is
// a difference in how the pipe is painted and nothing else.
//
// Loaded by index.html (benchmark) and gallery.html (visual comparison).

(function (global) {
  "use strict";

  var CELL_W = 260;
  var CELL_H = 120;
  var STAGE_W = 1440;
  var STAGE_H = 820;
  var DASH_ON = 34;
  var DASH_PERIOD = 52;

  var SHAPES = {
    normal: [
      { x1: 20, y1: 30, x2: 108, y2: 30 },
      { x1: 108, y1: 30, x2: 108, y2: 97 },
      { x1: 108, y1: 97, x2: 188, y2: 97 },
    ],
    // Every segment shorter than one dash period, which is the case a tiled
    // background and a traversing token disagree about most.
    short: [
      { x1: 20, y1: 40, x2: 44, y2: 40 },
      { x1: 44, y1: 40, x2: 44, y2: 64 },
      { x1: 44, y1: 64, x2: 68, y2: 64 },
    ],
    long: [
      { x1: 20, y1: 40, x2: 240, y2: 40 },
      { x1: 240, y1: 40, x2: 240, y2: 80 },
      { x1: 240, y1: 80, x2: 248, y2: 80 },
    ],
    mixed: [
      { x1: 20, y1: 30, x2: 130, y2: 30 },
      { x1: 130, y1: 30, x2: 130, y2: 42 },
      { x1: 130, y1: 42, x2: 244, y2: 42 },
    ],
  };
  var SHAPE = SHAPES.normal;

  var KINDS = ["pv", "battery", "output", "grid"];
  var COLORS = {
    pv: "#ffd166",
    battery: "#39e58c",
    output: "#38d5ff",
    grid: "#d87cff",
    idle: "#64748b",
  };
  var SPEED_SECONDS = { idle: 1.85, low: 1.7, medium: 1.38, high: 1.1 };
  var SPEED_ALPHA = { idle: 0.12, low: 0.48, medium: 0.68, high: 0.9 };

  var WATT_SAMPLES = [40, 100, 170, 300, 690, 1200, 2000, 3000];

  var RIBBON_IDLE_W = 3;
  var RIBBON_MIN_W = 4;
  var RIBBON_MAX_W = 15;
  var RIBBON_CEILING_W = 22;
  var SCALE_LADDER = [250, 500, 1000, 2000, 3000, 5000, 8000, 12000];

  var GLOWS = ["none", "static", "texture", "blur", "filter", "layered", "blend"];
  var ANIMS = ["var", "waapi"];

  // How much halo, relative to the pipe's own thickness. One number, so the
  // seven implementations differ in mechanism and not in how much they glow.
  var GLOW_SPREAD = 1.15;

  var TILED = [
    "capsule", "rect", "gradient-capsule", "repeating",
    "arrow", "comet", "particles", "wave", "plasma",
  ];

  var CANDIDATES = [
    {
      id: "capsule",
      letter: "A",
      name: "Capsule (control)",
      blurb: "Production. One moved layer per segment, tiled rounded-rect data: URI.",
    },
    {
      id: "rect",
      letter: "B",
      name: "Gradient rectangle",
      blurb: "Cheapest possible: a hard-stop linear-gradient, square ends, no image.",
    },
    {
      id: "radius-el",
      letter: "C",
      name: "border-radius elements",
      blurb: "Same moved layer, but real divs with border-radius instead of a background image.",
    },
    {
      id: "gradient-capsule",
      letter: "D",
      name: "CSS gradient capsule",
      blurb: "Rounded ends built from two pixel-exact radial-gradients plus a linear body.",
    },
    {
      id: "repeating",
      letter: "E",
      name: "repeating-linear-gradient",
      blurb: "One repeating gradient instead of a sized, repeated tile.",
    },
    {
      id: "tokens",
      letter: "F",
      name: "Multiple small tokens",
      blurb: "N separately animated capsules per segment, staggered. N is the axis.",
    },
    {
      id: "core",
      letter: "G",
      name: "Static pipe + moving core",
      blurb: "The pipe is painted once; only a short bright core is animated.",
    },
    {
      id: "pulse",
      letter: "H",
      name: "Static pipe + pulse",
      blurb: "Static pipe with a wide soft brightness travelling along it.",
    },
    {
      id: "arrow",
      letter: "J",
      name: "Directional capsule",
      blurb: "The control's tile with a pointed nose, mirrored for reversed pipes. Direction is readable in a still frame.",
    },
    {
      id: "plasma",
      letter: "K",
      name: "Plasma stream",
      blurb: "A bright core inside a soft sheath, with a second faster, fainter layer behind it. Reads as flowing light rather than as a dash pattern.",
    },
    {
      id: "comet",
      letter: "L",
      name: "Comet",
      blurb: "A bright head with a fading tail. Asymmetric, so direction survives a still frame without an arrowhead.",
    },
    {
      id: "particles",
      letter: "M",
      name: "Particle stream",
      blurb: "Many small quanta in several lanes. Density carries magnitude alongside thickness, so power is legible without a neighbour to compare against.",
    },
    {
      id: "wave",
      letter: "N",
      name: "Luminance wave",
      blurb: "No discrete token at all: a smooth brightness wave travelling along a continuous tube.",
    },
    {
      id: "minimal",
      letter: "I",
      name: "Minimal token",
      blurb: "Lower bound: a thin static pipe and one small dot per segment.",
    },
  ];

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function scaleReference(maxWatts) {
    var value = isFinite(maxWatts) ? Math.abs(maxWatts) : 0;
    for (var i = 0; i < SCALE_LADDER.length; i += 1) {
      if (value <= SCALE_LADDER[i]) return SCALE_LADDER[i];
    }
    return SCALE_LADDER[SCALE_LADDER.length - 1];
  }

  function ribbonWidth(watts, reference, active) {
    if (!active) return RIBBON_IDLE_W;
    var value = isFinite(watts) ? Math.abs(watts) : 0;
    var share = clamp(value / (reference || 1000), 0, 1);
    var width = RIBBON_MIN_W + (RIBBON_MAX_W - RIBBON_MIN_W) * share;
    return Math.min(RIBBON_CEILING_W, Math.round(width * 2) / 2);
  }

  function speedBucket(watts, active) {
    if (!active) return "idle";
    var value = Math.abs(Number(watts || 0));
    if (value >= 600) return "high";
    if (value >= 150) return "medium";
    return "low";
  }

  function hexRgb(hex) {
    var value = String(hex).replace("#", "");
    if (value.length === 3) {
      value = value[0] + value[0] + value[1] + value[1] + value[2] + value[2];
    }
    var n = parseInt(value, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  // Every stop is an explicit rgba(r,g,b,0) rather than `transparent`, which is
  // transparent BLACK: CSS interpolates through it and leaves a dark fringe on
  // the trailing edge of every token.
  function rgba(rgb, alpha) {
    return "rgba(" + rgb[0] + ", " + rgb[1] + ", " + rgb[2] + ", " + alpha + ")";
  }

  function svgUrl(markup) {
    return 'url("data:image/svg+xml;utf8,' + encodeURIComponent(markup) + '")';
  }

  function grid(flows, forcedCols, stageW, stageH) {
    var sw = stageW || STAGE_W;
    var sh = stageH || STAGE_H;
    var cols;
    var rows;
    if (forcedCols) {
      cols = Math.max(1, Math.min(flows, forcedCols));
      rows = Math.ceil(flows / cols);
    } else {
      var target = sw / sh;
      rows = Math.ceil(Math.sqrt((flows * CELL_W) / (target * CELL_H)));
      rows = Math.max(1, Math.min(rows, flows));
      cols = Math.ceil(flows / rows);
    }
    var scale = Math.min(sw / (cols * CELL_W), sh / (rows * CELL_H));
    return { rows: rows, cols: cols, scale: scale, w: cols * CELL_W, h: rows * CELL_H };
  }

  function wattsFor(opts, index) {
    if (opts.watts === "mixed") return WATT_SAMPLES[index % WATT_SAMPLES.length];
    var value = Number(opts.watts);
    return isFinite(value) ? value : 690;
  }

  function buildScene(opts) {
    var layout = grid(opts.flows, opts.cols, opts.stageW, opts.stageH);
    var wattList = [];
    for (var w = 0; w < opts.flows; w += 1) wattList.push(wattsFor(opts, w));
    var maxWatts = wattList.reduce(function (a, b) { return Math.max(a, Math.abs(b)); }, 0);
    var reference = scaleReference(maxWatts);

    var pipes = [];
    for (var i = 0; i < opts.flows; i += 1) {
      var col = i % layout.cols;
      var row = Math.floor(i / layout.cols);
      var ox = col * CELL_W;
      var oy = row * CELL_H;
      var watts = wattList[i];
      var active = Math.abs(watts) > 0;
      var reverse = opts.reverse === "all" ? true : (opts.reverse === "none" ? false : i % 5 === 4);
      if (watts < 0) reverse = !reverse;
      var bucket = opts.speeds === "single" ? (active ? "medium" : "idle") : speedBucket(watts, active);
      var kind = KINDS[i % KINDS.length];
      var cumulative = 0;
      var shape = SHAPES[opts.shape] || SHAPES.normal;
      var segments = shape.map(function (s) {
        var seg = {
          x1: s.x1 + ox, y1: s.y1 + oy, x2: s.x2 + ox, y2: s.y2 + oy,
        };
        seg.horizontal = seg.y1 === seg.y2;
        seg.length = Math.abs(seg.x2 - seg.x1) + Math.abs(seg.y2 - seg.y1);
        seg.direction = seg.horizontal
          ? (seg.x2 > seg.x1 ? "right" : "left")
          : (seg.y2 > seg.y1 ? "down" : "up");
        seg.before = cumulative;
        cumulative += seg.length;
        return seg;
      });
      pipes.push({
        index: i,
        kind: kind,
        color: active ? COLORS[kind] : COLORS.idle,
        watts: watts,
        active: active,
        reverse: reverse,
        bucket: bucket,
        seconds: SPEED_SECONDS[bucket],
        alpha: SPEED_ALPHA[bucket],
        width: ribbonWidth(watts, reference, active),
        share: active ? Math.min(1, Math.abs(watts) / (reference || 1000)) : 0,
        segments: segments,
        total: cumulative,
        d: "M" + segments[0].x1 + " " + segments[0].y1 +
           " H" + segments[0].x2 +
           " V" + segments[1].y2 +
           " H" + segments[2].x2,
      });
    }
    return { layout: layout, pipes: pipes, reference: reference, maxWatts: maxWatts };
  }

  var AXIS = {
    right: [1, 0], left: [-1, 0], down: [0, 1], up: [0, -1],
  };

  function translate(direction, distance) {
    var axis = AXIS[direction] || AXIS.right;
    return "translate3d(" + (axis[0] * distance).toFixed(2) + "px, " +
      (axis[1] * distance).toFixed(2) + "px, 0)";
  }

  // The same motion as the stylesheet's keyframes, expressed with literal
  // values. A keyframe that reads a custom property cannot be handed to the
  // compositor in Chromium, and the whole point of this axis is to find out
  // what that costs.
  function driveWaapi(el, spec) {
    if (typeof el.animate !== "function") return null;
    var frames = spec.travel
      ? [
          { transform: translate(spec.direction, -spec.tokenLength) },
          { transform: translate(spec.direction, spec.span) },
        ]
      : [
          { transform: "translate3d(0px, 0px, 0)" },
          { transform: translate(spec.direction, spec.step) },
        ];
    var animation = el.animate(frames, {
      duration: spec.seconds * 1000,
      iterations: Infinity,
      easing: "linear",
      direction: spec.reverse ? "reverse" : "normal",
      delay: spec.delay ? spec.delay * 1000 : 0,
    });
    if (spec.paused) animation.pause();
    return animation;
  }

  function div(className, parent) {
    var node = document.createElement("div");
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  function boxFor(segment, widthPx, scale, parent, className) {
    var horizontal = segment.horizontal;
    var box = div(className, parent);
    box.style.left = (Math.min(segment.x1, segment.x2) * scale - (horizontal ? 0 : widthPx / 2)) + "px";
    box.style.top = (Math.min(segment.y1, segment.y2) * scale - (horizontal ? widthPx / 2 : 0)) + "px";
    box.style.width = (horizontal ? segment.length * scale : widthPx) + "px";
    box.style.height = (horizontal ? widthPx : segment.length * scale) + "px";
    return box;
  }

  // A halo drawn as nested capsules with a falling alpha rather than a blur.
  // Four fills approximate a gaussian closely enough at these sizes and cost
  // nothing to decode, which a filter region does.
  function haloBody(rgb, x, y, on, thick, halo) {
    var body = "";
    var steps = 7;
    for (var i = steps; i >= 1; i -= 1) {
      var grow = (halo * i) / steps;
      var alpha = 0.22 * Math.pow(1 - (i - 1) / steps, 1.9);
      var h = thick + 2 * grow;
      body += '<rect x="' + (x - grow).toFixed(2) + '" y="' + (y - grow).toFixed(2) +
        '" width="' + (on + 2 * grow).toFixed(2) + '" height="' + h.toFixed(2) +
        '" rx="' + (h / 2).toFixed(2) + '" ry="' + (h / 2).toFixed(2) +
        '" fill="' + rgba(rgb, Number(alpha.toFixed(3))) + '"/>';
    }
    return body;
  }

  // Deterministic jitter: the particle lanes must be stable across renders, or
  // the same scene looks different every time it is drawn and no screenshot
  // comparison means anything.
  function jitter(seed) {
    var value = Math.sin(seed * 12.9898) * 43758.5453;
    return value - Math.floor(value);
  }

  function tokenBody(design, opts) {
    var color = opts.color;
    var rgb = opts.rgb;
    var x = opts.x;
    var y = opts.y;
    var on = opts.on;
    var thick = opts.thick;
    var r = Math.min(thick / 2, on / 2);
    var body = "";

    if (design === "arrow") {
      var nose = Math.min(thick * 0.62, on * 0.42);
      var tail = x + r;
      return '<path d="M' + tail.toFixed(2) + " " + y.toFixed(2) +
        " H" + (x + on - nose).toFixed(2) +
        " L" + (x + on).toFixed(2) + " " + (y + thick / 2).toFixed(2) +
        " L" + (x + on - nose).toFixed(2) + " " + (y + thick).toFixed(2) +
        " H" + tail.toFixed(2) +
        " A" + r.toFixed(2) + " " + r.toFixed(2) + " 0 0 0 " +
        tail.toFixed(2) + " " + y.toFixed(2) + ' Z" fill="' + color + '"/>';
    }

    if (design === "comet") {
      var head = x + on - r;
      var yc = y + thick / 2;
      var id = (opts.idPrefix || "c") + opts.index + "x" +
        (opts.suffix === undefined ? 0 : opts.suffix + 4);
      body += '<linearGradient id="' + id + '" x1="' + x.toFixed(2) + '" y1="0" x2="' +
        (x + on).toFixed(2) + '" y2="0" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0" stop-color="' + color + '" stop-opacity="0"/>' +
        '<stop offset="0.55" stop-color="' + color + '" stop-opacity="0.55"/>' +
        '<stop offset="1" stop-color="' + color + '" stop-opacity="1"/></linearGradient>';
      body += '<path d="M' + x.toFixed(2) + " " + yc.toFixed(2) +
        " L" + head.toFixed(2) + " " + y.toFixed(2) +
        " A" + r.toFixed(2) + " " + r.toFixed(2) + " 0 0 1 " +
        head.toFixed(2) + " " + (y + thick).toFixed(2) +
        ' Z" fill="url(#' + id + ')"/>';
      return body;
    }

    if (design === "particles") {
      var count = opts.density;
      var span = opts.span || on;
      for (var i = 0; i < count; i += 1) {
        var jx = jitter(opts.index * 7.1 + i * 3.3);
        var jy = jitter(opts.index * 3.7 + i * 9.1);
        var js = jitter(opts.index * 5.3 + i * 1.7);
        var radius = thick * (0.16 + 0.20 * js);
        var cx = x + (i + 0.15 + 0.7 * jx) * (span / count);
        var cy = y + radius + (thick - 2 * radius) * jy;
        var alpha = 0.55 + 0.45 * js;
        body += '<circle cx="' + cx.toFixed(2) + '" cy="' + cy.toFixed(2) +
          '" r="' + radius.toFixed(2) + '" fill="' + rgba(rgb, Number(alpha.toFixed(2))) + '"/>';
      }
      return body;
    }

    if (design === "plasma") {
      var core = thick * 0.42;
      var cy2 = y + thick / 2;
      body += '<rect x="' + x.toFixed(2) + '" y="' + (cy2 - thick / 2).toFixed(2) +
        '" width="' + on.toFixed(2) + '" height="' + thick.toFixed(2) +
        '" rx="' + (thick / 2).toFixed(2) + '" fill="' + rgba(rgb, 0.34) + '"/>';
      body += '<rect x="' + (x + on * 0.06).toFixed(2) + '" y="' + (cy2 - core / 2).toFixed(2) +
        '" width="' + (on * 0.88).toFixed(2) + '" height="' + core.toFixed(2) +
        '" rx="' + (core / 2).toFixed(2) + '" fill="' + color + '"/>';
      body += '<rect x="' + (x + on * 0.16).toFixed(2) + '" y="' + (cy2 - core * 0.22).toFixed(2) +
        '" width="' + (on * 0.5).toFixed(2) + '" height="' + (core * 0.44).toFixed(2) +
        '" rx="' + (core * 0.22).toFixed(2) + '" fill="#ffffff" opacity="0.45"/>';
      return body;
    }

    return '<rect x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '" width="' + on.toFixed(2) +
      '" height="' + thick.toFixed(2) + '" rx="' + r.toFixed(2) +
      '" ry="' + r.toFixed(2) + '" fill="' + color + '"/>';
  }

  function tileSvg(design, color, on, period, thick, repeats, rich, halo, glowMode, mirror, density) {
    var rgb = hexRgb(color);
    var pad = halo || 0;
    var height = thick + 2 * pad;
    var r = Math.min(thick / 2, on / 2);
    var body = "";
    var blurred = "";
    var first = pad > 0 ? -1 : 0;
    var last = pad > 0 ? repeats + 1 : repeats;
    for (var k = first; k < last; k += 1) {
      var x = k * period;
      var seed = ((k % repeats) + repeats) % repeats;
      if (pad > 0 && glowMode === "texture") {
        body += haloBody(rgb, x, pad, on, thick, pad);
      }
      if (pad > 0 && glowMode === "blur") {
        blurred += tokenBody(design, {
          color: color, rgb: rgb, x: x, y: pad, on: on, thick: thick,
          index: seed, span: period, density: density || 4, idPrefix: "b",
        });
      }
      body += tokenBody(design, {
        color: color, rgb: rgb, x: x, y: pad, on: on, thick: thick,
        index: seed, span: period, density: density || 4,
        idPrefix: "t", suffix: k,
      });
      if (k < 0 || k >= repeats) continue;
      if (rich && design === "capsule") {
        body += '<rect x="' + (x + r).toFixed(2) + '" y="' + (pad + thick * 0.18).toFixed(2) +
          '" width="' + Math.max(1, on - 2 * r).toFixed(2) + '" height="' +
          (thick * 0.26).toFixed(2) + '" rx="' + (thick * 0.13).toFixed(2) +
          '" fill="#ffffff" opacity="0.35"/>';
      }
    }
    var w = period * repeats;
    var defs = "";
    if (blurred) {
      // The filter region has to be opened up: the default -10%/+120% box clips
      // the blur and leaves a visible rectangular edge on the halo.
      defs = '<defs><filter id="g" x="-60%" y="-60%" width="220%" height="220%">' +
        '<feGaussianBlur stdDeviation="' + (pad * 0.42).toFixed(2) + '"/></filter></defs>' +
        '<g filter="url(#g)" opacity="0.85">' + blurred + "</g>";
    }
    if (mirror) {
      body = '<g transform="translate(' + w.toFixed(2) + ',0) scale(-1,1)">' + body + "</g>";
    }
    return '<svg xmlns="http://www.w3.org/2000/svg" width="' + w.toFixed(2) +
      '" height="' + height.toFixed(2) + '" viewBox="0 0 ' + w.toFixed(2) + " " +
      height.toFixed(2) + '">' + defs + body + "</svg>";
  }

  // Returns { image, size, repeat } for a tiled background along the direction
  // of travel. Vertical segments reuse the horizontal artwork by swapping the
  // viewBox axes rather than stretching a square tile, which would turn a
  // capsule into a lens.
  function tiledBackground(candidate, pipe, segment, geom) {
    var horizontal = segment.horizontal;
    var rgb = hexRgb(pipe.color);
    var solid = rgba(rgb, 1);
    var clear = rgba(rgb, 0);
    var period = geom.period;
    var on = geom.on;
    var thick = geom.thick;
    var axis = horizontal ? "to right" : "to bottom";
    var halo = geom.halo || 0;
    var band = thick + 2 * halo;
    var size = horizontal
      ? (period * geom.tile).toFixed(2) + "px " + band.toFixed(2) + "px"
      : band.toFixed(2) + "px " + (period * geom.tile).toFixed(2) + "px";
    var repeat = horizontal ? "repeat-x" : "repeat-y";

    if (candidate === "rect") {
      return {
        image: "linear-gradient(" + axis + ", " + solid + " 0, " + solid + " " +
          on.toFixed(2) + "px, " + clear + " " + on.toFixed(2) + "px, " + clear + " " +
          period.toFixed(2) + "px)",
        size: horizontal
          ? period.toFixed(2) + "px " + thick.toFixed(2) + "px"
          : thick.toFixed(2) + "px " + period.toFixed(2) + "px",
        repeat: repeat,
        cross: halo,
      };
    }

    if (candidate === "wave") {
      var stops = [];
      var steps = 16;
      for (var w = 0; w <= steps; w += 1) {
        var phase = (w / steps) * Math.PI * 2;
        var level = Math.pow((1 + Math.sin(phase - Math.PI / 2)) / 2, 1.8);
        stops.push(rgba(rgb, Number((0.08 + 0.92 * level).toFixed(3))) + " " +
          ((w / steps) * period).toFixed(2) + "px");
      }
      return {
        image: "linear-gradient(" + axis + ", " + stops.join(", ") + ")",
        size: horizontal
          ? period.toFixed(2) + "px " + thick.toFixed(2) + "px"
          : thick.toFixed(2) + "px " + period.toFixed(2) + "px",
        repeat: repeat,
        cross: halo,
      };
    }

    if (candidate === "repeating") {
      return {
        image: "repeating-linear-gradient(" + axis + ", " + solid + " 0, " + solid + " " +
          on.toFixed(2) + "px, " + clear + " " + on.toFixed(2) + "px, " + clear + " " +
          period.toFixed(2) + "px)",
        size: horizontal
          ? (period * 4).toFixed(2) + "px " + thick.toFixed(2) + "px"
          : thick.toFixed(2) + "px " + (period * 4).toFixed(2) + "px",
        repeat: repeat,
        cross: halo,
      };
    }

    if (candidate === "gradient-capsule") {
      var r = Math.min(thick / 2, on / 2);
      var cap = function (centre) {
        return horizontal
          ? "radial-gradient(circle " + r.toFixed(2) + "px at " + centre.toFixed(2) +
            "px 50%, " + solid + " 0, " + solid + " " + r.toFixed(2) + "px, " + clear + " " +
            r.toFixed(2) + "px)"
          : "radial-gradient(circle " + r.toFixed(2) + "px at 50% " + centre.toFixed(2) +
            "px, " + solid + " 0, " + solid + " " + r.toFixed(2) + "px, " + clear + " " +
            r.toFixed(2) + "px)";
      };
      var body = "linear-gradient(" + axis + ", " + clear + " 0, " + clear + " " +
        r.toFixed(2) + "px, " + solid + " " + r.toFixed(2) + "px, " + solid + " " +
        (on - r).toFixed(2) + "px, " + clear + " " + (on - r).toFixed(2) + "px, " +
        clear + " " + period.toFixed(2) + "px)";
      var flat = horizontal
        ? (period * geom.tile).toFixed(2) + "px " + thick.toFixed(2) + "px"
        : thick.toFixed(2) + "px " + (period * geom.tile).toFixed(2) + "px";
      return {
        image: [cap(r), cap(on - r), body].join(", "),
        size: [flat, flat, flat].join(", "),
        repeat: [repeat, repeat, repeat].join(", "),
        cross: halo,
      };
    }

    var design = geom.design || candidate;
    // The tile carries the direction for the asymmetric designs, and
    // animation-direction cannot flip a picture: a pipe that runs the other way
    // needs the artwork mirrored, or every reversed flow points backwards.
    var asymmetric = design === "arrow" || design === "comet";
    var forward = segment.direction === "right" || segment.direction === "down";
    var mirror = asymmetric && (forward === !!pipe.reverse);
    var tileOf = function () {
      return tileSvg(design, pipe.color, on, period, thick, geom.tile, geom.rich,
                     halo, geom.glow, mirror, geom.density);
    };
    var markup = tileOf();
    if (!horizontal) {
      markup = '<svg xmlns="http://www.w3.org/2000/svg" width="' + band.toFixed(2) +
        '" height="' + (period * geom.tile).toFixed(2) + '" viewBox="0 0 ' +
        band.toFixed(2) + " " + (period * geom.tile).toFixed(2) + '">' +
        '<g transform="rotate(90) translate(0,-' + band.toFixed(2) + ')">' +
        tileOf().replace(/^<svg[^>]*>/, "").replace(/<\/svg>$/, "") +
        "</g></svg>";
    }
    return { image: svgUrl(markup), size: size, repeat: repeat };
  }

  function paint(scene, ctx) {
    var scale = scene.layout.scale;
    var overlay = ctx.overlay;
    var candidate = ctx.candidate;
    var counts = { animated: 0, painted: 0, tokens: 0, staticBars: 0 };
    var paused = ctx.motion === "off";

    var glow = GLOWS.indexOf(ctx.glow) >= 0 ? ctx.glow : "none";
    var waapi = ctx.anim === "waapi";
    counts.glow = glow;
    counts.anim = waapi ? "waapi" : "var";

    scene.pipes.forEach(function (pipe) {
      var widthPx = pipe.width * scale;
      var haloPx = glow === "none" ? 0 : widthPx * GLOW_SPREAD;
      var period = DASH_PERIOD * scale;
      var on = DASH_ON * scale;
      var speedPxPerSec = period / pipe.seconds;

      pipe.segments.forEach(function (segment) {
        var horizontal = segment.horizontal;
        var lengthPx = segment.length * scale;
        var phase = -((segment.before % DASH_PERIOD) * scale);

        var baseAlpha = 0.22;
        if (candidate === "core") baseAlpha = 0.30;
        if (candidate === "pulse") baseAlpha = 0.32;
        if (candidate === "minimal") baseAlpha = 0.26;
        var barW = candidate === "minimal" ? Math.max(2, widthPx * 0.4) : widthPx;
        var bar = boxFor(segment, barW, scale, overlay, "ps-seg ps-static");
        bar.style.background = pipe.color;
        bar.style.opacity = String(pipe.alpha * baseAlpha);
        bar.style.borderRadius = (barW / 2).toFixed(2) + "px";
        if (glow === "static") {
          bar.style.boxShadow = "0 0 " + (haloPx * 0.9).toFixed(1) + "px " + pipe.color;
        }
        counts.painted += 1;
        counts.staticBars += 1;

        if (TILED.indexOf(candidate) >= 0) {
          var haloed = glow === "texture" || glow === "blur" || glow === "blend";
          // Whenever anything is drawn outside the pipe's own thickness -- a
          // halo, or plasma's sheath -- the box, the tile and the background
          // size have to grow by the same amount, or the token stops being
          // centred in its own band.
          var sheath = candidate === "plasma" ? Math.max(haloPx, widthPx * 0.5) : haloPx;
          var banded = haloed || glow === "layered" || candidate === "plasma";
          var bandPx = banded ? widthPx + 2 * sheath : widthPx;
          var geom = {
            period: period, on: on, thick: widthPx,
            tile: ctx.tile || 1, rich: ctx.texture === "rich",
            halo: banded ? sheath : 0,
            glow: glow === "blend" ? "texture" : glow,
            density: Math.max(3, Math.round(3 + 7 * (pipe.share || 0))),
          };
          var box = boxFor(segment, bandPx, scale, overlay, "ps-seg");
          box.style.color = pipe.color;
          box.style.opacity = String(pipe.alpha);
          var mover = div("ps-move dir-" + segment.direction +
            (pipe.reverse ? " reverse" : "") + (paused || !pipe.active ? " paused" : "") +
            (glow === "blend" ? " ps-add" : ""), box);
          if (glow === "filter") {
            mover.style.filter = "drop-shadow(0 0 " + (haloPx * 0.45).toFixed(1) + "px " +
              pipe.color + ")";
          }
          mover.style.setProperty("--step", period.toFixed(2) + "px");
          mover.style.setProperty("--secs", pipe.seconds + "s");
          if (waapi) {
            mover.classList.add("ps-js");
            driveWaapi(mover, {
              direction: segment.direction, step: period, seconds: pipe.seconds,
              reverse: pipe.reverse, paused: paused || !pipe.active,
            });
          }
          var pad = ctx.pad || 0;
          var overhang = period + pad;
          if (horizontal) {
            mover.style.left = -overhang + "px";
            mover.style.right = -overhang + "px";
            mover.style.top = -pad + "px";
            mover.style.bottom = -pad + "px";
          } else {
            mover.style.top = -overhang + "px";
            mover.style.bottom = -overhang + "px";
            mover.style.left = -pad + "px";
            mover.style.right = -pad + "px";
          }
          var bg = tiledBackground(candidate, pipe, segment, geom);
          mover.style.backgroundImage = bg.image;
          if (bg.size !== "auto") mover.style.backgroundSize = bg.size;
          mover.style.backgroundRepeat = bg.repeat;
          // The layer's leading edge moves out by `pad`, so the tile origin has to
          // come back by the same amount or the padded probe is not appearance-
          // neutral and measures a different picture, not a bigger layer.
          var originShift = phase + pad;
          // An SVG tile bakes its halo into the artwork and is already centred in
          // the band. A CSS gradient has no such padding, so it has to be pushed
          // down by the halo or it paints alongside the pipe instead of on it.
          var crossShift = pad + (bg.cross || 0);
          mover.style.backgroundPosition = horizontal
            ? originShift + "px " + crossShift + "px"
            : crossShift + "px " + originShift + "px";
          counts.animated += 1;
          counts.painted += 1;

          // A second, slower, fainter layer. `plasma` uses it as a sheath the
          // core slides inside; `layered` uses it as a blurred halo. Both cost
          // one more animated layer per segment, which is the point of measuring
          // them beside the one-layer designs.
          var second = null;
          if (candidate === "plasma") {
            second = { design: "capsule", halo: sheath, glow: "texture",
                       secs: pipe.seconds * 1.55, cls: " ps-sheath" };
          } else if (glow === "layered") {
            second = { design: candidate, halo: sheath, glow: "blur",
                       secs: pipe.seconds, cls: " ps-halo" };
          }
          if (second) {
            var secondGeom = {
              period: period, on: on, thick: widthPx, tile: geom.tile, rich: false,
              halo: second.halo, glow: second.glow, design: second.design,
              density: geom.density,
            };
            var behind = div("ps-move" + second.cls + " dir-" + segment.direction +
              (pipe.reverse ? " reverse" : "") +
              (paused || !pipe.active ? " paused" : ""), box);
            behind.style.setProperty("--step", period.toFixed(2) + "px");
            behind.style.setProperty("--secs", second.secs + "s");
            if (waapi) {
              behind.classList.add("ps-js");
              driveWaapi(behind, {
                direction: segment.direction, step: period, seconds: second.secs,
                reverse: pipe.reverse, paused: paused || !pipe.active,
              });
            }
            if (horizontal) {
              behind.style.left = -overhang + "px";
              behind.style.right = -overhang + "px";
              behind.style.top = "0";
              behind.style.bottom = "0";
            } else {
              behind.style.top = -overhang + "px";
              behind.style.bottom = -overhang + "px";
              behind.style.left = "0";
              behind.style.right = "0";
            }
            var behindPaint = tiledBackground(candidate, pipe, segment, secondGeom);
            behind.style.backgroundImage = behindPaint.image;
            behind.style.backgroundSize = behindPaint.size;
            behind.style.backgroundRepeat = behindPaint.repeat;
            behind.style.backgroundPosition = horizontal
              ? originShift + "px " + (behindPaint.cross || 0) + "px"
              : (behindPaint.cross || 0) + "px " + originShift + "px";
            box.insertBefore(behind, mover);
            counts.animated += 1;
            counts.painted += 1;
          }
          return;
        }

        if (candidate === "radius-el") {
          var boxC = boxFor(segment, widthPx, scale, overlay, "ps-seg");
          boxC.style.color = pipe.color;
          boxC.style.opacity = String(pipe.alpha);
          var moverC = div("ps-move dir-" + segment.direction +
            (pipe.reverse ? " reverse" : "") + (paused || !pipe.active ? " paused" : ""), boxC);
          moverC.style.setProperty("--step", period.toFixed(2) + "px");
          moverC.style.setProperty("--secs", pipe.seconds + "s");
          if (waapi) {
            moverC.classList.add("ps-js");
            driveWaapi(moverC, {
              direction: segment.direction, step: period, seconds: pipe.seconds,
              reverse: pipe.reverse, paused: paused || !pipe.active,
            });
          }
          if (horizontal) {
            moverC.style.left = -period + "px";
            moverC.style.right = -period + "px";
            moverC.style.top = "0";
            moverC.style.bottom = "0";
          } else {
            moverC.style.top = -period + "px";
            moverC.style.bottom = -period + "px";
            moverC.style.left = "0";
            moverC.style.right = "0";
          }
          var span = lengthPx + 2 * period;
          var count = Math.ceil(span / period) + 1;
          for (var k = 0; k < count; k += 1) {
            var tok = div("ps-chip", moverC);
            var offset = k * period + phase;
            tok.style.background = pipe.color;
            if (horizontal) {
              tok.style.left = offset + "px";
              tok.style.top = "0";
              tok.style.width = on + "px";
              tok.style.height = "100%";
            } else {
              tok.style.top = offset + "px";
              tok.style.left = "0";
              tok.style.height = on + "px";
              tok.style.width = "100%";
            }
            tok.style.borderRadius = (widthPx / 2).toFixed(2) + "px";
            if (glow !== "none" && glow !== "static") {
              tok.style.boxShadow = "0 0 " + (haloPx * 0.8).toFixed(1) + "px " + pipe.color;
            }
            counts.painted += 1;
          }
          counts.animated += 1;
          return;
        }

        var tokenCount = candidate === "tokens" ? (ctx.tokens || 2) : 1;
        var tokenLen = on;
        if (candidate === "tokens") {
          tokenLen = Math.min(on, ((lengthPx + on) / tokenCount) * 0.65);
        }
        if (candidate === "core") tokenLen = on * 0.55;
        if (candidate === "pulse") tokenLen = period * 2.2;
        if (candidate === "minimal") tokenLen = Math.max(3, widthPx);

        var host = boxFor(segment, widthPx, scale, overlay, "ps-seg");
        host.style.color = pipe.color;
        host.style.opacity = String(pipe.alpha);
        host.style.setProperty("--span", lengthPx.toFixed(2) + "px");
        var travel = lengthPx + tokenLen;
        var duration = travel / speedPxPerSec;
        for (var t = 0; t < tokenCount; t += 1) {
          var token = div("ps-token dir-" + segment.direction +
            (pipe.reverse ? " reverse" : "") + (paused || !pipe.active ? " paused" : ""), host);
          token.style.setProperty("--tw", tokenLen.toFixed(2) + "px");
          token.style.animationDuration = duration.toFixed(3) + "s";
          token.style.animationDelay = (-(t / tokenCount) * duration).toFixed(3) + "s";
          if (waapi) {
            token.classList.add("ps-js");
            driveWaapi(token, {
              travel: true, direction: segment.direction, span: lengthPx,
              tokenLength: tokenLen, seconds: duration, reverse: pipe.reverse,
              delay: -(t / tokenCount) * duration, paused: paused || !pipe.active,
            });
          }
          if (horizontal) {
            token.style.width = tokenLen + "px";
            token.style.height = "100%";
          } else {
            token.style.height = tokenLen + "px";
            token.style.width = "100%";
          }
          if (candidate === "pulse") {
            var rgbP = hexRgb(pipe.color);
            token.style.background = "radial-gradient(closest-side, " +
              rgba(rgbP, 0.95) + " 0, " + rgba(rgbP, 0.45) + " 45%, " + rgba(rgbP, 0) + " 100%)";
          } else {
            token.style.background = pipe.color;
            token.style.borderRadius = (Math.min(widthPx, tokenLen) / 2).toFixed(2) + "px";
          }
          if (glow === "texture" || glow === "blur" || glow === "blend" || glow === "layered") {
            token.style.boxShadow = "0 0 " + (haloPx * 0.8).toFixed(1) + "px " + pipe.color;
          }
          if (glow === "filter") {
            token.style.filter = "drop-shadow(0 0 " + (haloPx * 0.45).toFixed(1) + "px " +
              pipe.color + ")";
          }
          if (glow === "blend") token.classList.add("ps-add");
          counts.animated += 1;
          counts.painted += 1;
          counts.tokens += 1;
        }
      });
    });
    return counts;
  }

  global.PipeStudy = {
    CANDIDATES: CANDIDATES,
    SHAPES: Object.keys(SHAPES),
    GLOWS: GLOWS,
    ANIMS: ANIMS,
    TILED: TILED,
    WATT_SAMPLES: WATT_SAMPLES,
    SCALE_LADDER: SCALE_LADDER,
    RIBBON: {
      idle: RIBBON_IDLE_W, min: RIBBON_MIN_W, max: RIBBON_MAX_W, ceiling: RIBBON_CEILING_W,
    },
    STAGE_W: STAGE_W,
    STAGE_H: STAGE_H,
    DASH_PERIOD: DASH_PERIOD,
    DASH_ON: DASH_ON,
    buildScene: buildScene,
    paint: paint,
    ribbonWidth: ribbonWidth,
    scaleReference: scaleReference,
    speedBucket: speedBucket,
    colors: COLORS,
  };
})(window);
