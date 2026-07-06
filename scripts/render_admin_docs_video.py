# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render short Admin Console workflow demo videos for the user docs.

Reuses the docs-preview server (deterministic, non-secret demo data) to render
the real Admin static UI, captures each workflow step as a full-page frame with
headless Firefox, then stitches the frames into a 720p ``.webm`` with gentle
vertical pans (so tall pages stay readable) using ffmpeg.

Each committed demo ships in two formats: WebM/VP9 (compact) and MP4/H.264
(``yuv420p`` + ``faststart``, best compatibility for forums and mobile
browsers). By default both are produced; the MP4 is a transcode of the freshly
rendered WebM, so the two always show identical content. Use ``--format`` to
render only one.

No hardware, Docker, discovery, MQTT, config.json or password is involved, and
nothing is written to config/runtime state.

Usage::

    python3 scripts/render_admin_docs_video.py                 # both formats
    python3 scripts/render_admin_docs_video.py --format webm    # WebM only
    python3 scripts/render_admin_docs_video.py --format mp4     # MP4 from existing WebM
    python3 scripts/render_admin_docs_video.py --videos admin-guided-setup-demo.webm

Requires ``ffmpeg`` always; WebM rendering additionally needs firefox (headless)
and ImageMagick ``convert``. MP4-only reuses the committed WebM as its source.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from serve_admin_docs_preview import DEFAULT_HOST, DEFAULT_PORT, start_server  # noqa: E402

CANVAS_W, CANVAS_H = 1280, 720
PAGE_BG = "#070a0f"
FPS = 30
# Tall capture window so a full page renders; the empty tail is trimmed away.
PAGE_WINDOW = f"{CANVAS_W},3400"
SCREEN_WINDOW = f"{CANVAS_W},{CANVAS_H}"
MAX_ATTEMPTS = 6

# Each step: screen id (see admin_docs_preview.js), capture mode and hold time.
#   mode "page"   -> full-page frame, panned if taller than the canvas
#   mode "screen" -> fixed 1280x720 frame, shown static (landing, overlay)
# min_h is the trimmed content height that proves the demo data rendered.
VIDEOS = {
    "admin-guided-setup-demo.webm": {
        "description": "Fresh install / Guided Setup with hardware discovery",
        "steps": [
            {"screen": "landing", "mode": "screen", "seconds": 3.0, "min_h": 380},
            {"screen": "guided-setup-start", "mode": "page", "seconds": 5.5, "min_h": 700},
            {"screen": "discovery", "mode": "page", "seconds": 6.5, "min_h": 900},
            # Config page pans through the generated config and the expanded
            # feature list (taller page, longer pan).
            {"screen": "config-preview", "mode": "page", "seconds": 9.0, "min_h": 1550},
            # End on the Start step: EMS running and the "Open EMS Dashboard"
            # (localhost:8080) success card at the bottom.
            {"screen": "setup-start-done", "mode": "bottom", "seconds": 4.0, "min_h": 600},
        ],
    },
    "admin-guided-upgrade-demo.webm": {
        "description": "Guided EMS software upgrade with live validation checkmarks",
        "steps": [
            {"screen": "landing", "mode": "screen", "seconds": 2.5, "min_h": 380},
            # Show the full guided-upgrade plan once (the four stages and the
            # "Upgrade EMS" button), then lock onto the "04 Upgrade validation"
            # box at the bottom for the rest of the run.
            {"screen": "guided-upgrade", "mode": "page", "seconds": 6.5, "min_h": 1250},
            # Live EMS upgrade: the validation box ticks off each step in green.
            # min_h clears the taller filled step list (~1350px), so a cold-start
            # capture of the empty "Planning" box (~1160px) is retried, not kept.
            {"screen": "upgrade-run-1", "mode": "bottom", "seconds": 3.0, "min_h": 1300},
            {"screen": "upgrade-run-2", "mode": "bottom", "seconds": 3.0, "min_h": 1300},
            {"screen": "upgrade-run-3", "mode": "bottom", "seconds": 3.5, "min_h": 1300},
            {"screen": "upgrade-run-4", "mode": "bottom", "seconds": 3.0, "min_h": 1300},
            # End on the completed plan: every step green, "Upgrade completed".
            {"screen": "upgrade-done", "mode": "bottom", "seconds": 4.5, "min_h": 1300},
        ],
    },
}


