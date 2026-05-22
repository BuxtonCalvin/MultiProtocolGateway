# Bridge module for InfluxDB v3 output transport with persistent disk backlog and connection monitoring
import logging
import os
import pickle
import threading
import time

from influxdb_client_3 import InfluxDBClient3, Point

from defs.common import TransportSettings, strtobool

from ..protocol_settings import Data_Type, Registry_Type
from .transport_base import transport_base


class influxdb3_out(transport_base):
    ''' InfluxDB v3 output transport that writes data to an InfluxDB v3 server '''
    host: str = "https://us-east-1-1.aws.cloud2.influxdata.com"
    database: str = "solar"          # In v3 this is the "bucket" / database name
    token: str = ""                  # v3 uses token-based auth (replaces username/password)
    org: str = ""                    # Organization name (required for InfluxDB Cloud)
    measurement: str = "device_data"
    include_timestamp: bool = True
    include_device_info: bool = True
    batch_size: int = 100
    batch_timeout: float = 10.0
    force_float: bool = True         # Force all numeric fields to floats to avoid type conflicts

    # Connection monitoring settings
    reconnect_attempts: int = 5
    reconnect_delay: float = 5.0
    connection_timeout: int = 10

    # Exponential backoff settings
    use_exponential_backoff: bool = True
    max_reconnect_delay: float = 300.0  # 5 minutes max delay

    # Persistent storage settings
    enable_persistent_storage: bool = True
    persistent_storage_path: str = "influxdb3_backlog"
    max_backlog_size: int = 10000    # Maximum number of points to store
    max_backlog_age: int = 86400     # 24 hours in seconds

    # Periodic reconnection settings
    periodic_reconnect_interval: float = 14400.0  # 4 hours in seconds

    client = None
    last_batch_time = 0
    last_connection_check = 0
    connection_check_interval = 300  # Check connection every 300 seconds

    # Periodic reconnection tracking
    last_periodic_reconnect_attempt = 0

    # Persistent storage
    backlog_file = None
    backlog_points = []

    def __init__(self, settings: TransportSettings) -> None:
        if not isinstance(settings, TransportSettings):
            msg: str = f"Provided settings object {type(settings)} is missing required methods!"
            raise TypeError(msg)

        self.host = settings.get("host", fallback=self.host)
        self.database = settings.get("database", fallback=self.database)
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

        # Initialize instance variables for thread safety
        self.batch_points = []
        self._batch_lock = threading.Lock()

        # Initialize persistent storage
        if self.enable_persistent_storage:
            self._init_persistent_storage()

    # ------------------------------------------------------------------
    # Persistent storage helpers
    # ------------------------------------------------------------------

    def _init_persistent_storage(self) -> None:
        """Initialize persistent storage for data backlog"""
        try:
            if not os.path.exists(self.persistent_storage_path):
                os.makedirs(self.persistent_storage_path)

            self.backlog_file = os.path.join(
                self.persistent_storage_path,
                f"influxdb3_backlog_{self.transport_name}.pkl"
            )

            self._load_backlog()

            self._log.info(f"Persistent storage initialized: {self.backlog_file}")
            self._log.info(f"Loaded {len(self.backlog_points)} points from backlog")

        except Exception as e:
            self._log.error(f"Failed to initialize persistent storage: {e}")
            self.enable_persistent_storage = False

    def _load_backlog(self) -> None:
        """Load backlog points from persistent storage"""
        if not self.backlog_file or not os.path.exists(self.backlog_file):
            self.backlog_points = []
            return

        try:
            with open(self.backlog_file, 'rb') as f:
                self.backlog_points = pickle.load(f)  # noqa: S301

            # Remove points older than max_backlog_age
            current_time = time.time()
            original_count = len(self.backlog_points)
            self.backlog_points = [
                point for point in self.backlog_points
                if current_time - point.get('_backlog_time', 0) < self.max_backlog_age
            ]

            if len(self.backlog_points) < original_count:
                self._log.info(f"Cleaned {original_count - len(self.backlog_points)} old points from backlog")
                self._save_backlog()

        except Exception as e:
            self._log.error(f"Failed to load backlog: {e}")
            self.backlog_points = []

    def _save_backlog(self) -> None:
        """Save backlog points to persistent storage"""
        if not self.backlog_file or not self.enable_persistent_storage:
            return

        try:
            with open(self.backlog_file, 'wb') as f:
                pickle.dump(self.backlog_points, f)
        except Exception as e:
            self._log.error(f"Failed to save backlog: {e}")

    def _add_to_backlog(self, point) -> None:
        """Add a point dict to the persistent backlog"""
        if not self.enable_persistent_storage:
            return

        point['_backlog_time'] = time.time()
        self.backlog_points.append(point)

        if len(self.backlog_points) > self.max_backlog_size:
            removed = self.backlog_points.pop(0)
            self._log.warning(f"Backlog full, removed oldest point: {removed.get('measurement', 'unknown')}")

        self._save_backlog()

    def _flush_backlog(self) -> None:
        """Write all backlog points to InfluxDB v3"""
        if not self.backlog_points or not self.connected:
            return

        self._log.info(f"Flushing {len(self.backlog_points)} backlog points to InfluxDB v3")

        try:
            points_to_send = []
            for point in self.backlog_points:
                point_copy = point.copy()
                point_copy.pop('_backlog_time', None)
                points_to_send.append(self._dict_to_influx3_point(point_copy))

            if self.client is not None:
                self.client.write(record=points_to_send, database=self.database)
                self._log.info(f"Successfully wrote {len(points_to_send)} backlog points to InfluxDB v3")
                self.backlog_points = []
                self._save_backlog()

        except Exception as e:
            self._log.error(f"Failed to flush backlog to InfluxDB v3: {e}")

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initialize the InfluxDB v3 client connection"""
        self._log.info("influxdb3_out connect")

        try:
            self.client = InfluxDBClient3(
                host=self.host,
                token=self.token,
                database=self.database,
                org=self.org if self.org else None,
            )

            # InfluxDB v3 has no ping(); perform a lightweight health check via a
            # no-op query so we surface auth / connectivity problems early.
            self._health_check()

            self.connected = True
            self.last_connection_check: float = time.time()
            self.last_periodic_reconnect_attempt: float = time.time()
            self._log.info(f"Connected to InfluxDB v3 at {self.host}, database={self.database}")

            if self.enable_persistent_storage:
                self._flush_backlog()
                return True
            else:

                return True

        except ImportError:
            self._log.error(
                "InfluxDB v3 client not installed. "
                "Please install with: pip install influxdb3-python"
            )
            self.connected = False
            return False
        except Exception as e:
            self._log.error(f"Failed to connect to InfluxDB v3: {e}")
            self.connected = False
            return False

    def _quote_identifier(self, identifier: str) -> str:
        """Quote an InfluxDB SQL identifier while escaping embedded quotes."""
        if identifier is None:
            raise ValueError("Identifier cannot be None")
        return '"' + identifier.replace('"', '""') + '"'

    def _health_check(self) -> None:
        """Perform a lightweight query to verify connectivity and credentials."""
        if self.client is None:
            raise RuntimeError("Client not initialized")

        # A static query that does not require dynamic table names
        query_str = "SELECT 1 FROM system.tables LIMIT 1"

        self.client.query(
            query_str,
            database=self.database,
            language="sql",
        )

    def _check_connection(self) -> bool:
        """Check if the connection is still alive and reconnect if necessary"""
        current_time = time.time()

        # Periodic proactive reconnect
        if (self.periodic_reconnect_interval > 0 and
                current_time - self.last_periodic_reconnect_attempt >= self.periodic_reconnect_interval):

            self.last_periodic_reconnect_attempt = current_time
            self._log.info(f"Periodic reconnection check (every {self.periodic_reconnect_interval} seconds)")

            if self.connected and self.client:
                try:
                    self._health_check()
                except Exception as e:
                    self._log.warning(f"Periodic connection check failed: {e}")
                    return self._attempt_reconnect()
            else:
                return self._attempt_reconnect()

        # Throttle routine checks
        if current_time - self.last_connection_check < self.connection_check_interval:
            return self.connected

        self.last_connection_check = current_time

        if not self.connected or not self.client:
            return self._attempt_reconnect()

        try:
            self._health_check()
            return True  # noqa: TRY300
        except Exception as e:
            self._log.warning(f"Connection check failed: {e}")
            return self._attempt_reconnect()

    def _attempt_reconnect(self) -> bool:
        """Attempt to reconnect to InfluxDB v3 with exponential backoff"""
        self._log.info(f"Attempting to reconnect to InfluxDB v3 at {self.host}")

        for attempt in range(self.reconnect_attempts):
            try:
                self._log.info(f"Reconnection attempt {attempt + 1}/{self.reconnect_attempts}")

                if self.client:
                    try:
                        self.client.close()
                    except Exception:  # noqa: S110
                        pass

                from influxdb_client_3 import InfluxDBClient3
                self.client = InfluxDBClient3(
                    host=self.host,
                    token=self.token,
                    database=self.database,
                    org=self.org if self.org else None,
                )

                self._health_check()

                self.connected = True
                self.last_periodic_reconnect_attempt = time.time()
                self._log.info("Successfully reconnected to InfluxDB v3")

                if self.enable_persistent_storage:
                    self._flush_backlog()

                return True  # noqa: TRY300

            except Exception as e:
                self._log.warning(f"Reconnection attempt {attempt + 1} failed: {e}")
                if attempt < self.reconnect_attempts - 1:
                    if self.use_exponential_backoff:
                        delay = min(self.reconnect_delay * (2 ** attempt), self.max_reconnect_delay)
                        self._log.info(f"Waiting {delay:.1f} seconds before next attempt (exponential backoff)")
                    else:
                        delay = self.reconnect_delay
                        self._log.info(f"Waiting {delay:.1f} seconds before next attempt")
                    time.sleep(delay)

        self._log.error(f"Failed to reconnect after {self.reconnect_attempts} attempts")
        self.connected = False
        return False

    def trigger_periodic_reconnect(self) -> bool:
        """Manually trigger a periodic reconnection check"""
        self.last_periodic_reconnect_attempt = 0
        return self._check_connection()

    # ------------------------------------------------------------------
    # Data writing
    # ------------------------------------------------------------------

    def write_data(self, data: dict[str, int | float | str], from_transport: transport_base) -> None:
        if "LCDMachineModelCode" in data and data["LCDMachineModelCode"] and data["LCDMachineModelCode"] != "MPG":
            from_transport.device_model = str(data["LCDMachineModelCode"])

        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug(
                f"Received data from {from_transport.transport_name} "
                f"(serial: {from_transport.device_serial_number}) with {len(data)} fields"
            )

        if not self._check_connection():
            self._log.warning("Not connected to InfluxDB v3, storing data in backlog")
            self._process_and_store_data(data, from_transport)
            return

        self._process_and_write_data(data, from_transport)

    def _build_fields(self, data: dict[str, int | float | str], from_transport: transport_base) -> dict:
        """Classify and coerce each data key into the appropriate Python type for InfluxDB."""
        fields = {}
        for key, value in data.items():
            should_force_float = False
            is_enum = False
            is_ascii = False

            if hasattr(from_transport, 'protocolSettings') and from_transport.protocolSettings:
                for registry_type in [Registry_Type.INPUT, Registry_Type.HOLDING]:
                    registry_map = from_transport.protocolSettings.get_registry_map(registry_type)
                    for e in registry_map:
                        if e.variable_name.lower() == key.lower():
                            if e.unit_mod != 1.0:
                                should_force_float = True
                            if getattr(e, 'has_enum_mapping', False):
                                is_enum = True
                            if getattr(e, 'data_type', None) == Data_Type.ASCII:
                                is_ascii = True
                            break
                    if should_force_float or is_enum or is_ascii:
                        break

            if is_enum or is_ascii:
                fields[key] = str(value)
                continue

            try:
                float_val = float(value)
                if self.force_float or should_force_float:
                    fields[key] = float_val
                else:
                    fields[key] = int(float_val) if float_val.is_integer() else float_val
            except (ValueError, TypeError):
                fields[key] = str(value)
                self._log.debug(f"Field {key}: {value} -> string (conversion failed)")

        return fields

    def _build_tags(self, from_transport: transport_base) -> dict:
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

    def _create_point_dict(self, data: dict[str, int | float | str], from_transport: transport_base) -> dict:
        """Build a plain dict representation of an InfluxDB point (used for backlog persistence)."""
        point = {
            "measurement": self.measurement,
            "tags": self._build_tags(from_transport),
            "fields": self._build_fields(data, from_transport),
        }
        if self.include_timestamp:
            point["time"] = int(time.time() * 1e9)  # nanoseconds
        return point

    def _dict_to_influx3_point(self, point_dict: dict) -> Point:
        """Convert a plain point dict into an influxdb_client_3 Point object."""
        p = Point(point_dict["measurement"])

        for tag_key, tag_val in point_dict.get("tags", {}).items():
            p = p.tag(tag_key, tag_val)

        for field_key, field_val in point_dict.get("fields", {}).items():
            p = p.field(field_key, field_val)

        if "time" in point_dict:
            p = p.time(point_dict["time"])

        return p

    def _process_and_store_data(self, data: dict[str, int | float | str], from_transport: transport_base):
        """Build a point and place it in the persistent backlog (offline path)."""
        if "LCDMachineModelCode" in data and data["LCDMachineModelCode"] and data["LCDMachineModelCode"] != "MPG":
            from_transport.device_model = str(data["LCDMachineModelCode"])

        if not self.enable_persistent_storage:
            self._log.warning("Persistent storage disabled, data will be lost")
            return

        point = self._create_point_dict(data, from_transport)
        self._add_to_backlog(point)

        should_flush = False
        with self._batch_lock:
            self.batch_points.append(point)
            current_time = time.time()
            if (len(self.batch_points) >= self.batch_size or
                    (current_time - self.last_batch_time) >= self.batch_timeout):
                should_flush = True

        if should_flush:
            self._flush_batch()

    def _process_and_write_data(self, data: dict[str, int | float | str], from_transport: transport_base):
        """Build a point and add it to the write batch (online path)."""
        if "LCDMachineModelCode" in data and data["LCDMachineModelCode"] and data["LCDMachineModelCode"] != "MPG":
            from_transport.device_model = str(data["LCDMachineModelCode"])

        point = self._create_point_dict(data, from_transport)

        should_flush = False
        with self._batch_lock:
            self.batch_points.append(point)
            current_time = time.time()
            if (len(self.batch_points) >= self.batch_size or
                    (current_time - self.last_batch_time) >= self.batch_timeout):
                should_flush = True

        if should_flush:
            self._flush_batch()

    def _flush_batch(self) -> None:
        """Flush the current batch of points to InfluxDB v3"""
        with self._batch_lock:
            if not self.batch_points:
                return
            points_to_write = self.batch_points.copy()
            self.batch_points = []

        if not self._check_connection():
            self._log.warning("Not connected to InfluxDB v3, storing batch in backlog")
            for point in points_to_write:
                self._add_to_backlog(point)
            return

        influx_points = [self._dict_to_influx3_point(p) for p in points_to_write]

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

    def _log_batch_debug(self, points: list[dict], verb: str) -> None:
        """Emit structured debug lines for a batch of point dicts."""
        serial_numbers = []
        sample_values = []
        sample_field_names = ['vacr', 'VacR', 'soc', 'SOC', 'fwcode', 'FWCode', 'vbat', 'Vbat', 'pinv', 'Pinv']

        for point in points:
            tags = point.get('tags', {})
            fields = point.get('fields', {})

            serial_numbers.append(tags.get('device_serial_number', 'None'))

            sample_data = {k: fields[k] for k in sample_field_names if k in fields}
            if sample_data:
                sample_values.append(sample_data)
            elif fields:
                sample_values.append(f"No sample fields found. Available fields: {list(fields.keys())[:10]}")
            else:
                sample_values.append('No fields found')

        self._log.info(f"{verb} {len(points)} points to InfluxDB v3 (serial numbers: {', '.join(serial_numbers)})")

        for i, (serial, samples) in enumerate(zip(serial_numbers, sample_values)):
            transport_name = points[i].get('tags', {}).get('transport', 'unknown')
            self._log.debug(f"  Point {i+1} tags: {points[i].get('tags', {})}")
            if isinstance(samples, dict):
                sample_str = ', '.join(f"{k}={v}" for k, v in samples.items())
                self._log.debug(f"  Point {i+1} ({serial}) from {transport_name}: {sample_str}")
            else:
                self._log.debug(f"  Point {i+1} ({serial}) from {transport_name}: {samples}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init_bridge(self, from_transport: transport_base) -> None:
        """Initialize bridge - not needed for InfluxDB output"""
        pass

    def __del__(self) -> None:
        """Cleanup on destruction - flush any remaining points"""
        if self.batch_points:
            self._flush_batch()
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                self._log.warning(f"Cleanup exception {e}")
