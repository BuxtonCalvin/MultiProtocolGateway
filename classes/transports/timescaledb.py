"""
timescaledb transport bridge module is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

timescaledb transport bridge module is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You can find a copy of the GNU Affero General Public License in the documentation/bridges/timescaledb folder.
If not, see <https://www.gnu.org>.

timescaledb transport bridge module (with rollup continuous aggregates) and persistent disk backlog.
python > 3.9 is required.

Features:
 - Auto-create database (default "solar", configurable)
 - device_info (multi unique transport scraper devices) (for future multi-device/transport support)
 - device_metrics_wide hypertable
 - device_metrics_narrow hypertable
 - Hypertable compression & retention (idempotent)
 - Continuous aggregates rollups: hourly_rollup, daily_rollup, weekly_rollup, monthly_rollup with hierarchical dependencies and policies
 - Async flushing + persistent disk backlog
 - OS-local timestamps

Terminology:
Continuous Aggregates	The official TimescaleDB feature name. It's an automatically and incrementally updated
    materialized SQL view that pre-computes aggregate data (e.g., averages, sums over a minute, hour, day, week or month)
    from raw data and stores it in a separate hypertable view.

Continuous Rollups	 This term refers to the process of downsampling data into successively
    coarser time granularities (e.g., from raw data to hourly summaries, then to daily summaries, then to weekly summaries,
    then to monthly summaries). This is achieved using the hierarchical continuous aggregates feature,
    where a continuous aggregate based on the output of a previous one is created. For example, a daily rollup continuous
    aggregate would be defined based on the hourly rollup continuous aggregate, and so on.

"""
import asyncio
import json
import logging
import math
import queue
import re
import threading
import time
import warnings
from _thread import RLock, lock
from configparser import SectionProxy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional, Tuple

import requests
from sqlalchemy import (
    Connection,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Inspector,
    Integer,
    PrimaryKeyConstraint,
    Row,
    Table,
    Text,
    TextClause,
    create_engine,
    engine,
    insert,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import (
    insert as pg_insert,  # Postgres Insert (with on_conflict)
)

# from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.orm.session import Session
from tzlocal import get_localzone_name

from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

from .transport_base import transport_base

SessionGlobal: Callable[..., Session] = sessionmaker(
    autocommit=False,
    expire_on_commit=False,
    autoflush=False
)
machine_timezone: str = get_localzone_name()
# base class for all tables.
__version__ = "0.9.0"

class Base(DeclarativeBase):
    pass
class DeviceInfo(Base):
    __tablename__: str = "device_info"

    device_info_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_identifier: Mapped[Optional[str]] = mapped_column(Text, index=True)
    device_serial_number: Mapped[Optional[str]] = mapped_column(Text)
    device_name: Mapped[Optional[str]] = mapped_column(Text)
    device_manufacturer: Mapped[Optional[str]] = mapped_column(Text)
    device_model: Mapped[Optional[str]] = mapped_column(Text)
    device_firmware: Mapped[Optional[str]] = mapped_column(Text)
    device_location: Mapped[Optional[str]] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now().astimezone())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now().astimezone(), onupdate=lambda: datetime.now().astimezone())

    devicemetricswide: Mapped[List["DeviceMetricsWide"]] = relationship(
        "DeviceMetricsWide", back_populates="device_info"
    )
    devicemetricsnarrow: Mapped[List["DeviceMetricsNarrow"]] = relationship(
        "DeviceMetricsNarrow", back_populates="device_info"
    )

class DeviceMetricsWide(Base):
    __tablename__: str = "device_metrics_wide"

    m_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now().astimezone(), primary_key=True)
    device_info_id: Mapped[int] = mapped_column(ForeignKey("device_info.device_info_id"), primary_key=True)

    __table_args__: Tuple = (
        PrimaryKeyConstraint('m_time', 'device_info_id', name='device_metrics_wide_pkey'),
    )

    device_info: Mapped["DeviceInfo"] = relationship("DeviceInfo", back_populates="devicemetricswide")

class DeviceMetricsNarrow(Base):
    __tablename__: str = "device_metrics_narrow"

    # In TimescaleDB/SQLAlchemy, mark columns that are part of PK as primary_key=True in mapped_column
    # as well as defining the constraint in __table_args__
    # metric_value is forced float for all numerics and booleans.  Text (ASCII) is filtered out prior to append.
    m_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now().astimezone(), primary_key=True)
    device_info_id: Mapped[int] = mapped_column(ForeignKey("device_info.device_info_id"), primary_key=True)
    metric_name: Mapped[str] = mapped_column(Text, primary_key=True)
    metric_value: Mapped[float] = mapped_column(Float)

    __table_args__: Tuple = (
        PrimaryKeyConstraint('m_time', 'device_info_id', 'metric_name', name='device_metrics_narrow_pkey'),
    )

    device_info: Mapped["DeviceInfo"] = relationship("DeviceInfo", back_populates="devicemetricsnarrow")

class MetricCatalog(Base):
    """ Metric catalog for dynamic columns ensures that each metric has a corresponding column that is safe for SQL column naming.
    """
    __tablename__: str = "metric_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    clean_column_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    data_type: Mapped[str] = mapped_column(Text, default='double precision', nullable=False)
    unit_mod: Mapped[str] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),  default=lambda: datetime.now().astimezone(), onupdate=lambda: datetime.now().astimezone())
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

