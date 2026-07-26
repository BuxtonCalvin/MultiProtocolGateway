# Description: services/influxdb_service.py — Read-only bridge-health lookups for InfluxDB v1/v3 output bridges.
# File: influxdb_service.py
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
services/influxdb_service.py — Read-only bridge-health lookups for the
InfluxDB v1 (influxdb_out) and v3 (influxdb3_out) output bridges' device
pages.

Unlike services/timescale_service.py, this module is intentionally scoped
to a single panel: influxdb_out / influxdb3_out have no rollups, no
compression policy, and no background scheduler jobs to report on (those
are TimescaleDB continuous-aggregate concepts with no InfluxDB analog), and
neither class currently exposes any way to query row counts or storage
size back out of the server. Only "Bridge Health" — connection/backlog/
staleness state these classes already track on themselves — is built here.
If either class grows read/query support later, a Storage Overview-style
panel could be added the same way the TimescaleDB one was.

Also unlike get_timescale_bridge() (a singleton lookup — there's only ever
one TimescaleDB bridge), lookups here are scoped by transport_name, since
a gateway can have more than one InfluxDB v1 and/or v3 bridge configured
at once. The device page always knows which one it's showing.
"""

from __future__ import annotations

import time
from typing import Any


def get_influxdb_bridge(gateway: Any, device_section: str) -> Any | None:
    """
    Finds the live influxdb_out / influxdb3_out bridge transport whose
    transport_name matches device_section (e.g. "transport.influxdb_out"),
    if any.

    Returns None if there's no gateway yet (startup race), or no such
    transport is configured under that name. Duck-typed on the class name
    (rather than isinstance) so this module never needs a hard import of
    either transport class, mirroring timescale_service.get_timescale_
    bridge()'s access pattern.
    """
    if gateway is None:
        return None
    transports: list[Any] = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if type(t).__name__ in ("influxdb_out", "influxdb3_out") and getattr(t, "transport_name", None) == device_section:
            return t
    return None


def is_influxdb_bridge(gateway: Any, device_section: str) -> bool:
    """True when an InfluxDB v1 or v3 bridge with this name is attached to the gateway."""
    return get_influxdb_bridge(gateway, device_section) is not None


def _format_elapsed(seconds: float) -> str:
    """Human-readable elapsed time, e.g. 125.0 -> '2m ago'. Negative/near-zero -> 'just now'."""
    if seconds < 5:
        return "just now"
    seconds_int: int = int(seconds)
    if seconds_int < 60:
        return f"{seconds_int}s ago"
    minutes: int = seconds_int // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours: int = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days: int = hours // 24
    return f"{days}d ago"


def get_influxdb_health(gateway: Any, device_section: str) -> dict[str, Any]:
    """
    Read-only connection/backlog/staleness snapshot for the "Bridge
    Health" panel, with a human-readable `last_periodic_reconnect_display`
    added. See influxdb_out.get_health_snapshot (identical on influxdb3_
    out) for the rest of the field list.

    Raises RuntimeError if no InfluxDB bridge with this name is attached
    to the gateway.
    """
    bridge: Any | None = get_influxdb_bridge(gateway, device_section)
    if bridge is None:
        msg: str = (f"No InfluxDB bridge named '{device_section}' is attached to this gateway.")
        raise RuntimeError(msg)

    health: dict[str, Any] = bridge.get_health_snapshot()

    last_attempt: float = health.get("last_periodic_reconnect_attempt") or 0.0
    if last_attempt > 0:
        health["last_periodic_reconnect_display"] = _format_elapsed(time.time() - last_attempt)
    else:
        health["last_periodic_reconnect_display"] = "never"

    return health


def _format_bytes(n: int | None) -> str:
    """Human-readable byte size, e.g. 1536 -> '1.5 KB'. None/0 -> '0 B'."""
    if not n:
        return "0 B"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # unreachable, keeps type checkers happy


def get_influxdb_storage(gateway: Any, device_section: str) -> dict[str, Any]:
    """
    Best-effort, read-only storage snapshot for the "Storage Overview"
    panel, with a human-readable `data_dir_size_display` added when a
    data_dir size was found. See influxdb_out.get_storage_overview (or
    influxdb3_out's version — the field names line up so one template can
    render both) for the rest of the field list.

    Raises RuntimeError if no InfluxDB bridge with this name is attached
    to the gateway.
    """
    bridge: Any | None = get_influxdb_bridge(gateway, device_section)
    if bridge is None:
        msg: str = (f"No InfluxDB bridge named '{device_section}' is attached to this gateway.")
        raise RuntimeError(msg)

    storage: dict[str, Any] = bridge.get_storage_overview()
    storage["data_dir_size_display"] = (
        _format_bytes(storage["data_dir_size_bytes"]) if storage.get("data_dir_size_bytes") is not None else None
    )
    return storage
