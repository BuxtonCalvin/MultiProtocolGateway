# Description: Bridge module for InfluxDB3 output transport with persistent disk backlog and connection monitoring
# File: influxdb3_out.py
# forked from influxdb_out.py in the original PythonProtocolGateway repository by Jared Mauch
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

# Bridge module for InfluxDB v3 output transport with persistent disk backlog and connection monitoring

# When working with InfluxDB v3 (influxdb_client_3), query results are natively returned as Apache Arrow tables (pyarrow.Table).

from __future__ import annotations

import logging
import math
import pickle
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, cast
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo

import pyarrow as pa
import requests

# influx db methods are not recognized by type checker
from influxdb_client_3 import InfluxDBClient3, Point
from requests.adapters import HTTPAdapter
from tzlocal import get_localzone_name

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings, strtobool

from ..protocol_settings import Data_Type, Registry_Type
from .transport_base import (
    BridgeHealthSnapshot,
    ColumnInfo,
    DataPayload,
    HeapProfile,
    StaleRegistryState,
    StorageOverview,
    TableStat,
    transport_base,
)

if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

# Type alias for a serializable InfluxDB point dict (including the optional
# internal '_backlog_time' sentinel used for age-based eviction)
InfluxPoint = dict[str, object]


class InfluxDB3ClientKwargs(TypedDict):
    """Keyword arguments for InfluxDBClient3 construction (see
    _build_client_kwargs). org is NotRequired — it's omitted entirely for
    self-hosted IOx deployments since some client versions reject a None
    value, and only included when explicitly set in config for Cloud
    Dedicated / Cloud Serverless deployments that require it."""
    host: str
    token: str
    database: str
    org: NotRequired[str]


