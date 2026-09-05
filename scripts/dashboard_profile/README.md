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

Reports land in `reports/dashboard-perf/profile-*.json`.

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
