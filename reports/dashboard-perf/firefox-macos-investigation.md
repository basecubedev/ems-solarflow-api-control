# Firefox/macOS dashboard slowdown — investigation

*Why can the dashboard feel sluggish in Firefox even with
`animation_mode=off`, and why can having it in the foreground make another
Firefox window slower?*

This is the investigation the rendering studies kept deferring. The renderer
question is closed: [`energy-flow-visualization-study.md`](energy-flow-visualization-study.md)
settled the technology and [`energy-pipe-performance-study.md`](energy-pipe-performance-study.md)
settled the artwork, and neither found a meaningful gain available there. What
was left is the original symptom, which the animation switch does not fix.

**The reported platform is Firefox on macOS. There is no Mac in this
environment.** Nothing below is a macOS measurement. What is here is Linux
evidence, **two causes found and fixed on Linux**, and a harness built so the
same numbers can be taken on the machine where the symptom was seen. Section 9
says exactly which claims that leaves open.

The short version, for anyone who reads no further:

| | | |
|---|---|---|
| §4–5 | a forced synchronous layout, once per snapshot, to discover that a view is not on screen | main thread: −66 to −76 %, and 2143 → 477 ms under 16× CPU throttling |
| §5b | an animated paint property on twenty-six masked pseudo-elements | compositor: Chromium's control view 45.2 → 133.1 fps |
| §2 | there is no standing cost at all — a silent feed measures 0 ms | so `animation_mode=off` had nothing to remove, and §4 shows it made what was there worse |
| §6 | no browser-wide effect reproduces on Linux | a page beside the dashboard measured 144.0 fps and zero blocking |
| §9 | none of this is a macOS measurement | the harness exists so one can be taken |

Hardware: NVIDIA GTX 1660 Ti, 144 Hz display, eight cores, Debian 13, Firefox
and Chromium headed on the real display, one case at a time behind a load gate.

---

## 1. What was measured, and with what

The existing benchmark answers "how many frames". That is the wrong instrument
for this question: on this machine the dashboard runs at the display's ceiling
in every configuration, including the ones a user calls sluggish. The question
is where the main thread goes, so a new instrument was built for it.

`scripts/dashboard_profile/` drives the **real dashboard** and charges
main-thread time to whoever consumed it, by wrapping the entry points through
which a page can spend its thread: event listeners (including the `EventSource`
handler that receives every snapshot), timers, animation frames,
`ResizeObserver`, `MutationObserver`, `fetch`, and — behind a flag — the
reads that force synchronous layout. It also adds the three arrangements the
frame-rate harness cannot express:

| | what it isolates |
|---|---|
| `feed=silent` | the page has rendered once and nothing more arrives |
| `feed=frozen` | data arrives and the page decides not to render it |
| `feed=live` | the full path |
| a second, trivial page | whether an open dashboard costs its neighbour anything |
| foreground vs background | whether an unfocused dashboard keeps working |

Nothing in it is browser-specific. Running it on a Mac is the point of it
existing; `scripts/dashboard_profile/README.md` is the instruction.

---

## 2. The dashboard is not continuously busy

The first thing to rule out, because it is the intuitive explanation for a page
that feels heavy: a standing cost that runs whether or not anything happens.

There is none. Firefox, animation off, ten-second windows:

| view | feed | attributed main-thread work | DOM mutations |
|---|---|---:|---:|
| aggregated | silent | **0 ms** | 0 |
| aggregated | frozen | **0 ms** | 0 |
| aggregated | live | 60 ms | 395 |
| devices | silent | **0 ms** | 0 |
| devices | frozen | **0 ms** | 0 |
| devices | live | 159 ms | 985 |
| control | silent | **0 ms** | 0 |
| control | frozen | **0 ms** | 0 |
| control | live | 269 ms | 365 |

