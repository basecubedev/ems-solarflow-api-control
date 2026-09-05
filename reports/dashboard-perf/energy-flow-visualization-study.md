# What is the best way to draw energy moving through this system?

**Measured 2026-09-04 on Linux with an NVIDIA GTX 1660 Ti.** Every benchmark
here records the rasterisation path it ran on and the machine's load at the
time; read section 2.1 before comparing any number with an older report. No
macOS host exists for this project, and macOS is where the original symptom was
reported.

This is a technology study, not a defence of the current implementation. The
previous investigation
([flow-rendering-investigation.md](flow-rendering-investigation.md)) asked how
to stop paying for the animation and answered it. This one asks a different
question -- what the visualisation should *be* -- and is allowed to conclude
that the technology, the metaphor or the frontend architecture should change.

---

## 1. Executive summary

**The flow visualisation did not have a rendering problem. It had an encoding
problem, and two lines of CSS.**

On a real GPU, every rendering technique tested reaches the display's refresh
ceiling on the real dashboard -- the HTML tile layer that ships, a 2D canvas,
an OffscreenCanvas worker, a hand-written WebGL renderer, and the original
`stroke-dashoffset` SVG technique this project spent two investigations
escaping, which measures **132 fps**. There is nothing left to win by changing
technology.

The two problems this study inherited were **software-rasterisation artifacts**.
Firefox's devices-view collapse: 11.2 fps headless, **134.6 fps headed** at eight
devices. Chromium's `backdrop-filter` ceiling: 9.6 fps on SwiftShader, **115.6
headed**. Both had been recorded as properties of the engines.

What is genuinely wrong is what the picture says. Magnitude was encoded in
**three steps across a 75x power range**, with the within-step difference
carried by a speed channel spanning **1.55x**. A 700 W flow and a 3000 W flow
were pixel-identical, and so were the 1.20 kW and 690 W feeders sitting one
above the other in the devices view. Four independent visual evaluations reached
that separately, and none of the seven metaphors tested could fix it, because
all seven sat on the same three-step rules.

The fix is free. The renderer moves one transform-animated layer per segment and
paints it with a background image, so **appearance is independent of cost**:
seven visually different metaphors measure 143.5-143.8 fps in Firefox and
57.1-58.8 in Chromium, with identical element and animation counts. The design
question and the performance question are separable.

**Four changes shipped** -- 154 added and 25 removed lines across two files,
a good half of it comment -- guarded by 24
contract tests that need no browser:

1. **Magnitude is continuous** — thickness proportional to power on a scale that
   follows the installation. 700 W and 3000 W now draw at 6.5 px and 15 px.
   Nothing draws thinner than the old minimum.
2. **Tokens have round ends again**, restoring the `stroke-linecap: round` the
   tile renderer lost. The only metaphor all four evaluations rank at or above
   what ships.
3. **`backdrop-filter` deleted from the panels** — measured invisible (mean
   channel delta 0.008/255 in Firefox), and costing ~19% in the control view.
4. **A forced synchronous layout removed** from the tile rebuild: 166.4 ms of a
   167.4 ms rebuild, caused by reads interleaved with writes.

**Nothing else is recommended.** No library (every JS animation library reverts
the shipped fix; the CSP blocks WebAssembly and PixiJS outright; GSAP's licence
is a poor fit for AGPL). No framework (the render path costs 0.04-0.51% of wall
time, and the main thread is idle exactly where the page is slow). No canvas and
no WebGL — and not because they fail. **Both objections that had removed them
turned out to be measurement artifacts**: the "dimming" was a bare `canvas {}`
selector in this project's own stylesheet, and the "Firefox collapses when a
canvas presents per frame" was headless Firefox compositing on the CPU. On a
real display a WebGL canvas presenting every frame costs Firefox 7%. They are
declined because they reach a ceiling the current renderer already reaches, for
considerably more machinery.

Three conclusions in this document were written and then overturned by better
measurement; section 19 keeps them visible, because the way they were wrong is
the most transferable thing here.

---

## 2. What was already established, and what was wrong with it

The previous investigation's central mechanism holds and is not re-litigated
here:

> An SVG element cannot be composited on its own. Any CSS animation inside an
> SVG subtree re-rasterises that whole subtree every frame, and this subtree is
> full of `filter: drop-shadow()`. The animated property is nearly irrelevant.

Its fix -- moving the dashes to an HTML layer the compositor can translate --
took Firefox's aggregated view from 5.5 to about 57 fps and is what ships
today. That is the baseline this study measures against.

Three things it left open, which are this study's starting points:

1. **Firefox's devices view still collapses past two devices** (57.6 fps at
   two, 9.2 at four, 12.4 at eight). The trigger was isolated to the tile layer
   growing taller than the viewport rather than to the number of tiles.
2. **Chromium was capped near 17 fps whenever `backdrop-filter` was active.**
3. **Canvas won the isolated lab and failed in the real dashboard**, for two
   measured reasons: a full-size canvas over the flow SVG made everything under
   it render darker, and its per-frame repaint cost Firefox everything.

### 2.1 A correction to the previous study's environment claims

The previous report states that everything it measured was software-rendered on
Linux. That is **true for headless Chromium and false for Firefox**, and the
distinction was never recorded per run. Measured on this host today:

| Configuration | What actually rasterises |
|---|---|
| Chromium headless, default | `ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)` -- **software** |
| Chromium headless + `--use-gl=angle --use-angle=gl --enable-gpu --ignore-gpu-blocklist` | `ANGLE (NVIDIA Corporation, NVIDIA GeForce GTX 1660 Ti/PCIe/SSE2, OpenGL 4.5.0)` -- **real GPU** |
| Chromium headed on `:0` | real GPU |
| Firefox headless | reports `NVIDIA GeForce GTX 980, or similar` for **WebGL**, but does **not** GPU-composite the page |
| Firefox headed on `:0` | real GPU, and it composites the page |

The host has an NVIDIA GeForce GTX 1660 Ti Mobile with 6 GB and direct
rendering, and a live X display.

**A trap inside the trap.** The renderer string comes from
`WEBGL_debug_renderer_info`, which names the device *WebGL* got -- not the one
the page compositor is using. Firefox headless reports NVIDIA and still
composites the page on the CPU. So the probe this study added is necessary but
not sufficient: it can prove a run was software, and it cannot prove a run was
hardware. Only headed-on-`:0` is certain, and that is where the decisive
numbers in this study are taken.

This matters most for the `backdrop-filter` ceiling. A full-viewport blur is
exactly the kind of work whose cost collapses on a GPU, so a Chromium ceiling
measured on SwiftShader is not evidence about Chromium on a GPU. Every matrix
in this study therefore carries a `--gpu` axis, and **every run records the
renderer string it actually observed** (`rasterisation.renderer`) so that a
software number can never again be filed as if it were a hardware one.

### 2.2 What this host still cannot show

- **macOS**, which is where the symptom was reported. There is no macOS host.
- **WebGPU.** `navigator.gpu` is absent in both engines here, so WebGPU is
  assessed for feasibility only and is never benchmarked.
- **A quiet machine, for free.** This host is a live desktop and is CPU-limited
  when several things run at once. Both harnesses now take `--max-load`
  (default 2.0) and wait for the 1-minute load average to fall below it before
  each case, and every case records `load_average` and `quiet_gate`.

---

## 3. What the picture has to say

