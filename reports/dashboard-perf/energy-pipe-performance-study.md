# Energy pipe performance study

*What is the simplest pipe/token construction that gives the dashboard the most
convincing energy-flow visualisation for the least rendering cost, given that
the HTML/CSS compositor architecture stays?*

This is a focused follow-up to
[`energy-flow-visualization-study.md`](energy-flow-visualization-study.md),
which settled the renderer question: HTML/CSS moved by the compositor, and on
real hardware every candidate technology reached the display's refresh ceiling.
Nothing here reopens that. The question left over is one level down — inside the
chosen architecture, how should the pipe and its moving token actually be built?

Everything below was measured on this project's Linux host (NVIDIA GTX 1660 Ti,
144 Hz display) on 2026-09-04. There is no macOS machine; no claim is made about
one.

---

## 1. Executive summary

Fourteen ways of building the moving energy token were measured against each
other — the nine the brief asked for, four designed for this study, and a
directional variant of the control — across 400-odd cases, both engines, headed
on a real GPU, one at a time.

**At the size a dashboard actually draws, none of them is distinguishable from
any other.** Twelve devices is 108 animated layers; every construction runs at
the display's 144 Hz ceiling in Firefox and Chromium, and the spread across all
fourteen (140.2–144.0 in Chromium) is smaller than the spread between two runs
of the same case (up to 3.53). The current capsule is not merely adequate. It is
tied with everything built to beat it.

So the study's useful output is not a faster pipe. It is three things:

**A licence.** The artwork is free. A sixteen-fold texture, a physically larger
composited layer, a more complicated tile and a gaussian halo are all inside the
noise in both engines at every size. The trace says why: across **108 traced
cases, `Paint` was zero in all of them** and `RasterTask` was zero in 107 — the
one exception recorded ten tasks totalling 2.6 ms over eight seconds, which is
not per-frame and did not move the frame rate. The layer is rasterised once and
thereafter only moved. How the flow *looks* is therefore not a performance decision, and can be
chosen on how it looks.

**Two guardrails, both on the obvious path.** The obvious way to add a glow is
`filter: drop-shadow()` on the moving layer; it costs Chromium 71 % of its frame
rate at forty-eight pipes and 86 % at ninety-six, while the main thread sits
idle — a filtered layer cannot be "rasterise once, then only move". The obvious
way to add more motion is a token per position; Chromium is flat to about 288
animated layers and then falls roughly inversely with their number, which four
tokens per segment reaches at eight devices. Meanwhile *painted* elements are
free: the candidate that paints 1728 elements measured faster than the control
that paints 576. Complexity belongs inside the layer, never in more of them.

**A free visual improvement.** Two things the dashboard cannot do today cost
nothing: direction that survives a still frame (an asymmetric token, mirrored
for reversed pipes) and a halo (a gaussian baked into the tile). The
recommendation is candidate **L, `comet`**, and it is a design decision rather
than a performance one — this report supplies the evidence that it is free, not
the authority to make it.

Two negative results are worth as much as the positive ones. The rule inherited
from the previous investigation — nothing in the tile layer may carry a filter —
is the first inherited claim in this series of studies that survived re-testing
on real hardware; it is true, for a different reason than was assumed. And the
one look-preserving speed-up found here (literal keyframes through the Web
Animations API, worth about 2 ms of main thread per frame and 31 % at 192 pipes)
**is not recommended**: at realistic size the gain is inside the noise, and it
would buy that invisible gain by putting a second source of truth for motion
next to the stylesheet the renderer currently reads.

---

## 2. Candidates

Nine constructions, all using the same mechanism the production renderer
already uses: an HTML layer moved by a CSS `transform` the compositor can carry.
What differs is how the pipe and the moving token are painted.

| | id | construction | animated elements per segment | painted elements per segment |
|---|---|---|---|---|
| **A** | `capsule` | Production control. One moved layer carrying a tiled rounded-rect `data:` URI. | 1 | 2 |
| **B** | `rect` | The cheapest thing that can work: a hard-stop `linear-gradient`, square ends, no image at all. | 1 | 2 |
| **C** | `radius-el` | The same moved layer, but the tokens are real `div`s with `border-radius` instead of one background image. | 1 | 1 + ⌈run/period⌉ |
| **D** | `gradient-capsule` | Rounded ends without an image: two pixel-exact `radial-gradient` caps plus a `linear-gradient` body, three background layers. | 1 | 2 |
| **E** | `repeating` | One `repeating-linear-gradient` instead of a sized, repeated tile. | 1 | 2 |
| **F** | `tokens` | *N* separately animated capsules per segment, staggered by `animation-delay`. *N* ∈ {1, 2, 4, 8, 16} is the axis. | *N* | 1 + *N* |
| **G** | `core` | The pipe is painted once and never animated; only a short bright core travels along it. | 1 | 2 |
| **H** | `pulse` | Static pipe with a wide soft brightness travelling along it. | 1 | 2 |
| **I** | `minimal` | The lower bound: a thin static pipe and one small dot per segment. | 1 | 2 |

Every candidate paints the same static pipe underneath, so the substrate cannot
bias the comparison; G, H and I differ from the others in that their *animated*
part no longer covers the whole pipe.

Held constant across all of them: geometry, segment positions, viewport, power
values, animation phase, travel speed in px/s, direction, device count, browser,
benchmark duration, page structure, colour palette, magnitude mapping, and the
`transform`-on-a-promoted-layer mechanism. The magnitude mapping is not
reimplemented from memory — `pipe_verify.mjs` reads `FLOW_RIBBON_*` and
`FLOW_SCALE_LADDER` out of `dashboard/static/app.js` and fails if the study has
drifted from it.

---

## 3. Visual comparison

The gallery (`scripts/flow_pipe_study/gallery.html`) shows all fourteen
constructions animating against the same scene, with controls for power,
automatic sweep, animation on/off, direction, device count, scenario, segment
length and glow.

These are judgements, made from the pictures, written before the phase-two
numbers were read. Where a judgement is a measurement instead, it says so.

### The nine the study was asked for