With nothing arriving the page does nothing at all — no timers firing into work,
no observers, no animation frames, no mutations. `frozen` is as important:
snapshots arrive at the normal rate, the render guard decides the timestamp is
unchanged, and the cost is again zero. **Unchanged data causes no rendering
work**, which was hypothesis 2 and is now ruled out.

Every cost in this dashboard is paid per rendered snapshot.

---

## 3. What one snapshot costs, and where it goes

Ten seconds is five snapshots at the two-second interval. Firefox, headed,
animation off, before any change:

| view | per snapshot | of which the SSE handler | of which animation frames |
|---|---:|---:|---:|
| aggregated | 12 ms | 4.2 ms | 7.8 ms |
| devices | 32 ms | 10.4 ms | 21.4 ms |
| control | **54 ms** | 22.2 ms | **31.6 ms** |

The larger half is in `requestAnimationFrame` callbacks — with the animation
switched off. Turning the deep-read instrument on says what those callbacks are
actually doing:

| Firefox, control view | animation frames | of which `getBoundingClientRect` | calls |
|---|---:|---:|---:|
| animation normal | 126 ms | **125 ms** | 5 |
| animation off | 181 ms | **180 ms** | 5 |

The animation-frame cost *is* `getBoundingClientRect`. Five calls in ten
seconds — one per snapshot — at 25 to 36 ms each. That is a forced synchronous
layout of a 1741-node document, once per snapshot, and it is essentially the
entire cost of the callback.

---

## 4. The first cause: a layout flush on the main thread

`invalidateFlowTiles()` schedules one animation frame per snapshot, which calls
`rebuildFlowTiles()`, which calls `buildFlowTileHost(svg)` for every flow SVG.
The first thing that function did was measure:

```js
const rect = svg.getBoundingClientRect();
if (!rect.width || !rect.height) {
  // The view is switched away.
  layer.hidden = true;
  return null;
}
```

Reading a box forces the browser to flush every pending style and layout change
first. In the control view — and in energy, analytics, diagnose, logs and
maintenance — **no flow SVG is on screen at all**, so this measured a hidden
element, discovered it was hidden, and returned. The dashboard paid for a full
layout in order to learn something `setFlowView` had already recorded by setting
`svg.hidden = true`.

Removing the tile renderer at runtime confirms the attribution rather than
inferring it. Same run, deep reads on, median of two:

| view / animation | tile layer on | tile layer off | `getBoundingClientRect` |
|---|---:|---:|---|
| aggregated, normal | 56 ms | 12 ms | 20 ms → **0** |
| aggregated, off | 72 ms | 19 ms | 24 ms → **0** |
| devices, normal | 178 ms | 22 ms | 72 ms → **0** |
| devices, off | 298 ms | 62 ms | 112 ms → **0** |
| control, normal | 290 ms | 42 ms | 124 ms → **0** |
| control, off | 456 ms | 96 ms | 170 ms → **0** |

Every layout-forcing read in the dashboard comes from the flow tile rebuild, and
removing it removes about 79 % of the main-thread work in every view.

### Why `animation_mode=off` made it worse

The number that does not fit the intuition, and it is reproducible — three runs,
non-overlapping ranges:

| view | animation normal | animation off |
|---|---:|---:|
| aggregated | 39 ms (36–41) | 43 ms (32–51) |
| devices | 97 ms (93–108) | **136 ms (123–196)** |
| control | 169 ms (165–178) | **261 ms (249–271)** |
| energy | 151 ms (148–164) | 151 ms (123–173) |

Identical call counts, identical mutation counts, identical DOM size — the same
work taking longer. That is what a forced layout flush looks like when the tree
is dirtier: with an animation running the style is being recalculated every
frame anyway, so the reader arrives to a nearly clean tree; with the animation
off, nothing has flushed since the last snapshot and the reader pays for all of
it.

So the switch that exists to make the dashboard cheaper made its largest cost
**40 to 54 % more expensive** in the two views where it mattered.