class influxdb3_out(transport_base):
    transport_type = "bridge"
    """InfluxDB v3 output transport that writes solar metrics to an InfluxDB v3 server."""

    # ------------------------------------------------------------------
    # Class-level attribute declarations (overridden in __init__)
    # ------------------------------------------------------------------
    host: str = "https://us-east-1-1.aws.cloud2.influxdata.com"

    # Optional. If set, overrides any port embedded directly in the `host`
    # setting (legacy "myhost:8181" style config). Left empty, an embedded
    # host port is kept as-is — so existing configs written before this
    # setting existed keep working unchanged. See _normalize_host.
    port: str = ""
    database: str = "solar"       # In v3 this is the "bucket" / database name
    token: str = ""               # v3 uses token-based auth (replaces username/password)
    org: str = ""                 # Organization name (required for InfluxDB Cloud)
    measurement: str = "device_data"
    include_timestamp: bool = True
    include_device_info: bool = True
    batch_size: int = 100
    batch_timeout: float = 10.0
    force_float: bool = True  # Force all numeric fields to floats to avoid type conflicts
    # Timestamp settings
    use_utc_timestamp: bool = False

    # Connection monitoring settings
    reconnect_attempts: int = 5
    reconnect_delay: float = 5.0
    connection_timeout: int = 10

    # Stale data detection settings
    stale_data_timeout: int = 300       # seconds before data is considered stale
    max_stale_attempts: int = 3         # max reconnect attempts per stale period
    retry_delay_mins: int = 5           # minimum minutes between stale reconnect attempts

    # Exponential backoff settings
    use_exponential_backoff: bool = True
    max_reconnect_delay: float = 300.0  # 5 minutes max delay

    # Persistent storage settings
    enable_persistent_storage: bool = True
    persistent_storage_path: str = "backlogs"
    max_backlog_size: int = 10000  # Maximum number of points to store
    max_backlog_age: int = 86400   # 24 hours in seconds

    # Periodic reconnection settings
    periodic_reconnect_interval: float = 14400.0  # 4 hours in seconds

    # Optional local filesystem path to InfluxDB v3's object store (e.g.
    # "/var/lib/influxdb3/object_store"), used only to report on-disk size
    # in the Storage Overview panel. Empty by default — MPG and InfluxDB
    # are very often on different hosts, so this is opt-in, not assumed.
    object_store_dir: str = ""

    # Optional URL to the Rust/pprof heap-profile debug endpoint (e.g.
    # "http://localhost:8089/debug/pprof/heap"). Empty by default — this
    # debug endpoint typically isn't enabled or exposed on the same port
    # as the main API, so it's never assumed, only probed if configured.
    # See _probe_heap_profile for what "probed" means here.
    debug_pprof_url: str = ""

    # Runtime state — typed explicitly so mypy / pyright can track them
    client: Optional[InfluxDBClient3] = None
    last_batch_time: float = 0.0
    last_connection_check: float = 0.0
    connection_check_interval: float = 300.0  # seconds
    last_periodic_reconnect_attempt: float = 0.0

    # Persistent storage runtime state
    backlog_file: Optional[Path] = None
    backlog_points: list[InfluxPoint] = []

    def __init__(self, settings: TransportSettings) -> None:

        super().__init__(settings)

        self.host = settings.get("host", fallback=self.host)
        self.port = settings.get("port", fallback=self.port)
        self.host, self.port = self._normalize_host(self.host, self.port)
        self.mgmt_api_url: str = settings.get("mgmt_api_url", fallback=f"{self._endpoint_url}/api/v3/databases")
        self.database = settings.get("database", fallback=self.database)
        self.auto_create_database: bool = strtobool(settings.get("auto_create_database", fallback="true"))
        self.token = settings.get("token", fallback=self.token)
        self.org = settings.get("org", fallback=self.org)
        self.measurement = settings.get("measurement", fallback=self.measurement)
        self.include_timestamp = strtobool(settings.get("include_timestamp", fallback=self.include_timestamp))
        self.include_device_info = strtobool(settings.get("include_device_info", fallback=self.include_device_info))
        self.batch_size = settings.getint("batch_size", fallback=self.batch_size)
        self.batch_timeout = settings.getfloat("batch_timeout", fallback=self.batch_timeout)
        self.force_float = strtobool(settings.get("force_float", fallback=self.force_float))

        # Connection monitoring settings
        self.reconnect_attempts = settings.getint("reconnect_attempts", fallback=self.reconnect_attempts)
        self.reconnect_delay = settings.getfloat("reconnect_delay", fallback=self.reconnect_delay)
        self.connection_timeout = settings.getint("connection_timeout", fallback=self.connection_timeout)

        # Stale data detection settings
        self.stale_data_timeout: int = settings.getint("stale_data_timeout", fallback=self.stale_data_timeout)
        self.max_stale_attempts: int = settings.getint("max_stale_attempts", fallback=self.max_stale_attempts)
        self.retry_delay_mins: int = settings.getint("retry_delay_mins", fallback=self.retry_delay_mins)

        # Stale data runtime state — keyed by transport_name, tracks last seen data and timestamps for stale detection logic
        self._stale_registry: dict[str, StaleRegistryState] = {}

        # Timestamp timezone setting — mirrors timescaledb.use_utc_timestamp
        self.use_utc_timestamp: bool = strtobool(settings.get("use_utc_timestamp", fallback=str(self.use_utc_timestamp)))
        self.machine_timezone: str = "UTC" if self.use_utc_timestamp else get_localzone_name()
        self._log.info(f"InfluxDB timestamp timezone: {self.machine_timezone}")

        # Upstream reconnect callback — wired by protocol_gateway via _wire_reconnect_hooks,
        self.request_upstream_reconnect: Callable[[str], None] | None = None

        # Exponential backoff settings
        self.use_exponential_backoff = strtobool(settings.get("use_exponential_backoff", fallback=self.use_exponential_backoff))
        self.max_reconnect_delay = settings.getfloat("max_reconnect_delay", fallback=self.max_reconnect_delay)

        # Persistent storage settings
        self.enable_persistent_storage = strtobool(settings.get("enable_persistent_storage", fallback=self.enable_persistent_storage))
        self.persistent_storage_path = settings.get("persistent_storage_path", fallback=self.persistent_storage_path)
        self.max_backlog_size = settings.getint("max_backlog_size", fallback=self.max_backlog_size)
        self.max_backlog_age = settings.getint("max_backlog_age", fallback=self.max_backlog_age)

        # Periodic reconnection settings
        self.periodic_reconnect_interval = settings.getfloat("periodic_reconnect_interval", fallback=self.periodic_reconnect_interval)

        # Optional local filesystem / debug-endpoint settings for the
        # Storage Overview panel — see class-level comments above.
        self.object_store_dir = settings.get("object_store_dir", fallback=self.object_store_dir)
        self.debug_pprof_url = settings.get("debug_pprof_url", fallback=self.debug_pprof_url)


        # Instance-level mutable state
        self.batch_points: list[InfluxPoint] = []
        self.backlog_points: list[InfluxPoint] = []
        self._batch_lock: threading.Lock = threading.Lock()

        if self.enable_persistent_storage:
            self._init_persistent_storage()

    # ------------------------------------------------------------------
    # Persistent storage helpers
    # ------------------------------------------------------------------

    def _init_persistent_storage(self) -> None:
        """Initialize persistent storage for data backlog."""
        try:
            project_root: Path = Path(__file__).resolve().parents[2]
            # Force path to look relative by stripping leading slashes/drives
            clean_setting: str = self.persistent_storage_path.lstrip("\\/")
            storage_dir: Path = (project_root / clean_setting).resolve()

            storage_dir.mkdir(parents=True, exist_ok=True)

            self.backlog_file = storage_dir / f"influxdb3_backlog_{self.transport_name}.pkl"

            self._load_backlog()

            self._log.info(f"Persistent storage initialized: {self.backlog_file}")
            self._log.info(f"Loaded {len(self.backlog_points)} points from backlog")

        except Exception as e:
            self._log.error(f"Failed to initialize persistent storage: {e}")
            self.enable_persistent_storage = False
            self.send_message(
                message="Error: Failed to initialize persistent storage for InfluxDB backlog. Check logs for details.",
                title="MPG InfluxDB Backlog Initialization Error",
                priority=1
            )

    def _load_backlog(self) -> None:
        """Load backlog points from persistent storage."""
        if self.backlog_file is None or not self.backlog_file.exists():
            self.backlog_points = []
            return

        try:
            self.backlog_points = pickle.loads(self.backlog_file.read_bytes())  # noqa: S301

            # Remove points older than max_backlog_age
            current_time: float = time.time()
            original_count: int = len(self.backlog_points)
            self.backlog_points = [
                point for point in self.backlog_points
                if current_time - cast(float, point.get("_backlog_time", 0.0)) < self.max_backlog_age
            ]

            if len(self.backlog_points) < original_count:
                self._log.info(f"Cleaned {original_count - len(self.backlog_points)} old points from backlog")
                self._save_backlog()

        except Exception as e:
            self._log.error(f"Failed to load backlog: {e}")
            self.backlog_points = []

    def _save_backlog(self) -> None:
        """Save backlog points to persistent storage."""
        if self.backlog_file is None or not self.enable_persistent_storage:
            return

        try:
            self.backlog_file.write_bytes(pickle.dumps(self.backlog_points))
        except Exception as e:
            self._log.error(f"Failed to save backlog: {e}")
            self.send_message(
                message="Error: Failed to save InfluxDB backlog. Check logs for details.",
                title="MPG InfluxDB Backlog Save Error",
                priority=1
            )

    def _add_to_backlog(self, point: InfluxPoint) -> None:
        """Add a point dict to the persistent backlog.
        _backlog_time is always UTC Unix time regardless of the timezone setting
        """
        if not self.enable_persistent_storage:
            return

        point["_backlog_time"] = time.time()
        self.backlog_points.append(point)

        if len(self.backlog_points) > self.max_backlog_size:
            removed: InfluxPoint = self.backlog_points.pop(0)
            self._log.warning(f"Backlog full, removed oldest point: {removed.get('measurement', 'unknown')}")

        self._save_backlog()

    def _flush_backlog(self) -> None:
        """Write all backlog points to InfluxDB v3."""
        if not self.backlog_points or not self.connected:
            return

        self._log.info(f"Flushing {len(self.backlog_points)} backlog points to InfluxDB v3")

        try:
            points_to_send: list[Point] = [
                self._dict_to_influx3_point({k: v for k, v in point.items() if k != "_backlog_time"})
                for point in self.backlog_points
            ]

            if self.client is not None:
                self.client.write(record=points_to_send, database=self.database) # type: ignore[reportUnknownMemberType]
                self._log.info(f"Successfully wrote {len(points_to_send)} backlog points to InfluxDB v3")
                self.backlog_points = []
                self._save_backlog()

        except Exception as e:
            self._log.error(f"Failed to flush backlog to InfluxDB v3: {e}")
            # Don't clear backlog on failure — will retry later

    def _now_ts(self) -> datetime:
        """
        Returns the current timestamp in the configured timezone.
        UTC if use_utc_timestamp = true, otherwise local machine timezone.
        _now_tz() module function scoped to this instance since InfluxDB timestamps are set imperatively.
        """
        if self.use_utc_timestamp:
            return datetime.now(timezone.utc)
        return datetime.now().astimezone()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initialize the InfluxDB v3 client connection and handle lifecycle states."""
        self._log.info("influxdb3_out connecting to server...")
        try:
            # Clean up old diagnostic session if this is a reconnect attempt
            old_session: Optional[requests.Session] = getattr(self, "session", None)
            if old_session is not None:
                try:
                    old_session.close()
                except Exception as e:
                    self._log.debug(f"Failed to close old diagnostic session: {e}")
                self.session = None

            # Initialize the official gRPC engine client
            client_kwargs: InfluxDB3ClientKwargs = self._build_client_kwargs()
            self.client = InfluxDBClient3(**client_kwargs)

            # Establish companion HTTP session for diagnostics
            token: str = client_kwargs.get("token", "")
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=1)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            if token:
                session.headers.update({"Authorization": f"Token {token}"})
            self.session: requests.Session | None= session

            # Attempt a read probe against the targeted database context
            try:
                self._health_check()  # Runs: SHOW TABLES LIMIT 1
                self._log.debug(f"Database '{self.database}' verified online.")
            except Exception as health_err:
                err_msg = str(health_err).lower()

                # Intercept the exact error indicating the database is not initialized
                if "database not found" in err_msg or "cannot retrieve database" in err_msg:
                    if not self.auto_create_database:
                        self._log.warning(
                            f"Database '{self.database}' does not exist on the server "
                            f"and auto_create_database=false. Aborting connection setup."
                        )
                        return False

                    # SELF-HEALING ACTION: Bypass the query block.
                    # InfluxDB 3 auto-provisions namespaces on the first write payload.
                    self._log.info(
                        f"Database '{self.database}' is missing from the catalog. "
                        f"The server will automatically create it upon your module's first write batch."
                    )
                else:
                    # Immediately re-raise real issues like networking or bad credentials
                    raise health_err  # noqa: TRY201

        except Exception as e:
            self._log.error(f"Failed to connect to InfluxDB: {e}")
            self.connected = False
            if getattr(self, "session", None) is not None:
                try:
                    self.session.close() # type: ignore
                except Exception:  # noqa: S110
                    pass
                self.session = None
            return False
        else:
            self.connected = True
            self.last_connection_check = time.time()
            self.last_periodic_reconnect_attempt = time.time()
            self._log.info(f"Connected to InfluxDB v3 at {self._endpoint_url}, database={self.database}")

            if self.enable_persistent_storage:
                self._flush_backlog()
            return True


    @property
    def _endpoint_url(self) -> str:
        """self.host + self.port combined into one URL, for logging and
        client construction — the one place these two separate attributes
        (see _normalize_host) are joined back together."""
        return f"{self.host}:{self.port}" if self.port else self.host

    @staticmethod
    def _normalize_host(host: str, port: str) -> tuple[str, str]:
        """
        Split `host` and `port` into two clean, separate values — this is
        the actual "separation" of host and port: `self.host` and
        `self.port` stay independent attributes afterward, each holding
        exactly one thing, rather than `self.host` silently becoming a
        combined "host:port" string. Downstream consumers (the endpoint
        display, log messages, _build_client_kwargs) can each use host and
        port however they need — concatenated for a URL, shown separately
        in a UI, etc. — without either one being wrong.

        Returns (host, port) — host includes the scheme, never a port;
        port is "" when none is configured or embedded.
        """
        host = host.strip()

        scheme: str = "http"
        for candidate in ("http", "https"):
            prefix: str = f"{candidate}://"
            if host.startswith(prefix):
                scheme = candidate
                host = host[len(prefix):]
                break

        # Parsed via urlsplit (rather than a hand-rolled rsplit on ":") so
        # IPv6 literals like "[::1]:8181" are split into hostname/port
        # correctly instead of on every colon.
        parsed: SplitResult = urlsplit(f"//{host}")
        hostname: str = parsed.hostname or host
        if ":" in hostname:  # bare (unbracketed) IPv6 literal — needs brackets in a URL
            hostname = f"[{hostname}]"

        resolved_port: str = port.strip() or (str(parsed.port) if parsed.port else "")
        return f"{scheme}://{hostname}{parsed.path}", resolved_port

    def _build_client_kwargs(self) -> InfluxDB3ClientKwargs:
        """
        Builds the keyword arguments for InfluxDBClient3 construction.
        org is omitted entirely for self-hosted IOx deployments since it
        is not used and some client versions reject a None value.
        Only included when explicitly set in config for Cloud Dedicated
        or Cloud Serverless deployments that require it.
        """
        # self.host and self.port are kept separate (see _normalize_host) —
        # combined into one URL only here, where the client actually needs it.
        host_url: str = self._endpoint_url
        kwargs: InfluxDB3ClientKwargs = {
            "host":     host_url,
            "token":    self.token,
            "database": self.database,
        }
        if self.org:
            kwargs["org"] = self.org
        return kwargs

    def _create_database(self) -> None:
        """Programmatically provisions the database target using the native gRPC management client."""
        self._log.info(f"Database '{self.database}' missing. Programmatically creating it via SDK...")

        if self.client is None:
            raise RuntimeError("Cannot create database: InfluxDBClient3 client is not initialized.")

        try:
            # The official v3 client provides a native management method
            # that bypasses the SQL planning engine entirely
            self.client.create_database(database=self.database) # type: ignore
            self._log.info(f"Database '{self.database}' created successfully from code.")

        except Exception as e:
            error_msg: str = str(e).lower()
            if "already exists" in error_msg or "409" in error_msg:
                self._log.info(f"Database {self.database} already exists on the server.")
                return

            msg: str = f"Failed to programmatically create database {self.database}. Error: {e}"
            self._log.error(msg)
            raise RuntimeError(msg) from e

    def _health_check(self) -> None:
        """Perform a lightweight query to verify connectivity and credentials.

        InfluxDB v3 does not expose a /ping endpoint, so we issue a minimal
        SQL query. An exception here propagates to the caller so they can
        handle the failure appropriately.
        """
        if self.client is None:
            raise RuntimeError("Client not initialized")

        # Validates auth, network, and database existence using a supported system query.
        query_str = "SELECT 1 FROM information_schema.tables LIMIT 1"
        self.client.query(query_str, database=self.database, language="sql") # type: ignore[reportUnknownMemberType]


    def _check_connection(self) -> bool:
        """Check if the connection is still alive and reconnect if necessary."""
        current_time: float = time.time()

        # Proactive periodic reconnect check
        if (self.periodic_reconnect_interval > 0 and
                current_time - self.last_periodic_reconnect_attempt >= self.periodic_reconnect_interval):

            self.last_periodic_reconnect_attempt = current_time
            self._log.info(f"Periodic reconnection check (every {self.periodic_reconnect_interval} seconds)")

            # Check if active connection exists; if not, immediately attempt reconnect
            if self.connected and self.client:
                try:
                    self._health_check()
                except Exception as e:
                    self._log.warning(f"Periodic connection check failed: {e}")
                    return self._attempt_reconnect()
            else:
                return self._attempt_reconnect()

        # Throttle routine checks to avoid excessive health-check queries
        if current_time - self.last_connection_check < self.connection_check_interval:
            return self.connected

        self.last_connection_check = current_time

        if not self.connected or not self.client:
            return self._attempt_reconnect()

        try:
            self._health_check()
        except Exception as e:
            self._log.warning(f"Connection check failed: {e}")
            return self._attempt_reconnect()
        else:
            return True


    def _attempt_reconnect(self) -> bool:
        """Attempt to reconnect to InfluxDB v3 with exponential backoff."""
        self._log.info(f"Attempting to reconnect to InfluxDB v3 at {self._endpoint_url}")

        for attempt in range(self.reconnect_attempts):
            try:
                self._log.info(f"Reconnection attempt {attempt + 1}/{self.reconnect_attempts}")

                if self.client:
                    try:
                        self.client.close()
                    except Exception as e:
                        self._log.warning(
                            f"Failed to close existing InfluxDB3 client during reconnect attempt: {e} "
                            f"{attempt + 1}/{self.reconnect_attempts}"
                        )

                self.client = InfluxDBClient3(**self._build_client_kwargs())

                self._health_check()

            except Exception as e:
                self._log.warning(f"Reconnection attempt {attempt + 1} failed: {e}")
                if attempt < self.reconnect_attempts - 1:
                    if self.use_exponential_backoff:
                        delay: float = min(self.reconnect_delay * (2 ** attempt), self.max_reconnect_delay)
                        self._log.info(f"Waiting {delay:.1f} seconds before next attempt (exponential backoff)")
                    else:
                        delay = self.reconnect_delay
                        self._log.info(f"Waiting {delay:.1f} seconds before next attempt")
                    time.sleep(delay)
            else:
                # This block runs only if client creation and healthcheck succeed without throwing exceptions
                self.connected = True
                self.last_periodic_reconnect_attempt = time.time()
                self._log.info("Successfully reconnected to InfluxDB")

                if self.enable_persistent_storage:
                    self._flush_backlog()
                    self._log.info("Flushed backlog after successful reconnection")

                return True

        self._log.error(f"Failed to reconnect after {self.reconnect_attempts} attempts")
        self.connected = False
        return False

    def trigger_periodic_reconnect(self) -> bool:
        """Manually trigger a periodic reconnection check."""
        self.last_periodic_reconnect_attempt = 0.0
        return self._check_connection()

    def get_health_snapshot(self) -> BridgeHealthSnapshot:
        """
        Read-only snapshot of this bridge's live connection/backlog/
        staleness state, for the device page's "Bridge Health" panel.
        Pulls together state that's otherwise scattered across connection
        management, persistent storage, and stale-data detection — nothing
        here is a fresh query, just this instance's own attributes.

        `connected` is included for completeness but isn't necessarily
        rendered by the panel — the device page already shows connection
        status in its own status badge, so the template may choose to
        skip repeating it here.
        """
        stale_count: int = sum(1 for s in self._stale_registry.values() if s.get("is_stale"))

        return {
            "connected": self.connected,
            "batch_pending": len(self.batch_points),
            "batch_size": self.batch_size,
            "backlog_count": len(self.backlog_points),
            "max_backlog_size": self.max_backlog_size,
            "max_backlog_age": self.max_backlog_age,
            "persistent_storage_enabled": self.enable_persistent_storage,
            "periodic_reconnect_interval": self.periodic_reconnect_interval,
            "last_periodic_reconnect_attempt": self.last_periodic_reconnect_attempt,
            "stale_transport_count": stale_count,
            "tracked_transport_count": len(self._stale_registry),
        }

    def get_storage_overview(self) -> StorageOverview:
        """
        Best-effort, read-only storage snapshot for the device page's
        Storage Overview panel. Tailored for influxdb3-core.

        Three independent sources, each attempted separately:
        1. information_schema.columns (table names) + system.parquet_files
           (row_count, size_bytes per file, summed per table) — Core has no
           'system.chunks' table (that's Enterprise/Cloud-only), but it does
           persist real per-file size and row count in system.parquet_files,
           which this aggregates by table_name. If parquet_files comes back
           completely empty (nothing flushed to disk yet, or the server has
           no persistent object store configured at all), falls back to a
           live per-table COUNT(*) so row_count is still populated —
           file_size_bytes has no such fallback and stays 0 in that case,
           since COUNT(*) can't produce a size.
        2. information_schema.columns — Flat schema map of all columns,
           types, and iox designations.
        3. object_store_dir — Local directory size calculation. Crucial
           for 3-core instances to capture true disk footprints.

        Nothing here raises — errors are logged and the remaining functional
        metrics still return to populate the UI panel.
        """
        result: StorageOverview = {
            "connected": self.connected,
            "database": self.database,
            "items_label": "Tables",
            "has_table_stats": True,
            "table_stats": [],       # [{table_name, row_count, file_size_bytes, memory_bytes}]
            "columns": [],           # [{table_name, column_name, data_type, iox_column_type}]
            "item_names": [],         # table names only, for template compatibility
            "sample_item": None,
            "sample_item_approx_rows": None,
            "retention_policies": None,  # not applicable to v3
            "data_dir": self.object_store_dir or None,
            "data_dir_size_bytes": None,
            "heap_profile": None,
            "error": None,
        }

        if not self.connected or self.client is None:
            self._log.debug(
                f"get_storage_overview: skipping — connected={self.connected}, "
                f"client={'set' if self.client is not None else 'None'}"
            )
            result["error"] = "Not connected to InfluxDB."
            return result

        self._log.debug(f"get_storage_overview: starting for database '{self.database}' via {self._endpoint_url}")

        # 1. Gather Table Metric Fallbacks
        try:
            # Drop the 'public' filter and explicitly filter out system tables instead
            tables_sql: str = (
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema NOT IN ('information_schema', 'system')"
            )
            self._log.debug(f"get_storage_overview: running table-name query: {tables_sql}")
            tables_query: pa.Table = cast(pa.Table, self.client.query( # type: ignore
                tables_sql,
                database=self.database,
                language="sql",
            ))
            table_rows: list[dict[str, object]] = tables_query.to_pylist()
            table_names: list[str] = sorted(
                cast(str, row["table_name"]) for row in table_rows if row.get("table_name")
            )
            self._log.debug(
                f"get_storage_overview: table-name query returned {len(table_rows)} row(s), "
                f"table_names={table_names!r}"
            )

            # Real per-table row counts and on-disk sizes, aggregated from
            # system.parquet_files — InfluxDB 3's actual persisted-storage
            # system table (system.chunks, which the previous version of
            # this method targeted, is Enterprise/Cloud-only and doesn't
            # exist on Core). One query total, rather than a COUNT(*) per
            # table: COUNT(*) against a table backed by many parquet files
            # can fail outright once a table has enough of them ("Query
            # would exceed file limit of N parquet files"), which this
            # avoids entirely, and it's the only way to get a real
            # file_size_bytes at all — COUNT(*) never could.
            #
            # Trade-off: parquet_files only reflects data already flushed
            # to disk, so a table with very recent, not-yet-persisted
            # writes may show a slightly lower row_count here than a live
            # COUNT(*) would. Tables enumerated above but with no persisted
            # files yet still appear, at 0 rows / 0 bytes, rather than
            # being silently absent.
            parquet_stats: dict[str, dict[str, int]] = {}
            try:
                # Quoted exactly as InfluxDB's own documented example does —
                # system."parquet_files", not system.parquet_files. Whether
                # that quoting is strictly required or just defensive isn't
                # documented, but it costs nothing to match it exactly.
                parquet_sql: str = 'SELECT table_name, size_bytes, row_count FROM system."parquet_files"'
                self._log.debug(f"get_storage_overview: running parquet-files query: {parquet_sql}")
                parquet_table: pa.Table = cast(pa.Table, self.client.query(  # type: ignore[reportUnknownMemberType]
                    parquet_sql,
                    database=self.database,
                    language="sql",
                ))
                parquet_rows: list[dict[str, object]] = parquet_table.to_pylist()
                self._log.debug(
                    f"get_storage_overview: parquet-files query returned {len(parquet_rows)} row(s)"
                    + (f", columns={parquet_table.column_names!r}" if parquet_rows else "")
                )
                if parquet_rows:
                    # Log a sample row verbatim so a column-name mismatch
                    # (e.g. if a future client version renames size_bytes)
                    # is visible here instead of silently producing zeros.
                    self._log.debug(f"get_storage_overview: parquet-files sample row: {parquet_rows[0]!r}")

                skipped_no_table_name = 0
                for row in parquet_rows:
                    table_name: str | None = cast(Optional[str], row.get("table_name"))
                    if not table_name:
                        skipped_no_table_name += 1
                        continue
                    bucket: dict[str, int] = parquet_stats.setdefault(
                        table_name, {"row_count": 0, "file_size_bytes": 0}
                    )
                    bucket["row_count"] += int(cast(Optional[int], row.get("row_count")) or 0)
                    bucket["file_size_bytes"] += int(cast(Optional[int], row.get("size_bytes")) or 0)

                if skipped_no_table_name:
                    self._log.warning(
                        f"get_storage_overview: {skipped_no_table_name} of {len(parquet_rows)} "
                        "parquet-files row(s) had no usable 'table_name' value and were skipped — "
                        "if this is every row, the column name returned by the server may not "
                        "match what this query expects (see the sample row logged above)."
                    )

                self._log.debug(f"get_storage_overview: aggregated parquet_stats (pre-fallback)={parquet_stats!r}")

                if not parquet_rows:
                    # Query succeeded but returned nothing at all — distinct
                    # from "returned rows for some tables but not others",
                    # which is normal for a brand-new table. Two real causes
                    # produce this, and only one resolves on its own:
                    #   - not-yet-flushed: Core persists on an interval, not
                    #     on every write, so very fresh data legitimately
                    #     has no parquet files yet — this clears up after
                    #     the next persist cycle with no action needed.
                    #   - in-memory object store: if the server was started
                    #     without a persistent object store (e.g. no
                    #     --object-store=file/s3/... configured), there are
                    #     no parquet files, ever, by design — this table
                    #     will stay empty permanently, not just for now.
                    # This module can't tell which case it's in from here,
                    # so it falls back to a live per-table COUNT(*) below
                    # to get *a* row count either way; file_size_bytes has
                    # no fallback (COUNT(*) can't produce a size), so it
                    # stays 0 whenever parquet_files is empty.
                    self._log.info(
                        "get_storage_overview: system.\"parquet_files\" returned no rows for "
                        f"database '{self.database}' — either no data has been persisted to disk "
                        "yet (still buffered in the WAL — resolves on its own), or the server has "
                        "no persistent object store configured (permanent — check its --object-store "
                        "startup setting). Falling back to a live row count per table."
                    )
                    for name in table_names:
                        try:
                            count_sql: str = f'SELECT COUNT(*) as row_count FROM "{name}"'  # noqa: S608
                            count_table: pa.Table = cast(pa.Table, self.client.query(  # type: ignore[reportUnknownMemberType]
                                count_sql, database=self.database, language="sql",
                            ))
                            count_rows: list[dict[str, object]] = count_table.to_pylist()
                            live_count: int = int(cast(Optional[int], count_rows[0].get("row_count")) or 0) if count_rows else 0
                            parquet_stats[name] = {"row_count": live_count, "file_size_bytes": 0}
                            self._log.debug(f"get_storage_overview: live COUNT(*) for '{name}' = {live_count}")
                        except Exception as count_err:
                            self._log.debug(f"get_storage_overview: live COUNT(*) for '{name}' failed: {count_err}")
                    self._log.debug(f"get_storage_overview: aggregated parquet_stats (post-fallback)={parquet_stats!r}")
            except Exception as parquet_err:
                self._log.warning(f"get_storage_overview: system.\"parquet_files\" query failed: {parquet_err}")
                result["error"] = f"Per-table size/row-count query failed: {parquet_err}"

            table_stats: list[TableStat] = [
                {
                    "table_name": name,
                    "row_count": parquet_stats.get(name, {}).get("row_count", 0),
                    "file_size_bytes": parquet_stats.get(name, {}).get("file_size_bytes", 0),
                    # Not exposed by any Core system table — system.compactor,
                    # which would have this, is Enterprise Pro-only.
                    "memory_bytes": 0,
                }
                for name in table_names
            ]

            self._log.debug(f"get_storage_overview: final table_stats={table_stats!r}")

            result["table_stats"] = table_stats
            result["item_names"] = table_names

        except Exception as e:
            self._log.warning(f"get_storage_overview: fallback table metrics failed: {e}")
            result["has_table_stats"] = False


        # 2. Gather Complete Schema Maps
        try:
            # Apply the same robust system exclusion rule here
            cols_table: pa.Table = cast(pa.Table, self.client.query(   # type: ignore
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema NOT IN ('information_schema', 'system')",
                database=self.database,
                language="sql",
            ))

            raw_columns = cols_table.to_pylist()
            processed_columns: list[ColumnInfo] = []

            for row in raw_columns:
                t_name = row.get("table_name")
                c_name = row.get("column_name")
                d_type = row.get("data_type")

                if c_name == "time":
                    iox_type = "timestamp"
                elif d_type in ("Utf8", "Dictionary(Int32, Utf8)"):
                    iox_type = "tag"
                else:
                    iox_type = "field"

                processed_columns.append({
                    "table_name": t_name,
                    "column_name": c_name,
                    "data_type": d_type,
                    "iox_column_type": iox_type,
                })

            result["columns"] = processed_columns
            self._log.debug(f"get_storage_overview: schema query returned {len(raw_columns)} column row(s)")

            if not result["item_names"] and result["columns"]:
                result["item_names"] = sorted(list({row["table_name"] for row in result["columns"]}))  # noqa: C414
        except Exception as e:
            self._log.error(f"get_storage_overview: information_schema.columns query failed: {e}")
            result["error"] = f"Schema query failed: {e}"


        # 3. Object Store Directory Disk Footprint
        self._log.debug(f"get_storage_overview: object_store_dir setting = {self.object_store_dir!r}")
        if self.object_store_dir:
            try:
                store_path: Path = Path(self.object_store_dir)
                if store_path.exists():
                    result["data_dir_size_bytes"] = sum(
                        f.stat().st_size for f in store_path.rglob("*") if f.is_file()
                    )
            except Exception as e:
                self._log.warning(
                    f"get_storage_overview: could not size object_store_dir '{self.object_store_dir}': {e}"
                )

        # 4. Heap Profile Reachability Probe
        if self.debug_pprof_url:
            result["heap_profile"] = self._probe_heap_profile()

        return result


    def _probe_heap_profile(self) -> HeapProfile:
        """
        Best-effort reachability probe for the Rust/pprof heap-profile
        debug endpoint (GET .../debug/pprof/heap).
        """
        probe: HeapProfile = {
            "reachable": False,
            "size_bytes": None,
            "elapsed_ms": None,
            "error": None
        }

        # Safe verification check for the target configuration
        if not self.debug_pprof_url:
            return probe

        session = getattr(self, "session", requests)

        try:
            start: float = time.time()

            # stream=True prevents massive binary profiles from hitting RAM all at once
            with session.get(
                self.debug_pprof_url,
                timeout=self.connection_timeout,
                stream=True
            ) as resp:

                probe["elapsed_ms"] = int((time.time() - start) * 1000)

                if resp.status_code == 200:
                    probe["reachable"] = True

                    # Profiles are gzipped on the fly; manual stream reading is always required
                    bytes_counted = 0
                    for chunk in resp.iter_content(chunk_size=16384):
                        if chunk:
                            bytes_counted += len(chunk)
                    probe["size_bytes"] = bytes_counted
                else:
                    # Capture 404 errors elegantly if endpoints are absent in 3-core
                    probe["error"] = f"HTTP {resp.status_code} - Endpoint Unavailable"

        except requests.exceptions.RequestException as e:
            # Isolate network transport drops safely
            probe["error"] = f"Connection Failed: {e}"
        except Exception as e:
            probe["error"] = str(e)

        return probe


    # ------------------------------------------------------------------
    # Data writing
    # ------------------------------------------------------------------

    def write_data(self, data: DataPayload, from_transport: transport_base) -> None:
        """Entry point for incoming inverter data. Routes to online or offline path."""
        # Promote LCDMachineModelCode to device_model if present and meaningful
        if data.get("LCDMachineModelCode") and data["LCDMachineModelCode"] != "MPG":
            from_transport.device_model = str(data["LCDMachineModelCode"])

        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug(
                f"InfluxDB3_Out: Received data from {from_transport.transport_name} "
                f"(serial: {from_transport.device_serial_number}) with {len(data)} fields"
            )

        # Stale data detection — runs regardless of connection state so that
        # a stale scraper is detected even while InfluxDB itself is offline.
        transport_id: str = from_transport.transport_name
        timestamp: datetime = self._now_ts()
        is_stale: bool = self._check_is_stale(transport_id, data, timestamp)
        self._commit_transport_state(transport_id, data, timestamp, is_stale)

        if not self._check_connection():
            self._log.warning("Not connected to InfluxDB v3, storing data in backlog")
            self._process_and_store_data(data, from_transport)
            return

        self._process_and_write_data(data, from_transport)

    def _build_tags(self, from_transport: transport_base) -> dict[str, str]:
        """Return a tag dict populated with device metadata."""
        if not self.include_device_info:
            return {}
        return {
            "device_identifier": from_transport.device_identifier,
            "device_name": from_transport.device_name,
            "device_manufacturer": from_transport.device_manufacturer,
            "device_model": from_transport.device_model,
            "device_serial_number": from_transport.device_serial_number,
            "transport": from_transport.transport_name,
        }

    def _build_fields(self, data: DataPayload, from_transport: transport_base) -> dict[str, int | float | str]:
        """Classify and coerce each data key into the appropriate Python type for InfluxDB."""
        fields: dict[str, int | float | str] = {}

        for key, value in data.items():
            should_force_float: bool = False
            is_enum: bool = False
            is_ascii: bool = False

            if hasattr(from_transport, "protocolSettings") and from_transport.protocolSettings:
                for registry_type in [Registry_Type.INPUT, Registry_Type.HOLDING, Registry_Type.COIL, Registry_Type.DISCRETE]:
                    registry_map: list[registry_map_entry] = from_transport.protocolSettings.get_registry_map(registry_type)
                    for e in registry_map:
                        if e.variable_name.lower() == key.lower():
                            if e.unit_mod != 1.0:
                                should_force_float = True
                            if getattr(e, "has_enum_mapping", False):
                                is_enum = True
                            # pickup code descriptions here.
                            if getattr(e, "data_type", None) == Data_Type.ASCII or getattr(e, 'data_type', None) == Data_Type.STRING:
                                is_ascii = True
                            break
                    if should_force_float or is_enum or is_ascii:
                        break

            if is_enum or is_ascii:
                fields[key] = str(value)
                continue

            try:
                # If it's already a string and contains alpha characters ie synthetic labels, don't log it as a failure
                if isinstance(value, str) and any(c.isalpha() for c in value):
                    fields[key] = value
                    continue

                float_val: float = float(value)
                if self.force_float or should_force_float:
                    fields[key] = float_val
                else:
                    fields[key] = int(float_val) if float_val.is_integer() else float_val
            except (ValueError, TypeError):
                fields[key] = str(value)
                self._log.debug(f"InfluxDB_Out: Field {key}: {value} -> string (conversion failed)")

        return fields

    def _create_point_dict(self, data: DataPayload, from_transport: transport_base) -> InfluxPoint:
        """Create an InfluxDB point dict from data and transport metadata."""
        tags: dict[str, str] = self._build_tags(from_transport)
        self._log.debug(f"Tags: {tags}")

        point: InfluxPoint = {
            "measurement": self.measurement,
            "tags": tags,
            "fields": self._build_fields(data, from_transport),
        }

        if self.include_timestamp:
            point["time"] = int(self._now_ts().timestamp() * 1e9)  # nanoseconds

        return point

    def _check_is_stale(self, transport_id: str, row: DataPayload, timestamp: datetime) -> bool:
        """
        Compares the incoming data payload against the last seen payload for this
        transport. Returns True if data is identical and has been so for longer
        than stale_data_timeout seconds. Numeric comparisons use math.isclose
        to avoid false positives from floating point noise.
        """
        state: StaleRegistryState | None = self._stale_registry.get(transport_id)
        if not state:
            return False

        for key, val in row.items():
            prev: int | float | str | None = state["last_row"].get(key)
            if isinstance(val, (int, float)) and isinstance(prev, (int, float)):
                if not math.isclose(val, prev, rel_tol=1e-4, abs_tol=1e-6):
                    return False
            elif val != prev:
                return False

        elapsed: timedelta = timestamp - state["start_ts"]
        return elapsed > timedelta(seconds=self.stale_data_timeout)

    def _dict_to_influx3_point(self, point_dict: InfluxPoint) -> Point:
        """Convert a plain point dict into an influxdb_client_3 Point object."""
        p: Point = Point(cast(str, point_dict["measurement"]))

        for tag_key, tag_val in cast(dict[str, str], point_dict.get("tags", {})).items():
            p = p.tag(tag_key, tag_val) # type: ignore[reportUnknownMemberType]

        for field_key, field_val in cast(dict[str, int | float | str], point_dict.get("fields", {})).items():
            p = p.field(field_key, field_val) # type: ignore[reportUnknownMemberType]

        if "time" in point_dict:
            p = p.time(cast(int, point_dict["time"])) # type: ignore[reportUnknownMemberType]
        return p

    def _commit_transport_state(self, transport_id: str, row: DataPayload, timestamp: datetime, is_stale: bool) -> None:
        """
        Updates the stale registry for this transport after each write_data call.
        On fresh data resets all counters. On first stale detection triggers
        _handle_stale_event once per stale period — subsequent calls within the
        same stale window are no-ops until data changes and resets the state.
        """
        if transport_id not in self._stale_registry:
            self._stale_registry[transport_id] = {
                "last_row": dict(row), "start_ts": timestamp, "is_stale": False,
                "last_seen": timestamp, "stale_event_count": 0, "last_event_ts": None,
            }

        state: StaleRegistryState = self._stale_registry[transport_id]
        state["last_seen"] = timestamp

        self._log.debug(
            f"InfluxDB3_Out: Committing state for transport: {transport_id} | "
            f"is_stale: {is_stale} | "
            f"elapsed: {timestamp - state['start_ts']}"
        )

        if not is_stale:
            # Fresh data — reset everything including the throttle timer
            state.update({
                "last_row": dict(row), "start_ts": timestamp,
                "is_stale": False, "stale_event_count": 0,
                "last_event_ts": None,
            })
        elif not state["is_stale"]:
            # First detection of staleness for this period — trigger once only
            state["is_stale"] = True
            state["last_event_ts"] = timestamp
            elapsed: timedelta = timestamp - state["start_ts"]
            self._handle_stale_event(transport_id, timestamp, elapsed)


    def _handle_stale_event(self, transport_id: str, current_time: datetime, total_stale_elapsed: timedelta) -> None:
        """
        Fires when a transport's data is detected as stale. Triggers an upstream
        reconnect via the gateway callback (if wired) up to max_stale_attempts
        times, with a minimum of retry_delay_mins between attempts.
        Sends a push notification on each attempt.
        """
        state: StaleRegistryState | None = self._stale_registry.get(transport_id)
        if not state:
            return

        # Cap reconnect attempts per stale period
        if state["stale_event_count"] >= self.max_stale_attempts:
            self._log.debug(f"[{transport_id}] InfluxDB3_Out: Max stale retry attempts reached. No further reconnects.")
            return

        # Throttle: enforce minimum gap between attempts
        if state["last_event_ts"] is not None:
            time_since_last: timedelta = current_time - state["last_event_ts"]
            if time_since_last < timedelta(minutes=self.retry_delay_mins):
                return

        state["stale_event_count"] += 1
        state["last_event_ts"] = current_time

        # Trigger upstream reconnect via gateway callback
        if self.request_upstream_reconnect:
            try:
                self._log.warning(
                    f"[{transport_id}] InfluxDB3_Out: Data stale. Requesting reconnect "
                    f"(Attempt {state['stale_event_count']}/{self.max_stale_attempts})."
                )
                self.request_upstream_reconnect(transport_id)
            except Exception:
                self._log.exception(f"[{transport_id}] InfluxDB3_Out: Failed requesting upstream reconnect.")

        # Push notification
        try:
            minutes: float = total_stale_elapsed.total_seconds() / 60
            self.send_message(
                message=(
                    f"InfluxDB3_Out: Transport [{transport_id}] stale for {minutes:.1f} mins. "
                    f"Attempt {state['stale_event_count']} of {self.max_stale_attempts}."
                ),
                title="MPG Stale Data Alert",
                priority=1,
            )
        except Exception:
            self._log.exception(f"[{transport_id}] Failed sending stale data notification.")

    def _log_batch_debug(self, points: list[InfluxPoint], verb: str) -> None:
            """Emit structured debug lines for a batch of point dicts."""
            sample_field_names: list[str] = [
                "vacr", "VacR", "soc", "SOC", "fwcode", "FWCode",
                "vbat", "Vbat", "pinv", "Pinv",
            ]

            serial_numbers: list[str | None] = []
            sample_values: list[dict[str, object] | str] = []

            for point in points:
                raw_tags: object = point.get("tags", {})
                if isinstance(raw_tags, dict):
                    tags: dict[str, str] = cast(dict[str, str], raw_tags)
                else:
                    tags = {}
                serial_numbers.append(tags.get("device_serial_number", None))

                raw_fields: object = point.get("fields", {})
                if isinstance(raw_fields, dict):
                    fields: dict[str, int | float | str] = cast(dict[str, int | float | str], raw_fields)
                else:
                    fields = {}

                sample_data: dict[str, object] = {k: fields[k] for k in sample_field_names if k in fields}

                if sample_data:
                    sample_values.append(sample_data)
                elif fields:
                    sample_values.append(f"No sample fields found. Available fields: {list(fields.keys())[:10]}")
                else:
                    sample_values.append("No fields found")

            serial_str: str = ",".join(s for s in serial_numbers if s is not None)
            self._log.info(f"{verb} {len(points)} points to InfluxDB (serial numbers: {serial_str})")

            for i, (serial, samples) in enumerate(zip(serial_numbers, sample_values)):
                raw_point_tags: object = points[i].get("tags", {})
                if isinstance(raw_point_tags, dict):
                    point_tags: dict[str, str] = cast(dict[str, str], raw_point_tags)
                else:
                    point_tags = {}

                transport_name: str = point_tags.get("transport", "unknown")
                self._log.debug(f"Point {i+1} tags: {point_tags}")

                if isinstance(samples, dict):
                    sample_str: str = ",".join(f"{k}={v}" for k, v in samples.items())
                    self._log.debug(f"Point {i+1} ({serial}) from {transport_name}: {sample_str}")
                else:
                    self._log.debug(f"Point {i+1} ({serial}) from {transport_name}: {samples}")

    def _process_and_store_data(self, data: DataPayload, from_transport: transport_base) -> None:
        """Build a point and place it in the persistent backlog (offline path)."""
        if not self.enable_persistent_storage:
            self._log.warning("Persistent storage disabled, data will be lost")
            return

        point: InfluxPoint = self._create_point_dict(data, from_transport)
        self._add_to_backlog(point)

        should_flush: bool = False
        with self._batch_lock:
            self.batch_points.append(point)
            current_time: float = time.time()
            if (len(self.batch_points) >= self.batch_size or
                    (current_time - self.last_batch_time) >= self.batch_timeout):
                should_flush = True

        if should_flush:
            self._flush_batch()

    def _process_and_write_data(self, data: DataPayload, from_transport: transport_base) -> None:
        """Build a point and add it to the write batch (online path)."""
        point: InfluxPoint = self._create_point_dict(data, from_transport)

        should_flush: bool = False
        with self._batch_lock:
            self.batch_points.append(point)
            current_time: float = time.time()
            if (len(self.batch_points) >= self.batch_size or
                    (current_time - self.last_batch_time) >= self.batch_timeout):
                should_flush = True

        if should_flush:
            self._flush_batch()

    def _flush_batch(self) -> None:
        """Flush the current batch of points to InfluxDB v3."""
        with self._batch_lock:
            if not self.batch_points:
                return
            points_to_write: list[InfluxPoint] = self.batch_points.copy()
            self.batch_points = []

        if not self._check_connection():
            self._log.warning("Not connected to InfluxDB v3, storing batch in backlog")
            for point in points_to_write:
                self._add_to_backlog(point)
            return

        influx_points: list[Point] = [self._dict_to_influx3_point(p) for p in points_to_write]

        try:
            if self.client is not None:
                self.client.write(record=influx_points, database=self.database) # type: ignore[reportUnknownMemberType]

            if self._log.isEnabledFor(logging.DEBUG):
                self._log_batch_debug(points_to_write, "Wrote")
            else:
                self._log.info(f"Wrote {len(points_to_write)} points to InfluxDB v3")

            self.last_batch_time = time.time()

        except Exception as e:
            self._log.error(f"Failed to write batch to InfluxDB v3: {e}")
            if self._attempt_reconnect():
                try:
                    if self.client is not None:
                        self.client.write(record=influx_points, database=self.database) # type: ignore[reportUnknownMemberType]

                    if self._log.isEnabledFor(logging.DEBUG):
                        self._log_batch_debug(points_to_write, "Successfully wrote (after reconnect)")
                    else:
                        self._log.info(
                            f"Successfully wrote {len(points_to_write)} points to InfluxDB v3 after reconnection"
                        )

                    self.last_batch_time = time.time()

                except Exception as retry_e:
                    self._log.error(f"Failed to write batch after reconnection: {retry_e}")
                    for point in points_to_write:
                        self._add_to_backlog(point)
                    self.connected = False
            else:
                for point in points_to_write:
                    self._add_to_backlog(point)
                self.connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init_bridge(self, from_transport: transport_base) -> None:
        """Initialize bridge — not needed for InfluxDB output."""
        pass

    def close(self) -> None:
        """
        Gracefully terminate the connection.
        Flushes pending metric batches and closes persistent network sockets.
        """
        self._log.info("Closing InfluxDB v3 transport bridge...")

        if getattr(self, "batch_points", None):
            try:
                self._flush_batch()
            except Exception as e:
                self._log.error(f"Failed to flush batch during explicit close: {e}")

        session: requests.Session | None = getattr(self, "session", None)
        if session is not None:
            try:
                session.close()
                self._log.debug("Diagnostic HTTP session closed successfully.")
            except Exception as e:
                self._log.debug(f"Error closing diagnostic session: {e}")
            finally:
                self.session = None

        client: InfluxDBClient3 | None = getattr(self, "client", None)
        if client is not None:
            try:
                if hasattr(client, "close"):
                    client.close()
                    self._log.debug("InfluxDB client connection closed.")
            except Exception as e:
                self._log.warning(f"Error during client connection close: {e}")
            finally:
                self.client = None

        self.connected = False
        self._log.info("InfluxDB v3 transport bridge closed cleanly.")


    def __del__(self) -> None:
        try:
            if hasattr(self, "close") and callable(getattr(self, "close", None)):
                self.close()
        except Exception as e:
            if hasattr(self, '_log'):
                try:
                    self._log.error(f"Exception in __del__: {e}")
                except Exception:
                    self._log.error(f"Exception in __del__: {e}")


# =============================================================================
# Metrics Edit -- read/edit admin operations for InfluxDB v3, the v3
# counterpart of InfluxDB v1's InfluxV1AdminManager (classes/transports/
# influxdb_out.py) and TimescaleDB's BridgeAdminManager (classes/transports/
# timescaledb.py). Lives in this module (not the web layer) per the same
# separation those two use: routers/influxdb.py and services/
# influxdb_service.py only orchestrate HTTP/staging concerns; every actual
# InfluxDB v3 read/write happens here, against a live influxdb3_out bridge
# instance, via SQL (DataFusion) rather than InfluxQL.
#
# InfluxDB 3 Core has NO row-level or field-level DELETE at all as of this
# writing. "Delete value(s)" here uses InfluxDB 3 ENTERPRISE's row-delete
# API (POST /api/v3/row_delete_requests, the same request the `influxdb3
# delete rows` CLI command submits) -- an Enterprise-only feature that
# requires the upgraded storage engine (--use-pacha-tree /
# --upgrade-pacha-tree). SUPPORTS_DELETE is True here (this class always
# offers the action; the ENTERPRISE REQUIREMENT is enforced by the server
# itself, not pre-checked client-side) -- routers/influxdb.py shows
# "Delete value(s)" for v3 the same as v1, and any failure (wrong edition,
# missing permission, storage engine not upgraded) surfaces as a clear
# error from _delete_rows_via_enterprise_api rather than being hidden.
#
# Three things make this delete meaningfully different from a v1 InfluxQL
# DELETE, and both are surfaced to the admin (see edit_metric_values'
# docstring and the UI warning in pages/influxdb_metrics_edit.html):
#   1. Whole-row only: the row-delete predicate supports tag equality only
#      (AND-combined, no OR/NOT/IN, no field columns) -- exactly like v1's
#      InfluxQL DELETE, this removes the ENTIRE row (every field) at each
#      matching timestamp, never just the one field chosen in the UI.
#   2. Asynchronous: submitting the request only records it. The
#      compactor applies it later -- by default, up to 24 hours afterward
#      (tunable server-side via --pt-row-delete-min-age) -- and the
#      targeted rows remain queryable in the meantime. This is nothing
#      like a v1 InfluxQL DELETE or a database DELETE statement, which
#      take effect (at least locally) as soon as they return.
#   3. Enterprise + upgraded storage engine only: InfluxDB 3 Core, or an
#      Enterprise cluster that hasn't run the storage engine upgrade,
#      rejects the request outright; there is no client-side way to
#      detect this in advance, so a clear server error is surfaced
#      instead of pre-flighting it.
#
# "Editing" a value (the "set_value" action, unaffected by any of the
# above) means the same thing it does for v1: SQL SELECT * the matching
# rows (capturing each row's full original tag set and exact timestamp),
# then re-write ONE Point per row containing that same tag set, timestamp,
# and ONLY the corrected field. InfluxDB 3's storage engine merges writes
# per-column at the same (tag-set, timestamp) primary key, the same
# last-write-wins-per-field behavior InfluxDB v1's TSM engine has, so
# every other field already stored at that timestamp is left untouched.
# This part is synchronous and ordinary SQL/write-API traffic -- it works
# identically on Core and Enterprise.
# =============================================================================

# The six tags every influxdb3_out point carries (see _build_tags above) --
# used by list_metric_edit_fields to exclude tag columns from the editable
# field list (information_schema.columns has no separate "is this a tag"
# flag to query instead), and by edit_metric_values to know which columns
# of a fetched row to re-attach as tags rather than treat as a field.
_INFLUX3_TAG_NAMES: frozenset[str] = frozenset({
    "device_identifier", "device_name", "device_manufacturer",
    "device_model", "device_serial_number", "transport",
})

# Arrow/DataFusion data_type strings (as reported by information_schema.
# columns) bucketed by kind -- used only by _coerce_v3_field_value to
# basic-validate a Metrics Edit replacement value before it's written.
_INFLUX3_INTEGER_TYPES: frozenset[str] = frozenset({
    "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64",
})
_INFLUX3_FLOAT_TYPES: frozenset[str] = frozenset({"Float16", "Float32", "Float64"})


def _sql_quote_ident(name: str) -> str:
    """
    Double-quotes a SQL identifier (table/column name) for DataFusion SQL,
    escaping any embedded double quote by doubling it -- the SQL-dialect
    counterpart of InfluxQL's quote_ident (see influxdb_out.py, and
    InfluxDBClient3.query() has no bind_params support at all to lean on
    instead, unlike the v1 client).
    """
    return '"' + name.replace('"', '""') + '"'


def _sql_quote_literal(value: str) -> str:
    """
    Single-quotes a SQL string literal for DataFusion SQL, escaping any
    embedded single quote by doubling it -- the SQL-dialect counterpart of
    InfluxQL's quote_literal. Used for every value (tag values, mainly)
    that has to be inlined into a query string, since InfluxDBClient3.
    query() offers no parameterized-query mechanism.
    """
    return "'" + value.replace("'", "''") + "'"


def _sql_timestamp_literal(value: datetime) -> str:
    """
    Formats a datetime as a DataFusion TIMESTAMP literal, normalized to UTC
    first so the literal is unambiguous regardless of the server's session
    timezone.
    """
    return f"CAST({_sql_quote_literal(value.astimezone(timezone.utc).isoformat())} AS TIMESTAMP)"


def _coerce_v3_field_value(value: object, data_type: str | None) -> float | int | str | bool:
    """
    Basic validation/coercion of one admin-entered Metrics Edit replacement
    value against an InfluxDB v3 field's reported Arrow data_type (from
    information_schema.columns) before Influx3AdminManager.
    edit_metric_values() writes it. A value that doesn't fit the reported
    type is rejected outright rather than silently reinterpreted, since
    this writes directly to historical data with no further review step
    once committed.

    data_type=None (a column information_schema.columns didn't report,
    which shouldn't normally happen for a field the admin picked from that
    same listing) is treated permissively: numeric-looking input becomes a
    float, anything else is kept as a string.

    Returns:
        The value coerced to the Python type that matches data_type.

    Raises:
        ValueError: value doesn't parse as the reported type, or an
                    integer type's value isn't a whole number.
    """
    if data_type == "Boolean":
        if isinstance(value, bool):
            return value
        text_val: str = str(value).strip().lower()
        if text_val in ("true", "t", "1", "yes", "on"):
            return True
        if text_val in ("false", "f", "0", "no", "off"):
            return False
        msg: str = f"'{value}' is not a valid boolean for a BOOLEAN field (use true/false)."
        raise ValueError(msg)

    if data_type is None:
        try:
            return float(cast(Any, value))
        except (TypeError, ValueError):
            return str(value)

    if data_type in _INFLUX3_INTEGER_TYPES or data_type in _INFLUX3_FLOAT_TYPES:
        try:
            parsed: float = float(cast(Any, value))
        except (TypeError, ValueError):
            msg = f"'{value}' is not a valid number for a {data_type} field."
            raise ValueError(msg) from None

        if data_type in _INFLUX3_INTEGER_TYPES:
            if not parsed.is_integer():
                msg = f"'{value}' is not a whole number, but this field is {data_type}."
                raise ValueError(msg)
            return int(parsed)
        return parsed

    # Utf8, Dictionary(...), or anything else not recognized as numeric -- text.
    return str(value)


@dataclass
class Influx3MetricEditDevice:
    """One device with existing rows in a measurement, for the Metrics Edit device picker."""
    device_identifier: str
    device_name: str | None


@dataclass
class Influx3MetricEditField:
    """One editable field column in a measurement, for the Metrics Edit field picker."""
    name: str
    data_type: str | None  # Arrow type string, e.g. "Float64", "Int64", "Utf8", "Boolean"


@dataclass
class Influx3ValueSample:
    """One matched (time, value) pair for the given field, for the Metrics Edit preview."""
    time_iso: str
    value: float | int | str | bool | None


@dataclass
class Influx3EditPreview:
    """Read-only preview of what an Influx3AdminManager.edit_metric_values() call would affect."""
    row_count: int
    sample: list[Influx3ValueSample]


@dataclass
class Influx3EditResult:
    """
    Outcome of an Influx3AdminManager.edit_metric_values() call.

    For "set_value": points_affected is the exact number of rows
    rewritten, synchronously, by the time this returns -- field_name and
    new_value are always set.

    For "delete": points_affected is a best-effort SQL COUNT(*) taken
    just before submitting the row-delete request (see
    edit_metric_values), NOT a confirmation of anything actually deleted
    yet -- field_name/new_value are None (the row-delete predicate has no
    field concept; see the module-level comment), and `pending`/`sequence`
    describe the request itself: the delete is asynchronous and may not
    apply for up to 24 hours (server-configurable via
    --pt-row-delete-min-age), during which the targeted rows remain
    queryable.
    """
    measurement: str
    device_identifier: str
    action: Literal["delete", "set_value"]
    field_name: str | None
    new_value: float | int | str | bool | None
    start_time: datetime
    end_time: datetime
    points_affected: int
    pending: bool = False               # True for "delete" -- the request was accepted but not yet applied
    sequence: int | None = None         # the row-delete request's sequence number, for "delete" only


class Influx3AdminManager:
    """
    Read/edit/delete admin operations against one live influxdb3_out (v3)
    bridge's stored data, for the "InfluxDB -> Metrics Edit 3.x" admin
    screen. See the module-level comment above for what "edit" and
    "delete" each mean on InfluxDB v3 -- delete in particular behaves very
    differently from a normal DELETE statement (whole-row only,
    asynchronous, Enterprise + upgraded-storage-engine only).

    Usage:
        admin_mgr = Influx3AdminManager(bridge)
        measurements = admin_mgr.list_metric_edit_measurements()
        devices = admin_mgr.list_metric_edit_devices("device_data")
        fields = admin_mgr.list_metric_edit_fields("device_data")
        preview = admin_mgr.preview_metric_edit("device_data", "4066670074", "cap_remaining", start, end)
        result = admin_mgr.edit_metric_values(
            "device_data", "4066670074", "set_value", start, end, field_name="cap_remaining", new_value=80,
        )
        deleted = admin_mgr.edit_metric_values("device_data", "4066670074", "delete", start, end)
    """

    # True: this class always offers "Delete value(s)" (see
    # edit_metric_values / _delete_rows_via_enterprise_api) -- unlike v1,
    # where delete support is unconditional, v3's delete requires InfluxDB
    # 3 ENTERPRISE with the upgraded storage engine, which this class
    # cannot detect in advance. routers/influxdb.py still shows the
    # action; a Core server (or an un-upgraded Enterprise one) rejects the
    # request with a clear error surfaced back to the admin, rather than
    # this flag hiding the option pre-emptively.
    SUPPORTS_DELETE: bool = True

    def __init__(self, bridge: influxdb3_out) -> None:
        self._bridge: influxdb3_out = bridge
        self._log: logging.Logger = logging.getLogger(__name__)

    @property
    def _client(self) -> InfluxDBClient3:
        """The bridge's live client, or raises if the bridge isn't connected."""
        client: InfluxDBClient3 | None = self._bridge.client
        if client is None:
            raise RuntimeError("Not connected to InfluxDB -- bridge must be connected before editing metric values.")
        return client

    def _query(self, sql: str) -> pa.Table:
        """Runs one SQL query against this bridge's database and returns the raw pyarrow Table."""
        return cast(pa.Table, self._client.query(sql, database=self._bridge.database, language="sql"))  # type: ignore[reportUnknownMemberType]

    # -------------------------
    # Read-only listing for the UI
    # -------------------------

    def list_metric_edit_measurements(self) -> list[str]:
        """Returns every measurement (table) on this bridge's database, for the Metrics Edit measurement picker."""
        table: pa.Table = self._query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema NOT IN ('information_schema', 'system')"
        )
        rows: list[dict[str, object]] = table.to_pylist()
        names: set[str] = {cast(str, r["table_name"]) for r in rows if r.get("table_name")}
        return sorted(names)

    def list_metric_edit_devices(self, measurement: str) -> list[Influx3MetricEditDevice]:
        """Returns every (device_identifier, device_name) pairing recorded in `measurement`, via a plain SQL DISTINCT."""
        query: str = f"SELECT DISTINCT device_identifier, device_name FROM {_sql_quote_ident(measurement)}"  # noqa: S608
        table: pa.Table = self._query(query)
        rows: list[dict[str, object]] = table.to_pylist()

        devices: list[Influx3MetricEditDevice] = [
            Influx3MetricEditDevice(
                device_identifier=cast(str, r["device_identifier"]),
                device_name=cast(Optional[str], r.get("device_name")),
            )
            for r in rows if r.get("device_identifier")
        ]
        devices.sort(key=lambda d: d.device_identifier)
        return devices

    def list_metric_edit_fields(self, measurement: str) -> list[Influx3MetricEditField]:
        """
        Returns every editable field column in `measurement`, for the
        Metrics Edit field picker -- every information_schema.columns
        column for this table except "time" and the six known tag names
        (_INFLUX3_TAG_NAMES), since a tag defines series identity rather
        than holding an editable value (see the module-level comment).
        """
        query: str = (
            "SELECT column_name, data_type FROM information_schema.columns "  # noqa: S608
            f"WHERE table_schema NOT IN ('information_schema', 'system') AND table_name = {_sql_quote_literal(measurement)}"
        )
        table: pa.Table = self._query(query)
        rows: list[dict[str, object]] = table.to_pylist()

        fields: list[Influx3MetricEditField] = []
        for row in rows:
            column_name: str | None = cast(Optional[str], row.get("column_name"))
            data_type: str | None = cast(Optional[str], row.get("data_type"))
            if not column_name or column_name == "time" or column_name in _INFLUX3_TAG_NAMES:
                continue
            fields.append(Influx3MetricEditField(name=column_name, data_type=data_type))

        fields.sort(key=lambda f: f.name)
        return fields

    def preview_metric_edit(
        self,
        measurement: str,
        device_identifier: str,
        field_name: str,
        start_time: datetime,
        end_time: datetime,
        sample_limit: int = 25,
        ) -> Influx3EditPreview:
        """
        Read-only count + small sample of what an edit_metric_values()
        call with the same arguments would affect -- lets the Metrics Edit
        screen show the admin what's about to change before they stage it.
        Never writes anything.

        measurement/field_name are embedded via _sql_quote_ident and must
        already be validated against list_metric_edit_measurements()/
        list_metric_edit_fields() by the caller; device_identifier is
        embedded via _sql_quote_literal -- InfluxDBClient3.query() has no
        parameterized-query mechanism to prefer instead (unlike the v1
        client's bind_params).

        Raises:
            ValueError: end_time before start_time.
        """
        if end_time < start_time:
            raise ValueError("End time must not be before start time.")

        quoted_table: str = _sql_quote_ident(measurement)
        quoted_field: str = _sql_quote_ident(field_name)
        device_lit: str = _sql_quote_literal(device_identifier)
        start_lit: str = _sql_timestamp_literal(start_time)
        end_lit: str = _sql_timestamp_literal(end_time)
        where_clause: str = f"WHERE device_identifier = {device_lit} AND time >= {start_lit} AND time <= {end_lit}"

        count_table: pa.Table = self._query(
            f"SELECT COUNT({quoted_field}) AS row_count FROM {quoted_table} {where_clause}"  # noqa: S608
        )
        count_rows: list[dict[str, object]] = count_table.to_pylist()
        row_count: int = int(cast(Any, count_rows[0].get("row_count")) or 0) if count_rows else 0

        sample_table: pa.Table = self._query(
            f"SELECT time, {quoted_field} FROM {quoted_table} {where_clause} "  # noqa: S608
            f"ORDER BY time DESC LIMIT {int(sample_limit)}"
        )
        sample_rows: list[dict[str, object]] = sample_table.to_pylist()
        sample: list[Influx3ValueSample] = [
            Influx3ValueSample(time_iso=self._row_time_to_iso(row.get("time")), value=cast(Any, row.get(field_name)))
            for row in sample_rows
        ]

        return Influx3EditPreview(row_count=row_count, sample=sample)

    def _row_time_to_iso(self, row_time: object) -> str:
        """
        Formats one row's "time" column (a Python datetime by the time
        pyarrow's to_pylist() hands it here) as an ISO 8601 string in this
        bridge's own configured machine_timezone -- "UTC" if
        use_utc_timestamp is set, otherwise the local zone influxdb3_out
        itself stamps every written point's time with (see
        influxdb3_out.__init__). Using anything else here (the row's own
        UTC-by-default Arrow timestamp, unconverted, say) would show
        preview timestamps in a different zone than the Start/End range
        the admin just typed, which routers/influxdb.py's
        _parse_local_datetime() interprets in this exact same
        machine_timezone.

        Arrow's timestamp -> Python datetime conversion normally produces
        a tz-aware value (UTC), but a naive one is treated as UTC first
        (rather than raising) since that's the only timezone IOx itself
        ever stores time in internally.
        """
        if not isinstance(row_time, datetime):
            return str(row_time) if row_time is not None else ""
        aware: datetime = row_time if row_time.tzinfo is not None else row_time.replace(tzinfo=timezone.utc)
        return aware.astimezone(ZoneInfo(self._bridge.machine_timezone)).isoformat()

    # -------------------------
    # Edit (no delete -- see SUPPORTS_DELETE / module-level comment)
    # -------------------------

    def edit_metric_values(
        self,
        measurement: str,
        device_identifier: str,
        action: Literal["delete", "set_value"],
        start_time: datetime,
        end_time: datetime,
        field_name: str | None = None,
        new_value: object = None,
        ) -> Influx3EditResult:
        """
        Edits or deletes existing rows for one device over [start_time,
        end_time], within `measurement`.

          - "set_value": queries every matching row via SELECT * (capturing
            each row's full original tag values and exact timestamp), then
            re-writes ONE Point per row containing that same tag set, the
            original timestamp, and ONLY the corrected field -- InfluxDB
            3's storage engine merges writes per-column at the same
            (tag-set, timestamp) key, so every other field already stored
            at that timestamp is left untouched. Synchronous: every
            rewritten point has been written by the time this returns.
          - "delete": submits an InfluxDB 3 ENTERPRISE row-delete request
            (see _delete_rows_via_enterprise_api and the module-level
            comment) scoped to device_identifier and the time range. Not
            available on InfluxDB 3 Core, or on an Enterprise cluster
            without the storage engine upgrade -- the server rejects the
            request and this raises RuntimeError with the server's own
            error message. Removes the ENTIRE row (every field) at each
            matching timestamp, never just one field -- field_name/
            new_value are ignored for this action. Asynchronous: a
            successful call here only means the request was accepted, not
            that anything has been deleted yet (up to 24 hours by
            default) -- see Influx3EditResult.pending.

        Timestamp round-tripping note (applies to "set_value"): pyarrow's
        to_pylist() converts a nanosecond-precision Arrow timestamp to a
        Python datetime, which only holds microsecond precision -- any
        true sub-microsecond component would be lost on round-trip. This
        is a no-op in practice for this application specifically:
        influxdb3_out's own write path (_create_point_dict) only ever
        derives a point's timestamp from Python's own datetime.timestamp(),
        which is already microsecond-precision at the source -- there is
        no sub-microsecond information to lose for data this bridge wrote
        itself. A row written by some other client with genuine
        sub-microsecond precision would not round-trip exactly and could
        land as a near-duplicate point rather than a true overwrite.

        Args:
            measurement: the measurement to edit.
            device_identifier: the device (via its device_identifier tag)
                        whose rows are being edited.
            action: "delete" or "set_value".
            start_time / end_time: inclusive bounds on time.
            field_name: required for "set_value" -- must be one of
                        list_metric_edit_fields(measurement)'s names;
                        ignored for "delete".
            new_value: required for "set_value"; ignored for "delete".

        Returns:
            Influx3EditResult summarizing what happened.

        Raises:
            ValueError: end_time before start_time, an unknown action,
                        "set_value" without field_name/new_value, or
                        new_value doesn't fit the field's reported type.
            RuntimeError: bridge isn't connected (see the _client
                        property), or (for "delete") the row-delete
                        request was rejected by the server -- commonly
                        because it's InfluxDB 3 Core, or an Enterprise
                        cluster without the storage engine upgrade.
            Exception: any other failure querying/writing is logged and
                        re-raised.
        """
        if end_time < start_time:
            raise ValueError("End time must not be before start time.")
        if action not in ("delete", "set_value"):
            msg: str = f"Unknown action '{action}' -- expected 'delete' or 'set_value'."
            raise ValueError(msg)
        if action == "set_value":
            if not field_name:
                raise ValueError("field_name is required when action is 'set_value'.")
            if new_value is None:
                raise ValueError("new_value is required when action is 'set_value'.")

        try:
            if action == "delete":
                row_count: int = self._count_rows_in_range(measurement, device_identifier, start_time, end_time)
                response: dict[str, Any] = self._delete_rows_via_enterprise_api(
                    measurement, device_identifier, start_time, end_time
                )
                sequence: int | None = cast(Optional[int], response.get("sequence") or response.get("sequence_id"))

                self._log.info(
                    f"Influx3AdminManager: submitted an Enterprise row-delete request (every field) for device "
                    f"'{device_identifier}' in '{measurement}' over [{start_time}, {end_time}] -- "
                    f"~{row_count} row(s), sequence={sequence}. This is asynchronous and may take up to "
                    "24 hours to actually apply."
                )
                return Influx3EditResult(
                    measurement=measurement, device_identifier=device_identifier, action=action,
                    field_name=None, new_value=None, start_time=start_time, end_time=end_time,
                    points_affected=row_count, pending=True, sequence=sequence,
                )

            # action == "set_value" -- field_name/new_value already validated non-empty above.
            resolved_field_name: str = cast(str, field_name)
            data_type: str | None = self._lookup_field_type(measurement, resolved_field_name)
            coerced_value: float | int | str | bool = _coerce_v3_field_value(new_value, data_type)

            quoted_table: str = _sql_quote_ident(measurement)
            device_lit: str = _sql_quote_literal(device_identifier)
            start_lit: str = _sql_timestamp_literal(start_time)
            end_lit: str = _sql_timestamp_literal(end_time)
            fetch_query: str = (
                f"SELECT * FROM {quoted_table} "  # noqa: S608
                f"WHERE device_identifier = {device_lit} AND time >= {start_lit} AND time <= {end_lit}"
            )
            fetch_table: pa.Table = self._query(fetch_query)
            rows: list[dict[str, object]] = fetch_table.to_pylist()

            points: list[Point] = []
            for row in rows:
                row_time: object = row.get("time")
                if not isinstance(row_time, datetime):
                    continue
                point: Point = Point(measurement)
                for tag_name in _INFLUX3_TAG_NAMES:
                    tag_val: object = row.get(tag_name)
                    if tag_val is not None:
                        point = point.tag(tag_name, str(tag_val))  # type: ignore[reportUnknownMemberType]
                point = point.field(resolved_field_name, coerced_value)  # type: ignore[reportUnknownMemberType]
                point = point.time(int(row_time.timestamp() * 1e9))  # type: ignore[reportUnknownMemberType]
                points.append(point)

            if points:
                self._client.write(record=points, database=self._bridge.database)  # type: ignore[reportUnknownMemberType]

            self._log.info(
                f"Influx3AdminManager: set '{resolved_field_name}' = {coerced_value!r} for device "
                f"'{device_identifier}' in '{measurement}' over [{start_time}, {end_time}] -- "
                f"{len(points)} point(s) rewritten."
            )
            return Influx3EditResult(
                measurement=measurement, device_identifier=device_identifier, action=action,
                field_name=resolved_field_name, new_value=coerced_value, start_time=start_time, end_time=end_time,
                points_affected=len(points),
            )

        except Exception as e:
            self._log.error(
                f"Influx3AdminManager.edit_metric_values failed for '{measurement}' device "
                f"'{device_identifier}': {e}"
            )
            raise

    def _count_rows_in_range(
        self, measurement: str, device_identifier: str, start_time: datetime, end_time: datetime
        ) -> int:
        """
        Best-effort SQL COUNT(*) of rows matching device_identifier in
        [start_time, end_time] -- used to report how many rows a "delete"
        is about to affect, since the row-delete request itself is
        asynchronous and reports no immediate count.
        """
        quoted_table: str = _sql_quote_ident(measurement)
        device_lit: str = _sql_quote_literal(device_identifier)
        start_lit: str = _sql_timestamp_literal(start_time)
        end_lit: str = _sql_timestamp_literal(end_time)
        query: str = (
            f"SELECT COUNT(*) AS row_count FROM {quoted_table} "  # noqa: S608
            f"WHERE device_identifier = {device_lit} AND time >= {start_lit} AND time <= {end_lit}"
        )
        try:
            table: pa.Table = self._query(query)
            rows: list[dict[str, object]] = table.to_pylist()
            return int(cast(Any, rows[0].get("row_count")) or 0) if rows else 0
        except Exception as e:
            self._log.warning(f"_count_rows_in_range: COUNT(*) failed for '{measurement}': {e}")
            return 0

    def _delete_rows_via_enterprise_api(
        self, table: str, device_identifier: str, start_time: datetime, end_time: datetime
        ) -> dict[str, Any]:
        """
        Submits an InfluxDB 3 ENTERPRISE row-delete request: POST
        /api/v3/row_delete_requests -- the same request the `influxdb3
        delete rows` CLI command submits. Not available on InfluxDB 3
        Core, and requires the upgraded storage engine
        (--use-pacha-tree/--upgrade-pacha-tree) on Enterprise.

        Not wrapped by InfluxDBClient3 -- its SDK exposes only query()/
        write() as of this writing -- so the request is built and sent
        directly with `requests`, reusing the bridge's own diagnostic
        `self.session` when available (same fallback-to-bare-`requests`
        pattern _probe_heap_profile() above already uses for endpoints
        the SDK doesn't cover) but with an explicit "Bearer" Authorization
        header for this call, matching the documented convention for
        InfluxDB 3's `/api/v3/*` management endpoints specifically (the
        session's own default "Token" header is a v1/v2-compatibility
        convention used elsewhere, not for this endpoint).

        The delete predicate supports tag equality only -- scoped here to
        device_identifier alone, which is sufficient to target one
        device's rows; there is no field predicate at all, so this always
        removes every field at each matching row (see the module-level
        comment). max_time is EXCLUSIVE per the API, so it's nudged one
        nanosecond past end_time to keep this call's own [start_time,
        end_time] contract inclusive on both ends, matching every other
        edit_metric_values() implementation in this codebase.

        Returns:
            The parsed JSON response body (includes "sequence", the
            request's tracking number) -- {} if the response had no body.

        Raises:
            RuntimeError: the request could not be sent (network failure),
                        or the server rejected it -- commonly because the
                        connected server is InfluxDB 3 Core, or an
                        Enterprise cluster without the storage engine
                        upgrade, or the token lacks delete permission on
                        this database. The response body/status is
                        included in the message to help distinguish these.
        """
        # host/port joined the same way influxdb3_out._endpoint_url does
        # internally (that property is private to the bridge class, so
        # this reconstructs the join here rather than reaching into it).
        host_url: str = f"{self._bridge.host}:{self._bridge.port}" if self._bridge.port else self._bridge.host
        url: str = f"{host_url}/api/v3/row_delete_requests"
        escaped_device_id: str = device_identifier.replace("'", "''")
        min_time_ns: int = int(start_time.timestamp() * 1e9)
        max_time_ns: int = int(end_time.timestamp() * 1e9) + 1  # max_time is exclusive; nudge past end_time
        body: dict[str, Any] = {
            "db": self._bridge.database,
            "table": table,
            "delete_predicate": f"device_identifier = '{escaped_device_id}'",
            "min_time": min_time_ns,
            "max_time": max_time_ns,
        }
        headers: dict[str, str] = {"Authorization": f"Bearer {self._bridge.token}", "Content-Type": "application/json"}
        session: requests.Session = cast(requests.Session, getattr(self._bridge, "session", None) or requests)

        try:
            resp: requests.Response = session.post(
                url, json=body, headers=headers, timeout=self._bridge.connection_timeout
            )
        except requests.exceptions.RequestException as e:
            msg = f"Could not reach the InfluxDB 3 row-delete API at {url}: {e}"
            raise RuntimeError(msg) from e

        if resp.status_code not in (200, 202):
            msg = (
                f"InfluxDB 3 row-delete request failed (HTTP {resp.status_code}): {resp.text[:500]} -- "
                "this requires InfluxDB 3 ENTERPRISE with the storage engine upgrade "
                "(--use-pacha-tree/--upgrade-pacha-tree) and db:<database>:delete permission; "
                "it is never available on InfluxDB 3 Core."
            )
            raise RuntimeError(msg)

        try:
            return cast(dict[str, Any], resp.json())
        except ValueError:
            return {}

    def _lookup_field_type(self, measurement: str, field_name: str) -> str | None:
        """Best-effort lookup of one field's reported Arrow data_type, for _coerce_v3_field_value. None if not found."""
        for field in self.list_metric_edit_fields(measurement):
            if field.name == field_name:
                return field.data_type
        return None

    def validate_metric_edit_value(
        self, measurement: str, field_name: str | None, action: Literal["delete", "set_value"], new_value: object
        ) -> None:
        """
        Read-only type-check of a "set_value" edit's replacement value
        before it's staged -- looks up field_name's reported Arrow
        data_type and runs it through the same _coerce_v3_field_value()
        edit_metric_values() will run at commit time, without querying or
        writing any row data. A no-op for action == "delete" (no value to
        validate -- and no way to pre-validate that the connected server
        even supports it; see SUPPORTS_DELETE / the module-level comment).
        Purely advisory: edit_metric_values() re-validates again at commit
        time regardless, since the field's reported type can still change
        between staging and commit.

        Raises:
            ValueError: action == "set_value" with no field_name/new_value,
                        or new_value doesn't fit the field's reported type.
        """
        if action != "set_value":
            return
        if not field_name:
            raise ValueError("field_name is required.")
        if new_value is None:
            raise ValueError("new_value is required.")
        data_type: str | None = self._lookup_field_type(measurement, field_name)
        _coerce_v3_field_value(new_value, data_type)
