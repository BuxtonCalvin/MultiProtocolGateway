# Description: services/timescale_service.py — Runtime helpers for the TimescaleDB "Delete Columns" admin screen.
# File: timescale_service.py
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
services/timescale_service.py — Runtime helpers for the TimescaleDB
"Delete Columns" admin screen.

The gateway instance is accessed via app.state.gateway, passed in at
startup — same pattern as analysis_service.py. Wide tables and their
columns are live TimescaleDB/Postgres schema, not config.cfg settings, so
this module talks to the running timescaledb bridge transport directly via
WideTableFieldManager (see transports/timescaledb.py) rather than the
staging (SQLite) DB used by Setting/ProtocolRegister.

Staging model:
Checking a field's checkbox in the UI does NOT delete the column
immediately. It stages the deletion in-memory, on app.state alongside the
gateway, so the change can ride the app's existing "Commit All Changes"
button exactly like a staged Setting edit does. commit_staged_deletions()
is what actually calls WideTableFieldManager.delete_fields() against
Postgres — it's wired into routers/commit.py's do_commit(), and
clear_staged_deletions() is wired into its discard endpoint.

Staging is intentionally in-memory rather than a new DB table: it mirrors
analysis_service.py's live-gateway-state approach rather than the
config-staging approach, since there is no "value_disk" for a column that
no longer exists once dropped — there's nothing to roll back to.
"""
from __future__ import annotations

import itertools
import logging
import threading
from typing import TYPE_CHECKING, Any

_log: logging.Logger = logging.getLogger(__name__)

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
# always derived/re-creatable from the wide/narrow tables (never a source of
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
