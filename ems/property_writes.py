# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport-neutral Zendure device property writes.

The controller's state/mode reconciliation must never pick a transport itself:
an HTTP device POSTs to its local ``/properties/write`` endpoint, an MQTT
control device publishes over its own broker channel, and a device with
neither capability fails closed. This module is the single dispatch point —
the controller never touches ``dev.session`` and never falls through from one
transport to another.
"""

import logging

from ems.clients import zendure_write
from ems.logging_utils import log_event
from ems.mqtt_control import dispatch


def write_device_properties(
    dev, properties, *, reason, field=None, error_event=None, log_fields=None
):
    """Write device properties over the device's own transport.

    Returns a structured :class:`~ems.mqtt_control.dispatch.WriteDispatchResult`
    (truthy on success). ``log_fields`` are secret-free context fields for the
    transport's own error logging. Transport exceptions propagate so callers
    keep their existing error handling. A device without any property-write
    capability is rejected — never routed through another transport's write
    path.
    """

    capability = getattr(dev, "write_properties", None)
    if callable(capability):
        return capability(
            properties,
            reason=reason,
            field=field,
            error_event=error_event,
            log_fields=log_fields,
        )

    if getattr(dev, "session", None) is not None:
        ok = zendure_write(
            dev,
            field or ",".join(properties),
            properties,
            error_event or "write_properties_error",
            **(log_fields or {}),
        )
        return dispatch.published(None) if ok else dispatch.failed(
            None, reason="http_write_failed"
        )

    log_event(
        logging.WARNING,
        "property_write_unsupported_transport",
        device=getattr(dev, "name", "?"),
        reason=reason,
        properties=",".join(properties),
    )
    return dispatch.rejected(None, reason="transport_property_writes_unsupported")


__all__ = ["write_device_properties"]