The visualisation must communicate where energy comes from, where it goes, its
direction, whether a path is active, relative magnitude, relative intensity,
and the same story at device level as at system level.

Constraints that are not negotiable: the animation stays (removing it is not a
solution); direction, active/inactive, magnitude, speed and pipe association
must survive; and nothing in EMS control, the backend, auth, CSRF, runtime
writes or hardware gates may change. Production stays Python-served with no
build step unless a build step is affirmatively justified.

Pixel-identity with today's design is **not** a requirement. A candidate is
allowed to look better.

---

## 4. Candidates

Grouped by the question each one answers.

| Family | Candidate | Question it answers |
|---|---|---|
| control | `dashoffset` | what the original SVG technique costs |
| shipped | `dom-tiles` | what ships today |
| SVG | `svg-transform`, `svg-pattern`, `svg-mask` | can SVG be made cheap without leaving SVG |
| Canvas | `canvas`, `canvas-bloom` | the technique that won the lab and lost the dashboard |
| Canvas | `canvas-worker` (new) | does OffscreenCanvas on a worker survive the objection that killed canvas |
| GPU | `webgl` (new) | can the GPU make the glow -- the thing that made everything expensive -- nearly free |
| metaphor | `dash`, `capsule`, `particles`, `comet`, `chevron`, `pulse`, `sweep` (new) | what should it look like, given the mechanism is fixed |

### 4.1 The metaphor axis is free, and that is the point

`dom-tiles` moves **one div per segment** and paints its pattern with a
background image. The compositor translates that layer whatever is drawn on it,
so *what the pattern looks like is independent of what it costs*. Metaphor and
mechanism are separable.

That turns "should it look different?" from a performance question into a pure
design question, and it is why this study can afford to explore visual
alternatives at all. Section 9 tests the claim rather than assuming it.

---

## 5. Prototype architecture

All candidates render the same scene through the same geometry, in
`scripts/flow_lab/`, selected by `?renderer=` and `?metaphor=`. Nothing about
the pipes, colours, widths, dash geometry or speed buckets differs between
them, so a difference in the result is a difference in the technique.

Two renderers were built for this study:

- **`webgl`** draws every segment in the scene as **one instanced draw call**.
  A unit quad is expanded along each segment in the vertex shader with padding
  for the glow; the fragment shader evaluates a capsule signed-distance field
  for the dash and takes the glow as an **exponential falloff on that same
  distance** -- so the glow needs no blur pass, no second render target and no
  `shadowBlur`. That is the property that makes GPU rendering interesting here,
  because the glow is what made every other technique expensive. Twelve flows is
  216 instances and one float uniform per frame. It falls back to `dom-tiles`
  when the context is missing or the shader fails to compile, which is exercised:
  the first version used `active` as a variable name, which is reserved in
  GLSL ES 3.00, and the fallback is what showed up on screen.
- **`canvas-worker`** transfers an `OffscreenCanvas` to a worker so the raster
  never touches the main thread. Workers have no `requestAnimationFrame` in
  either engine, so the loop self-schedules and takes its phase from
  `performance.now()`, which makes jittered ticks change smoothness but never
  drift.

Guards, because the previous investigation once reported a 13x win from an
experiment that never executed:

- `scripts/flow_lab_verify.mjs` proves every candidate **builds without a page
  error, actually moves with motion on, and is actually still with motion off**
  before any number taken from it is believed. All 17 renderer/metaphor
  combinations pass in both engines.
- `scripts/flow_lab_fidelity.mjs` freezes every candidate at phase 0 and
  measures its pixel distance from the control.
- `scripts/flow_lab_gallery.mjs` captures each candidate moving, across six
  scenarios and three instants, because a frozen frame cannot show whether
  motion reads as flow or as strobing.

---

## 6. Method

Two harnesses. `scripts/flow_lab_bench.py` compares rendering techniques
against each other in an isolated scene; `scripts/dashboard_bench.py` measures
the real dashboard. **The second is the one that decides**, for a reason the
previous study paid for: canvas won the lab by a wide margin and then failed on
the real page, because the lab page is otherwise empty and the dashboard is
not.

Primary metric is event-loop lag (`lagP95Ms`) -- the thing the reported symptom
is actually made of, and the only metric both engines report identically.
Frame rate is used for comparison between two runs of the same browser on the
same rasterisation path, never as an absolute.

Rules held to throughout: one benchmark at a time; same machine, browser
build, viewport, data and duration within a comparison; headless and headed
never mixed in one table; and the rasterisation path recorded per run rather
than assumed.

---

## 7. Benchmark results

### 7.1 The isolated lab, on real hardware, no longer discriminates

The single most useful number in this section is the one that says the method
has a limit.

Twelve flows, one tab, 8 s windows, load 0.48 at the gate:

| Renderer | Chromium (GPU, headless) | Firefox (headed, `:0`) |
|---|---:|---:|
| `dashoffset` (the original SVG technique) | 34.8 | **132.0** |
| `dom-tiles` (ships today) | 57.3 | 143.7 |
| `canvas` | 58.8 | 142.7 |
| `canvas-worker` (OffscreenCanvas) | 58.5 | 143.0 |
| `webgl` (instanced, SDF glow) | 58.5 | 143.4 |
| *no-motion floor* | 58.7 | 143.5 |

Firefox's display refreshes at 144 Hz, so ~143 is the ceiling. **Every technique
reaches it, and so does the original `stroke-dashoffset` at 92% of it.** The
technique that measured 4-17 fps in every headless run in this project's history
is essentially free on a GPU.

That is a finding about the harness as much as the renderers. In an isolated
scene on real hardware there is no dynamic range left to measure with -- the
only visible difference is Chromium's software-ish 34.8 for `dashoffset`, and
even that is a GPU-flagged headless path rather than a real window. **Technique
choice has to be decided on the real dashboard**, which is section 7.3, and the
lab's value is now limited to proving a candidate works at all and to
scaling behaviour.

### 7.2 The metaphor costs nothing, measured two ways

Structurally identical first: all seven produce 36 animations, all on
`transform`, 72 elements, no filter on any animated layer and the same painted
area, in both engines.

And then in time, Chromium on GPU, 12 flows, against a 58.7 fps no-motion floor:

| Metaphor | fps | lag P95 | elements |
|---|---:|---:|---:|
| `dash` (ships) | 57.5 | 0.1 ms | 109 |
| `capsule` | 58.8 | 0.1 ms | 109 |
| `chevron` | 58.5 | 0.1 ms | 109 |
| `comet` | 58.4 | 0.1 ms | 109 |
| `particles` | 57.1 | 0.1 ms | 109 |
| `pulse` | 58.3 | 0.1 ms | 109 |
| `sweep` | 58.1 | 0.1 ms | 109 |

A spread of 1.7 fps across the whole set, inside run-to-run noise, with
identical main-thread lag.

Firefox headed is even flatter -- all seven land between **143.5 and 143.8 fps**
against a 143.2 floor, a spread of 0.3 fps, with `lagP95` of 1 ms everywhere:

| Metaphor | dash | capsule | chevron | comet | particles | pulse | sweep |
|---|---:|---:|---:|---:|---:|---:|---:|
| fps | 143.6 | 143.5 | 143.7 | 143.5 | 143.8 | 143.5 | 143.5 |

