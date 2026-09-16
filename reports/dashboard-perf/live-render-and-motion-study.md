# Scrolling the cockpit: what a live snapshot costs, and what motion costs

Reported symptom: sections of the cockpit have to be drawn while scrolling,
and again on the way back to the top, where nothing is new.

Answered in three parts, all measured against
`scripts/serve_dashboard_preview.py` with synthetic payloads. Nothing here says
anything about a real installation's data, only about what the frontend costs
to render.

## The symptom is not lazy loading

There is none. `dashboard/static/app.js` has no `IntersectionObserver` for
content, no scroll handler that fetches anything, and `styles.css` carries no
`content-visibility`, which is the property that produces exactly this symptom
when it is used. Screencast frames captured during a fast scroll on this desktop
show no blank bands.

What is being drawn again is what the page threw away a moment earlier. Two
independent causes, both measured.

## Cause 1: every snapshot rebuilt what it rendered into

`renderDevices` opened with `grid.innerHTML = ""` and rebuilt every card;
`renderRules` rebuilt all nine rows; the energy board and the control
explanation were each written with `innerHTML`. That is every two seconds, for
as long as the page is open. Replacing a node costs the browser the layout and
the rasterised tiles of the area it covers.

Headless Chromium, twelve devices, the devices view, eight seconds of live
snapshots with nobody touching the page:

| | rasterising | painting | scripting |
|---|---|---|---|
| rebuilt | 820.5-959.5 ms | 149.9-170.1 ms | 148.0-170.8 ms |
| patched | 113.4-121.0 ms | 104.4-118.7 ms | 108.8-118.9 ms |

The energy view alone went from 701.5-741.6 ms of rasterising to 9.6-15.7, and
the control view from 264.0-275.2 ms of painting to 8.4-9.3.

`patchHtml` renders the same markup into the nodes already there: text written
only where it differs, attributes only where they differ, an element replaced
only when the structure genuinely changed. It also remembers the markup each
host was last given, because most snapshots change a few numbers in one section
and nothing in the others. Without that cache the control view traded 240 ms of
painting for 90 ms of scripting -- its subtree is the largest in the cockpit,
208 nodes plus 266 per device.

`style` is expensive to strip out of a subtree that is being reused: the SOC
bar's width is written by script and never appears in the markup, so an
attribute sweep that removed what the markup does not carry would reset the bar
on every snapshot. It is left alone by name.

## Cause 2: motion ran where nobody could see it

The cockpit runs 153 endless animations at twelve devices -- one per pipe
segment, a pulse per sun and battery fill, and a sliding ring per control result
row. Each is ticked on the main thread for every frame it runs, and the Web
Animations API keeps running one whose element is in a switched-away view: the
control view, which shows no flow drawing at all, was running ninety of them.

Style recalculation per eight seconds of idle, twelve devices:

| view | before | after |
|---|---|---|
| control | 639.5 / 639.4 ms | 123.3 / 133.0 ms |
| devices | 438.8 / 439.5 ms | 146.4 / 138.1 ms |

The observer callback must not read from the DOM. The first version called
`element.getAnimations()` inside it; a full scroll fires the callback once per
element per direction, and those 306 calls cost 205-223 ms of scripting -- more
than the work they were meant to save. Recording each element's animations once,
when they are found, brings that to 39-47 ms.

Only endless animations are paused. A CSS transition -- the SOC bar is one --
would be stranded half-way.

## Together

Twelve devices, devices view, two runs each:

| | style | painting | scripting | rasterising |
|---|---|---|---|---|
| idle, before | 578-600 ms | 151.0-151.6 ms | 180.4-186.2 ms | 824.8-936.7 ms |
| idle, after | 196.8-203.5 ms | 116.6-126.3 ms | 53.3-56.2 ms | 100.3-116.6 ms |
| scroll, before | 106.4-123.9 ms | 74.0-98.4 ms | 23.8-37.0 ms | 4102-4192 ms |
| scroll, after | 37.6-44.8 ms | 56.5-58.0 ms | 38.4-47.8 ms | 4197-4633 ms |

Two devices, which is what the reporting installation runs: frames longer than
33 ms during a full scroll down and back fall from 3.5 to 1, the frame p95 from
25.0 ms to 16.8, and idle rasterising from 205.3 ms to 125.3.

## What this did not change

Raster work **during** a scroll is unchanged: 4.1-4.6 s per scroll at twelve
devices, 536-595 ms at two. That is the compositor drawing tiles as they come
into view, and it is the remaining candidate for the symptom on a weak client.
It is bounded by how much page there is and by how expensive each tile is to
paint. Two things were checked and are not the cause:

- **Layer count.** There are 314 compositor layers at twelve devices, but the
  memory is not in them: two layers the size of the whole document
  (1600x5878) hold 71.8 MB of the 138.7 MB total, and another three large ones
  hold 41 MB. The ~300 small ones together hold under 20 MB. A long page has a
  document-sized layer whatever the cockpit does, and rasterising it tile by
  tile while scrolling is the normal cost of that.
- **`will-change: transform` on the flow tiles.** Removing it changes the layer
  count by two and the memory not at all (314 vs 316 layers, 141.7 vs
  141.6 MB), with raster work within run-to-run noise. It was left in place.

What is left is the cost of painting each tile: a stylesheet with 25 `filter`
and 58 `box-shadow` declarations, and a page that is 5878 px tall at twelve
devices. Both are design questions rather than rendering defects. Headless measures software
rasterisation, so these figures are an upper bound rather than what a person
sees: [energy-flow-visualization-study.md](energy-flow-visualization-study.md)
measured the gap between the two at more than a factor of ten on this host.

## Reproducing

```bash
python3 scripts/serve_dashboard_preview.py --port 8098 --devices 12 \
    --scenario write-mode --sse-interval 2
```

Then drive it with Playwright, tracing `devtools.timeline`, and scroll **from
inside the page**. `page.mouse.wheel` waits for a frame, and a window the
compositor considers hidden is throttled to 1 Hz, which turns a three-second
scroll into forty-six and measures the desktop rather than the page. A run whose
idle phase draws fewer than 20 fps is measuring that, not this.
