# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-aware Zendure MQTT power control.

This package is the single authority that separates telemetry transport
(``topic_family``) from hardware identity (``hardware_profile``) and the verified
write protocol (``power_write_profile``). It owns the hardware registry and the
protocol command builders; no other module may infer write support from a topic
family or construct a ``function/invoke`` payload directly.
"""