**What the flow looks like is a free choice**, in both engines, on real
hardware. That is what licenses section 14 to change the appearance on visual
grounds alone, and it is the single most useful structural fact in this study:
the design question and the performance question are separable, so neither has
to be traded against the other.

### 7.3 The real dashboard, on real hardware

The shipped dashboard with this study's changes, both engines headed on `:0`,
8 s windows, load recorded per case:

| View | devices | Firefox fps | Firefox lagP95 | Chromium fps | Chromium lagP95 | DOM mutations |
|---|---:|---:|---:|---:|---:|---:|
| aggregated | 2 | 140.4 | 2.0 ms | 132.6 | 1.1 ms | ~540 |
| aggregated | 4 | 141.5 | 1.0 ms | 134.8 | 1.6 ms | ~520 |
| aggregated | 8 | 141.1 | 2.0 ms | 136.4 | 1.2 ms | ~570 |
| devices | 2 | 139.2 | 4.0 ms | 135.4 | 2.0 ms | ~860 |
| devices | 4 | 137.7 | 3.0 ms | 129.1 | 2.3 ms | ~1160 |
| devices | 8 | 134.6 | 14.0 ms | 133.3 | 3.3 ms | ~2040 |

Both engines sit at the refresh ceiling in every view at every device count.
There is no configuration in this matrix where the flow animation is a problem.

The one number that moves is Firefox's `lagP95` at eight devices -- 14 ms -- and
it moves with DOM mutations rather than with the animation. That is the snapshot
rebuild, which is section 10's subject and section 14.4's fix.

### 7.4 Before and after, on the same machine in the same session

The two touched files were reverted from git for the "before" run and restored
afterwards -- no commit, no stash -- so both halves are the same harness, the
same scenarios and the same browser build minutes apart.

| Case | Firefox before → after | Chromium before → after |
|---|---|---|
| aggregated, 2 devices | 141.3 → 140.4 (0.99) | 127.4 → 132.6 (**1.04**) |
| aggregated, 4 devices | 140.8 → 141.5 (1.01) | 127.9 → 134.8 (**1.05**) |
| aggregated, 8 devices | 141.0 → 141.1 (1.00) | 126.4 → 136.4 (**1.08**) |
| devices, 2 devices | 140.7 → 139.2 (0.99) | 128.1 → 135.4 (**1.06**) |
| devices, 4 devices | 139.2 → 137.7 (0.99) | 126.3 → 129.1 (1.02) |
| devices, 8 devices | 135.7 → 134.6 (0.99) | 128.3 → 133.3 (**1.04**) |

**Firefox is neutral and Chromium gains 2-8%**, the latter almost entirely from
deleting the panel `backdrop-filter`. That is the honest result: on hardware
these changes are not a speed fix, because there was no speed problem left to
fix. Their value is that the picture now says something it did not say before.

Chromium's main-thread lag also improved where it was largest: `lagP95` at eight
devices went 5.2 ms → 3.3 ms.

### 7.5 A regression this benchmark caught, in this study's own change

Firefox's `lagP95` at eight devices went the other way: **3 ms → 14 ms**, while
DOM mutations rose from 1872 to 2030 and frame rate did not move.

The cause is the change itself. The pipe's style cache is keyed on its
appearance, and making the width continuous made that key change on nearly every
snapshot instead of only when a bucket boundary was crossed -- so the style was
rewritten far more often.

Quantising the width to half a pixel, which is below what the eye resolves at
these sizes, takes most of it back. Firefox, devices view, eight devices,
median of two runs:

| | before | after, continuous | after, quantised |
|---|---:|---:|---:|
| fps | 135.7 | 134.6 | **135.6** |
| lagP95 | 3.0 ms | 14.0 ms | **5.5 ms** |
| DOM mutations | 1872 | 2030 | 2051 |

The frame rate is fully recovered and the lag is not: 5.5 ms against 3.0 ms
before. The residual is honest cost -- the mutation count stays about 10% up,
because the encoding writes a `data-flow-watts` attribute per device pipe and
still rewrites a style whenever a flow crosses a half-pixel boundary. At 135 fps
with the main thread otherwise idle it buys a magnitude channel the dashboard
did not have, which is a trade worth making, but it is a trade.

It is recorded rather than quietly fixed because it is the argument for running
the before/after at all: the frame rate was unchanged and the picture was
correct, so nothing except the lag metric would ever have shown it.

---

## 8. What the candidates look like

Two different questions, measured two different ways.

**Distance from today** is `scripts/flow_lab_fidelity.mjs`: every candidate
frozen at dash phase zero and diffed against the shipped technique. Consistent
across both engines:

| Candidate | mean channel delta (Chromium) | differing |
|---|---:|---:|
| `svg-transform` | 0.44 | 0.7% |
| `svg-pattern` | 0.92 | 5.0% |
| `svg-mask` | 1.43 | 9.8% |
| **`webgl`** | **1.68** | 15.5% |
| `dom-tiles` (ships) | 1.77 | 16.9% |
| `canvas` | 1.92 | 18.0% |
| `canvas-bloom` / `canvas-worker` | 2.09 | 17.8% |

Two things fall out. **WebGL is closer to the original SVG than the renderer
that ships** -- its round caps and analytic glow are more faithful than square
gradient tokens. And `canvas-worker` matches `canvas-bloom` to three decimals in
both engines, which is a useful cross-check that drawing on a worker produces
the same picture rather than merely a fast one.

But a low number here is not the goal. This study explicitly permits a candidate
to look *better*, and a metaphor designed to differ scores worse by construction.

**Whether it is any good** is `scripts/flow_lab_gallery.mjs` plus 28 screenshots
of each metaphor repainted into the real dashboard, judged by four independent
evaluations with different briefs: the operator at a glance, visual craft,
density and scaling, and accessibility.

They converged on five things:

1. **None of the seven touches magnitude.** Unanimous. All seven sit on the same
   three-step rules, so 1.20 kW and 690 W are pixel-identical in all of them.
   Whatever fixes magnitude is a different change -- which is section 14.1.
2. **Static direction is a clean binary and only `chevron` has it.** `dash`,
   `capsule`, `particles`, `pulse` and `sweep` are symmetric patterns on a
   symmetric line: they would look identical drawn backwards. A screenshot *is*
   the `prefers-reduced-motion` state, and today's design deletes the pattern
   entirely under it.
3. **`sweep`, `pulse` and `particles` are worse than what ships.** Unanimous.
   They win the best-looking-still contest precisely by deleting hard edges,
   which is the same act as deleting information.
4. **`capsule` is free and strictly non-regressive.** Three evaluations rank it
   at or above `dash`, the fourth a dead heat. It measured *crisper*, not softer
   (edge acuity 17.8 against 12.6), and the round cap makes the corner
   truncation disappear (joint step 8.1 against 33.9).
5. **`chevron`'s requirement survives; its execution does not.** It is the only
   static direction carrier, and it is being asked to work below its own
   legibility floor: an outlined arrowhead first reads as an arrow at about
   7 px and is only confident at 10 px, against a production pipe of 3-6 px.

That is why section 14 ships `capsule` and the thickness encoding, and does not
ship a repeating direction glyph. Direction remains the one thing the still
frame does not state, and section 17 records it as the open item it is.

---

## 9. Browser-specific findings

### 9.1 Firefox: the devices-view cliff is a software-rasterisation artifact

