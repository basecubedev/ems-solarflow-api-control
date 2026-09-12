#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture the appearance gallery: what a palette, a style and a density do.

The three appearance axes are the one feature in this product whose
documentation cannot be prose. "Pressed into the page rather than laid on it"
is an honest description of the `inset` object style and tells a reader almost
nothing; a picture of it tells them immediately. This script produces one strip
per axis per surface -- three variants side by side, cropped to the same region
of the same page, with only the one axis changed between them.

It drives each surface's own deterministic server, the same ones the browser
suites and the documentation captures use: no hardware, no MQTT, no history
database, no secrets, no real config. Each surface gets a throwaway state root.

    python3 scripts/capture_appearance_gallery.py
    python3 scripts/capture_appearance_gallery.py --surface dashboard

The strips carry no text. Labelling them inside the image would need a font
this script cannot rely on, and the names belong in the document anyway, where
they can be read by a screen reader and translated.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "assets" / "screenshots"

# One variant per column. The axis under test varies; the other two are held at
# their defaults so the strip shows one thing at a time.
AXES = {
    "palettes": [
        {"theme": "signal"},
        {"theme": "void"},
        {"theme": "copper"},
    ],
    "styles": [
        {"style": "glass"},
        {"style": "console"},
        {"style": "brutal"},
    ],
    "density": [
        {"density": "compact"},
        {"density": "normal"},
        {"density": "roomy"},
    ],
}

# The basenames this script writes, spelled out rather than derived. Two
# scripts now write into docs/assets/screenshots/admin/ and two into
# .../appliance/, and the contracts that keep those directories honest compare
# a declared manifest against what is committed -- a manifest they can only read
# if it is literal.
SCREENS = {
    "admin-appearance-palettes.png",
    "admin-appearance-styles.png",
    "admin-appearance-density.png",
    "appliance-appearance-palettes.png",
    "appliance-appearance-styles.png",
    "appliance-appearance-density.png",
    "dashboard-appearance-palettes.png",
    "dashboard-appearance-styles.png",
    "dashboard-appearance-density.png",
}

SURFACES = {
    "admin": {
        "command": ["bash", "tests/e2e/run-admin.sh"],
        "env": {"EMS_ADMIN_E2E_PORT": "{port}"},
        "ready": "/api/admin/auth/status",
        "prefix": "admin/admin-appearance",
        "selects": ("#theme-select", "#style-select", "#density-select"),
    },
    "appliance": {
        "command": ["bash", "tests/e2e-appliance/run-appliance.sh"],
        "env": {"EMS_APPLIANCE_E2E_PORT": "{port}"},
        "ready": "/api/session",
        "prefix": "appliance/appliance-appearance",
        "selects": ("#theme-select", "#style-select", "#density-select"),
    },
    "dashboard": {
        "command": [
            sys.executable,
            "scripts/serve_dashboard_preview.py",
            "--host",
            "127.0.0.1",
            "--port",
            "{port}",
        ],
        "env": {},
        "ready": "/api/auth/status",
        "prefix": "dashboard/dashboard-appearance",
        "selects": ("#themeSelect", "#styleSelect", "#densitySelect"),
    },
}


def require(name):
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"required executable not found: {name}")
    return found


def free_port():
    """A port nothing else holds.

    Deliberately not one of the suite ports: Playwright's `reuseExistingServer`
    adopts whatever answers rather than failing, so a capture server left on a
    suite's port quietly becomes that suite's system under test.
    """

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port, path, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"the server on {port} never answered {path}")


def start(surface, port):
    spec = SURFACES[surface]
    env = dict(os.environ)
    for key, value in spec["env"].items():
        env[key] = value.format(port=port)
    command = [part.format(port=port) for part in spec["command"]]
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    wait_for(port, spec["ready"])
    return process


def capture(surface, port, out_dir):
    """Drive the page with Playwright and write one PNG per variant."""

    script = ROOT / "scripts" / "capture_appearance_gallery.mjs"
    subprocess.run(
        ["node", str(script), surface, str(port), str(out_dir)],
        cwd=ROOT,
        check=True,
    )


def strip(convert, out_dir, prefix, axis, variants):
    """Join the variants into one image, left to right."""

    parts = [str(out_dir / f"{axis}-{index}.png") for index in range(len(variants))]
    target = SHOTS / f"{prefix}-{axis}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 1200px wide rather than the captured ~1700: a document renders these at
    # about 900px anyway, and the smaller file is half the bytes with no visible
    # loss. Quantising the colours instead would have been smaller still and was
    # rejected -- at 256 colours the glows band, and the glow is the thing these
    # pictures exist to show.
    subprocess.run(
        [convert, *parts, "-background", "#05070b", "-splice", "12x0+0+0", "+append",
         "-chop", "12x0+0+0", "-resize", "1200x", "-strip",
         "-define", "png:compression-level=9", str(target)],
        cwd=ROOT,
        check=True,
    )
    return target


def difference(compare, out_dir, axis, count):
    """How much of the picture actually changes between the columns.

    A strip whose columns are identical documents nothing, and it is an easy
    mistake to make: the cockpit's first screen is mostly an SVG flow diagram
    with pill-shaped nodes, and the object style leaves pills alone by design.
    Three columns of that are three copies of one picture. This measures the
    first column against each of the others and returns the largest share of
    pixels that differ, so a subject that cannot show its axis says so instead
    of being committed.
    """

    first = str(out_dir / f"{axis}-0.png")
    worst = 0.0
    for index in range(1, count):
        result = subprocess.run(
            [compare, "-metric", "AE", "-fuzz", "4%", first,
             str(out_dir / f"{axis}-{index}.png"), "null:"],
            cwd=ROOT, capture_output=True, text=True,
        )
        changed = float((result.stderr.strip() or "0").split()[0].replace("(", ""))
        identify = subprocess.run(
            ["identify", "-format", "%w %h", first], cwd=ROOT, capture_output=True, text=True
        )
        width, height = (int(part) for part in identify.stdout.split())
        worst = max(worst, changed / (width * height))
    return worst


MIN_DIFFERENCE = 0.04


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=sorted(SURFACES), action="append")
    args = parser.parse_args(argv)

    convert = require("convert")
    compare = require("compare")
    require("identify")
    require("node")

    scratch = ROOT / ".appearance-gallery"
    scratch.mkdir(exist_ok=True)
    written = []
    weak = []
    try:
        for surface in args.surface or sorted(SURFACES):
            port = free_port()
            print(f"=== {surface} on {port}", flush=True)
            server = start(surface, port)
            try:
                capture(surface, port, scratch)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
            for axis, variants in AXES.items():
                share = difference(compare, scratch, axis, len(variants))
                flag = "" if share >= MIN_DIFFERENCE else "  <-- TOO ALIKE"
                print(f"    {axis:9} {share * 100:5.1f}% of pixels differ{flag}", flush=True)
                if share < MIN_DIFFERENCE:
                    weak.append(f"{surface}/{axis} ({share * 100:.1f}%)")
                written.append(strip(convert, scratch, SURFACES[surface]["prefix"], axis, variants))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    for path in written:
        print(f"  {path.relative_to(ROOT)}")
    print(f"\n{len(written)} strips. Review the diff before committing.")
    if weak:
        raise SystemExit(
            "these strips show too little of their own axis, so the subject is wrong "
            f"rather than the axis: {', '.join(weak)}"
        )


if __name__ == "__main__":
    main()
