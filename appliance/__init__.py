# SPDX-License-Identifier: AGPL-3.0-or-later
"""EMS SolarFlow Raspberry Pi Appliance Manager.

Host-side management and recovery for an EMS SolarFlow appliance. The package
runs directly on Raspberry Pi OS as two systemd services (an unprivileged web
process and a privileged operation agent) and stays available when Docker, the
EMS Admin container or EMS itself are broken.
"""

from appliance.version import installed_version

__all__ = ["installed_version"]