This was the previous investigation's largest unsolved item -- the shipped
renderer recovering the aggregated view to ~57 fps while the devices view
collapsed to 9.2 fps at four devices and 12.4 at eight, with the trigger
isolated to the tile layer growing taller than the viewport.

On a real display it does not happen. The shipped dashboard, Firefox headed on
`:0`, 144 Hz panel, load at the gate 1.2-1.8:

| View | 2 devices | 4 devices | 8 devices |
|---|---:|---:|---:|
| aggregated | 140.4 | 141.5 | 141.1 |
| devices | 139.2 | 137.7 | **134.6** |

Against 11.2 fps for the same eight-device case in headless Firefox. The cliff
is real, reproducible and about eleven-fold -- and it is a property of Firefox
compositing the page on the CPU, which is what headless Firefox does on this
host even while reporting an NVIDIA device for WebGL.

What remains true is the *mechanism* -- measured once, and **not independently
re-verified**, because the adversarial review of that particular result was one
of the three that died on a session limit (17.1). It is worth keeping because it
will matter again on a weak machine: a transform animation on an element that is not
entirely inside the visual viewport makes Firefox re-rasterise the region every
frame, and the region here is full of `drop-shadow` and `backdrop-filter`. The
dose-response is sharp -- one such element is free, two collapse the page -- and
it is a function of viewport height rather than device count, which is why two
devices also collapse in a short enough window.

Main-thread lag stays low throughout (`lagP95` 1-4 ms), rising to 14 ms only at
eight devices where 2030 DOM mutations per window make snapshot rebuild, not
animation, the cost.

### 9.2 Chromium: the backdrop-filter ceiling was SwiftShader

Covered in section 13.1. Headless-default 9.6 fps, headless with GPU flags 58.5,
headed 115.6, for the identical eight-device case. What survives on hardware is
a ~19-39% cost confined to the control view, and only while something animates.

### 9.3 The renderer string is necessary and not sufficient

Both harnesses now record `rasterisation.renderer` per run. It comes from
`WEBGL_debug_renderer_info`, which names the device *WebGL* was given -- not the
one the page compositor is using. Firefox headless reports
`NVIDIA GeForce GTX 980, or similar` and composites the page on the CPU anyway.

So the probe can prove a run was software and cannot prove a run was hardware.
Only headed-on-`:0` is certain. Every decisive number in this study is taken
there.

---

## 10. Scaling

**In the isolated lab, on real hardware, nothing scales badly** -- see 7.1, where
every technique including the original `stroke-dashoffset` sits at the refresh
ceiling at 12 flows.

**On the real dashboard, the cost that grows is not the animation.** Going from
two devices to eight in Firefox costs 4.6 fps (139.2 to 134.6) while DOM
mutations grow from 834 to 2030 and `lagP95` from 4 ms to 14 ms. The animation
is flat; the snapshot rebuild is what scales.

That matters for what to do next. The flow layer is finished as a performance
problem on hardware. The thing that grows with device count is the rebuild path
-- and inside it, the read/write interleaving fixed in section 14.4 was the
dominant term.

---

## 11. Libraries and frameworks

Both questions have the same shape -- "would an existing thing carry some of
this for us?" -- and both answer no, for different reasons.

### 11.1 No visualisation or animation library is justified

The decisive measurement is not about any library's quality. It is about what
drives the animation.

| Driver | `document.getAnimations()` | Firefox, 40 flows |
|---|---:|---:|
| CSS keyframes (what ships) | 36 | 60.0 fps |
| `Element.animate()` (WAAPI) | 36 | 60.0 fps |
| GSAP / anime.js / Motion | **0** | **5.9-9.5 fps** |

Every JavaScript animation library **reverts the fix this project already
made**, because they all drive the transform from a main-thread
`requestAnimationFrame` loop rather than declaring an animation the compositor
can run. The tell is structural and visible before any timing is taken: a
library that registers no animations with the engine is animating on the main
thread by definition. The one library that ties, Motion's `animateMini`, ties
because it is a 46.5 KB wrapper over the platform's own `Element.animate()`.

Three further findings close the question:

- **The dashboard's CSP blocks the whole category.** `script-src 'self'` with no
  `'unsafe-eval'` (`dashboard/server.py:54`) **blocks WebAssembly outright** in
  both engines, and **PixiJS v8 refuses to initialise** because it uses
  `new Function()` for uniform codegen. Renderer libraries are not merely
  oversized here, several of them do not run.
- **GSAP is a licensing red flag for this repository specifically.** The project
  is AGPL-3.0-or-later; GSAP's npm `license` field is not an SPDX identifier.
- **The canvas scene-graph class inherits a disqualification** it cannot fix --
  see section 9's glow finding, which applies to Konva, Two.js, Paper.js, PixiJS,
  Cytoscape and vis-network alike.

There is a calibrated precedent in this repo for what a library must be worth:
`uPlot.iife.min.js` is 21.7 KB gzipped, vendored with its licence beside it, and
it buys an entire charting engine. The flow renderer it would replace is about
4.6 KB of hand-written code. Nothing on the list is close to that trade.

Worth recording: **the most-deployed energy-flow visualisation in the world uses
no library either.** Home Assistant's energy distribution card animates a few
`<circle>` elements along an SVG path with SMIL, hand-written.

Also worth recording: **React Flow ships exactly the technique this project
already measured and removed** -- `stroke-dasharray` with an animated
`stroke-dashoffset`. Adopting it would reintroduce the original defect.

### 11.2 No framework is justified by this study's evidence

The UI/state case and the rendering case must be separated, because only one of
them is real.

**Rendering: a framework cannot help.** The entire per-snapshot JavaScript
render path costs between **0.04% and 0.51% of wall-clock time**. A framework's
whole rendering contribution is to reduce that number, and reducing it to zero
cannot move a frame rate. Two measurements make this concrete:

- Render cost and frame rate are **anti-correlated** in this dataset. The most
  expensive render measured (Chromium energy view, 41.9 ms) runs at 56.3 fps;
  the cheapest (aggregated, 3.1-6.0 ms) ran at 5.4 fps in Firefox before the fix.
- **The main thread is idle exactly where the page is slow.** In the slowest
  configuration measured -- Firefox, control view, 4.22 fps -- a 50 ms interval
  timer still fires within 3 ms at P95. The constraint is not JavaScript.

The prior A/B already settled it independently: halving DOM mutations (446 to
216, the best case any framework can reach) changed the frame rate by 1%.

**What *is* left on the table is not DOM churn.** It is a **forced synchronous
layout inside the shipped tile renderer**, and it is 5-16x larger than the whole
render path: `buildFlowTileHost` accounts for 166.4 ms of `rebuildFlowTiles`'
167.4 ms. The cause is read/write interleaving --
`getBoundingClientRect()`, then a write to `layer.hidden`, then a second
`getBoundingClientRect()`, then four style writes, and only then a
`getComputedStyle`/`getCTM` read per pipe, each of which forces a fresh layout.
No framework fixes that; batching the reads before the writes does. It is a real
item and it is independent of which renderer wins.

**The maintainability case is real and separate.** `app.js` is 6,255 lines with
333 top-level functions and eight independently hand-maintained memoisation
mechanisms, plus two hand-written state-preservation workarounds that keyed
reconciliation would remove by construction.

