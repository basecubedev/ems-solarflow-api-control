# Keeping the energy flow moving without paying for it

An investigation into how the dashboard's animated energy flow should be
rendered. It replaces the conclusion of the previous pass
in a handoff document, which named the wrong cause.

- **Date**: 2026-09-04.
- **Subject**: the flow views of an EMS operator dashboard — an SVG of pipes and
  device boxes, with a dash pattern animated along each pipe.
- **Reported symptom**: the dashboard becomes unresponsive, in particular
  **Firefox on macOS**.
- **Outcome**: the flow now renders as an HTML layer moved with a CSS transform.
  The aggregated view, which is what the dashboard opens on, goes from 4.4 to
  about 53 fps in Firefox. Two problems are characterised but unsolved, and both
  are named below.

---

## 1. Problem statement

The previous pass established that the frame rate collapses whenever the flow
animation runs, and concluded that `stroke-dashoffset` is expensive because it
is not a compositable property. That conclusion predicted that a compositable
replacement would fix it.

It is wrong in an instructive way. `stroke-dashoffset` is not the problem, and
neither is any other single property. The measurements below identify the actual
rule, which is coarser and more useful:

> **An SVG element cannot be composited on its own. Any CSS animation on one
> forces its whole subtree to be rasterised again for every frame, and this
> subtree is full of `drop-shadow` filters. Which property is animated barely
> matters; that an animation is running at all does.**

A second, independent cost was found on top of it, and it belongs to the page
rather than to the flow:

> **`backdrop-filter` is re-evaluated whenever anything on the page repaints.**
> In Chromium this alone caps the dashboard at about 17 fps no matter what the
> flow does.

---

## 2. How these numbers were produced

Two harnesses, both driving real browsers over Playwright against a local
preview server. No EMS, no hardware, no network.

| Harness | What it measures |
|---|---|
| `scripts/flow_lab/` + `scripts/flow_lab_bench.py` | an isolated scene of N pipes with the dashboard's exact geometry, colours and speed buckets, rendered by a selectable technique |
| `scripts/dashboard_bench.py` | the real dashboard, with CSS overrides injected per scenario |

`fps` is how often `requestAnimationFrame` ran, which is a good comparator and a
poor absolute. Event-loop lag, DOM mutations and (Chromium only) a DevTools
trace are recorded alongside.

**Four limits bound everything below.**

1. **Linux, not macOS.** The report is Firefox on macOS and this project has no
   macOS host. These are statements about the engine, not the platform.
2. **Headless and software-rendered** for the benchmarks. Where it mattered,
   findings were repeated on a real X display under Xvfb and are noted as such.
3. **The isolated lab is not the dashboard.** Section 6 is entirely about a
   finding that held in the lab and did not transfer, which is the most
   important methodological lesson here.
4. **Ratios within one browser and one session** are the only comparison these
   numbers support.

Rule the harness cannot enforce: never run two benchmarks at once.

---

## 3. Baseline

Real dashboard, devices view, four devices, two runs each.

| | Firefox | Chromium |
|---|---:|---:|
| as shipped before this work | 3.8 | 14.6 |
| every CSS animation in the flow SVG disabled | 55.7 | 56.3 |

The gap is the whole subject of this report.

---

## 4. Candidates, measured in isolation

Nine techniques, all drawing the same scene: twelve pipes, identical geometry,
colours, speed buckets and active/inactive state; only the animated layer
differs. Every candidate was checked to actually move before it was measured
(`scripts/flow_lab_verify.mjs`) — the previous pass reported a 13x win from an
experiment that never executed, and this guard exists because of it.

Twelve pipes, one tab, headless, median of two runs. `none` is the same scene
with no energy layer at all, which is the ceiling.

