# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compatibility exports for Admin callers of the central config catalog."""

from ems.config_catalog import (
    LEVELS,
    RISKS,
    SCOPES,
    get_config_feature_field_index,
    get_config_feature_sections,
)

__all__ = [
    "LEVELS",
    "RISKS",
    "SCOPES",
    "get_config_feature_field_index",
    "get_config_feature_sections",
]
