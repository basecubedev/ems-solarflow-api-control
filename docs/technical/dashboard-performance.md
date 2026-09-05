# Dashboard performance

How the dashboard frontend stays cheap over time and across several open tabs,
and how to prove that a change helped. The visual rules are in
[`../developer/dashboard-style-guide.md`](../developer/dashboard-style-guide.md);
this document is only about cost.

## Measuring first

`scripts/dashboard_bench.py` drives real browsers over Playwright against the
synthetic payloads in `scripts/dashboard_preview_data.py`. It needs no EMS, no
hardware and no network.

```bash
python3 scripts/dashboard_bench.py --matrix ab       --browser firefox
python3 scripts/dashboard_bench.py --matrix baseline --browser chromium
python3 scripts/dashboard_bench_report.py reports/dashboard-perf/*.json
```

Reports and the measurement caveats live in
[`../../reports/dashboard-perf/README.md`](../../reports/dashboard-perf/README.md).
Two rules that the harness cannot enforce:

- **Never run two benchmarks at once.** They contend for the same CPU and both
  results become fiction.
- **Compare ratios, not absolutes.** Headless renders in software.

## The live path

One snapshot arrives per `loop_interval` over SSE, or every two seconds over the
polling fallback. Both enter `updateSnapshot`, which is the only place that
decides whether a render happens.

```
EventSource /api/events ─┐
                         ├─> updateSnapshot ─> renderSnapshot ─> the visible view
GET /api/live (polling) ─┘        │
                                  ├─ same timestamp as the last render? stop.
                                  └─ document.hidden? store it, render on visibilitychange.
```

Three invariants hold here, and
[`../../tests/test_dashboard_live_transport.py`](../../tests/test_dashboard_live_transport.py)
fails if any of them stops holding:

1. **An unchanged snapshot renders nothing.** Measured: an unchanged timestamp
   drops a view's DOM mutations by roughly half, which is the whole rebuild.
2. **A hidden tab renders nothing.** Browsers throttle `requestAnimationFrame`
   and pause CSS animations in a background tab, but they do not throttle
   `EventSource` delivery or a `setInterval` poll. The newest state is kept and
   one render happens when the tab is shown again, however many updates arrived
   meanwhile.
3. **A tab holds at most one live stream.** All stream creation goes through
   `connectEventSource`, which closes the previous one first.

### Recovering from the polling fallback

The fallback used to be permanent, and two ordinary events trigger it: the third
tab from one machine is refused by `MAX_SSE_CONNECTIONS_PER_IP`, and
`SSE_MAX_CONNECTION_SECONDS` closes even a healthy stream after thirty minutes.
Tabs therefore drifted from SSE to polling monotonically, which is what "it gets
worse the longer it runs" was.

While polling, a timer retries the stream every `SSE_PROMOTION_INTERVAL_MS`.
Telemetry on a retry cancels both the polling loop and the retry timer.

## Static assets

Responses carry a content-derived `ETag` and revalidate; a tab with an unchanged
copy gets a 304 with no body. The bytes are read once and cached by mtime and
size. `/api/*` keeps `Cache-Control: no-store` and gains no validator —
[`../../tests/test_dashboard_static_caching.py`](../../tests/test_dashboard_static_caching.py)
asserts both halves so the two paths cannot drift together.

Revalidation rather than `immutable` because the frontend ships unfingerprinted.
A bundle with content-hashed filenames could be served `immutable` instead.

## Animation cost

An SVG element cannot be composited on its own, so a CSS animation on one makes
the browser rasterise its whole subtree again for every frame -- and the flow
subtree is full of `drop-shadow` filters. Which property is animated barely
matters; that an animation is running at all does. Two rules used to do it: the
dash pattern on `.pipe-energy`, and `softPulse`, whose keyframe animated the
`filter` property itself.

Neither could be removed on its own. Firefox, devices view, four devices:

| | fps |
|---|---:|
| both running | 3.8 |
| dash animation off | 4.2 |
| `softPulse` animating opacity instead of a filter | 4.2 |
| **both** | **57.9** |

### The flow tile layer

