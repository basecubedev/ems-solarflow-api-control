# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MQTT mapping: device topics in, the project's external-device standard out.

It reads the source topics a mapping names, turns their payloads into the
external catalog's values and republishes them as ``ems-solarflow/<id>/state``,
where the existing discovery and the EMS read them like any bridge's output. It
writes nothing else and never writes to a device. The Admin owns its
configuration; the service only reads it.
"""
