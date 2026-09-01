# Description: Implements analysis functionality for the MultiProtocolGateway application.
# File: analysis.py
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

from __future__ import annotations

import asyncio
import csv
import json
import logging
import queue
import shutil
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...transports.modbus_base import ProtocolAnalysisReport, modbus_base
from ...transports.transport_base import transport_base

if TYPE_CHECKING:
    from _csv import Writer

    # Deferred at runtime — importing protocol_gateway at module load time
    # risks a circular import, since it's what wires up the WebServer app
    # in the first place (see the same pattern in commit.py/devices.py).
    # Only needed here, under TYPE_CHECKING, for the annotations below.
    from protocol_gateway import Protocol_Gateway

router = APIRouter(prefix="/api/analyze", tags=["analysis"])

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level progress queue registry
# ---------------------------------------------------------------------------
# The POST endpoint creates a queue here before starting the analysis.
# The GET/SSE endpoint reads from it.  This lets both share a single scan
# that runs inside analyze_protocols (under _transport_lock) rather than
# running two competing scans.
ProgressMessage = dict[str, str | int]


class _ScanDoneSentinel:
    """Sentinel placed on a progress queue to signal scan completion."""

    __slots__ = ()


ProgressQueueItem = ProgressMessage | _ScanDoneSentinel

_SCAN_DONE = _ScanDoneSentinel()
_progress_queues: dict[str, "queue.Queue[ProgressQueueItem]"] = {}
_progress_queues_lock: threading.Lock = threading.Lock()


def _register_progress_queue(device_name: str) -> "queue.Queue[ProgressQueueItem]":
    q: "queue.Queue[ProgressQueueItem]" = queue.Queue()
    with _progress_queues_lock:
        _progress_queues[device_name] = q
    return q


def _get_progress_queue(device_name: str) -> "queue.Queue[ProgressQueueItem] | None":
    with _progress_queues_lock:
        return _progress_queues.get(device_name)


def _unregister_progress_queue(device_name: str) -> None:
    with _progress_queues_lock:
        _progress_queues.pop(device_name, None)


class AnalyzeRequest(BaseModel):
    protocol_names: list[str] = Field(default_factory=list[str])
    current_protocol: str | None = None
    batch_size: int = Field(default=40, ge=1, le=125)


class AnalysisChange(BaseModel):
    protocol_name: str
    registry_type: str
    action: str
    register_address: str
    variable_name: str = ""
    documented_name: str = ""
    data_type: str = ""
    values_range: str = ""
    unit: str = ""
    read_interval: str = ""
    write_mode: str = "R"
    adjustments: str = ""
    note: str = ""
    raw_value: float | None = None


class CommitAnalysisRequest(BaseModel):
    changes: list[AnalysisChange] = Field(default_factory=list[AnalysisChange])


def _clean_device_name(device_name: str) -> str:
    """
    Strip any surrounding quotation marks that can appear when a Jinja
    tojson value is double-serialized through a URL parameter.
    e.g. '"Inverter1"' -> 'Inverter1'
    """
    return device_name.strip().strip("'\"")


def _find_transport(gateway: "Protocol_Gateway | None", device_name: str) -> transport_base | None:
    if gateway is None:
        return None
    clean: str = _clean_device_name(device_name)
    transport_names: set[str] = {clean, f"transport.{clean}"}
    return next(
        (
            t for t in getattr(gateway, "_Protocol_Gateway__transports", [])
            if t.transport_name in transport_names
        ),
        None,
    )


def _require_modbus_transport(request: Request, device_name: str) -> modbus_base:
    gateway: "Protocol_Gateway | None" = getattr(request.app.state, "gateway", None)
    transport: transport_base | None = _find_transport(gateway, device_name)
    if transport is None:
        raise HTTPException(status_code=404, detail=f"Transport '{device_name}' not found")
    if not isinstance(transport, modbus_base):
        raise HTTPException(
            status_code=400,
            detail="Analyze is only available for Modbus-based scrapers",
        )
    return transport