The dashes are now an HTML layer above each flow SVG, one box per visible run of
each pipe, each box clipping a tiled token strip that a CSS `transform` moves by
one dash period. The token is a rounded rect drawn as a `data:` URI, which
restores the round line caps the SVG stroke had; a generated stylesheet would be
blocked by the dashboard's `style-src 'self'` CSP, and `element.style` is not. A transform on a promoted layer is handled by the
compositor and repaints nothing: eighty of these tiles measured 60.2 fps against
60.1 with none at all. `softPulse` animates opacity only.

| Firefox headless, before → after | |
|---|---|
| aggregated view, any device count | 5.5 → **57** fps |
| devices view, two devices | 4.7 → **57.6** fps |
| devices view, four or more | 4.1 → 9.2 fps |

Those are headless numbers; see "What is still slow, and what only looked slow"
below before drawing a conclusion from the last row.

Three properties of the renderer are worth knowing before changing it:

- **It reads the appearance back out of the CSS.** Dash pattern, width, colour,
  opacity, speed, direction and whether to move at all come from
  `getComputedStyle` on the `.pipe-energy` element the stylesheet still defines.
  That is why `animation_mode`, `prefers-reduced-motion`, the idle state and the
  four flow-speed buckets keep working without being implemented twice. Change
  the CSS and the layer follows; do not add a second source of truth.
- **Nothing in the layer may carry a `filter`.** Re-measured headed on a GPU and
  it still holds -- the only inherited rule in this series that survived: a
  `drop-shadow` on the moving layer costs Chromium 71% of its frame rate at
  forty-eight pipes and 86% at ninety-six, while its main thread sits idle. A
  filtered layer cannot be "rasterise once, then only move", which is the whole
  basis of this renderer. A halo that is *drawn into the tile* costs nothing
  instead, in either engine at any size. Today's halo comes from the static
  `.pipe-glow` stroke in the SVG.
- **One animated layer per pipe segment.** What costs is the number of animated
  layers, not the number of painted elements: a construction painting 1728
  elements measured *faster* than one painting 576, and Chromium falls roughly
  inversely with animated-layer count past about 288 of them. Measured at twelve
  devices: 138.4 fps with one layer per segment, 99.4 with two, 88.1 with four
  tokens. Firefox holds the refresh rate to 2304 layers and bends at 4608.
  Complexity belongs inside a layer, never in more of them.
- **Magnitude is thickness, and it is continuous inside the old range.**
  `--pipe-width` is proportional to power on a scale that snaps to a coarse
  ladder taken from the system's own output, with hysteresis. It replaced three
  fixed steps (4, 5 and 6 px at 150 W and 600 W thresholds) that made a 700 W
  flow and a 3000 W flow identical. It runs from the steps' own 4 px floor to
  8 px, two past where they stopped: a top of 15 px was shipped first and drew
  an 800 W flow at 13 px on a 1 kW scale, which reads as a heavier page rather
  than a more informative one. The scale buys the ordering between two flows,
  not more of the panel covered. Thickness was chosen because it is the one magnitude
  channel that survives desaturation and the deuteranopic collapse of the PV and
  battery colours.
- **It never asks the layout engine a question an attribute can answer.**
  Reading a box forces the browser to flush pending style and layout first, and
  the rebuild runs once per snapshot. Deciding whether a view is on screen by
  measuring it cost 25-36 ms of main thread per snapshot in Firefox in the
  control view -- where no flow SVG is on screen at all, so the measurement was
  taken purely to discover that it should not have been taken.
  `flowSvgOffScreen` answers from the `hidden` attribute `setFlowView` already
  sets, and the rect check stays behind it as the fallback for anything hidden
  some other way. Removing that flush cut main-thread work by 66-76% in the
  control and energy views and left the views where the SVG is visible
  unchanged.
- **It refuses what it cannot represent.** The path parser takes only `M`, `L`,
  `H` and `V`; anything else, or a browser without `getComputedStyle`, leaves the
  CSS animation in place rather than showing no flow at all.

[`../../tests/test_dashboard_flow_tiles.py`](../../tests/test_dashboard_flow_tiles.py)
pins all three.

### What is still slow, and what only looked slow

Every number above is **headless**, which on this project's Linux host means
Chromium rasterises in software (ANGLE/SwiftShader) and Firefox composites the
page on the CPU. Re-measured with a real GPU and a real window, two of the
things this document used to list as unsolved do not happen:

| Firefox, devices view, 8 devices | fps |
|---|---:|
| headless | 11.2 |
| headed on a real display | **134.6** |