def require_executable(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required executable not found: {name}")
    return path


def _shoot(firefox, url, profile, window, out_png):
    subprocess.run(
        [
            firefox,
            "--headless",
            "--new-instance",
            "-no-remote",
            "-profile",
            profile,
            f"--window-size={window}",
            "--screenshot",
            out_png,
            url,
        ],
        check=True,
        cwd=ROOT,
        env={**os.environ, "MOZ_HEADLESS": "1"},
    )


def _height(convert, path):
    out = subprocess.run(
        [convert, path, "-format", "%h", "info:"],
        check=True, cwd=ROOT, capture_output=True, text=True,
    )
    return int(out.stdout.strip())


def _normalize_page(convert, raw, out):
    # Trim the empty window tail/margins to the content, then restore a fixed
    # canvas width so every frame pans on the same 1280px column.
    subprocess.run(
        [convert, raw, "-fuzz", "6%", "-trim", "+repage", out],
        check=True, cwd=ROOT,
    )
    height = _height(convert, out)
    subprocess.run(
        [convert, out, "-background", PAGE_BG, "-gravity", "center",
         "-extent", f"{CANVAS_W}x{height}", out],
        check=True, cwd=ROOT,
    )
    return height


def capture_frame(firefox, convert, host, port, tmpdir, step):
    screen = step["screen"]
    mode = step["mode"]
    window = SCREEN_WINDOW if mode == "screen" else PAGE_WINDOW
    url = f"http://{host}:{port}/?screen={screen}"
    best, best_h = None, -1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = os.path.join(tmpdir, f"{screen}-{attempt}-raw.png")
        profile = os.path.join(tmpdir, f"prof-{screen}-{attempt}")
        os.makedirs(profile, exist_ok=True)
        _shoot(firefox, url, profile, window, raw)
        if mode == "screen":
            # A fixed-canvas frame; measure trimmed content to confirm it drew.
            probe = os.path.join(tmpdir, f"{screen}-{attempt}-probe.png")
            subprocess.run([convert, raw, "-fuzz", "6%", "-trim", "+repage", probe],
                           check=True, cwd=ROOT)
            height = _height(convert, probe)
            frame = raw
        else:
            frame = os.path.join(tmpdir, f"{screen}-{attempt}.png")
            height = _normalize_page(convert, raw, frame)
        if height > best_h:
            best, best_h = frame, height
        if best_h >= step["min_h"]:
            break
        time.sleep(1.0)
    if best_h < step["min_h"]:
        print(f"  warning: {screen} rendered short ({best_h}px < {step['min_h']}px)")
    return best


def _segment_filter(idx, seconds, height, mode):
    """Build the per-frame filter chain (static, pan, or fixed bottom) + fades."""
    dur = seconds
    fade = 0.3
    common = f"fps={FPS},setsar=1,fade=t=in:st=0:d={fade},fade=t=out:st={dur - fade}:d={fade}"
    if mode == "bottom" and height > CANVAS_H:
        # Hold a fixed 1280x720 window on the bottom of the page (e.g. the
        # success card) instead of panning.
        geom = f"scale={CANVAS_W}:-1,crop={CANVAS_W}:{CANVAS_H}:0:ih-{CANVAS_H}"
    elif height <= CANVAS_H:
        # Center the short frame on the canvas and hold it.
        geom = (
            f"scale={CANVAS_W}:-1,"
            f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color={PAGE_BG}"
        )
    else:
        # Pan a 1280x720 window down the page, holding ~0.9s at top and bottom.
        hold = 0.9
        span = max(dur - 2 * hold, 0.1)
        y = f"min(max((t-{hold})/{span}\\,0)\\,1)*(ih-{CANVAS_H})"
        geom = f"scale={CANVAS_W}:-1,crop={CANVAS_W}:{CANVAS_H}:0:'{y}'"
    return f"[{idx}:v]{geom},{common},format=yuv420p[v{idx}]"


def render_video(ffmpeg, convert, frames, steps, out_path):
    inputs = []
    filters = []
    labels = []
    for idx, (frame, step) in enumerate(zip(frames, steps)):
        inputs += ["-loop", "1", "-t", str(step["seconds"]), "-i", frame]
        height = _height(convert, frame)
        filters.append(_segment_filter(idx, step["seconds"], height, step.get("mode", "page")))
        labels.append(f"[v{idx}]")
    graph = ";".join(filters)
    graph += ";" + "".join(labels) + f"concat=n={len(frames)}:v=1:a=0[v]"
    subprocess.run(
        [ffmpeg, "-y", *inputs,
         "-filter_complex", graph, "-map", "[v]",
         "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36",
         "-deadline", "good", "-cpu-used", "2", "-pix_fmt", "yuv420p",
         "-an", out_path],
        check=True, cwd=ROOT, capture_output=True,
    )


def transcode_to_mp4(ffmpeg, webm_path, mp4_path):
    """Transcode a rendered WebM into a forum/mobile-friendly H.264 MP4.

    H.264 + ``yuv420p`` + ``+faststart`` gives the widest browser and forum
    compatibility (e.g. the Zendure forum), where MP4 is usually safer than
    WebM. No audio track is produced.
    """
    subprocess.run(
        [ffmpeg, "-y", "-i", webm_path,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         "-crf", "23", "-preset", "medium", "-an", mp4_path],
        check=True, cwd=ROOT, capture_output=True,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT + 1)
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "docs", "assets", "videos", "admin"),
    )
    parser.add_argument("--videos", nargs="+", choices=sorted(VIDEOS))
    parser.add_argument(
        "--format",
        choices=("webm", "mp4", "all"),
        default="all",
        help=(
            "which format(s) to produce (default: all). 'mp4' transcodes the "
            "existing committed WebM and needs no browser/capture tooling."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    want_webm = args.format in ("webm", "all")
    want_mp4 = args.format in ("mp4", "all")
    ffmpeg = require_executable("ffmpeg")
    names = args.videos or list(VIDEOS)
    os.makedirs(args.output_dir, exist_ok=True)
    written = []

    if want_webm:
        firefox = require_executable("firefox")
        convert = require_executable("convert")
        display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        server = start_server(args.host, args.port)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                for name in names:
                    spec = VIDEOS[name]
                    print(f"Rendering {name} ...")
                    frames = [
                        capture_frame(firefox, convert, display_host, args.port, tmpdir, step)
                        for step in spec["steps"]
                    ]
                    out_path = os.path.join(args.output_dir, name)
                    render_video(ffmpeg, convert, frames, spec["steps"], out_path)
                    written.append(out_path)
        finally:
            server.shutdown()
            server.server_close()

    if want_mp4:
        for name in names:
            webm_path = os.path.join(args.output_dir, name)
            mp4_path = os.path.splitext(webm_path)[0] + ".mp4"
            if not os.path.isfile(webm_path):
                raise SystemExit(
                    f"cannot make MP4: source WebM not found: {webm_path}\n"
                    "render the WebM first (--format webm or --format all)."
                )
            print(f"Transcoding {name} -> {os.path.basename(mp4_path)} ...")
            transcode_to_mp4(ffmpeg, webm_path, mp4_path)
            written.append(mp4_path)

    print("Rendered Admin Console demo videos:")
    for path in written:
        size = os.path.getsize(path)
        print(f"  {os.path.relpath(path, ROOT)}  ({size // 1024} KiB)")


if __name__ == "__main__":
    main()