But there is an argument against migration that this study is in a position to
make and that a general maintainability argument would miss: **the fix that
worked is a global CSS invariant** -- no CSS animation and no filter anywhere
inside the flow SVG subtree -- and it is enforceable today only because all
dashboard CSS lives in one file that a test can read
(`tests/test_dashboard_flow_tiles.py`). Component-scoped styles would distribute
that invariant across dozens of files and make the regression guard much harder
to write. A framework migration would have to bring a replacement for that
guard with it.

Measured bundle sizes (2026-09-04, one-component app, esbuild + gzip -9): Solid
4,963 B; Preact 5,364 B; Lit 5,875 B; Svelte 16,750 B; Vue 25,308 B; React with
react-dom 60,108 B.

**Verdict: no framework change is justified by this study.** The renderer
boundary should be cut so that a framework decision can be made later,
independently, on maintainability grounds -- which is section 14's business.

---

---

## 12. Rejected, and why

Each of these was built or measured, not merely considered.

### 12.1 Canvas over the flow SVG -- a one-line CSS bug, not a browser one

**This section previously said canvas and WebGL were disqualified. That was
wrong, and the way it was wrong is worth more than the conclusion was.**

The previous investigation recorded that a canvas over the flow SVG made the
page render darker and never found the cause. Re-measuring it, the effect
sharpened: it was not general darkening but **the SVG's `drop-shadow` glow
disappearing**, with strokes and HTML text keeping full brightness. The lit
fraction of the flow panel fell from 0.44 to 0.14. It reproduced identically
for a 2D and a WebGL context, in both engines, for a canvas that drew nothing
at all -- and no compositing hint touched it: `isolation`, `will-change`,
`translateZ(0)`, a `contain: paint` wrapper, isolating the host and giving the
SVG its own layer all measured the same.

An empty `div` of identical size and position changed nothing. So did an `img`
and a `video`. It looked like a browser-internal interaction specific to
`<canvas>`, and it was written up as one.

It is a line in this project's own stylesheet:

```css
/* dashboard/static/styles.css:1796 */
canvas { width: 100%; min-height: 94px; border: 1px solid rgba(148,163,184,.13);
         border-radius: 14px;
         background: linear-gradient(180deg, rgba(15,23,42,.80), rgba(15,23,42,.52));
         box-shadow: inset 0 1px 0 rgba(255,255,255,.05); }
```

A bare element selector, written for the chart canvases, that paints a
semi-opaque dark gradient onto **every** `<canvas>` on the page. The overlay was
never transparent. It was covering the glow with a dark background, exactly as
asked.

Every observation follows from that and none needs a browser mystery: a `div`,
an `img` and a `video` match no such rule; a 1x1 canvas is too small to cover
anything; `opacity: 0`, `visibility: hidden` and `display: none` never paint it;
a canvas behind the SVG puts its background behind; 2D and WebGL agree to the
byte because the background is CSS and has nothing to do with the context; and
no compositing hint helps because nothing about it is compositing.

Confirmed by the one-line fix, measured on the real dashboard:

| Overlay | Mean brightness | Lit fraction |
|---|---:|---:|
| no canvas | 48.04 | 0.4466 |
| canvas, dashboard CSS as-is | 34.44 | 0.1533 |
| **canvas with `background: none`** | **48.03** | **0.4466** |

Byte-identical to having no canvas at all, in both engines.

**So canvas and WebGL are not disqualified**, and the objection that removed
canvas from the previous study was never real. The other objection to canvas was the repaint cost: in headless Firefox any
per-frame update of canvas content collapses the page regardless of technique
-- full repaint 3.3 fps, many small canvases 4.2, dirty rectangles 3.2, against
a 59.9 fps ceiling. That one is a headless artifact too, and section 12.1.1 is
the measurement.

The architecture that survives is to **stop redrawing**: bake the dash texture
into a canvas once and move it with a CSS transform, which measures at the
no-animation ceiling in both engines. That is the same insight the HTML tile
layer already embodies, reached from the other direction -- which is why this
correction changes the shape of the evidence without, in the end, changing the
recommendation.

**The lesson worth keeping** is methodological. Two investigations in a row
concluded "the browser does something strange with canvas here" from a
consistent, reproducible, cross-engine, cross-context result. Cross-engine
agreement *to the byte* should have been the tell: two independent rendering
engines do not share an internal quirk that precisely, but they do share a
stylesheet. The variable that was never isolated was the page's own CSS.

#### 12.1.1 The Firefox canvas blocker is also a headless artifact

A canvas presenting once per frame, overlaid on the real dashboard, against the
same page with no canvas. A draw counter is reported beside the frame rate, so a
loop that stopped running cannot be mistaken for a loop that costs nothing --
`draws/s` equals the frame rate in every row below:

| Browser | Path | no canvas | 2D per frame | WebGL per frame |
|---|---|---:|---:|---:|
| Chromium | headless software | 60.0 | 59.8 | 60.0 |
| Chromium | headless + GPU | 60.0 | 60.0 | 59.5 |
| Chromium | headed | 141.4 | 141.8 | 142.1 |
| Firefox | headless | 58.0 | **8.3** | **8.0** |
| Firefox | **headed** | 143.7 | **141.9** | **134.2** |

The collapse is real and it is entirely a property of headless Firefox
compositing the page on the CPU. On a real display a canvas presenting every
frame costs Firefox 1% and WebGL 7%, and costs Chromium nothing on any path.

So **neither of the two objections that removed canvas from this project's
options was real**: the dimming was this repository's own stylesheet, and the
repaint collapse was the rasterisation path. Canvas and WebGL are genuinely
viable here.

They are still not recommended, and section 16 says why in one line: they reach
a ceiling that the renderer already reaches, and they charge for it in shader
code, a context-loss fallback path for a 24/7 wall panel, a second rendering
vocabulary, and a `background: none` that silently reintroduces 12.1 if anyone
forgets it.

### 12.2 Every JavaScript animation library

Measured, not assumed: they revert the fix. See section 11.1. The dividing line
is whether the animation is declared to the engine or driven from a main-thread
`requestAnimationFrame` loop, and every library in the category chooses the
latter.

### 12.3 Reducing the blur radius, and the standard backdrop mitigations

Sweeping `backdrop-filter` from 18px to 2px changes nothing in either engine on
either rasterisation path -- the expense is the backdrop-root flatten and
recompute, not the kernel. `will-change: backdrop-filter` does not help, and
`contain: paint` makes it **substantially worse** (12.0 to 9.0 fps). The lever
is the property's presence, not its parameters.

### 12.4 A framework change, for performance

Rejected on this study's evidence; see section 11.2. The main thread is idle
exactly where the page is slow, so the thing a framework improves is not the
thing that is wrong.

### 12.5 Carried forward from the previous investigation

- **`IntersectionObserver` to pause off-screen tiles.** Made things worse,
  aggregated 60 to 13 fps.
- **`animation-timing-function: steps(N)`.** steps(4) is barely motion and still
  only reached 2.13x.
- **Driving `stroke-dashoffset` from a JavaScript timer.** Slightly worse than
  the CSS animation, and adds a timer plus per-frame DOM writes.

### 12.6 Two harness traps that produced wrong answers

Recorded because both had already been believed.

- **`--matrix backdrop` could not answer its own question.** It hard-coded
  `animation="off"`, and backdrop-filter's cost is *only* an interaction with
  something animating. It pinned the one cell where the answer is always "free".
  Superseded by `--matrix glass`, which crosses the two axes.
