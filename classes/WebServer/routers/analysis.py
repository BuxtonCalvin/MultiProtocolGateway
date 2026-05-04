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
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...transports.modbus_base import modbus_base

router = APIRouter(prefix="/api/analyze", tags=["analysis"])

_log: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level progress queue registry
# ---------------------------------------------------------------------------
# The POST endpoint creates a queue here before starting the analysis.
# The GET/SSE endpoint reads from it.  This lets both share a single scan
# that runs inside analyze_protocols (under _transport_lock) rather than
# running two competing scans — which was why the progress bar froze.
_SCAN_DONE = object()
_progress_queues: dict[str, queue.Queue] = {}
_progress_queues_lock = threading.Lock()


def _register_progress_queue(device_name: str) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _progress_queues_lock:
        _progress_queues[device_name] = q
    return q


def _get_progress_queue(device_name: str) -> queue.Queue | None:
    with _progress_queues_lock:
        return _progress_queues.get(device_name)


def _unregister_progress_queue(device_name: str) -> None:
    with _progress_queues_lock:
        _progress_queues.pop(device_name, None)


class AnalyzeRequest(BaseModel):
    protocol_names: list[str] = Field(default_factory=list)
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
    note: str = ""


class CommitAnalysisRequest(BaseModel):
    changes: list[AnalysisChange] = Field(default_factory=list)


def _clean_device_name(device_name: str) -> str:
    """
    Strip any surrounding quotation marks that can appear when a Jinja
    tojson value is double-serialized through a URL parameter.
    e.g. '"Inverter1"' -> 'Inverter1'
    """
    return device_name.strip().strip("'\"")


def _find_transport(gateway: Any, device_name: str) -> None | Any:
    if gateway is None:
        return None
    clean = _clean_device_name(device_name)
    transport_names: set[str] = {clean, f"transport.{clean}"}
    return next(
        (
            t for t in getattr(gateway, "_Protocol_Gateway__transports", [])
            if t.transport_name in transport_names
        ),
        None,
    )


def _require_modbus_transport(request: Request, device_name: str) -> modbus_base:
    gateway = getattr(request.app.state, "gateway", None)
    transport = _find_transport(gateway, device_name)
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
    sample = csv_path.read_text(encoding="latin-1")[:4096]
    return ";" if sample.count(";") > sample.count(",") else ","


def _load_csv_matrix(csv_path: Path) -> tuple[str, list[str], list[list[str]]]:
    delimiter = _detect_delimiter(csv_path)
    with open(csv_path, newline="", encoding="latin-1") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        rows = list(reader)
    if not rows:
        msg: str = (f"CSV file is empty: {csv_path}")
        raise ValueError(msg)
    header = list(rows[0])
    body = [row + [""] * (len(header) - len(row)) for row in rows[1:]]
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


def _find_protocol_csv(protocols_dir: Path, protocol_name: str, registry_type: str) -> Path:
    registry_type = registry_type.lower()
    candidates = [
        path for path in protocols_dir.rglob("*.csv")
        if path.stem.lower().startswith(protocol_name.lower())
        and "registry_map" in path.name.lower()
        and registry_type in path.name.lower()
    ]
    if not candidates:
        msg: str = (f"Unable to locate {registry_type} registry CSV for protocol '{protocol_name}'")
        _log.debug(msg)
        raise FileNotFoundError(msg)
    candidates.sort()
    return candidates[0]


def _backup_protocol_csv(csv_path: Path) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_path = csv_path.with_suffix(f"{csv_path.suffix}.bak.{timestamp}")
    shutil.copy2(csv_path, backup_path)


def _row_matches_change(row: list[str], mapping: dict[str, int], change: AnalysisChange) -> bool:
    row_variable = _get_cell(row, mapping, "variable_name").lower()
    row_documented = _get_cell(row, mapping, "documented_name").lower()
    row_register = _get_cell(row, mapping, "register").lower()
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
    row = [""] * len(header)
    _set_cell(row, mapping, "variable_name", change.variable_name or f"register_{change.register_address}")
    _set_cell(row, mapping, "data_type", change.data_type or "ushort")
    _set_cell(row, mapping, "register", change.register_address)
    _set_cell(row, mapping, "read_interval", change.read_interval or "")
    _set_cell(row, mapping, "documented_name", change.documented_name or f"Register_{change.register_address}")
    _set_cell(row, mapping, "values", change.values_range or "0-65535")
    _set_cell(row, mapping, "unit", change.unit or "")
    _set_cell(row, mapping, "note", change.note or "")
    _set_cell(row, mapping, "write_mode", change.write_mode or "R")
    return row


def _apply_protocol_changes(csv_path: Path, changes: list[AnalysisChange]) -> tuple[bool, int]:
    delimiter, header, rows = _load_csv_matrix(csv_path)
    mapping = _header_index_map(header)
    changed = 0

    for change in changes:
        action = change.action.lower()
        if action == "add":
            if _row_exists(rows, mapping, change):
                continue
            rows.append(_build_added_row(header, mapping, change))
            changed += 1
        elif action == "remove":
            original_len = len(rows)
            rows = [row for row in rows if not _row_matches_change(row, mapping, change)]
            changed += original_len - len(rows)

    if changed == 0:
        return False, 0

    _backup_protocol_csv(csv_path)
    with open(csv_path, "w", newline="", encoding="latin-1") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
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
    clean = _clean_device_name(device_name)

    async def event_stream():
        # Wait up to 10 s for the POST to register a queue.  The browser
        # opens this SSE connection before issuing the POST, so a short
        # wait is expected.
        progress_queue: queue.Queue | None = None
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

            msg = None
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

            if msg is _SCAN_DONE:
                yield "data: " + json.dumps({"type": "done"}) + "\n\n"
                break

            yield "data: " + json.dumps(msg) + "\n\n"
            if msg.get("type") == "error":
                break

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


@router.post("/{device_name}")
async def run_analysis(device_name: str, payload: AnalyzeRequest, request: Request):
    transport = _require_modbus_transport(request, device_name)
    protocol_names = [name for name in payload.protocol_names if name]
    if not protocol_names:
        raise HTTPException(status_code=400, detail="Select at least one protocol to analyze")

    clean = _clean_device_name(device_name)

    # Register the progress queue BEFORE entering the thread so the SSE
    # endpoint can find it immediately after the browser issues the POST.
    progress_queue = _register_progress_queue(clean)

    def progress_cb(phase: str, done: int, total: int) -> None:
        pct = round((done / total) * 100) if total else 0
        progress_queue.put({"type": "progress", "phase": phase, "done": done, "total": total, "pct": pct})

    try:
        result = await asyncio.to_thread(
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
async def commit_analysis(device_name: str, payload: CommitAnalysisRequest, request: Request):
    _require_modbus_transport(request, device_name)
    if not payload.changes:
        return {"status": "ok", "files_written": 0, "changes_applied": 0}

    protocols_dir = request.app.state.protocols_dir
    grouped: dict[tuple[str, str], list[AnalysisChange]] = defaultdict(list)
    for change in payload.changes:
        grouped[(change.protocol_name, change.registry_type.lower())].append(change)

    files_written = 0
    changes_applied = 0
    touched_files: list[str] = []
    for (protocol_name, registry_type), changes in grouped.items():
        csv_path = _find_protocol_csv(protocols_dir, protocol_name, registry_type)
        file_changed, file_change_count = _apply_protocol_changes(csv_path, changes)
        if file_changed:
            files_written += 1
            changes_applied += file_change_count
            touched_files.append(str(csv_path))

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
