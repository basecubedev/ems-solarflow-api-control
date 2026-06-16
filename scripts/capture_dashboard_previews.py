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

from dashboard_preview_data import FLOW_VIEWS  # noqa: E402
from serve_dashboard_preview import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    ROOT,
    start_server,
)

# Screenshots default to the authenticated scenario so the operator-only
# Diagnose and Logs tabs render their content.
CAPTURE_SCENARIO = "write-mode"
DEFAULT_VIEWS = ("diagnose", "logs")


def require_executable(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required executable not found: {name}")
    return path


def capture_assets(host, port, output_dir, scenario=CAPTURE_SCENARIO, views=None):
    """Screenshot each requested preview view into <output_dir>/preview-<view>.jpg."""

    views = list(views) if views else list(DEFAULT_VIEWS)
    firefox = require_executable("firefox")
    convert = require_executable("convert")
    os.makedirs(output_dir, exist_ok=True)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

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
            subprocess.run(
                [
                    convert,
                    png_path,
                    "-quality",
                    "88",
                    os.path.join(output_dir, f"preview-{view}.jpg"),
                ],
                check=True,
                cwd=ROOT,
            )


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
        choices=FLOW_VIEWS,
        help="views to capture (default: diagnose logs)",
    )
    parser.add_argument("--serve-only", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    server = start_server(args.host, args.port, CAPTURE_SCENARIO)
    if args.serve_only:
        print(f"Serving preview pages on http://{args.host}:{args.port}")
        print(f"  http://{args.host}:{args.port}/preview/diagnose")
        print(f"  http://{args.host}:{args.port}/preview/logs")
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
        capture_assets(args.host, args.port, args.output_dir, CAPTURE_SCENARIO, args.views)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
