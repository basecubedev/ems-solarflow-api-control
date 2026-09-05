# Dashboard performance reports

Reproducible measurements of the dashboard frontend. Produced by
[`scripts/dashboard_bench.py`](../../scripts/dashboard_bench.py), which serves
the synthetic payloads from `scripts/dashboard_preview_data.py` and drives real
browsers through Playwright. No EMS, no hardware and no network are involved,
so a run here says nothing about a real installation's data — only about what
the frontend costs to render.

```bash
python3 scripts/dashboard_bench.py --matrix ab       --browser chromium
python3 scripts/dashboard_bench.py --matrix baseline --browser firefox
```

## Start here

[final-dashboard-performance-audit.md](final-dashboard-performance-audit.md) is
the current document. It is the last Linux-side pass over the whole frontend:
how it scales to twelve devices, what it rebuilds, what it retains, what it does
while nobody is looking at it. Three defects found and fixed, each with a
before/after taken from a worktree at the pre-change commit; the largest is that
the **authenticated** control view drew at 36 fps because the runtime editor
renders twenty submit buttons and each animated a paint property. Everything
before it had only ever benchmarked the read-only dashboard.

[firefox-macos-investigation.md](firefox-macos-investigation.md) is the document
before it, and the first that looked at the whole dashboard rather than its
flow rendering. It found and fixed the dashboard's largest per-snapshot cost --
a forced synchronous layout taken to discover that a view is not on screen --
and it is explicit that no macOS measurement exists, which is what the reported
symptom was about. Its harness,
[`scripts/dashboard_profile/`](../../scripts/dashboard_profile/), is built to be
run on a Mac; that directory's README says how.

[energy-pipe-performance-study.md](energy-pipe-performance-study.md) is the
study before it. It asks, given that the renderer question is settled, how the
pipe and its moving token should actually be built — fourteen constructions,
both engines, headed on a real GPU. Its short answer is that at the size a
dashboard draws none of them is distinguishable from any other, which turns the
question into a design one; its useful output is that the artwork is free, plus
two guardrails on the obvious paths that are not.

[energy-flow-visualization-study.md](energy-flow-visualization-study.md) is the
study before it, and the one that settled the renderer. It asks what the flow visualisation should *be*, not how to
make the existing one cheaper, and it corrects three conclusions in the reports
that precede it.

[flow-rendering-investigation.md](flow-rendering-investigation.md) is the
previous account. Its central mechanism still holds and the study builds on it.
Two of its conclusions do not: the Chromium `backdrop-filter` ceiling and the
Firefox devices-view cliff were both measured on a software rasteriser, and the
canvas "dimming" it could not explain turned out to be a bare `canvas {}`
selector in this project's own stylesheet. The two 2026-09-04 documents before
it are kept for their raw data and their record of how a wrong conclusion was
reached; both carry a note saying so.

**Read the rasterisation path before believing any number here.** Every report
written from 2026-09-04 onward records `environment.gpu` and, per run,
`rasterisation.renderer` and `load_average`. Everything older is Chromium on
ANGLE/SwiftShader, which is software, and is not comparable with a GPU run.
Note also that the renderer string proves a run was software but cannot prove it
was hardware: headless Firefox reports an NVIDIA device for WebGL while
compositing the page on the CPU.

A third harness sits beside the other two:
[`scripts/flow_pipe_study/`](../../scripts/flow_pipe_study/) renders one scene
with a selectable pipe construction and a selectable glow, and carries its own
correctness gate (`pipe_verify.mjs`) that must pass before any of its numbers
are believed.

A second harness sits beside this one:
[`scripts/flow_lab_bench.py`](../../scripts/flow_lab_bench.py) renders an
isolated scene of pipes with a selectable technique, so rendering approaches can
be compared against each other rather than against the whole dashboard. Its
Read section 6 of the investigation
before trusting one: a finding that held in that lab did not transfer.

## Matrices

| Matrix | Answers |
|---|---|
| `quick` | is the harness working |
| `ab` | the isolating experiments: animation on/off, backdrop-filter on/off, SSE vs polling, changing vs identical snapshots |
| `baseline` | the acceptance matrix: 1/2/5/10 tabs, the four views, 2/4/8 devices |
| `views` | the two flow views at 2/4/8 devices |
| `glass` | backdrop-filter crossed with animation on/off -- supersedes `backdrop`, which pinned animation at "off" and so could only ever answer "free" |
| `ff-cliff` | candidate explanations for the Firefox devices-view collapse |
| `gpu-recheck` | the same scenarios on `--gpu software` and `--gpu gpu`, to separate engine behaviour from rasterisation |

## Metrics, and which one to believe

| Field | Meaning |
|---|---|
| `lagP95Ms`, `lagMaxMs` | how late a 50 ms timer fired — time the main thread was not available. **The primary metric.** It is what the reported symptom is made of, and Chromium and Firefox report it identically. |
| `mutations` | DOM nodes added, removed or changed. Directly measures the rebuild-per-snapshot cost. |
| `longTasks`, `longTaskTotalMs` | tasks over 50 ms. Chromium only — Firefox has no `longtask` observer, and those runs report zero rather than nothing. |
| `frameP95Ms`, `fps` | frame pacing. **Weak in headless**, which is not vsync-locked; use it for comparison between two runs of the same browser, never as an absolute. |

## Reading a report

`totals` aggregates across tabs: sums for work, worst-case for lag. `perTab`
keeps every tab, because an average across ten tabs hides the one that was
demoted to polling. The last tab in the list is the foreground one.

## Rasterisation paths

`--gpu` selects one. It changes what Chromium does and only whether a window
appears for Firefox, which is itself the trap: Firefox headless does not
GPU-composite page content on this host.

| `--gpu` | Chromium | Firefox |
|---|---|---|
| `software` (default) | ANGLE/SwiftShader, software | headless, page composited on the CPU |
| `gpu` | real device via ANGLE | unchanged from `software` |
| `headed` | real device, real window | real device, real window -- **the only certain path** |

`--max-load` (default 2.0) makes each case wait for a quiet machine before it
runs. This host is a live desktop and is CPU-limited when several things run at
once; a frame rate taken under load is not a property of the renderer, and
afterwards it is indistinguishable from one that is.

## What these runs cannot show

The reported symptom is **Firefox on macOS**. This project has no macOS host, so
every number here is Linux. The Firefox runs measure the same engine on a
different compositing path, which is evidence about the engine and not about
the platform. A macOS run remains outstanding and no conclusion here should be
read as covering it.
