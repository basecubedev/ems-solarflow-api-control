# SPDX-License-Identifier: AGPL-3.0-or-later
"""History & analytics backends for the EMS dashboard.

This package abstracts time-series history behind a ``HistoryProvider`` so the
dashboard does not depend on where history is stored. The default provider is
backed by the existing local SQLite store; an optional provider is backed by
InfluxDB 2.x for the analytics experience.

Modules here are import-side-effect-free so the dashboard and ``emsctl`` can
import them directly.
"""

from ems.history.provider import (
    HistoryProvider,
    HistoryResult,
    SqliteHistoryProvider,
    create_history_provider,
)

__all__ = [
    "HistoryProvider",
    "HistoryResult",
    "SqliteHistoryProvider",
    "create_history_provider",
]
