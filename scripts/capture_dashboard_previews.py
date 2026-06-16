# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture dashboard preview screenshots from the shared preview server.

This is a thin wrapper around ``scripts/serve_dashboard_preview.py`` and the
shared ``scripts/dashboard_preview_data.py`` payloads, so the screenshots match
the interactive preview exactly. It serves the real dashboard static files with
synthetic, non-secret demo data and uses Firefox headless + ImageMagick
``convert`` to write JPGs.

Usage:
    python3 scripts/capture_dashboard_previews.py
    python3 scripts/capture_dashboard_previews.py --serve-only
    python3 scripts/capture_dashboard_previews.py --views diagnose logs devices

For a fully interactive preview (all views, all scenarios) prefer:
    python3 scripts/serve_dashboard_preview.py --scenario firmware-status
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import time

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serve_dashboard_preview import (  # noqa: E402
    CAPTURE_DEFAULT_SCENARIO,
    DEFAULT_CAPTURE_VIEWS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ROOT,
    normalize_views,
    start_server,
)

# Screenshots default to the authenticated scenario so the operator-only
# Diagnose and Logs tabs render their content.
CAPTURE_SCENARIO = CAPTURE_DEFAULT_SCENARIO
DEFAULT_VIEWS = DEFAULT_CAPTURE_VIEWS


def require_executable(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required executable not found: {name}")
    return path


def capture_assets(host, port, output_dir, scenario=CAPTURE_SCENARIO, views=None):
    """Screenshot each requested preview view into <output_dir>/preview-<view>.jpg.

    Returns the list of written file paths.
    """

    views = list(views) if views else list(DEFAULT_VIEWS)
    firefox = require_executable("firefox")
    convert = require_executable("convert")
    os.makedirs(output_dir, exist_ok=True)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    written = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for view in views:
            png_path = os.path.join(tmpdir, f"{view}.png")
            url = f"http://{display_host}:{port}/preview/{view}"
            subprocess.run(
                [
                    firefox,
                    "--headless",
                    "--window-size=1440,1200",
                    "--screenshot",
                    png_path,
                    url,
                ],
                check=True,
                cwd=ROOT,
            )
            jpg_path = os.path.join(output_dir, f"preview-{view}.jpg")
            subprocess.run(
                [convert, png_path, "-quality", "88", jpg_path],
                check=True,
                cwd=ROOT,
            )
            written.append(jpg_path)
    return written


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "docs", "assets"),
    )
    parser.add_argument(
        "--views",
        nargs="+",
        metavar="VIEW",
        help="views to capture: flow view names or 'all' (default: diagnose logs)",
    )
    parser.add_argument("--serve-only", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        views = normalize_views(args.views)
    except ValueError as exc:
        raise SystemExit(str(exc))

    server = start_server(args.host, args.port, CAPTURE_SCENARIO)
    if args.serve_only:
        print(f"Serving preview pages on http://{args.host}:{args.port}/preview")
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
        written = capture_assets(
            args.host, args.port, args.output_dir, CAPTURE_SCENARIO, views
        )
        print("Captured preview screenshots:")
        for path in written:
            print(f"  {os.path.relpath(path, ROOT)}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