- **`--gpu` defaults to `software`.** Any matrix re-run reproduces the
  SwiftShader artifact by default, and the pre-existing reports record
  `rasterisation.renderer` as `null` because the probe post-dates them, so a
  reader cannot tell from the file which hardware produced the number.

---

---

## 13. The glass panel

**Settled, and it does not need a benchmark to settle the visual half.**

`backdrop-filter: blur(18px)` is declared once, on
`.metric, .flow-panel, .rules-panel, .chart-panel, .device-card,
.energy-stats-panel` (`dashboard/static/styles.css:195`) -- ten panels on the
page, including the ones that *contain* the animation. An animating layer above
a backdrop root is the pathological arrangement for this property.

The question section 13 of the brief asks is whether the glass can be given up.
Measured, with every animation frozen in both shots so that dash phase cannot
be mistaken for a backdrop difference:

| Browser | View | Mean channel delta (/255) | Pixels differing strongly |
|---|---|---:|---:|
| Chromium | aggregated | 0.14 | 0.003% |
| Chromium | devices | 0.18 | 0.003% |
| Firefox | aggregated | 0.008 | 0.003% |
| Firefox | devices | 0.011 | 0.005% |

Three to five pixels per hundred thousand differ. The property was confirmed
active (`blur(18px)`) and confirmed removed (`none`) by reading back the
computed style in both engines, so this is not an experiment that failed to
run.

**Why it is invisible, rather than merely measured to be:** blur only changes
an image where it has high-frequency detail. The panels are 78-92% opaque, and
what sits behind them is three low-frequency linear gradients over near-black
plus `body::before` -- a 1px grid at `rgba(255,255,255,0.04)` under
`opacity: 0.25`, about 1% contrast. There is essentially nothing for an 18px
blur to act on.

### 13.1 What it costs, once the rasterisation path is controlled

The previous study's Chromium ceiling was measured on SwiftShader. Re-measured
across all three paths, same page, same scenario:

| Path | devices view, 8 devices, backdrop on | backdrop off |
|---|---:|---:|
| Chromium headless (SwiftShader, software) | 9.6 | 53.3 |
| Chromium headless + GPU flags | 58.5 | 57.3 |
| Chromium headed on `:0` | 115.6 | 132.0 |

So the 4.6-5.6x penalty is **almost entirely a software-rasterisation
artifact**. On a GPU the blur is free in the two views that matter.

A smaller, real cost survives on hardware, and it is confined to the **control
view** -- the one place where many animations run over glass: Chromium headed
27.8 with the blur against 38.7 without (a 39% penalty), Firefox headed 78.1
against 90.7 (16%). And it is strictly an *interaction*: with nothing animating,
backdrop-on is not slower than backdrop-off at all.

Two mitigations were tested and neither works. Sweeping the blur radius from
18px to 2px changes nothing in either engine on either path -- the expense is
the backdrop-root flatten and recompute, not the kernel size. `will-change:
backdrop-filter` does not help, and `contain: paint` makes it substantially
worse (12.0 to 9.0 fps).

### 13.2 Decision

No alternative glass treatment needs designing, because there is nothing to
replace: the property produces no perceptible pixels here. It has been
**deleted from the panel rule** (`dashboard/static/styles.css`), with the
reasoning recorded beside it so it is not restored by someone who assumes it is
doing something. `.modal-backdrop` keeps its blur: that one sits over real
page content, where a blur is both visible and wanted.

Verified after the change: restoring `blur(18px)` onto the shipped stylesheet
moves the rendered page by a mean of 0.15/255 in Chromium and 0.07/255 in
Firefox. The dashboard looks the same as it did.

A note on reading the `glass` matrix run *after* the removal: its `backdrop=on`
and `backdrop=off` cells now differ by 1-2 fps in every combination, because the
`off` cell injects `backdrop-filter: none` onto a rule that no longer sets it.
That run confirms the property is gone; it cannot measure what it cost. The cost
numbers in 13.1 come from runs taken before the removal, on the unmodified
stylesheet.

The glass character was never carried by the blur. It is carried by the
translucent gradient, the border, the inset highlight and the `::before` sheen
-- none of which costs anything per frame.

---

---

---

## 14. What was changed, and why each change earned it

Four changes were made to production. Every one is backed by a measurement in
this report, and each is small enough to revert on its own.

### 14.1 Magnitude is encoded again

**The defect.** Magnitude was three steps -- `low` under 150 W, `medium` to
600 W, `high` above -- rendered as stroke widths 4, 5 and 6 px. The channel that
was supposed to carry the difference within a step was speed, and it spans
**1.55x** (1.70s to 1.10s per dash period) across a power range of **75x or
more**. A 700 W flow and a 3000 W flow were pixel-identical, and so were the
1.20 kW and 690 W PV feeders sitting one above the other in the devices view.

Four independent visual evaluations reached this separately, and it is the one
thing none of the seven metaphors could fix, because all seven were painted on
top of the same three-step rules.

**The change.** Thickness is now continuous in watts, on a scale that snaps to a
coarse ladder taken from what the system is actually doing, with hysteresis so
one device ramping does not resize every ribbon on the page.

| Flow | before | after (2 kW scale) |
|---|---:|---:|
| 40 W | 4 px | 4 px |
| 170 W | 5 px | 5 px |
| 690 W | 6 px | 8 px |
| 1200 W | 6 px | 10.5 px |
| 2000 W | 6 px | 15 px |

**Corrected after the first installation saw it.** The range in this table is
not what ships. Run on real hardware, ribbons of that weight read as heavy
rather than as informative -- an 800 W flow on a 1 kW scale drew at 13 px -- and
the top of the range was brought down to 8 px, two above the 6 px the three
steps already used. The column above therefore describes the version this study
argued for, not the one in the dashboard; the shipped scale runs 4 px to 8 px
and the same rows read 4, 4.5, 5.5, 6.5 and 8 px. What survives is the finding this study exists for --
that magnitude has to be encoded at all, and that thickness is the channel to
encode it in. What it got wrong is how much of the panel to spend on it, which
is a question no measurement here could answer and the first look at a real
installation answered immediately.

Thickness was chosen over brightness or hue because it is the one magnitude
channel that survives desaturation and the deuteranopic collapse of `#ffd166`
against `#39e58c` -- the two colours this dashboard uses for PV and battery.

Two objections were raised against it and both were checked rather than
dismissed. That a linear mapping "starves the bottom" does not apply here: the
4 px floor means **no flow draws thinner than the old minimum**. That thickness
should be confined to the aggregated view was argued from a mockup whose
8-device panel did not match the real layout; rendered in the actual devices
view it is exactly where it earns most, because ranking devices against each
other is the question that view exists to answer.

### 14.2 The tokens have round ends again

`dom-tiles` replaced an SVG stroke that had `stroke-linecap: round` with a
square-ended gradient. That was a small fidelity regression nobody had noticed.
The tokens are now a tiled rounded rect, which restores the round caps, matches
the pill geometry the rest of the dashboard uses, and makes the cut where a run
stops at a device box stop reading as a guillotine.

It is free: **every metaphor is structurally identical** -- 36 animations, all
on `transform`, 72 elements, no filter on any animated layer, identical painted
area, in both engines.
Of the seven tested, this is the only one all four evaluations rank at or above
what ships.