# TimescaleDB transport bridge class
class timescaledb(transport_base):

    """
    TimescaleDB transport bridge with hypertable, continuous aggregates and rollup support.
    The class uses background threads for auto-refresh of rollups and stale data detection.
    Uses a global sqlalchemy session for most database operations.
    """

    # -------------------------
    # Default settings (overridable by settings SectionProxy)
    # -------------------------
    # Database connection
    host: str = "localhost"
    port: int = 5432
    database: str = "solar"
    username: str = ""
    password: str = ""
    # write_mode = "READ"  # Unclear from base classes on how to use, as TimescaleDB transport will only write to the database, never to the inverter

    force_float: bool = True

    timescale_type_map: dict[str, str] = {
        # 8-bit & 16-bit Unsigned/Signed -> SMALLINT or INTEGER
        # USHORT is mapped to INTEGER to accommodate values > 32767
        "BYTE": "SMALLINT",
        "USHORT": "INTEGER",
        "SHORT": "SMALLINT",
        # 32-bit Unsigned/Signed
        "UINT": "BIGINT",
        "INT": "INTEGER",
        # Flags & Bits (Integers are best for Delta-Delta compression)
        "_8BIT_FLAGS": "SMALLINT",
        "_16BIT_FLAGS": "INTEGER",
        "_32BIT_FLAGS": "BIGINT",
        # Strings (Dictionary compression)
        "ASCII": "TEXT",
        "ASCII_LE": "TEXT",
        "HEX": "TEXT",
        "_1BIT": "BOOLEAN",

        # 1. Unsigned Bit-lengths (_2BIT to _15BIT)
        **{f"_{i}BIT": "SMALLINT" for i in range(2, 16)},
        "_16BIT": "INTEGER", # 16-bit unsigned needs INTEGER

        # 2. Signed Bits (_2SBIT to _16SBIT)
        **{f"_{i}SBIT": "SMALLINT" for i in range(2, 17)},

        # 3. Signed Magnitude (_2SMBIT to _16SMBIT)
        **{f"_{i}SMBIT": "SMALLINT" for i in range(2, 17)},

        # Float (Add these if you use them - Gorilla compression)
        "FLOAT32": "REAL",
        "FLOAT64": "DOUBLE PRECISION"
    }

    flush_timeout: int = 15

    # persistent storage/backlog settings. Default folder name and file name are the same but can be user configured.
    enable_persistent_storage: bool = True
    backlog_storage_path: Path = Path(__file__).resolve().parent.parent.parent / "timescaledb_backlog"
    backlog_file_name: str  = "timescaledb_backlog"
    max_backlog_size: int = 10000
    max_backlog_age: int = 86400   # seconds

    # reconnect/backoff
    reconnect_attempts: int = 5
    reconnect_delay: int = 5
    use_exponential_backoff: bool = True
    max_reconnect_delay: int = 300
    tsdb_connected = False

    ### Hypertable and rollup settings defaults.  These can be overridden by settings SectionProxy, but defaults are provided here for clarity.
    # whether to attempt to migrate existing data when creating hypertables and rollups.  Set to False to skip migration and start fresh with new schema.
    migrate_data: bool = True
    # whether to enable compression on hypertables at startup.
    #Compression policies are created regardless, but this controls whether existing data is compressed on init.
    enable_compression: bool = True
    enable_rollups = True  # whether to create continuous aggregate rollups on init and start the auto-refresh thread.
    auto_refresh_interval: int = 21600  # seconds (default 6 hours), auto-refresh rollup
    enable_auto_refresh: bool = True  # whether to auto-refresh rollups periodically
    drop_after: str = "1 year"  # default retention policy for raw data in tables and views, can be overridden by settings SectionProxy

    # Pushover settings
    enable_pushover: bool = True
    pushover_token: str = None
    pushover_user: str = None

    # stale data settings and fields
    stale_data_timeout: int = 300       # seconds before considering data stale for incomplete batch cleanup
    stale_data_last_row: Optional[dict[str, Any]] = None  # last row of metrics for stale data detection
    stale_data_start_ts: Optional[datetime] = None # timestamp when stale data period started
    is_stale_data: bool = False  # flag indicating if stale data condition is active
    stale_event_count: int = 0
    last_stale_event_ts: Optional[datetime] = None
    max_stale_attempts: int = 3
    retry_delay_mins: int = 5
    schema_needs_refresh: bool = True  # flag to indicate if ORM schema refresh is needed after reconnect or column changes
    current_metric_count: int = 0

    def __init__(self, settings: SectionProxy) -> None:
        """
        Initialize the TimescaleDB transport bridge.

        Args:
            settings (SectionProxy): Configuration section containing database and transport options.

        Configuration options:
            - host (str): Database host (default: "localhost")
            - port (int): Database port (default: 5432)
            - database (str): Database name (default: "solar")
            - username (str): Database username
            - password (str): Database password
            - device_name (str): Name for the bridge (default: "TimeScaleDB PPG Bridge")
            - force_float (bool): Force metric values to float (default: True)
            - flush_timeout (int): Seconds between batch flushes (default: 15) matches read_interval of source transport
            - enable_persistent_storage (bool): Enable disk backlog (default: True)
            - backlog_storage_path (str): Path for backlog files (default: "parent/timescaledb_backlog")
            - max_backlog_size (int): Max backlog points (default: 10000)
            - max_backlog_age (int): Max age (seconds) for backlog points (default: 86400)  24 hours
            - reconnect_attempts (int): Max reconnect attempts (default: 5)
            - reconnect_delay (int): Initial reconnect delay (default: 5)
            - use_exponential_backoff (bool): Use exponential backoff (default: True)
            - max_reconnect_delay (int): Max reconnect delay (default: 300)
            - stale_data_timeout (int): Seconds before considering data stale for incomplete batch cleanup (default: 300)
            - hypertable_defaults: Dicts for hypertable narrow and wide creation and policies
            - enable_compression (bool): Enable compression on hypertables at startup (default: True)
            - rollup_defaults: dict for rollup settings
            - enable_auto_refresh (bool): Enable periodic rollup refresh (default: True)
            - auto_refresh_interval (int): Seconds between rollup refreshes (default: 21600)

        Thread behavior:
            - Starts a background thread for batch flushing of metrics.
            - Starts a background thread for periodic rollup refreshes.
            - Thread safety is ensured for internal operations.
        """

        """
        0.9.0 Initial Commit

        """

        # -------------------------
        # load user settings from SectionProxy
        # -------------------------

        # load connect configuration settings
        self.host = settings.get("host", fallback=self.host)
        self.port = settings.getint("port", fallback=self.port)
        self.database = settings.get("database", fallback=self.database)
        self.username = settings.get("username", fallback=self.username)
        self.password = settings.get("password", fallback=self.password)

        # transport_name -> device_info_id cache to minimize DB lookups for device_info_id during batch writes
        self._device_cache: dict[str, int] = {}

        if not self.username:
            raise ValueError("TimeScaleDB User is not set")

        if not self.password:
            warnings.warn("TimeScaleDB Password is empty", RuntimeWarning)

        # load reconnect/backoff settings
        self.use_exponential_backoff = settings.getboolean("use_exponential_backoff", fallback=self.use_exponential_backoff)
        self.max_reconnect_delay = settings.getint("max_reconnect_delay", fallback=self.max_reconnect_delay)

        # flush points settings
        self.flush_timeout = settings.getint("read_interval", fallback=self.flush_timeout)
        self.force_float = settings.getboolean("force_float", fallback=self.force_float)

        # stale data cleanup settings
        self.stale_data_timeout: int = settings.getint("stale_data_timeout", fallback=self.stale_data_timeout)

        # persistent backlog settings
        self.enable_persistent_storage = settings.getboolean("enable_persistent_storage", fallback=self.enable_persistent_storage)
        self.backlog_storage_path = Path(str(settings.get("backlog_storage_path", fallback=self.backlog_storage_path)))
        self.backlog_file_name = settings.get("backlog_file_name", fallback=self.backlog_file_name)
        self.max_backlog_size = settings.getint("max_backlog_size", fallback=self.max_backlog_size)
        self.max_backlog_age: int = settings.getint("max_backlog_age", fallback=self.max_backlog_age)

        # hypertable / rollup options that are user defined.  These are sent to the rollup manager
        # which will handle the hypertable and rollup creation and maintenance based on these settings.

        self.rollup_policy: dict[str,Any] = {
            # Hypertable Settings
            "current_metric_count": self.current_metric_count,
            "tsdb_connected": self.tsdb_connected,
            "drop_after": settings.get("drop_after", fallback=self.drop_after),
            "migrate_data": settings.getboolean("migrate_data", fallback=str(self.migrate_data)),
            "enable_compression": settings.getboolean("enable_compression", fallback=str(self.enable_compression)),
            # Rollup Settings
            "auto_refresh_interval": settings.getint("auto_refresh_interval", fallback=self.auto_refresh_interval),
            "enable_auto_refresh": settings.getboolean("enable_auto_refresh", fallback=str(self.enable_auto_refresh)),
            "enable_rollups": settings.getboolean("enable_rollups", fallback=str(self.enable_rollups)),
        }

        # pushover settings
        self.enable_pushover: bool = settings.getboolean("enable_pushover", fallback=self.enable_pushover)
        self.pushover_token: str = settings.get("pushover_token", fallback=self.pushover_token)
        self.pushover_user: str = settings.get("pushover_user", fallback=self.pushover_user)

        super().__init__(settings)

        # 3. Explicitly set bridge name
        self.device_name = "TimescaleDB PPG Bridge"

        self._verified_devices: set[str] = set()

        # end user settings
        #*********************************

        # TimescaleDB output is always write-enabled but this setting is unclear from base class
        # keep it enabled to allow access to transport_base.write_data method???
        self.write_enabled = True

        # load all registry metrics' map.
        self.registry_metrics: dict[Registry_Type, List[registry_map_entry]]  = protocol_settings.registry_map

        self._wide_columns: set[str] = set()  # cached set of existing wide table columns for fast lookup

        # metric_name with clean_column_name, data_type, unit for mapping dict for raw to safe metric name conversions
        self.metric_mapping: dict[str, tuple[str, str]] = {}
        self.device_info_id = None  # will be set after data scrape, per transport batch.
        self.wide_table_flag = True  # assume wide table unless too many metrics detected

        # SQLAlchemy init runtime
        self.engine = None  # engine connection
        self.SessionFactory = None

        # -------------------------
        # threading
        # -------------------------

        # Initialize async flush queue and worker thread.  Start it here so it's ready at full init.
        self._flush_queue: queue.Queue = queue.Queue(maxsize=0)
        self._flush_thread = threading.Thread(target=self._flush_worker, daemon=True, name="FlushWorker")
        self._flush_lock: lock = threading.Lock()
        self._flush_event: threading.Event = getattr(self, "_flush_event", threading.Event())
        # event used to stop all threads
        self._stop_event: threading.Event = getattr(self, "_stop_event", threading.Event())

        # init runtime backoff settings for connection attempts
        self._reconnect_lock: lock = threading.Lock()     # prevents multiple concurrent TSDB reconnect triggers
        self._reconnect_thread_running = False      # guard to prevent duplicate reconnect threads
        self._stop_reconnect_event:  threading.Event = getattr(self, "_stop_reconnect_event", threading.Event())

        self.migration_in_progress = threading.Event()  # event to pause flushes during rollup migration

        # Protects the BacklogManager (the list and the .jsonl file).
        self._backlog_lock: RLock = threading.RLock()  # lock for backlog operations

        # Lock for protecting schema mutations and metadata reflection
        # Use RLock to allow nested calls within the same thread
        # Protects the SQLAlchemy Metadata and Table Identifiers (the "structure" of the Wide Table).
        self._schema_lock: RLock = threading.RLock()

        # lock for protecting device_info appends when incoming data is from two or more source transports
        self._device_lock: lock = threading.Lock()

        # persistent backlog file and path, both the file path and the in-memory backlog file are initialized here
        self.backlog_file_path: Path = self.backlog_storage_path / f"{self.backlog_file_name}.jsonl"
        # full path to backlog file

        self.backlog = BacklogManager(
            backlog_file_path=self.backlog_file_path,
            max_backlog_age=self.max_backlog_age,
            max_backlog_size=self.max_backlog_size,
            flush_queue=self._flush_queue,
            flush_event=self._flush_event,
            backlog_lock=self._backlog_lock,
            log=self._log
        )

        if self.enable_persistent_storage:
            asyncio.run(asyncio.to_thread(self.backlog.load_from_disk))

        # attempt tsdb connection now
        try:
            self.connect_tsdb(transport_base)
        except Exception as e:
            self._log.error(f"Initial connect failed: {e}")
            self._set_tsdb_connected(False, "Initial connect was not successful")  # noqa: FBT003

        self.request_upstream_reconnect: Callable[[], None] | None = None

    def connect_tsdb(self, from_transport: transport_base) -> None:
        """
        Connect to DB, build device_metrics_wide table from metrics data, and ensure schema/hypertable/policies exist.
        If from_transport data provided, ensure device_info insert for that transport.
        """
        #self._log.info(f"Version: {self.__version__}")
        try:
        # 1 create database if missing.  Connect to standard default "postgres" database first to then check/create target database structure.

            self._create_database_if_missing()


        # 2 create engine by logging into the default (or changed name) database
            try:
                self._create_engine()
            except Exception as e:
                self._log.error(f"Engine creation error: {e}")

        # 3 create ORM tables. DeviceInfo, and stub columns for DeviceMetricsWide.  MetricCatalog for dynamic column names.
            try:
                self._create_tables()

            except Exception as e:
                self._log.error(f"ORM table creation error: {e}")


        # 5 If needed, create dynamic columns for metrics in device_metrics_wide and add metrics to metric_catalog table
             # Using the registry_map from protocol_settings to get metric names.  No live data access here.
            try:
                self._determine_wide_table()
            except Exception as e:
                self._log.error(f"Determine wide/narrow table failed: {e}")

            try:
                self._start_flush_thread()
            except Exception as e:
                self._log.error(f"thread start failed: {e}")

            # initialize rollup class.
            if self.rollup_policy.get("enable_rollups", True):
                self.rollup_mgr = RollupManager(
                    rollup_policy=self.rollup_policy,
                    SessionFactory=self.SessionFactory,
                    Engine=self.engine,
                    wide_table_flag=self.wide_table_flag,
                    migration_in_progress=self.migration_in_progress,
                    send_pushover_message = self._send_pushover_message,
                    log=self._log,
                    backlog_lock=self._backlog_lock,
                    flush_queue=self._flush_queue,
                    backlog=self.backlog,
                    reconnect_lock=self._reconnect_lock
                )
                self.rollup_mgr.setup_schema()

        # 6 Flush any existing backlog that might have been saved prior to initialization.
             # backlogs accumulated during connection down or method malfunctions are flushed during reconnect.
            try:
                if self.enable_persistent_storage:
                    self.backlog.replay_to_queue()
            except Exception as e:
                self._log.error(f"Persistent storage failed: {e}")

            # start _refresh_rollup_thread after connect completes successfully
            if self.rollup_policy.get("enable_rollups", True) and self.tsdb_connected:
                self._log.info("Rollups are enabled.")

                if not self.rollup_policy.get("enable_auto_refresh", True):
                    self._log.info("Auto rollup refresh is disabled.")
                    return
                self.rollup_mgr.start_auto_refresh()

        except Exception as e:
            self._set_tsdb_connected(False, "Connect unsuccessful")  # noqa: FBT003
            self._log.error(f"connect() failed: {e}")
            raise

    # Centralize state transitions for tsdb_connected helper
    def _set_tsdb_connected(self, conn_value: bool, con_reason: str) -> None:
        with self._reconnect_lock:
            if self.tsdb_connected != conn_value:
                self.tsdb_connected: bool = conn_value
                self.rollup_policy["tsdb_connected"] = conn_value
                self._log.info(f"tsdb_connected -> {conn_value} ({con_reason})")


    # -------------------------
    # reconnect/backoff
    # -------------------------
    def _attempt_reconnect(self) -> None:
        """
        Background reconnect worker. Uses class-configured retry/backoff settings.
        The method must clear self._reconnect_thread_running before returning.
        """
        try:
            self._log.warning("Auto-reconnect: connection lost, attempting to reconnect...")

            attempts: int = self.reconnect_attempts if getattr(self, "reconnect_attempts", None) is not None else 5
            delay: int = self.reconnect_delay if getattr(self, "reconnect_delay", None) is not None else 5
            use_exp = bool(getattr(self, "use_exponential_backoff", True))
            max_delay: int = getattr(self, "max_reconnect_delay", 300)

            attempt_no = 0
            while (attempts <= 0) or (attempt_no < attempts):  # attempts <= 0 => unlimited attempts
                if self._stop_reconnect_event.is_set():
                    self._log.info("Auto-reconnect: stop requested, exiting reconnect loop.")
                    break

                attempt_no += 1
                self._log.info(f"Reconnect attempt {attempt_no}{'' if attempts <= 0 else f'/{attempts}'} — waiting {delay}s before connect.")
                # Wait but allow early exit on stop
                waited = 0.0
                while waited < delay:
                    if self._stop_reconnect_event.is_set():
                        self._log.info("Auto-reconnect: stop requested during delay.")
                        break
                    sleep_chunk: float = min(1.0, delay - waited)
                    time.sleep(sleep_chunk)
                    waited += sleep_chunk
                if self._stop_reconnect_event.is_set():
                    break

                try:
                    # Attempt to re-establish DB connection.

                    with self.engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
                    self._set_tsdb_connected(True, "reconnect successful")  # noqa: FBT003
                except Exception as e:
                    self._set_tsdb_connected(False, "reconnect unsuccessful")  # noqa: FBT003
                    self._log.warning(f"Reconnect attempt {attempt_no} failed during connect(): {e}")

                with self._reconnect_lock:
                    tsdb_connected: bool = self.tsdb_connected

                if tsdb_connected:
                    self._log.info("Auto-reconnect: connection re-established.")

                    # Immediately try to flush backlog — don't let exceptions prevent thread exit
                    try:
                        if getattr(self, "enable_persistent_storage", False):
                            with self._backlog_lock:
                                self.backlog.replay_to_queue()
                    except Exception as e:
                        self._log.error(f"Auto-reconnect: backlog flush failed after reconnect: {e}")

                    break  # success: exit loop

                # not tsdb_connected: compute next delay (exponential if configured)
                if use_exp:
                    delay = min(delay * 2, max_delay)
                else:
                    delay = min(delay, max_delay)

            if not getattr(self, "tsdb_connected", False):
                # Final log if exhausted
                if attempts > 0 and attempt_no >= attempts:
                    self._log.error("Auto-reconnect: exhausted reconnect attempts. Will continue buffering to backlog.")
                else:
                    self._log.info("Auto-reconnect: stopped without establishing connection.")

        finally:
            # clear the thread-running guard so a future outage can spawn a new reconnect thread
            with self._reconnect_lock:
                self._reconnect_thread_running = False
            self._log.debug("Auto-reconnect thread exiting.")


    def _trigger_reconnect(self) -> None:
        """Prevent concurrent reconnect threads from being spawned """

        if not hasattr(self, "_reconnect_lock"):
            self._reconnect_lock = threading.Lock()
            self._reconnect_thread_running = False

        if not hasattr(self, "_stop_reconnect_event"):
            self._stop_reconnect_event = threading.Event()

        if self._stop_event.is_set():
            return

        with self._reconnect_lock:
            if self._reconnect_thread_running:
                return
            self._reconnect_thread_running = True
            # set tsdb_connected False immediately to cause upstream to stop DB work
            self._set_tsdb_connected(False, "Connect unsuccessful")  # noqa: FBT003
            self._stop_reconnect_event.clear()
            threading.Thread(target=self._attempt_reconnect, daemon=True, name= "TSDB ReconnectThread").start()
            self._log.info("Reconnect thread started.")

    def _stop_thread_reconnect(self) -> None:
        """
        Cleanly stop the reconnect thread.
        """
        if hasattr(self, "_stop_reconnect_event"):
            self._stop_reconnect_event.set()
            self._log.info("reconnect thread stopped.")

    # -------------------------
    # 1. Ensure the database exists
    # -------------------------
    def _create_database_if_missing(self):
        """ checks to see if the target default postgres database exists. Then creates the solar database
        (or whatever the user names the metrics database) if missing.  Datname = Database name in postgres.
        """
        try:
            self._log.debug(f"Checking database '{self.database}' existence")
            default_url: str = f"postgresql+psycopg2://{self.username}:{self.password}@{self.host}:{self.port}/postgres"
            try:
                self._log.debug(f"Connecting to default 'postgres' database at {self.host}:{self.port} as user '{self.username}'")
                default_engine: Engine = create_engine(default_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)

            except OperationalError as e:
                self._set_tsdb_connected(False, "Connect unsuccessful")  # noqa: FBT003
                self._log.error(f"OperationalError during engine/session creation during database creation: {e}")
                raise

            with default_engine.connect() as conn:
                row: Row[Any] | None = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": self.database}).fetchone()
                if not row:
                    self._log.info(f"Database '{self.database}' not found — creating")
                    conn.execute(text(f'CREATE DATABASE "{self.database}"'))
                    conn.execute(text('CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit'))
                    self._log.info(f"Database '{self.database}' created")
                else:
                    self._log.debug(f"Database '{self.database}' already exists")
            default_engine.dispose()
        except Exception as e:
            self._log.error(f"Failed to verify/create database '{self.database}': {e}")
            raise
    # -------------------------
    # 2. Create SQLAlchemy engine
    #-------------------------

    def _create_engine(self) -> None:
        """
        create SQLAlchemy engine for TimescaleDB database connection
        """

        url: str = f"postgresql+psycopg2://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"
        try:
            self._log.debug(f"Connecting to database '{self.database}' at {self.host}:{self.port} as user '{self.username}'")
            self.engine: Engine = create_engine(url, pool_pre_ping=True, future=True, pool_recycle=3600)
            SessionGlobal.configure(bind=self.engine)
            self.SessionFactory: Callable[..., Session] = SessionGlobal
            with self.engine.connect() as conn:   # make sure connection works
                conn.execute(text("SELECT 1"))
            self._set_tsdb_connected(True, "Connect successful")  # noqa: FBT003
            self._log.info(f"Connected to database '{self.database}'")
        except OperationalError as e:
            self._set_tsdb_connected(False, "Connect unsuccessful")  # noqa: FBT003
            self._log.error(f"OperationalError during engine creation: {e}")
            raise

    # -------------------------
    # 3. Ensure ORM tables exist
    # -------------------------

    def _create_tables(self) -> None:
        """
         Create ORM tables for device_info, device_metrics_wide, device_metrics_narrow and metric_catalog.
        """
        with self.SessionFactory() as session:
            with self._reconnect_lock:
                tsdb_connected: bool = self.tsdb_connected

            if not tsdb_connected or not session:
                self._log.debug("Cannot create tables, not tsdb_connected")
                return

            try:
                Base.metadata.create_all(self.engine)
                with session.begin():
                    session.execute(text("CREATE INDEX IF NOT EXISTS device_metrics_wide_pkey ON device_metrics_wide (m_time DESC, device_info_id);"))
                    session.execute(text("CREATE INDEX IF NOT EXISTS device_metrics_narrow_pkey ON device_metrics_narrow (m_time DESC, device_info_id, metric_name);"))
                    session.commit()
                self._log.info("ORM tables created/ensured")
            except Exception as e:
                self._log.error(f"ORM table creation error: {e}")

    # -------------------------
    #  4. Write device information metadata
    # -------------------------

    def _get_or_create_device(self, from_transport: transport_base) -> int:
        """ The database doesn't know which field changed (name, model, or serial). It only knows that the unique Key (transport) already exists.
            The First Run: The database sees a new transport name. It creates the row with all metadata.
            The Next Startup: If say you've changed the device_name in your config. The code tries to INSERT the row again.
            The Conflict: The database says: "Stop! I already have a row where transport is growatt_1."
            The Upsert Logic: Because of on_conflict_do_update, the database then takes the new values you just sent
              (the changed device_name) and overwrites the old values in that existing row."""

        t_name = from_transport.transport_name

        # 1. Verified & Cached
        # If it's in both, we know the DB is up-to-date for this session.
        if t_name in self._device_cache and t_name in self._verified_devices:
            return self._device_cache[t_name]

        # 2. The verification path (First packet of the session)
        with self._device_lock:
            # Double check inside lock
            if t_name in self._device_cache and t_name in self._verified_devices:
                return self._device_cache[t_name]

            with self.SessionFactory() as session:
                try:
                    # Prepare the Upsert Statement (PostgreSQL specific)
                    stmt = pg_insert(DeviceInfo).values(
                        transport=t_name,
                        device_identifier=from_transport.device_identifier,
                        device_name=from_transport.device_name,
                        device_manufacturer=from_transport.device_manufacturer,
                        device_model=from_transport.device_model,
                        device_serial_number=from_transport.device_serial_number,
                        device_location=from_transport.device_location,
                        created_at=datetime.now().astimezone(),
                        updated_at=datetime.now().astimezone()
                    )

                    # Define what to update if the 'transport' unique constraint is hit
                    upsert_stmt = stmt.on_conflict_do_update(
                        index_elements=['transport'],
                        set_={
                            "device_identifier":stmt.excluded.device_identifier,
                            "device_name": stmt.excluded.device_name,
                            "device_manufacturer": stmt.excluded.device_manufacturer,
                            "device_model": stmt.excluded.device_model,
                            "device_serial_number": stmt.excluded.device_serial_number,
                            "device_location": stmt.excluded.device_location,
                            "updated_at": stmt.excluded.updated_at
                        }
                    ).returning(DeviceInfo.device_info_id)

                    # Execute and get the ID
                    result = session.execute(upsert_stmt)
                    db_id = result.scalar_one()
                    session.commit()

                except Exception as e:
                    session.rollback()
                    self._log.error(f"Upsert failed for {t_name}: {e}")
                    # Fallback to cache if we have it, otherwise None
                    return self._device_cache.get(t_name)

                else:
                    # Update caches
                    self._device_cache[t_name] = db_id
                    self._verified_devices.add(t_name) # Mark as verified for this run
                    return db_id

    def _determine_wide_table(self) -> None:
        """
        Determine whether to create wide table based on metric_catalog entries.
        """
        try:
            #5a get metric names from registry_map
            metric_start_names: list[tuple[str, str]] = self._registry_metric_names()
            metric_count: int = len(metric_start_names)
            self.current_metric_count: int = metric_count

            if metric_count == 0 or not metric_start_names:
                self._log.error(f"Detected {metric_count} metrics — no metric names detected. ")
                raise ValueError("No metric names detected.")  # noqa: TRY301

            # too many metrics for wide table; use only narrow storage
            elif metric_count >= 200:
                self.wide_table_flag = False
                self._log.warning(f"Detected {metric_count} metrics exceeds 200 column limit; will use narrow metric storage.")
            else:  # 200 or fewer metrics; create dynamic columns
                self._log.info(f"Detected {metric_count} metrics; creating columns.")
                # ensure dynamic columns for metrics in device_metrics and metric_catalog.
                self.wide_table_flag: bool = self._ensure_columns_for_metrics(metric_start_names)

                if not self.wide_table_flag:
                    self._log.error("Failed to ensure metric columns despite valid metric names.")
                    raise ValueError("Failed to ensure metric columns.")  # noqa: TRY301

        except ValueError as e:

            self._log.error(f"No metric names detected: {e}")
            return  # Exit connect early if no metrics are detected  or column creation failed

        except Exception as e:
            # Catch any general exceptions that occurred during any step above

            self._log.error(f"device_metrics_wide table columns creation error: {e}")

    # -------------------------
    #  5a. Get metric's names from registry map
    # -------------------------

    def _registry_metric_names(self) -> List[str]:
        """ load registry map for validation of metric names for dynamic column creation. Return sorted list of metric names
            with their data types and notes.
        """

        ## returns all variable_name in registry_metrics as opposed to below selected Registry_Type.
        # return sorted([
        #     entry.variable_name
        #     for entries_list in self.registry_metrics.values()
        #     for entry in entries_list
        #     if hasattr(entry, 'variable_name')
        # ])

        return sorted([
            (entry.variable_name, getattr(entry, 'data_type', ''), getattr(entry, 'unit_mod', ''), getattr(entry, 'note', ''))
            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING)
            for entry in self.registry_metrics[registry_type]
            if hasattr(entry, 'variable_name')
        ])

    # -------------------------
    #  5b. Dynamically create wide table columns for metrics
    # -------------------------

    def _ensure_columns_for_metrics(self, metric_start_names: List[str]) -> bool:
        """
        Ensure each metric name as defined in the variable_mask/variable_screen filters has a corresponding column in device_metrics_wide,
        and an entry in the metric_catalog table-- which describes the wide table field definitions.
        Due to the memory limits of postgres, no more than 200 metrics as determined in the calling method.
        Using metric_name to map, return clean_column_name in the metric_catalog to create the SQL column in device_metrics_wide.

        There could potentially be thousands of metrics, which cannot all be ingested as columns. If over 200 metrics we only save metrics to the
        device_metrics_narrow table, so this method is not needed and is bypassed at the calling method connection stage.
        """

        with self.SessionFactory() as session:

            # Must be connected to tsdb to write metric names
            with self._reconnect_lock:
                tsdb_connected: bool = self.tsdb_connected

            if not tsdb_connected or not session:
                self._log.error("Cannot create columns — not tsdb_connected.")
                return False

            if not metric_start_names:
                self._log.error("No metric column names were detected")
                return False

            try:
                with self._schema_lock:
                    with session.begin():
                        # advisory lock to serialize schema changes
                        self._schema_advisory_lock(session)

                        # metric name, data_type, unit_mod, note
                        for m, d, u, n in metric_start_names:
                            # 1. Check for existing clean name that has already been mapped.
                            clean_value: Any | None = session.execute(
                                text("SELECT clean_column_name FROM metric_catalog WHERE metric_name = :m"),
                                {"m": m}
                            ).scalar()

                            d_type: str = self._timescale_type(d,u)

                            if clean_value:
                                # Update the data_type,unit_mod and notes fields in metric_catalog for a matching metric
                                # from the csv files.  All corrections/updates should take place in the CSV.
                                session.execute(
                                    text("""
                                        UPDATE metric_catalog SET data_type = :d, unit_mod = :u, notes = :n
                                        WHERE metric_name = :m
                                    """),
                                    {"d": d_type, "u": u, "m": m, "n": n}
                                )
                                # metric_mapping is used to process raw metrics data for coercion.
                                self.metric_mapping[m] = (clean_value, d_type)
                                # metric exists so return to the top of the loop.
                                continue

                            # 2. If clean_value was false, clean metric name for safe sql column naming.
                            col: str = self._clean_column_name(m)

                            # check if column name (cleaned name) exists in postgres information_schema
                            exists_wide: Any | None = session.execute(text("""
                                SELECT 1 FROM information_schema.columns
                                WHERE table_name = 'device_metrics_wide' AND column_name = :col
                            """), {"col": col}).scalar()

                            # Add column if missing.  Initial column creation is alphabetic due to sorted metric names.
                            # Per postgres docs, subsequent columns added after first init are always appended to the end of the table.
                            # ie if you want a new column in the middle of the wide table, you must manually delete all tables,
                            # (losing your data) and restart PPG to recreate the table with the new column in the desired location.

                            if not exists_wide:
                                session.execute(text(
                                    f"ALTER TABLE device_metrics_wide ADD COLUMN IF NOT EXISTS {col} {d_type};"
                                ))

                            params: dict = {
                                'm': m,         # metric_name
                                'col': col,     # clean_column_name
                                'dtype': d_type or 'double precision', # data_type mapped from registry, with default
                                'umod': u,
                                'col_date': datetime.now().astimezone(),
                                'n': n          # note from registry
                            }
                            # just in case the if clean_value: check failed.
                            # Take the existing record's field values and overwrite them with the value from the row that failed to insert".
                            session.execute(text("""
                                INSERT INTO metric_catalog (metric_name, clean_column_name, data_type, unit_mod, created_at, notes)
                                VALUES (:m, :col, :dtype, :umod, :col_date, :n)
                                ON CONFLICT (metric_name) DO UPDATE SET
                                    clean_column_name = EXCLUDED.clean_column_name,
                                    data_type = EXCLUDED.data_type,
                                    unit_mod = EXCLUDED.unit_mod,
                                    notes = EXCLUDED.notes
                            """), params)

                            self.metric_mapping[m] = (col, d_type)

                    self._cache_wide_table_columns()  # cache existing wide table columns for fast lookup validation during writes
                    self._sync_single_table_schema()  #  resync ORM table after dynamic column changes

                    self._log.info(f"Ensured {len(metric_start_names)} metric columns.")
                    return True  # noqa: TRY300

            except Exception as e:
                self._log.error(f"_ensure_columns_for_metrics failed (rolled back): {e}")
                return False

    #5c. advisory lock for schema changes
    def _schema_advisory_lock(self, session: Session, key_text: str = "timescaledb_schema_lock") -> None:
        """ transaction scoped advisory lock based on hash of key_text
            Locks wide table schema in postgres DB to allow dynamic wide table refactor.
        """

        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k));"), {"k": key_text})

    def _timescale_type(self,data_type,unit) -> str:
        dt_name = data_type.name

        # --- base type from lookup, use DOUBLE PRECISION as fall back to be safe.
        base_type: Any = self.timescale_type_map.get(dt_name, "DOUBLE PRECISION")

        # --- text and boolean never scale ---
        if base_type in ("TEXT", "BOOLEAN"):
            return base_type

        # --- scaling override ---
        # if a numeric value has a unit modifier, store as float
        if unit != 1.0:
            return "DOUBLE PRECISION"

        return base_type

    #5d. clean column name
    def _clean_column_name(self, metric_name: str) -> str:
        """
        Cleans a string to be a safe SQL column name.

        Removes potentially unsafe characters and ensures column name starts with a letter or underscore.
        PostgreSQL identifiers must start with a letter (or underscore) and can contain
        letters, numbers, and underscores. If not quoted, they are also case-insensitive.
        """
        # Replace non-alphanumeric characters (except underscore) with underscores
        cleaned_name = metric_name.strip().lower()
        cleaned_name = re.sub(r'[^a-zA-Z0-9_]', '_', cleaned_name)

        # Ensure it starts with a letter or an underscore
        if not re.match(r'^[a-zA-Z_]', cleaned_name):
            cleaned_name: str = '_' + cleaned_name

        # Truncate to length if necessary (PostgreSQL limit is 63 chars for identifiers)
        return cleaned_name[:63]

    #5e. wide table columns helper for write operations
    def _cache_wide_table_columns(self) -> None:
        """Updates the internal set of valid column names."""
        try:
            insp: Inspector = inspect(self.engine)
            cols: List[ReflectedColumn] = insp.get_columns("device_metrics_wide", schema='public')

            # Use a local variable then swap to keep the operation near-atomic
            new_cols: set[str] = {
                col['name'] for col in cols
                if col['name'] not in ("m_time", "device_info_id")
            }
            self._wide_columns = new_cols
        except Exception as e:
            self._log.error(f"Failed to cache wide columns: {e}")

    #5f. wide table row validation
    def _validate_wide_row(self, row: dict) -> bool:
        # Use the same lock to ensure we aren't validating against a table being swapped
        with self._schema_lock:

            # metadata keys like m_time and device_info_id are excluded in _cache_wide_table_columns
            extra_keys: set = set(row) - self._wide_columns

            if extra_keys:
                self._log.error( f"Wide-table schema mismatch; unknown columns: {sorted(extra_keys)}" )
                msg: str= f"Unknown columns: {sorted(extra_keys)}"
                raise ValueError(msg)
            else:
                return True

    # 5g. resync single wide table schema after dynamic column changes
    def _sync_single_table_schema(self) -> None:
        """
        Resyncs the SQLAlchemy ORM mapping after dynamic column changes.
        Uses a lock to prevent the flush worker from using a half-reflected table.
        """
        table_name: str = DeviceMetricsWide.__tablename__

        with self._schema_lock:
            self._log.info(f"Resyncing schema for {table_name}...")

            # 1. Unbind the old table from metadata
            old_table: Table  = Base.metadata.tables.get(table_name)
            if old_table is not None:
                Base.metadata.remove(old_table)

            # 2. Reflect the NEW structure from the database
            new_table = Table(
                table_name,
                Base.metadata,
                autoload_with=self.engine,
                extend_existing=True  # Ensures we overwrite the internal cache
            )

            # 3. Update the ORM class binding
            DeviceMetricsWide.__table__ = new_table

            # 4. Refresh the column cache used for validation
            self._cache_wide_table_columns()
            self._log.info(f"Schema resync complete for {table_name}")


    # # 10g
    def _start_flush_thread(self) -> None:
        """
        Launch background thread to process metrics.
        """
        if self._flush_thread.is_alive():
            return
        self._flush_thread.start()
        self._log.debug("Flush thread started.")

    # using inherited transport_base.write_data method

    def write_data(self, data: dict[str, Any], from_transport: transport_base) -> None:
        """Overload write_data to process incoming data from transports."""

        # 1. Trap: Ignore non-dictionary signals (like boolean True)
        if not isinstance(data, dict) or not data:
            self._log.warning(
                f"Received non-dict signal ({type(data).__name__}) from "
                f"[{from_transport.transport_name}]. Ignoring."
            )
            return

        # 3. Ensure device_info_id is set and transport is set for device metadata
        device_id: int = self._get_or_create_device(from_transport = from_transport)

        if device_id is None:
            self._log.error("Could not resolve Device ID. Dropping packet.")
            return

        # 3. Proceed only if there is "real" data
        self._log.debug(f"Data: {data}")
        self._log.debug(f"writing data from [{from_transport.transport_name}] to timescaledb bridge")

        payload = {
            "device_info_id": device_id,
            "metrics": data.copy(),
            "m_time": datetime.now().astimezone(),  # source of truth timestamp for all metrics.
            "transport_name": from_transport.transport_name
        }
        self._flush_queue.put(payload)

    # Flush worker thread to handle data writes to the database.
    def _flush_worker(self) -> None:
        """Async flush worker created during init.  Handles data appends to tables. Routing to backlog if needed.
            datacopy  -> wide dict of unaltered metrics passed from PPG or backlog
            wide_data  -> wide dict of processed datacopy for safe sql coercion.
            narrow_data  -> dict of appended new_data with deviceid and timestamp.  Needed because narrow table
            applies timestamp to individual metrics.
        """
        # Check if another thread flagged a schema change
        with self._schema_lock:
            if self.schema_needs_refresh:
                self._sync_single_table_schema()
                self.schema_needs_refresh = False

        while True:
            session: Session = self.SessionFactory()
            try:
                self._flush_event.wait(timeout=0)
                # Drain the queue with a 1s timeout to stay responsive
                data_in: dict | None = self._flush_queue.get(block=True)

                if data_in is None or data_in is True:
                    self._log.info("Shutdown sentinel received. Exiting flush worker.")
                    self._flush_queue.task_done()
                    break # Exit the loop cleanly and immediately

                # Now that we have data, wait here if a migration is running
                while self.migration_in_progress.is_set():
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.25)

                # 2. Extract the metadata from data_in
                device_info_id: int = data_in.get("device_info_id")
                timestamp: datetime = data_in.get("m_time")
                metrics_only: dict = data_in.get("metrics")
                transport_name: str = data_in.get("transport_name")


                # Check for stale data before attempting to process/write to the database. This is based on the timestamp from the transport
                # and the previous saved timestamp.

                is_stale: bool = self._is_stale_data(metrics_only, timestamp)
                if is_stale:
                    self._log.debug("Stale data detected, skipping DB write.")
                    continue

                # pre-process data to coerce floating point, integer as values (from metric_catalog definitions) for safe insertion to the wide table.
                # Also applies SQL-safe column renaming. Only process metric values for the wide table path.
                # The narrow table stores raw key/values as default double precision and is not subject to the same schema
                # constraints, so we can skip processing for narrow table entries with only metric names safe SQL cleaned for
                # consistency.

                wide_data, narrow_data = self._process_raw_metrics(metrics_only)

                if not wide_data:
                    self._flush_queue.task_done()
                    continue
                else:
                    # Add device_info_id and timestamp to wide_data for wide table insertion.  This is needed because the narrow table
                    # applies timestamp and device_info_id to individual rows, whereas the wide table has one row per
                    # timestamp/device with multiple metric columns.
                    valid_row: bool = self._validate_wide_row(wide_data)  # validate wide row without timestamp before insert
                    wide_data: dict = wide_data | {
                        "device_info_id": device_info_id,
                        "m_time": timestamp,
                    }

                if self._stop_event.is_set():
                    break

                while self.migration_in_progress.is_set():
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.5)
                try:
                    with self._schema_lock:
                        with session.begin():
                            # Have to further process the narrow data with the timestamp and device_info_id for insertion
                            # to the narrow table, which applies the timestamp and device_info_id to each metric/value pair.
                            self._flush_batch_narrow(narrow_data, device_info_id, timestamp, session)
                            if self.wide_table_flag:
                                if valid_row:
                                    stmt: Insert = insert(DeviceMetricsWide.__table__).values(**wide_data)
                                    session.execute(stmt)

                        self._commit_stale_state(metrics=metrics_only, time_read=timestamp, is_stale=is_stale)
                        self._log.debug(f"data write complete from [{transport_name}] to timescaledb bridge")
                except (SQLAlchemyError, ValueError) as e:
                    session.rollback()
                    self._log.warning("metrics data write failed.")

                    # Only backlog if setting enabled and DB is down
                    with self._reconnect_lock:
                        tsdb_connected: bool = self.tsdb_connected

                    if self.enable_persistent_storage and not tsdb_connected:
                        # Check if we can get the lock
                        acquired: bool = self._backlog_lock.acquire(blocking=False)
                        try:
                            if acquired:
                                payload: dict[str,Any] = {
                                    "metrics": metrics_only,
                                    "device_info_id": device_info_id, # Preserve ID
                                    "m_time": timestamp,             # Preserve original timestamp
                                    "transport_name": transport_name
                                }
                                self.backlog.enqueue(payload)

                        finally:
                            if acquired:
                                self._backlog_lock.release()

                    # Handle recovery
                    if isinstance(e, SQLAlchemyError):
                        self._set_tsdb_connected(False, "Connection failure")  # noqa: FBT003
                        self._trigger_reconnect()

                finally:

                    self._flush_queue.task_done()

                    # Clear flush event if queue is empty
                    if self._flush_queue.empty():
                        self._flush_event.clear()
            except Exception as e:
                self._log.critical(f" Fatal Flush Worker Crash: {e}")

            finally:
                session.close()

    def _process_raw_metrics(self, datacopy: dict) -> dict:

        try:
            # 1. Get the mapping entry (the tuple) or None if not found
            # 2. If it's a tuple, use the first element [0] (clean_name)
            # 3. If it's not found (None), fallback to the original key 'k'
            # 4. Insert the new clean_key into the dict in place of the original metric name.
            processed_wide_data: dict = {}
            processed_narrow_data: dict = {}

            # Type Coercion based on timescale_type_map values for field definitions.
            # data is coerced to improve compression in metrics' tables.
            INT_TYPES: set[str] = {"SMALLINT", "INTEGER", "BIGINT"}
            FLOAT_TYPES: set[str] = {"REAL", "DOUBLE PRECISION", "NUMERIC"}

            for k, v in datacopy.items():
                mapping_info: Tuple[str, str] = self.metric_mapping.get(k)

                if mapping_info:
                    clean_key, field_type = mapping_info

                    field_type_upper: str = field_type.upper()
                    # Store the original value for narrow table before coercion
                    nv: Any =v

                    try:
                        # Skip coercion if value is None.  Keep as a null to spot the issue in the data base.
                        if v is None:
                            processed_wide_data[clean_key] = None
                            continue

                        if field_type_upper in INT_TYPES:
                            v = int(float(v)) # float(v) handles cases like "16.0"
                        elif  field_type_upper in FLOAT_TYPES:
                            v = float(v)
                        elif field_type_upper == "BOOLEAN":
                            v = bool(v)

                        # TEXT/ASCII/HEX remains as is
                        else:
                            v= str(v)

                        processed_wide_data[clean_key] = v
                        # only return numeric and boolean values to the narrow table to preserve the raw data in the narrow table.
                        if isinstance(nv, (int, float, bool)):
                            processed_narrow_data[clean_key] = float(nv)

                    except (ValueError, TypeError) as e1:
                        # log the specific key that failed, but keep the original value
                        self._log.warning(
                            f"Coercion failed for metric '{k}' (Value: {v}, Target: {field_type_upper}). "
                            f"Error: {e1}. Keeping original value."
                        )

                        processed_wide_data[clean_key] = v
                        if isinstance(nv, (int, float, bool)):
                            processed_narrow_data[clean_key] = float(nv)
                else:
                    # Metric name not in catalog; keep original name/value
                    processed_wide_data[k] = v
                    if isinstance(nv, (int, float, bool)):
                        processed_narrow_data[k] = float(nv)

            self._log.debug("All metrics coerced")
            return processed_wide_data, processed_narrow_data  # noqa: TRY300

        except (TypeError, ValueError) as e2:
            self._log.error(f"Error in _process_raw_metrics {e2} encountered in: {datacopy}")

    def _flush_batch_narrow(self, newData: dict, device_info_id: str, timestamp: datetime, session: Session) -> None:
        """
            Flush new_data narrow-table metric points to the database.
            Any failed writes will be added to the backlog.
        """
        try:
            reading_time =  timestamp
            # Ensure we have a datetime object
            if isinstance(reading_time, str):
                # Convert string to datetime if needed
                reading_time: datetime = datetime.fromisoformat(reading_time)

            # Convert the flat dict into a list of row mappings, excluding strings
            narrow_mappings: list = [
                {
                    "m_time": reading_time,
                    "device_info_id": device_info_id,
                    "metric_name": key,
                    "metric_value": value
                }
                for key, value in newData.items()
                if isinstance(value, (int, float, bool))
            ]

            # Use the optimized 'insert' construct for bulk efficiency
            session.execute(insert(DeviceMetricsNarrow), narrow_mappings)

        except SQLAlchemyError as e:
            self._log.exception(f"Narrow flush failed: {e}")

            try:
                session.rollback()
            except SQLAlchemyError as e2:
                self._log.exception(f"Narrow flush rollback failed: {e2}")

            # === Auto Reconnect handling ===
            self._set_tsdb_connected(False, "Connect unsuccessful")  # noqa: FBT003
            self._trigger_reconnect()


    def _is_stale_data(self, row: dict, timestamp: datetime) -> bool:
        """
        Updates stale-data state tracking using transport data.

        If the incoming row's metric dictionary is identical to the previously
        observed row, this method maintains or initializes the stale-data timer.
        If the row differs, the stale-data state is reset, indicating new data
        flow.

        Args:
            row (dict): Incoming data row with metrics only, timestamp (datetime): timestamp of the read data.

        Notes:
            This method does not initiate reconnect attempts directly; stale-data
            handling is performed in `_handle_stale_event` to centralize state
            and timing logic.

            Tracks rows for stale-data detection.

            Called each time _flush_worker successfully produces a row.

            Behavior:
            - If the row's metrics are identical to the last recorded row, continue
            the stale counter (self.stale_data_start_ts).
            - If different, reset the stale counter.
        """

        stale_limit = timedelta(seconds=int(self.stale_data_timeout))

        time_read: datetime = timestamp
        if time_read is None:
            return False

        if isinstance(time_read, str):
            try:
                time_read = datetime.fromisoformat(time_read)
            except ValueError:
                time_read = datetime.strptime(time_read, "%Y-%m-%d %H:%M:%S%z")

        if self.stale_data_last_row is None:
            # First observation is never stale
            return False

        # 1. Determine if the data has changed
        for key, value in row.items():
            prev_val = self.stale_data_last_row.get(key)

            if isinstance(value, (int, float)) and isinstance(prev_val, (int, float)):
                if not math.isclose(value, prev_val, rel_tol=1e-4, abs_tol=1e-6):
                    return False
            elif value != prev_val:
                return False

        # 2. Data unchanged → check elapsed time
        elapsed: timedelta = time_read - self.stale_data_start_ts
        is_stale: bool = elapsed > stale_limit

        return is_stale

    # 3. Update stale state data
    def _commit_stale_state(self, *, metrics: dict, time_read: datetime, is_stale: bool) -> None:

        if self.stale_data_last_row is None:
            self.stale_data_last_row = metrics.copy()
            self.stale_data_start_ts = time_read
            self.is_stale_data = False
            return

        if not is_stale:
            # Data changed
            self.stale_data_last_row = metrics.copy()
            self.stale_data_start_ts = time_read
            self.is_stale_data = False
            self.stale_event_count = 0
            self.last_stale_event_ts = None
            return

        # Data unchanged and stale threshold crossed
        if is_stale and not self.is_stale_data:
            self.is_stale_data = True
            self._handle_stale_event(time_read, time_read - self.stale_data_start_ts)


    def _handle_stale_event(self, current_time: datetime, total_stale_elapsed: timedelta) -> None:
        """
        Triggers a reconnect max 3 times with a 5-minute gap between attempts.
        """
        # 1. Check if we have already hit the max attempts
        if self.stale_event_count >= self.max_stale_attempts:
            return

        # 2. Check if enough time has passed since the last attempt (if it's not the first one)
        if self.last_stale_event_ts is not None:
            time_since_last_attempt: timedelta = current_time - self.last_stale_event_ts
            if time_since_last_attempt < timedelta(minutes=int(self.retry_delay_mins)):
                return

        # 3. Proceed with the attempt
        self.stale_event_count += 1
        self.last_stale_event_ts: datetime = current_time

        # 4. Trigger reconnect
        if self.request_upstream_reconnect:
            try:
               self.request_upstream_reconnect()
            except Exception:
                self._log.exception("Failed requesting upstream reconnect")

        # Send Notification
        try:
            if getattr(self, "enable_pushover", False):
                msg = (f"Inverter data stale for {total_stale_elapsed.total_seconds()/60:.1f} mins. "
                    f"Attempt {self.stale_event_count} of {self.max_stale_attempts}.")
                self._send_pushover_message(title="Inverter Data Stale", message=msg)
        except Exception:
            self._log.exception("Failed sending Pushover notification.")

    def normalize(self, text: str) -> str:
        # Keeps only letters and numbers
        return "".join(char for char in text if char.isalnum())

    def _send_pushover_message(self, title: str, message: str) -> None:
        """

        Sends a notification through the Pushover service, if enabled.

        This method is used to notify operators of critical conditions such as
        stale-data detection, reconnection attempts, or prolonged database
        outages. Pushover integration must be explicitly enabled and configured
        via `pushover_enabled`, `pushover_token`, and `pushover_user`.

        Args:
            title (str): Short title summarizing the notification event.
            message (str): Detailed message describing the condition or alert.

        Raises:
            Exception: Network errors, authentication issues, or Pushover API
            failures are caught and logged but do not interrupt program flow.

        """
        try:

            token: str = self.pushover_token
            user: str = self.pushover_user

            if not token or not user:
                self._log.error("Pushover enabled but missing token or user key.")
                return

            requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": token,
                    "user": user,
                    "title": title,
                    "message": message,
                },
                timeout=5
            )

            self._log.info("Pushover notification sent.")

        except Exception:
            self._log.exception("Failed to send Pushover notification.")


    # -------------------------
    # Close / cleanup
    # -------------------------
    def close(self) -> None:
        self._log.debug("Closing transport")

        if self.rollup_mgr:
            try:
                self.rollup_mgr.stop_auto_refresh()
            except Exception as e:
                self._log.error(f"Error stopping auto refresh thread: {e}")
                self.rollup_mgr = None

        try:
            self._stop_thread_reconnect()
        except Exception as e:
            self._log.error(f"Error stopping reconnect thread: {e}")
        # Stop flush thread
        try:
            self._stop_event.set()
            self._flush_event.set()
            self._flush_queue.put(None)
            if self._flush_thread.is_alive():
                self._flush_thread.join(timeout=5.0)

        except Exception as e:
            self._log.error(f"Error stopping flush thread: {e}")

        try:
            if self.engine:
                self.engine.dispose()
        except Exception as e:
            self._log.error(f"Error disposing engine: {e}")

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
class BacklogManager:
    """
    Manages persistent backlog storage and replay for TimescaleDB writes.

    Owns:
    - in-memory backlog list
    - disk persistence (jsonl)
    - backlog synchronization
    - replay into the flush queue

    Does NOT:
    - talk to the database
    - manage sessions
    - manage reconnect logic
    """

    def __init__(
        self,
        backlog_file_path: Optional[Path],
        max_backlog_age: int,
        max_backlog_size: int,
        flush_queue: queue.Queue,
        flush_event: threading.Event,
        backlog_lock: threading.RLock,
        log: logging.Logger
    ) -> None:

        self.backlog_file_path: Path  = backlog_file_path
        self.max_backlog_age: int = max_backlog_age
        self.max_backlog_size: int = max_backlog_size
        self._flush_queue: queue.Queue = flush_queue
        self._flush_event: threading.Event = flush_event
        self._backlog_lock: threading.RLock = backlog_lock
        self._log: logging.Logger = log

        self.backlog_points: list[dict] = []

    # -------------------------
    # Load Persistent backlog
    # -------------------------

    def load_from_disk(self) -> None:
        if not self.backlog_file_path or not self.backlog_file_path.exists():
            return

        now: datetime = datetime.now().astimezone()
        loaded: list[dict] = []

        try:
            with self._backlog_lock: # Protect memory/disk during load
                with open(self.backlog_file_path, "r") as f:
                    for line in f:
                        clean: str = line.strip()
                        if not clean:
                            continue
                        try:
                            point: dict[str, Any] = json.loads(clean)
                            ts: str = point.get("m_time")
                            if not ts:
                                continue
                            m_time: datetime = datetime.fromisoformat(ts)
                            if now - m_time < timedelta(seconds=int(self.max_backlog_age)):
                                loaded.append(point)
                        except (json.JSONDecodeError, ValueError) as e:
                            self._log.info("Skipping corrupted backlog line: %s", e)
                self.backlog_points = loaded
                self._sync_to_disk()

            self._log.info("Loaded %d points from disk", len(loaded))

        except Exception:
            self._log.exception("Failed to process backlog file")

    def enqueue(self, point: dict) -> None:
        if isinstance(point, list):
            raise TypeError("enqueue() does not accept lists, only single dict or None.")

        with self._backlog_lock:
            # 1. Check if we are at or over the limit
            if len(self.backlog_points) >= self.max_backlog_size:
                # Drop the oldest point (Index 0) to make room
                self.backlog_points.pop(0)
                self._log.warning(f"Max backlog size ({self.max_backlog_size}) reached. Dropped oldest point.")
                # 2. Since we dropped a point, we must rewrite the file
                # otherwise the disk file will contain more points than the list.
                self.backlog_points.append(point)
                self._sync_to_disk()
            else:
                # 3. Normal path: append to list and disk
                self.backlog_points.append(point)
                self._append_to_disk(point)

    def replay_to_queue(self) -> int:
        """Transfers backlog to queue. Returns count replayed."""
        count = 0
        with self._backlog_lock:
            if not self.backlog_points:
                return 0
            count: int = len(self.backlog_points)
            if count > 0:
                self._log.debug(f"Replaying {count} points to flush queue.")
                for payload in self.backlog_points:
                    self._flush_queue.put(payload)
                self.backlog_points.clear()
                self._sync_to_disk()
        return count

    def _append_to_disk(self, point: dict) -> None:
        if not self.backlog_file_path:
            return
        json_string = json.dumps(point, default=str)
        # trap of errata "true" in points.
        cleaned_json: str = re.sub(r'^true', '', json_string, flags=re.IGNORECASE | re.MULTILINE)
        try:
            self.backlog_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.backlog_file_path, "a") as f:
                f.write(cleaned_json + "\n")
        except Exception as e:
            self._log.error(f"Failed to append point to backlog disk: {e}")

    def _sync_to_disk(self) -> None:
        if not self.backlog_file_path:
            return
        with self._backlog_lock:
            try:
                self.backlog_file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.backlog_file_path, "w") as f:
                    for p in self.backlog_points:
                        f.write(json.dumps(p, default=str) + "\n")
            except Exception as e:
                self._log.error(f"Failed to sync backlog to disk: {e}")


