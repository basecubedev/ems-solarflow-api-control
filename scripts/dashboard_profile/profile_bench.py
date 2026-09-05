#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attribution profiler for the dashboard: what is the page doing, not how fast.

The dashboard benchmark answers "how many frames". When a frame rate says the
page is fine and a user says it is not, the question left over is where the main
thread goes. This drives the real dashboard through
``scripts/dashboard_profile/profile_driver.mjs``, which charges main-thread time
to the callback that consumed it, and adds the three scenarios the frame-rate
harness has no way to express: a page with nothing arriving at all, a page in
the background, and a second, trivial page beside it.

    python3 scripts/dashboard_profile/profile_bench.py --matrix attribution \
        --browser firefox --gpu headed

Every matrix runs one case at a time and waits for a quiet machine first.
Never run two of these at once.

Running it on macOS is the point of it existing; see README.md in this
directory.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts")
DRIVER = os.path.join(SCRIPTS, "dashboard_profile", "profile_driver.mjs")
DEFAULT_OUT = os.path.join(ROOT, "reports", "dashboard-perf")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from serve_dashboard_preview import start_server  # noqa: E402
import dashboard_bench as bench  # noqa: E402

VIEWS = ("aggregated", "devices", "control", "energy")

# How much data reaches the page, which is the axis the frame-rate harness
# cannot express. `silent` is the important one: the page has rendered once and
# then nothing arrives at all, so whatever it still costs is the page merely
# existing.
FEEDS = {
    "live": {"freeze_timestamp": False, "sse_interval": 2.0},
    "frozen": {"freeze_timestamp": True, "sse_interval": 2.0},
    "silent": {"freeze_timestamp": False, "sse_interval": 3600.0},
}


def scenario(**over):
    base = {
        "view": "aggregated",
        "devices": 4,
        "animation": "off",
        "transport": "sse",
        "feed": "live",
        "software": False,
        "foreground": "dashboard",
        "deep_reads": False,
        "cpu_throttle": 1,
        "dashboard_open": True,
        "neighbour": False,
        "extra_js": "",
        "cdp_metrics": False,
        "trace": False,
        "cycle_views": None,
        "cycle_interval_ms": 2000,
        "sample_ms": 0,
        "gc": False,
        "duration_ms": None,
        "compositor_probe": None,
        # The default preview is read-only, where the runtime editor collapses
        # to a one-line notice. An authenticated install renders a form per
        # device there, so `write-mode` is the heavier and more realistic shape
        # of the control view.
        "scenario": "normal",
    }
    base.update(over)
    if "name" not in base:
        base["name"] = "%s-%s-anim%s-%s" % (
            base["view"], base["feed"], base["animation"],
            "sw" if base["software"] else "gpu",
        )
    return base


def matrix_feed():
    """What does the page cost when nothing is arriving?

    Separates the cost of being on screen from the cost of processing updates,
    which is the split the reported symptom needs and the frame-rate harness
    cannot make.
    """
    cases = []
    for view in ("aggregated", "devices", "control"):
        for feed in ("silent", "frozen", "live"):
            cases.append(scenario(view=view, feed=feed, animation="off"))
    return cases


def matrix_attribution():
    """Where the main thread goes, with the animation on and off."""
    cases = []
    for view in VIEWS:
        for animation in ("normal", "off"):
            cases.append(scenario(view=view, animation=animation, feed="live"))
    return cases


def matrix_visibility():
    """Does a backgrounded dashboard stop working?"""
    cases = []
    for animation in ("normal", "off"):
        cases.append(scenario(view="devices", animation=animation,
                              foreground="dashboard",
                              name="visible-anim%s" % animation))
        cases.append(scenario(view="devices", animation=animation,
                              foreground="neighbour", neighbour=True,
                              name="background-anim%s" % animation))
    return cases


def matrix_neighbour():
    """The browser-wide symptom: is a second page slower with the dashboard open?

    The neighbour is a trivial page; only its own responsiveness is read. The
    control is the same page with no dashboard open at all.
    """
    cases = [
        scenario(dashboard_open=False, neighbour=True, foreground="neighbour",
                 name="neighbour-alone"),
    ]
    for animation in ("normal", "off"):
        for view in ("devices", "control"):
            cases.append(scenario(view=view, animation=animation, neighbour=True,
                                  foreground="neighbour",
                                  name="neighbour-with-%s-anim%s" % (view, animation)))
    return cases


def matrix_software():
    """Rendering-path sensitivity, recorded rather than inferred."""
    cases = []
    for software in (False, True):
        for view in ("aggregated", "devices"):
            for animation in ("normal", "off"):
                cases.append(scenario(view=view, animation=animation,
                                      software=software))
    return cases


def matrix_charts():
    """Isolate the chart and polling views from the flow views.

    `analytics` draws a uPlot canvas; `logs` runs a two-second poll of its own.
    Both are recurring work the flow views do not have.
    """
    cases = []
    for view in ("aggregated", "energy", "analytics", "logs"):
        cases.append(scenario(view=view, animation="off", feed="live",
                              name="charts-%s" % view))
    # The chart is fed by its own analytics query rather than by the snapshot,
    # so it should not scale with device count at all. Worth confirming rather
    # than assuming, since the device selector is populated from the snapshot.
    for devices in (2, 12):
        cases.append(scenario(view="analytics", devices=devices, animation="normal",
                              feed="live", cdp_metrics=True, trace=True,
                              name="charts-analytics-%02ddev" % devices))
    return cases


# Turns the flow tile renderer off at runtime. The layer stops being rebuilt and
# the CSS animation takes the flow back -- which animation_mode=off has already
# disabled, so the two sides of this A/B look the same on screen.
# Immediately invoked. page.evaluate() treats a string as an expression, so an
# uninvoked arrow function evaluates to a function object and the body never
# runs -- which silently turned this A/B into a comparison of two identical
# pages the first time it was written.
DISABLE_TILES = (
    "(() => { let ok = false;"
    " try { flowTileState.active = false; ok = true; } catch (e) {}"
    " document.body.classList.remove('flow-tiles-active');"
    " document.querySelectorAll('.flow-tile-layer').forEach((n) => n.remove());"
    " window.__tilesDisabled = ok;"
    " return ok; })()"
)


def matrix_flowlayer():
    """Is the flow tile layer still being rebuilt when it cannot animate?

    With animation_mode=off the tiles are painted `still`. The rebuild path
    reads geometry and computed style back for every pipe on every snapshot
    regardless, and only the DOM write is skipped when nothing changed. This
    measures what that read-back costs by removing it.
    """
    cases = []
    for view in ("aggregated", "devices", "control"):
        for animation in ("off", "normal"):
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  deep_reads=True,
                                  name="tiles-on-%s-anim%s" % (view, animation)))
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  extra_js=DISABLE_TILES, deep_reads=True,
                                  name="tiles-off-%s-anim%s" % (view, animation)))
    return cases


def matrix_reads():
    """Where does the extra time with animation OFF actually go?

    Same code, same call counts, more time per call. The candidate explanation
    is a forced style/layout flush: with an animation running the style is
    recalculated every frame anyway, so a reader arrives to a clean tree; with
    it off, the first reader pays for everything that accumulated. This charges
    the layout-forcing reads themselves, so the explanation is measured rather
    than argued.
    """
    cases = []
    for view in ("devices", "control"):
        for animation in ("normal", "off"):
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  deep_reads=True,
                                  name="reads-%s-anim%s" % (view, animation)))
    return cases


