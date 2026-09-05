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

## Matrices

| Matrix | Answers |
|---|---|
| `quick` | is the harness working |
| `ab` | the isolating experiments: animation on/off, backdrop-filter on/off, SSE vs polling, changing vs identical snapshots |
| `baseline` | the acceptance matrix: 1/2/5/10 tabs, the four views, 2/4/8 devices |

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

## What these runs cannot show

The reported symptom is **Firefox on macOS**. This project has no macOS host, so
every number here is Linux. The Firefox runs measure the same engine on a
different compositing path, which is evidence about the engine and not about
the platform. A macOS run remains outstanding and no conclusion here should be
read as covering it.
