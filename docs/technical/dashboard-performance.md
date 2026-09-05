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
each pipe, each box clipping a repeating-gradient strip that a CSS `transform`
moves by one dash period. A transform on a promoted layer is handled by the
compositor and repaints nothing: eighty of these tiles measured 60.2 fps against
60.1 with none at all. `softPulse` animates opacity only.

| Firefox, before → after | |
|---|---|
| aggregated view, any device count | 5.5 → **57** fps |
| devices view, two devices | 4.7 → **57.6** fps |
| devices view, four or more | 4.1 → 9.2 fps |

Three properties of the renderer are worth knowing before changing it:

- **It reads the appearance back out of the CSS.** Dash pattern, width, colour,
  opacity, speed, direction and whether to move at all come from
  `getComputedStyle` on the `.pipe-energy` element the stylesheet still defines.
  That is why `animation_mode`, `prefers-reduced-motion`, the idle state and the
  four flow-speed buckets keep working without being implemented twice. Change
  the CSS and the layer follows; do not add a second source of truth.
- **Nothing in the layer may carry a `filter`.** Eighty tiles with one
  `drop-shadow` took Chromium from 17.3 to 9.1 fps. The halo comes from the
  static `.pipe-glow` stroke in the SVG.
- **It refuses what it cannot represent.** The path parser takes only `M`, `L`,
  `H` and `V`; anything else, or a browser without `getComputedStyle`, leaves the
  CSS animation in place rather than showing no flow at all.

[`../../tests/test_dashboard_flow_tiles.py`](../../tests/test_dashboard_flow_tiles.py)
pins all three.

### What is still slow

- **The devices view past two devices, in Firefox.** The trigger is the tile
  layer growing taller than the viewport, not the number of tiles: forty tiles
  in a 395px layer run at 60.2 fps, twenty-eight in a 909px layer at 8.8.
- **Chromium, at about 17 fps, because of `backdrop-filter`.** It is
  re-evaluated whenever anything on the page repaints. With it disabled every
  configuration measured reaches 60 fps. That is a visual-design decision, not a
  rendering one.

`dashboard.animation_mode` (`normal` | `reduced` | `off`) remains a real
performance control, and `prefers-reduced-motion` is honoured on top of it.

The full account, including six rejected techniques and a canvas renderer that
was built and withdrawn, is in
[`../../reports/dashboard-perf/flow-rendering-investigation.md`](../../reports/dashboard-perf/flow-rendering-investigation.md).

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