| Chromium, devices view, 8 devices | fps |
|---|---:|
| headless, default (SwiftShader) | 9.6 |
| headless with GPU flags | 58.5 |
| headed on a real display | **115.6** |

So the Firefox devices-view cliff and the Chromium `backdrop-filter` ceiling
were both software-rasterisation artifacts. On hardware the shipped dashboard
runs at the display's refresh ceiling in every view at 2, 4 and 8 devices in
both engines.

The **mechanism** behind the Firefox cliff was measured once and not
independently re-verified. It will matter again on a weak machine: a transform animation on an element that is not entirely inside
the visual viewport makes Firefox re-rasterise that region every frame, and the
region is full of `drop-shadow`. The dose-response is sharp -- one such element
is free, two collapse the page -- and it tracks viewport height rather than
device count.

`backdrop-filter` has been **removed from the panel rule**. It was measured
invisible on this dashboard: the panels are 78-92% opaque over a near-featureless
background, so turning an 18px blur off changes the page by a mean of 0.008/255
in Firefox. It was not free -- on a GPU it cost about 19% in the control view,
and only while something animated. Reducing the radius does not help; the cost
is the backdrop-root recompute, not the kernel.

### Reading a benchmark from this project

`--gpu {software,gpu,headed}` selects the rasterisation path, and every report
written from 2026-09-04 onward records `environment.gpu`, the observed
`rasterisation.renderer` per run, and the machine's `load_average`. `--max-load`
makes each case wait for a quiet machine first.

One trap is worth stating: the renderer string comes from
`WEBGL_debug_renderer_info`, which names the device *WebGL* was given, not the
one compositing the page. Headless Firefox reports an NVIDIA device and still
composites on the CPU. The probe can prove a run was software; it cannot prove
one was hardware. Only headed on a real display is certain.

`dashboard.animation_mode` (`normal` | `reduced` | `off`) is honoured together
with `prefers-reduced-motion`, but **do not reach for it as a performance
control**: measured on this hardware it makes the dashboard's main-thread work
1.3 to 2.3 times *more* expensive, because the cost is a layout flush and a
running animation keeps the style tree from accumulating one between snapshots.
Frame rate is unaffected either way. See the "nothing is happening" section
above and section 4 of
[`../../reports/dashboard-perf/firefox-macos-investigation.md`](../../reports/dashboard-perf/firefox-macos-investigation.md).
It stays as an accessibility and preference setting, which is the job it does
well; on hardware weak enough that the saved compositor work outweighs the extra
main-thread work it may still pay, and that has not been measured.

The full account is in
[`../../reports/dashboard-perf/energy-flow-visualization-study.md`](../../reports/dashboard-perf/energy-flow-visualization-study.md),
which supersedes
[`flow-rendering-investigation.md`](../../reports/dashboard-perf/flow-rendering-investigation.md)
on the two points above and on the canvas renderer it withdrew.

### What the artwork is allowed to cost

[`energy-pipe-performance-study.md`](../../reports/dashboard-perf/energy-pipe-performance-study.md)
compared fourteen ways of building the moving token inside this architecture.
At the size a dashboard draws -- twelve devices, 108 animated layers -- none of
them is distinguishable from any other in either engine, so how the flow looks
is not a performance decision. Over 108 traced Chromium cases the renderer
recorded **zero `Paint` events**, and `RasterTask` in one case out of 108: the
layer is rasterised once and thereafter only moved, which is why a sixteen-fold
texture, a richer tile and a baked gaussian halo are all free.

Two things are not free, and both are on the obvious path -- the two rules
above. A third is invisible to a frame rate: growing the layer costs texture
memory even when it costs no frames (72 px of transparent padding per side took
the scene from 5.6 MB to 14.8 MB of composited layer), while growing the
*texture* costs neither, because the layer is sized by the element and not by
the image.

## Animate a property the compositor can carry

A CSS animation is cheap or ruinous depending only on which property it moves.
`transform` and `opacity` are handed to the compositor; anything that changes
what a pixel looks like -- `background-position`, `background-size`,
`mask-position`, `filter` -- makes the browser repaint that element on every
frame, and the cost is multiplied by however many elements the rule matches.