def matrix_reads_after():
    """The next layer down: what still forces layout once the first fix is in.

    The control and energy views no longer rebuild the tile layer at all, and
    their remaining cost sits in the snapshot handler -- which shows the same
    animation-off penalty, so the same class of cause is likely. This charges
    the reads in every view so the next target is chosen from a measurement.
    """
    cases = []
    for view in ("aggregated", "devices", "control", "energy"):
        for animation in ("normal", "off"):
            cases.append(scenario(view=view, animation=animation, feed="live",
                                  deep_reads=True, devices=4,
                                  name="reads2-%s-anim%s" % (view, animation)))
    return cases


# Switches off the animated border on control-stage results, through the CSSOM
# so the dashboard's `style-src 'self'` CSP cannot refuse it. Returns how many
# rules it changed, so a run that treated nothing is visible in the report.
DISABLE_RESULT_BORDER = (
    "(() => { let n = 0;"
    " for (const sheet of document.styleSheets) {"
    "   try {"
    "     for (const rule of sheet.cssRules) {"
    "       if (rule.style && rule.style.animationName === 'controlResultBorderFlow') {"
    "         rule.style.animation = 'none'; n += 1; } } }"
    "   catch (e) {} }"
    " return n; })()"
)


def matrix_result_border():
    """Chromium draws the control view at 46 fps with the animation on.

    Less main-thread work than with it off, so the cost is compositor-side. The
    candidate is the control-stage result border: `background-position` animated
    on a pseudo-element that also carries `mask-composite: exclude`. Neither is
    a compositable property, so every frame repaints the element and recomputes
    its mask, once per stage per device.
    """
    # animation="normal" is not a detail: `.dashboard-animation-off` already
    # stops this keyframe, so running the A/B at this module's default of "off"
    # compares two pages that both have it off -- which is exactly what the
    # first attempt did.
    cases = []
    for devices in (2, 4, 8):
        cases.append(scenario(view="control", devices=devices, animation="normal",
                              name="border-on-control-%ddev" % devices))
        cases.append(scenario(view="control", devices=devices, animation="normal",
                              extra_js=DISABLE_RESULT_BORDER,
                              name="border-off-control-%ddev" % devices))
    # The same keyframe also drives .primary-button.compact::after, which is in
    # the top bar of every view -- so the aggregated view is the control for
    # "does one of these cost anything on its own".
    for view in ("aggregated", "devices"):
        cases.append(scenario(view=view, animation="normal",
                              name="border-on-%s" % view))
        cases.append(scenario(view=view, animation="normal",
                              extra_js=DISABLE_RESULT_BORDER,
                              name="border-off-%s" % view))
    return cases


DISABLE_RESULT_MASK = (
    "(() => { let n = 0;"
    " for (const sheet of document.styleSheets) {"
    "   try {"
    "     for (const rule of sheet.cssRules) {"
    "       if (rule.style && rule.style.animationName === 'controlResultBorderFlow') {"
    "         rule.style.webkitMask = 'none'; rule.style.mask = 'none';"
    "         rule.style.webkitMaskComposite = 'add'; rule.style.maskComposite = 'add';"
    "         n += 1; } } }"
    "   catch (e) {} }"
    " return n; })()"
)


def matrix_result_border_parts():
    """Which half of the effect costs: the animated paint, or the mask?

    The answer decides which fix is the minimal one. Keeping the animation and
    replacing the mask is a different change from keeping the mask and making
    the animation compositable, and only a measurement can say which is needed.
    """
    cases = []
    for browser_view in ("control",):
        for devices in (4, 8):
            cases.append(scenario(view=browser_view, devices=devices, animation="normal",
                                  name="parts-both-%ddev" % devices))
            cases.append(scenario(view=browser_view, devices=devices, animation="normal",
                                  extra_js=DISABLE_RESULT_BORDER,
                                  name="parts-noanim-%ddev" % devices))
            cases.append(scenario(view=browser_view, devices=devices, animation="normal",
                                  extra_js=DISABLE_RESULT_MASK,
                                  name="parts-nomask-%ddev" % devices))
    return cases


def matrix_throttle():
    """Slow the main thread down until the cost becomes visible.

    This desktop has so much headroom that the defect found in this
    investigation cost 25-36 ms per snapshot and never dropped a frame, which is
    why a frame-rate harness could not find it. Chromium's CPU throttling scales
    main-thread work by an exact factor, so the same page can be measured on the
    class of machine where 25 ms matters -- without inventing a macOS number.
    """
    cases = []
    for rate in (1, 4, 8, 16):
        for view in ("aggregated", "control"):
            for animation in ("normal", "off"):
                cases.append(scenario(view=view, animation=animation,
                                      cpu_throttle=rate,
                                      name="throttle%dx-%s-anim%s" % (rate, view, animation)))
    return cases


def matrix_devices():
    cases = []
    for devices in (2, 4, 8):
        for view in ("devices", "control"):
            cases.append(scenario(view=view, devices=devices, animation="off",
                                  name="scale-%s-%ddev" % (view, devices)))
    return cases


# ---------------------------------------------------------------------------
# Final audit matrices. Everything above answered "why is one machine slow";
# these answer "how does this page behave as it grows and as it is used".
# ---------------------------------------------------------------------------


def matrix_scale():
    """The scaling law: 2 -> 4 -> 8 -> 12 devices, every view, both animation modes.

    Chromium runs carry the engine counters, so style recalculation and layout
    are read from the renderer rather than inferred from wall time.
    """
    cases = []
    for devices in (2, 4, 8, 12):
        for view in VIEWS:
            for animation in ("off", "normal"):
                cases.append(scenario(
                    view=view, devices=devices, animation=animation, feed="live",
                    cdp_metrics=True,
                    name="scale-%s-%02ddev-anim%s" % (view, devices, animation),
                ))
    return cases


def matrix_pipeline():
    """Where a single snapshot goes, per view, with the stages the page cannot see.

    `deep_reads` charges the layout-forcing reads, the trace names paint and
    raster, and the engine counters name style and layout. One case per view is
    enough because the question is composition, not variance.
    """
    cases = []
    for view in VIEWS:
        cases.append(scenario(view=view, devices=4, animation="normal", feed="live",
                              deep_reads=True, cdp_metrics=True, trace=True,
                              name="pipeline-%s" % view))
    return cases


CYCLE = ["devices", "control", "energy", "aggregated"]


def matrix_lifecycle():
    """Repeated view changes: does anything accumulate?

    The control is the same page left on one view for the same wall time, so a
    level that only climbs while cycling is separable from one that climbs
    anyway.
    """
    return [
        scenario(view="aggregated", devices=4, animation="normal", feed="live",
                 cdp_metrics=True, gc=True, sample_ms=15000, duration_ms=180000,
                 name="lifecycle-static-3min"),
        scenario(view="aggregated", devices=4, animation="normal", feed="live",
                 cdp_metrics=True, gc=True, sample_ms=15000, duration_ms=180000,
                 cycle_views=CYCLE, cycle_interval_ms=2000,
                 name="lifecycle-cycling-3min"),
    ]