| | reading |
|---|---|
| **A** `capsule` | The reference. Rounded ends read as deliberate rather than cut off. With a halo it is a clear improvement on what ships today; without one it is flat. Direction is invisible in a still frame. |
| **B** `rect` | Square ends read as blunt at 15 px, like brickwork rather than energy. Indistinguishable from A at 4 px. Cannot carry a baked halo — see below. |
| **C** `radius-el` | Visually identical to A at every thickness. The construction is invisible to the eye, which is the finding: the `data:` URI buys nothing that `border-radius` does not. |
| **D** `gradient-capsule` | Also identical to A, at the cost of three background layers and pixel arithmetic in two places. |
| **E** `repeating` | Identical to B. |
| **F** `tokens` | Distinct beads; the most obviously "quantised" look. Legible and pleasant at N=2–4. At N=8 the tokens had to be shrunk or they merge into an unbroken bar (§11). |
| **G** `core` | A dim tube with one bright core travelling inside. Elegant and calm. Its weakness is a real one: as power rises the pipe grows and the core does not, so the animated fraction *shrinks* exactly when the flow matters most. |
| **H** `pulse` | Too soft. It reads as a gradient on a tube rather than as something moving, and direction is unreadable both in motion and at rest. The weakest of the set. |
| **I** `minimal` | Beads on a wire. Surprisingly clear at close range and the first to disappear at eight devices. |

### The four designed for this study, and the arrow

| | reading |
|---|---|
| **J** `arrow` | A's tile with a pointed nose. Direction is readable in a still frame, which nothing in the current dashboard achieves. The tile is mirrored for reversed pipes — verified in the gallery's reversed state, where the noses correctly point backwards. |
| **K** `plasma` | A bright core inside a lit sheath, with a second slower layer behind. The most vivid of all at density: at eight devices it is the only construction that still reads as *energy* rather than as decoration. It costs a second animated layer per segment. |
| **L** `comet` | A bright head with a fading tail. Direction survives a still frame without needing an arrowhead, and with a gaussian halo the light concentrates at the head, which looks like motion even stopped. The best-looking of the set to my eye. |
| **M** `particles` | Many small quanta in lanes, with density carrying magnitude alongside thickness. Convincing at close range. **It fails at eight devices**: a dot needs a few pixels to exist at all, and at that scale the stream washes out. The second magnitude channel is real but only above a certain size. |
| **N** `wave` | A smooth brightness wave along a continuous tube. The calmest and most technical look, and the only one with no discrete token. Direction is unreadable at rest. |

### Glow

Seven implementations were built and compared (§5 for what they cost).
Visually there are three outcomes and one exclusion:

- **`texture`** — a halo built from nested capsules with a falling alpha. At
  four steps it read as concentric rings; at seven it is acceptable but still
  slightly banded, and the halo is capsule-shaped whatever the token is.
- **`blur`** — a real `feGaussianBlur` inside the tile, applied to the token's
  *own* shape. Clearly the best-looking: smooth, and the light lands where the
  token is bright. It costs nothing per frame because it is resolved when the
  image is decoded, not when the layer is moved.
- **`static`**, **`filter`**, **`layered`**, **`blend`** — all produce a glow;
  see §5 for which of them is free.
- **B, D and E cannot have a baked halo at all.** A CSS gradient has no room
  outside the token to put one, so the glow modes that live in the artwork are
  unavailable to exactly the three candidates whose whole argument was avoiding
  the `data:` URI. That is a genuine cost of those constructions and it is not
  visible in any frame rate.

### The same judgements as a number

Each candidate photographed with the animation paused, against the control at
the same phase. Headless, because this is a comparison of pixels and not of
speed. "ink" is the fraction of lit pixels relative to the control, so it says
whether a construction puts more or less light on the screen.

| | differing pixels | ink vs control |
|---|---:|---:|
| C `radius-el` | 0.1 % | ×0.99 |
| D `gradient-capsule` | 0.2 % | ×1.00 |
| B `rect` | 0.4 % | ×1.05 |
| E `repeating` | 0.4 % | ×1.04 |
| J `arrow` | 1.5 % | ×0.73 |
| F `tokens ×8` | 2.8 % | ×1.08 |
| L `comet` | 3.7 % | ×0.66 |
| F `tokens ×1` | 3.8 % | ×0.20 |
| M `particles` | 4.4 % | ×0.50 |
| N `wave` | 5.0 % | ×1.18 |
| I `minimal` | 5.3 % | ×0.14 |
| G `core` / H `pulse` | 5.5 % | ×0.84 / ×0.90 |
| K `plasma` | 6.2 % | ×1.32 |

This confirms the eye rather than replacing it. C and D differ from the control
by a tenth of a percent of pixels — they are the same picture drawn three
different ways, which is exactly what §10 scores them on. At the other end,
`plasma` is the only candidate that puts *more* light on the screen than the
control, and `minimal` puts on a seventh of it.

One thing the table exposed that was not designed for: **170 W and 690 W produce
byte-identical frames** in every candidate. That is the magnitude ladder working
as intended — the scale snaps to a rung taken from the system's own output, so a
scene where every flow is 170 W and one where every flow is 690 W are both "all
flows at about two thirds of full scale". Magnitude here is relative to the
installation, never absolute, and a screenshot of one flow cannot be read as a
wattage. That is a property of the shipped design, not of any candidate.

### Where the constructions differ under stress

- **Corners.** Every tiled construction keeps its pattern continuous across a
  corner, because the phase carries over. The traversing-token candidates (F–I)
  do not: their tokens vanish at the corner and reappear on the next segment.
- **Very short segments.** A segment shorter than one dash period shows at most
  one token; the tiled constructions degrade gracefully, the traversing ones
  flicker.
- **Long segments.** No construction has trouble.
- **Density.** At eight devices, K remains vivid, A/J/L stay legible, M washes
  out, and I nearly disappears.

---

## 4. Benchmark methodology

### The tooling

`scripts/flow_pipe_study/` holds everything study-specific:

| file | what it does |
|---|---|
| `pipe_study.js` | the nine constructions and the shared scene |
| `pipe.css` | the shared, candidate-neutral stylesheet |
| `index.html` + `bench.js` | one candidate on one scene, exposing `window.__lab` |
| `gallery.html` + `gallery.js` | the visual gallery, with the study's controls |
| `pipe_verify.mjs` | the correctness gate — run before believing any number |
| `pipe_bench.py` | the scenario matrices |
| `pipe_layers.mjs` | Chromium compositor accounting (layers, area, paint counts) |
| `pipe_fidelity.mjs` | still-frame appearance distance from the control |
| `pipe_shots.mjs` | the screenshots |
| `pipe_report.py` | the tables quoted below |

The Playwright driver, the load gate and the environment record are reused from
`scripts/flow_lab_bench.py` rather than copied.

### The rasterisation path

Every primary measurement is **headed on the real display** (`--gpu headed`,
`DISPLAY=:0`), which is the only configuration in which both engines are known
to composite on the GPU. Headless Chromium defaults to ANGLE/SwiftShader, and
headless Firefox hands WebGL the NVIDIA device while compositing the page on the
CPU — so `WEBGL_debug_renderer_info` can prove a run was software but never that
it was hardware. Every report records `environment.gpu`, the observed
`rasterisation.renderer` per case, and the machine's load average.

Headless runs appear in this report only for the correctness gate and the
still-frame appearance comparison, both of which are about pixels rather than
speed, and both are labelled as headless where they are quoted.

### The load gate

This host is a live desktop with eight cores. `--max-load 2.0` makes every case
wait for a quiet machine before it starts, and each case records the load it
actually ran under. The whole series ran serially; no two benchmarks ever ran at
the same time.

### What is measured

Per case: frames per second, frame-time p95, event-loop lag p95 and max, long
tasks and their total duration, DOM mutations, the number of animated elements,
the number of painted elements, `document.getAnimations().length`, stage pixel
area, the rasteriser string, and the load average. On Chromium a DevTools trace
additionally counts `Paint`, `RasterTask`, `UpdateLayerTree`, `Commit`,
`DrawFrame`, `Layout` and `UpdateLayoutTree` events and sums their durations.

Firefox has no equivalent of the Chromium trace or of the compositor layer
accounting. Those cells are reported as unavailable. They are not inferred.

### Why frames per second is not the headline

At a realistic twelve pipes every one of the nine candidates runs at the
display's 144 Hz ceiling in both engines. A comparison at that size measures the
monitor. The scenes that discriminate are the stress ones (48 and 96 pipes) and
the counters that keep moving while the frame rate does not.

---

## 5. Raw benchmark results

Every number below is headed on the real display and GPU, one case at a time,
each waiting for a one-minute load average at or below 2.0. The raw files are
`scripts/flow_pipe_study/pipe_report.py`
regenerates these tables from them. 289 cases ran in phase one and none errored.

### What "within noise" means here

Measured, not assumed: the whole candidate matrix was run twice, and the spread
between two runs of the same case is

| | median | worst |
|---|---:|---:|
| Firefox | 0.23 fps | 0.74 fps |
| Chromium | 0.42 fps | 3.53 fps |

Two candidates differing by less than that are reported as equal below. No
smaller difference is claimed anywhere in this report.

### The headline: fourteen constructions, twelve pipes

Median of two runs, animation on, no glow:

| | Firefox | Chromium | Chromium lag p95 |
|---|---:|---:|---:|
| A `capsule` | 143.5 | 140.2 | 1.6 ms |
| B `rect` | 143.4 | 142.1 | 1.9 ms |
| C `radius-el` | 143.4 | 142.5 | 1.5 ms |
| D `gradient-capsule` | 143.5 | 141.0 | 1.4 ms |
| E `repeating` | 143.4 | 140.5 | 1.8 ms |
| F `tokens ×4` | 143.5 | 144.0 | 1.6 ms |
| G `core` | 143.5 | 142.5 | 2.2 ms |
| H `pulse` | 143.3 | 140.6 | 1.7 ms |
| I `minimal` | 143.3 | 141.1 | 2.1 ms |
| J `arrow` | 143.4 | 142.5 | 2.0 ms |
| K `plasma` | 143.5 | 142.5 | 1.1 ms |
| L `comet` | 143.3 | 140.5 | 2.1 ms |
| M `particles` | 143.4 | 141.1 | 2.2 ms |
| N `wave` | 143.6 | 142.4 | 1.9 ms |

Firefox spans 0.3 fps across all fourteen; Chromium spans 3.8, which is its own
run-to-run worst case. **At the size a dashboard actually draws, no construction
is distinguishable from any other in either engine.** Everything that follows is
about what happens past that point, and about what things cost that a frame rate
cannot see.

With the animation switched off, Chromium's lag p95 drops to 0.3–0.5 ms for
every candidate. That difference — roughly 1.5 ms of main thread per frame — is
the entire measurable cost of animating at this size, and §9 shows where it
comes from.

### What the `var()` keyframe costs

Chromium's trace records one `UpdateLayoutTree` per frame for a running
animation and none for a paused one. §9 explains why that does **not** prove the
animation is off the compositor — a second instrument contradicts it. What is
not in doubt is the difference between the two ways of writing the same motion.
Expressing it with literal keyframe values through the Web Animations API,
instead of keyframes that read a custom property, halves the time that step
takes:

| Chromium, headed | `var()` keyframes | WAAPI literal | change |
|---|---:|---:|---:|
| 12 pipes, style recalculation time | 1246 ms / 8 s | **667 ms** | −46 % |
| 48 pipes | 1874 ms | **1294 ms** | −31 % |
| 96 pipes | 3103 ms | **2064 ms** | −33 % |
| 48 pipes, fps | 137.0 | **143.1** | +4 % |
| 96 pipes, fps | 139.3 | **143.1** | +3 % |
| 12 devices, fps / lag p95 | 140.5 / 4.5 ms | **144.0 / 2.4 ms** | +2 % / −47 % |
| 192 pipes, fps | 68.6 | **90.0** | **+31 %** |