The control view learned this the expensive way. Its result chips carried a
travelling one-pixel gradient border driven by `background-position`, and the
view puts one on every stage of every device: twenty-six at four devices, all
repainted sixty times a second. Chromium drew that view at 45.2 fps with the
effect running and 134.6 with it switched off. Removing the mask instead of the
animation changed nothing, so the mask was never the cost. Firefox was
unaffected either way.

The effect is unchanged and costs no frames: the gradient lives in a child that
is translated, the child is two tiles wide so the loop has no seam, and the mask
that turns the box into a ring stayed where it was. 133.1 fps at eight devices,
frame p95 27.8 ms down to 7.0. It is not free of *main thread* -- it is one of
the animations in the "every animation costs a style recalculation per frame"
result below -- but that costs no frames in either engine at any size measured.

### The same mistake, made twice, and why the guardrail is now general

`.primary-button.compact::after` kept the old construction on the measured
grounds that it was a single element. It is a single element **in the read-only
dashboard**, which was the only state that had ever been benchmarked. With
authentication configured the runtime editor renders a submit button per stage
card -- twelve at four devices, twenty at twelve -- and the authenticated
control view drew at **52.8 fps at four devices and 36.1 at twelve**, painting
4463 times per ten seconds against the read-only view's 175. Disabling that one
rule was indistinguishable from disabling every animation on the page.

Both now use the same `.button-ring` / `.control-result-ring` construction and
`@keyframes controlResultBorderFlow` is gone: **52.8 -> 139.2 fps** and
**36.1 -> 133.8**, frame p95 to 7 ms, 96-97% of the paints removed. Firefox was
unaffected either way.

The rule to take from it is not "that element was fine". It is that **no rule
here is guaranteed to stay matched by one element**, so no keyframe on this page
may move a paint property. Three guardrails in
[`../../tests/test_dashboard_perf_guardrails.py`](../../tests/test_dashboard_perf_guardrails.py)
keep it that way: one fails if *any* referenced keyframe animates
`background-position`, `background-size`, `mask-position`, `filter`,
`box-shadow` or `clip-path`; one fails if a `.control-result` rule takes the old
keyframe back; and one fails if `prefers-reduced-motion` or
`dashboard-animation-off` stops reaching either ring.

**Benchmark the authenticated dashboard.** `--matrix writeframes` and the
`scenario="write-mode"` axis exist because every measurement in this project
before 2026-09-05 ran the read-only preview, where the control view has no
runtime editor at all.

## Every animation costs a style recalculation per frame

Chromium performs about **two thousand style recalculations per ten seconds**
while any animation on this page is running, and **six to twelve** when none is.
The cost of each pass grows with how many animations are running, not with how
many devices there are:

| running animations | style recalculation per 10 s |
|---:|---:|
| 0 (every keyframe stopped) | 4-92 ms |
| 12 (aggregated view) | ~700 ms |
| 26 (control, 4 devices) | 871 ms |
| 48 (devices, 4 devices) | 1696 ms |
| 66 (control, 12 devices) | 1809 ms |
| 144 (devices, 12 devices) | **2920 ms** |

The energy view is the control group: it has no flow animation and records five
recalculations with the animation setting on, exactly as many as with it off.
No treatment changed the paint count, so none of these is repainting -- the
entire cost is style. In Firefox the same constructions cost event-loop lag
instead (1-2 ms rising to 11-13 ms in the control view) and no frames.

**This is not a reason to switch the animation off**, for the reason the section
below gives: doing so makes per-snapshot work go *up*, in both engines, often by
a factor of two. It is a reason not to add more running animations, and a
budget: about 12-20 ms of main thread per ten seconds per animation.

It is also a reason to drive an animation from `element.animate()` with literal
keyframe values rather than from a keyframe that reads a custom property, which
is what the flow tiles now do: 28-40% less style-recalculation time in every
view at every CPU-throttling level measured. On this desktop that buys no
frames; on a main thread slowed sixteen-fold the aggregated view goes from
110.7 fps at a 13.9 ms frame p95 to 135.0 at 7.0. **Decide this kind of question
with `cpu_throttle`, not on the development machine** -- the whole reason it was
declined twice before is that a desktop with headroom cannot see it.

Two conditions make that trade safe, and both hold here. The renderer must
already hold the values in JavaScript, or `element.animate()` becomes a second
source of truth -- the tile renderer reads speed, direction, period and
appearance out of the stylesheet with `getComputedStyle`, so it does. And the
element must not be recreated on every snapshot, or per-frame style cost is
merely traded for per-snapshot script cost: that is why the control-stage result
rings, which the panel rebuilds twice a second, were measured and left on CSS.