def matrix_longrun():
    """Thirty minutes of continuous use, sampled every minute.

    Garbage is collected before every sample, so a level that keeps climbing is
    retention rather than allocation waiting to be reclaimed.
    """
    return [
        scenario(view="aggregated", devices=4, animation="normal", feed="live",
                 cdp_metrics=True, gc=True, sample_ms=60000, duration_ms=1800000,
                 cycle_views=CYCLE, cycle_interval_ms=5000,
                 name="longrun-cycling-30min"),
    ]


# Each of these turns one CSS mechanism off through the CSSOM -- generated
# stylesheets are refused by the dashboard's `style-src 'self'` -- and returns
# how many rules it touched, so a treatment that matched nothing is visible.
def _css_off(match, apply_js):
    return (
        "(() => { let n = 0;"
        " for (const sheet of document.styleSheets) {"
        "   try {"
        "     for (const rule of sheet.cssRules) {"
        "       if (!rule.style) continue;"
        "       if (%s) { %s n += 1; } } }"
        "   catch (e) {} }"
        " return n; })()" % (match, apply_js)
    )


DISABLE_BACKDROP = _css_off(
    "rule.style.backdropFilter || rule.style.webkitBackdropFilter",
    "rule.style.backdropFilter = 'none'; rule.style.webkitBackdropFilter = 'none';",
)
DISABLE_SHADOWS = _css_off(
    "rule.style.boxShadow && rule.style.boxShadow !== 'none'",
    "rule.style.boxShadow = 'none';",
)
DISABLE_FILTERS = _css_off(
    "rule.style.filter && rule.style.filter !== 'none'",
    "rule.style.filter = 'none';",
)
# Not a removal: adds paint containment to the repeating cards, which is the
# one containment opportunity the stylesheet does not take anywhere.
ADD_CONTAINMENT = (
    "(() => { let n = 0;"
    " for (const el of document.querySelectorAll("
    "   '.device-card, .control-stage, .control-result, .metric')) {"
    "   el.style.contain = 'paint'; n += 1; }"
    " return n; })()"
)


def matrix_css():
    """Which CSS mechanism costs anything on the real page, measured not guessed."""
    cases = []
    for view in ("aggregated", "devices", "control"):
        cases.append(scenario(view=view, devices=8, animation="normal", feed="live",
                              cdp_metrics=True, name="css-baseline-%s" % view))
        for label, js in (
            ("nobackdrop", DISABLE_BACKDROP),
            ("noshadow", DISABLE_SHADOWS),
            ("nofilter", DISABLE_FILTERS),
            ("contain", ADD_CONTAINMENT),
        ):
            cases.append(scenario(view=view, devices=8, animation="normal", feed="live",
                                  cdp_metrics=True, extra_js=js,
                                  name="css-%s-%s" % (label, view)))
    return cases


# Reports what is still animating and where it lives, so "a hidden view keeps
# working" is a count rather than an impression.
HIDDEN_WORK_PROBE = (
    "(() => {"
    " const offScreen = (el) => !el || (el.closest && Boolean(el.closest('[hidden]')))"
    "   || (el.getClientRects && el.getClientRects().length === 0);"
    " let running = 0, hidden = 0;"
    " const owners = {};"
    " try {"
    "   for (const anim of document.getAnimations()) {"
    "     if (anim.playState !== 'running') continue;"
    "     running += 1;"
    "     const el = anim.effect && anim.effect.target;"
    "     if (offScreen(el)) {"
    "       hidden += 1;"
    "       const owner = el && el.closest ? el.closest('section[id], div[id]') : null;"
    "       const key = (owner && owner.id) || (el && el.className) || 'unknown';"
    "       owners[String(key)] = (owners[String(key)] || 0) + 1; } } }"
    " catch (e) { return { error: String(e) }; }"
    " return { running, hidden, owners }; })()"
)


def matrix_hiddenwork():
    """Do switched-away views keep animating, and does a background tab stop?"""
    cases = []
    for view in VIEWS:
        cases.append(scenario(view=view, devices=8, animation="normal", feed="live",
                              cdp_metrics=True, trace=True, extra_js=HIDDEN_WORK_PROBE,
                              name="hidden-%s" % view))
    cases.append(scenario(view="devices", devices=8, animation="normal", feed="live",
                          cdp_metrics=True, trace=True, extra_js=HIDDEN_WORK_PROBE,
                          neighbour=True, foreground="neighbour",
                          name="hidden-devices-unfocused"))
    return cases


# Charges each view renderer separately and records, per call, whether the
# container it writes into was off screen at the time. The renderers are
# top-level declarations in a classic script, so they are globals; the wrapper
# accounts into the profiler's own table so SUMMARIZE reports it with the rest.
OFFSCREEN_PROBE = (
    "(() => { let n = 0;"
    " const account = (name, ms) => {"
    "   const w = window.__prof.work;"
    "   const slot = w[name] || (w[name] = { calls: 0, ms: 0, max: 0 });"
    "   slot.calls += 1; slot.ms += ms; if (ms > slot.max) slot.max = ms; };"
    " const offScreen = (id) => {"
    "   const el = document.getElementById(id);"
    "   return !el || el.hidden || Boolean(el.closest && el.closest('[hidden]')); };"
    " for (const [fn, container] of ["
    "   ['renderControlExplain', 'controlExplainView'],"
    "   ['renderDevices', 'deviceGrid'],"
    "   ['renderDeviceFlow', 'deviceFlowView'],"
    "   ['renderEnergyStats', 'energyStatsView']]) {"
    "   const original = window[fn];"
    "   if (typeof original !== 'function') continue;"
    "   window[fn] = function (...args) {"
    "     const hiddenNow = offScreen(container);"
    "     const startedAt = performance.now();"
    "     try { return original.apply(this, args); }"
    "     finally {"
    "       account('render:' + fn + (hiddenNow ? ':offscreen' : ':onscreen'),"
    "               performance.now() - startedAt); } };"
    "   n += 1; }"
    " return n; })()"
)


def matrix_offscreen():
    """Does a view that is not on screen still get rebuilt?

    Seventy-five seconds, because the auth refresh runs on a sixty-second
    interval and calls the control renderer whatever view is up; a ten-second
    window would miss it entirely.
    """
    cases = []
    for view in ("aggregated", "devices", "energy"):
        for devices in (4, 12):
            cases.append(scenario(view=view, devices=devices, animation="normal",
                                  feed="live", cdp_metrics=True,
                                  extra_js=OFFSCREEN_PROBE, duration_ms=75000,
                                  name="offscreen-%s-%02ddev" % (view, devices)))
    return cases