| Technique | Chromium | Firefox | elements |
|---|---:|---:|---:|
| none | 60.0 | 60.0 | 1 |
| **`stroke-dashoffset`** (as shipped) | 22.0 | 17.6 | 49 |
| SVG tile + CSS `transform`, clipped per segment | 20.2 | 17.9 | 229 |
| SVG `<pattern>` + animated `patternTransform` | 21.5 | 17.6 | 115 |
| SVG `<mask>` + moving stripes | 0.9 | 17.5 | 107 |
| **HTML tiles + `transform`** | 30.6 | **60.0** | 109 |
| CSS `offset-path` capsules | 27.3 | **60.0** | 109 |
| `<canvas>` + `shadowBlur` | **59.9** | 47.6 | 37 |
| `<canvas>` + stacked strokes instead of blur | **60.0** | **60.0** | 37 |

The first conclusion is already visible: **in Firefox every technique that stays
inside the SVG costs the same**, whatever property it animates. A compositable
`transform` on an SVG group is no better than `stroke-dashoffset`. What helps is
leaving SVG.

### 4.1 What the animated layer's filter costs

The halo on the moving layer is `drop-shadow(0 0 6px) drop-shadow(0 0 15px)`.
There is a second, *static* filter on the sibling `.pipe-glow`. Removing them
together cannot say which was being paid for, so each was removed alone.

Twelve pipes. `static-only` = the animating layer has no filter, the static one
remains.

| | Chromium | Firefox |
|---|---:|---:|
| `stroke-dashoffset`, both filters | 21.9 | 17.5 |
| `stroke-dashoffset`, animated layer unfiltered | **56.6** | 28.8 |
| `stroke-dashoffset`, static sibling unfiltered | 31.9 | 26.5 |
| `stroke-dashoffset`, neither filtered | 59.6 | 59.8 |

Chromium pays almost entirely for the filter on the moving element. Firefox pays
for **any** filter in the subtree being invalidated, static or not — which is
exactly why the previous pass measured no gain from moving the halo onto the
static sibling (1.00x). That was not a mismeasurement; it was an incomplete
model.

### 4.2 Scaling with the number of flows

Painted area held constant, flow count varied. One run each.

**Chromium**

| Technique | 12 | 50 | 100 |
|---|---:|---:|---:|
| `stroke-dashoffset` | 22.8 | 27.5 | 26.1 |
| HTML tiles | 31.4 | 12.1 | 7.1 |
| canvas + `shadowBlur` | 59.9 | 59.9 | 50.7 |
| canvas + stacked strokes | 60.0 | 60.0 | 60.0 |

**Firefox**

| Technique | 12 | 50 | 100 |
|---|---:|---:|---:|
| `stroke-dashoffset` | 17.8 | 12.1 | 10.0 |
| HTML tiles | 60.0 | 60.0 | 60.0 |
| canvas + `shadowBlur` | 48.3 | 44.3 | 13.2 |
| canvas + stacked strokes | 60.0 | 59.9 | 59.7 |

The two engines have opposite weaknesses. Chromium charges for a per-element
filter, so a hundred filtered HTML tiles collapse it. Firefox charges for a
per-frame gaussian blur, so a canvas using `shadowBlur` collapses it. Neither
charges for the technique itself.

### 4.3 Scaling with tabs

Twelve flows, one run each. The server caps concurrent event streams per client
at two, as production does.

| Tabs | `dashoffset` FF | tiles FF | canvas+strokes FF | `dashoffset` CR | canvas+strokes CR |
|---|---:|---:|---:|---:|---:|
| 1 | 17.8 | 60.0 | 60.0 | 22.9 | 60.0 |
| 2 | 12.9 | 60.0 | 60.0 | 14.4 | 60.0 |
| 5 | 5.1 | 60.0 | 58.5 | 6.3 | 58.0 |
| 10 | 2.8 | 60.0 | 58.6 | 2.8 | 25.7 |

### 4.4 Chromium trace

DevTools trace over the measured window, twelve pipes.

