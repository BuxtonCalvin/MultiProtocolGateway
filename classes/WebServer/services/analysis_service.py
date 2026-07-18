# Description: services/analysis_service.py — Runtime helpers for analysis-aware scraper pages.
# File: analysis_service.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
services/analysis_service.py — Runtime helpers for analysis-aware scraper pages.

The gateway instance is accessed via app.state.gateway, passed in at startup.
This module now provides live transport summaries and connection status only.
"""
from __future__ import annotations

import logging
from typing import Any

_log: logging.Logger = logging.getLogger(__name__)


def get_scraper_transports(gateway: Any) -> list[dict[str, str]]:
    """
    Returns a list of scraper transport summaries from the live gateway instance.
    Used by the UI to populate the Analyze dropdown.
    """
    if gateway is None:
        return []

    transports: Any | list[Any] = getattr(gateway, "_Protocol_Gateway__transports", [])
    result: list[dict[str, Any]] = []
    for t in transports:
        if getattr(t, "protocolSettings", None) is not None:
            result.append({
                "transport_name": t.transport_name,
                "transport_type": t.__class__.__name__,
                "connected": getattr(t, "connected", False),
                "protocol": getattr(t.protocolSettings, "protocol", ""),
            })
    return result


def get_transport_connection_status(gateway: Any) -> dict[str, bool]:
    """
    Returns {transport_name: is_connected} for all transports.
    Called by the bridge pane to show live connection status.
    """
    if gateway is None:
        return {}
    transports: Any | list[Any] = getattr(gateway, "_Protocol_Gateway__transports", [])
    return {t.transport_name: getattr(t, "connected", False) for t in transports}
