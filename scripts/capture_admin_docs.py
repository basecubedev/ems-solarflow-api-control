# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture Admin Console documentation screenshots.

Thin wrapper around ``scripts/serve_admin_docs_preview.py`` that drives Firefox
headless + ImageMagick ``convert`` to write the docs screenshots under
``docs/assets/screenshots/admin/`` from deterministic, non-secret demo data.

Usage::

    python3 scripts/capture_admin_docs.py
    python3 scripts/capture_admin_docs.py --serve-only
    python3 scripts/capture_admin_docs.py --screens landing maintenance-overview

For an interactive preview instead of a capture::

    python3 scripts/serve_admin_docs_preview.py
    # then open http://127.0.0.1:8092/?screen=maintenance-overview
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

from serve_admin_docs_preview import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    start_server,
)

# screen id -> (output PNG basename, minimum trimmed height in px). Headless
# Firefox occasionally captures a screen before its async demo data has rendered
# (a short landing/header frame); the height floor detects that so the capture
# is retried rather than a half-rendered frame being kept. Screen ids match the
# ?screen= values the drive script (admin_docs_preview.js) understands.
SCREENS = {
    "landing": ("admin-landing.png", 420),
    "guided-setup-start": ("admin-guided-setup-start.png", 680),
    "config-preview": ("admin-guided-setup-config-preview.png", 1000),
    "discovery": ("admin-discovery-preview.png", 850),
    "maintenance-overview": ("admin-maintenance-overview.png", 950),
    "backup-restore": ("admin-backup-restore.png", 800),
    "guided-upgrade": ("admin-guided-upgrade-plan.png", 1200),
    "admin-update-reconnect": ("admin-admin-update-reconnect.png", 850),
}
MAX_ATTEMPTS = 5

# Page background (admin.css --bg); the trim margin/border is filled with it so
# every screenshot keeps a small, consistent gutter around the 960px shell.
PAGE_BG = "#070a0f"
WINDOW_SIZE = "1440,1600"


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


def _shoot(firefox, url, profile, png_raw):
    subprocess.run(
        [
            firefox,
            "--headless",
            "--new-instance",
            "-no-remote",
            "-profile",
            profile,
            f"--window-size={WINDOW_SIZE}",
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
            "6%",
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


def capture(host, port, output_dir, screens):
    firefox = require_executable("firefox")
    convert = require_executable("convert")
    os.makedirs(output_dir, exist_ok=True)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    written = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for screen in screens:
            basename, min_height = SCREENS[screen]
            url = f"http://{display_host}:{port}/?screen={screen}"
            out_path = os.path.join(output_dir, basename)
            best_path, best_height = None, -1
            for attempt in range(1, MAX_ATTEMPTS + 1):
                png_raw = os.path.join(tmpdir, f"{screen}-{attempt}.png")
                trimmed = os.path.join(tmpdir, f"{screen}-{attempt}-trim.png")
                profile = os.path.join(tmpdir, f"profile-{screen}-{attempt}")
                os.makedirs(profile, exist_ok=True)
                _shoot(firefox, url, profile, png_raw)
                _trim(convert, png_raw, trimmed)
                # The raw frame is a fixed-height window; trimmed content height
                # reveals whether the screen actually rendered its demo data.
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
        default=os.path.join(ROOT, "docs", "assets", "screenshots", "admin"),
    )
    parser.add_argument(
        "--screens",
        nargs="+",
        metavar="SCREEN",
        choices=sorted(SCREENS),
        help="subset of screens to capture (default: all)",
    )
    parser.add_argument("--serve-only", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    screens = args.screens or list(SCREENS)
    server = start_server(args.host, args.port)
    if args.serve_only:
        print(f"Serving admin docs preview on http://{args.host}:{args.port}/?screen=landing")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
        finally:
            server.shutdown()
            server.server_close()
        return
    try:
        written = capture(args.host, args.port, args.output_dir, screens)
        print("Captured Admin Console screenshots:")
        for path in written:
            print(f"  {os.path.relpath(path, ROOT)}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