| Technique | fps | Paint | RasterTask | raster ms | Commit |
|---|---:|---:|---:|---:|---:|
| `stroke-dashoffset` | 21.7 | 356 | 4,287 | **30,580** | 183 |
| SVG tile + transform | 20.2 | 326 | 0 | 0 | 163 |
| HTML tiles | 30.6 | 0 | 0 | 0 | 247 |
| `offset-path` | 26.8 | 215 | 5,359 | 28,849 | 223 |
| canvas | 60.0 | 0 | 0 | 0 | 480 |
| none | 60.0 | 0 | 0 | 0 | 481 |

Thirty seconds of rasterisation inside an eight second window — raster runs on
several worker threads — against none at all. The canvas traces identically to
having no animation, and with the halo removed the `dashoffset` raster total
falls to 16,621 ms while its frame count roughly triples.

**Do not read this as "the canvas won".** Section 6 is about why it did not.

---

## 5. What the dashboard actually does, and why one animation is not enough

Everything above is the isolated lab. Applied to the real dashboard, removing
the flow animation alone recovered almost nothing:

| Firefox, devices view, four devices | fps |
|---|---:|
| baseline | 3.8 |
| flow dash animation off | 4.2 |
| `softPulse` animating opacity instead of a filter | 4.2 |
| **both** | **57.9** |

Two animations, each individually necessary and only jointly sufficient. The
dashboard runs three kinds inside the flow SVG, enumerated with
`document.getAnimations()` rather than guessed at
(`scripts/flow_lab_animations.mjs`): twelve `pipeFlow`, eight `softPulse` on the
sun and the inverter LED, four `fillPulse` on the battery fill. `softPulse`
animates the `filter` property itself, which is the same per-frame cost the
pipes had.

Chromium needs more than that. With the dash animation gone, every remaining
combination was tried:

| Chromium, devices view, four devices | fps |
|---|---:|
| dash animation off | 16.1 |
| + `softPulse` animating opacity | 17.1 |
| + `will-change: opacity` on the pulsing elements | 17.1 |
| + no filter on the sun and LED | 17.4 |
| + **pulses stopped entirely** | **55.6** |
| only `fillPulse` left running (plain opacity, unfiltered element) | 17.4 |

Any single animation left running in the SVG holds Chromium at about 17 fps,
including a plain opacity animation on an element that carries no filter at all.

`will-change: opacity` is worth calling out: it does nothing in Chromium and in
Firefox it is actively harmful — 55.8 fps without it, **4.8 with it**. It is not
a mitigation here.

### 5.1 A previous measurement that no longer reproduces

The earlier report states that removing the dash animation alone took Firefox to
58.0 fps. Re-run today against the current tree, the identical CSS override
gives **4.1**, while every other variant in that matrix reproduces within noise
(`anim-no-dash-no-pulses` 55.7 then, 55.8 now). The synthetic preview payload
changed between the two dates, which changes how many device visuals are
`active` and therefore how many `softPulse` animations run. The old figure is
superseded; the headline it supported was wrong.

---

## 6. The candidate that won the lab and lost the dashboard

`<canvas>` with stacked strokes was the best technique in the lab: 60 fps in
both engines at every flow count and almost every tab count. It was implemented
in production — geometry parsed from the SVG, appearance read back out of the
CSS, device boxes punched out with `destination-out`, a bloom of stacked strokes
in place of `shadowBlur` — and then **withdrawn**, for two independent reasons.

### 6.1 A full-size canvas over the flow SVG makes it render darker

Mean brightness of the flow panel, same frozen data, one variable:

| | Firefox | Chromium |
|---|---:|---:|
| no canvas | 52.5 | 51.8 |
| a plain, empty `<canvas>` sized over the SVG | **35.7** | **34.4** |
| the same box as a `<div>` | 52.5 | 51.8 |
| canvas again, removed | 52.5 | 51.8 |

