# Description: Bridge module for InfluxDB v1 output transport with persistent disk backlog and connection monitoring
# File: influxdb_out.py
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

# Bridge module for InfluxDB v1 output transport with persistent disk backlog and connection monitoring
from __future__ import annotations

import logging
import pickle
import threading
import time
from pathlib import Path
from typing import Optional, cast

from influxdb import InfluxDBClient

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings, strtobool

from ..protocol_settings import Data_Type, Registry_Type
from .transport_base import transport_base

# Type alias for the data payload shared across all write methods
DataPayload = dict[str, int | float | str]

# Type alias for a serializable InfluxDB point dict (including the optional
# internal '_backlog_time' sentinel used for age-based eviction)
InfluxPoint = dict[str, object]


class influxdb_out(transport_base):

    transport_type = "bridge"
    """InfluxDB v1 output transport that writes solar metrics to an InfluxDB server."""

    # ------------------------------------------------------------------
    # Class-level attribute declarations (overridden in __init__)
    # ------------------------------------------------------------------
    host: str = "localhost"
    port: int = 8086
    database: str = "solar"
    username: str = ""
    password: str = ""
    measurement: str = "device_data"
    include_timestamp: bool = True
    include_device_info: bool = True
    batch_size: int = 100
    batch_timeout: float = 10.0
    force_float: bool = True  # Force all numeric fields to floats to avoid type conflicts

    # Connection monitoring settings
    reconnect_attempts: int = 5
    reconnect_delay: float = 5.0
    connection_timeout: int = 10

    # Exponential backoff settings
    use_exponential_backoff: bool = True
    max_reconnect_delay: float = 300.0  # 5 minutes max delay

    # Persistent storage settings
    enable_persistent_storage: bool = True
    persistent_storage_path: str = "influxdb_backlog"
    max_backlog_size: int = 10000  # Maximum number of points to store
    max_backlog_age: int = 86400  # 24 hours in seconds

    # Periodic reconnection settings
    periodic_reconnect_interval: float = 14400.0  # 4 hours in seconds

    # Runtime state — typed explicitly so mypy / pyright can track them
    client: Optional[InfluxDBClient] = None
    last_batch_time: float = 0.0
    last_connection_check: float = 0.0
    connection_check_interval: float = 300.0  # seconds
    last_periodic_reconnect_attempt: float = 0.0

    # Persistent storage runtime state
    backlog_file: Optional[Path] = None
    backlog_points: list[InfluxPoint]

    def __init__(self, settings: TransportSettings) -> None:
        # This explicitly checks: "Does this object actually have the methods I need?"
        if not isinstance(settings, TransportSettings):
            msg: str = f"Provided settings object {type(settings)} is missing required methods!"
            raise TypeError(msg)

        self.host = settings.get("host", fallback=self.host)
        self.port = settings.getint("port", fallback=self.port)
        self.database = settings.get("database", fallback=self.database)
        self.username = settings.get("username", fallback=self.username)
        self.password = settings.get("password", fallback=self.password)
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

        super().__init__(settings)

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

            self.backlog_file = storage_dir / f"influxdb_backlog_{self.transport_name}.pkl"

            self._load_backlog()

            self._log.info(f"Persistent storage initialized: {self.backlog_file}")
            self._log.info(f"Loaded {len(self.backlog_points)} points from backlog")

        except Exception as e:
            self._log.error(f"Failed to initialize persistent storage: {e}")
            self.enable_persistent_storage = False
            self.send_message(
                message="Error: Failed to initialize persistent storage for InfluxDB backlog. Check logs for details.",
                title="InfluxDB Backlog Initialization Error",
                priority=1, services=["pushover"]
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
                title="InfluxDB Backlog Save Error",
                priority=1, services=["pushover"]
            )

    def _add_to_backlog(self, point: InfluxPoint) -> None:
        """Add a point to the persistent backlog."""
        if not self.enable_persistent_storage:
            return

        point["_backlog_time"] = time.time()
        self.backlog_points.append(point)

        if len(self.backlog_points) > self.max_backlog_size:
            removed: InfluxPoint = self.backlog_points.pop(0)
            self._log.warning(f"Backlog full, removed oldest point: {removed.get('measurement', 'unknown')}")

        self._save_backlog()

    def _flush_backlog(self) -> None:
        """Write all backlog points to InfluxDB."""
        if not self.backlog_points or not self.connected:
            return

        self._log.info(f"Flushing {len(self.backlog_points)} backlog points to InfluxDB")

        try:
            points_to_send: list[InfluxPoint] = [
                {k: v for k, v in point.items() if k != "_backlog_time"}
                for point in self.backlog_points
            ]

            if self.client is not None:
                self.client.write_points(points_to_send)
                self._log.info(f"Successfully wrote {len(points_to_send)} backlog points to InfluxDB")
                self.backlog_points = []
                self._save_backlog()

        except Exception as e:
            self._log.error(f"Failed to flush backlog to InfluxDB: {e}")
            # Don't clear backlog on failure — will retry later

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Initialize the InfluxDB client connection."""
        self._log.info("influxdb_out connect")

        try:
            self.client = InfluxDBClient(
                host=self.host,
                port=self.port,
                username=self.username if self.username else "",
                password=self.password if self.password else "",
                database=self.database,
                timeout=self.connection_timeout,
            )

            self.client.ping()

            # Create database if it doesn't exist
            databases: list[dict[str, str]] = self.client.get_list_database()
            if not any(db["name"] == self.database for db in databases):
                self._log.info(f"Creating database: {self.database}")
                self.client.create_database(self.database)

            self.connected = True
            self.last_connection_check = time.time()
            self.last_periodic_reconnect_attempt = time.time()
            self._log.info(f"Connected to InfluxDB at {self.host}:{self.port}")

            if self.enable_persistent_storage:
                self._flush_backlog()

        except ImportError:
            self._log.error("InfluxDB client not installed. Please install with: pip install influxdb")
            self.connected = False
        except Exception as e:
            self._log.error(f"Failed to connect to InfluxDB: {e}")
            self.connected = False

    def _check_connection(self) -> bool:
        """Check if the connection is still alive and reconnect if necessary."""
        current_time: float = time.time()

        # Proactive periodic reconnect check
        if (self.periodic_reconnect_interval > 0 and
                current_time - self.last_periodic_reconnect_attempt >= self.periodic_reconnect_interval):

            self.last_periodic_reconnect_attempt = current_time
            self._log.info(f"Periodic reconnection check (every {self.periodic_reconnect_interval} seconds)")

            if self.connected and self.client:
                try:
                    self.client.ping()
                except Exception as e:
                    self._log.warning(f"Periodic connection check failed: {e}")
                    self.send_message(
                        message="Error: Periodic connection check failed. Check logs for details.",
                        title="InfluxDB Connection Error",
                        priority=1, services=["pushover"]
                    )
                    return self._attempt_reconnect()
            else:
                return self._attempt_reconnect()

        # Throttle routine checks to avoid excessive pings
        if current_time - self.last_connection_check < self.connection_check_interval:
            return self.connected

        self.last_connection_check = current_time

        if not self.connected or not self.client:
            return self._attempt_reconnect()

        try:
            self.client.ping()
            return True  # noqa: TRY300
        except Exception as e:
            self._log.warning(f"Connection check failed: {e}")
            self.send_message(
                message="Error: Connection check failed. Check logs for details.",
                title="InfluxDB Connection Error",
                priority=1, services=["pushover"]
            )
            return self._attempt_reconnect()

    def _attempt_reconnect(self) -> bool:
        """Attempt to reconnect to InfluxDB with exponential backoff."""
        self._log.info(f"Attempting to reconnect to InfluxDB at {self.host}:{self.port}")

        for attempt in range(self.reconnect_attempts):
            try:
                self._log.info(f"Reconnection attempt {attempt + 1}/{self.reconnect_attempts}")

                if self.client:
                    try:
                        self.client.close()
                    except Exception as e:
                        self._log.warning(f"Failed to close existing InfluxDB client during reconnect attempt: {e} {attempt + 1}/{self.reconnect_attempts}")
                        pass

                from influxdb import InfluxDBClient
                self.client = InfluxDBClient(
                    host=self.host,
                    port=self.port,
                    username=self.username if self.username else "",
                    password=self.password if self.password else "",
                    database=self.database,
                    timeout=self.connection_timeout,
                )

                self.client.ping()

                self.connected = True
                self.last_periodic_reconnect_attempt = time.time()
                self._log.info("Successfully reconnected to InfluxDB")

                if self.enable_persistent_storage:
                    self._flush_backlog()

                return True  # noqa: TRY300

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

        self._log.error(f"Failed to reconnect after {self.reconnect_attempts} attempts")
        self.send_message(
            message="Warning: Failed to reconnect InfluxDB client during reconnect attempt. Check logs for details.",
            title="InfluxDB Reconnect Warning",
            priority=1, services=["pushover"]
        )
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

        if not self._check_connection():
            self._log.warning("Not connected to InfluxDB, storing data in backlog")
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
                float_val: float = float(value)  # type: ignore[arg-type]
                if self.force_float or should_force_float:
                    fields[key] = float_val
                else:
                    fields[key] = int(float_val) if float_val.is_integer() else float_val
            except (ValueError, TypeError):
                fields[key] = str(value)
                self._log.debug(f"Field {key}: {value} -> string (conversion failed)")

        return fields

    def _create_influxdb_point(self, data: DataPayload, from_transport: transport_base) -> InfluxPoint:
        """Create an InfluxDB point dict from data and transport metadata."""
        tags: dict[str, str] = self._build_tags(from_transport)
        self._log.debug(f"Tags: {tags}")

        point: InfluxPoint = {
            "measurement": self.measurement,
            "tags": tags,
            "fields": self._build_fields(data, from_transport),
        }

        if self.include_timestamp:
            point["time"] = int(time.time() * 1e9)  # nanoseconds

        return point

    def _log_batch_debug(self, points: list[InfluxPoint], verb: str) -> None:
        """Emit structured debug lines for a batch of point dicts."""
        sample_field_names: list[str] = [
            "vacr", "VacR", "soc", "SOC", "fwcode", "FWCode", "vbat", "Vbat", "pinv", "Pinv",
        ]
        serial_numbers: list[str] = []
        sample_values: list[dict[str, object] | str] = []

        for point in points:
            tags: dict[str, str] = point.get("tags", {})  # type: ignore[assignment]
            fields: dict[str, int | float | str] = point.get("fields", {})  # type: ignore[assignment]

            serial_numbers.append(tags.get("device_serial_number", "None"))

            sample_data: dict[str, object] = {k: fields[k] for k in sample_field_names if k in fields}
            if sample_data:
                sample_values.append(sample_data)
            elif fields:
                sample_values.append(f"No sample fields found. Available fields: {list(fields.keys())[:10]}")
            else:
                sample_values.append("No fields found")

        self._log.info(f"{verb} {len(points)} points to InfluxDB (serial numbers: {', '.join(serial_numbers)})")

        for i, (serial, samples) in enumerate(zip(serial_numbers, sample_values)):
            transport_name: str = points[i].get("tags", {}).get("transport", "unknown")  # type: ignore[union-attr]
            self._log.debug(f"  Point {i+1} tags: {points[i].get('tags', {})}")
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

        point: InfluxPoint = self._create_influxdb_point(data, from_transport)
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
        point: InfluxPoint = self._create_influxdb_point(data, from_transport)

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
        """Flush the current batch of points to InfluxDB."""
        with self._batch_lock:
            if not self.batch_points:
                return
            points_to_write: list[InfluxPoint] = self.batch_points.copy()
            self.batch_points = []

        if not self._check_connection():
            self._log.warning("Not connected to InfluxDB, storing batch in backlog")
            for point in points_to_write:
                self._add_to_backlog(point)
            return

        try:
            if self.client is not None:
                self.client.write_points(points_to_write)

            if self._log.isEnabledFor(logging.DEBUG):
                self._log_batch_debug(points_to_write, "Wrote")
            else:
                self._log.info(f"Wrote {len(points_to_write)} points to InfluxDB")

            self.last_batch_time = time.time()

        except Exception as e:
            self._log.error(f"Failed to write batch to InfluxDB: {e}")
            if self._attempt_reconnect():
                try:
                    if self.client is not None:
                        self.client.write_points(points_to_write)

                    if self._log.isEnabledFor(logging.DEBUG):
                        self._log_batch_debug(points_to_write, "Successfully wrote (after reconnect)")
                    else:
                        self._log.info(f"Successfully wrote {len(points_to_write)} points to InfluxDB after reconnection")

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