A `data:` URI is used rather than a generated stylesheet because this
dashboard's CSP is `style-src 'self'` with no `'unsafe-inline'`, which blocks a
generated `<style>`, while `img-src 'self' data:` permits this.

### 14.3 The glass stopped blurring nothing

Deleted from the panel rule; see section 13. Visually undetectable, and it was
costing about 19% in the control view on real hardware.

### 14.4 The tile rebuild stopped forcing layout per pipe

`buildFlowTileHost` interleaved reads and writes: measure the SVG, write
`layer.hidden`, measure the parent, write four style properties, and only then
read a computed style and a CTM **per pipe** -- each of which flushed layout
again. It accounted for 166.4 ms of a 167.4 ms rebuild.

Every read now precedes every write. Nothing between them changed what the reads
would return, so the ordering was free to change and the cost was not. A test
pins the ordering, and was checked against the previous code to confirm it can
actually fail.

### 14.5 A landmine defused with a comment

The bare `canvas { ... background: linear-gradient(...) }` selector at
`styles.css:1796` is left alone -- narrowing it risks the charts it was written
for -- but it now carries a comment saying what it does to any other canvas on
the page, because it has already cost two investigations a wrong conclusion.


---

## 15. Migration plan

There is no migration. That is the point of the recommendation: the rendering
technology does not change, so nothing has to be moved, no build step appears,
no dependency is added and no file is served that was not served before.

**What is already in place** (section 14), each revertible on its own:

| Change | Files | Reverting it costs |
|---|---|---|
| continuous magnitude | `app.js` (~50 lines, 4 call sites) | back to three steps |
| capsule tokens | `app.js` (`flowTileBackground`) | back to square gradient tokens |
| panel `backdrop-filter` deleted | `styles.css` (one declaration) | nothing visible, ~19% in the control view |
| reads batched before writes | `app.js` (`buildFlowTileHost`) | a forced layout per pipe per rebuild |

Guarded by 24 contract tests in `tests/test_dashboard_flow_tiles.py`, which run
without a browser. Five are new: magnitude is continuous rather than stepped, the scale follows
the installation, the scale does not flicker between rungs, the reads precede
the writes, and a reversed flow still runs backwards. The read-ordering one was
checked against the previous code to confirm it can actually fail.

**What is staged but not built**, in the order it is worth doing:

1. **An anchored arrowhead per segment** (section 17.2). The one remaining
   information defect: a stopped animation says nothing about direction. Needs
   its own measurement pass because it adds an element per segment.
2. **An explicit flow model.** Cut the seam at
   `EMS state -> flow model -> renderer` so the renderer stops re-deriving its
   model from the DOM. This is what would make a future renderer -- or a future
   framework -- a contained decision instead of a rewrite. It is a refactor with
   no user-visible effect, which is exactly why it should be done deliberately
   rather than folded into something else.
3. **A macOS measurement** (section 17.1). Not a change at all, and still the
   highest-value thing anyone could do next.

**What should not be done**: adopt a rendering library, adopt an animation
library, move the flow to a canvas or to WebGL, or change frontend framework for
performance reasons. Sections 11 and 12 give the evidence for each.

---

## 16. Decision matrix

Weights are the brief's, unchanged. Scores are 0-10.

The five options are: **A** keep the current renderer exactly as it was, **B**
keep the technology and fix the encoding and craft, **C** move the flow to a 2D
canvas, **D** move it to WebGL, **E** adopt a rendering or animation library.

| Criterion | Weight | A keep | B improve | C canvas | D WebGL | E library |
|---|---:|---:|---:|---:|---:|---:|
| Firefox performance | 20% | 9 | 9 | 9 | 9 | 1 |
| Chromium performance | 20% | 9 | 9 | 9 | 10 | 3 |
| Visual quality | 15% | 4 | 8 | 5 | 6 | 5 |
| Aggregate view | 10% | 5 | 9 | 6 | 6 | 5 |
| Devices view scaling | 10% | 5 | 8 | 6 | 7 | 4 |
| Browser compatibility | 5% | 10 | 10 | 8 | 5 | 7 |
| Implementation complexity | 5% | 10 | 8 | 4 | 2 | 3 |
| Maintainability | 5% | 8 | 8 | 5 | 3 | 4 |
| Future framework integration | 5% | 6 | 6 | 6 | 5 | 4 |
| Bundle / dependency / licence | 5% | 10 | 10 | 9 | 8 | 2 |
| **Weighted total** | | **7.40** | **8.60** | **7.15** | **7.15** | **3.45** |

Where the numbers come from, and where a score is a judgement rather than a
measurement:

- **Firefox performance.** A, B, C and D all measured at the refresh ceiling on
  a real display -- 134.6-141.5 fps for the shipped renderer across every view
  and device count, and 141.9 and 134.2 for a 2D and a WebGL canvas presenting
  every frame (sections 9.1, 12.1.1). An earlier draft scored C and D at 4 on a
  headless result; that was wrong and section 12.1.1 is the correction. E is
  measured and is genuinely slow: every JS animation library reverts the shipped
  fix.
- **Chromium performance.** All of A, B, C, D reach the ceiling on a real GPU.
  D scores one higher for being the only technique flat from 12 to 100 flows in
  the lab with sub-millisecond main-thread lag.
- **Visual quality.** A scores 4 because magnitude is genuinely broken -- three
  steps for a 75x range -- not because it is ugly. B scores 8 on four
  independent evaluations. C and D inherit whatever metaphor they draw, so they
  score for the glow quality they can produce, which for D is genuinely good.
- **Implementation complexity and maintainability.** A is free by definition. B
  is 154 added and 25 removed lines across two files, with five new contract
  tests. C requires
  the static pipe bases to move onto the canvas and a `background: none` that,
  if forgotten, silently reproduces the bug in section 12.1. D adds shader code,
  a context-loss fallback path for a 24/7 wall panel, and a second rendering
  vocabulary in a codebase that has one.
- **Bundle and licence.** A, B and C add nothing. D adds nothing if hand-written.
  E scores 2: the smallest credible library is several times the 4.6 KB of
  hand-written code it would replace, PixiJS does not run under this CSP at all,
  and GSAP's licence is a poor fit for an AGPL project.

C and D tie exactly at 7.15, and both land **below** simply keeping the current
renderer untouched. That is the shape of the answer: they are not bad, they are
unnecessary -- the same ceiling for more machinery.

**B wins, and it wins on the criteria the brief weighted highest** -- but not by
being faster. A and B are indistinguishable on performance on real hardware.
B wins because visual quality, the aggregate view and devices-view scaling are
35% of the weight between them, and those are exactly where A has a defect that
no rendering technology can fix.

---

## 17. Risks and open questions

### 17.1 Still unmeasured

- **macOS**, which is where the symptom was reported. There is no macOS host and
  nothing here should be read as covering it. This remains the single most
  valuable outstanding measurement, and it is a measurement rather than a
  change.
- **WebGPU.** `navigator.gpu` is absent in both engines here. Assessed for
  feasibility only, never benchmarked, and not recommended.
- **Three adversarial reviews did not run.** The critiques of the WebGL,
  Firefox-cliff and canvas-isolation research died on a session limit. Of those
  three, only the canvas finding was independently re-verified from scratch --
  and that one turned out to overturn a conclusion, which is a fair warning
  about the two that were not.

### 17.2 Direction is still mute in a still frame