# Each of these silences one animation and leaves the rest alone, so the style
# recalculation the engine reports can be charged to a mechanism instead of to
# "the animation". The px variant is deliberately not a shippable rendering --
# it answers whether the custom property in the keyframe is what costs, and a
# constant step changes how far a tile travels.
STILL_TILES = _css_off(
    "rule.style.animationName && rule.style.animationName.indexOf('flowTile') === 0",
    "rule.style.animation = 'none';",
)
FIXED_STEP_KEYFRAMES = (
    "(() => { let n = 0;"
    " const at = { flowTileRight: 'translate3d(40px, 0, 0)',"
    "   flowTileLeft: 'translate3d(-40px, 0, 0)',"
    "   flowTileDown: 'translate3d(0, 40px, 0)',"
    "   flowTileUp: 'translate3d(0, -40px, 0)' };"
    " for (const sheet of document.styleSheets) {"
    "   try {"
    "     for (const rule of sheet.cssRules) {"
    "       if (rule.type !== CSSRule.KEYFRAMES_RULE) continue;"
    "       const replacement = at[rule.name];"
    "       if (!replacement) continue;"
    "       rule.appendRule('to { transform: ' + replacement + '; }'); n += 1; } }"
    "   catch (e) {} }"
    " return n; })()"
)
NO_PULSE = _css_off(
    "rule.style.animationName === 'softPulse' || rule.style.animationName === 'fillPulse'",
    "rule.style.animation = 'none';",
)
NO_RING = _css_off(
    "rule.style.animationName === 'controlResultRingSlide'",
    "rule.style.animation = 'none';",
)


# The floor: every animation in the document stopped, whatever it is and
# wherever it lives. A treatment that names one keyframe can only say "this one
# is not the whole of it"; this one says how much there is to attribute.
NO_ANIMATION_AT_ALL = _css_off(
    "rule.style.animationName && rule.style.animationName !== 'none'",
    "rule.style.animation = 'none';",
)
NO_BUTTON_BORDER = _css_off(
    "rule.style.animationName === 'controlResultBorderFlow'",
    "rule.style.animation = 'none';",
)


# The ring is a masked box with an animated child. If the mask is what keeps the
# child's transform off the compositor, removing it should remove the per-frame
# style recalculation. It also changes the appearance, so this is a question
# about the mechanism and not a candidate rendering.
NO_RING_MASK = _css_off(
    "rule.style.maskComposite === 'exclude'"
    " || rule.style.webkitMaskComposite === 'xor'",
    "rule.style.webkitMask = 'none'; rule.style.mask = 'none';"
    " rule.style.webkitMaskComposite = 'add'; rule.style.maskComposite = 'add';",
)
NO_WILLCHANGE = _css_off(
    "rule.style.willChange && rule.style.willChange !== 'auto'",
    "rule.style.willChange = 'auto';",
)


def matrix_compositor():
    """Are these animations on the compositor at all?

    Every treatment in `animcost2` moved the style-recalculation time and none
    moved the paint count, which is consistent with "composited but still
    ticked on the main thread" and with "not composited and nothing to repaint".
    Only one question separates them, and it is asked directly here: the probe
    blocks the main thread for 600 ms and reads the element's transform on
    either side of it. A compositor-driven animation keeps moving; a
    main-thread one cannot.

    Named as the experiment that would settle this in the pipe study's remaining
    uncertainties, where it was written for a lab page and never run.
    """
    cases = []
    for devices in (4, 12):
        ring = dict(view="control", devices=devices, animation="normal",
                    feed="live", cdp_metrics=True, trace=True,
                    compositor_probe=".control-result-ring i")
        cases.append(scenario(**ring, name="comp-ring-base-%02d" % devices))
        cases.append(scenario(**ring, extra_js=NO_RING_MASK,
                              name="comp-ring-nomask-%02d" % devices))
        cases.append(scenario(**ring, extra_js=NO_WILLCHANGE,
                              name="comp-ring-nowillchange-%02d" % devices))
        for view in ("aggregated", "devices"):
            cases.append(scenario(view=view, devices=devices, animation="normal",
                                  feed="live", cdp_metrics=True, trace=True,
                                  compositor_probe=".flow-tile-inner",
                                  name="comp-tile-%s-%02d" % (view, devices)))
    return cases


def matrix_animcost2():
    """Which animation buys the per-frame style recalculation, isolated.

    The first pass established that the control view's whole recalculation cost
    disappears with one rule and the aggregated view's does not. This adds the
    floor -- every animation stopped -- and the one remaining candidate, so the
    remainder is attributed rather than left over.
    """
    cases = []
    for view in ("aggregated", "control", "devices"):
        for devices in (4, 12):
            base = dict(view=view, devices=devices, animation="normal",
                        feed="live", cdp_metrics=True, trace=True)
            for label, js in (("base", ""), ("noall", NO_ANIMATION_AT_ALL),
                              ("noring", NO_RING), ("notiles", STILL_TILES),
                              ("nobutton", NO_BUTTON_BORDER)):
                cases.append(scenario(**base, extra_js=js,
                                      name="a2-%s-%s-%02d" % (label, view, devices)))
    return cases


def matrix_animcost():
    """Which animation is buying the per-frame style recalculation?

    With the animation on the renderer reports one style recalculation per
    frame whose cost grows with device count -- 0.7 s per 10 s on the
    aggregated view, 2.4 s on the devices view at twelve devices. That is the
    largest single main-thread consumer left on the page, and it is worth
    knowing which construction pays for it.
    """
    cases = []
    for view in ("aggregated", "devices", "control"):
        for devices in (4, 12):
            base = dict(view=view, devices=devices, animation="normal",
                        feed="live", cdp_metrics=True, trace=True)
            cases.append(scenario(**base, name="anim-base-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=STILL_TILES,
                                  name="anim-notiles-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=FIXED_STEP_KEYFRAMES,
                                  name="anim-fixedstep-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=NO_PULSE,
                                  name="anim-nopulse-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=NO_RING,
                                  name="anim-noring-%s-%02d" % (view, devices)))
    return cases


def matrix_writemode():
    """The control view as an authenticated operator sees it.

    Everything else in this audit runs the read-only preview, where the runtime
    editor is a single line. With authentication configured it is a form per
    device, and the control view is the one that pays for it.
    """
    cases = []
    for devices in (2, 4, 8, 12):
        for scenario_name in ("normal", "write-mode"):
            cases.append(scenario(view="control", devices=devices, animation="normal",
                                  feed="live", cdp_metrics=True,
                                  scenario=scenario_name,
                                  name="write-%s-%02ddev" % (scenario_name, devices)))
    return cases


# Counts every innerHTML write and how many of them replace a subtree with
# byte-identical markup. The counter variant only observes; the guard variant
# skips the redundant writes, which is the shape any "render incrementally"
# change would have to beat. Both report through the profiler's own table.
def _innerhtml_patch(skip):
    return (
        "(() => {"
        " const d = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');"
        " if (!d || !d.set) return 0;"
        " const w = window.__prof.work;"
        " const bump = (name, ms) => {"
        "   const slot = w[name] || (w[name] = { calls: 0, ms: 0, max: 0 });"
        "   slot.calls += 1; slot.ms += ms; if (ms > slot.max) slot.max = ms; };"
        " Object.defineProperty(Element.prototype, 'innerHTML', {"
        "   configurable: true, enumerable: d.enumerable, get: d.get,"
        "   set(value) {"
        "     const same = d.get.call(this) === String(value);"
        "     const who = this.id || (this.className"
        "       ? String(this.className).split(' ')[0] : this.nodeName);"
        "     bump(same ? 'html:identical' : 'html:changed', 0);"
        "     bump((same ? 'same:' : 'diff:') + who, 0);"
        "     if (same && %s) return;"
        "     const startedAt = performance.now();"
        "     d.set.call(this, value);"
        "     bump('html:write', performance.now() - startedAt); } });"
        " return 1; })()" % ("true" if skip else "false")
    )