Firefox is neutral: 143.3–143.6 in both, lag unchanged.

Production uses the `var()` construction. The gain is one-sided — Chromium
improves at every size, Firefox loses nothing — and at the size a dashboard
draws it is nonetheless inside the run-to-run noise. §12 explains why it is not
recommended regardless.

---

## 6. Scaling results

The scenarios the study was asked for — one flow, aggregate at 2/4/8/12/24/48,
devices at 2/4/8/12 — and then past them, because none of them found a limit.

### Firefox does not scale, in the sense that there is nothing to scale

Thirty-two cases, five constructions, two to 192 pipes: **143.1 to 143.8 fps**.
Every single one. The devices view is the same, to twelve devices. The cost is
visible only in event-loop lag p95, and only as 1 ms → 2 ms between 6 and 576
animated layers.

There is no Firefox scaling curve to plot. That is the result.

### Chromium has a knee, and it is not where the pipes are

| Chromium, headed | animated layers | fps | frame p95 |
|---|---:|---:|---:|
| `capsule`, 48 pipes | 144 | 139.8 | 7.0 |
| `capsule`, 96 pipes | 288 | 143.0 | 7.0 |
| `capsule`, **192 pipes** | **576** | **68.1** | 20.9 |
| `tokens ×4`, 12 pipes | 144 | 144.0 | 7.0 |
| `tokens ×4`, 24 pipes | 288 | 130.0 | 13.9 |
| `tokens ×4`, **48 pipes** | **576** | **69.4** | 20.8 |

576 animated layers costs the same whether they are reached with 192 one-layer
pipes or 48 four-token pipes — 68.1 against 69.4 fps. Below 144 layers nothing
is ever measurable. The dashboard's own worst case, twelve devices, is 108.

Between those two points the picture is not clean: 288 animated layers measured
143.8, 143.0, 130.0 and 80.8 fps in four different configurations. Layer count
alone therefore does not predict the cost, and the most likely missing variable
is the total *area* of the animated layers rather than their number — the token
in the 80.8 case is much larger relative to its cell than in the 143.8 case.
That is a hypothesis; §5's compositor accounting is what can test it, and §13
records it as open either way.

### Where it actually stops

Pushed past every scenario the study was asked for, both engines do have a
limit, and they are about a factor of eight apart:

| animated layers | pipes | Chromium fps | Firefox fps |
|---:|---:|---:|---:|
| 288 | 96 | 141.0 | 143.3 |
| 576 | 192 | 62.3 | 143.5 |
| 960 | 320 | 46.7 | 143.6 |
| 1440 | 480 | 32.1 | 143.4 |
| 1800 | 600 | 24.6 | 142.6 |
| 2304 | 96 × 8 tokens | 20.5 | 143.4 |
| 4608 | 192 × 8 tokens | 10.3 | **100.9** |

Beyond its knee Chromium falls roughly inversely with layer count — halving the
frame rate for every doubling — which is the signature of a per-layer per-frame
cost rather than a threshold. Firefox holds the full refresh rate to 2304
animated layers and only bends at 4608.

### What this means for a dashboard

Every construction is free at every size a real installation reaches, in both
engines, on a GPU. The first configuration that is not free needs 576 animated
layers, which is 192 pipes — sixty-four devices — or a construction that puts
four separate moving tokens on every pipe segment. Only candidate F does the
latter, and only above 24 pipes.

---

---

## 7. Painted-area findings

Three axes, each one verified pixel-identical to the baseline before it was
measured (§11), so what changed is the amount of work and never the picture:

- `tile=4` / `tile=16` — the `data:` URI encodes 4 or 16 periods instead of 1,
  so the source image is 4× or 16× wider and the tile is scaled down to the
  same size on screen.
- `pad=24` / `pad=72` — the moved layer is grown by 24 or 72 px on every side.
  The parent clips it, so the picture is unchanged and the composited layer is
  larger.
- `texture=rich` — a second, highlight shape inside every token, roughly
  doubling the vector content of the tile.

Twelve pipes and forty-eight, headed on the GPU:

| case | Firefox fps | Chromium fps |
|---|---:|---:|
| baseline, 12 pipes | 143.4 | 143.5 |
| `tile=4` | 143.5 | 141.2 |
| `tile=16` | 143.4 | 143.9 |
| `pad=24` | 143.5 | 141.1 |
| `pad=72` | 143.4 | 141.0 |
| `texture=rich` | 143.2 | 141.2 |
| baseline, 48 pipes | 143.7 | 142.8 |
| `tile=16`, 48 pipes | 143.6 | 142.5 |
| `pad=72`, 48 pipes | 143.4 | 140.5 |

Firefox spans 0.5 fps across the whole table; Chromium spans 3.4 fps, and its
spread on repeated runs of the *same* case (§5) is of that order. **Nothing here
is distinguishable from noise in either engine.**

So the answer to "is a visually complicated background texture actually free
once the layer is compositor-translated?" is yes, and it is free along all three
axes that could have made it expensive: more vector content, a physically larger
source image, and a physically larger composited layer.

The trace says why, and it says it across the whole study rather than for these
nine cases alone. Over **108 traced cases** — every candidate, every glow mode,
every painted-area variant, at twelve, forty-eight and ninety-six pipes —
Chromium recorded:

| | cases with a non-zero count |
|---|---|
| `Paint` | **0 of 108** |
| `RasterTask` | **1 of 108** (ten tasks, 2.6 ms over 8 s, no effect on the frame rate) |
| `Layout` | 0 of 108 |

Nothing is repainted after the page settles. The layer is rasterised once and
from then on the compositor only moves it, which is why what is drawn on it
cannot cost anything per frame.

That is a licence, and it is the most useful thing this study produces. The
appearance of the token is not a performance decision. It can be chosen on how
it looks.

### The one cost a frame rate cannot see

Frames per second says nothing about memory, and at the refresh ceiling it is
the only place a difference could still hide. Chromium's compositor accounting,
twelve pipes:

| | composited layers | layer memory |
|---|---:|---:|
| baseline | 63 | 5.60 MB |
| `tile=16` — a 16× wider source image | 63 | **5.60 MB** |
| `pad=24` | 63 | 8.05 MB |
| `pad=72` | 63 | **14.84 MB** |
| `glow=texture` / `glow=blend` | 63 | 6.69 MB |
| `glow=layered` | 99 | 8.27 MB |
| `plasma` | 99 | 7.06 MB |
| `tokens ×8` | **315** | 5.31 MB |

The two axes separate cleanly. **A larger texture is free in memory too** — the
composited layer is sized by the element, not by the image, so sixteen times the
source artwork changes nothing. **A larger layer is not**: 72 px of transparent
padding on every side costs 9.2 MB for a picture that is pixel-identical.

That is the answer to the question §7 was set: a complicated texture really is
free, along every axis measured including this one. What is not free is making
the layer itself bigger — and the baked halo does exactly that, at 1.1 MB for
the whole scene, which is the price of the recommendation in §12 stated plainly.

### Where the halo lives decides whether it is free

The glow is the sharpest case of the same principle, and it was added to the
study because a glow is most of what makes a flow look like energy rather than
like paint. Seven implementations, same spread, same colour, same scene:

| Chromium, fps | 12 pipes | 48 pipes | 96 pipes |
|---|---:|---:|---:|
| no glow | 140.9 | 140.0 | 143.4 |
| `static` — `box-shadow` on the pipe that never moves | 140.1 | 141.9 | 141.1 |
| `texture` — nested shapes with a falling alpha, in the tile | 144.0 | 139.4 | 142.8 |
| **`blur` — a real `feGaussianBlur`, in the tile** | **143.8** | **142.5** | **142.4** |
| `filter` — `drop-shadow()` on the moving layer | 141.5 | **39.7** | **20.2** |
| `blend` — `mix-blend-mode: plus-lighter` | 140.1 | 103.5 | 50.8 |
| `layered` — a second, blurred moving layer | 141.6 | 82.1 | 41.9 |

Firefox measured 143.0–143.8 for **all seven** at all three sizes.

So the rule is not "no glow" and it is not "no blur". It is:

> **The glow has to be in the artwork, not in a live effect.** A halo baked into
> the tile — nested shapes or a gaussian — is free in both engines at every size
> measured, because it is resolved once when the image is decoded. The same
> halo produced by `filter: drop-shadow()` on the moving layer costs Chromium
> 71 % of its frame rate at forty-eight pipes and 86 % at ninety-six.

The trace says where that cost is *not*, which is as far as this harness can
see. With `filter`, Chromium's event-loop lag p95 **falls** to 0.2–1.2 ms and
its style-recalculation time falls with it — from 1989 ms to 584 ms at
forty-eight pipes — while frame-time p95 rises to 55 ms and the frame rate
collapses. Paint and raster counts stay at zero, exactly as in every other case.

So the main thread is idle and doing less work than the cheap variants, and the
frames still do not arrive. By elimination the cost is on the compositor or the
GPU. This study does not instrument either — `devtools.timeline` records the
renderer's main thread — so the mechanism below is an inference from a negative
result, not a measurement:

> A filter has to be applied wherever the layer is drawn, so a filtered layer
> cannot be "rasterise once, then only move". That is the one property this
> whole architecture rests on.

It fits every number here and it fits the shape of the collapse, but confirming
it needs a compositor-side trace that §13 records as not done.

This vindicates the rule inherited from
[`flow-rendering-investigation.md`](flow-rendering-investigation.md), which
said nothing in the tile layer may carry a filter. That rule was measured on a
software rasteriser and this study expected to overturn it, as it overturned
three others. It did not: the rule is real, it survives a real GPU, and the
reason is compositing rather than rasterisation speed. What is new is that the
*visual* goal the filter was reaching for is available for nothing by putting
the same blur inside the tile.

---

---

## 8. Token-count findings

This is where the candidates stop being equal.

Painted elements are free. Animated elements are not.

| Chromium, 96 pipes | animated | painted | fps |
|---|---:|---:|---:|
| `radius-el` — real `div`s instead of a background image | 288 | **1728** | 143.1 |
| `capsule` — the control | 288 | 576 | 125.6 |
| `tokens ×4` | **1152** | 1440 | **32.7** |

`radius-el` paints six times as many elements as the control and runs faster
than it. `tokens` paints barely more and collapses. The distinguishing variable
is how many elements are *animated*, which in this architecture means how many
compositor layers have to be moved and committed every frame.

The full sweep, Chromium, headed:

| animated layers | 12 pipes | 48 pipes |
|---:|---:|---:|
| 36–144 | 141.6–143.9 | 140.8 |
| 288 | 143.8 | **80.8** |
| 576 | **79.3** | 67.4 |
| 1152 | — | 40.5 |
| 2304 | — | 23.5 |

The same sweep in Firefox never leaves the ceiling: 2304 animated layers still
measured 142.8 fps. The cost surfaces there only as event-loop lag p95, which
rises roughly linearly — 1 ms at 144 layers, 5 ms at 2304 — and never as a
dropped frame. **The two engines have different limits and Chromium's is the
binding one.**

Note the row that does not fit a pure layer count: 288 layers is free at twelve
pipes and halves the frame rate at forty-eight. Layer count alone therefore does
not explain the knee, and this study does not claim it does — see §5 for what
the compositor accounting shows and §13 for what remains unexplained.

The practical rule is not in doubt, though:

> **One animated layer per pipe segment is the safe design. Any construction
> that multiplies that number has a knee inside the range a dense dashboard can
> reach, in Chromium, on a real GPU.**

Production today draws one layer per segment: 108 layers at twelve devices,
comfortably flat. Candidate F is the only one of the nine that breaks the rule,
and it breaks it by design.

---

---

## 9. Browser comparison

The two engines do not merely differ in degree here. They have different cost
models, and the study's practical limits all come from one of them.

### Firefox

