# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one app can be put on a phone's or tablet's home screen.

What these tests hold:

- the console's built page names its web app manifest, its iOS icon and its
  theme colour, and the manifest names the app, starts at the root and stands
  alone;
- every icon the manifest or the page names is shipped, a PNG of the size it
  says, and the build ships exactly the icons drawn beside the mark;
- the console serves the manifest and the icons with their own content types,
  signed out, as the page loads them before any sign-in.
"""

import json
import re
import struct
import threading
import urllib.request
from pathlib import Path

import pytest

from admin.server import create_server

pytestmark = [pytest.mark.contract, pytest.mark.admin]

ROOT = Path(__file__).resolve().parents[1]
BUILT = ROOT / "admin" / "static" / "modern"
BRAND = ROOT / "frontend" / "shared" / "brand"


def _png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", data[16:24])


def test_the_page_names_the_manifest_its_ios_icon_and_its_colour():
    page = (BUILT / "index.html").read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page
    manifest = json.loads((BUILT / "manifest.webmanifest").read_text(encoding="utf-8"))
    colour = re.search(r'<meta name="theme-color" content="([^"]+)">', page).group(1)
    assert manifest["theme_color"] == manifest["background_color"] == colour
    assert manifest["name"] == "EMS SolarFlow" and manifest["short_name"]
    assert (manifest["start_url"], manifest["scope"], manifest["display"]) == ("/", "/", "standalone")
    assert {icon.get("purpose", "any") for icon in manifest["icons"]} == {"any", "maskable"}


def test_every_icon_named_is_shipped_at_the_size_it_says():
    manifest = json.loads((BUILT / "manifest.webmanifest").read_text(encoding="utf-8"))
    named = {icon["src"].lstrip("/"): icon["sizes"] for icon in manifest["icons"]}
    named["apple-touch-icon.png"] = "180x180"
    for name, sizes in named.items():
        width, height = _png_size(BUILT / name)
        assert f"{width}x{height}" == sizes, name
        assert (BUILT / name).read_bytes() == (BRAND / name).read_bytes(), name
    assert sorted(path.name for path in BRAND.glob("*.png")) == sorted(named)


def test_the_console_serves_the_manifest_and_the_icons_signed_out(isolated_install_root):
    server = create_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path, kind in (("/manifest.webmanifest", "application/manifest+json"), ("/app-icon-192.png", "image/png"), ("/apple-touch-icon.png", "image/png")):
            with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith(kind), path
    finally:
        server.shutdown()
        server.server_close()


def _pixels(path):
    """The RGBA rows of an 8-bit RGBA PNG without interlacing, as Inkscape writes the icons."""

    import zlib

    data = path.read_bytes()
    position, chunks, header = 8, [], None
    while position < len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            chunks.append(body)
        position += length + 12
    width, height, depth, colour, _, _, interlace = header
    assert (depth, colour, interlace) == (8, 6, 0), path
    raw, stride, rows, previous = zlib.decompress(b"".join(chunks)), width * 4, [], bytearray(width * 4)
    for row in range(height):
        kind, line = raw[row * (stride + 1)], bytearray(raw[row * (stride + 1) + 1:(row + 1) * (stride + 1)])
        for index in range(stride):
            left = line[index - 4] if index >= 4 else 0
            up = previous[index]
            corner = previous[index - 4] if index >= 4 else 0
            if kind == 1:
                line[index] = (line[index] + left) & 0xFF
            elif kind == 2:
                line[index] = (line[index] + up) & 0xFF
            elif kind == 3:
                line[index] = (line[index] + (left + up) // 2) & 0xFF
            elif kind == 4:
                guess = left + up - corner
                near = min((abs(guess - left), left), (abs(guess - up), up), (abs(guess - corner), corner), key=lambda pair: pair[0])
                line[index] = (line[index] + near[1]) & 0xFF
        rows.append(line)
        previous = line
    return rows


def _token(name):
    css = (ROOT / "frontend" / "shared" / "styles" / "tokens.css").read_text(encoding="utf-8")
    root = css[css.index(":root {"):]
    value = re.search(rf"{re.escape(name)}:\s*#([0-9a-fA-F]{{6}});", root[:root.index("}")]).group(1)
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def test_the_icons_still_wear_the_mark_s_colours():
    """The PNGs are drawn once from the mark; a change of its tile or its ring's colours shows here until scripts/build_app_icons.mjs runs again."""

    tile, output = _token("--m-surface-2"), _token("--output")
    for name in ("app-icon-512.png", "app-icon-maskable-512.png"):
        rows = _pixels(BRAND / name)
        assert tuple(rows[256][60 * 4:60 * 4 + 3]) == tile, name
    ring = _pixels(BRAND / "app-icon-512.png")
    assert tuple(ring[145][367 * 4:367 * 4 + 3]) == output
