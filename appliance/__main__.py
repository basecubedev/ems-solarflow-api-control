# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python3 -m appliance`` runs the host CLI."""

from appliance.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