COUNT_HTML = _innerhtml_patch(False)
GUARD_HTML = _innerhtml_patch(True)


# Charges the control view's two mounts separately, and asks of each whether the
# string it just generated is the string it generated last time. That is the
# comparison a production guard would make. The innerHTML probe cannot make it:
# it compares a source string against the DOM's serialisation of itself, and
# `<input ... checked>` comes back as `checked=""`, so the runtime editor can
# never compare equal however unchanged it is.
MOUNT_COST_PROBE = (
    "(() => { let n = 0;"
    " const w = window.__prof.work;"
    " const bump = (name, ms) => {"
    "   const slot = w[name] || (w[name] = { calls: 0, ms: 0, max: 0 });"
    "   slot.calls += 1; slot.ms += ms; if (ms > slot.max) slot.max = ms; };"
    " const wrap = (fn, produce, label) => {"
    "   const original = window[fn];"
    "   if (typeof original !== 'function') return;"
    "   let previous = null;"
    "   window[fn] = function (...args) {"
    "     const startedAt = performance.now();"
    "     let generated = null;"
    "     try { generated = produce(args[0]); } catch (e) { generated = null; }"
    "     const unchanged = generated !== null && generated === previous;"
    "     previous = generated;"
    "     bump('mount:' + label + (unchanged ? ':same' : ':changed'), 0);"
    "     const out = original.apply(this, args);"
    "     bump('mount:' + label, performance.now() - startedAt);"
    "     return out; };"
    "   n += 1; };"
    " wrap('renderRuntimeEditorMount', () => runtimeControlPanel(), 'runtimeEditor');"
    " wrap('renderControlExplainMount', (snap) => controlExplainHtml(snap), 'explain');"
    " return n; })()"
)


def matrix_writeframes():
    """The authenticated control view draws at a third of the refresh rate.

    Read-only: 132-136 fps at every device count. Authenticated: 92.8, 53.2,
    38.7, 37.2 at 2, 4, 8 and 12 devices -- while style-recalculation time goes
    *down*, because fewer frames are produced. That is a compositor-side cost,
    and it is invisible to every other matrix in this audit because they all run
    the read-only preview. This isolates it one mechanism at a time.
    """
    cases = []
    for devices in (4, 12):
        base = dict(view="control", devices=devices, animation="normal",
                    feed="live", cdp_metrics=True, trace=True,
                    scenario="write-mode")
        for label, js in (("base", ""), ("nofilter", DISABLE_FILTERS),
                          ("noshadow", DISABLE_SHADOWS), ("noring", NO_RING),
                          ("noanim", NO_ANIMATION_AT_ALL),
                          ("nobackdrop", DISABLE_BACKDROP)):
            cases.append(scenario(**base, extra_js=js,
                                  name="wf-%s-%02d" % (label, devices)))
    # The read-only control at the same sizes, as the reference the numbers above
    # are a fall from.
    for devices in (4, 12):
        cases.append(scenario(view="control", devices=devices, animation="normal",
                              feed="live", cdp_metrics=True, trace=True,
                              name="wf-readonly-%02d" % devices))
    return cases


# Injects three controls next to the real thing, all animated by the same
# transform, and all read inside the same 600 ms block:
#   #ctlPlain  a bare fixed div, animated through the Web Animations API
#   #ctlMasked the same, but parented by a copy of the ring's masked box
#   #ctlCss    the same, driven by the page's own CSS keyframe
# If #ctlPlain does not move while the main thread is stopped, the probe cannot
# see a composited animation at all and no conclusion may be drawn from the real
# elements. That calibration is the whole point of it.
COMPOSITOR_CONTROLS = (
    "(() => {"
    " const make = (id, parent) => {"
    "   const el = document.createElement('div');"
    "   el.id = id;"
    "   el.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:4px;"
    "background:#0ff;opacity:.01;pointer-events:none;will-change:transform';"
    "   parent.appendChild(el); return el; };"
    " const host = document.createElement('div');"
    " host.style.cssText = 'position:fixed;left:0;bottom:0;width:40px;height:12px;"
    "padding:1px;overflow:hidden;opacity:.01;pointer-events:none;"
    "-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);"
    "-webkit-mask-composite:xor;mask:linear-gradient(#000 0 0) content-box,"
    "linear-gradient(#000 0 0);mask-composite:exclude';"
    " document.body.appendChild(host);"
    " const plain = make('ctlPlain', document.body);"
    " const masked = make('ctlMasked', host);"
    " const css = make('ctlCss', document.body);"
    " const frames = [{ transform: 'translate3d(0,0,0)' },"
    "                 { transform: 'translate3d(60px,0,0)' }];"
    " const timing = { duration: 4200, iterations: Infinity, easing: 'linear' };"
    " plain.animate(frames, timing); masked.animate(frames, timing);"
    " css.style.animation = 'controlResultRingSlide 4.2s linear infinite';"
    " return 3; })()"
)

PROBE_SELECTORS = "#ctlPlain,#ctlMasked,#ctlCss,.control-result-ring i,.flow-tile-inner"


# Re-drives the tile and ring animations through the Web Animations API with
# literal keyframe values, and stops the CSS ones. The pipe study credited a
# 31-46% style-time saving to expressing the keyframes as literals; this audit
# showed the `var()` is not what costs, so what WAAPI actually buys on the real
# dashboard is an open question with no measurement behind it.
#
# The control panel is rebuilt on every snapshot, so new rings keep appearing:
# the treatment re-applies on an interval. That interval is itself charged to
# the profiler and is the reason `setInterval` shows up in these runs.
USE_WAAPI = (
    "(() => { let n = 0;"
    " const apply = () => {"
    "   for (const el of document.querySelectorAll('.flow-tile-inner')) {"
    "     if (el.__waapi) continue;"
    "     const style = getComputedStyle(el);"
    "     const name = String(style.animationName || '').split(',')[0].trim();"
    "     if (!name || name === 'none') continue;"
    "     const step = parseFloat(getComputedStyle(el.parentElement)"
    "       .getPropertyValue('--tile-step')) || 0;"
    "     const seconds = parseFloat(style.animationDuration) || 0;"
    "     if (!step || !seconds) continue;"
    "     const dir = name.replace('flowTile', '').toLowerCase();"
    "     const to = dir === 'right' ? 'translate3d(' + step + 'px,0,0)'"
    "       : dir === 'left' ? 'translate3d(' + (-step) + 'px,0,0)'"
    "       : dir === 'down' ? 'translate3d(0,' + step + 'px,0)'"
    "       : 'translate3d(0,' + (-step) + 'px,0)';"
    "     const reverse = String(style.animationDirection || '')"
    "       .split(',')[0].trim() === 'reverse';"
    "     el.style.animation = 'none';"
    "     el.animate([{ transform: 'translate3d(0,0,0)' }, { transform: to }],"
    "       { duration: seconds * 1000, iterations: Infinity, easing: 'linear',"
    "         direction: reverse ? 'reverse' : 'normal' });"
    "     el.__waapi = true; n += 1; }"
    "   for (const el of document.querySelectorAll("
    "       '.control-result-ring i, .button-ring i')) {"
    "     if (el.__waapi) continue;"
    "     const style = getComputedStyle(el);"
    "     const name = String(style.animationName || '').split(',')[0].trim();"
    "     if (!name || name === 'none') continue;"
    "     const seconds = parseFloat(style.animationDuration) || 4.2;"
    "     el.style.animation = 'none';"
    "     el.animate([{ transform: 'translate3d(0,0,0)' },"
    "                 { transform: 'translate3d(-50%,0,0)' }],"
    "       { duration: seconds * 1000, iterations: Infinity, easing: 'linear' });"
    "     el.__waapi = true; n += 1; } };"
    " apply(); setInterval(apply, 400);"
    " return n; })()"
)