class RollupManager:
    """ summary logic
        The RollupManger class creates the structures that enable TimescaleDB rollup views.
        After all structures are built:
        1 RollupManager wakes up.
        2 RollupManager tells BacklogManager to put everything into the _flush_queue if backlog data exists.
            The _flush_queue is the threaded queue object that accepts PPG data obtained from the source transport.
        3 RollupManager calls _flush_queue.join() (it pauses here).
        4 _flush_worker finishes writing everything to the Hypertable and calls task_done() for each.
        5 RollupManager resumes and calls refresh_continuous_aggregate.

        Backlog Safety: The _refresh_rollup_loop wraps replay_to_queue() in the _backlog_lock and waits for completion via .join().
        Attribute Persistence: All granular intervals (e.g., hourly_compress_after_interval) are mapped from the rollup_policy in __init__.
        Live State: tsdb_connected and current_metric_count are implemented as @property to track the timescaledb class state in real-time.
        SQL Execution: The SET LOCAL work_mem uses the single-quote fix to prevent f-string placeholder errors.
    """

    def __init__(
        self,
        rollup_policy: dict,
        SessionFactory: Callable[..., Session],
        Engine: engine,
        wide_table_flag: bool,
        migration_in_progress: threading.Event,
        send_pushover_message,
        log: logging.Logger,
        backlog_lock: threading.RLock,
        flush_queue: queue.Queue,
        backlog: BacklogManager,
        reconnect_lock: threading.Lock
        ) -> None:

        self.rollup_policy: dict = rollup_policy
        self.SessionFactory: Callable[..., Session] = SessionFactory
        self.engine: engine = Engine
        self.wide_table_flag: bool = wide_table_flag
        self.migration_in_progress: threading.Event = migration_in_progress
        self._send_pushover_message = send_pushover_message
        self._log: logging.Logger = log
        self._backlog_lock: threading.RLock = backlog_lock
        self._flush_queue: queue.Queue  = flush_queue
        self.backlog: 'BacklogManager'  = backlog
        self._reconnect_lock: threading.Lock = reconnect_lock

        self._refresh_rollup_thread = threading.Thread(target=self._refresh_rollup_loop, daemon=True, name="RollupAutoRefreshThread")
        self._stop_refresh_rollup_event: threading.Event = getattr(self, "_stop_refresh_rollup_event", threading.Event())

        self.performance_tiers: dict[str, dict[str, Any]] = {
        "tier_low":    {"count": 50,  "work_mem": "32MB",  "lock_timeout": "10s", "flush_batch_size": 10},
        "tier_medium": {"count": 100, "work_mem": "64MB",  "lock_timeout": "15s", "flush_batch_size": 20},
        "tier_high":   {"count": 200, "work_mem": "128MB", "lock_timeout": "30s", "flush_batch_size": 40},
        }

        """
        Rollup Type
            refresh rollup    start_offset   compress_after   reason
            1 Hour	          3 hours	     2 days	          Allows a 3-hour window for late data before locking it via compression.
            1 Day	          3 days	     2 weeks	          Ensures daily rollups are finalized before compressing.
            1 Week	          3 weeks	     2 months	      Larger window helps capture any delayed source data updates.
            1 Month	          3 months	     6 months	      Maximum safety for long-term historical accuracy.
        """

        # hypertable defaults
        self.hypertable_defaults: dict[str, Any] = {
            "compress_segmentby_narrow": "device_info_id, metric_name",
            "compress_segmentby_wide": "device_info_id",
            "time_column": "m_time",
            "compress_orderby": "m_time DESC",
            "hourly_chunk_time_interval": "1 day",
            "hourly_compress_after_interval": "2 days",
            "daily_chunk_time_interval": "7 days",
            "daily_compress_after_interval": "2 weeks",
            "weekly_chunk_time_interval": "1 month",
            "weekly_compress_after_interval": "2 months",
            "monthly_chunk_time_interval": "4 months",
            "monthly_compress_after_interval": "6 months",
        }

        # rollup defaults, continuous aggregate bucket sizes
        self.rollup_defaults: dict[str, Any] = {
            "hourly_rollup_bucket": "1 hour",
            "hourly_rollup_start": "3 hours",
            "daily_rollup_bucket": "1 day",
            "daily_rollup_start": "3 days",
            "weekly_rollup_bucket": "1 week",
            "weekly_rollup_start": "3 weeks",
            "monthly_rollup_bucket": "1 month",
            "monthly_rollup_start": "3 months",
            "anchor_start_time_utc": "2000-01-01 00:00:00+00",  # default anchor time for rollup alignment
        }

        self.if_not_exists = True

        # Rollup /Hypertable Settings extracted from rollup_policy  (user can override defaults via rollup_policy)
        self.current_metric_count: int = self.rollup_policy.get("current_metric_count", 0)
        self.auto_refresh_interval: int = self.rollup_policy.get("auto_refresh_interval")
        self.enable_auto_refresh:bool = self.rollup_policy.get("enable_auto_refresh", True)
        self.enable_rollups = bool(self.rollup_policy.get("enable_rollups", True))
        self.drop_after: str = self.rollup_policy.get("drop_after")
        self.migrate_data = bool(self.rollup_policy.get("migrate_data",True))
        self.enable_compression = bool(self.rollup_policy.get("enable_compression",True))

        # Compression settings
        self.compress_segmentby_narrow: str= self.hypertable_defaults.get("compress_segmentby_narrow")
        self.compress_segmentby_wide: str= self.hypertable_defaults.get("compress_segmentby_wide")
        self.compress_orderby: str= self.hypertable_defaults.get("compress_orderby")
        self.time_column: str= self.hypertable_defaults.get("time_column")

        self.hourly_chunk_time_interval: str = self.hypertable_defaults.get("hourly_chunk_time_interval")
        self.daily_chunk_time_interval: str = self.hypertable_defaults.get("daily_chunk_time_interval")
        self.weekly_chunk_time_interval: str = self.hypertable_defaults.get("weekly_chunk_time_interval")
        self.monthly_chunk_time_interval: str = self.hypertable_defaults.get("monthly_chunk_time_interval")

        self.hourly_compress_after_interval: str = self.hypertable_defaults.get("hourly_compress_after_interval")
        self.daily_compress_after_interval: str = self.hypertable_defaults.get("daily_compress_after_interval")
        self.weekly_compress_after_interval: str = self.hypertable_defaults.get("weekly_compress_after_interval")
        self.monthly_compress_after_interval: str = self.hypertable_defaults.get("monthly_compress_after_interval")

        # Rollup view settings
        self.anchor_start_time_utc: str = self.rollup_defaults.get("anchor_start_time_utc")

        self.hourly_rollup_bucket: str = self.rollup_defaults.get("hourly_rollup_bucket")
        self.daily_rollup_bucket: str = self.rollup_defaults.get("daily_rollup_bucket")
        self.weekly_rollup_bucket: str = self.rollup_defaults.get("weekly_rollup_bucket")
        self.monthly_rollup_bucket: str = self.rollup_defaults.get("monthly_rollup_bucket")

        self.hourly_rollup_start: str = self.rollup_defaults.get("hourly_rollup_start")
        self.daily_rollup_start: str = self.rollup_defaults.get("daily_rollup_start")
        self.weekly_rollup_start: str = self.rollup_defaults.get("weekly_rollup_start")
        self.monthly_rollup_start: str = self.rollup_defaults.get("monthly_rollup_start")

    @property
    def tsdb_connected(self) -> bool:
        """Always returns the live connection state from the shared policy dict.
            This allows the RollupManager to react immediately to changes in TSDB connection status,
            which is critical for coordinating rollup refreshes and backlog replays.
        """
        val: bool = self.rollup_policy.get("tsdb_connected", False)
        # If val is the boolean False, the expression 'val is True or val == "True"' will return False.
        # basically tries to capture string "True" as well as boolean True if somehow the config was passed as a string.
        if val is True or val == "True":

            return val is True
        else:
            return False


    def setup_schema(self) -> None:
        """
        This method orchestrates the entire schema setup process, including hypertable creation,
        compression setup, retention policy, and continuous aggregate rollup creation.
        Called once during TSDB startup.
        """

    # 1 Hypertable & Policies
        try:
            self.ensure_hypertables()
            self._log.info("Hypertable check/creation complete")
        except Exception as e:
            self._log.error(f"Hypertable creation failed: {e}")

    # 2 Enable compression (if configured)
        try:
            if self.enable_compression:
                self.ensure_compression_enabled()
        except Exception as e:
            self._log.error(f"Enable compression failed: {e}")

    # 3 Add retention policy
        try:
            self.ensure_retention_policy()
        except Exception as e:
            self._log.error(f"Add retention policy failed: {e}")

    # 4 Setup continuous aggregate rollups
        if self.enable_rollups:
            # 4a
            try:
                self.setup_with_retry()
            except Exception as e:
                self._log.error(f"Aggregate Rollup setup failed: {e}")
            # 4b
            try:
                self.refresh_rollups(force_full=True)
            except Exception as e:
                self._log.error(f"Refresh Rollup failed: {e}")

    # 5 Start the rollup thread.  Called from TimescaleDB class upon connection to the database.
    def start_auto_refresh(self) -> None:

        if self._refresh_rollup_thread.is_alive():
            self._log.debug("Auto refresh thread already running.")
            return
        else:
            # start _refresh_rollup_thread after connect completes successfully
            self._refresh_rollup_thread.start()
            self._log.debug("Auto rollup refresh thread started.")

    # -------------------------
    # 6. Hypertable creation
    # -------------------------
    def ensure_hypertables(self) -> None:
        """Convert base tables into TimescaleDB hypertables.
           If the hypertables already exist, this function will do nothing due to the 'if_not_exists' flag.
           If wide_table_flag is False, only device_metrics_narrow will be processed.
        """
        # 1. The tables that need to be processed
        tables: List[str] = ["device_metrics_narrow"]
        if self.wide_table_flag:
            tables.append("device_metrics_wide")

        # 2. shared parameters
        params: dict[str, Any] = {
            "time_col": getattr(self, "time_column", "m_time"),
            "if_exists": getattr(self, "if_not_exists", True),
            "migrate": getattr(self, "migrate_data", True),
        }

        try:
            with self.SessionFactory() as session:
                for table in tables:

                    session.execute(
                        text(f"SELECT create_hypertable('{table}', :time_col, if_not_exists => :if_exists, migrate_data => :migrate)"),
                        params
                    )
                session.commit()
                self._log.debug(f"Hypertable creation ensured for: {', '.join(tables)}")

        except SQLAlchemyError as e:
            self._log.error("Failed to ensure hypertables: %s", e)


    # -------------------------
    # 7. Enable compression
    # -------------------------

    def ensure_compression_enabled(self) -> None:
        """
        Enable TimescaleDB compression on device_metrics_narrow and device_metrics_wide tables.
        """
        with self.SessionFactory() as session:
            self._log.info("Setting up compression policy")
            if not session:
                self._log.error("Cannot set up compression — not tsdb_connected.")
                return

            # Define tables and their specific segmentation configuration
            tables_to_configure: List[Tuple[str]] = [
                ("device_metrics_narrow", self.compress_segmentby_narrow),
            ]
            if self.wide_table_flag:
                tables_to_configure.append(("device_metrics_wide", self.compress_segmentby_wide))

            # Repetitive SQL Execution
            for table_name, segment_by in tables_to_configure:
                self._apply_compression_to_table(session, table_name, segment_by)

            #  Policy Creation
            with session.begin():
                chunk_intervals: List[str] = [
                    self.hourly_chunk_time_interval,
                    self.daily_chunk_time_interval,
                    self.weekly_chunk_time_interval,
                    self.monthly_chunk_time_interval,
                ]

                for table_name, _ in tables_to_configure:
                    for chunk_interval in chunk_intervals:
                        self.ensure_compression_policy(table_name, chunk_interval)

                session.commit()

    def _apply_compression_to_table(self, session: Session, table_name: str, segment_by: str) -> None:
        """Helper to apply compression ALTER TABLE statement with error handling."""
        try:
            sql: TextClause = text(
                f"ALTER TABLE {table_name} SET ("
                f"timescaledb.compress, "
                f"timescaledb.compress_orderby = '{self.compress_orderby}', "
                f"timescaledb.compress_segmentby = '{segment_by}'"
                ");"
            )
            session.execute(sql)
            session.commit()
            self._log.debug(f"Compression enabled on {table_name}")
        except SQLAlchemyError as e:
            self._log.error(f"Error enabling compression on {table_name}: {e}")
            try:
                session.rollback()
            except SQLAlchemyError as e2:
                self._log.error(f"Rollback error for {table_name}: {e2}")

    # -------------------------
    # 7b. Add compression policy
    # -------------------------
    def ensure_compression_policy(self, source, chunk_interval) -> None:
        """Automatically compress chunks older than chunk_time_interval.
        Parameters:
            source (str): The hypertable to which the compression policy will be applied (e.g. "device_metrics_narrow").
            chunk_interval (str): The time interval after which chunks should be compressed (e.g. "1 day", "7 days").
        """
        with self.SessionFactory() as session:

            if not session:
                    self._log.error("Cannot add compression policy — not tsdb_connected.")
                    return

            try:
                sql: TextClause = text(f"SELECT add_compression_policy('{source}', compress_after => INTERVAL '{chunk_interval}', if_not_exists => TRUE);")

                session.execute(sql)

                self._log.debug(f"_add_compression_policy {source} for {chunk_interval} executed")
            except SQLAlchemyError as e:
                self._log.error(f"_add_compression_policy {source} for {chunk_interval} error: {e}")
                try:
                    session.rollback()
                except SQLAlchemyError as e2:
                    self._log.error(f"_add_compression_policy {source} for {chunk_interval} rollback error: {e2}")

    # -------------------------
    # 9. Add retention policy
    # -------------------------
    def ensure_retention_policy(self) -> None:
        """
        Drop old data automatically after drop_after interval.
        This method first removes any existing retention policy to ensure that changes to the drop_after interval are applied correctly.
        """
        with self.SessionFactory() as session:
            if not session:
                    self._log.error("Cannot add retention policies — not tsdb_connected.")
                    return

            drop_after: str = self.drop_after

            try:
                sql1: str = "SELECT remove_retention_policy('device_metrics_narrow', if_exists => True);"
                sql2: str = f"SELECT add_retention_policy('device_metrics_narrow', INTERVAL '{drop_after}');"

                session.execute(text(sql1))
                session.execute(text(sql2))
                session.commit()
                self._log.debug("_add_retention_policy_narrow executed")
            except SQLAlchemyError as e:
                self._log.error(f"_add_retention_policy_narrow error: {e}")
                try:
                    session.rollback()
                except SQLAlchemyError as e2:
                    self._log.error(f"_add_retention_policy_narrow rollback error: {e2}")

            if self.wide_table_flag:
                try:
                    sql1b: str = "SELECT remove_retention_policy('device_metrics_wide', if_exists => True);"
                    sql2b: str = f"SELECT add_retention_policy('device_metrics_wide', INTERVAL '{drop_after}');"

                    session.execute(text(sql1b))
                    session.execute(text(sql2b))
                    session.commit()
                    self._log.debug("_add_retention_policy_wide executed")
                except SQLAlchemyError as e:
                    self._log.error(f"_add_retention_policy_wide error: {e}")
                    try:
                        session.rollback()
                    except SQLAlchemyError as e2:
                        self._log.error(f"_add_retention_policy_wide rollback error: {e2}")

    # -------------------------
    # 10 Rollups
    # -------------------------
    def setup_with_retry(self) -> None:
        """_summary_
         This method wraps the ensure_rollups() call with retry Logic to handle potential lock timeouts that can occur if the flush
            thread is actively writing to the source tables while we attempt to set up or refresh continuous aggregates.
            If a lock timeout is detected, the method will wait for 5 seconds and retry, up to a maximum of 3 attempts.
            This allows the flush thread to complete its current batch and release locks before we try again, improving
            the chances of a successful rollup setup without manual intervention.
            If the error is not a lock timeout, it will be raised immediately without retrying, as it likely indicates
            a different issue that needs attention.

        """
        max_rollup_retries = 3
        for attempt in range(max_rollup_retries):
            try:
                self.ensure_rollups()
                break # Success!
            except Exception as e:
                if "lock_timeout" in str(e):
                    self._log.warning(f"Lock timeout on attempt {attempt+1}. Retrying...")
                    time.sleep(5) # Wait for flush thread to clear
                else:
                    raise  # Real error, don't retry

    # 10a
    def ensure_rollups(self) -> None:
        """
        Sets up continuous aggregate rollups based on predefined configurations.
        Uses a Scan-then-Purge approach to handle hierarchical dependencies safely.
        Checks if rollups need to be rebuilt and creates them accordingly.
        The method scans existing rollup views to determine if any bucket interval changes have occurred.
        If a change is detected, it purges all rollups in the correct order (weekly -> daily -> hourly) to ensure
        a clean slate for rebuilding. After purging, it creates or verifies each rollup view from the
        bottom up (hourly -> daily -> weekly) to maintain hierarchical integrity.
        The entire process is wrapped in a migration lock to pause the flush thread and prevent conflicts during schema changes.

        """
        self.migration_in_progress.set()
        self._log.info("Pausing flush thread for migration...")

        try:
            with self.SessionFactory() as session:
                with self._reconnect_lock:
                    tsdb_connected: bool = self.tsdb_connected

                if not tsdb_connected or not session:
                    self._log.error("Cannot set up rollups — not tsdb_connected.")
                    return

                self._log.info("Starting continuous aggregate setup...")

                # 1. Configuration Setup
                contexts: list[dict[str]] = [
                    {
                        "table_name": "device_metrics_narrow",
                        "segments": {
                            "hourly_rollup": "hourly_rollup_narrow",
                            "daily_rollup": "daily_rollup_narrow",
                            "weekly_rollup": "weekly_rollup_narrow",
                            "monthly_rollup": "monthly_rollup_narrow",
                        }
                    }
                ]
                if self.wide_table_flag:
                    contexts.append({
                        "table_name": "device_metrics_wide",
                        "segments": {
                            "hourly_rollup": "hourly_rollup_wide",
                            "daily_rollup": "daily_rollup_wide",
                            "weekly_rollup": "weekly_rollup_wide",
                            "monthly_rollup": "monthly_rollup_wide",
                        }
                    })

                view_configs: List[Tuple[str]] = [
                    ("hourly_rollup", self.hourly_rollup_bucket, self.hourly_rollup_start, self.hourly_chunk_time_interval),
                    ("daily_rollup", self.daily_rollup_bucket, self.daily_rollup_start, self.daily_chunk_time_interval),
                    ("weekly_rollup", self.weekly_rollup_bucket, self.weekly_rollup_start, self.weekly_chunk_time_interval),
                    ("monthly_rollup", self.monthly_rollup_bucket, self.monthly_rollup_start, self.monthly_chunk_time_interval),
                ]

                # 2. Scan Phase: Detect if any bucket change exists across the whole stack
                any_rebuild_needed = False
                for context in contexts:
                    for view_key, bucket, _, _ in view_configs:
                        view_name: str = context["segments"][view_key]
                        if self.rollup_needs_rebuild(session, view_name, bucket):
                            any_rebuild_needed = True
                            break
                    if any_rebuild_needed:
                        break

                # 3. Purge Phase: If a change is detected, wipe the slate clean in correct order
                if any_rebuild_needed:
                    self._log.info("Bucket change detected. Purging all rollups for clean rebuild.")
                    # This method must drop Weekly -> Daily -> Hourly with sequential commits
                    self._drop_all_continuous_aggregates(session)
                    # Purge orphaned scheduler jobs
                    self._purge_ghost_jobs(session)

                # 4. Creation Phase: Build/Verify Bottom-Up (Hourly -> Daily -> Weekly)
                for context in contexts:
                    source_table: str = context["table_name"]
                    current_source: str = source_table  # Reset source for each context (Narrow vs Wide)
                    rollup_segments: str = context["segments"]

                    for view_key, bucket, start_offset, chunk_time_interval in view_configs:
                        view_name = rollup_segments[view_key]

                        # If the view doesn't exist (due to purge or first run), create it
                        if not self._view_exists(session, view_name):
                            self._log.info(f"Creating {view_name} from {current_source}...")
                            self._create_narrow_rollup(session, current_source, view_name, bucket, start_offset, chunk_time_interval)
                        else:
                            self._log.debug(f"View {view_name} is already up to date.")

                        # Update current_source for Hierarchical Aggregation
                        # Note: Monthly is kept on the source_table
                        if view_key == 'weekly_rollup':
                            current_source = source_table
                        elif view_key != 'monthly_rollup':
                            current_source = view_name

                self._log.info("Continuous aggregate setup completed successfully.")
        finally:
            # 2. Always re-enable flushing, even if migration fails
            self.migration_in_progress.clear()
            self._log.info("Resuming flush thread.")


    # -------------------------
    # 10d
    # -------------------------

    def _create_narrow_rollup(self, session: Session, source: str, view_name: str, bucket_interval: str, start_offset: str, chunk_time_interval: str) -> None:
        """
        Creates a continuous aggregate with proper hierarchical logic and locking.
        Parameters:
            session: Active database session for executing SQL commands.
            source: The source table or view from which to aggregate (e.g., "device_metrics_narrow" or a previous rollup view).
            view_name: The name of the continuous aggregate view to create.
            bucket_interval: The time bucket size for aggregation (e.g., "1 hour", "1 day").
            start_offset: The offset for the continuous aggregate policy (e.g., "3 hours", "3 days").
            chunk_time_interval: The chunk time interval for compression policy (e.g., "1 day", "7 days").

        """
        r_settings: dict = self._get_dynamic_settings()

        if not session:
            self._log.error("Cannot create rollup — not connected.")
            return

        # 1. Set local lock timeout to fail fast if blocked by flush thread.  Set dynamically from settings.
        session.execute(text(f"SET LOCAL lock_timeout = '{r_settings['lock_timeout']}';"))

        # 2. Determine Aggregation Mode
        # If source is another view, we MUST use rollup(). If it's a hypertable, use stats_agg().
        reading_from_raw = source in ["device_metrics_narrow", "device_metrics_wide"]

        if reading_from_raw:
            agg_func = "stats_agg(metric_value)"
            min_func = "metric_value"
            max_func = "metric_value"
        else:
            agg_func = "rollup(stats_summary)"
            min_func = "min_value"
            max_func = "max_value"

        try:
            # 3. Branch for Wide vs Narrow
            if self.wide_table_flag and "wide" in source:
                # mapping columns for wide table rollups
                self._create_wide_rollup(session, source, view_name, bucket_interval, agg_func)
            else:
                # 4. Standard Narrow View Creation
                # Uses 3-arg time_bucket for IANA timezone midnight alignment
                session.execute(text(f"""
                    CREATE MATERIALIZED VIEW {view_name}
                    WITH (timescaledb.continuous = true) AS
                    SELECT
                        time_bucket(INTERVAL '{bucket_interval}', m_time, '{machine_timezone}') AS m_time,
                        device_info_id,
                        metric_name,
                        MIN({min_func}) AS min_value,
                        MAX({max_func}) AS max_value,
                        {agg_func} AS stats_summary
                    FROM {source}
                    GROUP BY 1, 2, 3
                    WITH NO DATA;
                """))  # noqa: S608

            # 5. Apply Policies & Index
            self._add_aggregate_policy(session, view_name, bucket_interval, start_offset)


            # 6. Finalize the view so it is available as a 'source' for the next view in the loop
            session.commit()
            self._log.info(f"Successfully created hierarchical rollup: {view_name}")

        except Exception as e:
            session.rollback()
            self._log.error(f"Failed to create {view_name}: {e}")
            raise

    def _add_aggregate_policy(self, session: Session, view_name: str, bucket_interval: str, start_offset: str) -> None:
        """
        Applies refresh, retention, and compression policies to a newly created view
        using granularity-specific settings from hypertable_policy.
        Parameters:
            session: Active database session for executing SQL commands.
            view_name: The name of the continuous aggregate view to which policies will be applied.
            bucket_interval: The time bucket size for the continuous aggregate (e.g., "1 hour", "1 day").
            start_offset: The offset for the continuous aggregate policy (e.g., "3 hours", "3 days").
        """
        # 1. Map view name to its granularity key for lookup
        name_lower: str = view_name.lower()

        # Define the supported granularities
        granularities: List[str] = ["hourly", "daily", "weekly", "monthly"]

        # Find the first matching granularity or use "default"
        granularity: str = next((g for g in granularities if g in name_lower), "default")

        # Dynamically retrieve the value from self
        # This replaces the need for self.get() as the values are already stored as attributes
        compress_after: str = getattr(self, f"{granularity}_compress_after_interval")
        drop_after: str = getattr(self, "drop_after", "1 year")  # Default retention if not specified


        try:
            session.execute(text("SET LOCAL lock_timeout = '10s';"))

            # 2. Add Continuous Aggregate Refresh Policy
            session.execute(text(f"""
                SELECT add_continuous_aggregate_policy(
                    '{view_name}',
                    start_offset      => INTERVAL '{start_offset}',
                    end_offset        => INTERVAL '{bucket_interval}',
                    initial_start     => '{self.anchor_start_time_utc}'::timestamptz,
                    schedule_interval => INTERVAL '{bucket_interval}',
                    if_not_exists     => true
                );
            """))

            # 3. Add Data Retention Policy (Specific to the view)
            session.execute(text(f"""
                SELECT add_retention_policy(
                    '{view_name}',
                    drop_after => INTERVAL '{drop_after}',
                    if_not_exists => true
                );
            """))

            # 4. Enable and Configure Compression Policy for the view
            if self.enable_compression:
                # For CAGGs, enable compression via ALTER MATERIALIZED VIEW
                session.execute(text(f"ALTER MATERIALIZED VIEW {view_name} SET (timescaledb.compress = true);"))

                # Add compression policy using the granularity-matched interval
                session.execute(text(f"""
                    SELECT add_compression_policy(
                        '{view_name}',
                        compress_after => INTERVAL '{compress_after}',
                        if_not_exists => true
                    );
                """))

            # 5. Performance Index
            safe_view_name: str = view_name.replace('"', '').replace('.', '_')
            index_name: str = f"idx_{safe_view_name}_time"

            session.execute(text(f"""
                CREATE INDEX IF NOT EXISTS {index_name} ON {view_name} (m_time DESC);
            """))

            session.commit()
            self._log.info(f"Policies applied to {view_name}: Retention={drop_after}, Compression After={compress_after}")

        except Exception as e:
            session.rollback()
            self._log.error(f"Failed to apply policies to {view_name}: {e}")
            raise


    def _create_wide_rollup(self, session: Session, source: str, view_name: str, bucket_interval: str, agg_func: str) -> None:
        """
        Creates the complex 'wide' materialized view with dynamic column mapping.
        Parameters:
            session: Active database session for executing SQL commands.
            source: The source table or view from which to aggregate (e.g., "device_metrics_wide" or a previous wide rollup view).
            view_name: The name of the continuous aggregate view to create.
            bucket_interval: The time bucket size for aggregation (e.g., "1 hour", "1 day").
            agg_func: The aggregate function to use for the stats_summary column
                (e.g., "stats_agg(metric_value)" or "rollup(stats_summary)"), determined by whether the source
                is a raw hypertable or another view.
        """
        try:
            # 1. Fail-fast locking for the wide table migration
            session.execute(text("SET LOCAL lock_timeout = '15s';"))

            # 2. Build the dynamic SQL
            # We pass agg_func ('stats_agg' or 'rollup') to ensure hierarchical consistency
            metric_columns: list = self._resolve_metric_columns(session, agg_func)

            sql: str = f"""
                CREATE MATERIALIZED VIEW {view_name}
                WITH (timescaledb.continuous = true) AS
                SELECT
                    time_bucket(INTERVAL '{bucket_interval}', m_time, '{machine_timezone}') AS m_time,
                    device_info_id,
                    {', '.join(metric_columns)}
                FROM {source}
                GROUP BY 1, 2
                WITH NO DATA;
            """  # noqa: S608

            self._log.debug(f"Executing Wide View DDL for {view_name}")
            session.execute(text(sql))

            # 3. Commit the view structure before applying policies
            session.commit()

        except Exception as e:
            session.rollback()
            self._log.error(f"_create_wide_rollup error for {view_name}: {e}")
            raise

    def _resolve_metric_columns(self, session: Session, agg_func: str) -> List[str]:
        """
        Generates SQL aggregate expressions for the 'wide' table.
        - If reading from hypertable: uses stats_agg(column)
        - If reading from another view: uses rollup(stats_summary_column)
        This method dynamically fetches all metric columns from the catalog and constructs the appropriate aggregate expressions
        based on the aggregation level, ensuring that the wide rollups maintain hierarchical consistency regardless of the source.
        """
        # 1. Fetch all clean column names from the catalog that are not ASCII values.
        result = session.execute(text(
            "SELECT clean_column_name "
            "FROM metric_catalog "
            "WHERE data_type NOT IN ('TEXT', 'BOOLEAN') "
            "ORDER BY clean_column_name"
        ))

        column_names: List[str] = list(result.scalars())

        metric_expressions: list = []

        # Check if we are at the base level (reading raw data) or hierarchical (reading an aggregate)
        is_base_level: bool = agg_func == 'stats_agg(metric_value)'

        for col in column_names:
            if is_base_level:
                # Base Level: Creating the first aggregate (e.g., Hourly) from the Hypertable
                # Columns: min_Col, max_Col, stats_summary_Col
                metric_expressions.append(f"MIN({col}) AS min_{col}")
                metric_expressions.append(f"MAX({col}) AS max_{col}")
                metric_expressions.append(f"stats_agg({col}) AS stats_summary_{col}")
            else:
                # Hierarchical Level: Creating Daily from Hourly, or Weekly from Daily
                # We must target the Aliased columns created by the previous view level.
                # Example: rollup(stats_summary_Volts) AS stats_summary_Volts
                metric_expressions.append(f"MIN(min_{col}) AS min_{col}")
                metric_expressions.append(f"MAX(max_{col}) AS max_{col}")
                metric_expressions.append(f"rollup(stats_summary_{col}) AS stats_summary_{col}")

        return metric_expressions


    def rollup_needs_rebuild(self, session: Session, view_name: str, bucket_interval: str) -> bool:
        """
        Checks if a rollup exists and if its bucket matches the current config.
        Returns True if the rollup is missing or configuration is mismatched.
        Parameters:
            session: Active database session for executing SQL commands.
            view_name: The name of the continuous aggregate view to check.
            bucket_interval: The expected time bucket size for the view (e.g., "1 hour", "1 day").
        returns:
            bool: True if the rollup needs to be rebuilt (missing or config mismatch),
                  False if it exists and matches the expected bucket interval.
        """
        # 1. Map friendly terms to PG Interval inputs
        # This allows 'monthly' -> '1 month', while letting '2 hours' pass through as-is
        mapping: dict[str, str] = {
            "monthly": "1 month",
            "weekly": "7 days",
            "daily": "1 day",
            "hourly": "1 hour"
        }
        target_pg_val: str = mapping.get(bucket_interval.lower(), bucket_interval)

        try:
            # 2. Query the TimescaleDB catalog for the view definition
            # Use a bind parameter :view_name for security and performance
            check_sql: TextClause = text("""
                SELECT view_definition
                FROM timescaledb_information.continuous_aggregates
                WHERE view_name = :view_name
            """)
            view_def: Optional[str] = session.scalar(check_sql, {"view_name": view_name})

            # Logic: If it doesn't exist, we definitely need to build it
            if not view_def:
                self._log.debug(f"Rollup {view_name} does not exist. Rebuild required.")
                return True

            # 3. If the result exists, extract the 'interval' string from the definition
            # We use Postgres regex_match to find the first argument of time_bucket()
            # Pattern looks for: time_bucket('interval_text', ...)
            extract_sql: TextClause = text("""
                SELECT (regexp_match(:vdef, 'time_bucket\\(''([^'']+)''', 'i'))[1]
            """)
            current_interval_str: Optional[str] = session.scalar(extract_sql, {"vdef": view_def})

            if not current_interval_str:
                self._log.warning(f"Could not parse time_bucket interval from {view_name} definition.")
                return True

            # 4. Final Comparison: Let PostgreSQL handle the semantic equality
            # This correctly recognizes that '01:00:00'::interval = '1 hour'::interval
            match_sql: TextClause = text("SELECT (:current)::interval = (:target)::interval")
            is_match: bool = session.scalar(match_sql, {"current": current_interval_str, "target": target_pg_val})

            if not is_match:
                self._log.info(
                    f"Config mismatch for {view_name}. "
                    f"Found: {current_interval_str}, Expected: {target_pg_val}. Rebuild required."
                )
                return True
            else:
                # 4. Exists and matches config
                self._log.info(
                    f"Rollup config matches for {view_name}. "
                    f"Expected: {bucket_interval} and received {target_pg_val}. No rebuild required."
                )
                return False

        except SQLAlchemyError as e:
            self._log.error(f"Database error while checking rollup {view_name}: {e}")
            # Default to True to ensure we don't skip a necessary build on error
            return True


    # -------------------------
    #  Determine wide vs narrow table usage for resource settings
    # -------------------------
    def _get_dynamic_settings(self) -> dict:
        """
        Returns dynamic settings based on the current metric count.
        This allows the system to automatically adjust performance tiers based on the size of the data,
        without requiring manual intervention. If the metric count exceeds 200, it forces the use of 'tier_low'
        settings which are optimized for larger datasets for only the narrow rollups,
        as well as more conservative refresh policies. If the metric count is below or equal to 200,
        it allows the use of higher performance tiers (e.g., 'tier_high' or 'tier_medium')
        which may have more aggressive settings suitable for smaller datasets. This dynamic adjustment
        helps ensure that the system remains performant and stable as the volume of metrics grows over time.
        """
        metric_count: int = getattr(self, 'current_metric_count', 0)

        if not self.wide_table_flag or metric_count > 200:
            return self.performance_tiers["tier_low"]  # Force narrow table settings

        # Check tiers from highest to lowest
        for tier_name in ["tier_high", "tier_medium", "tier_low"]:
            tier: dict[str, Any] = self.performance_tiers[tier_name]
            if metric_count <= tier["count"]:
                return tier

        return self.performance_tiers["tier_low"] # Fallback default

    def _view_exists(self, session: Session, view_name: str) -> bool:
        """Check to see if a continuous aggregate exists in the catalog.
        Parameters:
            session: Active database session for executing SQL commands.
            view_name: The name of the continuous aggregate view to check for existence.
        Returns:
            bool: True if the view exists, False otherwise.
        """
        check_sql: TextClause = text("SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = :name")
        return session.execute(check_sql, {"name": view_name}).fetchone() is not None


    def _drop_all_continuous_aggregates(self, session: Session) -> None:
        """
        Teardown all rollups in correct dependency order (Top-Down) to
        fix the 'DependentObjectsStillExist' error.
        Process:
        1. Fetch all existing continuous aggregates from the TSDB catalog.
        2. Determine the drop order based on naming conventions (Weekly -> Daily -> Hourly).
        3. For each view: a) Set a short lock timeout to fail fast if blocked by flush thread,
                          b) Disable compression to avoid issues with compressed CAGGs,
                          c) Remove policies to prevent orphaned jobs,
                          d) Acquire an exclusive lock on the view to ensure no concurrent access,
                          e) Drop the view with CASCADE to clean up dependencies,
                          f) Commit after each drop to release locks and allow the next drop to proceed.
        """
        r_settings = self._get_dynamic_settings()
        try:
            # 1. Fetch current aggregates
            result = session.execute(text("""
                SELECT view_schema, view_name
                FROM timescaledb_information.continuous_aggregates;
            """))
            views = result.fetchall()

            if not views:
                self._log.info("No continuous aggregates found to drop.")
                return

            # 2. Define Priority: Child views (Weekly) MUST be dropped before Parent views (Daily)
            # This prevents internal _partial_view dependencies from blocking the drop.
            priority_map: dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "hourly": 1}

            def get_drop_rank(v_tuple) -> int:
                name_lower: str = v_tuple[1].lower()
                for key, val in priority_map.items():
                    if key in name_lower:
                        return val
                return 0

            # Sort descending: 4 (Weekly) drops first, 1 (Hourly) drops last.
            sorted_views: List[Row[Any]] = sorted(views, key=get_drop_rank, reverse=True)

            # 3. Iterate and drop each view safely
            for schema, name in sorted_views:
                full_name: str = f'"{schema}"."{name}"'
                self._log.info(f"Purging rollup: {full_name}")

                # 3b. Fail-fast if locked by background flush or refresh jobs
                session.execute(text(f"SET LOCAL lock_timeout = '{r_settings['lock_timeout']}';"))

                # 4. Disable Compression (Mandatory for a clean drop of compressed CAGGs)
                try:
                    session.execute(text(f"ALTER MATERIALIZED VIEW {full_name} SET (timescaledb.compress = false);"))
                except Exception:
                    self._log.info(f"View was already uncompressed: {full_name}")
                    pass # Already uncompressed or doesn't support it

                # 5. Remove policies first
                session.execute(text(f"SELECT remove_continuous_aggregate_policy('{full_name}', if_exists => true);"))
                session.execute(text(f"SELECT remove_retention_policy('{full_name}', if_exists => true);"))

                # 6. Acquire Exclusive Lock & Drop
                session.execute(text(f"LOCK TABLE {full_name} IN ACCESS EXCLUSIVE MODE;"))
                session.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {full_name} CASCADE;"))

                # 7. Critical: Commit after each view
                # This releases internal metadata locks and allows the next DROP in the stack to succeed.
                session.commit()
                self._log.info(f"Successfully purged {full_name}")

            self._log.info("Full rollup stack teardown complete.")

        except Exception as e:
            session.rollback()
            self._log.error(f"Failed to purge rollup stack: {e}")
            raise

    # 10e

    def refresh_rollups(self, force_full: bool = False) -> None:
        """
        Refreshes rollups in Bottom-Up order (Hourly -> Daily -> Weekly).
        This ensures parent views have data available from their child sources.
            Params: force_full (bool): If True, refreshes the entire range of data.
            This is useful for scenarios where incremental refreshes may not be sufficient,
            such as after a bucket interval change or if there are concerns about data consistency.
            When set to False, the method performs an incremental refresh based on the predefined
            start offsets for each rollup, which is more efficient for regular maintenance.
            The method includes robust error handling and logging to track the progress and
            duration of each refresh operation, as well as a watchdog mechanism to monitor
            long-running refreshes and send alerts if they become blocked by locks.
        """
        with self.SessionFactory() as session:
            with self._reconnect_lock:
                tsdb_connected: bool = self.tsdb_connected

            if not tsdb_connected or not session:
                self._log.error("Cannot refresh rollups — not tsdb_connected.")
                return

            try:
                # 1. Define the refresh sequence: Smallest Grain -> Largest Grain
                # Monthly is independent (from hypertable), so it can go anywhere.
                refresh_sequence: List[Tuple[str]] = [
                    ("hourly_rollup_narrow", self.hourly_rollup_start),
                    ("hourly_rollup_wide", self.hourly_rollup_start),
                    ("daily_rollup_narrow", self.daily_rollup_start),
                    ("daily_rollup_wide", self.daily_rollup_start),
                    ("weekly_rollup_narrow", self.weekly_rollup_start),
                    ("weekly_rollup_wide", self.weekly_rollup_start),
                    ("monthly_rollup_narrow", self.monthly_rollup_start),
                    ("monthly_rollup_wide", self.monthly_rollup_start),
                ]

                for view_name, start_offset in refresh_sequence:
                    # 2. Check if the view exists before refreshing
                    # (Safety check in case of a partial migration)
                    if self._view_exists(session, view_name):
                        self._refresh_single_rollup(session, view_name, start_offset, force_full)
                    else:
                        self._log.warning(f"Skipping refresh: {view_name} does not exist.")

            except Exception as e:
                self._log.error(f"Rollup refresh failed: {e}")

    def _refresh_single_rollup(self, session: Session, view_name: str, start_offset: str, force_full: bool = False) -> None:
        """
        Refreshes a rollup with duration tracking and performance logging.
        The watchdog monitors the refresh process and sends alerts if it detects that the refresh is blocked
        by locks for an extended period.
        parameters:
            session: Active database session for executing SQL commands.
            view_name: The name of the continuous aggregate view to refresh.
            start_offset: The offset for incremental refresh (e.g., "3 hours", "3 days").
            force_full: If True, performs a full refresh from the beginning of time to now. If
            False, performs an incremental refresh based on the start_offset.
        """
        r_settings: dict = self._get_dynamic_settings()

        stop_signal: List[bool] = self._start_refresh_watchdog(view_name)

        start_time: float = time.perf_counter()
        mode: Literal['FULL'] | Literal['INCREMENTAL'] = "FULL" if force_full else "INCREMENTAL"

        self._log.info(f"Starting {mode} refresh for {view_name}...")

        # AUTOCOMMIT is mandatory for CALL refresh_continuous_aggregate
        with session.bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"SET LOCAL work_mem = '{r_settings['work_mem']}';"))
            try:
                if force_full:
                    # Refresh from the beginning of time to now
                    conn.execute(text(f"CALL refresh_continuous_aggregate('{view_name}', NULL, now());"))
                else:
                    # Incremental refresh
                    conn.execute(text(f"""
                        CALL refresh_continuous_aggregate(
                            '{view_name}',
                            now() - INTERVAL '{start_offset}',
                            now()
                        );
                    """))

                end_time: float = time.perf_counter()
                duration_seconds: float = end_time - start_time

                # Log the duration in a scannable format
                self._log.info(
                    f"{mode} refresh COMPLETED for {view_name}. "
                    f"Duration: {duration_seconds:.2f}s "
                    f"({int(duration_seconds // 60)}m {int(duration_seconds % 60)}s)"
                )


            except Exception as e:
                self._log.error(f"{mode} refresh FAILED for {view_name} after {time.perf_counter() - start_time:.2f}s: {e}")
                raise # Re-raise to let the parent caller decide if it should continue

            finally:
                # Ensure the watchdog thread stops
                stop_signal[0] = True

    # watchdog refresh management
    def _stop_existing_watchdog(self) -> None:
        """Signals the existing watchdog to exit immediately.
        The watchdog thread checks the signal at short intervals and will terminate if the signal is set to True.
        """
        if hasattr(self, '_current_watchdog_signal') and self._current_watchdog_signal:
            self._current_watchdog_signal[0] = True
            self._current_watchdog_signal = None

    # watchdog thread to monitor long-running refreshes
    def _start_refresh_watchdog(self, view_name: str) -> List[bool]:
        """
        This watchdog runs in a separate thread to monitor the progress of a continuous aggregate refresh.
        It periodically checks the pg_stat_activity for the refresh query and sends a Pushover alert
        if it detects that the refresh is blocked by locks for an extended period (e.g., 30 seconds).
        Parameters:
            view_name (str): The name of the continuous aggregate view being refreshed, used for monitoring and alerting purposes.

        Returns:
            List[bool]: A mutable list containing a single boolean value that serves as a stop signal for the watchdog thread.
        """
        # 1. Kill any existing watchdog before starting a new one
        self._stop_existing_watchdog()

        # 2. Create the new stop signal
        stop_signal: List[bool] = [False]
        self._current_watchdog_signal: List[bool] = stop_signal

        def monitor() -> None:
            # Use a short-lived session specifically for monitoring
            with self.SessionFactory() as session:
                # Check both the signal and the DB state
                while not stop_signal[0]:
                    try:
                        # We check if the refresh is still running
                        sql: TextClause = text("""
                            SELECT wait_event_type FROM pg_stat_activity
                            WHERE query LIKE :pattern
                            AND state != 'idle'
                            AND pid != pg_backend_pid()
                        """)
                        res: Row[Any] | None = session.execute(sql, {"pattern": f"%refresh_continuous_aggregate%'{view_name}'%"}).fetchone()

                        # If the query is gone from pg_stat_activity, the refresh is done
                        if not res:
                            break

                        if res.wait_event_type == 'Lock':
                            self._send_pushover_message(f"⚠️ {view_name} is BLOCKED by a lock.")

                        # Sleep in small increments to remain responsive to the stop_signal
                        for _ in range(30):
                            if stop_signal[0]:
                                return
                            time.sleep(1)

                    except Exception:
                        break

        t = threading.Thread(target=monitor, name=f"Watchdog_{view_name}", daemon=True)
        t.start()
        return stop_signal

    def _refresh_rollup_loop(self) -> None:
        """
        Background loop: Replays backlog, then refreshes hierarchical aggregates.
        Uses @property tsdb_connected for live state awareness.
        The loop includes multiple safeguards:
            1. Live Connection Check: Before each refresh cycle, it checks if the database connection is healthy.
                If not, it skips the refresh and waits before retrying.
            2. Migration Gatekeeper: If a schema migration is currently in progress, it pauses the refresh loop to avoid conflicts.
            3. Backlog Drain: Before refreshing, it ensures that any backlogged data is replayed to the hypertable,
                so that the continuous aggregates include the most recent data.
            4. Sequential Hierarchical Refresh: It refreshes the continuous aggregates in the correct
                order (Hourly -> Daily -> Weekly -> Monthly) to ensure that parent views have their child data available.
            5. Dynamic Sleep: The loop uses the auto_refresh_interval from the policy to determine how long to wait
                between refresh cycles, allowing for dynamic adjustment of refresh frequency based on performance needs.

        """
        self._log.info("Background rollup refresh loop started.")

        while not self._stop_refresh_rollup_event.is_set():
            try:
                # 1. Live Connection Check
                # Uses the @property to peek into the shared rollup_policy dict
                with self._reconnect_lock:
                    tsdb_connected: bool = self.tsdb_connected

                if not tsdb_connected:
                    self._log.debug("Refresh skipped: Database not connected.")
                    # Wait 60s or until thread stop event is set
                    self._stop_refresh_rollup_event.wait(timeout=60)
                    continue

                # 2. Migration Gatekeeper
                # Pause if a schema migration (rebuild) is currently running
                if self.migration_in_progress.is_set():
                    self._log.debug("Refresh skipped: Migration in progress.")
                    self._stop_refresh_rollup_event.wait(timeout=30)
                    continue

                # 3. Drain Backlog Before Refresh
                # This ensures late/backlogged data is in the hypertable
                # so the CAGG refresh includes it.
                with self._backlog_lock:
                    count: int = self.backlog.replay_to_queue()
                    if count > 1:
                        self._log.debug(f"Replayed {count} points. Waiting for flush...")
                        # Wait for flush worker to finish writing replayed points
                        self._flush_queue.join()

                # 4. Sequential Hierarchical Refresh
                # Order: Hourly -> Daily -> Weekly -> Monthly
                with self.engine.connect() as conn:
                    conn: Connection = conn.execution_options(isolation_level="AUTOCOMMIT")
                    # Apply dynamic session settings from performance tiers
                    tier: dict = self._get_dynamic_settings()
                    conn.execute(text(f"SET work_mem = '{tier['work_mem']}';"))
                    conn.execute(text(f"SET lock_timeout = '{tier['lock_timeout']}';"))

                    granularities: List[str] = ["hourly", "daily", "weekly", "monthly"]
                    prefix: Literal['rollup_wide'] | Literal['rollup_narrow'] = "rollup_wide" if self.wide_table_flag else "rollup_narrow"

                    for gran in granularities:
                        view_name = f"{gran}_{prefix}"

                        self._log.debug(f"Refreshing continuous aggregate: {view_name}")

                        conn.execute(
                            text("CALL refresh_continuous_aggregate(:view, NULL, NULL);"),
                            {"view": view_name}
                        )

                # 5. Dynamic Sleep
                # Use the interval from policy (convert seconds to float for wait)
                wait_time = float(self.auto_refresh_interval)
                if self._stop_refresh_rollup_event.wait(timeout=wait_time):
                    break

            except Exception as e:
                self._log.error(f"Rollup refresh cycle failed: {e}")
                # Exponential backoff/safety sleep on error
                self._stop_refresh_rollup_event.wait(timeout=300)

        self._log.info("Background rollup refresh loop exiting.")

    # 10i
    def stop_auto_refresh(self) -> None:
        """
        Cleanly stop the auto rollup refresh thread.
        This method signals the background thread to exit and waits for it to finish,
        ensuring that no refresh operations are left in an inconsistent state.
        It also logs the shutdown of the refresh thread for monitoring purposes.
        """
        if hasattr(self, "_stop_refresh_rollup_event"):
            self._stop_refresh_rollup_event.set()
            self._log.info("Auto rollup refresh thread stopped.")

    def _purge_ghost_jobs(self, session: Session) -> None:
        """
        Cleans up aberrant TimescaleDB processes using dynamic type-based thresholds.
        This method identifies and purges three types of aberrant jobs:
            1. Orphaned Metadata Jobs: Jobs that reference non-existent continuous aggregates,
                often due to failed refreshes or manual drops without policy cleanup.
            2. Stale Execution Jobs: Jobs that have been in a 'Started' state for longer than
                their expected duration based on their type (e.g., Refresh, Compression, Retention).
            3. Ghost Processes: Jobs that are marked as 'Started' but have no corresponding active backend process,
                indicating they are stuck in a limbo state.

            The method uses a single SQL query with CASE statements to classify jobs into these categories based on
            their application_name, last run status, and timestamps. It then iterates through the identified aberrant jobs,
            logs the issues, and takes appropriate actions such as terminating stale backend processes and deleting
            the jobs to clean up the TimescaleDB environment. Error handling ensures that any issues during the
            purge process are logged and do not disrupt the overall system stability.
        """
        self._log.info("Initiating dynamic ghost & stale job sweep...")

        # Define thresholds based on TimescaleDB common job patterns
        # Refresh: Short (30m), Compression/Retention: Long (6h), Others: Standard (1h)
        detect_sql = text("""
            WITH job_thresholds AS (
                SELECT
                    j.job_id,
                    j.application_name,
                    js.last_run_started_at,
                    js.last_run_status,
                    CASE
                        WHEN j.application_name LIKE 'Refresh Continuous Aggregate%' THEN INTERVAL '30 minutes'
                        WHEN j.application_name LIKE 'Compression Policy%' THEN INTERVAL '6 hours'
                        WHEN j.application_name LIKE 'Retention Policy%' THEN INTERVAL '2 hours'
                        ELSE INTERVAL '1 hour'
                    END as allowed_duration
                FROM timescaledb_information.jobs j
                LEFT JOIN timescaledb_information.job_stats js ON j.job_id = js.job_id
            )
            SELECT
                t.job_id,
                t.application_name,
                CASE
                    -- 1. Orphaned
                    WHEN (t.application_name LIKE 'Refresh%' OR t.application_name LIKE 'Retention%')
                        AND NOT EXISTS (
                            SELECT 1 FROM timescaledb_information.continuous_aggregates c
                            JOIN timescaledb_information.jobs j2 ON t.job_id = j2.job_id
                            WHERE j2.hypertable_name = c.view_name
                        ) THEN 'ORPHANED_METADATA'

                    -- 2. Stale
                    WHEN t.last_run_status = 'Started' AND t.last_run_started_at < now() - t.allowed_duration
                        THEN 'STALE_EXECUTION'

                    -- 3. Ghost (PID check via application_name)
                    WHEN t.last_run_status = 'Started'
                        AND NOT EXISTS (
                            SELECT 1 FROM pg_stat_activity p
                            WHERE p.application_name LIKE '%' || t.job_id || '%'
                        ) THEN 'GHOST_PROCESS'

                    ELSE NULL
                END as aberrancy_type
            FROM job_thresholds t
            WHERE 1=1; -- Add filter for NOT NULL if needed
        """)

        try:
            ghosts = session.execute(detect_sql).fetchall()

            if not ghosts:
                self._log.info("All background workers healthy.")
                return

            for job_id, app_name,  issue in ghosts:
                self._log.warning(f"Purging {issue}: {app_name} (Job {job_id})")

                # Terminate hanging backend if it still technically exists (Stale case)
                if issue == 'STALE_EXECUTION':
                    session.execute(text("SELECT pg_terminate_backend(:pid);"))

                # delete_job() terminates any remaining worker and removes the schedule
                session.execute(text("SELECT delete_job(:job_id);"), {"job_id": job_id})

            session.commit()
        except Exception as e:
            session.rollback()
            self._log.error(f"Sweep failed: {e}")


    # 10  may eventually need this method to parse interval strings from settings
    def _parse_interval_days(self, interval_str: str) -> float:
        """
        Convert interval strings like '7 days' or '6 hours' into fractional days.
        No used at the moment, but this could be useful if we want to allow flexible interval inputs in the future.
        """
        try:
            parts: List[str] = interval_str.lower().split()
            if len(parts) != 2:
                return 7  # default fallback
            value = float(parts[0])
            unit: str = parts[1]
            if "hour" in unit:
                return value / 24.0
            elif "day" in unit:
                return value * 1
            elif "week" in unit:
                return value * 7
            elif "month" in unit:
                return value * 30
            else:
                return value
        except Exception:
            return 7.0

