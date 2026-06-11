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
from __future__ import annotations

import logging
import math
import pickle
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, cast

import pyarrow as pa
import requests
from influxdb_client_3 import InfluxDBClient3, Point
from tzlocal import get_localzone_name

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings, strtobool

from ..protocol_settings import Data_Type, Registry_Type
from .transport_base import transport_base

# Type alias for the data payload shared across all write methods
DataPayload = dict[str, int | float | str]

# Type alias for a serializable InfluxDB point dict (including the optional
# internal '_backlog_time' sentinel used for age-based eviction)
InfluxPoint = dict[str, object]


class influxdb3_out(transport_base):
    transport_type = "bridge"
    """InfluxDB v3 output transport that writes solar metrics to an InfluxDB v3 server."""

    # ------------------------------------------------------------------
    # Class-level attribute declarations (overridden in __init__)
    # ------------------------------------------------------------------
    host: str = "https://us-east-1-1.aws.cloud2.influxdata.com"
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

        # This explicitly checks: "Does this object actually have the methods I need?"
        if not isinstance(settings, TransportSettings):
            msg: str = f"Provided settings object {type(settings)} is missing required methods!"
            raise TypeError(msg)
        super().__init__(settings)

        self.host = settings.get("host", fallback=self.host)
        self.mgmt_api_url: str = settings.get("mgmt_api_url", fallback=f"{self.host}/api/v3/databases")
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
        self._stale_registry: dict[str, dict[str, Any]] = {}

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
                self.client.write(record=points_to_send, database=self.database)
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
        """Initialize the InfluxDB v3 client connection."""
        self._log.info("influxdb3_out connect")

        try:
            self.client = InfluxDBClient3(**self._build_client_kwargs())

            # Query system table to check if it exists
            databases_table: pa.Table = self.client.query("SELECT database_name FROM system.databases")
            # Convert the Apache Arrow table column to a flat Python list
            existing_databases = databases_table["database_name"].to_pylist()

            if self.database not in existing_databases:
                if not self.auto_create_database:
                    self._log.warning(
                        f"Database '{self.database}' not found and auto_create_database=false. "
                        f"Create it manually or set auto_create_database=true."
                    )
                else:
                    self._create_database()

            # InfluxDB v3 has no ping(); perform a lightweight health check via a
            # no-op query so we surface auth / connectivity problems early.
            self._health_check()

        except Exception as e:
            self._log.error(f"Failed to connect to InfluxDB: {e}")
            self.connected = False
            return False
        else:
            # This runs only if the try block succeeds perfectly
            self.connected = True
            self.last_connection_check = time.time()
            self.last_periodic_reconnect_attempt = time.time()
            self._log.info(f"Connected to InfluxDB v3 at {self.host}, database={self.database}")

            if self.enable_persistent_storage:
                self._flush_backlog()

        return True

    def _build_client_kwargs(self) -> dict[str, Any]:
        """
        Builds the keyword arguments for InfluxDBClient3 construction.
        org is omitted entirely for self-hosted IOx deployments since it
        is not used and some client versions reject a None value.
        Only included when explicitly set in config for Cloud Dedicated
        or Cloud Serverless deployments that require it.
        """
        kwargs: dict[str, Any] = {
            "host":     self.host,
            "token":    self.token,
            "database": self.database,
        }
        if self.org:
            kwargs["org"] = self.org
        return kwargs

    def _create_database(self) -> None:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        payload: dict[str, str] = {"db": self.database}
        response: requests.Response | None = None  # Explicitly bind the variable first

        try:
            response = requests.post(
                self.mgmt_api_url,
                json=payload,
                headers=headers,
                timeout=(self.connection_timeout, self.connection_timeout * 3),
            )

            if response.status_code == 409:
                self._log.info(f"Database {self.database} already exists.")
                return

            response.raise_for_status()
            self._log.info(f"Database {self.database} created successfully.")

        except requests.exceptions.HTTPError as e:
            # Check response to satisfy type-checkers that it isn't None
            status: int | Literal['Unknown'] = response.status_code if response else "Unknown"
            msg: str = f"Failed to create database {self.database}. Status: {status}. Error: {e}"
            raise RuntimeError(msg) from e

        except requests.exceptions.ConnectionError as e:
            msg: str = f"Could not connect to InfluxDB management API at {self.mgmt_api_url}. Check mgmt_api_url. Error: {e}"
            raise RuntimeError(msg) from e


    def _health_check(self) -> None:
        """Perform a lightweight query to verify connectivity and credentials.

        InfluxDB v3 does not expose a /ping endpoint, so we issue a minimal
        SQL query. An exception here propagates to the caller so they can
        handle the failure appropriately.
        """
        if self.client is None:
            raise RuntimeError("Client not initialized")
        # This query returns nothing but validates auth + network reachability.
        self.client.query(
            "SHOW TABLES LIMIT 1",
            database=self.database,
            language="sql",
        )

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
        self._log.info(f"Attempting to reconnect to InfluxDB v3 at {self.host}")

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
                f"Received data from {from_transport.transport_name} "
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
                for registry_type in [Registry_Type.INPUT, Registry_Type.HOLDING]:
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
                float_val: float = float(value)
                if self.force_float or should_force_float:
                    fields[key] = float_val
                else:
                    fields[key] = int(float_val) if float_val.is_integer() else float_val
            except (ValueError, TypeError):
                fields[key] = str(value)
                self._log.debug(f"Field {key}: {value} -> string (conversion failed)")

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
        state: dict[str, Any] | None = self._stale_registry.get(transport_id)
        if not state or state["last_row"] is None:
            return False

        for key, val in row.items():
            prev = state["last_row"].get(key)
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
            p = p.tag(tag_key, tag_val)

        for field_key, field_val in cast(dict[str, int | float | str], point_dict.get("fields", {})).items():
            p = p.field(field_key, field_val)

        if "time" in point_dict:
            p = p.time(cast(int, point_dict["time"]))
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

        state: dict[str, Any] = self._stale_registry[transport_id]
        state["last_seen"] = timestamp

        self._log.debug(
            f"Committing state for transport: {transport_id} | "
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
        state: dict[str, Any] | None = self._stale_registry.get(transport_id)
        if not state:
            return

        # Cap reconnect attempts per stale period
        if state["stale_event_count"] >= self.max_stale_attempts:
            self._log.debug(
                f"[{transport_id}] Max stale retry attempts reached. No further reconnects."
            )
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
                    f"[{transport_id}] Data stale. Requesting reconnect "
                    f"(Attempt {state['stale_event_count']}/{self.max_stale_attempts})."
                )
                self.request_upstream_reconnect(transport_id)
            except Exception:
                self._log.exception(f"[{transport_id}] Failed requesting upstream reconnect.")

        # Push notification
        try:
            minutes: float = total_stale_elapsed.total_seconds() / 60
            self.send_message(
                message=(
                    f"Transport [{transport_id}] stale for {minutes:.1f} mins. "
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
        # Allow None in the list type
        serial_numbers: list[str | None] = []
        sample_values: list[dict[str, object] | str] = []

        for point in points:
            raw_tags: object = point.get("tags", {})
            tags: dict[str, str] = raw_tags if isinstance(raw_tags, dict) else {}

            serial_numbers.append(tags.get("device_serial_number", None))
            # Fetch the raw object first
            raw_fields: object = point.get("fields", {})

            # Check the type and assign it to typed variable
            fields: dict[str, int | float | str] = raw_fields if isinstance(raw_fields, dict) else {}

            sample_data: dict[str, object] = {k: fields[k] for k in sample_field_names if k in fields}
            if sample_data:
                sample_values.append(sample_data)
            elif fields:
                sample_values.append(f"No sample fields found. Available fields: {list(fields.keys())[:10]}")
            else:
                sample_values.append("No fields found")

        # Use a fallback for serial numbers when joining strings
        serial_str: str = ", ".join(s for s in serial_numbers if s is not None)
        self._log.info(f"{verb} {len(points)} points to InfluxDB v3 (serial numbers: {serial_str})")

        for i, (serial, samples) in enumerate(zip(serial_numbers, sample_values)):
            # Extract tags safely into a local dictionary variable first
            raw_point_tags: object = points[i].get("tags", {})
            point_tags: dict[str, str] = raw_point_tags if isinstance(raw_point_tags, dict) else {}
            transport_name: str = point_tags.get("transport", "unknown")

            self._log.debug(f"Point {i+1} tags: {point_tags}")

            if isinstance(samples, dict):
                sample_str: str = ", ".join(f"{k}={v}" for k, v in samples.items())
                self._log.debug(f"  Point {i+1} ({serial}) from {transport_name}: {sample_str}")
            else:
                self._log.debug(f"  Point {i+1} ({serial}) from {transport_name}: {samples}")

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
                self.client.write(record=influx_points, database=self.database)

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
                        self.client.write(record=influx_points, database=self.database)

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

    def __del__(self) -> None:
        """Cleanup on destruction — flush any remaining points."""
        if self.batch_points:
            self._flush_batch()
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                self._log.warning(f"Cleanup exception {e}")
