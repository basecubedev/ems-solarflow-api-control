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

The single most expensive thing the dashboard does is animate the dash pattern
on the energy-flow pipes. `stroke-dashoffset` is not a compositable property, so
every change invalidates the stroke's raster.

Measured on the devices view with four devices:

| | Firefox | Chromium |
|---|---:|---:|
| `animation_mode: normal` | 4.4 fps | 14.4 fps |
| `animation_mode: off` | 55.1 fps | 56.3 fps |

`dashboard.animation_mode` (`normal` | `reduced` | `off`) is therefore a real
performance control rather than a preference, and `prefers-reduced-motion` is
honored on top of it. **It is the only lever that was found to work.**

Three ways to keep the motion and lose the cost were implemented and measured,
and all three were rejected: moving the filters off the animating layer (1.07x
Chromium, 1.00x Firefox), `steps(N)` timing (2.13x at four steps per cycle), and
driving the property from a timer instead of CSS (slightly *worse* than the CSS
animation at 20 Hz). The cost tracks the rate at which the property changes, not
the technique. See
[`../../reports/dashboard-perf/findings-2026-09-04.md`](../../reports/dashboard-perf/findings-2026-09-04.md).

**`backdrop-filter` is not a cost.** Measured in isolation with the animation
off, three runs each: Chromium 1.02x, Firefox 1.00x. The glass-panel look stays.

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