# Splits the control explanation into the two halves an incremental renderer
# would treat differently: building the markup string, and handing it to the
# parser. Rewriting the panel in place can only ever save the second -- the
# first is the work of deciding what the panel should say, which any renderer
# has to do. So this measures the ceiling of that change before anyone builds
# it.
SPLIT_EXPLAIN_PROBE = (
    "(() => {"
    " const w = window.__prof.work;"
    " const bump = (name, ms) => {"
    "   const slot = w[name] || (w[name] = { calls: 0, ms: 0, max: 0 });"
    "   slot.calls += 1; slot.ms += ms; if (ms > slot.max) slot.max = ms; };"
    " if (typeof renderControlExplainMount !== 'function') return 0;"
    " window.renderControlExplainMount = function (snapshot) {"
    "   const mount = document.getElementById('controlExplainMount');"
    "   if (!mount) return;"
    "   const t0 = performance.now();"
    "   const html = controlExplainHtml(snapshot);"
    "   const t1 = performance.now();"
    "   mount.innerHTML = html;"
    "   const t2 = performance.now();"
    "   bump('explain:generate', t1 - t0);"
    "   bump('explain:write', t2 - t1);"
    "   bump('explain:bytes', html.length); };"
    " return 1; })()"
)


# Tiles only, and no re-apply interval: the tile layer is rebuilt when its
# signature changes and not on every snapshot, so one pass is enough. The
# combined treatment above had to re-apply on a timer because the control panel
# *is* rebuilt every snapshot, and that timer is charged to the page -- which is
# both a measurement artefact and the argument against converting the rings.
USE_WAAPI_TILES = (
    "(() => { let n = 0;"
    " const apply = () => {"
    "   for (const el of document.querySelectorAll('.flow-tile-inner')) {"
    "     if (el.__waapi) continue;"
    "     if (el.closest && el.closest('[hidden]')) continue;"
    "     const style = getComputedStyle(el);"
    "     const name = String(style.animationName || '').split(',')[0].trim();"
    "     if (!name || name.indexOf('flowTile') !== 0) continue;"
    "     const step = parseFloat(getComputedStyle(el.parentElement)"
    "       .getPropertyValue('--tile-step')) || 0;"
    "     const seconds = parseFloat(style.animationDuration) || 0;"
    "     if (!step || !seconds) continue;"
    "     const dir = name.replace('flowTile', '').toLowerCase();"
    "     const to = dir === 'right' ? 'translate3d(' + step + 'px,0,0)'"
    "       : dir === 'left' ? 'translate3d(' + (-step) + 'px,0,0)'"
    "       : dir === 'down' ? 'translate3d(0,' + step + 'px,0)'"
    "       : 'translate3d(0,' + (-step) + 'px,0)';"
    "     const reverse = String(style.animationDirection || '')"
    "       .split(',')[0].trim() === 'reverse';"
    "     el.style.animation = 'none';"
    "     el.animate([{ transform: 'translate3d(0,0,0)' }, { transform: to }],"
    "       { duration: seconds * 1000, iterations: Infinity, easing: 'linear',"
    "         direction: reverse ? 'reverse' : 'normal' });"
    "     el.__waapi = true; n += 1; } };"
    " apply(); return n; })()"
)


def matrix_tilewaapi():
    """The tile animation on the Web Animations API, with nothing else changed."""
    cases = []
    for view in ("aggregated", "devices"):
        for devices in (4, 12):
            base = dict(view=view, devices=devices, animation="normal",
                        feed="live", cdp_metrics=True, trace=True)
            cases.append(scenario(**base, name="tw-css-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=USE_WAAPI_TILES,
                                  name="tw-js-%s-%02d" % (view, devices)))
    # The rings, measured where the panel is not rebuilt underneath them: a
    # silent feed renders once and then nothing arrives, so one pass holds.
    for devices in (4, 12):
        base = dict(view="control", devices=devices, animation="normal",
                    feed="silent", cdp_metrics=True, trace=True,
                    scenario="write-mode")
        cases.append(scenario(**base, name="tw-ring-css-%02d" % devices))
        cases.append(scenario(**base, extra_js=USE_WAAPI,
                              name="tw-ring-js-%02d" % devices))
    return cases


def matrix_waapithrottle():
    """Does the animation's main-thread cost become visible on a slow machine?

    On this desktop the Web Animations API saves 18-42% of style-recalculation
    time and not one frame: everything sits at the refresh ceiling either way.
    That is the whole argument for leaving it alone, and it is an argument about
    a machine with headroom. Chromium's CPU throttling removes the headroom by
    an exact factor, which is the only way to ask the question here.
    """
    cases = []
    for rate in (1, 4, 8, 16):
        for view in ("aggregated", "devices"):
            base = dict(view=view, devices=12, animation="normal", feed="live",
                        cdp_metrics=True, cpu_throttle=rate)
            cases.append(scenario(**base, name="thr%02d-css-%s" % (rate, view)))
            cases.append(scenario(**base, extra_js=USE_WAAPI_TILES,
                                  name="thr%02d-js-%s" % (rate, view)))
    return cases


def matrix_mountsplit():
    """Generating the control panel's markup, against handing it to the parser.

    The control view is the only one whose per-snapshot cost scales with device
    count, and the remedy on the table is to update it in place instead of
    replacing it. That can only save the parse. This says how much of the cost
    the parse actually is.
    """
    cases = []
    for scenario_name in ("normal", "write-mode"):
        for devices in (2, 4, 8, 12):
            cases.append(scenario(view="control", devices=devices,
                                  animation="normal", feed="live",
                                  cdp_metrics=True, scenario=scenario_name,
                                  extra_js=SPLIT_EXPLAIN_PROBE,
                                  name="split-%s-%02ddev" % (scenario_name, devices)))
    return cases


def matrix_waapi():
    """Does the Web Animations API remove the per-frame style recalculation?

    Every style recalculation on this page comes from an animation, and the
    cost tracks how many are running. The one look-preserving alternative is to
    drive them with literal keyframe values through element.animate(). Whether
    that changes anything here has never been measured on the dashboard itself.
    """
    cases = []
    for view in ("aggregated", "devices", "control"):
        for devices in (4, 12):
            base = dict(view=view, devices=devices, animation="normal",
                        feed="live", cdp_metrics=True, trace=True)
            cases.append(scenario(**base, name="waapi-css-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=USE_WAAPI,
                                  name="waapi-js-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=NO_ANIMATION_AT_ALL,
                                  name="waapi-none-%s-%02d" % (view, devices)))
    # And the one that matters commercially: the authenticated control view,
    # which is where the animations are most numerous.
    for devices in (4, 12):
        base = dict(view="control", devices=devices, animation="normal",
                    feed="live", cdp_metrics=True, trace=True,
                    scenario="write-mode")
        cases.append(scenario(**base, name="waapi-css-write-%02d" % devices))
        cases.append(scenario(**base, extra_js=USE_WAAPI,
                              name="waapi-js-write-%02d" % devices))
    return cases


