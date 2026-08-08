# Description: services/bridge_service.py — Consolidated runtime helpers for every bridge-type transport's admin screens and device-page info panels (TimescaleDB, InfluxDB v1/v3, MQTT).
# File: bridge_service.py
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
services/bridge_service.py — Consolidated runtime helpers for every bridge-
type transport's admin screens and device-page info panels.

Merged from three previously separate modules (timescale_service.py,
influxdb_service.py, mqtt_service.py) into one file, organized into three
clearly-delineated sections below (search for the "====" banners): each
section keeps that transport's original module-level docstring inline, so
the reasoning behind that transport's specific design choices (singleton
vs. name-scoped bridge lookup, what is/isn't queryable, etc.) stays right
next to its code rather than living only in a merge commit message.

    TIMESCALEDB   — Delete Columns / Rebuild Rollup Views admin screens,
                    plus Bridge Health / Storage Overview / Compression &
                    Retention / Background Jobs device-page info panels.
                    The only bridge type with a staging + commit flow (see
                    that section's docstring) and the only one treated as
                    a gateway-wide singleton (get_timescale_bridge) rather
                    than looked up by name — there's only ever one
                    TimescaleDB bridge.

    INFLUXDB      — Bridge Health / Storage Overview device-page info
                    panels for InfluxDB v1 (influxdb_out) and v3
                    (influxdb3_out). Name-scoped (get_influxdb_bridge),
                    since a gateway can have more than one v1/v3 bridge
                    configured at once.

    MQTT          — Bridge Health device-page info panel only, no Storage
                    Overview (MQTT brokers generally don't persist
                    historical data). Also name-scoped, for the same
                    reason as InfluxDB.

    PROMETHEUS    — Bridge Health (in-memory registry summary) / Target
                    Health (per-machine scrape_failures_total / last-scrape
                    table) device-page info panels for the pull-model
                    prometheus_out bridge. Name-scoped, same reasoning as
                    InfluxDB/MQTT — a gateway could run more than one
                    Prometheus bridge on different ports/paths.

Renamed on merge: the TimescaleDB section's health-panel function was
previously named get_bridge_health() (a reasonable name when it was the
only bridge type with one); it's now get_timescale_health() so it reads
consistently alongside get_influxdb_health() / get_mqtt_health() in the
same module. All three bridge-lookup/health/storage function names
otherwise carry their transport's name explicitly (get_timescale_bridge,
get_influxdb_bridge, get_mqtt_bridge, ...) specifically so it's never
ambiguous which transport a given call in this file is about.

A single shared _format_bytes() helper (previously defined identically in
both the TimescaleDB and InfluxDB sections) now lives once, in the shared
helpers section below, and is used by both. _format_dt() (TimescaleDB
rollup/job timestamps) and _format_elapsed() (InfluxDB reconnect elapsed
time) are each used by only one section and stay defined there rather than
being hoisted up — hoisting a single-use helper out of its only caller's
section would just make that section harder to read in isolation for no
sharing benefit.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

_log: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared formatting helpers — used across more than one bridge type's info
# panels. See module docstring above for why _format_dt / _format_elapsed
# are NOT here (each has exactly one caller, in one section).
# ---------------------------------------------------------------------------

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


# ============================================================================
# TIMESCALEDB
# ============================================================================

# The timescaledb bridge module is an optional/pluggable transport — a
# deployment without it configured shouldn't crash the webserver on import.
# Timescale_Available gates every function below and drives whether the
# "Timescale DB" nav pad is shown at all (see is_timescale_available()).
#
# The TYPE_CHECKING import below is for static type checkers only and is
# never executed. It gives every annotation in this file one single,
# unconditional class binding to check against. The runtime try/except
# further down binds the one class this module actually needs to
# instantiate (WideTableFieldManager) under a *different* name,
# _WideTableFieldManagerImpl — deliberately not reusing the same name,
# because `except ImportError: WideTableFieldManager = None` would make
# the type checker infer `type[WideTableFieldManager] | None` for that
# name (a variable, not a type), which is exactly what produces
# "Variable not allowed in type expression" on every annotation that
# references it. WideTableField / WideTableFieldDeletionResult are only
# ever used in annotations in this file (never instantiated here), so they
# don't need a runtime binding at all — under `from __future__ import
# annotations` those annotations are never evaluated at runtime.
if TYPE_CHECKING:
    from ...transports.timescaledb import (
        WideTableField,
        WideTableFieldDeletionResult,
        WideTableFieldManager,
    )

# Declared Any up front, before either branch assigns to it: this is what
# makes _field_manager() calling it not get flagged as "Object of type
# 'None' cannot be called". Without this, the type checker infers
# `type[WideTableFieldManager] | None` from the two conditional
# assignments below and won't treat it as callable — even though the
# `bridge is None` check in _field_manager() already guarantees this is
# never actually None by the time it's called.
_WideTableFieldManagerImpl: Any