The fix in section 5 does not remove this. It removes the flush that was
*largest*, and the same effect remains on what is left, at a smaller absolute
size — which makes the ratio look worse while the milliseconds get better:

| Firefox, median of 3, after the fix | normal | off | ratio |
|---|---:|---:|---:|
| aggregated | 33 ms | 48 ms | 1.45× |
| devices | 101 ms | 146 ms | 1.45× |
| control | 40 ms | 90 ms | 2.25× |
| energy | 47 ms | 62 ms | 1.32× |

Frame rate is untouched in all eight cases: 138–143 fps, inside the noise.

**`animation_mode=off` is not a performance control on this hardware.** It
removes no standing cost, because there is none to remove, and it makes the one
real cost more expensive. It is left in place: it is a legitimate accessibility
and preference setting, `prefers-reduced-motion` is honoured on top of it, and
on hardware weak enough that the saved compositor work outweighs the extra
main-thread work it may still pay — which has not been measured. What was
corrected is the documentation, which called it a real performance control.

---

## 5. The first fix

Test first, minimal, and confined to the one decision that was being made the
expensive way. `buildFlowTileHost` now asks whether the view is off screen
before it measures anything, using the attribute `setFlowView` already sets:

```js
function flowSvgOffScreen(svg) {
  if (!svg) return true;
  if (svg.hidden) return true;
  return typeof svg.closest === "function" && Boolean(svg.closest("[hidden]"));
}
```

Two contract tests pin it: one drives the predicate against stubs whose
`getBoundingClientRect` throws, so it fails if the answer is ever taken from
geometry; the other pins the ordering, because consulting it after the rect has
already been read would leave the cost exactly where it was.

Measured before and after, Firefox headed, median of three:

| view / animation | before | after | change |
|---|---:|---:|---:|
| **control**, normal | 169 ms | **40 ms** | **−76 %** |
| **control**, off | 261 ms | **90 ms** | **−66 %** |
| **energy**, normal | 151 ms | **47 ms** | **−69 %** |
| **energy**, off | 151 ms | **62 ms** | **−59 %** |
| aggregated, normal | 39 ms | 33 ms | within noise |
| aggregated, off | 43 ms | 48 ms | within noise |
| devices, normal | 97 ms | 101 ms | within noise |
| devices, off | 136 ms | 146 ms | within noise |

Animation-frame time in the control and energy views falls from 96–158 ms to
**0–1 ms**. The views where a flow SVG really is on screen are unchanged, which
is correct: there the measurement is needed and the fix does not touch it.

26 flow-tile contract tests and 419 dashboard tests pass.


---

## 5b. The second cause: a paint property on the compositor

The fix above removed three quarters of the control view's main-thread work and
did not move its frame rate at all: Chromium drew that view at 45.8 fps before
and 46.6 after, at every CPU throttling level. Something else was holding it
there, and it was not on the main thread.

It is the border on a control-stage result chip:

```css
.control-result::after {
  background: linear-gradient(90deg, …);
  background-size: 220% 100%;
  animation: controlResultBorderFlow 4.2s linear infinite;   /* background-position */
  mask-composite: exclude;                                   /* the 1px ring */
}
```

`background-position` is a paint property. Chromium cannot give it to the
compositor, so it repaints the chip on every frame — and the control view puts
one on every stage of every device. **Twenty-six of them at four devices**,
repainted sixty times a second, for a decorative one-pixel border.

Isolated by disabling one half at a time through the CSSOM, Chromium, eight
devices, headed:

| | fps | frame p95 |
|---|---:|---:|
| animation and mask, as shipped | **45.2** | 27.8 ms |
| **animation off**, mask kept | **134.6** | 7.0 ms |
| mask off, animation kept | 48.7 | 27.8 ms |

The mask is not the cost. The animated paint property is. Firefox was
unaffected in all three arrangements (135.5–139.7 fps), so this is
Chromium-specific — which means it is not the reported symptom, and it is still
a two-thirds frame-rate loss in one of the two engines.