def matrix_compositor2():
    """The same question as `compositor`, with a control that calibrates it."""
    cases = []
    for view in ("control", "aggregated"):
        for devices in (4, 12):
            cases.append(scenario(view=view, devices=devices, animation="normal",
                                  feed="live", cdp_metrics=True, trace=True,
                                  extra_js=COMPOSITOR_CONTROLS,
                                  compositor_probe=PROBE_SELECTORS,
                                  name="comp2-%s-%02d" % (view, devices)))
    return cases


def matrix_buttonborder():
    """Name the animation that costs the authenticated control view its frames.

    Stopping every animation restores 136 fps and takes the paint count from
    4431 per ten seconds to 175; stopping the result ring alone changes nothing.
    The only other animation in that view is `controlResultBorderFlow` on
    `.primary-button.compact::after`, which animates `background-position` -- a
    paint property -- and the runtime editor renders one submit button per stage
    card. The previous investigation kept that construction on the grounds that
    it was "a single element and measured free", which was measured on the
    read-only preview, where those buttons do not exist.
    """
    cases = []
    for devices in (4, 12):
        base = dict(view="control", devices=devices, animation="normal",
                    feed="live", cdp_metrics=True, trace=True,
                    scenario="write-mode")
        cases.append(scenario(**base, name="bb-base-%02d" % devices))
        cases.append(scenario(**base, extra_js=NO_BUTTON_BORDER,
                              name="bb-nobutton-%02d" % devices))
        cases.append(scenario(**base, extra_js=NO_ANIMATION_AT_ALL,
                              name="bb-noanim-%02d" % devices))
        # How many of them there are, so the cost has a denominator.
        cases.append(scenario(**base, extra_js=(
            "(() => document.querySelectorAll('.primary-button.compact')"
            ".length)()"), name="bb-count-%02d" % devices))
    return cases


def matrix_mountcost():
    """What each half of the control view costs, and whether it changed.

    `runtimeControlPanel()` takes no snapshot and reads none -- only
    `state.runtime` and `state.auth`, which change when `/api/runtime` is
    re-fetched and not otherwise. This charges the two mounts separately and
    compares each generated string with the one before it, which is the
    comparison a guard in the page would make and the one the innerHTML probe
    structurally cannot.
    """
    cases = []
    for scenario_name in ("normal", "write-mode"):
        for devices in (4, 12):
            cases.append(scenario(view="control", devices=devices,
                                  animation="normal", feed="live",
                                  cdp_metrics=True, scenario=scenario_name,
                                  extra_js=MOUNT_COST_PROBE,
                                  name="mount-%s-%02ddev" % (scenario_name, devices)))
    return cases


def matrix_htmlguard():
    """How much of the per-snapshot DOM churn replaces markup with itself?

    Every view renderer here writes `innerHTML` for a whole panel on every
    snapshot. If the markup it writes is usually the same markup that is
    already there, the parse and the subtree rebuild are pure waste and the
    cheapest possible fix is a comparison -- which is what the guard variant
    measures.
    """
    cases = []
    for view in ("aggregated", "devices", "control", "energy"):
        for devices in (4, 12):
            base = dict(view=view, devices=devices, animation="normal",
                        feed="live", cdp_metrics=True)
            cases.append(scenario(**base, extra_js=COUNT_HTML,
                                  name="html-count-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=GUARD_HTML,
                                  name="html-guard-%s-%02d" % (view, devices)))
    # The preview's snapshot is byte-identical between deliveries apart from its
    # timestamp, so a panel whose markup is derived from telemetry looks
    # redundant here and would not be in a real installation. `write-mode` is
    # the case that separates them: the runtime editor is a form per device and
    # is built from `state.runtime`, which no snapshot touches.
    for devices in (4, 12):
        base = dict(view="control", devices=devices, animation="normal",
                    feed="live", cdp_metrics=True, scenario="write-mode")
        cases.append(scenario(**base, extra_js=COUNT_HTML,
                              name="html-count-write-%02d" % devices))
        cases.append(scenario(**base, extra_js=GUARD_HTML,
                              name="html-guard-write-%02d" % devices))
    return cases


# Empties the control panel's subtree without touching anything else, so the
# cost of *carrying* an off-screen view is separable from the cost of building
# it. Returns how many nodes it removed.
DROP_CONTROL_SUBTREE = (
    "(() => {"
    " const el = document.getElementById('controlExplainView');"
    " if (!el) return 0;"
    " const n = el.getElementsByTagName('*').length;"
    " el.textContent = '';"
    " return n; })()"
)


def matrix_retained():
    """Does an off-screen view cost anything just by still being in the document?

    On the aggregated view at twelve devices the control panel is 3606 of the
    document's 4065 nodes and the only part that grows with device count, while
    the flow SVG actually on screen is 95. This asks whether those nodes cost
    anything per snapshot once they are no longer being rebuilt.
    """
    cases = []
    for view in ("aggregated", "energy"):
        for devices in (4, 12):
            base = dict(view=view, devices=devices, animation="normal",
                        feed="live", cdp_metrics=True)
            cases.append(scenario(**base, name="retained-keep-%s-%02d" % (view, devices)))
            cases.append(scenario(**base, extra_js=DROP_CONTROL_SUBTREE,
                                  name="retained-drop-%s-%02d" % (view, devices)))
    return cases


# Playwright reports document.hidden === false for a page that is merely not in
# front, so the dashboard's own hidden-tab deferral never engages in this
# harness. Overriding the property exercises that path directly; it measures the
# code, not the browser's own background throttling.
FORCE_HIDDEN = (
    "(() => {"
    " Object.defineProperty(document, 'hidden', { configurable: true,"
    "   get: () => true });"
    " Object.defineProperty(document, 'visibilityState', { configurable: true,"
    "   get: () => 'hidden' });"
    " return document.hidden === true ? 1 : 0; })()"
)


def matrix_hiddentab():
    """What a genuinely hidden tab costs, as opposed to an unfocused window."""
    cases = []
    for view in ("devices", "control"):
        base = dict(view=view, devices=8, animation="normal", feed="live",
                    cdp_metrics=True)
        cases.append(scenario(**base, name="tab-visible-%s" % view))
        cases.append(scenario(**base, extra_js=FORCE_HIDDEN,
                              name="tab-hidden-%s" % view))
        cases.append(scenario(**base, neighbour=True, foreground="neighbour",
                              name="tab-unfocused-%s" % view))
    return cases


def matrix_unfocused():
    """The unfocused window at four devices, and repeated, before it is believed.

    At eight devices a visible-but-unfocused dashboard measured two to three
    times the per-snapshot cost of a focused one, with its animation frames
    throttled to 1 fps. The previous investigation measured the same arrangement
    at four devices and reported no difference, so this repeats it there, with
    the animation both on and off. Pass ``--repeat 3`` to run each cell three
    times; a single pass is one sample of a claim that contradicts an earlier
    one, and the per-snapshot column is noisy at this size.
    """
    cases = []
    for view in ("devices", "control"):
        for animation in ("normal", "off"):
            base = dict(view=view, devices=4, animation=animation, feed="live",
                        cdp_metrics=True)
            cases.append(scenario(**base,
                                  name="focus-front-%s-anim%s" % (view, animation)))
            cases.append(scenario(**base, neighbour=True, foreground="neighbour",
                                  name="focus-back-%s-anim%s" % (view, animation)))
    return cases