Every SVG filter stops rendering: the device boxes lose their coloured halo and
the pipes lose their glow. It reproduces in both engines, headless and on a real
X display, appears wherever the canvas is in the DOM (including `document.body`)
as long as it overlaps the flow area, and does **not** reproduce on a minimal
page with one filtered SVG and one canvas. Its cause was not identified. Giving
the canvas `z-index: -1` avoids it.

This also means the lab's canvas figures were measured on a page that was
rendering filters differently from the page beside it. They are not trustworthy
as a comparison against the SVG techniques.

### 6.2 A canvas repaint costs as much as animating the SVG did

Measured with the working canvas renderer on the real dashboard, aggregated
view, two devices. The only variable is whether its `requestAnimationFrame` loop
is running.

| | Firefox | Chromium |
|---|---:|---:|
| canvas loop running | 5.2 | 16.9 |
| canvas loop stopped, canvas element still there | **60.1** | 16.4 |

One draw costs 0.54 ms. The cost is not the drawing; it is that a canvas has to
be *repainted* every frame, and on this page a repaint is expensive. Moving the
canvas above the SVG instead of behind it changed nothing (4.7 against 4.9).

A transform on a promoted layer is not a repaint. Adding transform-animated HTML
tiles to the same page, same view:

| animated tiles | 0 | 1 | 12 | 40 | 80 |
|---|---:|---:|---:|---:|---:|
| Firefox | 60.1 | 60.3 | 60.1 | 60.3 | 60.2 |

Eighty of them are free. That is the difference between the two approaches, and
it is invisible in the isolated lab because the lab page has nothing else in it.

---

## 7. Where Chromium's ceiling comes from

With the canvas renderer running, a factorial on the real dashboard
(aggregated view, two devices):

| backdrop-filter | pulses | canvas loop | Firefox | Chromium |
|---|---|---|---:|---:|
| on | on | on | 5.2 | 16.9 |
| on | on | off | 60.1 | 16.4 |
| on | off | off | 60.7 | 59.5 |
| off | on | on | 8.8 | **60.0** |
| off | on | off | 60.2 | 60.3 |
| off | off | on | 44.2 | 60.2 |
| off | off | off | 60.4 | 60.4 |

**With `backdrop-filter` disabled, Chromium is at 60 fps in every cell.** The
"flat toll for any animation" the previous pass described is `backdrop-filter`
being recomputed whenever anything repaints. That pass measured backdrop-filter
*with the animation off*, where it costs nothing, and recorded it as refuted —
the interaction was never measured.

This is a product decision rather than a rendering one: the glass-panel look
costs Chromium roughly 3.5x on this page whenever anything moves.

---

## 8. Visual correctness

Every candidate was frozen at animation time zero and compared pixel by pixel
against the production technique at the same phase
(`scripts/flow_lab_fidelity.mjs`), so "close enough" is a number.

| Candidate | differing pixels | strongly differing | mean channel delta |
|---|---:|---:|---:|
| SVG tile + transform | 0.7% | 0.03% | 0.4 |
| SVG `<pattern>` | 5.0% | 0.63% | 0.9 |
| SVG `<mask>` | 9.8% | 0.52% | 1.4 |
| HTML tiles | 16.9% | 0.13% | 1.8 |
| canvas + stacked strokes | 17.8% | 0.02% | 2.1 |
| `offset-path` | 18.6% | 2.08% | 3.1 |

The HTML-tile difference is almost all in the soft falloff: square dash ends
instead of round caps, and a halo that comes from the static `.pipe-glow` layer
rather than a filter on the dash itself. On the real dashboard the shipped
renderer differs from the CSS animation by 3% of mean panel brightness in the
aggregated view and 3% in the devices view, with the geometry, the colours, the
speed buckets, the idle state and the occlusion by the device boxes unchanged.

`offset-path` is the one candidate with a visible geometric error: its dash
spacing is the path length divided by a whole number of dashes rather than the
52-unit period, because the pipe is not a whole number of periods long.

