<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Final dashboard performance audit

**2026-09-05 · Linux · Chromium 144 Hz headed on a GPU, Firefox headed · no macOS**

The last Linux-side pass over the dashboard frontend, taken while
[`firefox-macos-investigation.md`](firefox-macos-investigation.md) had the tools
out. That investigation found and fixed two defects — a forced synchronous
layout on the main thread, and an animated paint property on the compositor.
This audit asks what is left: how the page scales, what it rebuilds, what it
retains, what it does while nobody is looking at it, and whether any of it is
worth another change.

---

## Executive summary

Sixteen matrices, both engines, headed on a GPU, one case at a time. Four
changes made, each with a before/after; the first three were taken from a
worktree at the pre-change commit.

The largest had never been measurable, because every benchmark this project has
ever run used the **read-only** preview. With authentication configured the
runtime editor renders a submit button per stage card — twenty at twelve
devices — and each animated `background-position`, a paint property. The
authenticated control view drew at **36 fps**; it now draws at 133.8, frame p95
34.7 → 7 ms, painting 200 times per ten seconds instead of 5912. The other two:
a sixty-second timer rebuilt the control panel behind whatever view was on
screen (19 ms per minute, and 89 % of the aggregated view's DOM), and the
runtime editor was rebuilt twice a second from data no snapshot touches
(16.5 → 1.6 ms).

A fourth followed from the audit's own numbers. Every animation on the page
costs a style recalculation per frame — up to 2.9 s per ten seconds — and on
this desktop that costs no frames, which is why it was first left alone. Under
16× CPU throttling it does: the aggregated view was drawing at 110.7 fps with a
13.9 ms frame p95. The tile animation now runs from `element.animate()` with
literal keyframes instead of a keyframe that reads a custom property, which
returns that case to **135.0 fps at 7.0 ms** and takes 28–40 % of the style
recalculation out of every view at every throttle level.

What remains is one measured cost declined on risk and one open question. The
control view's markup costs 1 ms to build and 12 ms to parse, twice a second at
twelve devices — in-place updating has a real ceiling, and it is a reconciler in
the view an operator reads to understand a write decision. And a
visible-but-unfocused window has its frames throttled to 1 fps while the page
keeps rendering in full, which is the closest thing here to the symptom that
started all of this and cannot be settled without a Mac.

Everything else is ruled out with numbers: the retained off-screen DOM, CSS
selectors, containment, shadows, filters, charts, timers, and any leak across
360 view changes in thirty minutes. Nothing scales super-linearly, and after the
fixes the aggregated and energy views do not scale with device count at all.

Further Linux-side work is not justified.

---

## 1. What was measured, and with what

Everything here comes from
[`scripts/dashboard_profile/`](../../scripts/dashboard_profile/), the harness
built for the previous investigation, extended for this one. It drives the
**real dashboard** against the preview server through Playwright, charges
main-thread time to the callback that spent it, and — new in this audit — reads
the renderer's own counters for the stages a page cannot see from inside.

| axis | added for this audit | what it answers |
|---|---|---|
| `cdp_metrics` | yes | `RecalcStyleCount/Duration`, `LayoutCount/Duration`, live `Nodes`, `JSEventListeners`, `Documents`, `JSHeapUsedSize` — Chromium's own numbers, taken as a delta across the window |
| `trace` | yes | `Paint`, `RasterTask`, `Commit`, `PrePaint`, `UpdateLayoutTree` counts and durations |
| `cycle_views` | yes | rotate the view on a fixed interval, and time each switch |
| `sample_ms` + `gc` | yes | levels sampled through a long run, with garbage collected first, so a level that keeps climbing is retention rather than allocation |
| lifecycle counters | yes | listeners added/removed, intervals created/cleared, observers constructed/disconnected, `EventSource`s opened/closed — cumulative for the life of the page, deliberately not reset per window |
| `domNodesByView` | yes | node counts per view container, so "how many nodes" comes with "whose" |
| `scenario` | yes | `write-mode` renders the control view as an authenticated operator sees it |
| `feed`, `foreground`, `neighbour`, `deep_reads`, `cpu_throttle` | from the previous investigation | standing cost, background window, a second page, forced layout, a slow machine |

`profile_report.py` renders any of those JSON files as a table; every column in
this document is a recorded field or a ratio of two of them.

One case at a time, each behind a quiet-machine gate. **No two benchmarks ever
ran concurrently.**

### Three harness defects found before any number was believed

Worth recording, because each of them would have produced a confident wrong
answer:

1. **The snapshot handler is `listener:telemetry`, not `sse:onmessage`.** The
   dashboard subscribes with `addEventListener`, so a summary that looked for
   the `onmessage` accessor reported zero per-snapshot cost for every view.
2. **Engine counters were decorating a copy.** `page.evaluate()` returns a
   structured clone, so assigning the CDP metrics onto the returned sample never
   reached the array the report reads. The samples looked empty.
3. **The same scenario reported two different DOM sizes**, 443 nodes or 1793,
   run to run. That was not noise, and chasing it produced this audit's first
   finding — see §3.

The two traps the previous investigation recorded still hold and are still in
that directory's README: a treatment written as an uninvoked arrow function
silently does nothing, and Firefox's software-rendering switch cannot be
confirmed from inside the page.

---

## 2. Scaling: 2 → 12 devices, four views, both animation modes

Thirty-two cases, ten seconds each — five snapshots at the two-second feed
interval — Chromium headed on a GPU. Raw data:
[`profile-audit-scale-chromium-2026-09-05.json`](profile-audit-scale-chromium-2026-09-05.json).

**Nothing scales super-linearly.** Six times the devices costs, at worst, 4.9×:

| view | animation | per snapshot, 2 → 12 devices | ratio | DOM nodes 2 → 12 | ratio |
|---|---|---:|---:|---:|---:|
| aggregated | off | 2.5 → 1.7 ms | 0.68× | 1225 → 4065 | 3.3× |
| aggregated | normal | 1.3 → 2.0 ms | 1.5× | " | " |
| energy | off | 7.6 → 8.1 ms | 1.07× | 1647 → 4487 | 2.7× |
| energy | normal | 11.0 → 7.2 ms | 0.65× | " | " |
| devices | off | 4.8 → 13.2 ms | 2.75× | 1580 → 6070 | 3.8× |
| devices | normal | 3.2 → 5.6 ms | 1.75× | " | " |
| **control** | **off** | **7.2 → 35.1 ms** | **4.88×** | 1225 → 4065 | 3.3× |
| control | normal | 5.1 → 11.8 ms | 2.31× | " | " |

The aggregated and energy views are **flat** — their cost does not depend on
how many devices exist. The devices view is strongly sub-linear. Only the
control view approaches linear, and it is the only view where per-snapshot cost
reaches tens of milliseconds.

Frame rate and responsiveness never became the limit on this machine:

| | worst of all 32 cases |
|---|---|
| frames per second | 130.5 (control, 12 devices) — of a 144 Hz display |
| frame time p95 | **7 ms in every single case** |
| event-loop lag p95 | 8.8 ms (control, 12 devices, animation on) |
| layout count | 5–10 per ten seconds, in every case |

Layout is effectively gone: 5 to 10 layouts per ten-second window across the
whole matrix, which is the previous investigation's fix holding.

### Where the nodes are

The per-container breakdown is where the scaling actually lives, and it is not
where it looks:

| on screen | total nodes | `flowSvg` | `deviceFlowView` | `deviceGrid` | **`controlExplainView`** | `energyStatsView` |
|---|---:|---:|---:|---:|---:|---:|
| aggregated, 2 dev | 1225 | 95 | 0 | 0 | **786** | 6 |
| aggregated, 12 dev | 4065 | 95 | 0 | 0 | **3606** | 6 |
| devices, 12 dev | 6070 | 95 | 733 | 1272 | **3606** | 6 |
| energy, 12 dev | 4487 | 95 | 0 | 0 | **3606** | 428 |
| control, 12 dev | 4065 | 95 | 0 | 0 | 3606 | 6 |

On the aggregated view at twelve devices, **3606 of 4065 nodes — 89 % — belong
to the control view, which is not on screen**. The view that *is* on screen is
the 95-node flow SVG. And the control subtree is the only part of the document
that grows with device count on the aggregated and energy views: 786 → 1350 →
2478 → 3606 at 2 → 4 → 8 → 12 devices.

That subtree is not there because the user visited the control view. It is
there because something builds it regardless — which is §3.

### The animation's cost is style recalculation, and it is the largest number here

Chromium's own counters, same thirty-two cases, milliseconds of
`RecalcStyleDuration` per ten-second window:

| view | 2 dev | 4 dev | 8 dev | 12 dev | recalcs per 10 s |
|---|---:|---:|---:|---:|---:|
| aggregated, animation **off** | 6 | 5 | 7 | 5 | 10 |
| aggregated, animation **on** | 596 | 820 | 786 | 580 | ~1410 |
| devices, animation **off** | 18 | 24 | 30 | 46 | 10 |
| devices, animation **on** | **1019** | **1466** | **1718** | **2214** | ~1395 |
| control, animation **off** | 27 | 42 | 66 | 80 | 5 |
| control, animation **on** | 488 | 633 | 881 | 1132 | ~1338 |
| energy, animation **off** | 26 | 27 | 26 | 22 | 5 |
| energy, animation **on** | 33 | 21 | 12 | 20 | **5** |

Two things are visible at once.

The **count** is ~1400 per ten seconds in every animated view — one style
recalculation per frame at 140 fps — and it does not move with device count.
The **duration** does: 1019 → 2214 ms on the devices view from two devices to
twelve. That is the signature of a per-element cost: the same number of passes,
each walking more animated elements.

And the energy view is the control group that makes it certain. It has no flow
animation at all, so it records **five** recalculations with the animation
"on", exactly as many as with it off. Nothing else about that view differs.

At twelve devices the devices view spends **2.2 seconds of every ten** in style
recalculation. It still holds 137 fps, because this machine has the headroom —
but that is the single largest main-thread cost left on the page, and §5 asks
what buys it.

### Long tasks: one view, and only past eight devices

Chromium's own long-task observer (>50 ms) across the same thirty-two cases:

| view | 2 dev | 4 dev | 8 dev | 12 dev |
|---|---:|---:|---:|---:|
| aggregated | 0 | 0 | 0 | 0 |
| devices | 0 | 0 | 0 | 0 |
| energy | 0 | 0 | 0 | 0 |
| **control** | 0 | 0 | **2–4** (117–224 ms) | **5** (324–336 ms) |

Five long tasks in a ten-second window is **one per snapshot**, averaging 67 ms
at twelve devices. That is the one place in this audit where an ordinary
snapshot exceeds a browser-defined responsiveness threshold, and it is the same
view §2 identified as the only one that scales.

The harness's engine-neutral stand-in — gaps beyond 12 ms in a self-rescheduling
zero timeout — is **not** usable at this resolution and is reported here only so
nobody reads it as signal: every case, including the cheapest, contains one gap
of 215–276 ms, so `blockingMs` never falls below ~150 ms whatever the page is
doing. Where Chromium's real long-task entries exist they are the number to
read; on Firefox, which has no such observer, this audit reports per-snapshot
main-thread time instead and does not claim a long-task count.

---

## 3. What a view does while it is not on screen

The dashboard's live path is careful about this: `renderSnapshot()` renders only
`state.flowView`, and
[`tests/test_dashboard_perf_guardrails.py`](../../tests/test_dashboard_perf_guardrails.py)
pins it — a snapshot arriving while Analytics is up leaves the device grid, the
device flow, the energy stats and the control panel untouched.

There is a second path into those renderers, and it is not gated.

```js
// app.js — loadAuthStatus() and loadRuntimeState(), both at boot
if (state.snapshot) renderControlExplain(state.snapshot, { forceRuntimeEditor });
```

```js
// app.js — initDashboardApp()
setInterval(loadAuthStatus, 60000);
// Periodic refresh skips fetching while a panel is off-screen, the tab is
// backgrounded (lazy loading), or the analytics chart is zoomed.
setInterval(() => {
  if (analyticsShouldAutoRefresh()) loadAnalytics(false);
  if (historyVisible()) loadHistory(false);
}, 30000);
```

The thirty-second refresh beside it checks visibility; the sixty-second auth
refresh does not, and `renderControlExplain` has no view gate of its own. So
**once a minute, in whatever view is on screen, the control panel is rebuilt
from scratch** — both of its mounts, `innerHTML` on each.

This was not looked for. It was found because the same scenario kept reporting
two different DOM sizes: whether the control subtree exists at all is a race
between the boot fetches and the first snapshot, and once a minute the auth
timer settles it either way. Making the harness wait for the control mount to
appear stalled every case by up to sixty seconds, which is the mechanism
measured directly.

---

## 4. The stylesheet, audited mechanism by mechanism

Static first, because some of what a style audit asks about can be answered by
reading the file, and a benchmark would only confirm it more slowly.

| | count | verdict |
|---|---:|---|
| `:has()` | **0** | not used at all |
| attribute selectors | 5 | trivial |
| `:nth-*`, `:not()`, `:is()`, `:where()` | 7 | trivial |
| selectors, total | 758 | of which **545 are a single compound** |
| deepest selector | **4 components** | `.shell.view-diagnose .flow-panel > .section-heading > h2` |
| `contain:` | **0** | the one opportunity not taken — measured in §7 |
| `mix-blend-mode` | 0 | — |
| `backdrop-filter` | 1 | already removed from the panel rule by the previous study |
| `box-shadow` | 32 | static, never animated |
| `filter` | 25 | static, never animated |

There is no selector-matching problem here to find. The measurement agrees: with
the animation off, Chromium performs **5 to 10 style recalculations per ten
seconds** across every view and device count, costing 5 to 80 ms. A page whose
selectors were expensive could not do that.

Every animation in the file, and the property it moves:

| keyframe | property | compositable | matches |
|---|---|---|---|
| `flowTileRight/Left/Down/Up` | `transform` (via `var(--tile-step)`) | yes | `.flow-tile-inner` — one per visible pipe run, scales with devices |
| `softPulse` | `opacity` | yes | `.solar-sun`, `.inverter-led` — one pair per device row |
| `fillPulse` | `opacity` | yes | `.battery-fill` — one per device row |
| `controlResultRingSlide` | `transform` | yes | `.control-result-ring i` — one per control stage |
| `pipeFlow` | `stroke-dashoffset` | **no** | `.pipe-energy`, which `.flow-tiles-active` sets to `display: none` — the fallback path only |
| `controlResultBorderFlow` | `background-position` | **no** | `.primary-button.compact::after` — **one element**, after the previous investigation moved the twenty-six chips off it |

So after the previous fix, every animation that actually runs moves a property
the compositor can carry. That makes the 1400-recalculations-per-second result
in §2 the interesting one: the properties are right and the cost is still
there, so something other than the property is buying it. The only structural
oddity in the list is the `var()` the four tile keyframes read.

### Measured: one rebuild per minute, in a view that is not on screen

Six cases, **seventy-five second** windows — long enough for the sixty-second
timer to fire once — with each view renderer wrapped so it reports whether its
container was off screen at the time. Raw data:
[`profile-audit-offscreen-chromium-2026-09-05.json`](profile-audit-offscreen-chromium-2026-09-05.json).

| on screen | devices | `renderControlExplain` **off screen** | cost | the on-screen renderers, same window |
|---|---:|---:|---:|---|
| aggregated | 4 | **1 call** | 3.3 ms | — (the aggregated flow is not a renderer of this kind) |
| aggregated | 12 | **1 call** | **57.8 ms** | — |
| devices | 4 | **1 call** | 9.3 ms | `renderDevices` 37 × 2.9 ms, `renderDeviceFlow` 37 × 0.7 ms |
| devices | 12 | **1 call** | 10.7 ms | `renderDevices` 37 × 5.3 ms, `renderDeviceFlow` 37 × 1.0 ms |
| energy | 4 | **1 call** | 20.9 ms | `renderEnergyStats` 37 × 5.6 ms |
| energy | 12 | **1 call** | 34.1 ms | `renderEnergyStats` 37 × 7.4 ms |

`renderControlExplain` was never called **on** screen in any of these — there is
no other path into it — and exactly once off screen in each, at the interval the
code says. Thirty-seven snapshots arrived in each window and none of them
touched it, which is the live path's view gate working correctly.

At twelve devices on the aggregated view that single call is **57.8 ms in one
task**: above the browser's long-task threshold, in the view where §2 measured
no long tasks at all, to rebuild 3606 nodes nobody is looking at.

Each of these is a single sample and they are noisy in the small (3.3 ms at four
devices against 9.3 ms in another view). What is not noisy is the *count*: one,
in every window, in every view.

---

## 5. Full rebuilds, and a measurement that would have lied

Every view renderer replaces a whole panel's `innerHTML` on every snapshot.
Whether that is waste depends on whether the markup it writes differs from the
markup already there, so the harness patched the `innerHTML` setter to count
both — and, in a second variant, to skip the writes that would replace a
subtree with itself.

Sixteen cases, Chromium, animation on, five snapshots per window:

| view | devices | write attempts / 10 s | of which identical | `innerHTML` time, count → guard | attributed work, count → guard |
|---|---:|---:|---:|---|---|
| aggregated | 4 | 50 | **0** | 4.7 → 5.0 ms | 35.3 → 37.9 ms |
| aggregated | 12 | 50 | **0** | 3.4 → 7.6 ms | 25.6 → 55.5 ms |
| devices | 4 | 75 | **0** | 20.6 → 14.7 ms | 113.4 → 90.2 ms |
| devices | 12 | 115 | **0** | 16.3 → 23.3 ms | 123 → 149.8 ms |
| control | 4 | 60 | 10 | 28.5 → **2.5 ms** | 65.6 → **13.5 ms** |
| control | 12 | 60 | 10 | 56.3 → **1.9 ms** | 128.8 → **21.7 ms** |
| energy | 4 | 55 | 5 | 36.8 → **4.7 ms** | 99.5 → **25.9 ms** |
| energy | 12 | 55 | 5 | 26.7 → **6.5 ms** | 78.7 → **39.3 ms** |

The two views with no identical writes are the control group and they behave
like one: the guard changes nothing beyond run-to-run noise, in both directions.

**And the control and energy numbers are not usable as they stand.** The
preview server builds its snapshot from a fixed scenario and changes only the
`timestamp` between deliveries, so every watt in `control_explain` and every
kilowatt-hour in `energy_stats` is byte-identical from one snapshot to the next.
A real installation's are not. Reported as a saving, "56.3 → 1.9 ms" would be a
measurement of the fixture.

What the run does establish is the mechanism — skipping an identical write does
remove its cost — and it points at the one write that is redundant for a reason
that is *not* the fixture:

```js
function renderControlExplain(snapshot, options = {}) {
  ...
  if (options.forceRuntimeEditor || !isRuntimeEditorEditing()) {
    renderRuntimeEditorMount();      // runtimeControlPanel() reads state.runtime
  }                                  // and state.auth. No snapshot touches either.
  renderControlExplainMount(snapshot);
}
```

`runtimeControlPanel()` takes no snapshot and reads none. It is rebuilt twice a
second from data that changes only when `/api/runtime` is re-fetched — and in
the read-only preview used above it collapses to a single line, so the run
cannot show what it costs. §6 measures it authenticated, where it is a form per
device.

### What `renderRules` costs, since it runs in every view

The aggregated view's fifty writes per ten seconds are `renderRules` and nothing
else: it clears its list and creates nine fixed rows on every snapshot, in every
view, whatever the snapshot contains. Ten `innerHTML` writes per snapshot, and
the harness charges them **3.4 to 7.6 ms per ten seconds — about one millisecond
per snapshot**. That is the whole of the aggregated view's DOM work. It is
measurable, it is avoidable, and it is not worth avoiding.

---

## 6. The control view as an authenticated operator sees it

Everything above runs the preview's read-only scenario, where
`runtimeControlPanel()` returns a single line — "Read-only mode. Dashboard
authentication is not configured." With authentication configured it returns a
form per device, plus the winter and Home Assistant blocks. That is the shape
the control view actually has for anyone who can write to the EMS, and it is
the half that is rebuilt from data no snapshot touches.

---

## 7. What buys the per-frame style recalculation

§2 established that with the animation running Chromium recalculates style once
per frame, and that the cost of each pass grows with device count. Thirty cases
turned one animation off at a time — through the CSSOM, because the dashboard's
`style-src 'self'` refuses a generated stylesheet — and asked which one it was.

The control view answers first, and unambiguously:

| control view, animation on | style recalcs / 10 s | style time / 10 s | fps |
|---|---:|---:|---:|
| 4 devices, as shipped | 1968 | 813 ms | 136.4 |
| 4 devices, **result ring stopped** | **6** | **54 ms** | 137.3 |
| 12 devices, as shipped | 1979 | 1731 ms | 132.9 |
| 12 devices, **result ring stopped** | **6** | **102 ms** | 133.3 |

One CSS rule. `.control-result-ring i` — the travelling border the previous
investigation put on the control-stage result chips — and with it stopped the
control view's style recalculation goes from two thousand passes per ten
seconds to **six**.

The aggregated view answers differently. There the tile animation carries most
of the *duration* (733 → 287 ms with the tiles stopped) but not the count, which
stays near two thousand in every treatment. So more than one thing is running,
and the first pass could not say what the remainder was. §7b adds the floor —
every animation in the document stopped — so the remainder is attributed rather
than left over.

### Carrying those nodes, as opposed to building them, costs nothing

Before treating "89 % of the document belongs to a view nobody is looking at" as
a cost, it had to be one. The control subtree was emptied at runtime and the
same scenario measured again:

| | nodes removed | DOM nodes | per snapshot | attributed work | style time | fps |
|---|---:|---:|---:|---:|---:|---:|
| aggregated, 4 dev, kept | — | 1793 | 1.2 ms | 20.6 ms | 501 ms | 139.8 |
| aggregated, 4 dev, **dropped** | 1350 | 443 | 2.3 ms | 34.8 ms | 729 ms | 136.8 |
| aggregated, 12 dev, kept | — | 4065 | 1.5 ms | 25.6 ms | 763 ms | 139.1 |
| aggregated, 12 dev, **dropped** | 3606 | 459 | 0.8 ms | 13.2 ms | 461 ms | 139.4 |
| energy, 4 dev, kept | — | 2215 | 5.6 ms | 28.7 ms | 19 ms | 138.4 |
| energy, 4 dev, **dropped** | 1350 | 865 | 8.4 ms | 42.3 ms | 22 ms | 137.2 |
| energy, 12 dev, kept | — | 4487 | 8.3 ms | 42.7 ms | 27 ms | 136.0 |
| energy, 12 dev, **dropped** | 3606 | 881 | 4.0 ms | 20.5 ms | 13 ms | 138.1 |

Removing up to 3606 nodes moves nothing in a consistent direction — the
per-snapshot cost goes up in two cases and down in two. **An off-screen
`[hidden]` subtree is not laid out, not painted and not walked**, so its size
does not appear in any per-snapshot number.

That matters for what the finding in §3 actually is. It is **not** "the document
is too big". It is one avoidable long task per minute, in a view where nothing
else produces one. The node count is how the defect was found, not what it
costs.

---

## 7b. The floor: every style recalculation on this page is an animation

Thirty more cases added the measurement the first pass was missing — every
animation in the document stopped at once — so the remainder is attributed
rather than left over. Chromium, headed, ten seconds. `anims` is
`document.getAnimations().length`, counted in the same run.

| view | dev | treatment | running animations | style recalcs / 10 s | style time / 10 s | paints | fps |
|---|---:|---|---:|---:|---:|---:|---:|
| aggregated | 4 | as shipped | 12 | 2003 | 704 ms | 45 | 140.8 |
| aggregated | 4 | **all animation off** | **0** | **12** | **8 ms** | 45 | 138.6 |
| aggregated | 4 | tiles off | 3 | 2007 | 309 ms | 45 | 140.8 |
| aggregated | 12 | as shipped | 12 | 2004 | 698 ms | 45 | 140.4 |
| aggregated | 12 | **all animation off** | **0** | **12** | **4 ms** | 45 | 140.1 |
| control | 4 | as shipped | 26 | 2025 | 871 ms | 175 | 135.9 |
| control | 4 | **all animation off** | **0** | **6** | **43 ms** | 175 | 135.9 |
| control | 4 | **result ring off** | **0** | **6** | **49 ms** | 175 | 136.3 |
| control | 12 | as shipped | 66 | 1974 | 1809 ms | 275 | 128.8 |
| control | 12 | **all animation off** | **0** | **6** | **92 ms** | 275 | 133.3 |
| control | 12 | **result ring off** | **0** | **6** | **98 ms** | 275 | 133.1 |
| devices | 4 | as shipped | 48 | 1996 | 1696 ms | 460 | 139.4 |
| devices | 4 | **all animation off** | **0** | **12** | **20 ms** | 460 | 138.1 |
| devices | 4 | tiles off | 12 | 1984 | 394 ms | 460 | 138.8 |
| devices | 12 | as shipped | **144** | 2028 | **2920 ms** | 1260 | 137.5 |
| devices | 12 | **all animation off** | **0** | **12** | **37 ms** | 1260 | 138.8 |
| devices | 12 | tiles off | 36 | 2018 | 769 ms | 1260 | 134.6 |

Three things follow, and the third is the one that matters.

**All of it is the animations.** With every keyframe stopped the page performs
six to twelve style recalculations per ten seconds costing 4 to 92 ms. With them
running it performs about two thousand, costing up to 2.9 seconds. There is no
other source.

**The cost tracks the number of running animations, not the device count.** The
devices view at twelve devices runs 144 animations and pays 2920 ms; the
aggregated view runs 12 and pays 698 ms at the same device count. Stopping the
tiles on the devices view takes 144 animations down to 36 and the cost from
2920 ms to 769 ms — the remaining 36 are the three SVG pulses per device row.

**No treatment changed the paint count.** 45, 175, 275, 460, 1260 — identical in
every arrangement of every view. Nothing here is repainting; the entire cost is
style. That is the distinction the previous investigation's fix was about, and
it holds: after it, no animation on this page animates a paint property.

### What this pass corrected

- **The compact button's border is not implicated.** `controlResultBorderFlow`
  still animates `background-position` on `.primary-button.compact::after`, and
  turning it off changes nothing in any view — because those buttons live in the
  diagnose, logs and analytics panels, which are `[hidden]` in the views
  measured. A hidden element does not animate.
- **The `var()` in the tile keyframes is not the cost.** Replacing it with a
  constant in the keyframe itself changes nothing (752 ms against 733 ms). The
  pipe study credited a 31–46 % saving to expressing the keyframes with literal
  values through the Web Animations API; this says the saving was not the
  literals. Whatever WAAPI bought, it bought it by being a different path.
- **And it is not inherent to the tile renderer.** The pipe study's lab page,
  running the same tile construction, recorded **five** style recalculations in
  two and a half seconds. The dashboard records two thousand in ten. Same
  renderer, different page.

---

## 8. Visible, unfocused, and hidden are three different pages

Chromium, eight devices, animation on, ten seconds. "Hidden" here means
`document.hidden` was overridden to `true`, which exercises the dashboard's own
deferral path; Playwright reports a non-foreground page as *visible*, so that
path never engages on its own in this harness.

| | `document.hidden` | DOM mutations | per snapshot | attributed work | app rAF callbacks | fps |
|---|---|---:|---:|---:|---:|---:|
| devices, focused | false | 1645 | 4.5 ms | 85.5 ms | 15 | 139.2 |
| devices, **hidden** | true | **20** | **0.3 ms** | **1.4 ms** | **0** | 141.9 |
| devices, **unfocused** | false | 1645 | **9.3 ms** | **250 ms** | 14 | **1.0** |
| control, focused | false | 365 | 9.6 ms | 48.6 ms | 5 | 131.8 |
| control, **hidden** | true | **20** | **0.1 ms** | **0.8 ms** | **0** | 142.6 |
| control, **unfocused** | false | 365 | **28.5 ms** | **143.6 ms** | 5 | **1.0** |

**The hidden-tab path works, completely.** Twenty mutations instead of 1645,
0.1–0.3 ms per snapshot instead of 4.5–9.6, no animation frames at all. A
dashboard in a background tab costs essentially nothing. (Its style-recalculation
column stays high because the override only tells the *page* it is hidden — the
browser still composites it and keeps the animations running. A real background
tab would throttle those too, so that column is an artifact of the instrument
and is not reported.)

**The unfocused window is the interesting one.** The page still believes it is
visible, so it renders every snapshot in full — the same 1645 mutations — but
the browser has throttled its animation frames to **1 fps**. And that makes each
snapshot **two to three times more expensive**: 4.5 → 9.3 ms on the devices
view, 9.6 → 28.5 ms on the control view, with attributed work going 85.5 → 250
and 48.6 → 143.6.

The mechanism is the one the previous investigation named. Work costs more when
nothing has flushed style since the last time: with animation frames arriving at
144 Hz the tree is clean when the snapshot handler runs, and at 1 Hz it is not.
A window that is visible but not in front gets the worst of both — full
rendering work, at the price the throttled case pays.

This is the closest thing on Linux to the reported macOS symptom, and it was not
visible in the previous investigation's numbers, which were taken at four
devices and recorded no frame rate for that arrangement. A like-for-like
re-check is in §8b.

### 6b. The authenticated control view, and an instrument that could not answer

With `write-mode`, the runtime editor becomes a form per device and the control
subtree grows from 1350 to 1609 nodes at four devices, and from 3606 to 4209 at
twelve. The view gets correspondingly more expensive:

| control view | per snapshot | `innerHTML` time / 10 s | attributed work |
|---|---:|---:|---:|
| read-only, 4 devices | 6.7 ms | 26.4 ms | 59.9 ms |
| read-only, 12 devices | 15.0 ms | 60.2 ms | 135.4 ms |
| **authenticated**, 4 devices | 7.5 ms | 29.6 ms | 67.5 ms |
| **authenticated**, 12 devices | **20.0 ms** | **79.8 ms** | **180.4 ms** |

The hypothesis in §5 — that `renderRuntimeEditorMount()` writes the same markup
every time, because it reads no snapshot — came back **refuted**: in write-mode
the probe recorded `runtimeEditorMount` as *changed* on all five writes.

That refutation does not hold, and the reason is worth recording because it
would have been reported as a result. The probe compares the string being
assigned against `element.innerHTML` **read back**, which is the DOM's
re-serialisation of itself. The runtime editor contains

```js
<input type="checkbox" name="${escapeHtml(name)}" ${value ? "checked" : ""}>
```

and a browser serialises that attribute as `checked=""`. The source string and
the read-back string therefore differ on every write no matter how unchanged the
content is. The instrument cannot answer this question at all; it can only
undercount. `controlExplainMount` compared equal only because its markup happens
to round-trip.

The comparison a guard in the page would actually make is *generated string
against previously generated string*, which never touches the DOM. §6c measures
that.

### 6c. And the authenticated control view does not hold the refresh rate

The same eight cases, read as frame rate rather than as work:

| control view | read-only fps | **authenticated fps** | style time, read-only → authenticated |
|---|---:|---:|---:|
| 2 devices | 136.1 | **92.8** | 460 → 324 ms |
| 4 devices | 136.1 | **53.2** | 637 → 306 ms |
| 8 devices | 132.0 | **38.7** | 1034 → 328 ms |
| 12 devices | 132.5 | **37.2** | 1107 → 433 ms |

At twelve devices an authenticated operator's control view draws at **a quarter
of the display's refresh rate**. And style-recalculation time *falls* as the
frame rate does — 1107 ms down to 433 — which is the signature of a cost that is
not on the main thread: fewer frames means fewer style passes, and the frames
are missing for another reason.

Every other measurement in this audit, and every measurement in the previous
investigation, ran the **read-only** preview. This is the first time the
dashboard has been benchmarked in the state an operator who can actually write
to the EMS sees, and it is the only place in the audit where the page visibly
fails to keep up. §9 isolates the mechanism.

### Every CSS mechanism, switched off one at a time — and none of them costs

Fifteen cases, eight devices, animation on, read-only preview. Each treatment
reports how many rules it changed, so a treatment that matched nothing is
visible rather than indistinguishable from a null result.

| view | treatment | rules changed | fps | per snapshot |
|---|---|---:|---:|---:|
| aggregated | baseline | — | 137.4 | 2.5 ms |
| aggregated | `backdrop-filter: none` | 1 | 138.4 | 1.7 ms |
| aggregated | `box-shadow: none` | 31 | 137.3 | 1.7 ms |
| aggregated | `filter: none` | 14 | 138.8 | 1.3 ms |
| aggregated | **`contain: paint` added** | 91 elements | 138.6 | 1.6 ms |
| devices | baseline | — | 139.8 | 5.3 ms |
| devices | `box-shadow: none` | 31 | 139.8 | 5.1 ms |
| devices | `filter: none` | 14 | 140.6 | 5.7 ms |
| devices | **`contain: paint` added** | 99 elements | 139.5 | 4.8 ms |
| control | baseline | — | 134.7 | 9.0 ms |
| control | `box-shadow: none` | 31 | 132.1 | 17.8 ms |
| control | `filter: none` | 14 | 134.0 | 10.4 ms |
| control | **`contain: paint` added** | 91 elements | 134.3 | 9.8 ms |

Frame time p95 is **7 ms in all fifteen**. Nothing moves outside run-to-run
noise, in either direction — the control view's 9.0 → 17.8 ms with shadows
*removed* is the clearest illustration that these differences are noise.

That answers the containment question the stylesheet leaves open — there is no
`contain:` anywhere in it — with a measurement rather than a principle: adding
paint containment to the ninety-odd repeating cards buys nothing here. It is
worth knowing that this is a statement about the read-only views; §9 asks the
same questions where the frame rate actually falls.

---

## 9. Charts and the polling views: a confirmation, not an investigation

The previous investigation ruled these out; this re-checks the three things that
could have changed and stops.

| view | per snapshot | attributed work / 10 s | DOM nodes | fps |
|---|---:|---:|---:|---:|
| aggregated | 1.9 ms | 30.2 ms | 1793 | 138.1 |
| energy | 6.6 ms | 33.4 ms | 2215 | 137.1 |
| **analytics** (uPlot canvas) | **1.2 ms** | **6.5 ms** | 1846 | 139.1 |
| **logs** (own 2 s poll) | **0.8 ms** | **4.6 ms** | 1794 | 141.2 |

The two views with a chart and a timer of their own remain the **cheapest views
in the dashboard**, by a factor of five.

And the chart does not scale with device count, which is the specific thing
worth confirming because its device selector is populated from the snapshot:

| analytics | per snapshot | work / 10 s | DOM nodes | fps |
|---|---:|---:|---:|---:|
| 2 devices | 1.1 ms | 9.6 ms | 1278 | 140.2 |
| 12 devices | 1.3 ms | 11.5 ms | 4118 | 139.9 |

Six times the devices, 3.2× the document, and 1.9 ms more work per ten seconds.
The chart is fed by its own analytics query and the live path only writes text
into the KPI cards — `renderAnalyticsLiveKpis` sets `textContent` on the cards
whose spec is marked live and touches nothing else. Nothing to reopen.

---

## 10. The mechanism behind the authenticated control view

Fourteen cases, `write-mode`, one mechanism removed at a time, with the trace on
so the paint count is visible.

| control view, authenticated | fps | frame p95 | **paints / 10 s** | raster tasks | style time |
|---|---:|---:|---:|---:|---:|
| 4 devices, as shipped | 51.9 | 27.8 ms | **4431** | 1621 | 417 ms |
| 4 devices, `filter: none` (14 rules) | 51.7 | 27.8 ms | 4423 | 1618 | 420 ms |
| 4 devices, `box-shadow: none` (31) | 45.2 | 27.9 ms | 3815 | 1387 | 356 ms |
| 4 devices, `backdrop-filter: none` (1) | 52.5 | 27.8 ms | 4415 | 1612 | 394 ms |
| 4 devices, **result ring stopped** | 51.5 | 27.8 ms | 4455 | 1632 | 144 ms |
| 4 devices, **every animation stopped** | **136.1** | **7 ms** | **175** | **25** | 55 ms |
| 12 devices, as shipped | 35.7 | 34.8 ms | **5912** | 1101 | 607 ms |
| 12 devices, **result ring stopped** | 35.4 | 34.8 ms | 5976 | 1113 | 225 ms |
| 12 devices, **every animation stopped** | **133.6** | **7 ms** | **200** | **30** | 108 ms |
| *read-only, 4 devices, as shipped* | *136.3* | *7 ms* | *175* | *160* | *876 ms* |
| *read-only, 12 devices, as shipped* | *132.6* | *7 ms* | *275* | *160* | *1771 ms* |

The read-only rows at the bottom are the same view with the same animations, and
they paint **175 and 275** times per ten seconds. Authenticated, the same view
paints **4431 and 5912** — twenty-five and twenty-one times as many — and loses
three quarters of its frame rate. Stopping every animation restores both at
once: 136 fps and 175 paints.

Filters, shadows and `backdrop-filter` are all cleared: none of them moves it.
**The result ring is cleared too** — stopping it leaves the frame rate exactly
where it was while cutting style time to a third, which is a clean separation of
the main-thread cost from the compositor one.

So it is an animation, it is not the ring, and it exists only when
authentication is configured. There is exactly one candidate:

```js
function runtimeSubmit(label = "Apply") {
  return `<button class="primary-button compact" type="submit">${escapeHtml(label)}</button>`;
}
```

`runtimeStageCard()` puts one of those in every card the runtime editor renders
— one per device plus system, winter and Home Assistant, so **fifteen at twelve
devices** — and `.primary-button.compact::after` animates
`controlResultBorderFlow`, which moves `background-position`. A paint property,
on a masked pseudo-element, multiplied by the device count.

That construction was kept deliberately by the previous investigation:

> `.primary-button.compact::after` uses the same keyframe and keeps the old
> construction on purpose. It is a single element and measured free.

It was a single element **in the read-only preview**, which is the only state
that had ever been benchmarked. §10b is the direct A/B.

### 6c². What the runtime editor actually costs, measured the right way

Wrapping the two mounts and comparing **the string each one generates against
the string it generated last time** — never touching the DOM, so serialisation
cannot interfere:

| control view | `renderRuntimeEditorMount` | of which unchanged | `renderControlExplainMount` | of which unchanged |
|---|---:|---:|---:|---:|
| read-only, 4 devices | 5 calls, 0.5 ms | **5** | 5 calls, 28.5 ms | 5 |
| read-only, 12 devices | 5 calls, 0.0 ms | **5** | 5 calls, 82.4 ms | 5 |
| authenticated, 4 devices | 5 calls, **6.5 ms** | **5** | 5 calls, 24.4 ms | 5 |
| authenticated, 12 devices | 5 calls, **16.2 ms** | **5** | 5 calls, 78.3 ms | 5 |

The original hypothesis holds after all: **`runtimeControlPanel()` produces a
byte-identical string on every snapshot**, in every scenario and at every device
count. The innerHTML probe's "changed" verdict was the serialisation artifact
and nothing else.

Authenticated at twelve devices that is **3.2 ms of the main thread per
snapshot**, twice a second, to rebuild a form nobody asked to change — and it
destroys and recreates every `<input>` in the runtime editor while doing it.

The explain mount also reports "unchanged" in all four, but that one **is** the
fixture: `control_explain` carries live watts that a real installation changes
between snapshots. Its 4.9–16.5 ms per snapshot is not waste, and no guard
should be built on this row.

### 8b. The unfocused window at four devices

Repeating §8's arrangement at the device count the previous investigation used,
with the animation both on and off. Single samples; the per-snapshot column is
noisy at this size and the fps and mutation columns are not.

| devices view | fps | per snapshot | attributed work | DOM mutations | style time |
|---|---:|---:|---:|---:|---:|
| in front, animation on | 136.4 | 3.1 ms | 60.0 ms | 985 | 1335 ms |
| **behind**, animation on | **1.0** | 5.5 ms | 94.2 ms | **985** | 35 ms |
| in front, animation off | 137.1 | 7.1 ms | 119.1 ms | 985 | 33 ms |
| **behind**, animation off | **1.0** | 10.4 ms | 113.0 ms | **985** | 22 ms |

| control view | fps | per snapshot | attributed work | DOM mutations | style time |
|---|---:|---:|---:|---:|---:|
| in front, animation on | 134.1 | 7.7 ms | 38.7 ms | 365 | 678 ms |
| **behind**, animation on | **1.0** | **21.2 ms** | 106.8 ms | **365** | 64 ms |
| in front, animation off | 133.2 | 18.5 ms | 93.2 ms | 365 | 65 ms |
| **behind**, animation off | **1.0** | 8.2 ms | 41.7 ms | **365** | 82 ms |

Two things are solid across all eight cases and one is not.

**Solid: an unfocused window's animation frames are throttled to 1 fps**, and
**the page does not notice** — the mutation count is identical to the focused
case in every pair, because `document.hidden` stays false and the dashboard's
deferral never engages.

**Solid: the style-recalculation cost collapses with the frame rate** — 1335 ms
down to 35 — which is simply the animation ticking 140 times less often.

**Not solid: what that does to per-snapshot cost.** With the animation on it
rises (3.1 → 5.5 ms, 7.7 → 21.2 ms); with it off, one pair rises and one falls.
These are single samples. What the eight-device run in §8 measured — a
consistent two- to three-fold rise — is not reproduced cleanly at four devices,
and this audit does not claim it as a general result. `--repeat 3` on the
`unfocused` matrix is the run that would settle it.

---

## 11. Firefox

The same thirty-two cases, Firefox headed on the same GPU and the same display.

| view | dev | fps (off / on) | frame p95 | per snapshot (off / on) | lag p95 (off / on) |
|---|---:|---|---:|---|---|
| aggregated | 2 | 142.8 / 143.4 | 7.4 / 7.3 | 3.6 / 1.8 ms | 1 / 1 ms |
| aggregated | 12 | 142.9 / 142.6 | 7.4 / 7.3 | 4.0 / 2.4 ms | 1 / 1 ms |
| devices | 2 | 141.9 / 143.1 | 7.3 | 10.2 / 3.8 ms | 1 / 1 ms |
| devices | 12 | 140.7 / 140.2 | 7.4 / 7.3 | 15.4 / 11.8 ms | 2 / 2 ms |
| energy | 12 | 141.1 / 141.3 | 7.4 | 13.4 / 13.0 ms | 2 / 1 ms |
| control | 2 | 139.8 / 140.4 | 7.4 | 16.6 / 5.2 ms | 1 / **7** ms |
| control | 4 | 139.5 / 136.9 | 7.4 / 7.1 | 19.8 / 7.2 ms | 1 / **10** ms |
| control | 8 | 138.5 / 133.0 | 7.4 / **13.2** | 17.8 / 13.8 ms | 1 / **13** ms |
| control | 12 | 137.5 / 132.2 | 7.4 / 7.2 | 19.8 / 17.0 ms | 2 / **12** ms |

**Firefox holds the refresh rate everywhere**: 132.2 to 143.6 fps across the
whole matrix, frame p95 7.1–7.4 ms in thirty-one of thirty-two cases. The DOM
node counts are identical to Chromium's, which is the cross-check that the
deterministic start state holds in both engines.

Two differences are real and both are in the control view.

**Chromium scales there and Firefox does not.** With the animation off, the
control view's per-snapshot cost in Chromium runs 7.2 → 13.7 → 26.0 → 35.1 ms
across 2 → 12 devices. Firefox runs **16.6 → 19.8 → 17.8 → 19.8** — flat, and
higher at two devices than Chromium is. Whatever Chromium pays per device there,
Firefox does not.

**Firefox's event-loop lag rises with the animation on, and only there**: 1–2 ms
off against 7–13 ms on, in the control view alone. Chromium's stays at 2–9 ms
throughout. This is the one place where the animation is measurably felt as
responsiveness rather than as throughput, and it is the engine the original
complaint was about.

### 11b. The same animations cost the two engines different things

Thirty cases per engine, one animation stopped at a time.

| Firefox | fps | attributed work / 10 s | **lag p95** | running animations |
|---|---:|---:|---:|---:|
| control, 4 dev, as shipped | 136.7 | 44 ms | **11 ms** | 26 |
| control, 4 dev, result ring stopped | 139.4 | **107 ms** | **2 ms** | 0 |
| control, 4 dev, every animation stopped | 139.9 | **99 ms** | **1 ms** | 0 |
| control, 12 dev, as shipped | 132.6 | 81 ms | **12 ms** | 66 |
| control, 12 dev, result ring stopped | 136.9 | **145 ms** | **1 ms** | 0 |
| control, 12 dev, every animation stopped | 135.6 | **195 ms** | **1 ms** | 0 |
| aggregated, 12 dev, as shipped | 143.3 | 44 ms | 1 ms | 12 |
| aggregated, 12 dev, every animation stopped | 143.0 | 53 ms | 1 ms | 0 |
| devices, 12 dev, as shipped | 140.7 | 200 ms | 3 ms | 144 |
| devices, 12 dev, every animation stopped | 139.7 | 263 ms | 1 ms | 0 |

Firefox's frame rate does not move for any treatment in any view — 131.5 to
143.6 across all thirty. What moves is **event-loop lag in the control view**,
and the ring is the whole of it there too: 11 → 2 ms at four devices, 12 → 1 ms
at twelve, from the same single rule that cleared Chromium's style time.

So the two engines charge for the same construction differently:

| | Chromium | Firefox |
|---|---|---|
| frame rate, read-only | unaffected | unaffected |
| frame rate, **authenticated** | **136 → 52 → 36 fps** | *not yet measured* |
| style-recalculation time | up to **2.9 s per 10 s** | not exposed |
| event-loop lag | 2–9 ms | **1–2 ms → 11–13 ms** in the control view |

And one thing holds in both engines and in every view: **stopping the animations
makes per-snapshot main-thread work go up**, often by a factor of two — Firefox
control at twelve devices, 81 ms as shipped against 195 ms with no animation at
all. That is the mechanism the previous investigation named: a running animation
keeps the style tree flushed, and a handler that arrives at a dirty tree pays for
everything accumulated since the last flush. It is why `animation_mode=off` is
not a performance control, restated with a second engine and a wider matrix.

---

## 12. The rendering path, this time verified

The previous investigation could not report a software-rendering result, because
Firefox's `gfx.webrender.software` could not be confirmed to have taken effect
from inside the page. **Chromium can be confirmed**, and by the project's own
rule: the renderer string proves software when it says so, even though it can
never prove hardware.

| | reported renderer | fps | frame p95 | work / 10 s |
|---|---|---:|---:|---:|
| aggregated, animation on, GPU | ANGLE / NVIDIA GTX 1660 Ti | 139.4 | 7 ms | 35.4 ms |
| aggregated, animation on, **`--disable-gpu`** | **ANGLE / SwiftShader** | 141.9 | 7 ms | 12.8 ms |
| aggregated, animation off, GPU | NVIDIA | 139.7 | 7 ms | 42.4 ms |
| aggregated, animation off, **software** | **SwiftShader** | 143.9 | 7 ms | 28.3 ms |
| devices, animation on, GPU | NVIDIA | 136.4 | 7 ms | 65.0 ms |
| devices, animation on, **software** | **SwiftShader** | **123.6** | **13.9 ms** | 51.2 ms |
| devices, animation off, GPU | NVIDIA | 136.1 | 7 ms | 119.3 ms |
| devices, animation off, **software** | **SwiftShader** | 142.3 | 7 ms | 113.0 ms |

This is the first verified software-rendering measurement in this project's
reports, and the result is undramatic: the aggregated view is not slower without
a GPU at all, and the devices view loses about 9 % of its frame rate — but only
with the animation running, which is consistent with everything else here.

**Firefox's software path is still not established** and nothing has changed
about that. `gfx_probe.mjs` still cannot open `about:support` through Playwright,
Firefox still returns the same sanitised renderer string either way, and no
Firefox number in this audit is filed as a software-rendering measurement.

### 10b. The direct A/B: one rule, and it is all of it

| authenticated control view | fps | frame p95 | paints / 10 s | raster tasks | compositor frames |
|---|---:|---:|---:|---:|---:|
| 4 devices, as shipped | 52.4 | 27.8 ms | 4399 | 1609 | 539 |
| 4 devices, **button border stopped** | **136.0** | **7 ms** | **175** | **25** | **1374** |
| 4 devices, every animation stopped | 135.7 | 7 ms | 175 | 25 | 1365 |
| 12 devices, as shipped | 35.6 | 34.8 ms | 5832 | 1086 | 363 |
| 12 devices, **button border stopped** | **135.4** | **7 ms** | **200** | **30** | 1356 |
| 12 devices, every animation stopped | 132.9 | 7 ms | 200 | 30 | 1343 |

Stopping that one rule is **indistinguishable from stopping every animation on
the page**. It is the whole of the loss: the frame rate, the frame time, the
4399 paints, and the 1609 raster tasks.

The same run counted the elements: **12 `.primary-button.compact` at four
devices and 20 at twelve**. The previous investigation's note that the
construction was "a single element and measured free" was true of the page it
was measured on and of no other.

One number in that table runs the other way and is worth reading correctly:
with the button border stopped, style-recalculation time goes **up**, 406 → 1105
ms at four devices. That is not a cost the fix introduces — it is the result
ring finally getting to animate at 136 fps instead of 52, so it ticks nearly
three times as often. The compositor was the bottleneck; removing it lets the
main-thread cost show its real size.

### 10c. Firefox does not have this problem

The same fourteen cases, Firefox:

| authenticated control view | fps | frame p95 | lag p95 | per snapshot |
|---|---:|---:|---:|---:|
| 4 devices, as shipped | 134.9 | 7.2 ms | 13 ms | 9.8 ms |
| 4 devices, result ring stopped | 138.2 | 7.1 ms | **7 ms** | 10.6 ms |
| 4 devices, every animation stopped | 139.5 | 7.3 ms | **2 ms** | 20.0 ms |
| 12 devices, as shipped | 131.7 | 7.1 ms | 13 ms | 20.4 ms |
| 12 devices, every animation stopped | 134.7 | 7.4 ms | **2 ms** | 40.0 ms |
| *read-only, 4 devices* | *136.9* | *7.1 ms* | *10 ms* | *8.2 ms* |
| *read-only, 12 devices* | *132.4* | *7.1 ms* | *12 ms* | *17.4 ms* |

Firefox holds 131.7 to 139.5 fps with authentication configured — the same as
read-only, and the same as every other view it draws. **The collapse is
Chromium-specific**, which is exactly what the previous investigation found for
the same keyframe on the result chips: `background-position` costs Chromium two
thirds of its frame rate and costs Firefox nothing.

That does not make it less worth fixing. It makes it a defect in the engine most
people will open the dashboard in, and one whose fix is already written and
tested in this codebase.

---

## 13. An experiment that does not work, reported as such

The pipe study left one question open and named the experiment that would settle
it: does the flow animation actually reach the compositor? Its test was to block
the main thread for 600 ms and see whether the layer keeps moving, since only a
compositor-driven animation can. It was written for a lab page and never run.

It is now written for the real dashboard, and it was run **with a control** — a
bare fixed `<div>` animated by `element.animate()` with literal `translate3d`
keyframes, no mask, no clipping, `will-change: transform`. If anything on a page
is composited, that is.

All five elements read inside the same 600 ms block:

| element | moved while the main thread was blocked |
|---|---|
| `#ctlPlain` — bare div, WAAPI transform, **the control** | **no** |
| `#ctlMasked` — the same, inside a copy of the ring's masked box | no |
| `#ctlCss` — the same motion, driven by the page's own CSS keyframe | no |
| `.control-result-ring i` — the real ring | no |
| `.flow-tile-inner` — the real flow tile | no |

**The control did not move either.** So the probe cannot distinguish a
composited animation from a main-thread one on this platform: it reads the
transform through `getComputedStyle`, which returns the main thread's own copy
of the style, and that copy cannot be refreshed while the main thread is the
thing being blocked.

The experiment is therefore **inconclusive, and no compositing claim in this
audit rests on it.** It is reported because the pipe study named it as the
missing measurement, and the useful outcome is that it is the wrong measurement:
taking it without a control would have produced five confident "not composited"
verdicts, one of which is provably wrong.

What *is* measured, and does not depend on it, is the paint count. The button
border produces 4399 paints per ten seconds and stopping it removes 4224 of
them; the ring and the tiles change the paint count by nothing at all under any
treatment. Whatever those two are doing, they are not repainting.

---

## 14. Repeated view changes, and three minutes of them

Two runs of three minutes each, sampled every fifteen seconds with garbage
collected before every sample, so a level that keeps climbing is retention
rather than allocation waiting to be reclaimed. One run sits on the aggregated
view; the other rotates `devices → control → energy → aggregated` every two
seconds — **ninety view changes**.

| | static, 3 min | cycling, 3 min (90 switches) |
|---|---|---|
| DOM nodes | 1793 → 1793 (flat, 11 samples) | 2846 → 2846 (flat from the first sample) |
| outstanding listeners | 94 → 94 | **94 → 94** |
| outstanding intervals | 2 → 2 | **2 → 2** |
| observers constructed | 1 `ResizeObserver`, 1 `MutationObserver` | **the same two, no more** |
| `EventSource`s opened / closed | 1 / 0 | **1 / 0** |
| running animations | 12 → 12 | 11 → 11 |
| renderer's live node count | 5622, flat | 8743–8990, no trend |
| renderer's `JSEventListeners` | 95, flat | **95, flat** |
| JS heap after GC | 2.2 → 2.8 MB | 2.5 → 3.4 MB |

**Nothing accumulates.** Ninety view changes add no listeners, no timers, no
observers, no `EventSource`s and no DOM nodes. The cycling run carries about a
thousand nodes more than the static one, and that is the one-off cost of having
visited the devices and energy views once — it appears between the start and the
first sample and never moves again.

And the switches do not get slower:

| switching to | switches | median | worst |
|---|---:|---:|---:|
| devices | 23 | 8.9 ms | 17.4 ms |
| control | 23 | 7.4 ms | 19.3 ms |
| energy | 22 | 2.4 ms | 10.2 ms |
| aggregated | 22 | 1.6 ms | 1.9 ms |

First ten switches: median 6.7 ms. **Last ten: 3.6 ms.** They get faster, not
slower.

The one level that does move is the JS heap, by roughly 0.2 MB per minute in
*both* runs — so it is not caused by view changes. Three minutes is too short to
say whether that is growth or the engine's normal early expansion; §15 runs it
for thirty.

---

## 15. The three changes, and what they measured

All three were implemented after the measurements above, with contract tests
first. Before and after are separate runs of the same matrices — the "before"
taken from a `git worktree` at the pre-change commit with the current harness
copied in, so the only difference is the dashboard itself. **Zero occluded
cases in either run** (see §15d).

### 15a. The submit-button border — the one that matters

`.primary-button.compact::after` animated `background-position`. It now carries
the construction the result chips already used: a `.button-ring` element whose
child is translated, and `@keyframes controlResultBorderFlow` is deleted.

| authenticated control view, as shipped | before | after |
|---|---:|---:|
| 4 devices, fps | 52.8 | **139.2** |
| 4 devices, frame p95 | 27.7 ms | **7 ms** |
| 4 devices, paints per 10 s | 4463 | **175** |
| 12 devices, fps | 36.1 | **133.8** |
| 12 devices, frame p95 | 34.7 ms | **7 ms** |
| 12 devices, paints per 10 s | 5912 | **200** |

**2.6× and 3.7× the frames, a quarter of the frame time, and 96–97 % of the
paints gone.** The second run of the same matrix agrees: 52.1 → 140.4 and
36.7 → 133.0.

The internal controls say the fix is complete rather than partial. Before, the
shipped page ran at 52.8 fps and the same page with *every* animation stopped
ran at 135.5 — a gap of 83 fps. After, the shipped page runs at 139.2 and the
all-animations-stopped variant at 138.9: **the gap is −0.3 fps.** There is no
compositor cost left to find in that view.

Read-only is untouched — 135.8 → 135.7 and 129.2 → 131.3 — which is the check
that the change did not pay for itself somewhere else.

One number moves the other way and is not a cost the change introduced: style
recalculation goes from 392 to 1082 ms per ten seconds at four devices. The page
now draws 2.7× as many frames, so the result ring — which was always
main-thread — ticks 2.7× as often. It is the same animation doing the same thing
more often, and it is §16's remaining item.

### 15b. The control panel built for a view nobody is on

`renderControlExplain` now returns immediately when its container is off screen.
The sixty-second auth refresh still fires; it just no longer rebuilds anything.

| 75-second window | the off-screen rebuild, before → after | document nodes, before → after |
|---|---|---|
| aggregated, 4 devices | 1 call, 5.7 ms → **1 call, 0.0 ms** | 1793 → **453** |
| aggregated, 12 devices | 1 call, **19.4 ms** → **1 call, 0.0 ms** | 4065 → **469** |
| devices, 12 devices | 1 call, 9.8 ms → 1 call, 0.1 ms | 6070 → **2474** |
| energy, 4 devices | 1 call, 17.9 ms → 1 call, 0.1 ms | 2215 → **875** |
| energy, 12 devices | 1 call, 8.6 ms → 1 call, 0.1 ms | 4487 → **891** |

The on-screen renderers in the same windows are unchanged — `renderDevices`
103.3 → 92.5 ms, `renderEnergyStats` 206.7 → 208.8 ms across 37 snapshots — so
nothing was traded away.

§3 established that carrying those nodes costs nothing per snapshot, and that is
still true: the aggregated view's per-snapshot cost is the same before and after
(1.0–2.9 ms against 1.3–2.5 ms across the four device counts). What the smaller
document buys is not speed. It is that a person who never opens the control view
no longer has 3606 nodes of it built, retained and rebuilt every minute — and at
twelve devices that is **an 8.7× smaller document**.

### 15c. The runtime editor rebuilt from unchanged data

`renderRuntimeEditorMount` compares the string it generated against the one it
generated last time, and skips the write when they match.

| control view, per 5 snapshots | before | after |
|---|---:|---:|
| authenticated, 4 devices | 6.6 ms | **0.9 ms** |
| authenticated, 12 devices | **16.5 ms** | **1.6 ms** |
| read-only, 12 devices | 0.5 ms | 0.2 ms |
| the explain mount, authenticated 12 devices | 75.4 ms | 68.8 ms (unchanged, correctly) |

**−90 % at twelve devices.** What remains is `runtimeControlPanel()` still
building the string, which the comparison needs; the parse, the subtree
teardown and the recreation of every `<input>` in the form are gone. The probe
still reports the generated string as identical on all five writes, which is why
the guard fires at all.

### 15d. Everything else, checked for regression

The full thirty-two-case scaling matrix, before against after: frame rate
between 131.5 and 142.2 in both, per-snapshot cost moving in both directions
inside run-to-run noise, no view worse. The only systematic change is the
document size, and only where the control view is not on screen.

**And the after-run had to be taken twice.** The first one reported 1.0 fps and
a 1000 ms frame time in every case that had a running animation — which is not a
regression but Chromium's throttle for a window that is behind another
application, and those headed windows had opened behind an editor. It is a
discrete state, not a slow machine, and nothing in the harness could tell it
from a result. `looks_occluded()` now rejects any case with a frame rate below 5
and a frame time above 500 ms, retries it up to three times, and the report
carries an `occluded_cases` count. Both runs quoted above record **zero**.

That first run also overwrote its own baseline, because the label and the date
made the same filename. Every run in this section is labelled `-before` or
`-after`, and the five datasets the bad run destroyed are not in the tree: what
they contained was the throttle, not a measurement.

The gate has one exception, and it needs one. Two matrices — `hiddentab` and
`unfocused` — put the dashboard behind another page *deliberately*, and there
1 fps is the measurement rather than a failure of it. `looks_occluded()` does
not fire when the scenario asked for the background.

---

## 15e. Thirty minutes, 360 view changes, on the fixed code

Sampled every minute with garbage collected before every sample, rotating
`devices → control → energy → aggregated` every five seconds. Zero occluded
cases.

| | at 1 min | at 15 min | at 30 min |
|---|---:|---:|---:|
| DOM nodes | 2856 | 2856 | **2856** |
| control subtree | 1350 | 1350 | **1350** |
| outstanding listeners | 94 | 95 | 96 |
| outstanding intervals | 2 | 2 | **2** |
| running animations | 11 | 11 | **11** |
| renderer's live node count | 8850 | 8753 | 8842 |
| renderer's `JSEventListeners` | 95 | 95 | **95** |
| JS heap after GC | 2.9 MB | 3.7 MB | 3.8 MB |

Over the whole run: 3 `EventSource`s opened and 2 closed — the preview's stream
is bounded at ten minutes, so the page reconnected twice and closed the previous
one each time, leaving exactly one live. 6 intervals created and 4 cleared,
leaving the same two the page starts with.

**The document does not grow.** 2856 nodes at minute one and 2856 at minute
thirty, through 360 view changes. The listener count moves by two across half an
hour while the renderer's own count stays at 95, which is the difference between
the page-side add/remove tally and what the engine actually holds.

**The heap settles rather than climbs.** 2.9 → 3.7 MB over the first thirteen
minutes and then 3.7 → 3.8 over the next seventeen. The three-minute run in §14
saw the first part of that curve and could not tell growth from the engine's
normal early expansion; thirty minutes can.

There is no leak here.

An earlier attempt at this run died at twenty-nine minutes with "Maximum call
stack size exceeded" — `Math.max(...values)` passes every element as an
argument, and a thirty-minute run collects tens of thousands of them. The
samplers are bounded now and the maximum is a loop. It is recorded because
losing twenty-nine minutes of measurement to the instrument is exactly the kind
of thing that gets quietly retried instead of fixed.

---

## 15f. The first NICE TO HAVE, taken after all

§16 classified the per-frame style recalculation NICE TO HAVE and left it, on
the grounds that it costs no frames. That was a statement about a machine with
headroom, and the harness can remove the headroom by an exact factor.

Expressing the tile motion with literal keyframe values through
`element.animate()` instead of a keyframe that reads `--tile-step`:

| Chromium, style recalculation per 10 s | before | after |
|---|---:|---:|
| aggregated, 12 devices | 912 ms | **599 ms** |
| devices, 12 devices | 2227 ms | **1328 ms** |
| aggregated, 4× CPU throttling | 1192 ms | **736 ms** |
| devices, 4× | 4891 ms | **3351 ms** |
| aggregated, 8× | 2099 ms | **1311 ms** |
| devices, 8× | 4632 ms | **3146 ms** |
| aggregated, 16× | 3687 ms | **2663 ms** |
| devices, 16× | 3806 ms | **2643 ms** |

**−28 % to −40 %, in every row.** And once the headroom is gone it is frames:

| | before | after |
|---|---|---|
| aggregated, 16× | 110.7 fps / p95 **13.9 ms** | **135.0 fps / p95 7.0 ms** |
| devices, 4× | 87.3 fps | **98.0** |
| devices, 8× | 36.6 fps | **40.6** |
| devices, 16× | 11.5 fps | **14.0** |
| aggregated, unthrottled | 136.6 fps | 137.8 |
| devices, unthrottled | 136.8 fps | 139.2 |

Unthrottled nothing moves, which is exactly why this needed a throttled run to
decide. The 16× aggregated result is the cleanest single effect: a view that was
dropping frames returns to the refresh ceiling and halves its frame time. Its
repeat sample read 131.4 fps at a 13.8 ms p95, so the frame-time recovery at 16×
is not perfectly stable across runs; the style-recalculation saving is, in all
eight rows.

**Two things this corrects in the pipe study.** It credited its 31–46 % saving
to literal values replacing a `var()`; replacing the custom property with a
constant in the keyframe changes nothing, so the mechanism is the path and not
the property. And it declined the change to avoid "a second source of truth for
motion" — but the renderer already reads speed, direction, period and appearance
out of the stylesheet and holds them in JavaScript. The keyframes existed only
to consume a custom property that the same code sets. Handing those values to
`element.animate()` is the same source applied differently.

The CSS keyframes remain as the fallback where `element.animate()` is missing,
and the direction class is omitted only when the API drives — otherwise both
animations would run. `animation_mode`, `prefers-reduced-motion` and the idle
state still arrive as `seconds = 0` through `readFlowPipe`, and a contract test
pins that neither route animates then.

## 15g. The second NICE TO HAVE: measured, and not taken

The control view is the only one whose per-snapshot cost scales with device
count, and the remedy on the table is to update it in place instead of replacing
it. §16 declined it because the preview's `control_explain` never changes, so a
skip-if-identical guard could only measure the fixture.

That objection does not apply to in-place updating, whose saving is the parse —
paid whether or not the content changed. So the two halves were charged
separately: building the markup string, and handing it to the parser.

| control view, per snapshot | generate | **write** | markup |
|---|---:|---:|---:|
| read-only, 2 devices | 0.8 ms | **7.2 ms** | 39 947 bytes |
| read-only, 4 devices | 1.0 ms | **7.4 ms** | 68 357 bytes |
| read-only, 8 devices | 1.0 ms | **10.7 ms** | 125 118 bytes |
| read-only, 12 devices | 1.2 ms | **12.3 ms** | 181 902 bytes |
| authenticated, 12 devices | 1.1 ms | **11.4 ms** | 181 902 bytes |

**The parse is 91 % of it.** Deciding what the panel should say costs about a
millisecond; handing 182 KB of markup to the parser twice a second costs twelve.
So the ceiling for in-place updating is real and now measured: about 12 ms per
snapshot at twelve devices, which is 88 % of that view's 14.0 ms.

It is still not taken here, and the reason is no longer a measurement gap. The
change is a reconciler for a 532-line generator across five nested constructs,
in the view an operator reads to understand why the EMS wrote what it wrote, and
a partial update that misses a field shows a stale decision rather than a slow
one. That is a correctness risk of a different kind from anything else in this
audit, and it is worth more than 12 ms per snapshot on hardware that loses no
frames to it.

What would make it safe is a differential test — render the panel both ways for
the same snapshot and assert the resulting markup is identical — and that test
is the first thing to write if it is ever picked up. The number to beat is in
the table above.

---

## 16. Findings

**Finding: the runtime editor's submit buttons animate a paint property, and the
authenticated control view loses three quarters of its frame rate.**
**Evidence:** `runtimeSubmit()` emits `<button class="primary-button compact">`
and `runtimeStageCard()` renders one per card — 12 buttons at four devices, 20 at
twelve, counted in the page. `.primary-button.compact::after` animated
`controlResultBorderFlow`, which moves `background-position`. Disabling that one
rule at runtime was indistinguishable from disabling every animation on the
page; disabling filters, shadows, `backdrop-filter` and the result ring changed
nothing.
**Measured impact:** 52.8 → 139.2 fps at four devices and 36.1 → 133.8 at
twelve, frame p95 27.7/34.7 → 7 ms, paints per ten seconds 4463 → 175 and
5912 → 200.
**Affected browsers/platforms:** Chromium only. Firefox held 131.7–139.5 fps
throughout, authenticated or not.
**Classification:** **FIX NOW**
**Recommendation:** done — the buttons carry the `.button-ring` construction the
result chips already used, and `@keyframes controlResultBorderFlow` is deleted.
A new guardrail fails on *any* keyframe that moves a paint property and is
referenced, so the "it is only one element" reasoning cannot recur.

---

**Finding: `renderControlExplain` has no view gate, and a sixty-second timer
rebuilds the control panel in whatever view is on screen.**
**Evidence:** `loadAuthStatus()` and `loadRuntimeState()` call it directly, and
`initDashboardApp()` runs `setInterval(loadAuthStatus, 60000)`. Instrumenting
each renderer over 75-second windows recorded exactly one off-screen call in
every view and zero on-screen calls. The thirty-second refresh beside it does
check visibility.
**Measured impact:** one rebuild per minute costing 3.3–19.4 ms (57.8 ms in one
sample at twelve devices) — the only task in the aggregated view that has ever
crossed the long-task threshold. It also built and retained 3606 of that view's
4065 nodes at twelve devices.
**Affected browsers/platforms:** both engines; it is application logic.
**Classification:** **FIX NOW**
**Recommendation:** done — the renderer returns early when its container is off
screen, and `setFlowView` already renders the view it switches to. The rebuild
now costs 0.0–0.1 ms and the aggregated view's document is 469 nodes instead of
4065.

---

**Finding: the runtime editor is rebuilt from data no snapshot touches.**
**Evidence:** `runtimeControlPanel()` takes no snapshot and reads only
`state.runtime` and `state.auth`. Comparing each generated string against the
previous one recorded it as byte-identical on every write, in all four
scenarios.
**Measured impact:** 6.6 ms per five snapshots at four devices and 16.5 ms at
twelve, authenticated — plus the destruction and recreation of every `<input>`
in the form twice a second.
**Affected browsers/platforms:** both.
**Classification:** **FIX NOW**
**Recommendation:** done — the generated string is cached and the write skipped
when it matches. 16.5 → 1.6 ms at twelve devices.

---

**Finding: every CSS animation on this page costs a style recalculation per
frame, and the cost scales with how many are running.**
**Evidence:** stopping all animations takes Chromium from ~2000 style
recalculations per ten seconds to 6–12, and from 698–2920 ms to 4–92 ms. The
energy view, which has no flow animation, records 5 either way. The cost tracks
`document.getAnimations().length`: 12 animations → ~700 ms, 26 → 871, 48 →
1696, 66 → 1809, 144 → 2920. No treatment changed the paint count, so nothing is
repainting.
**Measured impact:** up to 2.9 seconds of main thread per ten seconds at twelve
devices on the devices view. In Firefox the same construction costs event-loop
lag instead: 1–2 ms rises to 11–13 ms in the control view, and the result ring
alone accounts for all of it.
**Affected browsers/platforms:** both, differently.
**Classification:** **FIX NOW**, after a throttled run (was NICE TO HAVE)
**Recommendation:** done for the tile animation, which is the largest
contributor. On this desktop it costs no frames, which is why it was declined
first; on a main thread slowed sixteen-fold the aggregated view was dropping
them, and driving the tiles through `element.animate()` returns it to the
refresh ceiling. −28 % to −40 % of style-recalculation time in all eight
throttle/view combinations, and +11 % to +22 % frame rate on the devices view
under load. §15f. The result rings were measured and **not** converted: they are
recreated on every snapshot, so animating them from JavaScript would trade
per-frame style cost for per-snapshot script cost.

---

**Finding: a visible but unfocused window has its animation frames throttled to
1 fps while the page keeps rendering every snapshot in full.**
**Evidence:** with a neighbour page in front, the dashboard reports
`document.hidden === false`, produces the identical mutation count (985 and 365
in the two views), and measures 1.0 fps. Its own hidden-tab deferral — which
works completely, taking 1645 mutations down to 20 and 4.5 ms per snapshot down
to 0.3 — never engages.
**Measured impact:** at eight devices, per-snapshot cost rose 4.5 → 9.3 ms
(devices) and 9.6 → 28.5 ms (control). At four devices the direction was not
consistent across four single samples.
**Affected browsers/platforms:** measured on Chromium/Linux. This is the closest
thing here to the reported macOS symptom.
**Classification:** **NEEDS MACOS TEST**
**Recommendation:** do not act on it yet. A page cannot detect occlusion, and
the available signal — `document.hidden` — is correctly false. Any fix would be
a heuristic. The measurement to take first is the same arrangement on the
machine the complaint came from.

---

**Finding: the control view is the only view whose per-snapshot cost scales with
device count, and the only one that produces long tasks.**
**Evidence:** 7.2 → 35.1 ms across 2 → 12 devices with the animation off, while
aggregated and energy are flat and devices is strongly sub-linear. Chromium's
long-task observer records 2–5 tasks per ten seconds at 8 and 12 devices in that
view and zero everywhere else. Firefox does not reproduce the scaling: 16.6 →
19.8 ms, flat.
**Measured impact:** one long task per snapshot at twelve devices, averaging
67 ms.
**Classification:** **NICE TO HAVE**
**Recommendation:** not taken, and no longer for want of a measurement. The two
halves were charged separately: building the markup costs about 1 ms per
snapshot, handing 182 KB of it to the parser costs 12 — **the parse is 91 %**,
so in-place updating has a real 12 ms ceiling at twelve devices (§15g). It is
declined on risk, not on size: a reconciler for a 532-line generator in the view
an operator reads to understand a write decision, where a missed field shows a
stale decision rather than a slow one. Write the differential test first if it
is ever picked up.

---

## 17. What we deliberately did not optimise

- **The off-screen DOM, as a size problem.** Emptying the control subtree —
  1350 to 3606 nodes — moved the per-snapshot cost up in two cases and down in
  two. A `[hidden]` subtree is not laid out, not painted and not walked. The fix
  in §15b shrinks the document as a *consequence*; that was never the reason.
- **`renderRules`.** Nine fixed rows cleared and recreated on every snapshot in
  every view. Charged at 3.4–7.6 ms per ten seconds — about one millisecond per
  snapshot, and the whole of the aggregated view's DOM work. Measurable,
  avoidable, not worth avoiding.
- **Paint containment.** There is no `contain:` anywhere in the stylesheet.
  Adding `contain: paint` to the ninety-odd repeating cards changed nothing in
  any view, in either direction.
- **`box-shadow`, `filter`, `backdrop-filter`.** Switched off one at a time, 31,
  14 and 1 rules respectively. No effect in the read-only views, and no effect
  in the authenticated control view either, where the frame rate actually was
  falling.
- **Selector cost.** 545 of 758 selectors are a single compound, the deepest is
  four components, there is no `:has()`, and with the animation off the whole
  page performs 5–10 style recalculations per ten seconds. There is nothing here
  to find.
- **The flow tile renderer.** Settled by two previous studies and not reopened.
  This audit only refines one of their conclusions (the `var()` keyframe) and
  contradicts none of them.
- **Incremental DOM updating for the control explanation.** See §16. The one
  measurement that would justify it cannot be taken against a fixture whose data
  never changes.
- **The Web Animations API for the flow tiles.** The pipe study measured the
  saving and rejected the trade; nothing here changes that calculus, and the
  `var()` correction makes the saving smaller than it looked.

---

## 18. macOS gaps

Nothing in this audit was measured on a Mac. The harness is built to be run on
one and `scripts/dashboard_profile/README.md` has the commands.

1. **Whether the three fixes help there.** The largest is Chromium-specific and
   the reported symptom was Firefox. The other two are application logic and
   engine-independent, but their sizes on that hardware are unknown.
2. **The unfocused window.** §16's `NEEDS MACOS TEST` finding. macOS composites
   windows differently and a dashboard left open behind another window is
   exactly the arrangement the complaint described.
3. **A minimised window.** Not measurable in this harness at all — Playwright
   reports a non-foreground page as visible, and there is no way to minimise one.
4. **Firefox's software rendering path.** Still unverifiable from inside the
   page; unchanged from the previous investigation. Chromium's *is* now verified
   (§12) and costs the devices view about 9 %.
5. **Weak hardware generally.** Everything here is a GTX 1660 Ti at 144 Hz. The
   `throttle` matrix exists for exactly this and was not re-run after the fixes.

---

## 19. Performance matrix

Chromium and Firefox, headed on a GTX 1660 Ti at 144 Hz, ten-second windows,
five snapshots each. Frame rate / frame p95 / per-snapshot main-thread cost.

| view | devices | Chromium, animation off | Chromium, on | Firefox, off | Firefox, on |
|---|---:|---|---|---|---|
| aggregated | 2 | 140.3 / 7 / 1.8 | 138.4 / 7 / 2.0 | 142.8 / 7.4 / 3.6 | 143.4 / 7.3 / 1.8 |
| aggregated | 12 | 139.6 / 7 / 2.9 | 138.2 / 7 / 2.2 | 142.9 / 7.4 / 4.0 | 142.6 / 7.3 / 2.4 |
| devices | 2 | 137.3 / 7 / 4.4 | 136.8 / 7 / 2.3 | 141.9 / 7.3 / 10.2 | 143.1 / 7.3 / 3.8 |
| devices | 12 | 137.3 / 7 / 22.4 | 137.7 / 7 / 5.3 | 140.7 / 7.4 / 15.4 | 140.2 / 7.3 / 11.8 |
| energy | 12 | 137.1 / 7 / 7.8 | 136.8 / 7 / 8.6 | 141.1 / 7.4 / 13.4 | 141.3 / 7.4 / 13.0 |
| control | 2 | 137.4 / 7 / 12.6 | 136.5 / 7 / 4.8 | 139.8 / 7.4 / 16.6 | 140.4 / 7.4 / 5.2 |
| control | 12 | 131.5 / 7 / 27.3 | 133.7 / 7 / 12.3 | 137.5 / 7.4 / 19.8 | 132.2 / 7.2 / 17.0 |
| analytics | 12 | — | 139.9 / 7 / 1.3 | — | — |
| logs | 4 | 141.2 / 7 / 0.8 | — | — | — |
| **control, authenticated** | 4 | — | **139.2** / 7 / 7.5 | — | 134.9 / 7.2 / 9.8 |
| **control, authenticated** | 12 | — | **133.8** / 7 / 16.5 | — | 131.7 / 7.1 / 20.4 |

The authenticated rows are after the fix. Before it they were 52.8 and 36.1 fps
in Chromium, at frame p95 27.7 and 34.7 ms.

Every remaining cell is at the display's refresh ceiling in both engines.

---

## 20. Scaling analysis, 2 → 12 devices

Six times the devices. Chromium, animation off, per-snapshot main-thread cost:

| view | 2 | 4 | 8 | 12 | 12/2 | shape |
|---|---:|---:|---:|---:|---:|---|
| aggregated | 1.8 | 1.0 | 1.3 | 2.9 | 1.6× | flat |
| energy | 12.2 | 8.2 | 6.5 | 7.8 | 0.6× | flat |
| devices | 4.4 | 9.5 | 14.8 | 22.4 | 5.1× | sub-linear |
| control | 12.6 | 18.4 | 23.3 | 27.3 | 2.2× | sub-linear |

DOM nodes, after the off-screen fix: aggregated 449 → 469 (**flat**), energy
871 → 891 (**flat**), devices 804 → 2474 (3.1×), control 1235 → 4075 (3.3×).
Before it, every view carried the control subtree and every view scaled.

**Nothing is super-linear**, and the two views that scale are the two that
actually render one element per device. The aggregated and energy views no
longer grow with the installation at all.

---

## 21. Hidden and inactive work, summarised

| state | what the page does | verdict |
|---|---|---|
| a view switched away | nothing on the live path (pinned by a guardrail), and since §15b nothing on the auth-refresh path either | **fixed** |
| a switched-away view's DOM | retained, and free — not laid out, not painted, not walked | **ruled out as a cost** |
| a background tab (`document.hidden`) | 20 mutations instead of 1645, 0.1–0.3 ms per snapshot, no animation frames | **works completely** |
| a visible but unfocused window | full rendering work, animation frames throttled to 1 fps by the browser | **NEEDS MACOS TEST** |
| the analytics chart while another view is up | not redrawn; the live path writes text into KPI cards only | **ruled out** |
| the logs poll while another view is up | `stopLogsPolling()` on every view change | **ruled out** |
| the 30-second analytics/history refresh | already checks visibility | correct as written |
| the 60-second auth refresh | fetches always, renders only when the control view is up | **fixed** |

---

## 22. What the harness had to learn, twice

Both are recorded because both were nearly reported as results, and neither is
visible without a control.

**A window behind another window is not a slow page.** The first after-fix run
came back at 1.0 fps and a 1000 ms frame time in every case that had a running
animation. That is Chromium's throttle for an occluded window, it is a discrete
state, and nothing in the harness could tell it from a measurement. The tell was
the pattern rather than the size: the energy view, which has no animation, read
108 fps in the same batch. `looks_occluded()` now rejects any case below 5 fps
with a frame time above 500 ms, retries it up to three times, and every report
carries an `occluded_cases` count.

**A probe with no control proves nothing.** §13's compositor test — block the
main thread and see whether the layer still moved — returned "did not move" for
all five elements, including a bare `<div>` animated through the Web Animations
API that is composited if anything on a page is. Without that control it would
have produced five confident and wrong verdicts.

The same shape appears a third time in this audit's data rather than its
instruments: the `innerHTML` probe compares a source string against the DOM's
re-serialisation of itself, so `<input ... checked>` can never compare equal and
the probe can only *under*-count redundancy. It reported the runtime editor as
changing on every write. Comparing generated strings instead showed it identical
on every write, which is the finding §15c is built on.

---

## Appendix — reproducing this

One matrix at a time, each behind a quiet-machine gate. Two of these must never
run at once.

```bash
# the scaling law, and every engine counter in this report
python3 scripts/dashboard_profile/profile_bench.py --matrix scale       --browser chromium --gpu headed
# what a view does while it is not on screen (75 s windows)
python3 scripts/dashboard_profile/profile_bench.py --matrix offscreen   --browser chromium --gpu headed
# which animation buys the per-frame style recalculation, and the floor
python3 scripts/dashboard_profile/profile_bench.py --matrix animcost2   --browser chromium --gpu headed
# the authenticated control view, and the mechanism behind it
python3 scripts/dashboard_profile/profile_bench.py --matrix writeframes --browser chromium --gpu headed
python3 scripts/dashboard_profile/profile_bench.py --matrix buttonborder --browser chromium --gpu headed
# is any of this on the compositor? (it does not work -- see §13)
python3 scripts/dashboard_profile/profile_bench.py --matrix compositor2 --browser chromium --gpu headed
# writes that replace markup with itself, and the two control mounts separately
python3 scripts/dashboard_profile/profile_bench.py --matrix htmlguard   --browser chromium --gpu headed
python3 scripts/dashboard_profile/profile_bench.py --matrix mountcost   --browser chromium --gpu headed
# the two nice-to-have questions: does the Animations API help, and on what
# machine; and what does the control panel's markup cost to build against parse
python3 scripts/dashboard_profile/profile_bench.py --matrix tilewaapi    --browser chromium --gpu headed
python3 scripts/dashboard_profile/profile_bench.py --matrix waapithrottle --browser chromium --gpu headed
python3 scripts/dashboard_profile/profile_bench.py --matrix mountsplit   --browser chromium --gpu headed
# hidden tab, unfocused window, repeated view changes, thirty minutes of them
python3 scripts/dashboard_profile/profile_bench.py --matrix hiddentab   --browser chromium --gpu headed
python3 scripts/dashboard_profile/profile_bench.py --matrix unfocused   --browser chromium --gpu headed --repeat 3
python3 scripts/dashboard_profile/profile_bench.py --matrix lifecycle   --browser chromium --gpu headed
python3 scripts/dashboard_profile/profile_bench.py --matrix longrun     --browser chromium --gpu headed
```

Render any result as a table:

```bash
python3 scripts/dashboard_profile/profile_report.py \
    reports/dashboard-perf/profile-audit-scale-after-chromium-2026-09-05.json
```

`waapithrottle` is the one to reach for when a cost is real on the main thread
and invisible on this machine. It pairs a treatment with a 1×/4×/8×/16× CPU
sweep, and it is what decided §15f after two earlier passes had declined the
same change for lack of a machine without headroom.

**Taking a before.** The before/after pairs in §15 were produced by adding a
`git worktree` at the pre-change commit, copying `scripts/dashboard_profile/`
into it so the harness is identical, and pointing `--out` at the main
repository's report directory. Nothing in the working tree is touched, and the
only difference between the two runs is the dashboard itself.

Labels matter: a run writes `profile-<label>-<browser>-<date>.json`, so two runs
of the same matrix on the same day overwrite each other unless the label
distinguishes them. Every dataset in §15 is labelled `-before` or `-after`.

---

| Area | Classification | Evidence |
|---|---|---|
| DOM scalability | **RULED OUT** | 6× the devices costs at worst 5.1×; aggregated and energy are flat after §15b; 469 nodes at twelve devices where there were 4065 |
| incremental rendering | **NICE TO HAVE** | the parse is 91 % of the control view's per-snapshot cost — 1 ms to build the markup, 12 ms to parse 182 KB of it (§15g); declined on risk, not on size |
| CSS/style recalculation | **FIXED** (the tiles) | all of it is the animations (§7b); the tile animation now runs from `element.animate()`: −28 % to −40 % of style time in every throttle/view combination, and 110.7 → 135.0 fps at 16× on the aggregated view (§15f) |
| hidden views | **FIXED** | one off-screen rebuild per minute, 19.4 → 0.0 ms; carrying the nodes was measured free (§15b, §3) |
| visibility lifecycle | **NEEDS MACOS TEST** | hidden tab: 1645 → 20 mutations, works completely. Unfocused window: full work at 1 fps, `document.hidden` false (§8) |
| SSE lifecycle | **RULED OUT** | 360 view changes in 30 min add no listeners, timers, observers, `EventSource`s or nodes; switches get faster (§14, §15e) |
| memory/leaks | **RULED OUT** | heap 2.9 → 3.7 → 3.8 MB and plateauing; renderer's node and listener counts flat over 30 min (§15e) |
| snapshot pipeline | **FIXED** (partly) | the runtime editor 16.5 → 1.6 ms; the explain mount's 4.9–16.5 ms is legitimate work (§15c, §16) |
| charts/canvas | **RULED OUT** | analytics is the cheapest view at 1.2 ms per snapshot and does not scale with device count (§9) |
| browser differences | **RULED OUT** as a defect | Chromium pays in style time and, before the fix, frames; Firefox pays in control-view lag (11–13 ms). Both hold the refresh ceiling (§11, §11b) |
| GPU/software path | **NOT WORTH IT** | verified for Chromium: SwiftShader costs the devices view ~9 % and only with the animation running. Firefox's still unverifiable (§12) |
| macOS-specific behavior | **NEEDS MACOS TEST** | no number in this audit was taken on a Mac (§18) |

```text
OVERALL STATUS: MINOR OPTIMIZATIONS REMAIN

FIX NOW: 4          (all four implemented, tested and re-measured)
NICE TO HAVE: 1
NOT WORTH IT: 3
NEEDS MACOS TEST: 2
RULED OUT: 6

MACOS VERIFIED: NO

RECOMMENDATION:
Stop here on Linux. The three defects worth fixing are fixed and confirmed by
before/after runs with zero occluded cases; every view in both engines now sits
at the display's refresh ceiling with a 7 ms frame p95, nothing scales
super-linearly, and thirty minutes of continuous use leaks nothing. The two
NICE TO HAVE items are real and both were declined on the same grounds: they
cost no frames on any hardware measured, and the measurement that would justify
either of them cannot be taken here -- one needs a fixture whose data actually
changes, the other needs hardware without this machine's headroom. The one open
question is the unfocused window, and it is open precisely because the platform
it matters on is the one platform none of these numbers came from. The harness
is built to be run there and scripts/dashboard_profile/README.md says how; that
run, not more Linux measurement, is what would move this forward.
```