try:
    from ...transports.timescaledb import (
        WideTableFieldManager as _WideTableFieldManagerImpl,
    )
    Timescale_Available = True
except ImportError:
    _log.debug("timescale_service: transports.timescaledb is not importable — the TimescaleDB admin UI will stay hidden.")
    _WideTableFieldManagerImpl = None
    Timescale_Available = False

# ---------------------------------------------------------------------------
# Live bridge discovery
# ---------------------------------------------------------------------------

def get_timescale_bridge(gateway: Any) -> Any | None:
    """
    Finds the live timescaledb bridge transport on the gateway, if any.

    Returns None if there's no gateway yet (startup race), no such
    transport is configured, or the timescaledb module isn't importable in
    this deployment. Duck-typed on the class name (rather than isinstance)
    so this module never needs a hard import of the transport class itself.

    Mirrors analysis_service.get_scraper_transports()'s access pattern.
    """
    if gateway is None or not Timescale_Available:
        return None
    transports = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if type(t).__name__ == "timescaledb":
            return t
    return None


def is_timescale_available(gateway: Any) -> bool:
    """
    True when a live TimescaleDB bridge is attached to this gateway.
    Drives whether the "Timescale DB" nav pad is shown — see the
    `timescale_bridge_available()` Jinja global registered in main.py.
    """
    return get_timescale_bridge(gateway) is not None


def _get_live_transport(gateway: Any, protocol_name: str) -> Any | None:
    """
    Finds the live transport instance whose protocol_name matches, so its
    current (post variable_mask/variable_screen) registry_map can be
    compared against the columns already committed to the wide table.

    Duck-typed on the `protocol_name` property (see
    transport_base.protocol_name) rather than isinstance, mirroring
    get_timescale_bridge()'s walk of __transports. Returns None if the
    protocol has no live transport right now — a wide table can outlive
    its transport (e.g. config was edited to remove it, or it just hasn't
    connected yet), and that's a legitimate state, not an error.
    """
    if gateway is None:
        return None
    transports = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if getattr(t, "protocol_name", None) == protocol_name:
            return t
    return None


def _active_metric_names_for_protocol(gateway: Any, protocol_name: str) -> set[str] | None:
    """
    Returns the metric/variable names the live transport for
    protocol_name is currently configured to produce, via its
    variable_mask/variable_screen-filtered registry_map, plus any
    transport-declared synthetic fields (see
    transport_base.synthetic_fields_metadata). This is the same source
    timescaledb._extract_metric_names reads at schema-registration time.

    This is the "expected" side of the comparison
    timescaledb._validate_wide_row makes against a live scrape row
    (row_keys vs wide_columns) — used by list_wide_table_fields() to flag
    wide-table columns the current mask/screen config no longer produces,
    so the Delete Columns UI can highlight them for the admin instead of
    only surfacing the mismatch as a warning log line the next time data
    is scraped.

    Returns None — "unknown, don't flag anything" — if the protocol has
    no live transport attached right now, or that transport hasn't loaded
    a registry map yet. Callers must treat None as "no opinion", not as
    "everything is stale", since flagging every column red just because a
    transport hasn't connected yet would be misleading.
    """
    transport: Any | None = _get_live_transport(gateway, protocol_name)
    if transport is None:
        return None

    registry_map: dict[Any, list[Any]] = getattr(transport, "registry_map", None) or {}
    if not registry_map:
        return None

    names: set[str] = set()
    for entries in registry_map.values():
        for entry in entries:
            variable_name: str | None = getattr(entry, "variable_name", None)
            if variable_name:
                names.add(variable_name)

    for synthetic in getattr(transport, "synthetic_fields_metadata", []):
        # synthetic is a (variable_name, data_type, unit_mod, note) tuple —
        # see transport_base.synthetic_fields_metadata.
        if synthetic:
            names.add(synthetic[0])

    return names


def _field_manager(gateway: Any) -> "WideTableFieldManager":
    bridge = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    return _WideTableFieldManagerImpl(bridge)


# ---------------------------------------------------------------------------
# Read-only listings for the UI
# ---------------------------------------------------------------------------

def list_wide_tables(gateway: Any) -> list[dict[str, str]]:
    """
    Returns [{protocol_name, wide_table_name}, ...] for the wide-table
    picker screen (step 4 of the Delete Columns flow).
    """
    mgr: WideTableFieldManager = _field_manager(gateway)
    return [
        {"protocol_name": protocol_name, "wide_table_name": wide_table_name}
        for protocol_name, wide_table_name in mgr.list_editable_protocols()
    ]


def resolve_wide_table_name(gateway: Any, protocol_name: str) -> str:
    """Returns the wide_table_name for protocol_name. Raises ValueError if unknown/narrow-only."""
    mgr: WideTableFieldManager = _field_manager(gateway)
    return mgr.resolve_wide_table_name(protocol_name)