---

## 9. Rejected approaches

| Approach | Why |
|---|---|
| **SVG `<mask>` with moving stripes** | 0.9 fps in Chromium at twelve pipes, 0.4 at fifty. Worst candidate measured. |
| **SVG `<pattern>` with animated `patternTransform`** | Indistinguishable from the technique it replaces in both engines. |
| **SVG tile clipped per segment, moved with `transform`** | The best fidelity of any candidate (0.7%) and no faster: 0.92x Chromium, 1.02x Firefox. A compositable property on an SVG element is not composited. |
| **CSS `offset-path`** | Fast in Firefox, 1.24x in Chromium, worst fidelity, and the element count grows with pipe length. |
| **`<canvas>`** | Section 6: it dims the SVG beneath it, and its per-frame repaint costs Firefox everything. |
| **`will-change` on the pulsing SVG elements** | No effect in Chromium, 55.8 → 4.8 fps in Firefox. |
| **`IntersectionObserver` to pause off-screen tiles** | Implemented and measured: the aggregated view fell from 60 to 13 fps. Reverted. |
| **`animation-timing-function: steps(N)`** (previous pass) | 2.13x at four steps per cycle, which is not motion. |
| **Driving `stroke-dashoffset` from a timer** (previous pass) | Slightly worse than the CSS animation. |

---

## 10. What was implemented

An HTML layer above the flow SVG, one box per visible run of each pipe, each box
clipping a repeating-gradient strip that is moved by one dash period with a CSS
`transform`. `dashboard/static/app.js`, `dashboard/static/styles.css`.

Three properties are worth stating because they are what keep it maintainable:

1. **No appearance rule is written twice.** The renderer reads the dash pattern,
   width, colour, opacity, speed, direction and whether to move at all from
   `getComputedStyle` on the `.pipe-energy` element the CSS still defines. So
   `dashboard.animation_mode`, `prefers-reduced-motion`, the idle state and the
   four flow-speed buckets keep working through the CSS that already implements
   them, and the renderer has never heard of any of them.
2. **It refuses what it cannot represent.** The path parser accepts only `M`,
   `L`, `H` and `V`; a curve, a relative command or a `Z` disables the whole
   renderer and the CSS animation stays. So does a browser without
   `getComputedStyle`. The flow is never simply missing.
3. **Each run is cut back around the device boxes**, which is how the SVG hid
   the ends of every pipe, and the dash phase is carried across both the corners
   and the cuts.

`softPulse` now animates opacity only. Its `drop-shadow` keyframe was the second
of the two animations that had to stop before Firefox recovered.

Contract tests: `tests/test_dashboard_flow_tiles.py` (17 tests, node, no
browser) covering the parser, the CSS read-back for every animation mode, the
occlusion cutting, phase continuity across a cut in both directions, the
gradient never fading through transparent-black, both refusal paths, and CSS
assertions that no rule in the layer carries a filter and that `softPulse` does
not animate one.

---

## 11. Measured result

Same matrix, same machine, two runs each; "before" was taken by reverting the
two production files and re-running.

**Firefox**

| View | devices | before | after | |
|---|---:|---:|---:|---:|
| aggregated | 2 | 5.6 | **56.0** | 9.9x |
| aggregated | 4 | 5.4 | **57.6** | 10.6x |
| aggregated | 8 | 5.5 | **57.7** | 10.5x |
| devices | 2 | 4.7 | **57.6** | 12.2x |
| devices | 4 | 4.1 | 9.2 | 2.3x |
| devices | 8 | 4.5 | 12.4 | 2.8x |

**Chromium**

| View | devices | before | after | |
|---|---:|---:|---:|---:|
| aggregated | 2 | 15.9 | 17.1 | 1.07x |
| aggregated | 4 | 16.2 | 17.0 | 1.05x |
| aggregated | 8 | 15.9 | 17.0 | 1.07x |
| devices | 2 | 16.3 | 17.6 | 1.08x |
| devices | 4 | 14.6 | 16.9 | 1.16x |
| devices | 8 | 15.0 | 16.6 | 1.10x |