Every case in this study, headed on the GPU, at every size measured — 2 to 192
pipes, 6 to 2304 animated layers, ten candidates, six painted-area variants,
eight power levels, ten tabs — landed between **143.1 and 144.0 fps** on a
144 Hz display. The only measurement that moves at all is event-loop lag p95,
and it moves from 1 ms to 5 ms across a sixteen-fold increase in animated
layers.

There is no Firefox result in this study other than "the display's refresh rate".

### Chromium

Chromium is the engine that produces a curve, and the curve is about animated
layers rather than pipes, painting or area:

| animated layers | fps (best/worst across configurations) |
|---:|---|
| ≤ 144 | 139.8 – 144.0 |
| 288 | 80.8 – 143.8 |
| 576 | 68.1 – 79.3 |
| 1152 | 32.7 – 40.5 |
| 2304 | 23.5 |

It is also the engine that degrades with tab count — 141 fps at one tab, 118 at
ten, where Firefox holds 144 — although that measurement is of this isolated
scene and says nothing directly about the dashboard's own multi-tab behaviour,
which is dominated by other things.

### What the trace says, and what it changes

Chromium's trace explains the shape of that curve, and it is not what the
architecture predicts. Over an eight-second window at twelve pipes:

| | animation on | animation off |
|---|---:|---:|
| `Paint` | **0** | 0 |
| `RasterTask` | **0** | 0 |
| `Layout` | 0 | 0 |
| `UpdateLayoutTree` (style recalculation) | **1152** | **0** |
| `Commit` | 1152 | 1154 |

Zero paints and zero raster tasks in eight seconds is the architecture working
exactly as designed: the layer is rasterised once, before the measurement
window, and then only moved. That is why §7 finds painted complexity free.

The remaining row is 1152 `UpdateLayoutTree` events — one per frame, about
1.1 ms each at twelve pipes and 1.8 ms at forty-eight — and exactly zero when
the animation is paused. So the animation produces them.

**A first reading of that was wrong, and a second instrument caught it.** The
obvious inference is "the animation is recalculating style every frame, so it is
not on the compositor", and the obvious suspect is production's own keyframe:

```css
@keyframes flowTileRight { to { transform: translate3d(var(--tile-step), 0, 0); } }
```

Chromium cannot composite an animation whose keyframe values are not static, and
a keyframe reading a custom property is not static. That story is tidy and this
study cannot support it. The compositor probe reads the same page through
`Performance.getMetrics`, and after two and a half seconds of running animation
it reports **five** style recalculations, not the several hundred that reading
requires. Two instruments on one page disagree by two orders of magnitude, so
the trace event is not counting what its name suggests: `UpdateLayoutTree` here
is the animation tick inside the frame lifecycle, which happens whether or not
the animation is composited.

What survives is narrower and better supported. Replacing the `var()` keyframes
with literal values through the Web Animations API leaves the *count* untouched
and roughly halves the *time* — 1246 ms → 667 ms per eight seconds at twelve
pipes — and lifts the frame rate at the knee by 31 %. The most economical
explanation is that resolving a custom property for every animated element on
every frame is itself the cost, independently of where the animation runs.

Whether these animations reach the compositor at all is therefore **not settled
by this study**. §13 names the experiment that would settle it and why it was
not run.

---

## 10. Decision matrix

### The rubric, stated before the results

Scoring after the fact is how a study talks itself into the answer it already
liked. So the scale is fixed here, in advance of the numbers.

| criterion | weight | 10 means | 5 means | 0 means |
|---|---:|---|---|---|
| Visual quality | 25% | crisper than production, no artefact at any thickness, corner or segment length | as good as production | visibly worse, or breaks at some magnitude |
| Information clarity | 15% | direction, magnitude and activity all readable, including in a still frame | as readable as production | direction or magnitude becomes ambiguous |
| Firefox performance | 15% | best measured, and no worse at 96 pipes | within noise of production | measurably worse at a realistic size |
| Chromium performance | 15% | best measured, and no worse at 96 pipes | within noise of production | measurably worse at a realistic size |
| Scaling | 10% | cost per pipe flat to 96 | same slope as production | superlinear before 48 pipes |
| Implementation simplicity | 10% | fewer moving parts than production | the same | needs new machinery |
| Maintainability | 5% | one obvious place to change appearance | the same as production | appearance spread across several mechanisms |
| CSP / dependency safety | 5% | no new dependency, no inline style, no new CSP need | the same as production | needs `unsafe-inline` or a new origin |

Two rules follow from the weights, and they are the point of them: performance
is 40% and visual quality plus clarity is also 40%, so a candidate cannot win on
frame rate alone — and a visually better candidate wins if its cost is within
noise. "Within noise" is defined below in §5 from the observed run-to-run
spread, not chosen per candidate.

### The scores

Performance and scaling are scored against the rubric's own definitions, which
means that a candidate measured as indistinguishable from production scores 5
rather than 10. Twelve of the fourteen are exactly that, so those columns
mostly do not discriminate — which is itself the finding, not a defect of the
scoring.

| | vis 25% | clar 15% | FF 15% | Cr 15% | scal 10% | simp 10% | maint 5% | CSP 5% | **total** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **L** `comet` | 9 | 8 | 5 | 5 | 5 | 4 | 7 | 5 | **6.45** |
| **J** `arrow` | 8 | 8 | 5 | 5 | 5 | 4 | 7 | 5 | **6.20** |
| **M** `particles` | 7 | 7 | 5 | 5 | 5 | 3 | 6 | 5 | **5.65** |
| **A** `capsule` (control) | 7 | 4 | 5 | 5 | 5 | 5 | 7 | 5 | **5.45** |
| **C** `radius-el` | 7 | 4 | 5 | 5 | 5 | 4 | 5 | 6 | **5.30** |
| **N** `wave` | 6 | 3 | 5 | 5 | 5 | 6 | 8 | 6 | **5.25** |
| **B** `rect` | 4 | 4 | 5 | 5 | 5 | 7 | 8 | 6 | **5.00** |
| **E** `repeating` | 4 | 4 | 5 | 5 | 5 | 7 | 8 | 6 | **5.00** |
| **D** `gradient-capsule` | 6 | 4 | 5 | 5 | 5 | 3 | 4 | 6 | **4.90** |
| **K** `plasma` | 9 | 5 | 5 | 2 | 2 | 2 | 3 | 5 | **4.85** |
| **G** `core` | 5 | 4 | 5 | 5 | 5 | 4 | 5 | 5 | **4.75** |
| **I** `minimal` | 4 | 4 | 5 | 5 | 5 | 4 | 5 | 5 | **4.50** |
| **F** `tokens ×4` | 6 | 5 | 5 | 1 | 1 | 3 | 4 | 5 | **4.00** |
| **H** `pulse` | 2 | 2 | 5 | 5 | 5 | 4 | 5 | 5 | **3.70** |