MATRICES = {
    "waapithrottle": matrix_waapithrottle,
    "tilewaapi": matrix_tilewaapi,
    "mountsplit": matrix_mountsplit,
    "waapi": matrix_waapi,
    "compositor2": matrix_compositor2,
    "buttonborder": matrix_buttonborder,
    "writeframes": matrix_writeframes,
    "mountcost": matrix_mountcost,
    "unfocused": matrix_unfocused,
    "compositor": matrix_compositor,
    "animcost2": matrix_animcost2,
    "retained": matrix_retained,
    "hiddentab": matrix_hiddentab,
    "htmlguard": matrix_htmlguard,
    "writemode": matrix_writemode,
    "animcost": matrix_animcost,
    "offscreen": matrix_offscreen,
    "scale": matrix_scale,
    "pipeline": matrix_pipeline,
    "lifecycle": matrix_lifecycle,
    "longrun": matrix_longrun,
    "css": matrix_css,
    "hiddenwork": matrix_hiddenwork,
    "feed": matrix_feed,
    "attribution": matrix_attribution,
    "visibility": matrix_visibility,
    "neighbour": matrix_neighbour,
    "software": matrix_software,
    "charts": matrix_charts,
    "flowlayer": matrix_flowlayer,
    "reads": matrix_reads,
    "throttle": matrix_throttle,
    "reads-after": matrix_reads_after,
    "result-border": matrix_result_border,
    "result-border-parts": matrix_result_border_parts,
    "devices": matrix_devices,
}


# A headed window that ends up behind another application is throttled by the
# browser to one animation frame per second. It is a discrete state, not a slow
# machine: the frame rate is 1.0 and the frame time is 1000 ms, whatever the page
# is doing. Nothing else in this harness can tell that apart from a result, and a
# whole matrix of them was very nearly reported as a regression.
def looks_occluded(case, result):
    if not result:
        return False
    # Two matrices put the dashboard behind another page deliberately, and there
    # the 1 fps *is* the measurement. Only an unasked-for background counts.
    if case.get("foreground") != "dashboard" or case.get("neighbour"):
        return False
    for page in (result.get("dashboard"), result.get("neighbour")):
        if not page:
            continue
        fps = page.get("fps")
        frame_p95 = page.get("frameP95Ms")
        if fps is not None and fps < 5 and (frame_p95 or 0) > 500:
            return True
    return False


def run_case(node, case, browser, gpu, duration_ms, max_load, attempts=3):
    quiet = bench.wait_for_quiet(max_load) if hasattr(bench, "wait_for_quiet") else None
    load = bench.load_average() if hasattr(bench, "load_average") else None
    feed = FEEDS[case["feed"]]
    server = start_server(
        host="127.0.0.1",
        port=0,
        scenario=case.get("scenario", "normal"),
        device_count=case["devices"],
        freeze_timestamp=feed["freeze_timestamp"],
        sse_interval=feed["sse_interval"],
    )
    host, port = server.server_address
    payload = {
        "url": "http://%s:%s/" % (host, port),
        "view": case["view"],
        "animation": case["animation"],
        "transport": case["transport"],
        "durationMs": duration_ms,
        "browser": browser,
        "gpu": gpu,
        "software": bool(case["software"]),
        "deepReads": bool(case["deep_reads"]),
        "cpuThrottle": int(case["cpu_throttle"]),
        "foreground": case["foreground"],
        "dashboardOpen": bool(case["dashboard_open"]),
        "neighbourUrl": None,
        "extraJs": case["extra_js"],
        "navigationTimeoutMs": 90000,
        "cdpMetrics": bool(case.get("cdp_metrics")),
        "trace": bool(case.get("trace")),
        "cycleViews": case.get("cycle_views"),
        "cycleIntervalMs": int(case.get("cycle_interval_ms") or 2000),
        "sampleMs": int(case.get("sample_ms") or 0),
        "gc": bool(case.get("gc")),
        "compositorProbe": case.get("compositor_probe"),
    }
    if case.get("duration_ms"):
        payload["durationMs"] = int(case["duration_ms"])
    if case["neighbour"] and case["foreground"] != "neighbour":
        payload["foreground"] = case["foreground"]
    try:
        completed = subprocess.run(
            [node, DRIVER, json.dumps(payload)],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=max(300, payload["durationMs"] / 1000 * 6 + 300),
        )
    finally:
        server.shutdown()
        server.server_close()
    entry = {"case": case, "load_average": load, "quiet_gate": quiet}
    if completed.returncode != 0:
        entry["error"] = completed.stderr.strip()[:2000]
    else:
        entry["result"] = json.loads(completed.stdout)
    if looks_occluded(case, entry.get("result")):
        entry["occluded"] = True
        if attempts > 1:
            print("  . window was occluded (1 fps); retrying", file=sys.stderr,
                  flush=True)
            retry = run_case(node, case, browser, gpu, duration_ms, max_load,
                             attempts - 1)
            retry["occluded_attempts"] = entry.get("occluded_attempts", 0) + 1
            return retry
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=sorted(MATRICES), default="attribution")
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="firefox")
    parser.add_argument("--gpu", choices=("software", "gpu", "headed"), default="headed")
    parser.add_argument("--max-load", type=float, default=2.0)
    parser.add_argument("--duration-ms", type=int, default=10000)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--label", default=None)
    args = parser.parse_args(argv)

    node = bench.node_binary()
    if not node:
        raise SystemExit("node is required to drive Playwright")
    executable = bench.browser_available(node, args.browser)
    if not executable:
        raise SystemExit(
            "playwright's %s build is not installed; run: npx playwright install %s"
            % (args.browser, args.browser)
        )

    cases = MATRICES[args.matrix]()
    runs = []
    started = time.monotonic()
    for repeat in range(args.repeat):
        for index, case in enumerate(cases, 1):
            label = "[%d/%d] %s" % (index, len(cases), case["name"])
            if args.repeat > 1:
                label += " (run %d/%d)" % (repeat + 1, args.repeat)
            print(label, file=sys.stderr, flush=True)
            entry = run_case(node, case, args.browser, args.gpu,
                             args.duration_ms, args.max_load)
            entry["repeat"] = repeat + 1
            runs.append(entry)

    report = {
        "schema_version": 1,
        "kind": "dashboard-profile",
        "label": args.label or args.matrix,
        "matrix": args.matrix,
        "duration_ms": args.duration_ms,
        "environment": {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "browser": args.browser,
            "browser_executable": executable,
            "gpu": args.gpu,
            "note": (
                "Read rasterisation.renderer per run rather than assuming a "
                "path. The renderer string names the device WebGL was given, "
                "not the page compositor: it can prove software, never "
                "hardware. Compare ratios within one browser and session."
            ),
        },
        "wall_clock_seconds": round(time.monotonic() - started, 1),
        "occluded_cases": sum(1 for r in runs if r.get("occluded")),
        "runs": runs,
    }
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = "profile-%s-%s-%s.json" % (report["label"], args.browser, stamp)
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