### The fix

The gradient moves into a child that is translated, instead of living in a
background whose position is animated. The child is exactly two tiles wide and
moves by one, so the loop has no seam; the mask that turns the box into a ring
stays exactly where it was. The rendered appearance is unchanged — twenty-six
rings, all animating, verified in the browser.

| Chromium, control view, animation on | before | after |
|---|---:|---:|
| 2 devices | 45.3 fps / p95 27.8 ms | **135.9 / 7.0** |
| 4 devices | 46.4 / 27.8 | **133.6 / 7.1** |
| 8 devices | 45.2 / 27.8 | **133.1 / 7.0** |

With the animation disabled in the same run the numbers are the same, which is
the point: the effect is now free. Firefox is unchanged, 134.5–143.3 either way.
Other views are unchanged, because they do not multiply the chip.

`.primary-button.compact::after` uses the same keyframe and keeps the old
construction on purpose. It is a single element and measured free — the
aggregated view read 137.0 fps with it running against 137.8 with it disabled.
What costs here is the count, not the effect.

### Why these two are a pair

They are the same page and two different subsystems, and each hid the other:

| | where it costs | what it shows up as | who feels it |
|---|---|---|---|
| forced layout in the tile rebuild | main thread | event-loop lag, long tasks | responsiveness, worse the slower the machine |
| animated paint property | compositor | frame rate, frame p95 | visible stutter, immediately |

The frame-rate benchmark could not find the first — the dashboard never left the
refresh ceiling. The attribution profiler could not find the second — it reports
*less* main-thread work when the compositor is the bottleneck, because fewer
frames means fewer style recalculations. Finding both needed both instruments.

---

## 6. The browser-wide symptom

The second half of the report — a foreground dashboard making another Firefox
window slower — **does not reproduce on this machine**. A deliberately trivial
second page, opened beside the dashboard and brought to the front, measures its
own responsiveness:

| the neighbour page's own numbers | fps | lag p95 | lag max | blocking |
|---|---:|---:|---:|---:|
| neighbour alone, no dashboard open | 143.5 | 1.0 ms | 32 ms | 48 ms |
| dashboard open, control view, animation normal | **144.0** | 1.0 ms | 1 ms | **0 ms** |
| dashboard open, control view, animation off | **144.0** | 1.0 ms | 1 ms | **0 ms** |
| dashboard open, devices view, animation normal | **144.0** | 1.0 ms | 1 ms | **0 ms** |
| dashboard open, devices view, animation off | **144.0** | 1.0 ms | 1 ms | **0 ms** |

The neighbour is not merely unharmed, it is marginally *better* than the control
case, whose lag max and blocking time are page-construction noise. On Linux with
a GPU, an open dashboard costs another page nothing.

### What did show up: an unfocused dashboard keeps working

While the neighbour was in front, the dashboard reported
`document.hidden === false` and did its full work — 41 to 157 ms per ten-second
window, the same as when it was in front:

| | attributed work | mutations | animation frames | `document.hidden` |
|---|---:|---:|---:|---|
| dashboard focused, animation normal | 122 ms | 985 | 15 | false |
| dashboard focused, animation off | 116 ms | 985 | 15 | false |
| neighbour focused, animation normal | 110 ms | 985 | 15 | false |
| neighbour focused, animation off | 144 ms | 985 | 15 | false |

Identical work, identical mutation counts. This is worth stating precisely
because it is easy to over-read: in this harness a non-foreground page reports
`document.hidden === false`, so what was measured is a **visible but unfocused
window**, not a background tab. That is the arrangement the symptom describes —
and the dashboard's own optimisation, which renders nothing while
`document.hidden` is true, does not apply to it. A background *window* is not a
hidden *tab*, and the dashboard cannot tell that it is not being looked at.

