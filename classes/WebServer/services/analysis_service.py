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

    transports = getattr(gateway, "_Protocol_Gateway__transports", [])
    result = []
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
    transports = getattr(gateway, "_Protocol_Gateway__transports", [])
    return {t.transport_name: getattr(t, "connected", False) for t in transports}
