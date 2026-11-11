# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pair a HomeWizard P1 Meter for the Admin Console, one attempt per request.

The browser asks again every few seconds while the operator presses the
meter's button. Nothing is stored here: the token goes back into the editor's
draft and reaches config.json only through a checked and saved preview, where
it is a secret like any other.
"""

import ipaddress
import json

from admin.discovery import CidrValidationError, validate_cidr
from admin.install_context import detect_install_context
from ems.config import HOMEWIZARD_P1_DEFAULT_PORT, HOMEWIZARD_P1_PAIRING_NAME_PATTERN
from ems.homewizard import (
    OWN_PAIRING_NAME_PATTERN,
    REASON_BUTTON_NOT_PRESSED,
    HomeWizardError,
    HomeWizardP1Api,
    pairing_name,
)

PAIRING_TIMEOUT_S = 5.0


class PairingRequestError(ValueError):
    """The request names no address or port the console may pair with."""


def stored_pairing_name(base_dir=None):
    """The pairing name the installation's config.json holds, or ``None``."""

    context = detect_install_context(base_dir=base_dir)
    if not context.config_exists:
        return None
    try:
        config = json.loads(context.config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    grid_meter = config.get("grid_meter") if isinstance(config, dict) else None
    name = grid_meter.get("pairing_name") if isinstance(grid_meter, dict) else None
    name = name.strip() if isinstance(name, str) else ""
    return name if HOMEWIZARD_P1_PAIRING_NAME_PATTERN.fullmatch(name) else None


def pairing_target(body, stored_name=None):
    """Address, port and name of a pairing request; the address on the local network only.

    The name is the one config.json holds, so pairing again replaces only this
    installation's own pairing and never another one's. Before the config has
    one, a name the console made up earlier in the same draft is reused if it
    is one of ours; anything else gets a new name, so the browser can never
    pair under another application's name.
    """

    body = body if isinstance(body, dict) else {}
    text = str(body.get("ip") or "").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise PairingRequestError("Enter the P1 Meter's IP address.") from exc
    try:
        validate_cidr(f"{address}/{address.max_prefixlen}")
    except CidrValidationError as exc:
        raise PairingRequestError("The P1 Meter's address must be on the local network.") from exc
    port = body.get("port")
    if port in (None, ""):
        port = HOMEWIZARD_P1_DEFAULT_PORT
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PairingRequestError("The port must be a whole number from 1 to 65535.")
    if stored_name:
        return str(address), port, stored_name
    drafted = str(body.get("pairing_name") or "").strip()
    return str(address), port, pairing_name(drafted if OWN_PAIRING_NAME_PATTERN.fullmatch(drafted) else None)


def pair_homewizard_p1(ip, port, name, *, api_factory=HomeWizardP1Api):
    """One pairing attempt: ``waiting`` until the button is pressed, then ``paired``."""

    api = api_factory(ip, port=port, timeout=PAIRING_TIMEOUT_S)
    try:
        token = api.pair(name)
    except HomeWizardError as exc:
        if exc.reason == REASON_BUTTON_NOT_PRESSED:
            return {"state": "waiting"}
        return {"state": "failed", "reason": exc.reason, "message": str(exc)}
    finally:
        api.close()
    return {"state": "paired", "serial": api.connected_serial, "token": token, "pairing_name": name}