On this platform that costs nothing measurable. Whether it costs something on
macOS, where window compositing differs, is exactly what section 9 cannot say.

---

## 7. What the other hypotheses turned out to be

| # | hypothesis | verdict |
|---|---|---|
| 1 | repeated DOM rebuilding / excessive mutation | **Real but not the cost.** The devices view mutates 985 nodes per ten seconds and is cheaper than the control view, which mutates 365. Mutation count does not predict cost here. |
| 2 | work even when snapshots are unchanged | **Ruled out.** Frozen and silent feeds both measure 0 ms and 0 mutations. |
| 3 | forced synchronous layout outside the fixed path | **Confirmed, and it was the cause.** Section 4. |
| 4 | excessive style recalculation or paint from broad CSS | **Not observed.** No standing cost exists to attribute to it. |
| 5 | `backdrop-filter` or other effects | **Ruled out here**, and already removed from the panels by the previous study. |
| 6 | chart rendering | **Ruled out.** The analytics view, which draws a uPlot canvas, measures 10 ms of work per ten seconds — the cheapest view of all. |
| 7 | timers or recurring callbacks | **Ruled out.** The logs view runs a two-second poll of its own and still measures 10 ms. With a silent feed every timer in the page produces 0 ms. |
| 8 | SSE/polling update frequency | **Not a defect.** Cost is per rendered snapshot; the transport only decides how many there are. |
| 9 | per-tab duplicated work | not re-measured here; the previous study covers tab counts |
| 10 | Firefox compositor/WebRender behaviour | see section 8 |
| 11 | macOS-specific behaviour | **untested — no Mac.** Section 9 |
| 12 | shared-resource contention from a foreground dashboard | **Does not reproduce on Linux.** Section 6 |

---

## 8. Firefox against Chromium, and the rendering path

Chromium was run as a reference rather than as a competitor. After the fix,
median of two, headed on the GPU:

| view / animation | Chromium work | of which animation frames | long tasks |
|---|---:|---:|---:|
| aggregated, normal | 33 ms | 23 ms | 0 ms |
| aggregated, off | 31 ms | 22 ms | 0 ms |
| control, normal | 27 ms | **0 ms** | 0 ms |
| control, off | 38 ms | **0 ms** | 0 ms |
| devices, normal | 96 ms | 69 ms | 59 ms |
| devices, off | 119 ms | 84 ms | 0 ms |
| energy, normal | 45 ms | 1 ms | 0 ms |
| energy, off | 40 ms | 1 ms | 0 ms |

Chromium confirms the fix independently: animation-frame time in the control and
energy views is zero, and the profile has the same shape as Firefox's. The two
engines agree that the cost is per rendered snapshot and that it lives in the
same place. Where they differ is scale — the same work costs Firefox roughly
30 to 40 % more — and that difference is not what the symptom is made of.

One Chromium row is worth not glossing over: `control, animation normal`
recorded 45.5 fps in one of two runs while its attributed work stayed at 27 ms.
Work and frame rate came apart, which is a compositor-side effect the
main-thread instrument cannot see and which did not reproduce in the other run.
It is recorded here rather than dropped, and it is not a basis for any claim.

### The neighbour, in Chromium

Same answer as Firefox. The trivial page beside the dashboard measured 144.0
fps and zero blocking time in all four configurations, against 142.1 fps and
128 ms of blocking when it was open **alone**.

### Software rendering: not established

Firefox was launched with

```js
'gfx.webrender.software': true,
'gfx.webrender.software.opengl': false,
'layers.acceleration.disabled': true,
```

and measured no difference: aggregated 29/52 ms against 46/36, devices 107/200
against 91/158 — noise in both directions.

**That result is not reported as a software-rendering measurement**, because the
treatment could not be confirmed to have taken effect. Firefox returns the same
sanitised `WEBGL_debug_renderer_info` string with the prefs as without, and
`about:support`, which does carry the real compositor, cannot be opened through
Playwright — `scripts/dashboard_profile/gfx_probe.mjs` tries and records the
failure. The numbers are consistent with "the cost is layout, so the raster path
does not matter", which is what the rest of the report says; but consistent is
not the same as measured.

