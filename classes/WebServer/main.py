# Description: main.py — FastAPI application for the MPG Web Management UI.
# File: main.py
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
main.py — FastAPI application for the MPG Web Management UI.

Entry point called from protocol_gateway.py:

    from classes.WebServer.main import start_webserver
    start_webserver(config_file=config_path, gateway_instance=mpg)

The server runs on a daemon thread (port 1717) and shuts down automatically
when the gateway process exits.

config_path passed from protocol_gateway.main() is the fully-resolved Path to
the config file (e.g. <project_root>/config/config.cfg).  start_webserver()
derives project_root by walking up until pyproject.toml is found, matching
the same logic used in protocol_gateway.main().
"""

from __future__ import annotations

import asyncio
import hashlib as _hashlib
import json as _json
import logging
import logging.handlers
import queue as _queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, List, Tuple

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Row
from sqlalchemy.engine import Engine

from classes.WebServer.diff_engine import DiffResult
from classes.WebServer.models import (
    AppState,
    Base,
    ConfigBackup,
    ProtocolRegister,
    Setting,
    SettingDescription,
)

from ..transports.modbus_base import modbus_base
from .database import ensure_app_state, init_db, run_migrations, session_scope
from .diff_engine import build_diff
from .file_watcher import FileWatcher
from .routers.analysis import router as analysis_router
from .routers.commit import router as commit_router
from .routers.devices import get_app_state
from .routers.devices import router as devices_router
from .routers.help import FileResponse
from .routers.help import router as help_router
from .routers.pages import router as pages_router
from .routers.protocols import router as protocols_router
from .routers.transport_settings import router as transport_settings_router
from .scanner import Scanner, scan_transport_library
from .services.analysis_service import get_transport_connection_status
from .services.device_service import (
    DeviceSummary,
    NavData,
    get_device_settings,
    get_device_summary,
    get_nav_data,
    get_orphaned_settings,
    get_transport_library,
)
from .services.protocol_service import (
    build_synthetic_rows,
    get_protocol_groups,
    get_protocol_json,
    get_protocol_registers,
    get_protocols_for_device,
)
from .services.setting_description_service import (
    get_all_setting_descriptions,
    seed_setting_descriptions,
)

_log: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging — attach a dedicated rotating file handler to the "classes.WebServer"
# logger subtree only.  We never touch the root logger or the gateway's
# handlers.  All webserver loggers inherit from "classes.WebServer.*" so they
# naturally pick up this handler without stealing anything from the gateway.
#
# A QueueHandler + QueueListener pair is still used so the uvicorn event-loop
# thread never blocks on file I/O (the listener drains on its own thread).
# ---------------------------------------------------------------------------

_queue_listener: logging.handlers.QueueListener | None = None


def _install_webserver_logging() -> None:
    """
    Reroutes WebServer and Uvicorn logs into the central Gateway _log.
    Uses a QueueListener to prevent Windows file-locking (IO) contention.
    """
    global _queue_listener

    ws_logger: logging.Logger = logging.getLogger("classes.WebServer")

    # Idempotency check: don't re-install if QueueHandler is present
    if any(isinstance(h, logging.handlers.QueueHandler) for h in ws_logger.handlers):
        return

    # ── Identify Central Handlers ──────────────────────────────────────────
    # Grab the handlers already established on the root logger by the gateway
    root_handlers: List[logging.Handler] = logging.getLogger().handlers

    if not root_handlers:
        # If the gateway hasn't initialized logging yet, we allow
        # propagation so logs aren't lost, then exit.
        ws_logger.propagate = True
        return

    # ── Non-Blocking Queue Setup ───────────────────────────────────────────
    # This prevents the Uvicorn loop from hanging on File I/O and
    # bypasses Windows "file in use" errors by serializing writes.
    log_queue: _queue.SimpleQueue = _queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(log_queue)

    # The listener picks up items from the queue and feeds them
    # into the existing root handlers (File, Console, etc.)
    listener = logging.handlers.QueueListener(
        log_queue,
        *root_handlers,
        respect_handler_level=True
    )
    listener.start()
    _queue_listener = listener

    # ── Configuration ──────────────────────────────────────────────────────
    ws_logger.addHandler(queue_handler)
    ws_logger.setLevel(logging.DEBUG)

    # Important: False prevents double-logging (Queue -> Root -> File)
    ws_logger.propagate = False

    # Redirect 3rd party libraries to use the same non-blocking queue
    intercept_loggers = (
        "uvicorn", "uvicorn.access", "uvicorn.error",
        "fastapi", "alembic", "sqlalchemy.engine"
    )

    for name in intercept_loggers:
        logs: logging.Logger = logging.getLogger(name)
        # Remove any default handlers to ensure they only use our queue
        for hands in logs.handlers[:]:
            logs.removeHandler(hands)
        logs.addHandler(queue_handler)
        logs.propagate = False

    ws_logger.info("WebServer logging merged into central log via QueueListener.")


# ---------------------------------------------------------------------------
# Module-level path constants — resolved relative to this file
# (classes/WebServer/main.py → classes/WebServer/)
# ---------------------------------------------------------------------------
_WEB_DIR:      Path = Path(__file__).resolve().parent
_TEMPLATES_DIR: Path = _WEB_DIR / "templates"
_STATIC_DIR:   Path = _WEB_DIR / "static"
_ALEMBIC_INI:  Path = _WEB_DIR / "alembic.ini"


# ---------------------------------------------------------------------------
# Uvicorn server subclass — disables signal handling so the background
# thread does not conflict with the gateway's own signal handlers.
# ---------------------------------------------------------------------------

class NoSignalServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        """No-op: background threads cannot install signal handlers."""
        pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    config_path: Path,
    log_file: str,
    log_dir: str,
    project_root: Path,
    config_dir: Path | None = None,
    gateway_instance: Any = None,

) -> FastAPI:
    """
    Build and return the FastAPI application.

    config_path   — fully-resolved path to config.cfg
    project_root  — root of MultiProtocolGateway (contains protocols/, classes/)
    """
    db_dir: Path = config_dir / "data-db" if config_dir else project_root / "config" / "data-db"

    protocols_dir: Path = project_root / "protocols"
    if not protocols_dir.exists():
        _log.warning(f"Protocols directory missing at {protocols_dir}")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ---- Startup ----
        _install_webserver_logging()
        _log.info("MPG WebServer starting up...")


        # init_db is fast (in-memory setup only) — safe to run inline.
        engine: Engine = init_db(db_dir / "mpg_staging.db")

        def _startup_io() -> Scanner:
            run_migrations(db_dir / "mpg_staging.db", _ALEMBIC_INI)
            Base.metadata.create_all(bind=engine)
            with session_scope() as db:
                ensure_app_state(db)
            s = Scanner(config_path, project_root)

            # If config.cfg differs from the most recent backup, someone edited
            # it manually (or it was rolled back externally). Treat cfg as truth
            # so value_staged syncs to value_disk and no stale staged edits remain.
            try:

                cfg_hash: str = _hashlib.md5(config_path.read_bytes()).hexdigest()  # noqa: S324
                with session_scope() as _db:
                    latest: ConfigBackup | None = _db.query(ConfigBackup).order_by(
                        ConfigBackup.created_at.desc()
                    ).first()
                    if latest:
                        latest_path = Path(latest.filepath)
                        if latest_path.exists():
                            backup_hash: str = _hashlib.md5(latest_path.read_bytes()).hexdigest()  # noqa: S324
                            if cfg_hash != backup_hash:
                                _log.info(
                                    "config.cfg differs from last backup — "
                                    "treating cfg as ground truth for this scan"
                                )
                                s.set_cfg_is_truth(True)
            except Exception as _exc:
                _log.debug("Startup cfg-truth check skipped: %s", _exc)

            s.run()
            return s

        try:
            loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="MPGWebInit") as executor:
                scanner: Scanner = await loop.run_in_executor(executor, _startup_io)

        except Exception as exc:
            msg: str = f"Startup I/O failed (server still starting): {exc}"
            _log.error(msg)
            scanner = Scanner(config_path, project_root)

        _log.info(f"Database {engine.url} initialized and migrations applied.")

        # State management
        app.state.config_path    = config_path
        app.state.project_root   = project_root
        app.state.protocols_dir  = protocols_dir
        app.state.transports_dir = project_root / "classes" / "transports"
        app.state.config_dir = config_dir or config_path.parent
        app.state.log_file = log_file
        app.state.log_dir = log_dir
        app.state.db_dir = db_dir

        # Seed/update the setting_descriptions table on every startup
        with session_scope() as db:
            n, _ = seed_setting_descriptions(db, app.state.transports_dir)
            if n:
                _log.info("Setting descriptions: %d rows seeded/updated", n)
        app.state.gateway        = gateway_instance
        app.state.scanner        = scanner

        watcher: FileWatcher = FileWatcher(scanner, config_path, protocols_dir)
        watcher.start()
        app.state.file_watcher = watcher

        _log.info(f"MPG WebServer ready on http://0.0.0.0:{_current_port}")

        yield  # -----------------------------------------------------------

        # ---- Shutdown ----
        _log.info("MPG WebServer shutting down...")
        if hasattr(app.state, "file_watcher"):
            app.state.file_watcher.stop()
        if _queue_listener is not None:
            _queue_listener.stop()

    app = FastAPI(
        title="MPG Web Management UI",
        description="Protocol Gateway Configuration & Monitoring",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Static files
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # Templates
    templates: Jinja2Templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------

    app.include_router(devices_router)
    app.include_router(transport_settings_router)
    app.include_router(protocols_router)
    app.include_router(commit_router)
    app.include_router(analysis_router)
    app.include_router(help_router)
    app.include_router(pages_router)

    # ------------------------------------------------------------------
    # Core routes
    # /pages/* are in routers/pages.py (registered above).
    # Routes below need access to the `templates` closure.
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, response_model=None)
    async def dashboard(request: Request):
        protocols_dir_: Path = getattr(
            request.app.state, "protocols_dir", project_root / "protocols"
        )
        with session_scope() as db:
            nav: NavData            = get_nav_data(db)
            state: AppState                   = get_app_state(db)

        proto_groups: List[dict[str, Any]] = get_protocol_groups(protocols_dir_)

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "nav":          nav,
                "app_state":    state,
                "proto_groups": proto_groups,
            },
        )

    @app.get("/device/{device_name}", response_class=HTMLResponse, response_model=None)
    async def device_page(request: Request, device_name: str):
        section: str = f"transport.{device_name}"

        with session_scope() as db:
            nav:      NavData                  = get_nav_data(db)
            summary:  DeviceSummary | None     = get_device_summary(db, device_name)
            all_settings: List[Setting]        = get_device_settings(db, section)

            # For bridge devices, filter displayed settings to only the keys
            # the bridge module actually reads (AST-scanned keys only).
            # This prevents scraper base keys (protocol_version, read_interval,
            # variable_mask, etc.) from appearing in the bridge settings pane.
            if summary and summary.transport_type == "bridge":
                library: dict[str, dict[str, Any]] = scan_transport_library(request.app.state.transports_dir)
                bridge_info: dict[str, Any] = library.get(summary.transport_class, {})
                bridge_keys: set[Any] = set(bridge_info.get("keys", {}).keys())
                # Always keep log_level as it's shown in a dedicated dropdown
                bridge_keys.add("log_level")
                settings: List[Setting] = [
                    s for s in all_settings if s.key in bridge_keys
                ] if bridge_keys else all_settings
            else:
                settings = all_settings
            proto_tabs: List[dict[str, str]]   = (
                get_protocols_for_device(db, summary.protocol_version, device_name=device_name)
                if summary and summary.protocol_version
                else []
            )
            # Pre-compute whether any M/S/W selection exists across all tabs so
            # protocol_section.html can show "No chosen metrics" without Jinja sum.
            has_no_selections: bool = not any(
                t.get("mask_count", 0) or t.get("screen_count", 0) or t.get("write_count", 0)
                for t in proto_tabs
            ) if proto_tabs else False
            protocol_match = None
            if summary is None:
                protocol_match: Row[Tuple[str, str]] | None = (
                    db.query(ProtocolRegister.protocol_group, ProtocolRegister.protocol_name)
                    .filter(ProtocolRegister.protocol_name == device_name)
                    .first()
                )

        if summary is None:
            if protocol_match:
                return RedirectResponse(
                    url=f"/protocol-editor/{protocol_match[0]}/{protocol_match[1]}",
                    status_code=307,
                )
            return HTMLResponse("<p>Device not found.</p>", status_code=404)

        proto_groups: List[dict[str, Any]] = get_protocol_groups(
            request.app.state.protocols_dir
        )

        partial_template_name: str = (
            "partials/scraper_panes.html"
            if summary.transport_type == "scraper"
            else "partials/bridge_panes.html"
        )

        template_name = (
            partial_template_name
            if request.headers.get("HX-Request")
            else "device.html"
        )

        # Populate live connection status from the gateway instance
        gateway = getattr(request.app.state, "gateway", None)
        analyze_enabled = False
        if gateway is not None:
            conn_status: dict[str, bool] = get_transport_connection_status(gateway)
            # Gateway uses section name (e.g. "transport.mqtt") as transport_name
            summary.is_connected = conn_status.get(
                summary.section,                          # try "transport.mqtt"
                conn_status.get(summary.name, False)      # fall back to "mqtt"
            )
            live_transport = next(
                (
                    t for t in getattr(gateway, "_Protocol_Gateway__transports", [])
                    if t.transport_name in (summary.name, summary.section)
                ),
                None,
            )
            analyze_enabled = bool(
                summary.transport_type == "scraper"
                and isinstance(live_transport, modbus_base)
            )

        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={
                "nav":          nav,
                "device":       summary,
                "settings":     settings,
                "proto_tabs":   proto_tabs,
                "has_no_selections": has_no_selections,
                "proto_groups": proto_groups,
                "transport_library": get_transport_library(request.app.state.transports_dir),
                "device_partial_template": partial_template_name,
                "analyze_enabled": analyze_enabled,
            },
        )

    BASE_WEB_DIR: Path = Path(__file__).resolve().parent
    @app.get('/favicon.ico', include_in_schema=False)
    async def favicon() -> FileResponse:

        favicon_path: Path = BASE_WEB_DIR / "static" / "favicon.ico"

        return FileResponse(favicon_path)

    @app.get("/api/device/{device_name}/last-values")
    async def device_last_values(request: Request, device_name: str) -> JSONResponse:
        """Return the last bridge-confirmed scrape values for a device transport.

        Values come from ``_last_known_data`` which is populated in
        ``protocol_gateway._snapshot_scraper_data`` immediately before each
        ``bridge.write_data()`` call — the authoritative point where a cycle
        is confirmed complete and bridge-bound.
        """
        gateway = getattr(request.app.state, "gateway", None)
        if gateway is None:
            return JSONResponse({"values": {}, "status": "no_gateway"})
        transport = gateway.get_transport(f"transport.{device_name}")
        if transport is None:
            return JSONResponse({"values": {}, "status": "not_found"})

        raw: dict = getattr(transport, "_last_known_data", {})
        clean: dict[str, str] = {}
        for k, v in raw.items():
            if k.endswith("_desc"):
                continue
            try:
                clean[k] = str(round(v, 4)) if isinstance(v, float) else str(v)
            except Exception as e :
                _log.debug(f"error retrieving _last_known_data {e}")
                pass

        return JSONResponse({"values": clean, "status": "ok"})

    @app.get("/api/device/{device_name}/last-values/wait")
    async def device_last_values_wait(request: Request, device_name: str) -> JSONResponse:
        """Block until the next scrape cycle completes, then return its values.

        The refresh button calls this endpoint.  It waits on
        ``transport._values_ready_event`` which is set (then immediately
        cleared) in ``_snapshot_scraper_data`` each time a cycle's data is
        forwarded to a bridge.  The client therefore receives the values from
        the next complete cycle rather than a cached stale snapshot.

        Times out after ``timeout`` seconds (default 90 — enough for even a
        slow polling interval plus retries) and returns ``status: timeout``
        so the client can show an appropriate message.
        """
        import asyncio
        timeout: float = 90.0
        gateway = getattr(request.app.state, "gateway", None)
        if gateway is None:
            return JSONResponse({"values": {}, "status": "no_gateway"})
        transport = gateway.get_transport(f"transport.{device_name}")
        if transport is None:
            return JSONResponse({"values": {}, "status": "not_found"})

        event: threading.Event = getattr(transport, "_values_ready_event", threading.Event())
        if event is None:
            return JSONResponse({"values": {}, "status": "no_event"})

        # Run the blocking wait() in a thread pool so we don't block the
        # async event loop.  asyncio.to_thread requires Python 3.9+.
        fired: bool = await asyncio.to_thread(event.wait, timeout)
        if not fired:
            return JSONResponse({"values": {}, "status": "timeout"})

        raw: dict = getattr(transport, "_last_known_data", {})
        clean: dict[str, str] = {}
        for k, v in raw.items():
            if k.endswith("_desc"):
                continue
            try:
                clean[k] = str(round(v, 4)) if isinstance(v, float) else str(v)
            except Exception as e :
                _log.debug(f"error retrieving _last_known_data {e}")
                pass

        return JSONResponse({"values": clean, "status": "ok"})

    @app.get("/protocol/{protocol_name}/{registry_type}", response_class=HTMLResponse, response_model=None)
    async def protocol_table_partial(
        request: Request,
        protocol_name: str,
        registry_type: str,
        page: int = 1,
        device_name: str | None = None,
    ):
        """HTMX partial — register table rows, or JSON editor for json registry_type."""
        if registry_type == "json":
            # Look up protocol_group so we can find the .json file
            with session_scope() as db:
                row: Row[Tuple[str]] | None = (
                    db.query(ProtocolRegister.protocol_group)
                    .filter(ProtocolRegister.protocol_name == protocol_name)
                    .first()
                )
            protocol_group = row[0] if row else ""
            config_dir = getattr(request.app.state, "config_dir", None)
            json_data, is_override = get_protocol_json(
                request.app.state.protocols_dir, protocol_group, protocol_name,
                config_dir=config_dir,
            )
            json_data = json_data or {}
            return templates.TemplateResponse(
                request=request,
                name="partials/json_editor.html",
                context={
                    "protocol_name": protocol_name,
                    "protocol_group": protocol_group,
                    "json_data": json_data,
                    "is_override": is_override,
                },
            )

        with session_scope() as db:
            data: dict[str, Any] = get_protocol_registers(
                db, protocol_name, registry_type, page, page_size=5000, device_name=device_name
            )

        # Append synthetic metric rows when rendering a device (scraper) view.
        # Synthetic rows are display-only — they have no DB row, no toggle
        # endpoints, and are never written to mask/screen files.  The transport
        # is looked up by name via the gateway so the metadata stays live.
        if device_name:
            gateway = getattr(request.app.state, "gateway", None)
            if gateway is not None:
                transport = gateway.get_transport(f"transport.{device_name}")
                if transport is not None:
                    synthetic = build_synthetic_rows(transport)
                    if synthetic:
                        data["rows"] = list(data.get("rows", [])) + synthetic

        return templates.TemplateResponse(
            request=request,
            name="partials/protocol_table.html",
            context={
                "protocol_name": protocol_name,
                "registry_type": registry_type,
                "device_name": device_name,
                **data,
            },
        )

    @app.post("/api/protocol/{protocol_group}/{protocol_name}/json", response_class=HTMLResponse, response_model=None)
    async def save_protocol_json(
        request: Request,
        protocol_group: str,
        protocol_name: str,
    ):
        """Save updated JSON config for a protocol directly to disk."""

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "detail": "Invalid JSON body"}, status_code=400)
        config_dir = getattr(request.app.state, "config_dir", request.app.state.protocols_dir / protocol_group)
        config_dir.mkdir(parents=True, exist_ok=True)
        json_path = config_dir / f"{protocol_name}.json"
        try:
            json_path.write_text(_json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)
        return JSONResponse({"status": "ok", "path": str(json_path)})

    @app.get("/diff-panel", response_class=HTMLResponse, response_model=None)
    async def diff_panel(request: Request):
        """HTMX partial — visual diff of staged vs disk state."""
        with session_scope() as db:
            diff: DiffResult = build_diff(db)

        return templates.TemplateResponse(
            request=request,
            name="partials/diff_panel.html",
            context={"diff": diff},
        )

    @app.get("/orphan-modal", response_class=HTMLResponse, response_model=None)
    async def orphan_modal(request: Request):
        """HTMX partial — orphan review modal content."""
        with session_scope() as db:
            orphans: List[Setting] = get_orphaned_settings(db)

        return templates.TemplateResponse(
            request=request,
            name="partials/orphan_modal.html",
            context={"orphans": orphans},
        )

    @app.get("/pages/global-settings", response_class=HTMLResponse, response_model=None)
    async def global_settings_page(request: Request):
        with session_scope() as db:
            nav: NavData           = get_nav_data(db)
            general: List[Setting] = (
                db.query(Setting).filter_by(section="general").all()
            )
        return templates.TemplateResponse(
            request=request,
            name="pages/global_settings.html",
            context={
                "nav": nav,
                "settings": general,
                "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
            },
        )

    @app.get("/pages/transport-library", response_class=HTMLResponse, response_model=None)
    async def transport_library_page(request: Request):
        with session_scope() as db:
            nav: NavData = get_nav_data(db)
        library: List[dict[str, Any]] = get_transport_library(
            request.app.state.transports_dir
        )
        return templates.TemplateResponse(
            request=request,
            name="pages/transport_library.html",
            context={
                "nav": nav,
                "library": library,
                "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
            },
        )

    @app.get("/pages/transport-settings", response_class=HTMLResponse, response_model=None)
    async def transport_settings_page(request: Request):
        with session_scope() as db:
            nav: NavData = get_nav_data(db)
            settings: List[SettingDescription] = get_all_setting_descriptions(db)
            # Convert to plain dicts for template (avoids lazy-load issues outside session)
            settings_data = [
                {
                    "id": s.id,
                    "key": s.key,
                    "transports": s.transports or "",
                    "description": s.description or "",
                    "is_dirty": s.is_dirty,
                }
                for s in settings
            ]
        return templates.TemplateResponse(
            request=request,
            name="pages/transport_settings.html",
            context={
                "nav": nav,
                "settings": settings_data,
                "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
            },
        )

    @app.get("/pages/faq", response_class=HTMLResponse, response_model=None)
    async def faq_page(request: Request):
        with session_scope() as db:
            nav: NavData = get_nav_data(db)
        return templates.TemplateResponse(
            request=request,
            name="pages/faq.html",
            context={
                "nav": nav,
                "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
            },
        )

    @app.get("/pages/about", response_class=HTMLResponse, response_model=None)
    async def about_page(request: Request):
        with session_scope() as db:
            nav: NavData = get_nav_data(db)
        return templates.TemplateResponse(
            request=request,
            name="pages/about.html",
            context={
                "nav": nav,
                "proto_groups": get_protocol_groups(request.app.state.protocols_dir),
            },
        )

    @app.get("/api/scan", tags=["admin"])
    async def trigger_scan(request: Request) -> dict[str, Any]:
        """Manually trigger a re-scan of config + protocols."""
        # from classes.WebServer.debug_defaults import check_stale_db_rows, run_debug
        # run_debug(project_root=request.app.state.project_root, config_path=request.app.state.config_path)
        # check_stale_db_rows(
        #     db_path=request.app.state.db_dir / "mpg_staging.db",
        #     config_path=request.app.state.config_path,
        #     transports_dir=request.app.state.transports_dir,
        # )

        try:
            stats: dict[str, int] = request.app.state.scanner.run()
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
        else:
            return {"status": "ok", **stats}

    return app


# ---------------------------------------------------------------------------
# Module-level port holder — lets the startup log message show the real port
# ---------------------------------------------------------------------------
_current_port: int = 1717


# ---------------------------------------------------------------------------
# Launcher — called from protocol_gateway.py
# ---------------------------------------------------------------------------

def start_webserver(config_file_path: Path, log_file: str, log_dir: str, gateway_instance: Any = None, port: int = 1717) -> None:
    """
    Launch the FastAPI web server in a daemon thread.
    Returns immediately; the server runs in the background.

    config_file_path   — the fully-resolved Path to config.cfg as built by
                    protocol_gateway.main() (e.g. <root>/config/config.cfg)
    config_file  fully parsed config file.


    project_root is derived by walking parent directories until a folder
    containing pyproject.toml is found — matching the same discovery logic
    used in protocol_gateway.main().

    Usage in protocol_gateway.main():

        config_path: Path = root / "config" / config_file
        start_webserver(config_path, gateway_instance=mpg)
        mpg.run()   # blocks forever on the main thread
    """
    global _current_port
    _current_port = port

    # config_file is already a fully-resolved absolute Path from the gateway.
    # project_root: walk up until pyproject.toml is found (same logic as gateway).
    config_path: Path  = config_file_path.resolve()  # with file name.
    config_dir: Path   = config_path.parent          # e.g. <root>/config/
    project_root: Path = config_dir.parent           # e.g. <root>/  (fallback before pyproject.toml walk)

    _log.info(f"WebServer config_path  : {config_path}")
    _log.info(f"WebServer project_root : {project_root}")

    app: FastAPI = create_app(
        config_path=config_path,
        log_file= log_file,
        log_dir=log_dir,
        project_root=project_root,
        config_dir=config_dir,
        gateway_instance=gateway_instance,
    )

    uv_config = uvicorn.Config(
        app,
        host="0.0.0.0",  # noqa: S104
        port=port,
        log_level="info",
        access_log=False,
    )
    server: NoSignalServer = NoSignalServer(uv_config)

    def _run() -> None:

        try:
            asyncio.run(server.serve()) # type: ignore
        except KeyboardInterrupt:
            # Graceful exit on Ctrl+C
            pass

    thread = threading.Thread(target=_run, name="MPGWebServer", daemon=True)
    thread.start()
    _log.info(f"MPG WebServer launched on http://0.0.0.0:{port}") # noqa: G004

    # Store server reference on gateway so it can trigger graceful shutdown
    # via:  mpg.web_server.should_exit = True
    if gateway_instance is not None:
        gateway_instance.web_server = server
