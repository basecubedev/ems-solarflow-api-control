# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local admin helper container for ems-solarflow-api-control.

MVP scope is device discovery only: it scans a user-provided local network
range for EMS-related devices exposing known local HTTP APIs and presents them
in the EMS dashboard style. It never reads, writes, or modifies ``config.json``.
"""