Read this honestly. **The aggregated view -- the one the dashboard opens on --
is fixed in Firefox at every installation size, and the devices view is fixed up
to two devices.** Beyond that the devices view improves by 2-3x and stays slow,
for the reason in §12.1. Chromium barely moves, because its ceiling is
`backdrop-filter` and not the flow (§7).

---

## 12. Remaining risks and open problems

1. **The devices view still collapses past two devices in Firefox.** The trigger
   is the tile layer growing taller than the viewport, not the tile count:

   | devices | tiles | layer height | Firefox |
   |---:|---:|---:|---:|
   | 1 | 10 | 438 px | 50.0 |
   | 2 | 19 | 621 px | 60.1 |
   | 3 | 28 | 909 px | 8.8 |
   | 4 | 37 | 1197 px | 9.2 |

   Forty cloned tiles inside a 395 px layer run at 60.2, so it is not the
   number of animated elements. Pausing what is off screen made it worse
   (section 9). This is the largest open item.

2. **Chromium is capped at about 17 fps by `backdrop-filter`** whenever anything
   on the page moves (section 7). Removing it from the panels takes every
   measured configuration to 60. That is a visual-design decision.

3. **No macOS, no GPU.** The reported symptom is Firefox on macOS. Everything
   here is Linux, and everything except the checks explicitly marked otherwise
   is software-rendered.

4. **The canvas dimming in section 6.1 is unexplained.** It is avoidable and
   avoided, but it is worth knowing before anyone puts a canvas on this page.

5. **The control view was never in scope** and is dominated by twenty-six
   `controlResultBorderFlow` animations. It measures 4.4 fps in Firefox before
   and after this change.

---

## 13. Reproducing this

```bash
# the isolated lab
python3 scripts/flow_lab_bench.py --matrix renderers --browser firefox --repeat 2
python3 scripts/flow_lab_bench.py --matrix glow      --browser chromium --repeat 2
python3 scripts/flow_lab_bench.py --matrix scaling   --browser firefox
python3 scripts/flow_lab_report.py reports/dashboard-perf/flowlab-*.json

# the real dashboard
python3 scripts/dashboard_bench.py --matrix flow-svg --browser firefox --repeat 2
python3 scripts/dashboard_bench.py --matrix pulses   --browser chromium --repeat 2
python3 scripts/dashboard_bench.py --matrix views    --browser firefox --repeat 2

# the renderer itself
node scripts/flow_lab_verify.mjs    <labUrl> <outDir> firefox
node scripts/flow_lab_fidelity.mjs  <labUrl> <outDir> firefox
node scripts/flow_lab_animations.mjs <dashboardUrl> firefox
node scripts/flow_tiles_check.mjs   <dashboardUrl> <outDir> firefox
```

---

## 14. Recommendation

`RECOMMENDATION: HTML tile layer moved with a CSS transform`
`CONFIDENCE: MEDIUM`

The evidence for the technique is strong and consistent across both harnesses
and both engines: in the isolated lab it holds 60 fps at a hundred flows and ten
tabs in Firefox, and on the real dashboard eighty animated tiles cost nothing
measurable while a canvas repaint costs everything. It is the only candidate
that is fast, does not disturb how the page renders, and needs no per-frame
work.

Confidence is MEDIUM rather than HIGH for three reasons, all measured rather
than suspected: the devices view past two devices is still slow for a reason
that is characterised but not understood (§11.1); Chromium's ceiling is set by
`backdrop-filter` and not by anything this change touches (§7); and the reported
symptom is a platform nobody here can test on (§11.3).

The single most valuable next measurement is not another technique. It is
running `scripts/dashboard_bench.py` on the machine the symptom was reported
from.