---

## 9. What remains unproven

**No macOS measurement exists.** Not one number in this report was taken on a
Mac, and none was extrapolated to one. What that leaves open:

1. **Whether the fixed defect was the reported symptom.** A forced synchronous
   layout costing 25–36 ms per snapshot on a 144 Hz Linux desktop is invisible;
   the same layout on slower hardware, or on an engine that prices layout
   differently, is not. The fix is justified on its own measurement — it removes
   two thirds of the main-thread work in the control view and cannot make
   anything worse — but whether it removes *the* symptom is untested.
2. **Whether anything macOS-specific remains.** The most concrete candidate this
   investigation produced is in section 6: a dashboard in a visible but
   unfocused window keeps doing its full work, because `document.hidden` is
   false and the dashboard's own guard does not apply. On Linux that costs a
   neighbouring page nothing. macOS composites windows differently and that is
   precisely where the reported symptom lives.
3. **Whether the rendering path matters.** Section 8: unverified.

### The harness for the Mac

`scripts/dashboard_profile/README.md` has the commands. Three matrices matter,
in this order — `feed` to confirm there is no standing cost, `attribution` to
see where the per-snapshot time goes, `neighbour` to test the browser-wide half.
The comparison to make is **per-snapshot main-thread work**, not frames per
second: on this Linux desktop the dashboard never left the refresh ceiling in
any configuration, so a frame rate could not have shown the defect that was
found.

---

## 10. Ranked causes, by measured impact

| rank | cause | measured impact | status |
|---|---|---|---|
| 1 | **An animated paint property on twenty-six elements.** `background-position` on every control-stage result chip, which Chromium cannot composite. | Chromium control view **45.2 → 133.1 fps**, frame p95 27.8 → 7.0 ms | **fixed** |
| 2 | **Forced synchronous layout in the flow tile rebuild.** One `getBoundingClientRect` per snapshot per flow SVG, including SVGs belonging to views that are switched away. | 25–36 ms per snapshot in the control view; ~79 % of all main-thread work in every view. Under 16× CPU throttling, 2143 → 477 ms per ten seconds and lag p95 47 → 9 ms | **fixed**, −66 to −76 % in the affected views |
| 3 | **`animation_mode=off` makes a layout flush more expensive**, because nothing else has flushed style since the last snapshot. | +40 % (devices), +54 % (control) before the fixes; 1.3–2.3× after, on a smaller base | inherent to the mechanism; the documentation was corrected rather than the switch |
| 4 | **Snapshot rendering itself.** The remainder in the control and energy views, and it forces no layout at all: 42 ms (normal) and 92 ms (off) per ten seconds, scaling with DOM size (1193 → 2837 nodes at 2 → 8 devices). | control 78/96/99 ms at 2/4/8 devices | not addressed; see below |
| 5 | **The flow tile rebuild where the SVG really is on screen.** Legitimate work, and still a layout flush per snapshot: 35 reads per ten seconds in the aggregated view, 80 in the devices view. | aggregated 19–29 ms, devices 64–94 ms per ten seconds | not addressed; see below |
| — | An unfocused but visible dashboard keeps working | no measurable cost on Linux | by design, and the leading macOS hypothesis |

### Why 4 and 5 were left alone

Both have plausible fixes and neither has evidence that justifies one yet.

For **5**, the geometry read could be cached and invalidated from the
`ResizeObserver` the renderer already installs. The risk is real and specific: a
`ResizeObserver` fires on size changes, not on position changes, so a panel
above the flow growing taller would move the SVG without resizing it and leave
the cached rect stale — pipes drawn in the wrong place. And in the devices view
the SVG is genuinely rebuilt every snapshot (985 DOM mutations), so its geometry
really can change and the read really is needed. That is a correctness risk
taken for an invisible gain on the only hardware available. Not justified.