Two things it is *not*: it is not the `var()` in the tile keyframes -- replacing
that with a constant changes nothing -- and it is not inherent to the tile
renderer, whose own lab page records five recalculations for the same
construction. Whether any of these animations reaches the compositor is
**unknown**; the experiment that would answer it does not work, and
[`../../reports/dashboard-perf/final-dashboard-performance-audit.md`](../../reports/dashboard-perf/final-dashboard-performance-audit.md)
§13 says why.

## What the page costs when nothing is happening

Nothing. With the snapshot feed silent, or delivering data whose timestamp has
not changed, the dashboard measures **0 ms of attributed main-thread work and 0
DOM mutations** over ten seconds in Firefox. There is no idle loop and no timer
grinding away; every cost is paid per *rendered* snapshot.

That is worth knowing before optimising anything here, and it is why
`animation_mode` is a smaller lever than it looks: turning the animation off
does not remove a standing cost, because there isn't one. Measured, it made the
largest per-snapshot cost 40-54% *more* expensive, because that cost was a
layout flush and an animation keeps the style tree from accumulating work
between flushes. Fixing the flush (above) dissolved that.

`scripts/dashboard_profile/` is the harness that can say this. It charges
main-thread time to the callback that spent it -- listeners, timers, animation
frames, observers, and optionally the layout-forcing reads themselves -- and it
adds the axes the frame-rate harness has no way to express: a feed that is
silent, a dashboard in the background, and a second page beside it. It is
engine-neutral by construction, which is the point: the slowdown was reported
on Firefox on macOS, and
[`../../reports/dashboard-perf/firefox-macos-investigation.md`](../../reports/dashboard-perf/firefox-macos-investigation.md)
contains no macOS measurement. That directory's README is the instruction for
taking one.

## Views that are not on screen

The live snapshot path renders only `state.flowView`, and a guardrail pins it.
There used to be a second way in: `loadAuthStatus()` and `loadRuntimeState()`
call `renderControlExplain()` directly, and one of them runs on a sixty-second
timer whatever is on screen -- so once a minute the control panel was rebuilt
behind whichever view the user was actually looking at. Measured at one rebuild
per minute costing up to 19 ms, and it built 3606 of the aggregated view's 4065
nodes at twelve devices.

`renderControlExplain` now returns immediately when its container is off screen;
`setFlowView` renders the view it switches to, so nothing is lost. The rebuild
costs 0.0-0.1 ms and a dashboard whose owner never opens the control view has a
**469-node document instead of 4065**.

Carrying those nodes was never the cost, and that was measured before the change
was made: emptying the subtree at runtime moved the per-snapshot cost up in two
cases and down in two. A `[hidden]` subtree is not laid out, not painted and not
walked. What cost was the rebuild.

The same shape of question applies to any panel built from something other than
the snapshot. `renderRuntimeEditorMount()` was rebuilding the whole runtime
editor twice a second from `state.runtime` and `state.auth`, neither of which a
snapshot touches -- byte-identical markup on every write, 16.5 ms per five
snapshots at twelve devices. It now compares the string it generated against the
one it generated last time. Compare **generated strings, never the element's own
`innerHTML`**: reading that back gives the DOM's re-serialisation of itself, and
`<input ... checked>` returns as `checked=""`, so that comparison can never be
equal.

## Several tabs at once

Both engines degrade steeply with tab count, and this is unfixed:

| Tabs | Firefox | Chromium |
|---|---:|---:|
| 1 | 4.7 fps | 18.1 fps |
| 2 | 2.2 fps | 9.2 fps |
| 5 | 1.0 fps | 3.2 fps |
| 10 | 0.4-1.1 fps per tab | 1.5 fps |

One constraint is structural rather than a defect: over HTTP/1.1 a browser
allows about six connections per host, and an open SSE stream holds one for its
lifetime. With no server-side cap, ten tabs each opening a stream exhaust the
pool and the later tabs cannot load the page at all -- measured, they never
finish navigating. `MAX_SSE_CONNECTIONS_PER_IP` prevents that by pushing the
extra tabs onto polling, which is why the benchmark models it.