def list_wide_table_fields(
    gateway: Any,
    protocol_name: str,
    staged_columns: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Returns the alpha-ordered field list (step 5 of the Delete Columns
    flow) for one protocol's wide table, each row annotated with:
      - `checked` reflecting the currently staged-for-deletion set — so the
        checklist re-renders with the right boxes ticked if the admin
        navigates away and comes back before committing.
      - `stale` flagging columns the protocol's live variable_mask/
        variable_screen config no longer produces (see
        _active_metric_names_for_protocol) — the same condition
        timescaledb._validate_wide_row logs as `fewer_keys` when it turns
        up in a scraped row, surfaced here proactively so the UI can
        render likely-deletable columns in a distinct color. Never True
        when the protocol has no live transport attached right now — see
        _active_metric_names_for_protocol for why that's "no opinion"
        rather than "everything is stale".
    """
    mgr: WideTableFieldManager = _field_manager(gateway)
    staged: set[str] = staged_columns or set()
    active_metric_names: set[str] | None = _active_metric_names_for_protocol(gateway, protocol_name)
    fields: list[WideTableField] = mgr.list_fields(protocol_name, active_metric_names=active_metric_names)
    return [
        {
            "metric_name": f.metric_name,
            "column_name": f.column_name,
            "data_type": f.data_type,
            "checked": f.column_name in staged,
            "stale": f.stale,
        }
        for f in fields
    ]


# ---------------------------------------------------------------------------
# Rollup views — read-only inventory + on-demand full rebuild.
#
# Backs the "Timescale DB -> Rebuild Rollup Views" admin screen. Unlike the
# column deletions above, there's no staging step here: rollup views are
# always derived/recreatable from the wide/narrow tables (never a source of
# truth themselves), so a rebuild runs immediately on click rather than
# riding the app's "Commit All Changes" flow.
# ---------------------------------------------------------------------------

def list_rollup_views(gateway: Any) -> list[dict[str, Any]]:
    """
    Returns the rollup-view inventory (shared narrow stack + every wide
    protocol's hourly/daily/weekly/monthly stack) for the Rebuild Rollup
    Views screen. See RollupManager.list_rollup_views for the per-row shape.

    Raises RuntimeError if no bridge is attached, or the bridge is attached
    but hasn't finished connecting to TimescaleDB yet (rollup_mgr is None).
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    return bridge.rollup_mgr.list_rollup_views()


def list_rollup_view_groups(gateway: Any) -> list[dict[str, Any]]:
    """
    Same inventory as list_rollup_views(), grouped into one entry per
    source table -- the shared narrow stack, plus one entry per wide-table
    protocol -- for the Rebuild Rollup Views screen's group checkboxes.

    Rebuilding is only ever offered at this per-source-table granularity,
    never per individual hourly/daily/weekly/monthly view, because the
    finer-grained views in a stack are hierarchically dependent on the
    coarser ones within that same stack -- see
    RollupManager.rebuild_all_rollups for why.

    Uses itertools.groupby rather than Jinja's `groupby` filter: the latter
    re-sorts by the grouping key first, which would separate "shared_narrow"
    from wherever it falls alphabetically among protocol names.
    itertools.groupby only merges already-consecutive equal keys, which
    preserves list_rollup_views()'s ordering (shared narrow stack first,
    then wide protocols alphabetically) instead.
    """
    flat: list[dict[str, Any]] = list_rollup_views(gateway)
    groups: list[dict[str, Any]] = []
    for protocol_name, rows_iter in itertools.groupby(flat, key=lambda r: r["protocol_name"]):
        rows: list[dict[str, Any]] = list(rows_iter)
        groups.append({
            "protocol_name": protocol_name,
            "wide_table_name": rows[0]["wide_table_name"],
            "views": rows,
        })
    return groups


def rebuild_all_rollups(
    gateway: Any, protocol_names: set[str] | None = None, force: bool = False
) -> dict[str, Any]:
    """
    Triggers an immediate rebuild pass across the selected rollup stack(s)
    on the live bridge. Called from the "Rebuild Rollups" / "Force Rebuild"
    buttons on the Rebuild Rollup Views screen (PATCH /api/timescale/
    rollups/rebuild).

    Args:
        protocol_names: Which groups to act on -- protocol_name values as
            returned by list_rollup_view_groups(), plus "shared_narrow" for
            the shared narrow stack. None acts on every group.
        force: False ("Rebuild Rollups") only purges + re-materializes a
            group if it's actually out of date. True ("Force Rebuild")
            always purges + re-materializes every selected group.
        See RollupManager.rebuild_all_rollups for the per-group result
        shape (including each group's `changed` flag) and why selection
        stops at the per-source-table granularity.

    Raises RuntimeError if no bridge is attached, or the bridge hasn't
    finished connecting to TimescaleDB yet.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    return bridge.rollup_mgr.rebuild_all_rollups(protocol_names=protocol_names, force=force)


def refresh_selected_rollups(
    gateway: Any, protocol_names: set[str] | None = None, force_full: bool = False
) -> dict[str, Any]:
    """
    Pulls the latest raw data into the selected rollup groups' existing
    views, without dropping or recreating anything. Called from the
    "Refresh Now" button on the Rebuild Rollup Views screen (PATCH
    /api/timescale/rollups/refresh) -- the lighter, non-structural
    counterpart to rebuild_all_rollups().

    Args:
        protocol_names: Which groups to refresh -- protocol_name values as
            returned by list_rollup_view_groups(), plus "shared_narrow" for
            the shared narrow stack. None refreshes every group.
        force_full: False performs each view's normal incremental refresh.
            True refreshes each view's entire time range from scratch.
        See RollupManager.refresh_selected_rollups for the per-view result
        shape.

    Raises RuntimeError if no bridge is attached, or the bridge hasn't
    finished connecting to TimescaleDB yet.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    return bridge.rollup_mgr.refresh_selected_rollups(protocol_names=protocol_names, force_full=force_full)


# ---------------------------------------------------------------------------
# Rebuild Compression — backs the "Timescale DB -> Rebuild Compression"
# admin screen, a sibling of Rebuild Rollup Views above using the exact
# same group keys (a wide-table protocol_name, plus "shared_narrow"). Unlike
# the rollup functions, this doesn't touch view definitions at all -- it
# decompresses and recompresses already-compressed chunks in place, so a
# compress_segmentby/compress_orderby change (or a Delete Columns edit)
# actually takes effect on historical data instead of only new chunks.
# ---------------------------------------------------------------------------

def list_compression_groups(gateway: Any) -> list[dict[str, Any]]:
    """
    Read-only compression inventory (one row per group, each carrying a
    `tables` breakdown of that group's raw table + all four rollup views'
    chunk counts, size, and raw/uncompressed size) for the Rebuild
    Compression screen's group checkboxes, with human-readable
    `size_display` / `raw_size_display` added to each group row AND each
    of its per-table rows.

    Raises RuntimeError if no bridge is attached, or the bridge is
    attached but hasn't finished connecting to TimescaleDB yet.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    rows: list[dict[str, Any]] = bridge.rollup_mgr.list_compression_groups()
    for row in rows:
        row["size_display"] = _format_bytes(row.get("size_bytes"))
        row["raw_size_display"] = _format_bytes(row.get("raw_size_bytes"))
        for table_row in row.get("tables", []):
            table_row["size_display"] = _format_bytes(table_row.get("size_bytes"))
            table_row["raw_size_display"] = _format_bytes(table_row.get("raw_size_bytes"))
    return rows


def _annotate_compression_change(entry: dict[str, Any]) -> None:
    """
    Adds human-readable display fields to one before/after-size entry --
    either a group result or one of its `tables` rows from
    RollupManager.rebuild_compression -- in place: `size_before_display`,
    `size_after_display`, and `percent_reduced` (float, None when there's
    no size_before to compute a ratio against, e.g. a skipped group or an
    untouched table). Mirrors the size_display convention used elsewhere
    in this module (e.g. list_compression_groups) -- this is where display
    formatting belongs, not the transport layer or the frontend.
    """
    size_before: int = entry.get("size_before_bytes") or 0
    size_after: int = entry.get("size_after_bytes") or 0
    entry["size_before_display"] = _format_bytes(size_before)
    entry["size_after_display"] = _format_bytes(size_after)
    entry["percent_reduced"] = round((1 - (size_after / size_before)) * 100, 1) if size_before else None


def rebuild_compression(gateway: Any, protocol_names: set[str] | None = None) -> dict[str, Any]:
    """
    Triggers an immediate decompress -> recompress pass across every
    already-compressed chunk of the selected group(s)' raw table and all
    four rollup views. Called from the "Rebuild Compression" button (PATCH
    /api/timescale/compression/rebuild).

    Args:
        protocol_names: Which groups to act on -- protocol_name values as
            returned by list_compression_groups(), plus "shared_narrow"
            for the shared narrow stack. None acts on every group.
        See RollupManager.rebuild_compression for the per-group/per-table/
        per-chunk result shape and why this never touches a chunk that
        isn't already compressed.

    Adds `size_before_display` / `size_after_display` / `percent_reduced`
    to each group AND to each of its per-table entries, for the Rebuild
    Compression screen's post-rebuild "New Size" / "Percent Reduced"
    columns (see timescale_rebuild_compression.html).

    Raises RuntimeError if no bridge is attached, or the bridge hasn't
    finished connecting to TimescaleDB yet.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    result: dict[str, Any] = bridge.rollup_mgr.rebuild_compression(protocol_names=protocol_names)
    for group in result.get("groups", []):
        _annotate_compression_change(group)
        for table_row in group.get("tables", []):
            _annotate_compression_change(table_row)
    return result


# ---------------------------------------------------------------------------
# Bridge info pane — read-only snapshots for the device page's "Bridge
# Health", "Storage Overview", "Compression & Retention Status", and
# "Background Job Status" panels (see routers/timescale.py GET /health,
# /storage, /compression-retention, /jobs). Purely observational: none of
# these mutate anything, unlike the rollup functions above.
# ---------------------------------------------------------------------------
def _format_dt(value: Any) -> str:
    """
    Human-readable timestamp for template display, e.g. '2026-07-24 09:15
    UTC'. Formatted here rather than in the template since this app has no
    established Jinja datetime filter to rely on. None -> '—'.
    """
    if value is None:
        return "—"
    try:
        return value.strftime("%Y-%m-%d %H:%M %Z").strip()
    except AttributeError:
        return str(value)


def get_timescale_health(gateway: Any) -> dict[str, Any]:
    """
    Read-only connection/background-worker snapshot for the "Bridge
    Health" panel. See timescaledb.get_health_snapshot for the field list.

    Raises RuntimeError if no bridge is attached. Unlike the rollup
    functions, this does NOT require rollup_mgr to be initialized — a
    bridge that's still connecting is itself a valid, useful thing to show
    on a health panel, so this returns whatever it can rather than erroring.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    return bridge.get_health_snapshot()


def get_storage_overview(gateway: Any) -> list[dict[str, Any]]:
    """
    Read-only per-source-table storage snapshot for the "Storage Overview"
    panel, with a human-readable `size_display` added to each row. See
    timescaledb.get_storage_overview for the rest of the field list.

    Raises RuntimeError if no bridge is attached. Returns an empty list
    (rather than raising) if the bridge is attached but not yet connected
    to TimescaleDB, since there's simply nothing to report yet.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    rows: list[dict[str, Any]] = bridge.get_storage_overview()
    for row in rows:
        row["size_display"] = _format_bytes(row.get("size_bytes"))
        row["oldest_display"] = _format_dt(row.get("oldest"))
        row["newest_display"] = _format_dt(row.get("newest"))
    return rows


def get_compression_retention_summary(gateway: Any) -> dict[str, Any]:
    """
    Read-only compression/retention configuration summary for the
    "Compression & Retention Status" panel, with `dynamic_raw_tables` and
    `dynamic_views` lists merged in — see RollupManager.get_compression_
    retention_summary for the static-config field list, RollupManager.
    get_dynamic_raw_table_overview for the per-raw-table dynamic sizing
    rows (narrow plus every wide table, each with its own live-computed
    band), and RollupManager.get_dynamic_view_overview for the per-VIEW
    rows (narrow's four rollup views plus every wide table's own four,
    one row per (table, granularity) pair since a view's load is
    granularity-specific — see that method's docstring).

    Raises RuntimeError if no bridge is attached, or the bridge hasn't
    finished connecting to TimescaleDB yet (this is config sourced from
    the rollup manager's own attributes, not a live query, but the rollup
    manager itself doesn't exist until connected).
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    summary: dict[str, Any] = bridge.rollup_mgr.get_compression_retention_summary()
    summary["dynamic_raw_tables"] = bridge.rollup_mgr.get_dynamic_raw_table_overview()

    # Grouped by protocol for display (4 granularity rows per table) --
    # itertools.groupby only merges already-consecutive equal keys, not a
    # re-sort, which matters here the same way it does in list_rollup_
    # view_groups: it preserves get_dynamic_view_overview()'s narrow-first
    # ordering instead of alphabetizing "shared_narrow" in among protocol
    # names.
    flat_views: list[dict[str, Any]] = bridge.rollup_mgr.get_dynamic_view_overview()
    view_groups: list[dict[str, Any]] = []
    for protocol_name, rows_iter in itertools.groupby(flat_views, key=lambda r: r["protocol_name"]):
        view_groups.append({"protocol_name": protocol_name, "views": list(rows_iter)})
    summary["dynamic_view_groups"] = view_groups

    return summary


def get_background_jobs(gateway: Any) -> list[dict[str, Any]]:
    """
    Read-only snapshot of TimescaleDB's background scheduler jobs for
    every hypertable/view this bridge manages, for the "Background Job
    Status" panel. See RollupManager.get_background_jobs for the field
    list.

    Raises RuntimeError if no bridge is attached, or the bridge hasn't
    finished connecting to TimescaleDB yet.
    """
    bridge: Any | None = get_timescale_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No TimescaleDB bridge is attached to this gateway.")
    if bridge.rollup_mgr is None:
        raise RuntimeError("Rollup manager is not initialized yet — the bridge is not connected to TimescaleDB.")
    jobs: list[dict[str, Any]] = bridge.rollup_mgr.get_background_jobs()
    for job in jobs:
        job["last_successful_finish_display"] = _format_dt(job.get("last_successful_finish"))
        job["next_start_display"] = _format_dt(job.get("next_start"))
    return jobs


# ---------------------------------------------------------------------------
# Staging — in-memory, lives on app.state alongside the gateway.
#
# Shape: { protocol_name: { "wide_table_name": str,
#                            "columns": { column_name: data_type } } }
# ---------------------------------------------------------------------------

def _store(app_state: Any) -> dict[str, dict[str, Any]]:
    """Lazily initializes and returns the staging dict on app.state."""
    # Check if the attribute exists
    if not hasattr(app_state, "timescale_pending_deletions"):
        #Use setattr to dynamically apply it safely
        setattr(app_state, "timescale_pending_deletions", {})

    # Retrieve it via getattr to satisfy the static analyzer
    deletions: dict[str, dict[str, Any]] = getattr(app_state, "timescale_pending_deletions")
    return deletions


def _lock(app_state: Any) -> threading.RLock:
    """Lazily initializes and returns the staging lock on app.state."""
    if not hasattr(app_state, "timescale_pending_lock"):
        app_state.timescale_pending_lock = threading.RLock()
    return app_state.timescale_pending_lock


def stage_field_deletion(
    app_state: Any,
    protocol_name: str,
    wide_table_name: str,
    column_name: str,
    data_type: str,
    checked: bool,
) -> None:
    """
    Stages or un-stages a single field for deletion. Called on every
    checkbox toggle from the Delete Columns screen (PATCH
    /api/timescale/wide-tables/{protocol}/fields/{column}/stage).
    """
    with _lock(app_state):
        store: dict[str, dict[str, Any]] = _store(app_state)
        entry: dict[str, Any] | None = None
        if checked:
            entry = store.setdefault(
                protocol_name, {"wide_table_name": wide_table_name, "columns": {}}
            )
            entry["wide_table_name"] = wide_table_name
            entry["columns"][column_name] = data_type
        else:
            entry = store.get(protocol_name)
            if entry:
                entry["columns"].pop(column_name, None)
                if not entry["columns"]:
                    store.pop(protocol_name, None)


def get_staged_columns(app_state: Any, protocol_name: str) -> set[str]:
    """Column names currently staged for deletion on one protocol's wide table."""
    with _lock(app_state):
        entry: dict[str, Any] | None = _store(app_state).get(protocol_name)
        return set(entry["columns"].keys()) if entry else set()


def get_all_staged(app_state: Any) -> dict[str, dict[str, Any]]:
    """Returns a shallow copy of the full staged-deletions map, for a review/diff panel."""
    with _lock(app_state):
        return {
            protocol_name: {
                "wide_table_name": entry["wide_table_name"],
                "columns": dict(entry["columns"]),
            }
            for protocol_name, entry in _store(app_state).items()
        }


def has_staged_deletions(app_state: Any) -> bool:
    """Drives the commit/discard buttons' lit-up state, alongside has_dirty_settings/has_dirty_protocols."""
    with _lock(app_state):
        return bool(_store(app_state))


def staged_deletion_count(app_state: Any) -> int:
    """Total number of columns staged for deletion, across all protocols."""
    with _lock(app_state):
        return sum(len(entry["columns"]) for entry in _store(app_state).values())


def clear_staged_deletions(app_state: Any) -> None:
    """Discards all staged deletions without touching the database. Wired into /api/commit/discard."""
    with _lock(app_state):
        _store(app_state).clear()


# ---------------------------------------------------------------------------
# Commit — actually performs the drops via WideTableFieldManager
# ---------------------------------------------------------------------------

def commit_staged_deletions(gateway: Any, app_state: Any) -> list[dict[str, Any]]:
    """
    Executes every staged deletion against the live TimescaleDB bridge, one
    protocol at a time. Called from routers/commit.py's do_commit() as part
    of the global "Commit All Changes" flow.

    Each protocol that completes successfully is cleared from staging
    immediately, so a failure partway through does not re-offer
    already-applied deletions for retry on the next commit attempt. Any
    failure aborts the remaining protocols and re-raises so the caller's
    existing try/except turns it into a 500, matching do_commit()'s
    all-or-error behavior for the rest of the commit.

    Returns a list of per-protocol result summaries (successes only — the
    caller's except block handles the failure case).

    No-ops (returns []) if nothing is staged, without requiring a live
    bridge — so a commit with no pending column deletions never fails here
    even if TimescaleDB happens to be disconnected.
    """
    if not has_staged_deletions(app_state):
        return []

    mgr: WideTableFieldManager = _field_manager(gateway)
    staged: dict[str, dict[str, Any]] = get_all_staged(app_state)
    results: list[dict[str, Any]] = []

    with _lock(app_state):
        store: dict[str, dict[str, Any]] = _store(app_state)
        for protocol_name, entry in staged.items():
            column_names: list[str] = sorted(entry["columns"].keys())
            try:
                result: WideTableFieldDeletionResult = mgr.delete_fields(protocol_name, column_names)
            except Exception:
                _log.error(
                    "commit_staged_deletions: failed deleting %s from protocol '%s' — leaving it staged for retry.",
                    column_names, protocol_name,
                )
                raise
            else:
                store.pop(protocol_name, None)
                results.append({
                    "protocol_name": result.protocol_name,
                    "wide_table_name": result.wide_table_name,
                    "deleted": result.deleted,
                    "not_found": result.not_found,
                    "remaining_fields": result.remaining_fields,
                    "rollups_rebuilt": result.rollups_rebuilt,
                })

    _log.info(
        "commit_staged_deletions: committed %d protocol(s), %d column(s) total.",
        len(results), sum(len(r["deleted"]) for r in results),
    )
    return results


# ============================================================================
# INFLUXDB (v1 / v3)
# ============================================================================

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
        msg: str = f"No InfluxDB bridge named '{device_section}' is attached to this gateway."
        raise RuntimeError(msg)

    health: dict[str, Any] = bridge.get_health_snapshot()

    last_attempt: float = health.get("last_periodic_reconnect_attempt") or 0.0
    if last_attempt > 0:
        health["last_periodic_reconnect_display"] = _format_elapsed(time.time() - last_attempt)
    else:
        health["last_periodic_reconnect_display"] = "never"

    return health

def get_influxdb_storage(gateway: Any, device_section: str) -> dict[str, Any]:
    """
    Best-effort, read-only storage snapshot for the "Storage Overview"
    panel. See influxdb_out.get_storage_overview / influxdb3_out.
    get_storage_overview for the raw field list (the two line up so one
    template renders both). This adds display-only formatting on top:

    - `data_dir_size_display` — human-readable data_dir/object_store_dir size.
    - Each `table_stats` row gets `file_size_display` / `memory_display`
      (v3 only — `table_stats` is always empty on v1).
    - `columns_by_table` — the flat `columns` list (v3 only) grouped into
      {table_name: [{column_name, data_type, iox_column_type}, ...]} for
      per-table display, plus a `column_count` added onto each matching
      `table_stats` row.
    - `heap_profile.size_display` — human-readable raw profile size, when
      the heap-profile probe succeeded. This is still just the size of the
      undecoded binary profile, not a memory-usage figure — see
      influxdb3_out._probe_heap_profile for why nothing here decodes it
      into real allocation numbers.

    Raises RuntimeError if no InfluxDB bridge with this name is attached
    to the gateway.
    """
    bridge: Any | None = get_influxdb_bridge(gateway, device_section)
    if bridge is None:
        msg: str = f"No InfluxDB bridge named '{device_section}' is attached to this gateway."
        raise RuntimeError(msg)

    storage: dict[str, Any] = bridge.get_storage_overview()

    data_dir_size_bytes: int | None = storage.get("data_dir_size_bytes")
    storage["data_dir_size_display"] = (
        _format_bytes(data_dir_size_bytes) if data_dir_size_bytes is not None else None
    )

    # Group the flat schema-map rows by table, and fold a column count
    # onto each table_stats row so the panel can show it in one place.
    columns: list[dict[str, Any]] = storage.get("columns") or list[dict[str, Any]]()
    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        table_name: str | None = col.get("table_name")
        if not table_name:
            continue
        columns_by_table.setdefault(table_name, list[dict[str, Any]]()).append(col)
    storage["columns_by_table"] = columns_by_table

    table_stats: list[dict[str, Any]] = storage.get("table_stats") or list[dict[str, Any]]()
    for row in table_stats:
        row["file_size_display"] = _format_bytes(row.get("file_size_bytes"))
        row["memory_display"] = _format_bytes(row.get("memory_bytes"))
        row_table_name: str | None = row.get("table_name")
        row["column_count"] = (
            len(columns_by_table.get(row_table_name, list[dict[str, Any]]())) if row_table_name else 0
        )

    heap_profile: dict[str, Any] | None = storage.get("heap_profile")
    if heap_profile is not None:
        heap_size_bytes: int | None = heap_profile.get("size_bytes")
        heap_profile["size_display"] = _format_bytes(heap_size_bytes) if heap_size_bytes is not None else None

    return storage


# ============================================================================
# MQTT
# ============================================================================

def get_mqtt_bridge(gateway: Any, device_section: str) -> Any | None:
    """
    Finds the live mqtt bridge transport whose transport_name matches
    device_section (e.g. "transport.mqtt"), if any.

    Returns None if there's no gateway yet (startup race), or no such
    transport is configured under that name. Duck-typed on the class name
    (rather than isinstance) so this module never needs a hard import of
    the mqtt transport class, mirroring timescale_service.get_timescale_
    bridge() / influxdb_service.get_influxdb_bridge()'s access pattern.
    """
    if gateway is None:
        return None
    transports: list[Any] = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if type(t).__name__ == "mqtt" and getattr(t, "transport_name", None) == device_section:
            return t
    return None


def is_mqtt_bridge(gateway: Any, device_section: str) -> bool:
    """True when an MQTT bridge with this name is attached to the gateway."""
    return get_mqtt_bridge(gateway, device_section) is not None


def get_mqtt_health(gateway: Any, device_section: str) -> dict[str, Any]:
    """
    Read-only connection/reconnect/write-topic snapshot for the "Bridge
    Health" panel. See mqtt.get_health_snapshot for the field list.

    Raises RuntimeError if no MQTT bridge with this name is attached to
    the gateway.
    """
    bridge: Any | None = get_mqtt_bridge(gateway, device_section)
    if bridge is None:
        msg: str = f"No MQTT bridge named '{device_section}' is attached to this gateway."
        raise RuntimeError(msg)
    return bridge.get_health_snapshot()


# ============================================================================
# PROMETHEUS
# ============================================================================

def get_prometheus_bridge(gateway: Any, device_section: str) -> Any | None:
    """
    Finds the live prometheus_out bridge transport whose transport_name
    matches device_section (e.g. "transport.prometheus_out"), if any.

    Returns None if there's no gateway yet (startup race), or no such
    transport is configured under that name. Duck-typed on the class name
    (rather than isinstance) so this module never needs a hard import of
    the prometheus_out transport class, mirroring get_influxdb_bridge() /
    get_mqtt_bridge()'s access pattern.
    """
    if gateway is None:
        return None
    transports: list[Any] = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if type(t).__name__ == "prometheus_out" and getattr(t, "transport_name", None) == device_section:
            return t
    return None


def is_prometheus_bridge(gateway: Any, device_section: str) -> bool:
    """True when a Prometheus bridge with this name is attached to the gateway."""
    return get_prometheus_bridge(gateway, device_section) is not None


def _format_duration(seconds: float | None) -> str:
    """
    Human-readable plain duration for the bridge summary's "Uptime" field,
    e.g. 3725.0 -> '1h 2m'. Deliberately distinct from _format_elapsed
    above (which appends "ago" and is meant for "time since an event") --
    an uptime figure reads oddly as "1h 2m ago". None/0/negative -> '—'.
    """
    if not seconds or seconds < 0:
        return "—"
    total_seconds: int = int(seconds)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total_seconds}s"


def get_prometheus_health(gateway: Any, device_section: str) -> dict[str, Any]:
    """
    Read-only in-memory-registry summary for the "Bridge Health" panel —
    metrics registered, standalone-server state, and machine counts by
    connectivity bucket. See prometheus_out.get_bridge_summary for the raw
    field list; this adds a human-readable `uptime_display`.

    Raises RuntimeError if no Prometheus bridge with this name is attached
    to the gateway.
    """
    bridge: Any | None = get_prometheus_bridge(gateway, device_section)
    if bridge is None:
        msg: str = f"No Prometheus bridge named '{device_section}' is attached to this gateway."
        raise RuntimeError(msg)
    summary: dict[str, Any] = bridge.get_bridge_summary()
    summary["uptime_display"] = _format_duration(summary.get("uptime_seconds"))
    return summary


def get_prometheus_targets(gateway: Any, device_section: str) -> list[dict[str, Any]]:
    """
    Read-only per-machine scrape-target snapshot for the "Target Health"
    panel — one row per machine this bridge has ever been wired to or
    received data from. See prometheus_out.get_target_health for the raw
    field list; this adds `seconds_since_last_scrape` and a human-readable
    `last_scrape_display` (reusing _format_elapsed from the INFLUXDB
    section above — "time since an event" is exactly what it's for, and
    duplicating it here would just be the same function twice).

    Raises RuntimeError if no Prometheus bridge with this name is attached
    to the gateway.
    """
    bridge: Any | None = get_prometheus_bridge(gateway, device_section)
    if bridge is None:
        msg: str = f"No Prometheus bridge named '{device_section}' is attached to this gateway."
        raise RuntimeError(msg)

    rows: list[dict[str, Any]] = bridge.get_target_health()
    now: float = time.time()
    for row in rows:
        last_ts: float | None = row.get("last_scrape_timestamp")
        if last_ts:
            elapsed: float = now - last_ts
            row["seconds_since_last_scrape"] = elapsed
            row["last_scrape_display"] = _format_elapsed(elapsed)
        else:
            row["seconds_since_last_scrape"] = None
            row["last_scrape_display"] = "never"
    return rows
