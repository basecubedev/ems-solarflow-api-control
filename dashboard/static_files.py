# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe static asset lookup for the local dashboard servers."""

import os


STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
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