For **4**, the control view rebuilds its explanation from scratch on every
snapshot. The re-measured profile is unambiguous that this is not a layout
problem — `getBoundingClientRect` does not appear in that view's profile at all
any more — so the fix would be incremental DOM updating, a real change to a real
view for a cost that is 8–18 ms per snapshot on this machine. The next required
measurement is what it is on the machine where the symptom was seen.

---

## WHAT THIS INVESTIGATION RULED OUT

- **A standing cost.** With nothing arriving the page measures 0 ms of work and
  0 mutations. There is no idle loop, no timer grinding, no observer firing.
- **Work on unchanged data.** Snapshots arriving with an unchanged timestamp
  produce 0 ms and 0 mutations; the render guard holds.
- **Charts.** The analytics view, which draws a uPlot canvas, is the *cheapest*
  view measured: 10 ms per ten seconds.
- **Recurring timers.** The logs view runs its own two-second poll and also
  measures 10 ms. Auth refresh (60 s) and analytics refresh (30 s) never
  appeared.
- **`backdrop-filter`.** Already removed from the panels by the previous study;
  nothing here reintroduced a cost.
- **DOM mutation volume as the driver.** The devices view mutates 985 nodes per
  ten seconds and costs less than the control view, which mutates 365.
- **The animation as the main cost.** Turning `animation_mode` off changes the
  frame rate not at all and *increases* main-thread work. What did cost was one
  specific animation -- a paint property on a chip the control view multiplies
  by twenty-six -- and that is now free rather than switched off.
- **The pipe renderer's artwork.** Settled by the two preceding studies and not
  reopened; nothing measured here contradicts them.
- **A browser-wide effect on Linux.** A trivial page beside the dashboard
  measured 144.0 fps and zero blocking in both engines, in every configuration.

---

```text
DIAGNOSIS: Two independent costs, in two different subsystems, each of which
           hid the other. On the main thread: a forced synchronous layout
           performed once per snapshot for every flow SVG, including the ones
           belonging to views that are switched away -- 25-36 ms in the control
           view, to measure an element the page had already marked hidden, and
           turning the animation off made it worse because a flush pays for
           whatever accumulated since the last one. On the compositor: an
           animated background-position on twenty-six masked pseudo-elements,
           which cost Chromium two thirds of its frame rate in the control view.
           The frame-rate harness could only see the second; the attribution
           profiler could only see the first.
PRIMARY CAUSE: forced synchronous layout in buildFlowTileHost, and an animated
               paint property on .control-result::after (both measured)
SECONDARY CAUSES: snapshot rendering itself, which forces no layout at all
                  and scales with DOM size (control view: 8-18 ms per snapshot,
                  1193 -> 2837 nodes at 2 -> 8 devices); the same layout flush
                  in the views where the flow SVG really is on screen, where
                  the read is legitimate; a visible-but-unfocused dashboard
                  doing full work because document.hidden is false
PRODUCTION FIX: two, each with contract tests and a before/after measurement.
                (1) buildFlowTileHost decides off-screen views from the hidden
                    attribute setFlowView already sets, instead of measuring
                    them: -66 to -76% main-thread work in the control and energy
                    views, unchanged where the SVG is visible, and 2143 -> 477 ms
                    under 16x CPU throttling.
                (2) the control-stage result border animates a transform on a
                    child instead of background-position on a masked pseudo-
                    element: Chromium's control view 45.2 -> 133.1 fps, frame
                    p95 27.8 -> 7.0 ms, appearance unchanged, Firefox unaffected.
CONFIDENCE: HIGH that both defects are real, measured and fixed.
            LOW that they are the whole of the reported macOS symptom, which
            was seen on a platform none of these numbers came from.
MACOS VERIFIED: NO
```