def _normalize_header(value: str) -> str:
    return (
        (value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
        .replace("/", "_")
    )


HEADER_ALIASES: dict[str, str] = {
    "address": "register",
    "adjustments": "adjustments",
    "data_type": "data_type",
    "datatype": "data_type",
    "desc": "note",
    "description": "note",
    "documented": "documented_name",
    "documented_name": "documented_name",
    "interval": "read_interval",
    "name": "documented_name",
    "note": "note",
    "notes": "note",
    "r_w": "write_mode",
    "range": "values",
    "read_interval": "read_interval",
    "read_interval_ms": "read_interval",
    "readinterval": "read_interval",
    "reg": "register",
    "register": "register",
    "register_address": "register",
    "type": "data_type",
    "unit": "unit",
    "units": "unit",
    "value": "values",
    "values": "values",
    "values_range": "values",
    "variable": "variable_name",
    "variable_name": "variable_name",
    "writable": "write_mode",
    "write": "write_mode",
    "write_mode": "write_mode"
}


def _detect_delimiter(csv_path: Path) -> str:
    sample: str = csv_path.read_text(encoding="latin-1")[:4096]
    return ";" if sample.count(";") > sample.count(",") else ","


def _load_csv_matrix(csv_path: Path) -> tuple[str, list[str], list[list[str]]]:
    delimiter: str = _detect_delimiter(csv_path)
    with open(csv_path, newline="", encoding="latin-1") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        rows: list[list[str]] = list(reader)
    if not rows:
        msg: str = (f"CSV file is empty: {csv_path}")
        raise ValueError(msg)
    header: list[str] = list(rows[0])
    body: list[list[str]] = [row + [""] * (len(header) - len(row)) for row in rows[1:]]
    return delimiter, header, body


def _header_index_map(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, raw_header in enumerate(header):
        normalized: str = _normalize_header(raw_header)
        canonical: str = HEADER_ALIASES.get(normalized, normalized)
        mapping.setdefault(canonical, index)
    return mapping


def _get_cell(row: list[str], mapping: dict[str, int], canonical: str) -> str:
    index: int | None = mapping.get(canonical)
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def _set_cell(row: list[str], mapping: dict[str, int], canonical: str, value: str) -> None:
    index: int | None = mapping.get(canonical)
    if index is None:
        return
    while len(row) <= index:
        row.append("")
    row[index] = value


# Canonical column set shared by every hand-written and generated protocol
# map in protocols/ (see e.g. protocols/pace/pace_bms_v1.3.holding_registry_map.csv).
# Used when a brand-new registry-map CSV must be created from scratch for a
# stub-only protocol (a manufacturer folder with a JSON descriptor but no
# CSV yet) so the new file is immediately readable by protocol_settings,
# the Protocol Editor, and any other transport that loads registry maps.
STANDARD_REGISTRY_HEADER: list[str] = [
    "register",
    "variable_name",
    "documented_name",
    "unit",
    "data_type",
    "values",
    "read_interval",
    "writable",
    "adjustments",
    "note",
]


def _resolve_protocol_dir(protocols_dir: Path, protocol_name: str) -> Path:
    """Resolve the folder a protocol's files live (or should live) in.

    Uses the same prefix-matching convention as protocol_settings /
    _find_protocol_csv: the longest existing protocols/ subfolder name that
    protocol_name starts with. Falls back to protocols_dir itself if no
    folder matches (e.g. a protocol name that doesn't share a prefix with
    any existing manufacturer folder).
    """
    protocols_dir = Path(protocols_dir)
    protocol_lower: str = protocol_name.lower()

    existing_folders: list[Path] = [f for f in protocols_dir.iterdir() if f.is_dir()]
    existing_folders.sort(key=lambda f: len(f.name), reverse=True)

    for folder in existing_folders:
        if protocol_lower.startswith(folder.name.lower()):
            return folder

    return protocols_dir


def _create_protocol_csv(protocols_dir: Path, protocol_name: str, registry_type: str) -> Path:
    """Create a new, header-only registry-map CSV for a protocol that doesn't
    have one yet — the stub-protocol case (a manufacturer folder with only a
    JSON descriptor). Follows the same ``<protocol>.<type>_registry_map.csv``
    naming convention as ``protocol_settings.load_registry_map`` and writes
    the standard column header, so the result is a normal, editable registry
    map — no different from one written by hand or shipped with MPG.

    The only real decision here is *which* registry type to create (holding,
    input, coil, or discrete) — that comes from the analysis change itself
    (each live register is scored against a specific register type during
    the scan), so no guessing is needed.
    """
    target_dir: Path = _resolve_protocol_dir(protocols_dir, protocol_name)
    target_dir.mkdir(parents=True, exist_ok=True)

    registry_type_lower: str = registry_type.lower()
    csv_path: Path = target_dir / f"{protocol_name}.{registry_type_lower}_registry_map.csv"

    with open(csv_path, "w", newline="", encoding="latin-1") as handle:
        writer: Writer = csv.writer(handle, delimiter=",")
        writer.writerow(STANDARD_REGISTRY_HEADER)

    _log.info(
        "Commit: created new %s registry map for protocol %r at %s",
        registry_type, protocol_name, csv_path,
    )
    return csv_path


def _find_protocol_csv(protocols_dir: Path, protocol_name: str, registry_type: str) -> Path:
    protocols_dir = Path(protocols_dir)
    registry_type = registry_type.lower()
    protocol_lower: str = protocol_name.lower()

    target_dir: Path = _resolve_protocol_dir(protocols_dir, protocol_name)

    token: str = f"{registry_type}_"

    # Search is optimized to run only within target_dir
    candidates: list[Path] = [
        path for path in target_dir.rglob("*.csv")
        if protocol_lower in path.stem.lower()
        and "registry_map" in path.name.lower()
        and token in f"{path.stem.lower()}_"
    ]

    if not candidates:
        all_csvs: list[Path] = list(target_dir.rglob("*.csv"))
        _log.debug(
            "_find_protocol_csv: no match for protocol=%r registry=%r. "
            "target_dir=%s contains %d CSV(s): %s",
            protocol_name, registry_type, target_dir, len(all_csvs),
            [p.name for p in all_csvs[:20]],
        )
        msg: str = (f"Unable to locate {registry_type} registry CSV for protocol '{protocol_name}'")
        _log.debug(msg)
        raise FileNotFoundError(msg)

    candidates.sort()
    return candidates[0]



def _backup_protocol_csv(csv_path: Path) -> None:
    timestamp: str = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_path: Path = csv_path.with_suffix(f"{csv_path.suffix}.bak.{timestamp}")
    shutil.copy2(csv_path, backup_path)


def _row_matches_change(row: list[str], mapping: dict[str, int], change: AnalysisChange) -> bool:
    row_variable: str = _get_cell(row, mapping, "variable_name").lower()
    row_documented: str = _get_cell(row, mapping, "documented_name").lower()
    row_register: str = _get_cell(row, mapping, "register").lower()
    if change.variable_name and row_variable == change.variable_name.lower():
        return True
    if change.documented_name and row_documented == change.documented_name.lower():
        return True
    return bool(change.register_address and row_register == change.register_address.lower())


def _row_exists(rows: list[list[str]], mapping: dict[str, int], change: AnalysisChange) -> bool:
    for row in rows:
        if _row_matches_change(row, mapping, change):
            return True
    return False


def _build_added_row(header: list[str], mapping: dict[str, int], change: AnalysisChange) -> list[str]:
    row: list[str] = [""] * len(header)
    _set_cell(row, mapping, "variable_name", change.variable_name or f"register_{change.register_address}")
    _set_cell(row, mapping, "data_type", change.data_type or "ushort")
    _set_cell(row, mapping, "register", change.register_address)
    _set_cell(row, mapping, "read_interval", change.read_interval or "")
    _set_cell(row, mapping, "documented_name", change.documented_name or f"Register_{change.register_address}")
    _set_cell(row, mapping, "values", change.values_range or "0-65535")
    _set_cell(row, mapping, "unit", change.unit or "")
    _set_cell(row, mapping, "adjustments", change.adjustments or "")
    _set_cell(row, mapping, "note", change.note or "Unknown")
    _set_cell(row, mapping, "write_mode", change.write_mode or "R")
    _log.debug(
        "Adding register %s (raw_value=%s) to %s registry",
        change.register_address,
        change.raw_value,
        change.registry_type,
    )
    return row


def _apply_protocol_changes(csv_path: Path, changes: list[AnalysisChange]) -> tuple[bool, int]:
    delimiter, header, rows = _load_csv_matrix(csv_path)
    mapping: dict[str, int] = _header_index_map(header)
    changed = 0

    for change in changes:
        action: str = change.action.lower()
        if action == "add":
            if _row_exists(rows, mapping, change):
                continue
            rows.append(_build_added_row(header, mapping, change))
            changed += 1
        elif action == "remove":
            original_len: int = len(rows)
            rows: list[list[str]] = [row for row in rows if not _row_matches_change(row, mapping, change)]
            changed += original_len - len(rows)

    if changed == 0:
        return False, 0

    _backup_protocol_csv(csv_path)
    with open(csv_path, "w", newline="", encoding="latin-1") as handle:
        writer: Writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)

    return True, changed


@router.get("/{device_name}/progress")
async def analysis_progress(device_name: str, request: Request):
    """
    SSE endpoint — relays scan progress events to the browser.

    The POST endpoint registers a queue in _progress_queues before starting
    analyze_protocols (which holds _transport_lock for the full scan).  This
    endpoint simply reads from that shared queue, so only one scan runs and
    the progress bar reflects the real scan rather than a competing one.

    Events emitted:
      data: {"type": "progress", "phase": "input"|"holding", "done": N, "total": N, "pct": 0-100}
      data: {"type": "done"}
      data: {"type": "error", "detail": "..."}
    """
    _require_modbus_transport(request, device_name)
    clean: str = _clean_device_name(device_name)

    async def event_stream() -> AsyncGenerator[str, None]:
        # Wait up to 10 s for the POST to register a queue.  The browser
        # opens this SSE connection before issuing the POST, so a short
        # wait is expected.
        progress_queue: "queue.Queue[ProgressQueueItem] | None" = None
        for _ in range(200):          # 200 × 50 ms = 10 s
            await asyncio.sleep(0.05)
            if await request.is_disconnected():
                return
            progress_queue = _get_progress_queue(clean)
            if progress_queue is not None:
                break

        if progress_queue is None:
            yield "data: " + json.dumps({"type": "error", "detail": "Analysis did not start in time."}) + "\n\n"
            return

        while True:
            if await request.is_disconnected():
                break

            msg: ProgressQueueItem | None = None
            for _ in range(20):       # poll up to 1 s in 50 ms ticks
                await asyncio.sleep(0.05)
                try:
                    msg = progress_queue.get_nowait()
                    break
                except queue.Empty:
                    continue

            if msg is None:
                yield ": keep-alive\n\n"
                continue

            if isinstance(msg, _ScanDoneSentinel):
                yield "data: " + json.dumps({"type": "done"}) + "\n\n"
                break

            yield "data: " + json.dumps(msg) + "\n\n"
            if msg.get("type") == "error":
                break

    headers: dict[str, str] = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


@router.post("/{device_name}")
async def run_analysis(device_name: str, payload: AnalyzeRequest, request: Request)-> dict[str, str | ProtocolAnalysisReport]:
    transport: modbus_base = _require_modbus_transport(request, device_name)
    protocol_names: list[str] = [name for name in payload.protocol_names if name]
    if not protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one protocol to analyze")

    clean: str = _clean_device_name(device_name)

    # Register the progress queue before entering the thread so the SSE
    # endpoint can find it immediately after the browser issues the POST.
    progress_queue: queue.Queue[ProgressMessage | _ScanDoneSentinel] = _register_progress_queue(clean)

    def progress_cb(phase: str, done: int, total: int) -> None:
        pct: int = round((done / total) * 100) if total else 0
        progress_queue.put({"type": "progress", "phase": phase, "done": done, "total": total, "pct": pct})

    try:
        result: ProtocolAnalysisReport = await asyncio.to_thread(
            transport.analyze_protocols,
            protocol_names,
            payload.current_protocol,
            progress_cb,
            payload.batch_size,
        )
    except Exception as exc:
        progress_queue.put({"type": "error", "detail": str(exc)})
        raise
    finally:
        # Signal the SSE stream that the scan is done regardless of outcome,
        # then remove the queue so the next run starts clean.
        progress_queue.put(_SCAN_DONE)
        _unregister_progress_queue(clean)

    return {"status": "ok", "result": result}


@router.post("/{device_name}/commit")
async def commit_analysis(device_name: str, payload: CommitAnalysisRequest, request: Request)-> dict[str, str | int | list[str]]:
    _require_modbus_transport(request, device_name)
    if not payload.changes:
        return {"status": "ok", "files_written": 0, "changes_applied": 0}

    protocols_dir = Path(request.app.state.protocols_dir)
    _log.info("Commit: protocols_dir=%s (exists=%s)", protocols_dir, protocols_dir.exists())
    grouped: dict[tuple[str, str], list[AnalysisChange]] = defaultdict(list)
    for change in payload.changes:
        grouped[(change.protocol_name, change.registry_type.lower())].append(change)

    files_written = 0
    changes_applied = 0
    touched_files: list[str] = []
    errors: list[str] = []
    for (protocol_name, registry_type), changes in grouped.items():
        _log.info(
            "Commit: protocol=%r registry=%r changes=%d actions=%s",
            protocol_name,
            registry_type,
            len(changes),
            [f"{c.action}:{c.register_address}" for c in changes],
        )
        try:
            csv_path: Path = _find_protocol_csv(protocols_dir, protocol_name, registry_type)
        except FileNotFoundError as exc:
            # No registry-map CSV exists yet for this protocol/registry type —
            # expected for a stub protocol (JSON descriptor only, no map).
            # If we're only being asked to remove rows, there's nothing to
            # do. If we're adding rows, create the map now: the standard
            # column header is fixed, and the registry type to create is
            # already given by the change itself, so this is safe to do
            # without any further input.
            if not any(change.action.lower() == "add" for change in changes):
                _log.error("Commit: %s", exc)
                errors.append(str(exc))
                continue
            try:
                csv_path = _create_protocol_csv(protocols_dir, protocol_name, registry_type)
            except OSError as create_exc:
                msg: str = f"Unable to create {registry_type} registry CSV for protocol '{protocol_name}': {create_exc}"
                _log.error("Commit: %s", msg)
                errors.append(msg)
                continue
        _log.info("Commit: writing to %s", csv_path)
        file_changed, file_change_count = _apply_protocol_changes(csv_path, changes)
        if file_changed:
            files_written += 1
            changes_applied += file_change_count
            touched_files.append(str(csv_path))
        else:
            _log.warning(
                "Commit: no rows changed in %s (rows may already exist or addresses didn't match)",
                csv_path,
            )

    if errors and not files_written:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    if files_written:
        request.app.state.scanner.run()

    _log.info(
        "Analysis commit for %s wrote %d file(s) and applied %d change(s)",
        device_name,
        files_written,
        changes_applied,
    )
    return {
        "status": "ok",
        "files_written": files_written,
        "changes_applied": changes_applied,
        "touched_files": touched_files,
    }
