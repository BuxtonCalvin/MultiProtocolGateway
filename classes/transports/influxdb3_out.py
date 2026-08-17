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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, TypedDict, cast
from urllib.parse import urlsplit

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

        scheme = "http"
        for candidate in ("http", "https"):
            prefix: str = f"{candidate}://"
            if host.startswith(prefix):
                scheme = candidate
                host = host[len(prefix):]
                break

        # Parsed via urlsplit (rather than a hand-rolled rsplit on ":") so
        # IPv6 literals like "[::1]:8181" are split into hostname/port
        # correctly instead of on every colon.
        parsed = urlsplit(f"//{host}")
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
