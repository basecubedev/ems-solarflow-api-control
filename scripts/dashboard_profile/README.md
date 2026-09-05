# Dashboard attribution profiler

Answers "what is the page doing", where
[`../dashboard_bench.py`](../dashboard_bench.py) answers "how fast is it". Use
this one when a frame rate says the dashboard is fine and a person says it is
not — which is exactly the case that produced
[`../../reports/dashboard-perf/firefox-macos-investigation.md`](../../reports/dashboard-perf/firefox-macos-investigation.md).

It drives the **real dashboard** against the preview server, and charges
main-thread time to whoever consumed it.

```bash
python3 scripts/dashboard_profile/profile_bench.py --matrix attribution \
    --browser firefox --gpu headed
```

Each run writes its JSON under `--out`, which defaults to
`reports/dashboard-perf/`. Those files are **not committed** -- the repository
keeps the written accounts and this harness, not the output of a run.

## Running it on macOS

**This is the reason the harness exists.** The slowdown was reported on Firefox
on macOS and there is no Mac in the development environment, so nothing in the
investigation report is a macOS measurement. These commands take the same
numbers on one.

```bash
git clone <this repo> && cd ems-solarflow-api-control
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt
npx playwright install firefox chromium

# The three that matter, in order. Each takes a few minutes and opens
# real browser windows. Run them one at a time and do not use the machine
# while they run.
python3 scripts/dashboard_profile/profile_bench.py --matrix feed        --browser firefox --gpu headed --repeat 2
python3 scripts/dashboard_profile/profile_bench.py --matrix attribution --browser firefox --gpu headed --repeat 3
python3 scripts/dashboard_profile/profile_bench.py --matrix neighbour   --browser firefox --gpu headed --repeat 2
```

Then send the `reports/dashboard-perf/profile-*.json` files back. The three
questions they answer, and what Linux said, are in sections 2, 3 and 6 of the
investigation report. The comparison that matters is **per-snapshot
main-thread work**, not frames per second: on a 144 Hz Linux desktop with a GPU
the dashboard never drops below the refresh rate in any configuration, so the
frame rate cannot show the symptom even when the work is there.

`--max-load` is a Linux-only gate (it reads `os.getloadavg`), and it is
harmless on macOS; pass `--max-load 0` to skip the wait entirely.

## What it measures

Per run, for the dashboard page and — where the scenario opens one — for a
trivial neighbour page:

| | |
|---|---|
| `work` | main-thread time per callback source: `listener:<type>` (including the `EventSource` handler that receives every snapshot), `setTimeout`, `setInterval`, `requestAnimationFrame`, `ResizeObserver`, `MutationObserver`, `IntersectionObserver` |
| `read:*` | with `deep_reads`, the layout-forcing reads themselves — `getBoundingClientRect`, `getComputedStyle`, `offsetHeight`/`offsetWidth`/`clientHeight`/`clientWidth` |
| `mutationTargets` | DOM mutations grouped by the nearest ancestor with an id, so churn can be traced to a panel |
| `blockingMs` | engine-neutral stand-in for long tasks: a self-rescheduling zero timeout, and every gap beyond 12 ms |
| `lagP95Ms` / `lagMaxMs` | a 50 ms timer's lateness — what a person feels |
| `domNodes`, `fetches`, `animationsRunning`, `hidden` | context that makes two runs comparable |

Everything is engine-neutral. Firefox has no long-task observer and no DevTools
trace; nothing here needs either.

## The axes the frame-rate harness does not have

| axis | values | what it isolates |
|---|---|---|
| `feed` | `silent`, `frozen`, `live` | nothing arrives / arrives and is discarded / the full path. `silent` is how you prove there is no standing cost. |
| `foreground` | `dashboard`, `neighbour` | whether an unfocused dashboard keeps working |
| `neighbour` | on/off | whether an open dashboard costs another page anything |
| `deep_reads` | on/off | charges forced synchronous layout to the reader |
| `software` | on/off | **unverified — see below** |

## What the final audit added

[`../../reports/dashboard-perf/final-dashboard-performance-audit.md`](../../reports/dashboard-perf/final-dashboard-performance-audit.md)
needed answers this harness could not give, so it grew four axes. All of them
are off by default and none changes what the existing matrices measure.

| axis | engines | what it adds |
|---|---|---|
| `cdp_metrics` | Chromium | `RecalcStyleCount/Duration`, `LayoutCount/Duration`, `ScriptDuration`, `TaskDuration`, and the live levels `Nodes`, `JSEventListeners`, `Documents`, `JSHeapUsedSize`, as a delta across the measurement window |
| `trace` | Chromium | `Paint`, `RasterTask`, `Commit`, `PrePaint`, `UpdateLayoutTree`, and the renderer's own composite-failure reasons |
| `cycle_views` | both | rotates the view on a fixed interval and times each switch |
| `sample_ms` (+ `gc`) | both (heap: Chromium) | levels sampled through a long run, garbage collected first, so a level that keeps climbing is retention rather than allocation |
| `scenario` | both | `write-mode` renders the control view as an authenticated operator sees it, where the runtime editor is a form per device instead of one line |

The page-side instrument also keeps **cumulative lifecycle counters** — listeners
added and removed, intervals created and cleared, observers constructed and
disconnected, `EventSource`s opened and closed — deliberately *not* reset by
`RESET`, because a leak is a level that keeps climbing across measurement
windows and restarting the counter hides exactly that. `domNodesByView` reports
node counts per view container, so "how many nodes" arrives with "whose".

`profile_report.py` renders any of these JSON files as a table. Every column it
prints is a recorded field or a ratio of two of them; it recomputes nothing.

### One thing the driver now does on purpose

After the first snapshot it calls `loadAuthStatus()` once and waits for
`#controlExplainMount`. That is not cosmetic. Whether the control panel exists
at all is a race between the boot fetches and the first snapshot — it is worth
~1350 nodes at four devices — and the same scenario reported 443 or 1793 nodes
run to run until this was pinned. The dashboard reaches that state on its own
within a minute, because the auth refresh runs on a sixty-second timer; the
driver just stops waiting for it.

## Two traps this harness has already fallen into

**An A/B whose treatment silently did nothing.** `page.evaluate()` treats a
string as an *expression*, so `"() => { ... }"` evaluates to a function object
and never runs. The first flow-layer A/B compared two identical pages and
looked like a clean null result. The driver now records what the treatment
returned, and `DISABLE_TILES` is written as an immediately invoked expression.

**A software-rendering switch that cannot be confirmed.** Passing
`gfx.webrender.software` does not change what content can observe: Firefox
reports the same sanitised `WEBGL_debug_renderer_info` string either way, and
`about:support`, which does carry the compositor, cannot be opened through
Playwright. `gfx_probe.mjs` attempts it and records the failure rather than
guessing. **Do not report the `software` axis as a software measurement** until
something verifies it; the investigation report does not.
