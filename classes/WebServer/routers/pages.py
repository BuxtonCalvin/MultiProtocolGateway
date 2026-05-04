"""
routers/pages.py — Page routes and supporting API endpoints.
Registered in main.py via app.include_router(pages_router).

TemplateResponse convention used throughout this module:
    request.app.state.templates.TemplateResponse(
        request=request,
        name="template/path.html",
        context={"key": value, ...},
    )

Session scope convention:
    Data is fetched inside `with session_scope() as db:` and the session
    is closed before TemplateResponse is constructed — ORM objects must
    be fully loaded (no lazy-load) before the session closes.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...transports.modbus_base import modbus_base
from ..config_writer import create_backup
from ..database import get_session, refresh_app_state, session_scope
from ..models import ProtocolRegister, Setting
from ..scanner import TRANSPORT_BASE_KEYS, _load_config, scan_transport_library
from ..services.device_service import (
    DeviceSummary,
    NavData,
    get_device_summary,
    get_nav_data,
)
from ..services.protocol_service import get_protocol_groups, get_protocol_json

router = APIRouter(tags=["pages"])
_log: logging.Logger = logging.getLogger(__name__)


def _base_context(request: Request, nav: NavData) -> dict[str, Any]:
    return {
        "nav": nav,
        "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
    }


def _analysis_protocol_options(protocol_groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for group in protocol_groups:
        group_name = str(group.get("group", ""))
        if group_name.startswith("_"):
            continue
        for protocol_name in group.get("protocols", []):
            protocol_name = str(protocol_name)
            lower_name = protocol_name.lower()
            if protocol_name.startswith("_"):
                continue
            if "registry" in lower_name or "debug" in lower_name:
                continue
            options.append({
                "group": group_name,
                "name": protocol_name,
            })
    return options


# ---------------------------------------------------------------------------
# Global Settings pages
# ---------------------------------------------------------------------------

@router.get("/pages/global-settings", response_class=HTMLResponse, response_model=None)
async def global_settings_page(request: Request):
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
        settings: List[Setting] = (
            db.query(Setting)
            .filter(
                Setting.section == "general",
                Setting.key != "log_level",
            )
            .order_by(Setting.key)
            .all()
        )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/global_settings.html",
        context={**_base_context(request, nav), "settings": settings},
    )


@router.get("/pages/logging-settings", response_class=HTMLResponse, response_model=None)
async def logging_settings_page(request: Request):
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
        settings: List[Setting] = (
            db.query(Setting)
            .filter_by(section="logging")
            .order_by(Setting.key)
            .all()
        )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/logging_settings.html",
        context={**_base_context(request, nav), "settings": settings},
    )


@router.get("/pages/view-log", response_class=HTMLResponse, response_model=None)
async def view_log_page(request: Request):
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/view_log.html",
        context=_base_context(request, nav),
    )


@router.get("/pages/transport-library", response_class=HTMLResponse, response_model=None)
async def transport_library_page(request: Request):
    from ..services.device_service import get_transport_library
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    library: List[dict[str, Any]] = get_transport_library(
        request.app.state.transports_dir
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/transport_library.html",
        context={**_base_context(request, nav), "library": library},
    )


@router.get("/pages/faq", response_class=HTMLResponse, response_model=None)
async def faq_page(request: Request):
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/faq.html",
        context=_base_context(request, nav),
    )


@router.get("/pages/about", response_class=HTMLResponse, response_model=None)
async def about_page(request: Request):
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/about.html",
        context=_base_context(request, nav),
    )


@router.get("/pages/create-device", response_class=HTMLResponse, response_model=None)
async def create_device_page(request: Request):
    with session_scope() as db:
        nav: NavData = get_nav_data(db)
    proto_groups: List[dict[str, Any]] = get_protocol_groups(
        request.app.state.protocols_dir
    )
    transport_library = scan_transport_library(request.app.state.transports_dir)
    create_device_data = {
        "scrapers": [
            {
                "name": name,
                "keys": info.get("keys", {}),
            }
            for name, info in sorted(transport_library.items())
            if info.get("classification") == "scraper"
        ],
        "bridges": [
            {
                "name": name,
                "section": f"transport.{name}",
            }
            for name, info in sorted(transport_library.items())
            if info.get("classification") == "bridge"
        ],
        "shared_keys": sorted(
            key for key in TRANSPORT_BASE_KEYS.keys()
            if key not in {"transport", "bridge", "protocol_version", "log_level"}
        ),
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/create_device.html",
        context={
            "nav": nav,
            "proto_groups": proto_groups,
            "create_device_data": create_device_data,
        },
    )


@router.get("/pages/analyze/{device_name}", response_class=HTMLResponse, response_model=None)
async def analyze_device_page(request: Request, device_name: str):
    gateway = getattr(request.app.state, "gateway", None)
    transport = None
    if gateway is not None:
        transports = getattr(gateway, "_Protocol_Gateway__transports", [])
        transport = next(
            (
                t for t in transports
                if t.transport_name in (device_name, f"transport.{device_name}")
            ),
            None,
        )

    with session_scope() as db:
        nav: NavData = get_nav_data(db)
        device: DeviceSummary | None = get_device_summary(db, device_name)

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")
    if device.transport_type != "scraper":
        raise HTTPException(status_code=400, detail="Analyze is only available for scrapers")
    if transport is None or not isinstance(transport, modbus_base):
        raise HTTPException(
            status_code=400,
            detail="Analyze is only available for Modbus-based scrapers",
        )

    proto_groups: list[dict[str, Any]] = get_protocol_groups(request.app.state.protocols_dir)
    protocol_options: List[dict[str, str]] = _analysis_protocol_options(proto_groups)
    current_protocol: str = device.protocol_version or ""

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="pages/analyze_device.html",
        context={
            "nav": nav,
            "proto_groups": proto_groups,
            "device": device,
            "analysis_protocols": protocol_options,
            "current_protocol": current_protocol,
        },
    )


# ---------------------------------------------------------------------------
# Protocol editor
# ---------------------------------------------------------------------------

@router.get("/protocol-editor/{protocol_group}/{protocol_name}",
            response_class=HTMLResponse, response_model=None)
async def protocol_editor(
    request: Request,
    protocol_group: str,
    protocol_name: str,
):
    """
    Standalone protocol viewer/editor reached from the Protocols nav menu.
    Shows the register table for a CSV/JSON file independent of any device.

    Responds with a partial (HTMX swap) or a full page depending on whether
    the HX-Request header is present.
    """
    protocols_dir: Path = request.app.state.protocols_dir

    with session_scope() as db:
        nav: NavData = get_nav_data(db)

        rows = (
            db.execute(
                select(
                    ProtocolRegister.protocol_name,
                    ProtocolRegister.registry_type,
                    func.count(ProtocolRegister.id).label("total"),
                )
                .where(ProtocolRegister.protocol_name == protocol_name)
                .group_by(
                    ProtocolRegister.protocol_name,
                    ProtocolRegister.registry_type,
                )
                .order_by(ProtocolRegister.registry_type)
            )
            .all()
        )

    proto_tabs: List[dict[str, Any]] = [
        {
            "protocol_name": r[0],
            "registry_type": r[1],
            "total":         r[2],
        }
        for r in rows
    ]

    json_data_raw, _is_override = get_protocol_json(
        protocols_dir, protocol_group, protocol_name
    )
    json_data: dict[str, Any] = json_data_raw or {}

    csv_path: str | None = None
    candidate: Path = protocols_dir / protocol_group / f"{protocol_name}.csv"
    if candidate.exists():
        csv_path = str(candidate)

    proto_groups: List[dict[str, Any]] = get_protocol_groups(protocols_dir)

    context: dict[str, Any] = {
        "nav":            nav,
        "proto_groups":   proto_groups,
        "protocol_group": protocol_group,
        "protocol_name":  protocol_name,
        "proto_tabs":     proto_tabs,
        "json_data":      json_data,
        "csv_path":       csv_path,
        "app_state":      None,
    }

    template_name = (
        "partials/protocol_editor_panes.html"
        if request.headers.get("HX-Request")
        else "protocol_editor.html"
    )

    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context,
    )


# ---------------------------------------------------------------------------
# Log reader API
# ---------------------------------------------------------------------------

@router.get("/api/log", response_class=PlainTextResponse)
async def read_log(request: Request, lines: int = 250):
    # defaults to 250 lines. The UI allows up to 1000, but we set a reasonable default to prevent
    # accidentally trying to read a huge log file when just opening the page. The endpoint can still be used to read more
    # lines if needed.

    # Get strings from state
    log_file: str = request.app.state.log_file
    log_dir: str = request.app.state.log_dir
    project_root: Path = request.app.state.project_root

    # Path discovery (Check parent and current root)
    log_path = None
    for base in [project_root.parent, project_root]:
        candidate = base / log_dir / log_file
        if candidate.exists():
            log_path = candidate
            break

    if not log_path:
        return PlainTextResponse("Log file not found.", status_code=404)

    # Efficient Tail
    try:
        # We read in binary mode to seek accurately from the end
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size: int = f.tell()

            # Start by looking at the last 256KB (plenty for 1000 lines)
            # which is the max allowed by the UI.
            # This avoids loading a multi-gig file into memory
            buffer_size: int = min(file_size, 262144)
            f.seek(file_size - buffer_size)

            # Decode only what we need
            chunk = f.read(buffer_size).decode("utf-8", errors="replace")
            log_lines = chunk.splitlines()

            # Handle the case where the first line might be partial
            if len(log_lines) > lines:
                result: str = "\n".join(log_lines[-lines:])
            else:
                result = "\n".join(log_lines)

            return PlainTextResponse(result)

    except Exception as exc:
        return PlainTextResponse(f"Error: {exc}", status_code=500)

# ---------------------------------------------------------------------------
# Create Device API
# ---------------------------------------------------------------------------

FIXED_CREATE_KEYS: tuple[str, ...] = (
    "transport",
    "bridge",
    "protocol_version",
    "log_level",
    "transport_type_cached",
)
LOG_LEVELS: tuple[str, ...] = (
    "CRITICAL",
    "FATAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
    "EXCEPTION",
)


class CreateDeviceSettingInput(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    value: str = ""
    is_active: bool = True

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError("Setting keys must be alphanumeric or underscore.")
        return value


class CreateDeviceRequest(BaseModel):
    device_name: str = Field(..., pattern=r"^[a-zA-Z0-9_]+$", min_length=1, max_length=64)
    scraper_transport: str
    bridge: str
    protocol_version: str = ""
    log_level: str = "INFO"
    settings: list[CreateDeviceSettingInput] = []

    @field_validator("bridge")
    @classmethod
    def validate_bridge(cls, value: str) -> str:
        if not value.startswith("transport."):
            raise ValueError("Bridge must be saved as a transport section reference.")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in LOG_LEVELS:
            msg: str = (f"Unknown log level '{value}'.")
            raise ValueError(msg)
        return upper


def _append_section_to_config(config_path: Path, section: str, fields: list[tuple[str, str]]) -> None:
    section_text = "\n".join(
        [f"[{section}]", *[f"{key} = {value}" for key, value in fields]]
    ) + "\n"

    existing_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if existing_text:
        separator = ""
        if not existing_text.endswith("\n"):
            separator = "\n"
        if not existing_text.endswith("\n\n"):
            separator += "\n"
        config_path.write_text(existing_text + separator + section_text, encoding="utf-8")
    else:
        config_path.write_text(section_text, encoding="utf-8")


@router.post("/api/devices/create")
def create_device(
    request: Request,
    payload: CreateDeviceRequest,
    db: Session = Depends(get_session),
):
    """
    Create a new device directly in config.cfg, then re-scan so the staging
    database reflects the new on-disk section without disturbing existing rows.
    """
    section = f"transport.{payload.device_name}"
    config_path: Path = request.app.state.config_path
    config_data = _load_config(config_path)
    if section in config_data or db.query(Setting).filter_by(section=section).first():
        raise HTTPException(status_code=409, detail=f"Device '{payload.device_name}' already exists.")

    library = scan_transport_library(request.app.state.transports_dir)
    scraper_info = library.get(payload.scraper_transport)
    if not scraper_info or scraper_info.get("classification") != "scraper":
        raise HTTPException(status_code=400, detail="Selected scraper transport is not valid.")

    bridge_name = payload.bridge.removeprefix("transport.")
    bridge_info = library.get(bridge_name)
    if not bridge_info or bridge_info.get("classification") != "bridge":
        raise HTTPException(status_code=400, detail="Selected bridge is not valid.")

    allowed_keys = {
        key for key in scraper_info.get("keys", {}).keys()
        if key not in FIXED_CREATE_KEYS
    }

    seen_keys: set[str] = set()
    ordered_fields: list[tuple[str, str]] = [
        ("transport", payload.scraper_transport),
        ("bridge", payload.bridge),
        ("protocol_version", payload.protocol_version),
        ("log_level", payload.log_level),
    ]

    for item in payload.settings:
        if not item.is_active:
            continue
        if item.key in FIXED_CREATE_KEYS:
            continue
        if item.key not in allowed_keys:
            raise HTTPException(status_code=400, detail=f"Unknown setting key '{item.key}' for selected scraper.")
        if item.key in seen_keys:
            continue
        seen_keys.add(item.key)
        ordered_fields.append((item.key, item.value))

    try:
        create_backup(config_path, db, trigger="create_device")
        db.commit()
        _append_section_to_config(config_path, section, ordered_fields)
        request.app.state.scanner.set_cfg_is_truth(True)
        request.app.state.scanner.run(db)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    refresh_app_state(db)
    db.commit()

    _log.info("Device '%s' appended to config.cfg", payload.device_name)
    return {
        "status": "created",
        "device_name": payload.device_name,
        "section": section,
        "keys_added": len(ordered_fields),
    }
