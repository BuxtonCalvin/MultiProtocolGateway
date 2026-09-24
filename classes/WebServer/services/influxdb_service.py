# Description: services/influxdb_service.py — Runtime helpers for the InfluxDB v1/v3 "Metrics Edit" admin screens: bridge discovery, read-only listings, staging, and commit.
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
services/influxdb_service.py — Runtime helpers backing routers/influxdb.py's
"InfluxDB -> Metrics Edit 1.x" / "Metrics Edit 3.x" admin screens.

Mirrors services/bridge_service.py's TimescaleDB Metrics Edit section
(bridge discovery -> read-only listings -> staging -> commit), but as its
own file per the version's own request, since InfluxDB v1 (influxdb_out,
InfluxQL) and v3 (influxdb3_out, SQL/DataFusion) are different enough
backends that every actual query/write lives in their own transport module
(classes/transports/influxdb_out.py's InfluxV1AdminManager, classes/
transports/influxdb3_out.py's Influx3AdminManager) -- this file only ever
orchestrates HTTP/staging concerns, dispatching to whichever admin manager
matches the `version` ("1" or "3") a call is for.

Both influxdb_out and influxdb3_out are optional/pluggable transports, same
as timescaledb -- a deployment without either configured shouldn't crash
the webserver on import. InfluxV1_Available / Influx3_Available gate every
function below independently, and drive whether the "InfluxDB" nav pad (and
which of its two menu items) is shown at all.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

from starlette.datastructures import State

from ...transports.transport_base import transport_base

_log: logging.Logger = logging.getLogger(__name__)

# See bridge_service.py's own top-of-TimescaleDB-section comment for why
# these TYPE_CHECKING imports exist alongside a *different*-named runtime
# binding (_InfluxV1AdminManagerImpl / _Influx3AdminManagerImpl) below --
# same idiom, same reasoning, applied to two admin managers instead of one.
if TYPE_CHECKING:
    from protocol_gateway import Protocol_Gateway

    from ...transports.influxdb3_out import (
        Influx3AdminManager,
        Influx3EditPreview,
        Influx3EditResult,
        Influx3MetricEditDevice,
        Influx3MetricEditField,
        influxdb3_out,
    )
    from ...transports.influxdb_out import (
        InfluxV1AdminManager,
        InfluxV1EditPreview,
        InfluxV1EditResult,
        InfluxV1MetricEditDevice,
        InfluxV1MetricEditField,
        influxdb_out,
    )

InfluxVersion = Literal["1", "3"]

# Declared Any up front, before either branch assigns to it -- see
# bridge_service.py's _BridgeAdminManagerImpl for why this matters (a type
# checker would otherwise infer `type[...] | None` from the two conditional
# assignments and refuse to treat the name as callable).
_InfluxV1AdminManagerImpl: Any
try:
    from ...transports.influxdb_out import (
        InfluxV1AdminManager as _InfluxV1AdminManagerImpl,
    )
    InfluxV1_Available = True
except ImportError:
    _log.debug("influxdb_service: transports.influxdb_out is not importable -- the InfluxDB v1 Metrics Edit UI will stay hidden.")
    _InfluxV1AdminManagerImpl = None
    InfluxV1_Available = False

_Influx3AdminManagerImpl: Any
try:
    from ...transports.influxdb3_out import (
        Influx3AdminManager as _Influx3AdminManagerImpl,
    )
    Influx3_Available = True
except ImportError:
    _log.debug("influxdb_service: transports.influxdb3_out is not importable -- the InfluxDB v3 Metrics Edit UI will stay hidden.")
    _Influx3AdminManagerImpl = None
    Influx3_Available = False


# ---------------------------------------------------------------------------
# Live bridge discovery
# ---------------------------------------------------------------------------

def get_influxdb1_bridge(gateway: "Protocol_Gateway | None") -> "influxdb_out | None":
    """
    Finds the live influxdb_out (v1) bridge transport on the gateway, if
    any. Duck-typed on the class name (rather than isinstance), mirroring
    bridge_service.get_timescale_bridge()'s access pattern -- this module
    never needs a hard import of the transport class itself at runtime.

    Returns the first match if more than one is configured, same
    single-bridge assumption get_timescale_bridge() makes.
    """
    if gateway is None or not InfluxV1_Available:
        return None
    transports: list[transport_base] = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if type(t).__name__ == "influxdb_out":
            return t  # type: ignore[return-value]
    return None


def get_influxdb3_bridge(gateway: "Protocol_Gateway | None") -> "influxdb3_out | None":
    """The v3 (influxdb3_out) counterpart of get_influxdb1_bridge() -- see that function's docstring."""
    if gateway is None or not Influx3_Available:
        return None
    transports: list[transport_base] = getattr(gateway, "_Protocol_Gateway__transports", [])
    for t in transports:
        if type(t).__name__ == "influxdb3_out":
            return t  # type: ignore[return-value]
    return None


def is_influxdb1_available(gateway: "Protocol_Gateway | None") -> bool:
    """True when a live InfluxDB v1 bridge is attached to this gateway. Drives the "Metrics Edit 1.x" nav item."""
    return get_influxdb1_bridge(gateway) is not None


def is_influxdb3_available(gateway: "Protocol_Gateway | None") -> bool:
    """True when a live InfluxDB v3 bridge is attached to this gateway. Drives the "Metrics Edit 3.x" nav item."""
    return get_influxdb3_bridge(gateway) is not None


def is_influxdb_available(gateway: "Protocol_Gateway | None") -> bool:
    """True when at least one of InfluxDB v1/v3 is attached. Drives whether the "InfluxDB" nav pad is shown at all."""
    return is_influxdb1_available(gateway) or is_influxdb3_available(gateway)


def _v1_admin_manager(gateway: "Protocol_Gateway | None") -> "InfluxV1AdminManager":
    bridge = get_influxdb1_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No InfluxDB v1 bridge is attached to this gateway.")
    return _InfluxV1AdminManagerImpl(bridge)


def _v3_admin_manager(gateway: "Protocol_Gateway | None") -> "Influx3AdminManager":
    bridge = get_influxdb3_bridge(gateway)
    if bridge is None:
        raise RuntimeError("No InfluxDB v3 bridge is attached to this gateway.")
    return _Influx3AdminManagerImpl(bridge)


def _validate_version(version: str) -> InfluxVersion:
    """Narrows an untrusted `version` path/query param to InfluxVersion, raising ValueError otherwise."""
    if version not in ("1", "3"):
        msg: str = f"Unknown InfluxDB version '{version}' -- expected '1' or '3'."
        raise ValueError(msg)
    return version  # type: ignore[return-value]


def supports_delete(version: str) -> bool:
    """
    Whether the given InfluxDB version's Metrics Edit screen offers a
    "Delete value(s)" action at all -- True for v1 (InfluxQL DELETE, whole
    point only -- see InfluxV1AdminManager's module docstring), False for
    v3 (InfluxDB 3 Core has no DELETE at all -- see Influx3AdminManager.
    SUPPORTS_DELETE). Read directly off each admin manager's own
    SUPPORTS_DELETE where possible so this can never drift from what
    edit_metric_values() actually does; v1 has no such flag since delete
    support there is unconditional, not bridge-dependent. This is a
    version-level answer only -- whether the *connected* v3 server is Core
    (and so cannot delete) is delete_unavailable_reason()'s job.
    """
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1":
        return True
    return bool(getattr(_Influx3AdminManagerImpl, "SUPPORTS_DELETE", False))


def delete_unavailable_reason(gateway: "Protocol_Gateway | None", version: str) -> str | None:
    """
    Why "Delete value(s)" cannot be used against the connected server right now, or None if it can.

    Only InfluxDB 3 Core is ever ruled out: it has no row-delete API, and
    Influx3AdminManager.detect_edition() can identify it from GET /ping. When the
    edition is Enterprise -- or cannot be determined -- this returns None and the
    server itself remains the authority (an Enterprise server without the storage
    engine upgrade still rejects the request with its own message). v1 never has
    a reason: its InfluxQL DELETE is unconditional.
    """
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1" or not supports_delete(version):
        return None
    if _v3_admin_manager(gateway).detect_edition() == "core":
        return "Deleting values is not available on InfluxDB 3 Core — it requires InfluxDB 3 Enterprise."
    return None


# ---------------------------------------------------------------------------
# Read-only listings for the UI
# ---------------------------------------------------------------------------

def list_metric_edit_measurements(gateway: "Protocol_Gateway | None", version: str) -> list[str]:
    """Returns every measurement/table for the Metrics Edit measurement picker, for either InfluxDB version."""
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1":
        return _v1_admin_manager(gateway).list_metric_edit_measurements()
    return _v3_admin_manager(gateway).list_metric_edit_measurements()


def list_metric_edit_devices(
    gateway: "Protocol_Gateway | None", version: str, measurement: str
    ) -> list[dict[str, str | None]]:
    """Returns [{device_identifier, device_name}, ...] for the Metrics Edit device picker, for either InfluxDB version."""
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1":
        v1_devices: list["InfluxV1MetricEditDevice"] = _v1_admin_manager(gateway).list_metric_edit_devices(measurement)
        return [{"device_identifier": d.device_identifier, "device_name": d.device_name} for d in v1_devices]
    v3_devices: list["Influx3MetricEditDevice"] = _v3_admin_manager(gateway).list_metric_edit_devices(measurement)
    return [{"device_identifier": d.device_identifier, "device_name": d.device_name} for d in v3_devices]


def lookup_metric_edit_device_name(
    gateway: "Protocol_Gateway | None", version: str, measurement: str, device_identifier: str
    ) -> str | None:
    """
    Returns the friendly device_name for one device_identifier, or None, for either InfluxDB version.

    v1's device list carries identifiers only (index-only SHOW TAG VALUES), so the
    name is fetched here once a device is picked. v3's device list already
    includes names, so its manager returns None.
    """
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1":
        return _v1_admin_manager(gateway).get_metric_edit_device_name(measurement, device_identifier)
    return _v3_admin_manager(gateway).get_metric_edit_device_name(measurement, device_identifier)


def list_metric_edit_fields(
    gateway: "Protocol_Gateway | None", version: str, measurement: str
    ) -> list[dict[str, str | None]]:
    """Returns [{name, data_type}, ...] for the Metrics Edit field picker, for either InfluxDB version."""
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1":
        v1_fields: list["InfluxV1MetricEditField"] = _v1_admin_manager(gateway).list_metric_edit_fields(measurement)
        return [{"name": f.name, "data_type": f.field_type} for f in v1_fields]
    v3_fields: list["Influx3MetricEditField"] = _v3_admin_manager(gateway).list_metric_edit_fields(measurement)
    return [{"name": f.name, "data_type": f.data_type} for f in v3_fields]


def preview_metric_edit(
    gateway: "Protocol_Gateway | None",
    version: str,
    measurement: str,
    device_identifier: str,
    field_name: str,
    start_time: datetime,
    end_time: datetime,
    ) -> dict[str, Any]:
    """
    Returns {row_count, sample: [{time_iso, value}, ...]} for the Metrics
    Edit "Preview" step, for either InfluxDB version -- a read-only look at
    what a matching edit_metric_values() call would affect.
    """
    resolved: InfluxVersion = _validate_version(version)
    if resolved == "1":
        v1_preview: "InfluxV1EditPreview" = _v1_admin_manager(gateway).preview_metric_edit(
            measurement, device_identifier, field_name, start_time, end_time
        )
        return {
            "row_count": v1_preview.row_count,
            "sample": [{"time_iso": s.time_iso, "value": s.value} for s in v1_preview.sample],
        }
    v3_preview: "Influx3EditPreview" = _v3_admin_manager(gateway).preview_metric_edit(
        measurement, device_identifier, field_name, start_time, end_time
    )
    return {
        "row_count": v3_preview.row_count,
        "sample": [{"time_iso": s.time_iso, "value": s.value} for s in v3_preview.sample],
    }


# ---------------------------------------------------------------------------
# Metrics Edit — staging + commit. Independent staging store from
# TimescaleDB's (bridge_service.StagedMetricEdit) — a different admin
# screen, a different backend, a different set of fields on each staged
# entry (field_name is nullable here, for a v1 "delete", which has no
# field concept at all).
#
# Same shape as TimescaleDB's: one entry per complete, atomic Metrics Edit
# request (version, measurement, device, field, range, action, value),
# staged via "Add to Staged Changes" and applied (all versions/measurements
# together) by the same header "Commit All Changes" button, through
# commit_staged_influx_edits() below.
# ---------------------------------------------------------------------------

class StagedInfluxEdit(TypedDict):
    edit_id: str
    version: InfluxVersion
    measurement: str
    device_identifier: str
    device_label: str
    field_name: str | None            # None only for a v1 "delete"
    start_time: datetime
    end_time: datetime
    action: str                       # "delete" or "set_value"
    new_value: float | int | str | bool | None


def _influx_edit_store(app_state: State) -> dict[str, StagedInfluxEdit]:
    """Lazily initializes and returns the InfluxDB Metrics Edit staging dict (edit_id -> entry) on app.state."""
    if not hasattr(app_state, "influxdb_pending_metric_edits"):
        setattr(app_state, "influxdb_pending_metric_edits", {})
    store: dict[str, StagedInfluxEdit] = getattr(app_state, "influxdb_pending_metric_edits")
    return store


def _influx_edit_lock(app_state: State) -> threading.RLock:
    """Lazily initializes and returns the InfluxDB Metrics Edit staging lock on app.state."""
    if not hasattr(app_state, "influxdb_pending_metric_edits_lock"):
        app_state.influxdb_pending_metric_edits_lock = threading.RLock()
    return app_state.influxdb_pending_metric_edits_lock


def validate_metric_edit_value(
    gateway: "Protocol_Gateway | None",
    version: str,
    measurement: str,
    field_name: str | None,
    action: str,
    new_value: object,
    ) -> None:
    """
    Read-only type-check of a "set_value" edit's replacement value before
    it's staged -- dispatches to the matching admin manager's own
    validate_metric_edit_value(), so the exact same coercion logic
    edit_metric_values() runs at commit time is what rejects an invalid
    value here, at staging time (see stage_metric_edit()).

    Raises:
        ValueError: unknown version/action, action == "delete" requested
                    for v3 (see supports_delete()), or (from the admin
                    manager) a value that doesn't fit the field's type.
    """
    resolved: InfluxVersion = _validate_version(version)
    if action not in ("delete", "set_value"):
        msg: str = f"Unknown action '{action}' -- expected 'delete' or 'set_value'."
        raise ValueError(msg)
    if action == "delete" and not supports_delete(resolved):
        raise ValueError("Deleting is not supported for this InfluxDB v3 server -- see the Action field's help text.")

    if resolved == "1":
        _v1_admin_manager(gateway).validate_metric_edit_value(measurement, field_name, action, new_value)  # type: ignore[arg-type]
    else:
        _v3_admin_manager(gateway).validate_metric_edit_value(measurement, field_name, action, new_value)  # type: ignore[arg-type]


def stage_metric_edit(
    gateway: "Protocol_Gateway | None",
    app_state: State,
    version: str,
    measurement: str,
    device_identifier: str,
    device_label: str,
    field_name: str | None,
    start_time: datetime,
    end_time: datetime,
    action: str,
    new_value: float | int | str | bool | None = None,
    ) -> str:
    """
    Stages one InfluxDB Metrics Edit request. Called from the "Add to
    Staged Changes" button, after the admin has reviewed a
    preview_metric_edit() result. Nothing is written to InfluxDB until the
    admin presses the existing "Commit All Changes" button, which calls
    commit_staged_influx_edits() below.

    Runs validate_metric_edit_value() first, so an obviously invalid value
    is rejected here rather than only surfacing at commit time. Raises
    straight through on failure -- nothing is staged.

    Returns the generated edit_id, so the caller can render it into the
    staged-changes list with a matching "remove" control.
    """
    resolved: InfluxVersion = _validate_version(version)
    validate_metric_edit_value(gateway, resolved, measurement, field_name, action, new_value)

    edit_id: str = uuid.uuid4().hex
    with _influx_edit_lock(app_state):
        _influx_edit_store(app_state)[edit_id] = {
            "edit_id": edit_id,
            "version": resolved,
            "measurement": measurement,
            "device_identifier": device_identifier,
            "device_label": device_label,
            "field_name": field_name,
            "start_time": start_time,
            "end_time": end_time,
            "action": action,
            "new_value": new_value,
        }
    return edit_id


def unstage_metric_edit(app_state: State, edit_id: str) -> bool:
    """Removes one staged InfluxDB Metrics Edit entry by id. Returns False if it was already gone."""
    with _influx_edit_lock(app_state):
        return _influx_edit_store(app_state).pop(edit_id, None) is not None


def get_staged_metric_edits(app_state: State) -> list[StagedInfluxEdit]:
    """Returns every currently staged InfluxDB Metrics Edit entry, in stage order, for the staged-changes panel."""
    with _influx_edit_lock(app_state):
        return list(_influx_edit_store(app_state).values())


def has_staged_metric_edits(app_state: State) -> bool:
    """Drives the commit/discard buttons' lit-up state, alongside TimescaleDB's has_staged_metric_edits/has_staged_deletions/etc."""
    with _influx_edit_lock(app_state):
        return bool(_influx_edit_store(app_state))


def staged_metric_edit_count(app_state: State) -> int:
    """Total number of staged InfluxDB Metrics Edit entries, for the header's dirty-count badge."""
    with _influx_edit_lock(app_state):
        return len(_influx_edit_store(app_state))


def clear_staged_metric_edits(app_state: State) -> None:
    """Discards all staged InfluxDB Metrics Edit entries without touching InfluxDB. Wired into /api/commit/discard."""
    with _influx_edit_lock(app_state):
        _influx_edit_store(app_state).clear()


def commit_staged_metric_edits(gateway: "Protocol_Gateway | None", app_state: State) -> list[dict[str, Any]]:
    """
    Executes every staged InfluxDB Metrics Edit entry (v1 and v3 together,
    in the order they were staged) against their respective live bridges.
    Called from routers/commit.py's do_commit() as part of the global
    "Commit All Changes" flow, alongside TimescaleDB's commit_staged_
    metric_edits()/commit_staged_deletions() -- three independent staging
    stores, all applied on the same commit.

    Each entry that completes successfully is cleared from staging
    immediately, so a failure partway through does not re-offer
    already-applied edits for retry on the next commit attempt. Any
    failure aborts the remaining entries and re-raises so the caller's
    existing try/except turns it into a 500, matching TimescaleDB's
    commit_staged_metric_edits()' all-or-error behavior.

    Returns a list of per-entry result summaries (successes only).

    No-ops (returns []) if nothing is staged, without requiring a live
    bridge -- so a commit with no pending InfluxDB edits never fails here
    even if InfluxDB happens to be disconnected.
    """
    if not has_staged_metric_edits(app_state):
        return []

    staged: list[StagedInfluxEdit] = get_staged_metric_edits(app_state)
    results: list[dict[str, Any]] = []

    with _influx_edit_lock(app_state):
        store: dict[str, StagedInfluxEdit] = _influx_edit_store(app_state)
        for entry in staged:
            pending: bool = False
            try:
                if entry["version"] == "1":
                    v1_result: "InfluxV1EditResult" = _v1_admin_manager(gateway).edit_metric_values(
                        entry["measurement"],
                        entry["device_identifier"],
                        entry["action"],  # type: ignore[arg-type]
                        entry["start_time"],
                        entry["end_time"],
                        field_name=entry["field_name"],
                        new_value=entry["new_value"],
                    )
                    points_affected: int = v1_result.points_affected
                else:
                    v3_result: "Influx3EditResult" = _v3_admin_manager(gateway).edit_metric_values(
                        entry["measurement"],
                        entry["device_identifier"],
                        entry["action"],  # type: ignore[arg-type]
                        entry["start_time"],
                        entry["end_time"],
                        field_name=entry["field_name"],
                        new_value=entry["new_value"],
                    )
                    points_affected = v3_result.points_affected
                    # "delete" on v3 is an InfluxDB 3 Enterprise row-delete
                    # request -- accepted here, but applied asynchronously
                    # by the server (up to 24h by default), unlike every
                    # other action/version, which are synchronous. Surfaced
                    # in the result so the caller (routers/commit.py's
                    # response, and ultimately the admin) doesn't read
                    # "committed" as "already gone."
                    pending = v3_result.pending
            except Exception:
                _log.error(
                    "commit_staged_metric_edits: failed applying edit_id=%s (v%s, %s on %s, device=%s) -- "
                    "leaving it staged for retry.",
                    entry["edit_id"], entry["version"], entry["action"], entry["measurement"],
                    entry["device_identifier"],
                )
                raise
            else:
                store.pop(entry["edit_id"], None)
                results.append({
                    "edit_id": entry["edit_id"],
                    "version": entry["version"],
                    "measurement": entry["measurement"],
                    "device_identifier": entry["device_identifier"],
                    "field_name": entry["field_name"],
                    "action": entry["action"],
                    "points_affected": points_affected,
                    "pending": pending,
                })

    _log.info(
        "commit_staged_metric_edits: committed %d InfluxDB edit(s), %d point(s) total.",
        len(results), sum(r["points_affected"] for r in results),
    )
    return results
