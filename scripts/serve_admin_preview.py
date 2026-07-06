# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local preview launcher for the admin device-discovery UI.

Thin wrapper around ``python -m admin`` so the feature can be previewed from the
repo without installing anything. Serves the real admin UI and discovery API,
binds to loopback by default, and never writes ``config.json``.

Usage:
    python3 scripts/serve_admin_preview.py --host 127.0.0.1 --port 8090
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT)

from admin.__main__ import main  # noqa: E402


if __name__ == "__main__":
    main()
