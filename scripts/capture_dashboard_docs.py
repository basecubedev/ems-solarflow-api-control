# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture EMS Dashboard documentation screenshots.

Thin wrapper around ``scripts/serve_dashboard_preview.py`` and the shared
``scripts/dashboard_preview_data.py`` payloads that drives Firefox headless +
ImageMagick ``convert`` to write the docs screenshots under
``docs/assets/screenshots/dashboard/`` from deterministic, non-secret demo data.

Companion to ``scripts/capture_admin_docs.py``; the two together are driven by
``scripts/capture-docs-screenshots.sh``.

Usage::

    python3 scripts/capture_dashboard_docs.py
    python3 scripts/capture_dashboard_docs.py --screens overview control
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

from serve_dashboard_preview import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    start_server,
)

DESKTOP = "1440,2600"
NARROW = "390,1800"

# screen id -> (flow view, scenario, output PNG basename, min trimmed height,
# window size). The height floor detects a frame captured before the dashboard
# finished rendering its demo data, so the capture is retried instead of a
# half-rendered frame being kept. Each entry is one documented state; the same
# view appears more than once only where a scenario changes what it shows.
SCREENS = {
    "overview": ("aggregated", "normal", "dashboard-overview.png", 900, DESKTOP),
    "overview-narrow": (
        "aggregated",
        "normal",
        "dashboard-overview-narrow.png",
        900,
        NARROW,
    ),
    "devices": ("devices", "normal", "dashboard-devices.png", 900, DESKTOP),
    "devices-offline": (
        "devices",
        "offline-device",
        "dashboard-devices-offline.png",
        900,
        DESKTOP,
    ),
    "devices-readonly": (
        "devices",
        "auth-readonly",
        "dashboard-devices-readonly.png",
        700,
        DESKTOP,
    ),
    "energy": ("energy", "normal", "dashboard-energy.png", 700, DESKTOP),
    "analytics": ("analytics", "normal", "dashboard-analytics.png", 700, DESKTOP),
    "control": ("control", "write-mode", "dashboard-control.png", 1400, DESKTOP),
    "control-readonly": (
        "control",
        "auth-readonly",
        "dashboard-control-readonly.png",
        900,
        DESKTOP,
    ),
    "diagnose": ("diagnose", "write-mode", "dashboard-diagnose.png", 700, DESKTOP),
    "logs": ("logs", "write-mode", "dashboard-logs.png", 600, DESKTOP),
    "maintenance": (
        "maintenance",
        "write-mode",
        "dashboard-maintenance.png",
        1000,
        DESKTOP,
    ),
}
MAX_ATTEMPTS = 5

# Page background (styles.css --bg); the trim border is filled with it so every
# screenshot keeps a small, consistent gutter.
PAGE_BG = "#070a0f"


def require_executable(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required executable not found: {name}")
    return path


def _image_height(convert, path):
    out = subprocess.run(
        [convert, path, "-format", "%h", "info:"],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _shoot(firefox, url, profile, png_raw, window_size):
    subprocess.run(
        [
            firefox,
            "--headless",
            "--new-instance",
            "-no-remote",
            "-profile",
            profile,
            f"--window-size={window_size}",
            "--screenshot",
            png_raw,
            url,
        ],
        check=True,
        cwd=ROOT,
        env={**os.environ, "MOZ_HEADLESS": "1"},
    )


def _trim(convert, png_raw, out_path):
    subprocess.run(
        [
            convert,
            png_raw,
            "-fuzz",
            "8%",
            "-trim",
            "+repage",
            "-bordercolor",
            PAGE_BG,
            "-border",
            "28",
            "-strip",
            out_path,
        ],
        check=True,
        cwd=ROOT,
    )


def capture(server, host, port, output_dir, screens):
    firefox = require_executable("firefox")
    convert = require_executable("convert")
    os.makedirs(output_dir, exist_ok=True)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    written = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for screen in screens:
            view, scenario, basename, min_height, window_size = SCREENS[screen]
            # The handler reads the scenario per request, so one server can serve
            # every documented scenario without a restart.
            server.scenario_name = scenario
            url = f"http://{display_host}:{port}/preview/{view}"
            out_path = os.path.join(output_dir, basename)
            best_path, best_height = None, -1
            for attempt in range(1, MAX_ATTEMPTS + 1):
                png_raw = os.path.join(tmpdir, f"{screen}-{attempt}.png")
                trimmed = os.path.join(tmpdir, f"{screen}-{attempt}-trim.png")
                profile = os.path.join(tmpdir, f"profile-{screen}-{attempt}")
                os.makedirs(profile, exist_ok=True)
                _shoot(firefox, url, profile, png_raw, window_size)
                _trim(convert, png_raw, trimmed)
                height = _image_height(convert, trimmed)
                if height > best_height:
                    best_path, best_height = trimmed, height
                if best_height >= min_height:
                    break
                time.sleep(1.0)
            if best_height < min_height:
                print(
                    f"  warning: {screen} rendered short ({best_height}px < "
                    f"{min_height}px) after {MAX_ATTEMPTS} attempts"
                )
            shutil.copyfile(best_path, out_path)
            written.append(out_path)
    return written


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "docs", "assets", "screenshots", "dashboard"),
    )
    parser.add_argument(
        "--screens",
        nargs="+",
        metavar="SCREEN",
        choices=sorted(SCREENS),
        help="subset of screens to capture (default: all)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    screens = args.screens or list(SCREENS)
    server = start_server(args.host, args.port)
    try:
        written = capture(server, args.host, args.port, args.output_dir, screens)
        print("Captured EMS Dashboard screenshots:")
        for path in written:
            print(f"  {os.path.relpath(path, ROOT)}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