Where the scores come from, for the ones that are not 5:

- **Visual** is a judgement, made from the gallery before the phase-two numbers
  were read, and it is the only column with real spread. It is one person's eye;
  §13 says so plainly.
- **Information clarity** separates the four constructions that say something a
  still frame can read — J and L show direction, M shows magnitude twice — from
  the ten that do not.
- **Chromium performance** is low for the only two candidates that need more
  than one animated layer per segment, and both were measured at the dashboard's
  own maximum rather than inferred from the layer-count law:

  | twelve devices, Chromium | animated layers | fps |
  |---|---:|---:|
  | one layer per segment (control) | 108 | 138.4 |
  | **K** `plasma`, two layers | 216 | **99.4** |
  | **F** `tokens ×4` | 432 | **88.1** |

  Firefox measured 143.5 for all three. Both drops are far outside the 3.53 fps
  noise band, so both score 2 and 1 respectively against a rubric whose 0 is
  "measurably worse at a realistic size". Neither is *broken* — 99 fps is still
  above any 60 Hz display — but on this hardware they are the only two
  constructions where the difference is real.
- **Scaling** follows the same measurement.
- **Implementation simplicity** counts against the interesting ones honestly: J
  and L need the tile mirrored for reversed pipes, M needs seeded jitter and
  wrap-around ghosts, K needs a second layer and a sheath size. B, E and N are
  the simplest things that work.
- **CSP** is 5 everywhere and 6 for the four that avoid the `data:` URI
  altogether. Nothing here needs a new origin, an inline `<style>`, or a
  dependency.

---

## 11. Rejected approaches

### Rejected before this study, and not reopened

Canvas 2D, OffscreenCanvas, WebGL, visualisation libraries and frontend
frameworks were settled by
[`energy-flow-visualization-study.md`](energy-flow-visualization-study.md) and
are out of scope by instruction. Nothing measured here contradicts that
finding, so none of them is revisited.

### Constructions that failed before they could be benchmarked

These are the ones worth recording, because each of them benchmarks *well*.
A construction that renders nothing, or that renders a picture which happens
not to change, produces excellent numbers. `pipe_verify.mjs` exists to catch
exactly that, and it caught all four of these before a single frame rate was
believed.

| what was built | what the gate saw | what was actually wrong |
|---|---|---|
| C, tokens as `border-radius` divs inside the moved layer | ink normal, **zero** moving pixels over 420 ms | The moved layer is absolutely positioned with only `left`/`right` set, so its height was 0 and every chip at `height: 100%` was invisible. The static pipe underneath supplied all the ink, which is why "does it render" passed. |
| F at eight tokens per segment | ink much *higher* than the control, zero moving pixels | Eight full-length tokens overlap into one unbroken bar. A saturated bar looks identical however fast it moves. Token length now scales with *N*. |
| The padded-layer probe, first attempt | 1.5–2.8 % of pixels differed from the unpadded control | `background-size: <period>px 100%` resolves against the *padded* box, so padding stretched the tile. |
| The padded-layer probe, second attempt | still ~1.5 % differing | The layer's leading edge moves out by `pad`, which shifts the tile origin by `pad mod period`. The probe was measuring a different picture, not a bigger layer. |

The last two matter beyond their own bug: the whole painted-area investigation
in §7 rests on the claim that the `tile` and `pad` axes change only the amount
of texture and layer, never the image. That claim is now a measurement —
0.00 % differing pixels in both engines — rather than an assumption.

---

## 12. Production recommendation

### On performance grounds: change nothing

There is nothing to fix. At the size a dashboard draws — twelve devices, 108
animated layers — every one of the fourteen constructions runs at the display's
refresh ceiling in both engines on a GPU, and the differences between them are
smaller than the same case measured twice. The current capsule is not merely
adequate; it is indistinguishable from every alternative including the ones
built to beat it.

The one look-preserving change this study found is not worth making. Expressing
the keyframes with literal values through the Web Animations API gives Chromium
back about 2 ms of main thread per frame and 31 % more frames at 192 pipes. But
at twelve devices the frame-rate difference is inside the run-to-run noise, the
2 ms is on a page already at the refresh ceiling, and the cost is real: it
replaces a declarative CSS rule with imperative `element.animate()` calls and
weakens the property that makes the current renderer maintainable — that it
reads its speed, direction and appearance back out of the stylesheet with
`getComputedStyle`. A second source of truth for motion, in exchange for
something nobody can see, is a bad trade. It is recorded in §5 and §13 in case a
future change makes the headroom matter.

### On visual grounds: there is a free improvement, and it is worth taking

This is where the study has something to offer, and it exists because of the
§7 result rather than in spite of it:

> The artwork is free. Sixteen times the texture, a physically larger layer, a
> more complicated tile, a gaussian halo — all inside the noise, in both
> engines, at every size. Zero paints and zero raster tasks in eight seconds.

So the appearance of the flow is not a performance decision, and two things the
current dashboard cannot do are available at no cost:

1. **Direction that survives a still frame.** Today the flow says which way it
   goes only while it is moving; a stopped, reduced-motion or screenshotted
   dashboard says nothing. An asymmetric token — `comet`'s fading tail or
   `arrow`'s nose — fixes that, and the tile is mirrored for reversed pipes.
2. **A halo.** The pipes read flat today. A gaussian blur *inside the tile*
   costs nothing because it is resolved when the image is decoded.