Today's design, and the version this study ships, say nothing about which way
energy is flowing when the animation is stopped. Under
`prefers-reduced-motion` the dashboard removes the pattern entirely and leaves
a flat line.

`chevron` is the only candidate that fixes it, and it cannot be shipped as
drawn: an arrowhead needs roughly 7 px to read as an arrow and 10 px to be
unambiguous, against a production pipe of 3-6 px. Now that thickness is
continuous, high flows do reach that size -- but low flows do not, and a cue
that appears only on big pipes is worse than one that is consistently absent.

The shape that would work is one **anchored** arrowhead per segment at the
destination end, sized independently of the stroke and kept when the animation
is off, rather than a repeating glyph train. That was designed and not built,
because it adds an element per segment and would need its own measurement pass.

### 17.3 The magnitude scale is a judgement call

The ladder-with-hysteresis makes a pipe's thickness depend, weakly, on what the
rest of the system is doing. A rung change is visible: every ribbon resizes at
once. Hysteresis at 50% of the current rung makes it rare, and the alternative
-- a fixed full-scale -- is wrong for either a 600 W balcony system or a 10 kW
installation, whichever one it is not tuned for.

If this turns out to be annoying in daily use, the honest fixes are a
longer-baseline reference (a rolling daily maximum rather than the current
snapshot) or a configured system size. Both are small; neither is justified
before someone has watched it for a day.

### 17.4 What this study did not touch

- **The control view.** It carries 26 `controlResultBorderFlow` animations and
  is the one view where `backdrop-filter` measurably cost something on real
  hardware. Deleting the panel blur helps it, but the view itself was never
  examined.
- **The renderer boundary.** The tile renderer still re-derives its model from
  the DOM on every rebuild -- `getComputedStyle` on a `display:none` SVG path,
  plus `getCTM` and `getBoundingClientRect`. That round-trip, not the tile
  technique, is the seam a future renderer would have to cut. Reading the
  display decisions back out of CSS is *why* `animation_mode`,
  `prefers-reduced-motion`, idle and the speed buckets are not implemented
  twice, so the round-trip buys something real. It should be replaced by an
  explicit flow model, not simply deleted.
- **A framework migration**, which this study finds unjustified on performance
  grounds and does not evaluate on maintainability grounds beyond section 11.2.

---

## 18. Recommendation

```text
RECOMMENDATION: B — improve the current renderer
WINNING TECHNOLOGY: HTML + CSS, animated by the compositor
WINNING RENDERER: the HTML tile layer (dom-tiles), with a continuous
                  magnitude encoding and capsule tokens
CONFIDENCE: HIGH on Linux with a GPU; LOW for macOS, which was never measured
```

### Why it wins

Not because it is faster. On real hardware it is indistinguishable from what
was already there, and so is canvas, and so is WebGL, and so — at 132 fps —
is the original `stroke-dashoffset` technique this project spent two
investigations escaping.

It wins because **the flow visualisation's real defect was never a rendering
defect.** Magnitude was encoded in three steps across a 75x range, with the
in-step difference carried by a speed channel spanning 1.55x. A 700 W flow and
a 3000 W flow were the same picture. No rendering technology fixes that, and
every one of the seven metaphors tested inherited it.

The structural fact that makes the fix free is section 7.2: the renderer moves
one transform-animated layer per segment and paints it with a background image,
so **what the flow looks like is independent of what it costs** — 143.5 to
143.8 fps across seven visually different metaphors in Firefox, 57.1 to 58.8 in
Chromium, identical element and animation counts in both. The design question
and the performance question are separable, so the design could be chosen on
design grounds.

### Performance evidence

- Shipped dashboard, both engines headed on a real GPU: **129-141 fps** in every
  view at 2, 4 and 8 devices, `lagP95` 1-4 ms (section 7.3).
- The two problems this study inherited were both software-rasterisation
  artifacts. Firefox's devices-view cliff: 11.2 fps headless, **134.6 headed**
  at eight devices. Chromium's `backdrop-filter` ceiling: 9.6 fps on
  SwiftShader, 58.5 with GPU flags, **115.6 headed** (sections 9.1, 13.1).
- Canvas and WebGL reach the same ceiling and add nothing to it.

### Visual evidence

Four independent evaluations of the seven metaphors rendered into the real
dashboard converged: none of them touches magnitude; `sweep`, `pulse` and
`particles` are worse than what ships; `capsule` is the only strictly
non-regressive improvement (edge acuity 17.8 against 12.6, corner step 8.1
against 33.9); and thickness is the right channel for magnitude because it is
the only one that survives desaturation and the deuteranopic collapse of this
dashboard's PV and battery colours.

### Architectural reasoning

The technology does not change, so nothing migrates: no build step, no
dependency, no new file served, and the CSS invariant that makes the whole fix
enforceable — no animation and no filter inside the flow SVG — stays testable
because all the CSS is still in one file a test can read.

### The biggest remaining risk

**macOS.** Every conclusion here is Linux. This study's central lesson is that
two of its predecessor's headline findings dissolved when the rasterisation path
changed, and macOS is another rasterisation path that has never been measured.
The changes are safe there — they remove work rather than adding it — but the
claim "this is fast" is not established for the platform the symptom was
actually reported on.

### Recommended next step

Run `scripts/dashboard_bench.py --matrix views --gpu headed` on a Mac. It is one
command, it needs no code, and it is worth more than any further work on this
machine.

---

## 19. Postscript: what this study got wrong

Three conclusions in this document were written, measured, and then overturned
by better measurement. They are left visible rather than quietly fixed, because
the failure mode is the useful part.

**The canvas glow.** A canvas over the flow SVG made the glow disappear. It
reproduced in both engines, for both 2D and WebGL, for a canvas that drew
nothing, and no compositing property changed it. It was written up as a browser
interaction and it was a bare `canvas {}` selector in this project's own
stylesheet painting a dark gradient onto every canvas on the page. The tell was
there and was read backwards: **two independent rendering engines agreeing to
the byte is evidence of shared input, not shared internals.**

**The hardware.** The previous investigation recorded that everything it
measured was software-rendered. That was true for Chromium and false for
Firefox, and the distinction was never recorded per run — so numbers from two
different rasterisation paths sat in the same tables. Both harnesses now record
the renderer string per run. That probe is necessary and not sufficient: it can
prove a run was software and cannot prove it was hardware, because headless
Firefox reports an NVIDIA device for WebGL while compositing the page on the
CPU. Only headed-on-`:0` is certain.

**The canvas repaint collapse.** Having found that the dimming was not real,
this document still carried canvas's *other* disqualification: in Firefox, any
per-frame canvas update collapsed the page. It did, in headless Firefox, which
composites on the CPU. On a real display the same experiment costs 1% for a 2D
canvas and 7% for WebGL. The mistake here was narrower and more ordinary than
the first two: having established in section 2.1 that headless Firefox is not a
hardware measurement, I went on quoting a headless Firefox measurement.

The common thread in the first two is a consistent, reproducible, cross-engine
result — the kind that feels like a finding. Reproducibility rules out noise. It
does not rule out a shared cause sitting outside the variable under test. The
third is a plainer failure: knowing a caveat and not applying it.

The practical residue is in the harness rather than in anyone's discipline.
`--gpu` and the per-run renderer string exist so that the next person cannot
quote a software number without it being labelled as one, and
`scripts/canvas_present_probe.mjs` exists so the specific question that went
wrong three times has a one-command answer.
