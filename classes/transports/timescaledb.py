"""
timescaledb_out transport bridge module (with rollup continuous aggregates) and persistent disk backlog.

Features:
 - Auto-create database (default "solar", configurable)
 - device_info (multi unique devices) (for future multi-device/transport support)
 - device_metrics_wide hypertable
 - device_metrics_narrow hypertable
 - Hypertable compression & retention (idempotent)
 - Continuous aggregates rollups: hourly_rollup, daily_rollup, weekly_rollup, monthly_rollup (configurable)
 - Async flushing + persistent disk backlog
 - OS-local timestamps

Terminology:
Continuous Aggregates	The official TimescaleDB feature name. It's an automatically and incrementally updated
    materialized SQL view that pre-computes aggregate data (e.g., averages, sums over a minute, hour, day, week or month)
    from raw data and stores it in a separate hypertable.

Continuous Rollups	    This term refers to the process of downsampling data into successively
    coarser time granularities (e.g., from raw data to hourly summaries, then to daily summaries, then to weekly summaries,
    then to monthly summaries). This is achieved using the hierarchical continuous aggregates feature,
    where a continuous aggregate based on the output of a previous one is created.

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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    create_engine,
    engine,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import Insert

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

from classes.protocol_settings import registry_map_entry

from ..protocol_settings import (
    Registry_Type,
    protocol_settings,  # Registry_Type and data helpers
)
from .transport_base import transport_base

SessionGlobal: Callable[..., Session] = sessionmaker(
    autocommit=False,
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
    device_identifier: Mapped[str] = mapped_column(Text, index=True)
    device_serial_number: Mapped[Optional[str]] = mapped_column(Text)
    device_name: Mapped[str] = mapped_column(Text)
    device_manufacturer: Mapped[Optional[str]] = mapped_column(Text)
    device_model: Mapped[Optional[str]] = mapped_column(Text)
    device_firmware: Mapped[Optional[str]] = mapped_column(Text)
    device_location: Mapped[Optional[str]] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text)

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
    data_type: Mapped[str] = mapped_column(Text, default='double precision')
    # func.now() is a SQL function, keep it as it is
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),  default=lambda: datetime.now().astimezone(), onupdate=lambda: datetime.now().astimezone())


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
    device_manufacturer: str = "Inverter Manufacturer"
    device_model: str = "Inverter Model"
    device_serial_number: str = "0001"
    device_name: str = "Buxton TimeScaleDB PPG Bridge"
    force_float: bool = True

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


    """
    Rollup Type
        refresh rollup    start_offset   compress_after   reason
        1 Hour	          3 hours	     1 day	          Allows a 3-hour window for late data before locking it via compression.
        1 Day	          3 days	     7 days	          Ensures daily rollups are finalized before compressing.
        1 Week	          2 weeks	     1 month	      Larger window helps capture any delayed source data updates.
        1 Month	          2 months	     3 months	      Maximum safety for long-term historical accuracy.
    """

    # hypertable defaults
    hypertable_defaults: Dict[str, Any] = {
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
        "drop_after": "1 year",
        "migrate_data": True,
        "enable_compression": True,  # enable compression on hypertables at startup
    }

    # rollup defaults, continuous aggregate bucket sizes
    rollup_defaults: Dict[str, Any] = {
        "hourly_rollup_bucket": "1 hour",
        "hourly_rollup_start": "3 hours",
        "daily_rollup_bucket": "1 day",
        "daily_rollup_start": "3 days",
        "weekly_rollup_bucket": "1 week",
        "weekly_rollup_start": "3 weeks",
        "monthly_rollup_bucket": "1 month",
        "monthly_rollup_start": "2 months",
        "anchor_start_time_utc": "2000-01-01 00:00:00+00",  # default anchor time for rollup alignment
        "enable_rollups": True,
        "auto_refresh_interval": 21600,  # seconds (default 6 hours), auto-refresh rollup
        "enable_auto_refresh": True,  # whether to auto-refresh rollups periodically
    }

    # Pushover settings
    enable_pushover: bool = True
    pushover_token: str = None
    pushover_user: str = None

    # stale data settings and fields
    stale_data_timeout = 300       # seconds before considering data stale for incomplete batch cleanup
    stale_data_last_row: Optional[Dict[str, Any]] = None  # last row of metrics for stale data detection
    stale_data_start_ts: Optional[datetime] = None # timestamp when stale data period started
    is_stale_data: bool = False  # flag indicating if stale data condition is active
    stale_event_count = 0
    last_stale_event_ts = None
    max_stale_attempts = 3
    retry_delay_mins = 5
    schema_needs_refresh = True  # flag to indicate if ORM schema refresh is needed after reconnect or column changes
    current_metric_count = 0

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
            - device_name (str): Name for the bridge (default: "Buxton TimeScaleDB Bridge")
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
            - rollup_defaults: Dict for rollup settings
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

        if not self.username:
            raise ValueError("TimeScaleDB User is not set")

        if not self.password:
            warnings.warn("TimeScaleDB Password is empty", RuntimeWarning)

        # device info of the transport device and software bridge
        self.device_manufacturer: str = settings.get("manufacturer", fallback=self.device_manufacturer)
        self.device_model: str = settings.get("model", fallback=self.device_model)
        self.device_serial_number: str = settings.get("serial_number", fallback=self.device_serial_number)
        self.device_identifier = f"{self.normalize(self.device_model)}_{self.normalize(self.device_serial_number)}"
        # self.device_name: str = settings.get("device", fallback="TimescaleDB_Bridge")

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
        self.backlog_storage_path = Path(settings.get("backlog_storage_path", fallback=self.backlog_storage_path))
        self.backlog_file_name = settings.get("backlog_file_name", fallback=self.backlog_file_name)
        self.max_backlog_size = settings.getint("max_backlog_size", fallback=self.max_backlog_size)
        self.max_backlog_age: int = settings.getint("max_backlog_age", fallback=self.max_backlog_age)

        # hypertable / rollup options

        self.rollup_policy: dict[str,Any] = {
            # Hypertable Settings
            "current_metric_count": self.current_metric_count,
            "tsdb_connected": self.tsdb_connected,
            "compress_segmentby_narrow": self.hypertable_defaults["compress_segmentby_narrow"],
            "compress_segmentby_wide": self.hypertable_defaults["compress_segmentby_wide"],
            "time_column": self.hypertable_defaults["time_column"],
            "compress_orderby": self.hypertable_defaults["compress_orderby"],
            "anchor_start_time_utc": self.rollup_defaults["anchor_start_time_utc"],
            "hourly_chunk_time_interval": settings.get("hourly_chunk_time_interval", fallback=self.hypertable_defaults["hourly_chunk_time_interval"]),
            "daily_chunk_time_interval": settings.get("daily_chunk_time_interval", fallback=self.hypertable_defaults["daily_chunk_time_interval"]),
            "weekly_chunk_time_interval": settings.get("weekly_chunk_time_interval", fallback=self.hypertable_defaults["weekly_chunk_time_interval"]),
            "monthly_chunk_time_interval": settings.get("monthly_chunk_time_interval", fallback=self.hypertable_defaults["monthly_chunk_time_interval"]),
            "hourly_compress_after_interval": settings.get("hourly_compress_after_interval", fallback=self.hypertable_defaults["hourly_compress_after_interval"]),
            "daily_compress_after_interval": settings.get("daily_compress_after_interval", fallback=self.hypertable_defaults["daily_compress_after_interval"]),
            "weekly_compress_after_interval": settings.get("weekly_compress_after_interval", fallback=self.hypertable_defaults["weekly_compress_after_interval"]),
            "monthly_compress_after_interval": settings.get("monthly_compress_after_interval", fallback=self.hypertable_defaults["monthly_compress_after_interval"]),
            "drop_after": settings.get("drop_after", fallback=self.hypertable_defaults["drop_after"]),
            "migrate_data": settings.getboolean("migrate_data", fallback=str(self.hypertable_defaults["migrate_data"])),
            "enable_compression": settings.getboolean("enable_compression", fallback=str(self.hypertable_defaults["enable_compression"])),

            # Rollup Settings
            "hourly_rollup_bucket": settings.get("hourly_rollup_bucket", fallback=self.rollup_defaults["hourly_rollup_bucket"]),
            "daily_rollup_bucket": settings.get("daily_rollup_bucket", fallback=self.rollup_defaults["daily_rollup_bucket"]),
            "weekly_rollup_bucket": settings.get("weekly_rollup_bucket", fallback=self.rollup_defaults["weekly_rollup_bucket"]),
            "monthly_rollup_bucket": settings.get("monthly_rollup_bucket", fallback=self.rollup_defaults["monthly_rollup_bucket"]),
            "hourly_rollup_start": settings.get("hourly_rollup_start", fallback=self.rollup_defaults["hourly_rollup_start"]),
            "daily_rollup_start": settings.get("daily_rollup_start", fallback=self.rollup_defaults["daily_rollup_start"]),
            "weekly_rollup_start": settings.get("weekly_rollup_start", fallback=self.rollup_defaults["weekly_rollup_start"]),
            "monthly_rollup_start": settings.get("monthly_rollup_start", fallback=self.rollup_defaults["monthly_rollup_start"]),
            "auto_refresh_interval": settings.getint("auto_refresh_interval", fallback=self.rollup_defaults["auto_refresh_interval"]),
            "enable_auto_refresh": settings.getboolean("enable_auto_refresh", fallback=str(self.rollup_defaults["enable_auto_refresh"])),
            "enable_rollups": settings.getboolean("enable_rollups", fallback=str(self.rollup_defaults["enable_rollups"])),
        }

        # pushover settings
        self.enable_pushover: bool = settings.getboolean("enable_pushover", fallback=self.enable_pushover)
        self.pushover_token: str = settings.get("pushover_token", fallback=self.pushover_token)
        self.pushover_user: str = settings.get("pushover_user", fallback=self.pushover_user)

        super().__init__(settings)

        # end user settings
        #*********************************

        self.write_enabled = True  # TimescaleDB output is always write-enabled but this setting is unclear from base class
                                    # keep it enabled to allow access to transport_base.write_data method???
        # self.transport_connected: bool = transport_base.connected   # inverter connection state from base class

        # load all registry metrics' map.
        self.registry_metrics: Dict[Registry_Type, List[registry_map_entry]]  = protocol_settings.registry_map

        self._wide_columns: set[str] = set()  # cached set of existing wide table columns for fast lookup
        self.metric_mapping: Dict[str, str] = {}  # metric_name and clean_column_name mapping dict for raw to safe metric name conversions
        self.device_info_id = None  # will be set during init with _ensure_device_info insert, and after data scrape, per transport batch (future feature).
        self.wide_table_flag = True  # assume wide table unless too many metrics detected
        self.metric_lookup: Dict[str, registry_map_entry] = {
            entry.variable_name: entry
            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING)
            for entry in self.registry_metrics.get(registry_type, [])
            if hasattr(entry, 'variable_name')
        }

        # SQLAlchemy init runtime
        self.engine = None  # engine connection
        self.SessionFactory = None

        # -------------------------
        # threading
        # -------------------------

        # Initialize async flush queue and worker thread.  Start it here so it's ready at full init.
        self._flush_queue: queue = queue.Queue(maxsize=0)
        self._flush_thread = threading.Thread(target=self._flush_worker, daemon=True)
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
        self._backlog_lock: lock = threading.RLock()  # lock for backlog operations

        # Lock for protecting schema mutations and metadata reflection
        # Use RLock to allow nested calls within the same thread
        # Protects the SQLAlchemy Metadata and Table Identifiers (the "structure" of the Wide Table).
        self._schema_lock: RLock = threading.RLock()

        # persistent backlog file and path, both the file path and the in-memory backlog file are initialized here
        self.backlog_file_path: Path = self.backlog_storage_path / f"{self.backlog_file_name}.jsonl"
        # full path to backlog file

        self.backlog = BacklogManager(
            backlog_file_path=self.backlog_file_path,
            max_backlog_age=self.max_backlog_age,
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

        # 4 Write device information metadata for single device/transport on startup if from_transport provided.
          # multi-device support is via multiple transport instances with unique device identifiers where device_info_id is captured during batch writes.
            try:
                self.device_info_id: int = self._ensure_device_info()
            except Exception as e:
                self._log.error(f"Device Information Data write failed: {e}")

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
            threading.Thread(target=self._attempt_reconnect, daemon=True).start()
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
            SessionGlobal.configure(bind=self.engine, expire_on_commit=False)
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
    def _ensure_device_info(self) -> int:
        """
        Pull basic device metadata from device attributes and write to device_info table.
        Ensure that device information is present in the TimescaleDB database. If not, insert it.

        Args:
            from_transport (transport_base): The transport object containing device metadata.
        """
        # if we've lost the session, and can't check against the timescaledb table, then return with 0 as default

        with self.SessionFactory() as session:

            with self._reconnect_lock:
                tsdb_connected: bool = self.tsdb_connected
            if not tsdb_connected or not session:
                deviceID = 0
                self._log.debug("device_info unknown, skipping insert, returning error ID 0")

                return deviceID
            else:
                self._log.debug("Ensuring device_info record exists")
            try:
                # pull device info from transport user settings configuration
                existing: DeviceInfo = (
                    session.execute(
                    select(DeviceInfo).where(
                        (DeviceInfo.device_identifier == self.device_identifier) &
                        (DeviceInfo.device_name == self.device_name) &
                        (DeviceInfo.device_manufacturer == self.device_manufacturer) &
                        (DeviceInfo.device_model == self.device_model) &
                        (DeviceInfo.device_serial_number == self.device_serial_number) &
                        (DeviceInfo.transport == self.transport_name)
                    )
                ).scalar_one_or_none()
                )
            except SQLAlchemyError as e:
                self._log.error(f"_ensure_device_info error: {e}")
                try:
                    session.rollback()
                except SQLAlchemyError:
                    self._log.debug("Ensuring device_info rollback failed")
                raise

            if existing:
                self._log.debug("Exact device_info exists — skipping insert")
                return existing.device_info_id
            else:
                self._log.debug("device_info not found — inserting new record")
            try:
                dev = DeviceInfo(
                    device_identifier=self.device_identifier,
                    device_name=self.device_name,
                    device_manufacturer=self.device_manufacturer,
                    device_model=self.device_model,
                    device_serial_number=self.device_serial_number,
                    transport=self.transport_name,
                    created_at=datetime.now().astimezone(),
                )

                session.add(dev)
                session.commit()
                self._log.info(f"Inserted DeviceInfo for {self.device_identifier}")

                if dev.device_info_id is None:
                    raise ValueError("Failed to retrieve device_info_id after insert.")  # noqa: TRY301
                else:
                    return dev.device_info_id

            except SQLAlchemyError as e:
                self._log.error(f"device_info insert error: {e}")
                try:
                    session.rollback()
                except SQLAlchemyError:
                    self._log.debug("inserting new record failed, rollback failed")
                raise

    def _determine_wide_table(self) -> None:
        """
        Determine whether to create wide table based on metric_catalog entries.
        """
        try:
            #5a get metric names from registry_map
            metric_start_names: list = self._registry_metric_names()
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
        """ load registry map for validation of metric names for dynamic column creation. Return sorted list of metric names.
        """

        ## returns all variable_name in registry_metrics as opposed to below selected Registry_Type.
        # return sorted([
        #     entry.variable_name
        #     for entries_list in self.registry_metrics.values()
        #     for entry in entries_list
        #     if hasattr(entry, 'variable_name')
        # ])

        return sorted([
            entry.variable_name
            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING)
            for entry in self.registry_metrics[registry_type]
            if hasattr(entry, 'variable_name')
        ])

    # -------------------------
    #  5b. Dynamically create wide table columns for metrics
    # -------------------------

    def _ensure_columns_for_metrics(self, metric_start_names: List[str]) -> bool:
        """
        Ensure each metric name as defined in the variable_mask/variable_screen filters has a corresponding column in device_metrics, and an entry
        in the metric_catalog table.
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
                self._log.info("No metric column names were detected")
                return False

            try:
                with self._schema_lock:
                    with session.begin():
                        # advisory lock to serialize schema changes
                        self._schema_advisory_lock(session)

                        for m in metric_start_names:
                            # 1. Check for existing mapping
                            row_value: Any | None = session.execute(
                                text("SELECT clean_column_name FROM metric_catalog WHERE metric_name = :m"),
                                {"m": m}
                            ).scalar()

                            if row_value:
                                self.metric_mapping[m] = row_value
                                continue

                            # 2. Clean name and ensure column exists in WIDE table
                            col: str = self._clean_column_name(m)

                            # check if column name (cleaned metric name) exists in postgres information_schema
                            exists_wide: Any | None = session.execute(text("""
                                SELECT 1 FROM information_schema.columns
                                WHERE table_name = 'device_metrics_wide' AND column_name = :col
                            """), {"col": col}).scalar()

                            # add column if missing.  Initial column creation should be alphabetic due to sorted metric names.
                            # per postgres docs, subsequent columns added after first init are appended to the end of the table.
                            if not exists_wide:
                                session.execute(text(
                                    f"ALTER TABLE device_metrics_wide ADD COLUMN IF NOT EXISTS {col} double precision;"
                                ))
                            params: dict = {
                                'm': m,
                                'col': col,
                                'dtype': 'double precision',
                                'col_date': datetime.now().astimezone()
                            }

                            session.execute(text("""
                                INSERT INTO metric_catalog (metric_name, clean_column_name, data_type, created_at)
                                VALUES (:m, :col, :dtype, :col_date)
                                ON CONFLICT (metric_name) DO UPDATE SET clean_column_name = EXCLUDED.clean_column_name
                            """), params)

                            self.metric_mapping[m] = col

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
        table_name = DeviceMetricsWide.__tablename__

        with self._schema_lock:
            self._log.info(f"Resyncing schema for {table_name}...")

            # 1. Unbind the old table from metadata
            old_table = Base.metadata.tables.get(table_name)
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
        if not isinstance(data, dict):
            self._log.warning(
                f"Received non-dict signal ({type(data).__name__}) from "
                f"[{from_transport.transport_name}]. Ignoring."
            )
            return

        # 2. Trap: Ignore empty dictionaries
        if not data:
            self._log.debug("Received empty data dictionary. Skipping.")
            return

        # 3. Proceed only if there is "real" data
        self._log.debug(f"Data: {data}")
        self._log.debug(f"writing data from [{from_transport.transport_name}] to timescaledb_out bridge")

        # Safe to copy now that we know it's a dict
        self._flush_queue.put(data.copy())

    def _prepare_final_data(self,datacopy: dict) -> dict:
        try:
            new_data: dict = {self.metric_mapping.get(k, k): v for k, v in datacopy.items()}
            for key, raw_value in new_data.items():

                # get registry metadata for data type conversions
                r_metadata: registry_map_entry | None  = self.metric_lookup.get(key)

                # type metric coercion to float value
                if r_metadata and hasattr(r_metadata, "data_type"):  # field in registry mapping
                    dt: str = str(r_metadata.data_type).lower()
                    if dt in ("int", "integer"):
                        raw_value = int(raw_value)
                    elif dt in ("float", "double"):
                        raw_value = float(raw_value)
                    else:
                        raw_value = raw_value
                else:
                    raw_value: float | Any = float(raw_value) if self.force_float else raw_value

                new_data[key] = raw_value
            return new_data  # noqa: TRY300

        except (TypeError, ValueError):
            self._log.warning(f"Invalid metric value encountered in: {datacopy}")


    # Flush worker thread to handle data writes to the database.
    def _flush_worker(self) -> None:
        """Async flush worker created during init.  Handles data appends to tables. Routing to backlog if needed.
            datacopy  -> wide dict of unaltered metrics passed from PPG
            new_data  -> wide dict of processed datacopy for safe sql and floating point value coercion.
            final_data  -> wide dict of appended new_data with deviceid and timestamp.  Needed because narrow table
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
                datacopy: dict | None = self._flush_queue.get(block=True)

                if datacopy is None or datacopy is True:
                    self._log.info("Shutdown sentinel received. Exiting flush worker.")
                    self._flush_queue.task_done()
                    break # Exit the loop cleanly and immediately

                # Now that we have data, wait here if a migration is running
                while self.migration_in_progress.is_set():
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.25)

                #  NOTE get most current device_info_id here if future PPG allows multi transports
                # pre-process data to coerce floating point as values

                # # Apply SQL-safe renaming. New dictionary via comprehension/force floats
                new_data = self._prepare_final_data(datacopy)
                if not new_data:
                    continue

                # Add device_info_id and timestamp to new_data
                final_data: dict = new_data | {
                    "device_info_id": self.device_info_id,
                    "m_time": datetime.now().astimezone()
                }
                is_stale, time_read, metrics = self._is_stale_data(final_data)
                if is_stale:
                    self._log.debug("Stale data detected, skipping DB write.")
                    continue

                if self._stop_event.is_set():
                    break

                while self.migration_in_progress.is_set():
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.5)
                try:
                    with self._schema_lock:
                        with session.begin():
                            self._flush_batch_narrow(final_data, session)
                            if self.wide_table_flag:
                                valid_row = self._validate_wide_row(new_data)  # validate wide row without timestamp before insert
                                if valid_row:
                                    stmt: Insert = insert(DeviceMetricsWide.__table__).values(**final_data)
                                    session.execute(stmt)

                        self._commit_stale_state(metrics=metrics, time_read=time_read, is_stale=is_stale)

                except (SQLAlchemyError, ValueError) as e:
                    session.rollback()
                    self._log.warning("metrics data write failed.")

                    # Only backlog if setting enabled and DB is down
                    with self._reconnect_lock:
                        tsdb_connected: bool = self.tsdb_connected

                    if self.enable_persistent_storage and not tsdb_connected:
                        # Check if we can get the lock
                        acquired = self._backlog_lock.acquire(blocking=False)
                        try:
                            if acquired:
                                self.backlog.enqueue(final_data)
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

    def _flush_batch_narrow(self, newData: Dict, session: Session) -> None:
        """
            Flush new_data narrow-table metric points to the database.
            Any failed writes will be added to the backlog.
        """
        try:
            back_data: Dict = newData.copy()  # use for backlog only in case of failure
            narrow_data: Dict = newData.copy()

            device_info_id: str =  narrow_data.pop('device_info_id', Optional[int])
            reading_time =  narrow_data.pop('m_time', datetime.now().astimezone())
            # Ensure we have a datetime object
            if isinstance(reading_time, str):
                # Convert string to datetime if needed
                reading_time: datetime = datetime.fromisoformat(reading_time)

            # Convert the flat dict into a list of row mappings
            narrow_mappings: list = [
                {
                    "m_time": reading_time,
                    "device_info_id": device_info_id,
                    "metric_name": key,
                    "metric_value": value
                }
                for key, value in  narrow_data.items()
            ]

            # Use the optimized 'insert' construct for bulk efficiency
            session.execute(insert(DeviceMetricsNarrow), narrow_mappings)

        except SQLAlchemyError as e:
            self._log.exception(f"Narrow flush failed: {e}")

            try:
                session.rollback()
            except SQLAlchemyError as e2:
                self._log.exception(f"Narrow flush rollback failed: {e2}")

            # Add new_data to backlog only for the narrow table failure
            try:
                # since this is a copy of final_data, we have already completely processed the data.
                self.backlog.enqueue(back_data)
            except Exception as e2:
                self._log.error(f"Failed to add narrow point to backlog: {e2}")

            # === Auto Reconnect handling ===
            self._set_tsdb_connected(False, "Connect unsuccessful")  # noqa: FBT003
            self._trigger_reconnect()


    def _is_stale_data(self, row: dict) -> tuple[bool, datetime | None, dict | None]:
        """
        Updates stale-data state tracking using fully coerced transport data.

        If the incoming row's metric dictionary is identical to the previously
        observed row, this method maintains or initializes the stale-data timer.
        If the row differs, the stale-data state is reset, indicating new data
        flow.

        Args:
            row (dict): Incoming data row with metrics and metadata.

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

        stale_limit = timedelta(seconds=self.stale_data_timeout)

        time_read: datetime | None = row.get("m_time")
        if time_read is None:
            return False, None, None

        if isinstance(time_read, str):
            try:
                time_read = datetime.fromisoformat(time_read)
            except ValueError:
                time_read = datetime.strptime(time_read, "%Y-%m-%d %H:%M:%S%z")

        # Build metrics-only view (exclude metadata)
        metrics: dict = {
            k: v for k, v in row.items()
            if k not in ("m_time", "device_info_id")
        }

        if self.stale_data_last_row is None:
            # First observation is never stale
            return False, time_read, metrics

        # 1. Determine if the data has changed
        for key, value in metrics.items():
            prev_val = self.stale_data_last_row.get(key)

            if isinstance(value, (int, float)) and isinstance(prev_val, (int, float)):
                if not math.isclose(value, prev_val, rel_tol=1e-4, abs_tol=1e-6):
                    return False, time_read, metrics
            elif value != prev_val:
                return False, time_read, metrics

        # 2. Data unchanged → check elapsed time
        elapsed: timedelta = time_read - self.stale_data_start_ts
        is_stale = elapsed > stale_limit

        return is_stale, time_read, metrics

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
            if time_since_last_attempt < timedelta(minutes=self.retry_delay_mins):
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

        except Exception as e:
            self._log.error(f"Error closing tsdb_session: {e}")

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
        backlog: 'BacklogManager',
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

        self._refresh_rollup_thread = threading.Thread(target=self. _refresh_rollup_loop, daemon=True)
        self._stop_refresh_rollup_event: threading.Event = getattr(self, "_stop_refresh_rollup_event", threading.Event())

        self.performance_tiers: dict[str, dict[str, Any]] = {
        "tier_low":    {"count": 50,  "work_mem": "32MB",  "lock_timeout": "10s", "flush_batch_size": 10},
        "tier_medium": {"count": 100, "work_mem": "64MB",  "lock_timeout": "15s", "flush_batch_size": 20},
        "tier_high":   {"count": 200, "work_mem": "128MB", "lock_timeout": "30s", "flush_batch_size": 40},
        }

        self.if_not_exists = True

        # Rollup Settings extracted from rollup_policy
        self.current_metric_count = self.rollup_policy.get("current_metric_count", 0)
        self.anchor_start_time_utc: str = self.rollup_policy.get("anchor_start_time_utc")
        self.compress_segmentby_narrow= self.rollup_policy.get("compress_segmentby_narrow")
        self.compress_segmentby_wide= self.rollup_policy.get("compress_segmentby_wide")
        self.compress_orderby= self.rollup_policy.get("compress_orderby")
        self.time_column= self.rollup_policy.get("time_column")
        self.auto_refresh_interval = self.rollup_policy.get("auto_refresh_interval")
        self.enable_auto_refresh = bool(self.rollup_policy.get("enable_auto_refresh", 0))
        self.enable_rollups = bool(self.rollup_policy.get("enable_rollups", 0))

        self.hourly_chunk_time_interval = self.rollup_policy.get("hourly_chunk_time_interval")
        self.daily_chunk_time_interval = self.rollup_policy.get("daily_chunk_time_interval")
        self.weekly_chunk_time_interval = self.rollup_policy.get("weekly_chunk_time_interval")
        self.monthly_chunk_time_interval = self.rollup_policy.get("monthly_chunk_time_interval")

        self.hourly_compress_after_interval = self.rollup_policy.get("hourly_compress_after_interval")
        self.daily_compress_after_interval = self.rollup_policy.get("daily_compress_after_interval")
        self.weekly_compress_after_interval = self.rollup_policy.get("weekly_compress_after_interval")
        self.monthly_compress_after_interval = self.rollup_policy.get("monthly_compress_after_interval")

        self.drop_after = self.rollup_policy.get("drop_after")
        self.migrate_data = bool(self.rollup_policy.get("migrate_data",0))
        self.enable_compression = bool(self.rollup_policy.get("enable_compression",0))

        self.hourly_rollup_bucket = self.rollup_policy.get("hourly_rollup_bucket")
        self.daily_rollup_bucket = self.rollup_policy.get("daily_rollup_bucket")
        self.weekly_rollup_bucket = self.rollup_policy.get("weekly_rollup_bucket")
        self.monthly_rollup_bucket = self.rollup_policy.get("monthly_rollup_bucket")

        self.hourly_rollup_start = self.rollup_policy.get("hourly_rollup_start")
        self.daily_rollup_start = self.rollup_policy.get("daily_rollup_start")
        self.weekly_rollup_start = self.rollup_policy.get("weekly_rollup_start")
        self.monthly_rollup_start = self.rollup_policy.get("monthly_rollup_start")

    @property
    def tsdb_connected(self) -> bool:
        """Always returns the live connection state from the shared policy dict."""
        return self.rollup_policy.get("tsdb_connected", False)


    def setup_schema(self) -> None:
        """
        Called once during TSDB startup.
        Safe to call repeatedly (idempotent).
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
                self._log.error(f"Rollup refresh failed: {e}")

    # 5 Start the rollup thread.  Called from TimescaleDB class upon connection to the database.
    def start_auto_refresh(self) -> None:

        if self._refresh_rollup_thread and self._refresh_rollup_thread.is_alive():
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
        """Convert base tables into TimescaleDB hypertables."""
        # 1. The tables that need to be processed
        tables: List[str] = ["device_metrics_narrow"]
        if self.wide_table_flag:
            tables.append("device_metrics_wide")

        # 2. shared parameters
        params = {
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
        """Enable TimescaleDB compression on device_metrics_narrow and device_metrics_wide tables.
        """
        with self.SessionFactory() as session:
            self._log.info("Setting up compression policy")

            if not session:
                    self._log.error("Cannot set up compression — not tsdb_connected.")
                    return
            # Enable TimescaleDB compression on device_metrics_narrow.
            try:
                sql: str = (
                    "ALTER TABLE device_metrics_narrow SET ("
                    "timescaledb.compress, "
                    f"timescaledb.compress_orderby = '{self.compress_orderby}', "
                    f"timescaledb.compress_segmentby = '{self.compress_segmentby_narrow}'"
                    ");"
                )
                session.execute(text(sql))
                session.commit()
                self._log.debug("_enable_compression_narrow executed")
            except SQLAlchemyError as e:
                self._log.error(f"_enable_compression_narrow error device_metrics_narrow: {e}")
                try:
                    session.rollback()
                except SQLAlchemyError as e2:
                    self._log.error(f"_enable_compression_narrow rollback error: {e2}")

            # Enable TimescaleDB compression on device_metrics_wide.
            if self.wide_table_flag:
                try:
                    sql = (
                        "ALTER TABLE device_metrics_wide SET ("
                        "timescaledb.compress, "
                        f"timescaledb.compress_orderby = '{self.compress_orderby}', "
                        f"timescaledb.compress_segmentby = '{self.compress_segmentby_wide}'"
                        ");"
                    )
                    session.execute(text(sql))
                    session.commit()
                    self._log.debug("_enable_compression_wide executed")
                except SQLAlchemyError as e:
                    self._log.error(f"_enable_compression_wide error device_metrics_wide: {e}")
                    try:
                        session.rollback()
                    except SQLAlchemyError as e2:
                        self._log.error(f"_enable_compression_wide rollback error: {e2}")

            with session.begin():
                for  chunk_interval in [
                    (self.hourly_chunk_time_interval),
                    (self.daily_chunk_time_interval),
                    (self.weekly_chunk_time_interval),
                    (self.monthly_chunk_time_interval),
                ]:

                    # add compression policy
                    self.ensure_compression_policy("device_metrics_narrow", chunk_interval)
                    if self.wide_table_flag:
                        self.ensure_compression_policy("device_metrics_wide", chunk_interval)

            session.commit()

    # -------------------------
    # 7b. Add compression policy
    # -------------------------
    def ensure_compression_policy(self, source, chunk_interval) -> None:
        """Automatically compress chunks older than chunk_time_interval."""
        with self.SessionFactory() as session:

            if not session:
                    self._log.error("Cannot add compression policy — not tsdb_connected.")
                    return

            try:
                sql: str = f"SELECT add_compression_policy('{source}', compress_after => INTERVAL '{chunk_interval}', if_not_exists => TRUE);"

                session.execute(text(sql))

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
                        view_name = context["segments"][view_key]
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
                    source_table = context["table_name"]
                    current_source = source_table  # Reset source for each context (Narrow vs Wide)
                    rollup_segments = context["segments"]

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
        """
        r_settings = self._get_dynamic_settings()

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
            self._add_aggregate_policy(session, view_name, bucket_interval, start_offset, chunk_time_interval)


            # 6. Finalize the view so it is available as a 'source' for the next view in the loop
            session.commit()
            self._log.info(f"Successfully created hierarchical rollup: {view_name}")

        except Exception as e:
            session.rollback()
            self._log.error(f"Failed to create {view_name}: {e}")
            raise

    def _add_aggregate_policy(self, session: Session, view_name: str, bucket_interval: str, start_offset: str, chunk_time_interval: str) -> None:
        """
        Applies refresh, retention, and compression policies to a newly created view
        using granularity-specific settings from hypertable_policy.
        """
        # 1. Map view name to its granularity key for lookup
        name_lower: str = view_name.lower()

        # Define the supported granularities
        granularities: List[str] = ["hourly", "daily", "weekly", "monthly"]

        # Find the first matching granularity or use "default"
        granularity: str = next((g for g in granularities if g in name_lower), "default")

        # Dynamically retrieve the value from self
        # This replaces the need for self.get() if the values are already stored as attributes
        compress_after = getattr(self, f"{granularity}_compress_after_interval")
        drop_after = getattr(self, "drop_after", "2 years")


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
        """
        try:
            # 1. Fail-fast locking for the wide table migration
            session.execute(text("SET LOCAL lock_timeout = '15s';"))

            # 2. Build the dynamic SQL
            # We pass agg_func ('stats_agg' or 'rollup') to ensure hierarchical consistency
            metric_columns = self._resolve_metric_columns(session, agg_func)

            sql = f"""
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
            self._log.error(f"_create_rollup_wide error for {view_name}: {e}")
            raise

    def _resolve_metric_columns(self, session: Session, agg_func: str) -> List[str]:
        """
        Generates SQL aggregate expressions for the 'wide' table.
        - If reading from hypertable: uses stats_agg(column)
        - If reading from another view: uses rollup(stats_summary_column)
        """
        # 1. Fetch all clean column names from your catalog
        result = session.execute(text("SELECT clean_column_name FROM metric_catalog ORDER BY clean_column_name"))
        column_names = list(result.scalars())

        metric_expressions = []

        # Check if we are at the base level (reading raw data) or hierarchical (reading an aggregate)
        is_base_level = agg_func == 'stats_agg(metric_value)'

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
        """
        try:
            # 1. Query the TimescaleDB catalog for the view definition
            # Use a bind parameter :view_name for security and performance
            check_sql: str = text("""
                SELECT view_definition
                FROM timescaledb_information.continuous_aggregates
                WHERE view_name = :view_name
            """)
            result = session.execute(check_sql, {"view_name": view_name}).fetchone()

            # 2. Logic: If it doesn't exist, we definitely need to build it
            if not result:
                self._log.debug(f"Rollup {view_name} does not exist. Rebuild required.")
                return True

            # 3. Logic: If it exists, check the 'interval' string in the definition
            # We look for the specific time_bucket interval string (e.g., "1 hour")
            view_def = result.view_definition

            expected_interval: str = self.get_normalized_pg_interval(session, bucket_interval)
            clean_interval: logging.Pattern[str] = re.compile(re.escape(expected_interval).replace(r'\ ', r'\s+'), re.IGNORECASE)

            if not clean_interval.search(view_def):
                self._log.info(
                    f"Config mismatch for {view_name}. "
                    f"Expected: {bucket_interval}. Rebuild required."
                )
                return True
            else:
                # 4. Exists and matches config
                self._log.info(
                    f"Rollup config matches for {view_name}. "
                    f"Expected: {bucket_interval} and received {expected_interval}. No rebuild required."
                )
                return False

        except Exception as e:
            # If we can't query the catalog, assume something is wrong and signal a rebuild
            self._log.error(f"Error checking rebuild status for {view_name}: {e}")
            return True


    # -------------------------
    #  Determine wide vs narrow table usage
    # -------------------------
    def _get_dynamic_settings(self) -> dict:
        """Returns dynamic settings based on the current metric count."""
        metric_count: int = getattr(self, 'current_metric_count', 0)

        if not self.wide_table_flag or metric_count > 200:
            return self.performance_tiers["tier_low"]  # Force narrow table settings

        # Check tiers from highest to lowest
        for tier_name in ["tier_high", "tier_medium", "tier_low"]:
            tier: Dict[str, Any] = self.performance_tiers[tier_name]
            if metric_count <= tier["count"]:
                return tier

        return self.performance_tiers["tier_low"] # Fallback default

    # kluge method to convert to timescaledb internal naming conventions for intervals.
    def get_normalized_pg_interval(self, session: Session, interval_str: str) -> str:
        """
        Normalizes 'monthly', 'hourly', etc., into the exact string
        found in TimescaleDB's view_definition.
        """
        # 1. Map friendly terms to PG Interval inputs
        mapping: Dict[str, str] = {
            "monthly": "1 month",
            "weekly": "7 days",
            "daily": "1 day",
            "hourly": "1 hour"
        }

        # Use mapped value or fallback to the raw interval_str string
        pg_input: str = mapping.get(interval_str.lower(), interval_str)

        # 2. Let PostgreSQL return its internal string representation
        # This turns '1 month' -> '1 mon' and '1 hour' -> '01:00:00'
        normalized_val: str = session.execute(
            text("SELECT (:val)::interval::text"),
            {"val": pg_input}
        ).scalar()

        # 3. Format to match the view_definition style: 'string'::interval
        return f"'{normalized_val}'::interval"

    def _view_exists(self, session: Session, view_name: str) -> bool:
        """Check to see if a continuous aggregate exists in the catalog."""
        check_sql = text("SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = :name")
        return session.execute(check_sql, {"name": view_name}).fetchone() is not None


    def _drop_all_continuous_aggregates(self, session: Session) -> None:
        """
        Teardown all rollups in correct dependency order (Top-Down) to
        fix the 'DependentObjectsStillExist' error.
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
            priority_map: Dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "hourly": 1}

            def get_drop_rank(v_tuple) -> int:
                name_lower: str = v_tuple[1].lower()
                for key, val in priority_map.items():
                    if key in name_lower:
                        return val
                return 0

            # Sort descending: 4 (Weekly) drops first, 1 (Hourly) drops last.
            sorted_views = sorted(views, key=get_drop_rank, reverse=True)

            # 3. Iterate and drop each view safely
            for schema, name in sorted_views:
                full_name = f'"{schema}"."{name}"'
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
        """
        r_settings = self._get_dynamic_settings()

        stop_signal = self._start_refresh_watchdog(view_name)

        self._start_refresh_watchdog(view_name)

        start_time = time.perf_counter()
        mode = "FULL" if force_full else "INCREMENTAL"

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

                end_time = time.perf_counter()
                duration_seconds = end_time - start_time

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


    def _stop_existing_watchdog(self):
        """Signals the existing watchdog to exit immediately."""
        if hasattr(self, '_current_watchdog_signal') and self._current_watchdog_signal:
            self._current_watchdog_signal[0] = True
            self._current_watchdog_signal = None

    def _start_refresh_watchdog(self, view_name: str):
        # 1. Kill any existing watchdog before starting a new one
        self._stop_existing_watchdog()

        # 2. Create the new stop signal
        stop_signal = [False]
        self._current_watchdog_signal = stop_signal

        def monitor() -> None:
            # Use a short-lived session specifically for monitoring
            with self.SessionFactory() as session:
                # Check both the signal and the DB state
                while not stop_signal[0]:
                    try:
                        # We check if the refresh is still running
                        sql: str = text("""
                            SELECT wait_event_type FROM pg_stat_activity
                            WHERE query LIKE :pattern
                            AND state != 'idle'
                            AND pid != pg_backend_pid()
                        """)
                        res = session.execute(sql, {"pattern": f"%refresh_continuous_aggregate%'{view_name}'%"}).fetchone()

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
                    tier = self._get_dynamic_settings()
                    conn.execute(text(f"SET work_mem = '{tier['work_mem']}';"))
                    conn.execute(text(f"SET lock_timeout = '{tier['lock_timeout']}';"))

                    granularities: List[str] = ["hourly", "daily", "weekly", "monthly"]
                    prefix = "rollup_wide" if self.wide_table_flag else "rollup_narrow"

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
        """
        if hasattr(self, "_stop_refresh_rollup_event"):
            self._stop_refresh_rollup_event.set()
            self._log.info("Auto rollup refresh thread stopped.")

    def _purge_ghost_jobs(self, session: Session) -> None:
        """
        Cleans up aberrant TimescaleDB processes using dynamic type-based thresholds.
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
        flush_queue: queue.Queue,
        flush_event: threading.Event,
        backlog_lock: threading.RLock,
        log: logging.Logger
    ) -> None:

        self.backlog_file_path: Path | None = backlog_file_path
        self.max_backlog_age: int = max_backlog_age
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
                            point = json.loads(clean)
                            ts = point.get("m_time")
                            if not ts:
                                continue
                            m_time: datetime = datetime.fromisoformat(ts)
                            if now - m_time < timedelta(seconds=self.max_backlog_age):
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
            self.backlog_points.append(point)
            self._append_to_disk(point)

    def replay_to_queue(self) -> int:
        """Transfers backlog to queue. Returns count replayed."""
        count = 0
        with self._backlog_lock:
            if not self.backlog_points:
                return 0
            count: int = len(self.backlog_points)
            if count > 1:
                self._log.debug(f"Replaying {count} points to flush queue.")
                for point in self.backlog_points:
                    self._flush_queue.put(point)
                self.backlog_points.clear()
                self._sync_to_disk()
        return count

    def _append_to_disk(self, point: dict) -> None:
        if not self.backlog_file_path:
            return
        json_string = json.dumps(point, default=str)
        # trap of errata "true" in points.
        cleaned_json = re.sub(r'true', '', json_string, flags=re.IGNORECASE)
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