Both are the same one-layer-per-segment mechanism that ships today. Neither
changes the geometry, the magnitude mapping, the speed buckets, or how the
renderer reads its appearance out of the CSS.

**This has not been implemented.** The evidence for it is in this report; the
decision is a design decision, not a performance one, and it belongs to whoever
owns the dashboard's appearance. If it is taken, §14 of the study brief applies:
contract tests first, then the smallest change, then re-measure.

### Two guardrails, which are the durable part

These matter more than the recommendation, because both traps are on the obvious
path and neither is visible without measuring:

- **Never put a filter on the moving layer.** `filter: drop-shadow()` is the
  obvious way to add a glow. It costs Chromium 71 % of its frame rate at
  forty-eight pipes and 86 % at ninety-six, while the main thread sits idle.
  Bake the halo into the artwork instead. `mix-blend-mode` is cheaper but not
  free: 26 % and 65 %.
- **Never multiply the animated layers.** One per pipe segment is the design.
  Chromium is flat to about 288 animated layers and then falls roughly inversely
  with their number. Measured at twelve devices: 138.4 fps with one layer per
  segment, 99.4 with two, 88.1 with four tokens. Doubling the layers is enough
  to lose a quarter of the frame rate at the size the dashboard already has.
  Painted elements are free — six times as many measured *faster* — so
  complexity belongs inside the layer, never in more of them.

```text
RECOMMENDATION: L — comet, with the halo baked into the tile
PIPE TECHNOLOGY: one compositor-moved HTML layer per segment, painted with a
                 tiled SVG data: URI whose artwork carries an feGaussianBlur halo
VISUAL SCORE: 9
PERFORMANCE SCORE: 5   (indistinguishable from production in both engines at
                        every size a dashboard reaches)
SCALING SCORE: 5
OVERALL: 6.45
PRODUCTION CHANGE: YES — on visual grounds only, and not implemented here.
                   NO change is warranted for performance.
CONFIDENCE: MEDIUM
```

Confidence is MEDIUM rather than HIGH because the two halves of the
recommendation are not equally solid. That candidate L costs nothing is measured
in both engines at four sizes and is HIGH. That candidate L is the *right look*
is one person's judgement from a gallery, and the runner-up is 0.25 points
behind on a scale I chose myself. The gallery exists so that judgement can be
overruled by looking rather than by argument.

---

## 13. Remaining uncertainties

### Things this study measured but cannot explain

**Whether these animations reach the compositor at all.** Two instruments on the
same page disagree by two orders of magnitude: the trace records 1152
`UpdateLayoutTree` events per eight seconds with the animation running and zero
with it paused, while `Performance.getMetrics` reports five style recalculations
over two and a half seconds of the same animation. The trace event is therefore
the animation tick rather than a style invalidation, and this study's first
reading of it — "one recalculation per frame, so it is not composited" — is
withdrawn in §9. What replaced it is narrower: resolving a `var()` in a keyframe
costs per element per frame, wherever the animation runs. Where it runs is still
unknown. The experiment that would settle it is written and not run:
`scripts/flow_pipe_study/pipe_composited.mjs` compares nine controlled variants
— a bare div, a clipped div, a div with a background image, transform against
opacity, CSS against WAAPI — and pairs the style-recalculation count with a
second, independent signal: whether the layer keeps moving while the main thread
is blocked for 600 ms, which only a compositor-driven animation can do. It was
dropped once the recommendation it supported was withdrawn.

**Why 288 animated layers costs between 80.8 and 143.8 fps in Chromium
depending on the configuration.** Layer count alone does not predict the knee.
The most likely missing variable is the total *area* of the animated layers, and
the compositor accounting in the layer probe is the place to test it.

**Where the `filter` cost actually sits.** The renderer main thread is provably
idle — fewer style recalculations than the cheap variants, zero paints, zero
raster tasks — and the frames still do not arrive. By elimination it is the
compositor or the GPU. This harness instruments neither.

### Things this study did not measure

- **macOS. Nothing here says anything about it.** There is no Mac on this
  project and no result was extrapolated to one.
- **Weak hardware.** Everything is a GTX 1660 Ti at 144 Hz. The conclusion "the
  artwork is free" is a conclusion about a machine with headroom. A phone or a
  low-power tablet may well find the knee earlier, and the 2 ms of main thread
  that this report dismisses as invisible may not be invisible there.
- **The dashboard itself.** Every number is from the isolated lab scene. The
  flow layer is one part of a page that also parses SSE, updates the DOM and
  draws charts, and this study says nothing about how they interact. The
  before/after that would close that gap —
  `scripts/dashboard_bench.py --matrix views --gpu headed` — was not run,
  because no production change is being recommended for performance.
- **Candidate C with a glow.** `radius-el` would take its halo from
  `box-shadow` on real elements rather than from the artwork. That path was
  never benchmarked; only the tiled candidates were measured across the glow
  axis. It is scored on the assumption that a `box-shadow` paints into the layer
  like any other decoration, which is plausible and unverified.

### Things that changed under the study's own feet

- Candidate **E** `repeating` was re-tiled after phase one, to fix the halo
  alignment shared by all the CSS-gradient candidates. Its phase-one numbers in
  `pipes-candidates-*` and `pipes-stress-*` are from the earlier construction;
  its numbers in `pipes-candidates-r2-*` are from the current one. Both are
  within noise of everything else, so nothing rests on the difference, but the
  files are not describing identical code.
- The served files were frozen for the whole of phase one and verified unchanged
  by checksum afterwards. Two edits made minutes before the series began — the
  segment-shape variants and negative-power handling — landed between the first
  and second matrix. Both are inert for the cases measured, which was checked by
  reading them; the repeat run in phase two re-measured every candidate with the
  final code and agrees.

### The judgement that is not a measurement

Section 3 and the visual column of section 10 are one person's eye on a gallery.
They are the highest-weighted input to the recommendation and the least solid
thing in this report. The gallery is a deliverable precisely so that this can be
checked by looking: `scripts/flow_pipe_study/gallery.html`, served from that
directory, with controls for power, sweep, direction, device count, segment
length and glow.
