# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe static asset lookup for the local dashboard servers, and the dashboard's addresses."""

import os


STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def build_static_asset_index(static_dir):
    """Return request keys mapped to pre-resolved static files."""

    static_root = os.path.abspath(static_dir)
    assets = {}
    for root, dirs, files in os.walk(static_root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for filename in files:
            full_path = os.path.abspath(os.path.join(root, filename))
            relative_path = os.path.relpath(full_path, static_root)
            if relative_path.startswith(".."):
                continue
            key = relative_path.replace(os.sep, "/")
            _, extension = os.path.splitext(filename)
            content_type = STATIC_CONTENT_TYPES.get(
                extension.lower(), DEFAULT_CONTENT_TYPE
            )
            assets[key] = (full_path, content_type)
    return assets


def static_asset_key(request_path):
    """Normalize a request path into a static asset index key."""

    path = "/index.html" if request_path in ("", "/") else request_path
    if "\x00" in path:
        return None

    normalized = os.path.normpath(path.lstrip("/")).replace(os.sep, "/")
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        return None
    return normalized


def lookup_static_asset(assets, request_path):
    """Resolve a request to an indexed asset, serving ``<dir>/index.html`` for a directory.

    The Admin console's Modern front end is a second entry page at
    ``/modern/``; the root page keeps its own mapping in
    :func:`static_asset_key`. The dashboard maps its addresses onto the tree
    first, in :func:`dashboard_asset_path`.
    """

    key = static_asset_key(request_path)
    if key is None:
        return None
    asset = assets.get(key)
    if asset is not None:
        return asset
    return assets.get(f"{key}/index.html")


CLASSIC_PREFIX = "/classic/"
MODERN_DIR = "modern"
LEGACY_MODERN_PATH = "/modern"
SHARED_ROOT_FILES = frozenset({"uPlot.min.css", "uPlot.iife.min.js", "uPlot.LICENSE"})


def dashboard_redirect(request_path):
    """Where an old dashboard address moved to, or ``None``.

    The Modern dashboard was served at ``/modern/`` and now answers at the
    root, so a bookmark of the old address is sent there; ``/classic`` gets
    its slash so the Classic page's relative world stays one directory. The
    servers send it as a temporary redirect that is never cached: a browser
    that remembered it would loop between ``/`` and ``/modern/`` after a
    return to an earlier release, whose ``/`` still sends it to ``/modern/``.
    """

    if request_path == LEGACY_MODERN_PATH or request_path.startswith(LEGACY_MODERN_PATH + "/"):
        return "/" + request_path[len(LEGACY_MODERN_PATH):].lstrip("/")
    if request_path == CLASSIC_PREFIX.rstrip("/"):
        return CLASSIC_PREFIX
    return None


def dashboard_asset_path(request_path):
    """The path inside the static tree that a dashboard address serves, or ``None``.

    The Modern dashboard answers at the root from its build directory; the
    deprecated Classic dashboard answers under ``/classic/`` from the files at
    the static root, where they have always been. The two trees stay apart: a
    Classic address never reaches the Modern build. uPlot is the one exception
    (``SHARED_ROOT_FILES``): it is vendored once, hash-pinned in the licence
    inventory, and both pages load it from the root.
    """

    if request_path.startswith(CLASSIC_PREFIX):
        key = static_asset_key("/" + request_path[len(CLASSIC_PREFIX):])
        if key is None or key.split("/", 1)[0] == MODERN_DIR:
            return None
        return "/" + key
    name = request_path.lstrip("/")
    if name in SHARED_ROOT_FILES:
        return "/" + name
    return f"/{MODERN_DIR}/{name}"
