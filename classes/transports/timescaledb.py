# bridge transport module for TimescaleDB, implementing a high-performance transport that writes protocol metrics
# to a TimescaleDB database with support for hypertables, continuous aggregates, rollups, and persistent disk backlog.
"""
File: timescaledb.py
timescaledb transport bridge module is free software written by Kevin Burke: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or any later version.

Copyright 2026 Kevin Burke

timescaledb transport bridge module is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.


from __future__ import annotations

You can find a copy of the GNU Affero General Public License in the documentation/bridges/timescaledb folder.
If not, see <https://www.gnu.org>.
----------------------------------------------------------------------------------------------------------------------
timescaledb transport bridge module (with rollup continuous aggregates) and persistent disk backlog.
python > 3.10 is required, 3.13 is recommended for best performance and latest features.
The transport uses the latest SQLAlchemy version for database interactions and supports automatic schema management,
including dynamic column creation based on the protocol registry, hypertable setup, and continuous
aggregate rollups for efficient querying of historical data.

Features:
 - Auto-create database (default "solar", configurable)
 - device_info (multi unique transport scraper devices)
 - device_metrics_wide hypertable per protocol with dynamic column creation based on protocol registry metrics,
        with intelligent type mapping and coercion for optimal compression
 - device_metrics_narrow hypertable
 - Hypertable compression & retention (idempotent)
 - Continuous aggregates rollups: hourly_rollup, daily_rollup, weekly_rollup, monthly_rollup with hierarchical dependencies and policies
 - Async flushing + persistent disk backlog
 - OS-local timestamps

Terminology:
Continuous Aggregates	The official TimescaleDB feature name. It's an automatically and incrementally updated
    materialized SQL view that pre-computes aggregate data (e.g., averages, sums over a time window: minute, hour, day, week or month)
    from raw data and stores it in a separate hypertable view.

Continuous Rollups	 This term refers to the process of downsampling data into successively
    coarser time granularities (e.g., from raw data to hourly summaries, then to daily summaries, then to weekly summaries,
    then to monthly summaries). This is achieved using the hierarchical continuous aggregates feature,
    where a continuous aggregate based on the output of a previous one is created. For example, a daily rollup continuous
    aggregate would be defined based on the hourly rollup continuous aggregate, and so on.

"""

import json
import logging
import math
import queue
import re
import sqlite3
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import (
    Any,
    Callable,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    Union,
)

from sqlalchemy import (
    Boolean,
    Connection,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Identity,
    Inspector,
    Integer,
    PrimaryKeyConstraint,
    Row,
    Table,
    Text,
    TextClause,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

# from sqlalchemy.engine.interfaces import ReflectedColumn
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
    registry_map_entry,
)
from defs.common import TransportSettings

from .transport_base import transport_base


class TimezoneEngine:
    def __init__(self) -> None:
        self.use_utc: bool = False
        self.machine_timezone: str = "UTC"

    def configure(self, use_utc: bool) -> None:
        """Called once at application startup to lock in the timezone configuration."""
        self.use_utc = use_utc
        self.machine_timezone = "UTC" if use_utc else get_localzone_name()

# Instantiate the single, global state engine
_TZengine = TimezoneEngine()

def configure_application_timezone(use_utc: bool) -> None:
    """Public function to set up the global timestamp configuration."""
    _TZengine.configure(use_utc)

def get_machine_timezone() -> str:
    """Returns the correct timezone string ('UTC' or IANA name) for TimescaleDB."""
    return _TZengine.machine_timezone

def _now_tz() -> datetime:
    """
    The universal timestamp generator.
    For SQLAlchemy models and all application logic.
    """
    if _TZengine.use_utc:
        return datetime.now(timezone.utc)
    return datetime.now().astimezone()

# base class for all tables.
class Base(DeclarativeBase):
    pass

class ProtocolRegistry(Base):
    __tablename__ = "protocol_registry"

    protocol_id: Mapped[int] = mapped_column(Integer, Identity(cache=1),primary_key=True, autoincrement=True)
    protocol_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    wide_table_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # None = narrow only (>200 metrics)

    metric_count: Mapped[int] = mapped_column(Integer, default=0)

    # Rollup state — enough for RollupManager to rediscover and manage views
    rollup_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # e.g. "rollup_wide__eg4_18kpv" — the base name views are derived from
    # None if rollups are disabled or not yet set up for this protocol

    rollup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rollup_setup_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    # False until setup_narrow_rollup completes successfully — allows restart recovery

    last_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Tracks when rollups were last successfully refreshed per protocol

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_tz)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_tz, onupdate=_now_tz)

    metriccatalog: Mapped[List["MetricCatalog"]] = relationship(
        "MetricCatalog", back_populates="protocolregistry"
    )
    deviceinfo: Mapped[List["DeviceInfo"]] = relationship(
        "DeviceInfo", back_populates="protocolregistry"
    )

class MetricCatalog(Base):
    __tablename__: str = "metric_catalog"

    catalog_id: Mapped[int] = mapped_column(Integer, Identity(cache=1), primary_key=True, autoincrement=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocol_registry.protocol_id"), nullable=False)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    clean_column_name: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[str] = mapped_column(Text, default='double precision', nullable=False)
    unit_mod: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_tz, onupdate=_now_tz)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Same metric name can exist in multiple protocols
        UniqueConstraint('protocol_id', 'metric_name', name='uq_metric_catalog_protocol_metric'),
        # Same clean column name can exist in multiple protocols' wide tables
        # but must be unique within a protocol
        UniqueConstraint('protocol_id', 'clean_column_name', name='uq_metric_catalog_protocol_column'),)

    protocolregistry: Mapped["ProtocolRegistry"] = relationship(
        "ProtocolRegistry", back_populates="metriccatalog")
class DeviceInfo(Base):
    __tablename__: str = "device_info"

    device_info_id: Mapped[int] = mapped_column(Integer, Identity(cache=1), primary_key=True, autoincrement=True)
    protocol_id: Mapped[Optional[int]] = mapped_column(ForeignKey("protocol_registry.protocol_id"), nullable=True, index=True)
    device_identifier: Mapped[Optional[str]] = mapped_column(Text, index=True)
    device_serial_number: Mapped[Optional[str]] = mapped_column(Text)
    device_name: Mapped[Optional[str]] = mapped_column(Text)
    device_manufacturer: Mapped[Optional[str]] = mapped_column(Text)
    device_model: Mapped[Optional[str]] = mapped_column(Text)
    device_firmware: Mapped[Optional[str]] = mapped_column(Text)
    device_location: Mapped[Optional[str]] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_tz)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_tz, onupdate=_now_tz)

    # Relationships
    protocolregistry: Mapped[Optional["ProtocolRegistry"]] = relationship(
        "ProtocolRegistry", back_populates="deviceinfo")
    devicemetricsnarrow: Mapped[List["DeviceMetricsNarrow"]] = relationship(
        "DeviceMetricsNarrow", back_populates="deviceinfo")

class DeviceMetricsNarrow(Base):
    __tablename__: str = "device_metrics_narrow"

    # In TimescaleDB/SQLAlchemy, mark columns that are part of PK as primary_key=True in mapped_column
    # as well as defining the constraint in __table_args__ to ensure correct behavior and indexing.
    # metric_value is forced float for all numerics and booleans. Ascii values are stored in metric_ascii column,
    # which is nullable and only populated for string types.
    m_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_tz, primary_key=True)
    device_info_id: Mapped[int] = mapped_column(ForeignKey("device_info.device_info_id"), primary_key=True)
    metric_name: Mapped[str] = mapped_column(Text, primary_key=True)
    metric_value: Mapped[float] = mapped_column(Float)
    metric_ascii: Mapped[str] = mapped_column(Text, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint('m_time', 'device_info_id', 'metric_name', name='device_metrics_narrow_pkey'),
    )

    deviceinfo: Mapped["DeviceInfo"] = relationship("DeviceInfo", back_populates="devicemetricsnarrow")

# The TimescaleDBConnectionManager class encapsulates the SQLAlchemy engine and connection pool management for a single TimescaleDB database.
class TimescaleDBConnectionManager:
    """
    Owns the SQLAlchemy engine and connection pool for a single TimescaleDB database.
    Shared across all protocol instances pointing at the same database.

    Usage:
        manager = TimescaleDBConnectionManager(host, port, database, username, password)
        manager.connect()

        protocol_a = timescaledb(settings_a, connection_manager=manager)
        protocol_b = timescaledb(settings_b, connection_manager=manager)

        manager.dispose()  # clean shutdown of shared pool
    """

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_recycle: int = 3600,
        log: logging.Logger | None = None
    ) -> None:

        self.host: str = host
        self.port: int = port
        self.database: str = database
        self.username: str = username
        self.password: str = password
        self.pool_size: int = pool_size
        self.max_overflow: int = max_overflow
        self.pool_recycle: int = pool_recycle
        self._log: logging.Logger = log or logging.getLogger(__name__)

        self._engine: Engine | None = None
        self._lock: threading.RLock = threading.RLock()
        self._ref_count: int = 0  # tracks how many protocol instances are using this manager
        self._connected: bool = False


    @classmethod
    def from_settings(cls, settings: TransportSettings, log: logging.Logger | None = None ) -> "TimescaleDBConnectionManager":
        """
        Construct a ConnectionManager directly from a configparser SectionProxy.
        Connection settings are read from the section, with the same defaults
        as the timescaledb class uses.
        """
        return cls(
            host=settings.get("host", fallback="localhost"),
            port=settings.getint("port", fallback=5432),
            database=settings.get("database", fallback="solar"),
            username=settings.get("username", fallback=""),
            password=settings.get("password", fallback=""),
            pool_size=settings.getint("pool_size", fallback=5),
            max_overflow=settings.getint("max_overflow", fallback=10),
            pool_recycle=settings.getint("pool_recycle", fallback=3600),
            log=log
        )

    # -------------------------
    # Connection lifecycle
    # -------------------------

    def connect(self) -> None:
        """
        Create the shared engine and verify the connection.
        Safe to call multiple times — only creates the engine once.
        """
        with self._lock:
            if self._engine is not None:
                return
            try:
                self._create_database_if_missing()
                url: str = (
                    f"postgresql+psycopg2://{self.username}:{self.password}"
                    f"@{self.host}:{self.port}/{self.database}"
                )
                self._engine = create_engine(
                    url,
                    pool_pre_ping=True,
                    future=True,
                    pool_recycle=self.pool_recycle,
                    pool_size=self.pool_size,
                    max_overflow=self.max_overflow
                )
                with self._engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                self._connected = True
                self._log.info(f"ConnectionManager: connected to '{self.database}'")
            except OperationalError as e:
                self._connected = False
                self._log.error(f"ConnectionManager: failed to connect: {e}")
                self._log.error(f"❌ [COMMUNICATION LOST] --- Name: {self.database} ---")
                raise

    def dispose(self) -> None:
        """
        Dispose the shared engine. Should only be called when all protocol
        instances have been closed — use ref counting to enforce this.
        """
        with self._lock:
            if self._ref_count > 0:
                self._log.warning(
                    f"ConnectionManager: dispose() called with {self._ref_count} "
                    f"active references — deferring."
                )
                return
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None
                self._connected = False
                self._log.info("ConnectionManager: engine disposed.")

    # -------------------------
    # Session factory
    # -------------------------

    def make_session_factory(self) -> sessionmaker[Session]:
        """
        Returns a new sessionmaker bound to the shared engine.
        Each protocol instance calls this once during its own init
        and holds the returned factory for its lifetime.
        """
        if self._engine is None:
            raise RuntimeError("ConnectionManager: connect() must be called before make_session_factory()")
        return sessionmaker(
            bind=self._engine,
            autocommit=False,
            expire_on_commit=False,
            autoflush=False
        )

    # -------------------------
    # Reference counting
    # -------------------------

    def register(self) -> None:
        """Called by each protocol instance during __init__."""
        with self._lock:
            self._ref_count += 1
            self._log.debug(f"ConnectionManager: registered instance (total: {self._ref_count})")

    def unregister(self) -> None:
        """
        Called by each protocol instance during close().
        Automatically disposes the engine when the last instance unregisters.
        """
        with self._lock:
            self._ref_count = max(0, self._ref_count - 1)
            self._log.debug(f"ConnectionManager: unregistered instance (total: {self._ref_count})")
            if self._ref_count == 0:
                self._log.info("ConnectionManager: last instance unregistered — disposing engine.")
                self.dispose()

    # -------------------------
    # Engine access
    # -------------------------

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("ConnectionManager: engine is not initialized. Call connect() first.")
        return self._engine

    @property
    def connected(self) -> bool:
        return self._connected


    def _create_database_if_missing(self) -> None:
        default_url: str = (
            f"postgresql+psycopg2://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/postgres"
        )
        try:
            default_engine: Engine = create_engine(default_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
            with default_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :d"),
                    {"d": self.database}
                ).fetchone()
                if not row:
                    self._log.info(f"Database '{self.database}' not found — creating")
                    conn.execute(text(f'CREATE DATABASE "{self.database}"'))
                    self._log.info(f"Database '{self.database}' created")
                else:
                    self._log.debug(f"Database '{self.database}' already exists")
            default_engine.dispose()
        except Exception as e:
            self._log.error(f"Failed to verify/create database '{self.database}': {e}")
            raise


# TimescaleDB transport bridge class
class timescaledb(transport_base):


    transport_type = "bridge"
    """
    TimescaleDB transport bridge with hypertable, continuous aggregates and rollup support.
    The class uses background threads for auto-refresh of rollups and stale data detection.
    Uses a global sqlalchemy session for most database operations.
    """
    __version__: str = "1.0.0"

    # -------------------------
    # Default settings (overridable by settings SectionProxy)
    # -------------------------
    # Database connection
    host: str = "localhost"
    port: int = 5432
    database: str = "solar"
    username: str = ""
    password: str = ""

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
        "UINT64": "NUMERIC",
        "ACC32": "BIGINT",
        # Flags & Bits (Integers for Delta-Delta compression)
        "_8BIT_FLAGS": "SMALLINT",
        "_16BIT_FLAGS": "INTEGER",
        "_32BIT_FLAGS": "BIGINT",
        # Strings (Dictionary compression)
        "ASCII": "TEXT",
        "HEX": "TEXT",
        "STRING": "TEXT",
        "STRING16": "TEXT",
        "STRING32": "TEXT",
        "TEXT": "TEXT",
        "_1BIT": "BOOLEAN",

        # 1. Unsigned Bit-lengths (_2BIT to _15BIT)
        **{f"_{i}BIT": "SMALLINT" for i in range(2, 16)},
        "_16BIT": "INTEGER", # 16-bit unsigned needs INTEGER

        # 2. Signed Bits (_2SBIT to _16SBIT)
        **{f"_{i}SBIT": "SMALLINT" for i in range(2, 17)},

        # 3. Signed Magnitude (_2SMBIT to _16SMBIT)
        **{f"_{i}SMBIT": "SMALLINT" for i in range(2, 17)},

        # Float (Add these to the register maps if you use them - Gorilla compression)
        "FLOAT32": "REAL",
        "FLOAT64": "DOUBLE PRECISION"
    }

    # Type Coercion based on timescale_type_map values for field definitions.
    # data is coerced to improve compression in metrics' tables.
    INT_TYPES: set[str] = {"SMALLINT", "INTEGER", "BIGINT"}
    FLOAT_TYPES: set[str] = {"REAL", "DOUBLE PRECISION", "NUMERIC", "FLOAT"}

    # persistent storage/backlog settings. Default folder name and file name are the same but can be user configured.
    enable_persistent_storage: bool = True
    backlog_storage_path: Path = Path(__file__).resolve().parent.parent.parent / "backlogs"
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

    # stale data settings and fields.  Stale data is read per transport batch based on the timestamp of the last row of metrics data received.
    # If the current time exceeds that timestamp by more than the stale_data_timeout, then the transport will consider the data to be stale
    # and trigger a cleanup of incomplete batches in the database, as well as an optional upstream reconnect if request_upstream_reconnect
    # callback is set by the user.
    stale_data_timeout: int = 300       # seconds before considering data stale for incomplete batch cleanup
    max_stale_attempts: int = 3         # Number of times to read the data stream to determine if it's stale.
    retry_delay_mins: int = 5

    current_metric_count: int = 0

    # write_requires_complete_cycle is used to prevent writing incomplete batches of data to the database,
    # which can cause issues with rollup continuous aggregates and data integrity.
    # When True, the transport will only write data to the database once it has received a complete cycle
    # of metrics for a given protocol, as determined by the registry map.  This allows the transport
    # to ensure that all expected metrics for a given timestamp have been received before writing to the database,
    # which is important for maintaining data integrity and ensuring that rollups are accurate.
    write_requires_complete_cycle = True


    def __init__(self, settings: TransportSettings, connection_manager: TimescaleDBConnectionManager | None = None) -> None:
        # This explicitly checks: "Does this object actually have the methods I need?"
        if not isinstance(settings, TransportSettings):
            msg: str = f"Provided settings object {type(settings)} is missing required methods!"
            raise TypeError(msg)
        """
        Initialize the TimescaleDB transport bridge.

        Args:
            settings (SectionProxy): Configuration section containing database and transport options.

        Configuration options:
            - auto_refresh_interval (int): Seconds between rollup refreshes (default: 21600)
            - backlog_storage_path (str): Path for backlog files (default: "parent/timescaledb_backlog")
            - database (str): Database name (default: "solar")
            - device_name (str): Name for the bridge (default: "TimeScaleDB MPG Bridge")
            - enable_auto_refresh (bool): Enable periodic rollup refresh (default: True)
            - enable_compression (bool): Enable compression on hypertables at startup (default: True)
            - enable_persistent_storage (bool): Enable disk backlog (default: True)
            - force_float (bool): Force metric values to float (default: True)
            - host (str): Database host (default: "localhost")
            - hypertable_defaults: Dicts for hypertable narrow and wide creation and policies
            - max_backlog_age (int): Max age (seconds) for backlog points (default: 86400) 24 hours
            - max_backlog_size (int): Max backlog points (default: 10000)
            - max_reconnect_delay (int): Max reconnect delay (default: 300)
            - migrate_data: whether to attempt to migrate existing data when creating hypertables and rollups. Set to False to skip migration and start fresh with new schema.
            - password (str): Database password
            - port (int): Database port (default: 5432)
            - reconnect_attempts (int): Max reconnect attempts (default: 5)  if set to 0, no limit.
            - reconnect_delay (int): Initial reconnect delay (default: 5)
            - rollup_defaults: dict for rollup settings
            - stale_data_timeout (int): Seconds before considering data stale for incomplete batch cleanup (default: 300)
            - use_exponential_backoff (bool): Use exponential backoff (default: True)
            - use_utc_timestamp (bool): Use UTC timezone for all timestamps instead of local machine timezone (default: False)
            - username (str): Database username

        Thread behavior:
            - Starts a background thread for batch flushing of metrics.
            - Starts a background thread for periodic rollup refreshes.
            - Thread safety is ensured for internal operations.
        """

        """
        1.0.0 Initial Production Version

        """
        # Per-protocol state — populated as init_bridge is called for each scraper
        self._registered_protocols: set[str] = set()

        # Maps protocol_name -> wide_table_name (or None if narrow-only)
        self._protocol_wide_table_map: dict[str, str | None] = {}

        # Maps protocol_name -> metric_mapping dict
        # Each protocol has its own metric_name -> (clean_column_name, data_type) map
        self._protocol_metric_mappings: dict[str, dict[str, tuple[str, str]]] = {}
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

        # load reconnect/backoff settings
        self.use_exponential_backoff = settings.getboolean("use_exponential_backoff", fallback=self.use_exponential_backoff)
        self.max_reconnect_delay = settings.getint("max_reconnect_delay", fallback=self.max_reconnect_delay)
        self.reconnect_attempts: int = settings.getint("reconnect_attempts", fallback=self.reconnect_attempts)
        self.reconnect_delay: int = settings.getint("reconnect_delay", fallback=self.reconnect_delay)
        self.force_float = settings.getboolean("force_float", fallback=self.force_float)

        # stale data settings
        self.stale_data_timeout: int = settings.getint("stale_data_timeout", fallback=self.stale_data_timeout)
        # wait for complete data to write to db.
        self.write_requires_complete_cycle = settings.getboolean("write_requires_complete_cycle", fallback=self.write_requires_complete_cycle)

        # UTC timestamp mode setting - set context for this transport instance
        self.use_utc_timestamp: bool = settings.getboolean("use_utc_timestamp", fallback=False)
        configure_application_timezone(self.use_utc_timestamp)
        self.machine_timezone: str = get_machine_timezone()

        # persistent backlog settings
        project_root: Path = Path(__file__).resolve().parents[2]
        # Force path to look relative by stripping leading slashes/drives
        self.enable_persistent_storage = settings.getboolean("enable_persistent_storage", fallback=self.enable_persistent_storage)
        self.backlog_storage_path_value: str | Path = settings.get("backlog_storage_path", fallback=self.backlog_storage_path)
        if not isinstance(self.backlog_storage_path_value, Path):
            clean_setting: str = self.backlog_storage_path_value.lstrip("\\/")
            self.backlog_storage_path_value = Path(clean_setting)

        self.backlog_storage_path = (project_root /self.backlog_storage_path_value).resolve()

        self.backlog_file_name = settings.get("backlog_file_name", fallback=self.backlog_file_name)
        self.max_backlog_size = settings.getint("max_backlog_size", fallback=self.max_backlog_size)
        self.max_backlog_age: int = settings.getint("max_backlog_age", fallback=self.max_backlog_age)

        # hypertable / rollup options that are user defined.  These are sent to the rollup manager class
        # which will handle the hypertable and rollup creation and maintenance based on these settings.

        self.rollup_policy: dict[str,Any] = {
            # Hypertable Settings
            "current_metric_count": self.current_metric_count,
            "tsdb_connected": self.tsdb_connected,
            "machine_timezone": self.machine_timezone,
            "drop_after": settings.get("drop_after", fallback=self.drop_after),
            "migrate_data": settings.getboolean("migrate_data", fallback=self.migrate_data),
            "enable_compression": settings.getboolean("enable_compression", fallback=self.enable_compression),
            # Rollup Settings
            "auto_refresh_interval": settings.getint("auto_refresh_interval", fallback=self.auto_refresh_interval),
            "enable_auto_refresh": settings.getboolean("enable_auto_refresh", fallback=self.enable_auto_refresh),
            "enable_rollups": settings.getboolean("enable_rollups", fallback=self.enable_rollups),
        }

        # Explicitly set bridge name if not set by user, since this transport doesn't have a device name from an
        # upstream transport to pull from.
        self.device_name = settings.get("device_name", fallback="TimescaleDB MPG Bridge")

        super().__init__(settings)

        # registry map for the last batch of data received, used for stale data detection.
        self._stale_registry: dict[str, dict[str, Any]] = {}
        self._last_cleanup_time: float = time.time()

        self._verified_devices: set[str] = set()
        # transport_name -> device_info_id cache to minimize DB lookups for device_info_id during batch writes
        self._device_cache: dict[str, int] = {}

        # end user settings
        #*********************************

        # Instance-scoped metadata — isolates schema per connection
        #self._metadata: MetaData = MetaData()
        self._base: DeclarativeBase  # will be constructed after engine is ready

        # load all registry metrics' map.
        self.registry_metrics: dict[Registry_Type, List[registry_map_entry]]  = {}

        # keyed as: self._wide_columns_cache[wide_table_name] = {col1, col2, ...}
        self._wide_columns_cache: dict[str, set[str]] = {}

        # keyed as: self._protocol_metric_mappings[protocol_name][metric_name] = (clean_col, data_type)
        self._protocol_metric_mappings: dict[str, dict[str, tuple[str, str]]] = {}

        self.device_info_id = None  # will be set after data scrape, per transport batch.

        # SQLAlchemy init runtime
        # If a shared manager is provided, use it.
        # Otherwise create an instance-scoped one.
        # number of extra connections to add to the pool size beyond the number of scrapers,
        # to ensure there are enough connections for background threads and other operations.

        #  multiple scrapers registering via init_bridge can each trigger schema work +
        # # rollup refresh thread + schema/init_bridge thread + reconnect thread -- pool overhead.
        POOL_OVERHEAD = 3
        pool_size: int = max(2, self.scraper_count + POOL_OVERHEAD)

        if connection_manager is not None:
            self._connection_manager: TimescaleDBConnectionManager = connection_manager
        else:
            self._connection_manager = TimescaleDBConnectionManager(
                host=self.host,
                port=self.port,
                database=self.database,
                username=self.username,
                password=self.password,
                pool_size=pool_size,
                log=self._log
            )
            self._connection_manager.connect()

        self._connection_manager.register()

        # Engine and SessionFactory come from the manager
        self.engine = self._connection_manager.engine
        self.SessionFactory = self._connection_manager.make_session_factory()

        # -------------------------
        # threading
        # -------------------------

        # Initialize async flush queue and worker thread.  Start it here so it's ready at full init.
        self._flush_queue: queue.Queue = queue.Queue(maxsize=0)
        self._flush_thread = threading.Thread(target=self._flush_worker, daemon=True, name="FlushWorker")
        self._flush_lock: Lock = threading.Lock()
        self._flush_event: threading.Event = getattr(self, "_flush_event", threading.Event())
        # event used to stop all threads
        self._stop_event: threading.Event = getattr(self, "_stop_event", threading.Event())

        # init runtime backoff settings for connection attempts
        self._reconnect_lock: RLock = threading.RLock()     # prevents multiple concurrent TSDB reconnect triggers
        self._reconnect_thread_running = False      # guard to prevent duplicate reconnect threads
        self._stop_reconnect_event:  threading.Event = getattr(self, "_stop_reconnect_event", threading.Event())

        self.migration_in_progress = threading.Event()  # event to pause flushes during rollup migration

        # Protects the BacklogManager (the list and the .jsonl file).
        self._backlog_lock: RLock = threading.RLock()  # lock for backlog operations

        # Lock for protecting schema mutations and metadata reflection
        # RLock to allow nested calls within the same thread
        # Protects the SQLAlchemy Metadata and Table Identifiers (the "structure" of the Wide Table).
        self._schema_lock: RLock = threading.RLock()

        # lock for protecting device_info appends when incoming data is from two or more source transports
        self._device_lock: Lock = threading.Lock()

        # persistent backlog file and path, both the file path and the in-memory backlog file are initialized here
         # Ensure the backlog storage path is a valid directory
        if not self.backlog_storage_path.exists():
            try:
                self.backlog_storage_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self._log.error(f"Failed to create backlog storage directory '{self.backlog_storage_path}': {e}")
                self.send_message(
                    message=f"Error: Unable to create backlog storage directory at '{self.backlog_storage_path}'. Check logs for details.",
                    title="MPG Backlog Storage Error",
                    priority=1
                )
                raise
        # full path to backlog file
        self.backlog_file_path: Path = self.backlog_storage_path / f"{self.backlog_file_name}.db"

        self.backlog = BacklogManager(
            backlog_file_path=self.backlog_file_path,
            max_backlog_age=self.max_backlog_age,
            max_backlog_size=self.max_backlog_size,
            send_message = self.send_message,
            flush_queue=self._flush_queue,
            flush_event=self._flush_event,
            backlog_lock=self._backlog_lock,
            log=self._log
        )
        # Rollup manager is initialized after the database connection is established, since
        # it needs the session factory and engine to set up the schema and policies.
        # don't AttributeError if init raises early.
        self.rollup_mgr: "RollupManager | None" = None

        if self.enable_persistent_storage:
            self.backlog.load_from_disk()

        # attempt tsdb connection now
        try:
            self.connect_tsdb()
        except Exception as e:
            self._log.error(f"Initial connect failed: {e}")
            self._set_tsdb_connected(conn_value = False, conn_reason = "Initial TSDB connect was not successful")
        """
            Attribute:
            request_upstream_reconnect (Callable[[], None] | None):
            Optional callback function that, if set by the user,
            will be called to trigger an source data reconnect when stale data is detected or a reconnect is required.

            The Timescaledb bridge transport itself will handle reconnecting to the TimescaleDB database when a connection issue is detected,
            but this callback allows users to also trigger a reconnect of the upstream data source (e.g., inverter or scraper)
            if they want to attempt to resolve stale data conditions or other issues that might be mitigated by refreshing the
            data source connection.
        """
        self.request_upstream_reconnect: Callable[[str], None] | None = None

    def connect_tsdb(self) -> None:
        """
        Connect to DB, build device_metrics_wide__* table from metrics data, and ensure schema/hypertable/policies exist.
        If from_transport data provided, ensure device_info insert for that transport.
        """
        try:
        # create database if missing.  Connect to standard default "postgres" database first to then check/create target database structure.
            self._log.info(f"Starting Timescaledb {self.__version__} and attempting connection:")
            try:
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit"))
                self._set_tsdb_connected(conn_value=True, conn_reason="Connection verified")
            except OperationalError as e:
                self._set_tsdb_connected(conn_value=False, conn_reason="Connection failed")
                self._log.error(f"Connection verification failed: {e}")
                self._log.error(f"❌ [COMMUNICATION LOST] --- Name: {self.transport_name} ---")
                raise

            # Clean up any orphaned locks from a previous crashed session
            # before attempting schema work that could deadlock against them.
            try:
                self._cleanup_orphaned_locks()
            except Exception as e:
                self._log.error(f"Orphaned lock cleanup failed: {e}")

            # create ORM tables. DeviceInfo, DeviceMetricsNarrow.  MetricCatalog for dynamic column names.
            # ProtocolRegistry for protocol metadata.
            try:
                self._create_tables()

            except Exception as e:
                self._log.error(f"ORM table creation error: {e}")

            try:
                self._start_flush_thread()
            except Exception as e:
                self._log.error(f"thread start failed: {e}")

        except Exception as e:
            self._set_tsdb_connected(conn_value = False, conn_reason ="Connect unsuccessful at startup")
            self._log.error(f"connect() failed: {e}")
            raise

    # Centralize state transitions for tsdb_connected helper
    def _set_tsdb_connected(self, conn_value: bool, conn_reason: str) -> None:
        """
        This helper centralizes all updates to the tsdb_connected state variable, ensuring that related state and logging are consistently handled.
        Connection status messaging is handled separately in the connect base property setter to avoid issues with calling
        send_message before the transport is fully initialized.  This method focuses on internal state management and logging.
        """
        with self._reconnect_lock:
            if self.tsdb_connected != conn_value:   # if we change state
                self.tsdb_connected: bool = conn_value  # new state
                self.connected = self.tsdb_connected  # set the MPG connected flag here to mimic central connection state.
                self.rollup_policy["tsdb_connected"] = conn_value  # set the connect status in the rollup class.
                self._log.info(f"TimescaleDB is connected -> {conn_value} ({conn_reason})")

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
                self._log.info(f"Reconnect attempt {attempt_no} starting")
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
                    self._set_tsdb_connected(conn_value = True, conn_reason = f"Reconnect attempt {attempt_no} successful")
                    self._log.info(f"Reconnect attempt {attempt_no} successful.")
                except Exception as e:
                    self._set_tsdb_connected(conn_value = False, conn_reason = f"Reconnect attempt {attempt_no} unsuccessful")
                    self._log.warning(f"Reconnect attempt {attempt_no} failed during connect(): {e}")
                    self._log.warning(f"❌ [COMMUNICATION LOST] {e} --- to TimescaleDB --- Attempt {attempt_no}{'' if attempts <= 0 else f'/{attempts}'} --- Will retry in {delay}s.")

                with self._reconnect_lock:
                    tsdb_connected: bool = self.tsdb_connected

                if tsdb_connected:
                    self._log.info("Auto-reconnect: connection re-established.")

                    # Restore runtime state before allowing flush worker to resume
                    try:
                        self._rediscover_protocols()
                        # repopulate rollup views after reconnect to ensure they're up to date before any flushes occur.
                        # This is important in case the connection issue was due to a transient schema issue or if the
                        # schema changed during the disconnect.
                        if self.rollup_mgr is not None:
                            try:
                                self.rollup_mgr.repopulate_known_rollup_views()
                            except Exception as e:
                                self._log.error(f"Failed to repopulate rollup views after reconnect: {e}")
                    except Exception as e:
                        self._log.error( f"Protocol rediscovery failed after reconnect: {e}" )
                        # Non-fatal — protocols will re-register via init_bridge
                        # on next MPG restart if rediscovery fails here

                    try:
                        if getattr(self, "enable_persistent_storage", False):
                            with self._backlog_lock:
                                self.backlog.replay_to_queue()
                    except Exception as e:
                        self._log.error(f"Backlog flush failed after reconnect: {e}")
                        self.send_message(
                            message="Error: Backlog flush failed after reconnect. Check logs for details.",
                            title="MPG TimescaleDB Backlog Flush Error",
                            priority=1
                        )

                    break

                # not tsdb_connected: compute next delay (exponential if configured)
                if use_exp:
                    delay = min(delay * 2, max_delay)
                else:
                    delay = min(delay, max_delay)

            if not getattr(self, "tsdb_connected", False):
                # Final log if exhausted
                if attempts > 0 and attempt_no >= attempts:
                    self._log.error("Auto-reconnect: exhausted reconnect attempts. Will continue buffering to backlog.")
                    msg: str = (f"❌ [COMMUNICATION LOST] --- to TimescaleDB --- Attempt "
                                f"{attempt_no}{'' if attempts <= 0 else f'/{attempts}'} --- Will retry in {delay}s.")
                    self._log.error(msg)
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
            self._reconnect_lock = threading.RLock()
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
            self._set_tsdb_connected(conn_value = False, conn_reason = "Connect unsuccessful")
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


    def _ensure_wide_table_exists(self, table_name: str) -> None:
        """
        Ensures a protocol-specific wide metrics table exists in PostgreSQL.
        Creates it if missing with the same base structure as device_metrics_wide__*
        (m_time, device_info_id) — dynamic metric columns are added later
        by _ensure_columns_for_metrics.

        The table is created as a plain PostgreSQL table. RollupManager's
        ensure_hypertables() converts it to a TimescaleDB hypertable
        during setup_narrow_rollup(), which is called after this method completes.

        Safe to call on every startup — the IF NOT EXISTS guard makes it
        idempotent.

        Args:
            table_name: The protocol-specific wide table name, e.g.
                        'device_metrics_wide__eg4_18kpv'. Must be a
                        pre-validated SQL-safe name from _safe_table_name().

        Raises:
            ConnectionError: if not connected to TimescaleDB.
            SQLAlchemyError: if table creation fails unexpectedly.
        """
        with self._reconnect_lock:
            tsdb_connected: bool = self.tsdb_connected

        if not tsdb_connected:
            msg: str = f"Cannot create wide table '{table_name}' — not connected."
            raise ConnectionError(msg)

        with self.SessionFactory() as session:
            try:
                with session.begin():
                    self._schema_advisory_lock(session)

                    # 1. Create the base table if it doesn't exist.
                    #    Only the structural columns are created here —
                    #    metric columns are added by _ensure_columns_for_metrics.
                    #    table_name comes from _safe_table_name() so f-string is safe.
                    session.execute(text(f"""
                        CREATE TABLE IF NOT EXISTS {table_name} (
                            m_time          TIMESTAMPTZ     NOT NULL,
                            device_info_id  INTEGER         NOT NULL
                                REFERENCES device_info(device_info_id)
                                ON DELETE CASCADE,
                            CONSTRAINT {table_name}_pkey
                                PRIMARY KEY (m_time, device_info_id)
                        );
                    """))

                    # 2. Create the descending index on (m_time, device_info_id)
                    #    TimescaleDB will add its own chunk indexes on top of this
                    #    when the table is converted to a hypertable.
                    session.execute(text(f"""
                        CREATE INDEX IF NOT EXISTS {table_name}_time_device_idx
                            ON {table_name} (m_time DESC, device_info_id);
                    """))

                # 3. Register the new table in SQLAlchemy metadata so the ORM
                #    and flush worker can reference it by name immediately.
                #    Uses extend_existing=True so re-running on restart is safe.
                with self._schema_lock:
                    Table(table_name, Base.metadata, autoload_with=self.engine, extend_existing=True)

                self._log.info(f"Wide table '{table_name}' created/verified successfully.")

            except SQLAlchemyError as e:
                self._log.error(f"_ensure_wide_table_exists failed for '{table_name}': {e}")
                raise

    # -------------------------
    # 3. Ensure ORM tables exist
    # -------------------------

    def _create_tables(self) -> None:
        """
         Create ORM tables for device_info, device_metrics_narrow, protocol_registry and metric_catalog.
            The wide tables for each protocol/device are created dynamically by _ensure_wide_table_exists when protocols register via init_bridge,

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
                    session.execute(text("CREATE INDEX IF NOT EXISTS device_metrics_narrow_time_idx ON device_metrics_narrow (m_time DESC, device_info_id, metric_name);"))
                self._log.info("ORM tables and indexes created/ensured")
            except Exception as e:
                self._log.error(f"ORM tables and indexes creation error: {e}")
                raise

    # -------------------------
    #  4. Write device information metadata
    # -------------------------

    def _get_or_create_device(self, from_transport: transport_base) -> int:
        """
        Upserts a row into device_info for the given transport and returns
        its device_info_id.

        First call per session hits the DB to insert or update all metadata
        fields (name, model, serial, location, protocol_id). Subsequent calls
        for the same transport_name are served from the two-level cache
        (_device_cache + _verified_devices) with no DB round-trip.

        The database doesn't know which field changed between restarts — it
        only knows the unique key (transport). on_conflict_do_update ensures
        any changed metadata (e.g. device_name in config) is kept current.
        """

        t_name: str = from_transport.transport_name

        # Fast path — verified and cached this session, no DB needed.  False if transport_name is missing
        # from the cache for any reason, including a name change, which is safe since the DB will be the source of truth on the first
        # call and will populate the cache.
        if t_name in self._device_cache and t_name in self._verified_devices:
            return self._device_cache.get(t_name) or 100

        # Slow path — first packet for this transport this session.
        # Lock prevents two concurrent threads racing to insert the same row.
        with self._device_lock:

            # Double-check inside the lock in case another thread just resolved it.
            if t_name in self._device_cache and t_name in self._verified_devices:
                return self._device_cache[t_name]

            with self.SessionFactory() as session:
                try:
                    # Resolve protocol_id within the same session so we don't
                    # need a separate round-trip.  nullable=True on the column
                    # means a None here is safe.
                    protocol_name: str = from_transport.protocol_name
                    protocol_id: int | None = None
                    if protocol_name:
                        protocol_id = session.execute(
                            text(
                                "SELECT protocol_id FROM protocol_registry "
                                "WHERE protocol_name = :p"
                            ),
                            {"p": protocol_name}
                        ).scalar()

                    # Build the upsert — transport is the unique key.
                    stmt = pg_insert(DeviceInfo).values(
                        transport=t_name,
                        protocol_id=protocol_id,
                        device_identifier=from_transport.device_identifier,
                        device_name=from_transport.device_name,
                        device_manufacturer=from_transport.device_manufacturer,
                        device_model=from_transport.device_model,
                        device_serial_number=from_transport.device_serial_number,
                        device_location=from_transport.device_location,
                        created_at=_now_tz(),
                        updated_at=_now_tz()
                    )

                    upsert_stmt = stmt.on_conflict_do_update(
                        index_elements=['transport'],
                        set_={
                            "protocol_id":         stmt.excluded.protocol_id,
                            "device_identifier":   stmt.excluded.device_identifier,
                            "device_name":         stmt.excluded.device_name,
                            "device_manufacturer": stmt.excluded.device_manufacturer,
                            "device_model":        stmt.excluded.device_model,
                            "device_serial_number":stmt.excluded.device_serial_number,
                            "device_location":     stmt.excluded.device_location,
                            "updated_at":          stmt.excluded.updated_at,
                        }
                    ).returning(DeviceInfo.device_info_id)

                    db_id: int = session.execute(upsert_stmt).scalar_one()
                    session.commit()

                except Exception as e:
                    session.rollback()
                    self._log.error(f"Upsert failed for '{t_name}': {e}")
                    # Return cached id if available, otherwise a sentinel value.
                    return self._device_cache.get(t_name, 100)
                else:
                    self._device_cache[t_name] = db_id
                    self._verified_devices.add(t_name)
                    return db_id

    def _extract_metric_names(
        self,
        registry_map: dict[Registry_Type, list[registry_map_entry]],
        synthetic_fields: list[tuple[str, str, Any, Any]] | None = None,
    ) -> list[tuple[str, str, Any, Any]]:

        """
        Extracts metric names, data types, unit modifiers and notes from a
        transport's registry map for use in schema creation.

        accepts any transport's registry_map directly, enabling
        multi-protocol schema registration.

        Args:
            registry_map: The registry map from a transport_base instance,
                        accessed via from_transport.registry_map.
                        Keyed by Registry_Type, values are lists of
                        registry_map_entry objects.
            synthetic_fields: Optional list of (variable_name, data_type,
                        unit_mod, note) tuples from
                        transport_base.synthetic_fields_metadata.  These are
                        appended to the registry-derived metrics so that
                        columns computed by post_process_data are registered
                        in the wide table schema with the correct types at
                        init_bridge time.  This prevents _validate_wide_row
                        from seeing them as unknown extra_keys and crashing
                        the flush worker.

        Returns:
            Sorted list of (variable_name, data_type, unit_mod, note) tuples,
            filtered to INPUT, HOLDING, COIL and DISCRETE registry types only,
            with synthetic fields appended (duplicates removed by variable_name).
            Returns empty list if registry_map is None, empty, or malformed.
            TODO  adapt for non-modbus registers.
        """
        if not registry_map:
            return []

        results: list[tuple[str, str, Any, Any]] = []
        seen_names: set[str] = set()

        for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING, Registry_Type.COIL, Registry_Type.DISCRETE):

            entries: list[registry_map_entry] | None = registry_map.get(registry_type)

            if not entries:
                continue

            for entry in entries:
                if not hasattr(entry, 'variable_name') or not entry.variable_name:
                    continue

                if entry.variable_name in seen_names:
                    continue
                seen_names.add(entry.variable_name)

                results.append((
                    entry.variable_name,
                    getattr(entry, 'data_type', ''),
                    getattr(entry, 'unit_mod', 1.0),   # default to 1.0 — no scaling
                    getattr(entry, 'note', '')
                ))

        # Append transport-declared synthetic fields, skipping any that
        # collide with registry-derived names (registry takes precedence).
        if synthetic_fields:
            for variable_name, data_type, unit_mod, note in synthetic_fields:
                if variable_name in seen_names:
                    self._log.debug(
                        f"_extract_metric_names: synthetic field '{variable_name}' "
                        f"already present in registry map — skipping duplicate"
                    )
                    continue
                seen_names.add(variable_name)
                results.append((variable_name, data_type, unit_mod, note))
                self._log.debug(
                    f"_extract_metric_names: registered synthetic field "
                    f"'{variable_name}' ({data_type}) from transport"
                )

        return sorted(results)

    def _upsert_protocol_registry(self, protocol: str, wide_table_name: str | None, metric_count: int ) -> int:
        """
        Upserts a row into protocol_registry for the given protocol.

        On first insert:
            - Creates the row with rollup_setup_complete = False
            - rollup_prefix derived from wide_table_name if provided

        On subsequent updates:
            - Updates wide_table_name, metric_count, rollup_prefix, updated_at
            - Does NOT reset rollup_setup_complete — a completed setup
            survives restarts

        Returns the protocol_id for use by downstream methods such as
        _ensure_columns_for_metrics.

        Raises:
            ConnectionError: if not connected to TimescaleDB
            RuntimeError: if the upsert executes but returns no protocol_id
        """
        with self._reconnect_lock:
            tsdb_connected: bool = self.tsdb_connected

        if not tsdb_connected:
            msg: str = f"Cannot upsert protocol_registry for '{protocol}' — not connected."
            raise ConnectionError(msg)

        # Derive rollup_prefix from wide_table_name if provided.
        # None for narrow-only protocols (metric_count >= 200).
        # e.g. wide_table_name = "device_metrics_wide__eg4_18kpv"
        #      rollup_prefix   = "rollup_wide__eg4_18kpv"
        rollup_prefix: str | None = None
        if wide_table_name is not None:
            # Strip the "device_metrics_" prefix to get the protocol suffix,
            # then prepend "rollup_" to form the rollup view base name.
            # e.g. "device_metrics_wide__eg4_18kpv"
            #   -> "wide__eg4_18kpv"
            #   -> "rollup_wide__eg4_18kpv"
            suffix = wide_table_name.removeprefix("device_metrics_")
            rollup_prefix = f"rollup_{suffix}"

        with self.SessionFactory() as session:
            try:
                with session.begin():

                    stmt = pg_insert(ProtocolRegistry).values(
                        protocol_name=protocol,
                        wide_table_name=wide_table_name,
                        metric_count=metric_count,
                        rollup_prefix=rollup_prefix,
                        rollup_enabled=True,
                        rollup_setup_complete=False,   # safe default on first insert
                        created_at=_now_tz(),
                        updated_at=_now_tz()
                    ).on_conflict_do_update(
                        index_elements=["protocol_name"],
                        set_={
                            # Update structural fields that may change between restarts
                            # e.g. metric_count grows if CSV is updated
                            "wide_table_name": pg_insert(ProtocolRegistry).excluded.wide_table_name,
                            "metric_count": pg_insert(ProtocolRegistry).excluded.metric_count,
                            "rollup_prefix": pg_insert(ProtocolRegistry).excluded.rollup_prefix,
                            "updated_at": _now_tz(),
                            # rollup_setup_complete is intentionally not updated here —
                            # once True it should only be reset explicitly via
                            # mark_rollup_setup_complete(False) when a schema change
                            # requires rollup views to be rebuilt
                        }
                    ).returning(ProtocolRegistry.protocol_id)

                    protocol_id: int | None = session.execute(stmt).scalar_one_or_none()

            except Exception as e:
                self._log.error(f"_upsert_protocol_registry failed for '{protocol}': {e}")
                raise

            else:

                if protocol_id is None:
                    msg1: str = f"_upsert_protocol_registry: no protocol_id returned for protocol '{protocol}' — upsert may have silently failed."
                    raise RuntimeError(msg1)

                self._log.info(
                    f"Protocol registry upserted: '{protocol}' "
                    f"(id={protocol_id}, table='{wide_table_name}', "
                    f"metrics={metric_count})"
                )
                return protocol_id

    # -------------------------
    #  Dynamically create wide table columns for metrics
    # -------------------------

    def _ensure_columns_for_metrics(self, metric_start_names: List[Tuple[str, str, Any, Any]], table_name: str, protocol: str) -> bool:
        """
        Ensure each metric name as defined in the variable_mask/variable_screen filters has a corresponding column in device_metrics_wide__*,
        and an entry in the metric_catalog table-- which describes the wide table field definitions.
        Due to the memory limits of postgres, no more than 200 metrics as determined in the calling method.
        Using metric_name to map, return clean_column_name in the metric_catalog to create the SQL column in device_metrics_wide__*.

        There could potentially be thousands of metrics, which cannot all be ingested as columns. If over 200 metrics we only save metrics to the
        device_metrics_narrow table, so this method is not needed and is bypassed at the calling method connection stage.
        """

        with self.SessionFactory() as session:

            # Must be connected to tsdb to write metric names
            with self._reconnect_lock:
                tsdb_connected: bool = self.tsdb_connected

            if not tsdb_connected or not session:
                self._log.error("Cannot create columns — not tsdb_connected.")
                raise ConnectionError("Not connected to TimescaleDB.")


            if not metric_start_names:
                self._log.error("No metric column names were detected")
                raise ValueError("No metric column names were detected.")

            try:
                with self._schema_lock:
                    with session.begin():
                        # advisory lock to serialize schema changes
                        self._schema_advisory_lock(session)

                        # lookup the protocol ID from the protocol name
                        protocol_id: int = session.execute(
                            text("SELECT protocol_id FROM protocol_registry WHERE protocol_name = :p"),
                            {"p": protocol}
                        ).scalar_one()

                        # metric name, data_type, unit_mod, note
                        for m, d, u, n in metric_start_names:
                            # 1. Check for existing clean name that has already been mapped.
                            clean_value: Any | None = session.execute(
                                text("SELECT clean_column_name FROM metric_catalog WHERE metric_name = :m  AND protocol_id = :p""" ),
                                {"m": m, "p": protocol_id}
                            ).scalar()

                            d_type: str = self._timescale_type(d,u)

                            if clean_value:
                                # Update the data_type, unit_mod and notes fields in metric_catalog for a matching metric
                                # from the csv files.  All corrections/updates should take place in the CSV or the UI in the webserver.
                                session.execute(
                                    text("""
                                        UPDATE metric_catalog SET data_type = :d, unit_mod = :u, notes = :n
                                        WHERE metric_name = :m AND protocol_id = :p
                                    """),
                                    {"d": d_type, "u": u, "m": m, "n": n, "p": protocol_id}
                                )
                                # _protocol_metric_mappings is used to process raw metrics data for coercion.
                                self._protocol_metric_mappings.setdefault(protocol, {})[m] = (clean_value, d_type)
                                # metric exists so return to the top of the loop.
                                continue

                            # 2. If clean_value was false, clean metric name for safe sql column naming.
                            col: str = self._clean_column_name(m)

                            # check if column name (cleaned name) exists in postgres information_schema
                            exists_wide: Any | None = session.execute(text("""
                                SELECT 1 FROM information_schema.columns
                                WHERE table_name = :tname AND column_name = :col
                            """), {"tname": table_name, "col": col}).scalar()

                            # Add column if missing.  Initial column creation is alphabetic due to sorted metric names.
                            # Per postgres docs, subsequent columns added after first init are always appended to the end of the table.
                            # ie if you want a new column in the middle of the wide table, you must manually delete all tables,
                            # (losing your data) and restart MPG to recreate the table with the new column in the desired location.

                            if not exists_wide:
                                session.execute(text(
                                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col} {d_type};"
                                ))

                            params: dict = {
                                'm': m,         # metric_name
                                'p': protocol_id, # protocol ID
                                'col': col,     # clean_column_name
                                'dtype': d_type or 'double precision', # data_type mapped from registry, with default
                                'umod': u,
                                'col_date': _now_tz(),
                                'n': n          # note from registry
                            }
                            # just in case the if clean_value: check failed.
                            # Take the existing record's field values and overwrite them with the value from the row that failed to insert".
                            session.execute(text("""
                                INSERT INTO metric_catalog (protocol_id, metric_name, clean_column_name, data_type, unit_mod, created_at, notes)
                                VALUES (:p, :m, :col, :dtype, :umod, :col_date, :n)
                                ON CONFLICT (protocol_id, metric_name) DO UPDATE SET
                                    clean_column_name = EXCLUDED.clean_column_name,
                                    data_type = EXCLUDED.data_type,
                                    unit_mod = EXCLUDED.unit_mod,
                                    notes = EXCLUDED.notes
                            """), params)

                            self._protocol_metric_mappings.setdefault(protocol, {})[m] = (col, d_type)

                    self._cache_wide_table_columns(table_name)  # cache existing wide table columns for fast lookup validation during writes
                    self._sync_single_table_schema(table_name)  # resync ORM table after dynamic column changes

                    self._log.info(f"Ensured {len(metric_start_names)} metric columns.")
                    return True

            except Exception as e:
                self._log.error(f"_ensure_columns_for_metrics failed (rolled back): {e}")
                return False

    # advisory lock for schema changes
    def _schema_advisory_lock(self, session: Session, key_text: str = "timescaledb_schema_lock") -> None:
        """ transaction scoped advisory lock based on hash of key_text
            Locks wide table schema in postgres DB to allow dynamic wide table refactor.
        """

        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k));"), {"k": key_text})

    def _timescale_type(self,data_type,unit) -> str:
        dt_name = getattr(data_type, "name", data_type)

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

    # clean column name
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

    # wide table columns helper for write operations.  Per protocol wide table name.
    def _cache_wide_table_columns(self, table_name: str) -> None:
        try:
            insp: Inspector = inspect(self.engine)
            cols = insp.get_columns(table_name, schema='public')
            self._wide_columns_cache[table_name] = {
                col['name'] for col in cols
                if col['name'] not in ("m_time", "device_info_id")
            }
        except Exception as e:
            self._log.error(f"Failed to cache wide columns for {table_name}: {e}")

    # wide table row validation
    def _validate_wide_row(self, row: dict, table_name: str) -> tuple[bool, str | None]:
        # 1. Strip metadata keys instantly to prevent false-positive resyncs
        METADATA_KEYS: set[str] = {"m_time", "device_info_id"}
        row_keys: set[str] = set(row) - METADATA_KEYS

        with self._schema_lock:
            wide_columns: set[str] = self._wide_columns_cache.get(table_name, set())
            if not wide_columns:
                self._log.debug(f"_validate_wide_row: no cache entry for '{table_name}'. Cache keys: {list(self._wide_columns_cache.keys())}")

            extra_keys: set[str] = row_keys - wide_columns
            fewer_keys: set[str] = wide_columns - row_keys
            fewer_keys_count: int = len(fewer_keys)

            if extra_keys:
                self._log.info(f"New metrics detected: {extra_keys}. Triggering resync...")

                # Safe to call because it's an RLock, but it will block other threads during the sync
                self._sync_single_table_schema(table_name)

                # Re-read the cache inside the lock context
                refreshed_cols: set[str] = self._wide_columns_cache.get(table_name, set())
                still_extra: set[str] = row_keys - refreshed_cols

                if still_extra:
                    msg = f"Database schema is still missing columns after resync: {sorted(still_extra)}"
                    self._log.error(msg)
                    raise ValueError(msg)
                return True, None

            elif fewer_keys:
                self._log.warning(f"TimescaleDB Wide-table schema mismatch; missing {fewer_keys_count} keys in scrape data:"
                                  f"{sorted(fewer_keys)} consider deleting {sorted(fewer_keys)} "
                                  f"column from the wide table or adding them to the scrape data.")
                msg: str = f"Missing {len(sorted(fewer_keys))} columns: {sorted(fewer_keys)}"
                return False, msg

            else:
                return True, None


    # resync single wide table schema after dynamic column changes
    def _sync_single_table_schema(self, table_name: str) -> None:
        with self._schema_lock:
            self._log.info(f"Resyncing schema for {table_name}...")

            old_table = Base.metadata.tables.get(table_name)
            if old_table is not None:
                Base.metadata.remove(old_table)

            Table(table_name, Base.metadata, autoload_with=self.engine, extend_existing=True)

            # Update column cache for this specific table
            self._cache_wide_table_columns(table_name)
            self._log.info(f"Schema resync complete for {table_name}")


    def _rediscover_protocols(self) -> None:
        """
        Called during reconnect to rebuild runtime state from protocol_registry.
        Restores _protocol_wide_table_map and retries any incomplete
        rollup setups via RollupManager.add_wide_rollup.

        Separated from _register_protocol_schema because on reconnect the
        physical tables and metric_catalog entries already exist — only the
        in-memory state and any incomplete rollup views need recovery.
        """
        with self.SessionFactory() as session:
            try:
                rows = session.execute(
                    text("""
                        SELECT protocol_name, wide_table_name,
                            rollup_setup_complete
                        FROM protocol_registry
                        WHERE rollup_enabled = true
                        ORDER BY created_at ASC
                    """)
                ).fetchall()

            except SQLAlchemyError as e:
                self._log.error(f"_rediscover_protocols query failed: {e}")
                return

        if not rows:
            self._log.info("_rediscover_protocols: no registered protocols found.")
            return

        for protocol_name, wide_table_name, setup_complete in rows:

            # Restore runtime mapping — this is what lets write_data and
            # _flush_worker route correctly without re-running full schema setup
            self._protocol_wide_table_map[protocol_name] = wide_table_name
            self._registered_protocols.add(protocol_name)

            # Re-reflect the wide table into Base.metadata so the flush worker
            # can look it up by name after reconnect.
            if wide_table_name is not None:
                with self._schema_lock:
                    Table(
                        wide_table_name,
                        Base.metadata,
                        autoload_with=self.engine,
                        extend_existing=True
                    )
                self._cache_wide_table_columns(wide_table_name)

            # Restore per-protocol metric mapping from metric_catalog
            # so _process_raw_metrics can coerce values correctly
            self._restore_metric_mapping(protocol_name)

            self._log.info(f"Rediscovered protocol '{protocol_name}' (table='{wide_table_name}', setup_complete={setup_complete})")

            if not setup_complete and self.rollup_mgr is not None:
                self._log.warning(f"Protocol '{protocol_name}' has incomplete rollup setup — retrying via RollupManager.add_wide_rollup.")
                try:
                    self.rollup_mgr.add_wide_rollup(protocol_name, wide_table_name)
                except Exception as e:
                    self._log.error(f"Recovery failed for protocol '{protocol_name}': {e} — will retry on next startup.")
                    # Non-fatal — protocol data can still flow via narrow table
                    # even if wide table rollups are incomplete

    def _restore_metric_mapping(self, protocol: str) -> None:
        """
        Rebuilds _protocol_metric_mappings[protocol] from metric_catalog
        after a reconnect, so _process_raw_metrics can coerce values
        correctly without re-running _ensure_columns_for_metrics.
        """
        with self.SessionFactory() as session:
            try:
                rows = session.execute(
                    text("""
                        SELECT mc.metric_name, mc.clean_column_name, mc.data_type
                        FROM metric_catalog mc
                        JOIN protocol_registry pr
                            ON mc.protocol_id = pr.protocol_id
                        WHERE pr.protocol_name = :p
                    """),
                    {"p": protocol}
                ).fetchall()

                mapping: dict[str, tuple[str, str]] = {
                    metric_name: (clean_column_name, data_type)
                    for metric_name, clean_column_name, data_type in rows
                }

                self._protocol_metric_mappings[protocol] = mapping

                self._log.debug(f"Restored {len(mapping)} metric mappings for protocol '{protocol}'")

            except SQLAlchemyError as e:
                self._log.error(f"_restore_metric_mapping failed for '{protocol}': {e}")
                raise

    # # 10g
    def _start_flush_thread(self) -> None:
        """
        Launch background thread to process metrics.
        """
        if self._flush_thread.is_alive():
            return
        self._flush_thread.start()
        self._log.debug("Flush thread started.")


    def _register_protocol_schema(
        self,
        protocol: str,
        registry_map: dict[Registry_Type, list[registry_map_entry]],
        synthetic_fields: list[tuple[str, str, Any, Any]] | None = None,
    ) -> None:
        """
        Ensures wide table columns and metric_catalog entries exist for
        all metrics in this protocol's registry map that have been filtered
        in by the variable_mask/variable_screen config, plus any synthetic
        fields declared by the transport via synthetic_fields_metadata.

        Synthetic fields are registered with the correct data type at
        schema-creation time so _validate_wide_row never encounters them
        as unknown extra_keys during the flush worker cycle.

        Each protocol gets its own wide table: device_metrics_wide__{protocol}
        The narrow table is shared across all protocols.
        """
        # Derive the table name for this protocol up front
        # so it can be passed to every downstream method that needs it
        wide_table_name: str = self._safe_table_name(protocol)

        # Extract metric names from this transport's registry map,
        # appending transport-declared synthetic fields
        metric_names: list[tuple[str, str, Any, Any]] = self._extract_metric_names(
            registry_map,
            synthetic_fields=synthetic_fields,
        )
        metric_count: int = len(metric_names)

        if metric_count == 0:
            self._log.error(f"No metrics found for protocol '{protocol}'")
            return

        #  Upsert into protocol_registry — creates the protocol row
        #  and anchors the table name before metric_catalog FKs are written
        self._upsert_protocol_registry(protocol, wide_table_name, metric_count)

        if metric_count >= 200:
            self._log.warning(f"Protocol '{protocol}' has {metric_count} metrics — exceeds 200 column limit, using narrow table only.")
            self._protocol_wide_table_map[protocol] = None
            return

        # 5. Ensure the wide table exists as a physical table in postgres
        #    before _ensure_columns_for_metrics tries to ALTER it
        self._ensure_wide_table_exists(wide_table_name)

        # 6. Create columns and metric_catalog entries —
        #    table_name and protocol are both passed through
        success: bool = self._ensure_columns_for_metrics(metric_names, table_name=wide_table_name, protocol=protocol)

        if not success:
            self._log.error(f"Failed to ensure metric columns for protocol '{protocol}'. Wide table will not be used.")
            self._protocol_wide_table_map[protocol] = None
            return

        self._protocol_wide_table_map[protocol] = wide_table_name
        self._log.info(f"Schema ready for protocol '{protocol}' → table '{wide_table_name}' ({metric_count} metrics)")

        self._init_or_update_rollup_manager(protocol)

    def _init_or_update_rollup_manager(self, protocol) -> None:
        """
        Creates RollupManager on first call, adds new protocol views on
        subsequent calls as more protocols register via init_bridge.
        """
        if not self.rollup_policy.get("enable_rollups", True):
            return

        if self.rollup_mgr is None:
            # First protocol registered — create the manager
            self.rollup_mgr = RollupManager(
                rollup_policy=self.rollup_policy,
                my_session_factory=self.SessionFactory,
                my_engine=self.engine,
                migration_in_progress=self.migration_in_progress,
                log=self._log,
                send_message=self.send_message,
                backlog_lock=self._backlog_lock,
                flush_queue=self._flush_queue,
                backlog=self.backlog,
                reconnect_lock=self._reconnect_lock
            )
            self.rollup_mgr.setup_narrow_rollup()
            # First protocol also needs per-protocol setup.
            # setup_narrow_rollup() only creates shared narrow rollups.
            self.rollup_mgr.add_wide_rollup(
                protocol_name=protocol,
                wide_table_name=self._protocol_wide_table_map[protocol]
            )

            if self.rollup_policy.get("enable_auto_refresh", True):
                self.rollup_mgr.start_auto_refresh()

            # Replay backlog now that schema is confirmed ready.
            # Must be after setup_narrow_rollup releases migration_in_progress.
            try:
                if self.enable_persistent_storage:
                    self.backlog.replay_to_queue()
            except Exception as e:
                self._log.error(f"Backlog replay after schema setup failed: {e}")
        else:
            # Subsequent protocol — add its views to existing manager
            self.rollup_mgr.add_wide_rollup( protocol_name=protocol, wide_table_name=self._protocol_wide_table_map[protocol])

    def _safe_table_name(self, protocol: str) -> str:
        """Converts a protocol name to a safe PostgreSQL table name."""
        safe = re.sub(r'[^a-zA-Z0-9_]', '_', protocol.strip().lower())
        if not re.match(r'^[a-zA-Z_]', safe):
            safe = '_' + safe
        return f"device_metrics_wide__{safe}"[:63]

    # In timescaledb — override init_bridge to register each scraper's protocol
    def init_bridge(self, from_transport: transport_base) -> None:
        """
        Called by MPG after all transports are constructed, once per
        scraper transport wired to this bridge. This is where per-protocol
        schema setup should happen, since we now know which transports
        (and therefore which registry maps) are actually feeding us.
        """
        if not isinstance(from_transport, transport_base):
            return

        if not from_transport.registry_map:
            self._log.debug(
                f"init_bridge: skipping '{from_transport.transport_name}' "
                f"— no registry map (likely a bridge transport)"
            )
            return

        protocol: str = from_transport.protocol_name
        if not protocol:
            return

        if protocol in self._registered_protocols:
            self._log.debug(f"Protocol '{protocol}' already registered, skipping.")
            return

        self._log.info(f"Registering protocol '{protocol}' from transport '{from_transport.transport_name}'")

        try:
            synthetic: list[tuple[str, str, Any, Any]] = getattr(
                from_transport, 'synthetic_fields_metadata', []
            )
            if synthetic:
                self._log.info(
                    f"Transport '{from_transport.transport_name}' declares "
                    f"{len(synthetic)} synthetic field(s) for schema registration: "
                    f"{[s[0] for s in synthetic]}"
                )
            self._register_protocol_schema(
                protocol,
                from_transport.registry_map,
                synthetic_fields=synthetic or None,
            )
            self._registered_protocols.add(protocol)
        except Exception as e:
            self._log.error(f"Failed to register schema for protocol '{protocol}': {e}")

    # using  override transport_base.write_data method

    def write_data(self, data: dict[str, int | float | str ], from_transport: transport_base) -> None:

        if not isinstance(data, dict) or not data:
            return

        protocol: str = from_transport.protocol_name
        device_id: int = self._get_or_create_device(from_transport)

        if device_id is None:
            self._log.error("Could not resolve Device ID. Dropping packet.")
            return

        # Look up the wide table for this transport's protocol
        wide_table_name: str | None = self._protocol_wide_table_map.get(protocol)

        payload: dict[str, Any] = {
            "device_info_id": device_id,
            "metrics": data.copy(),
            "m_time": _now_tz(),
            "transport_name": from_transport.transport_name,
            "protocol": protocol,                    # carry protocol through to flush worker
            "wide_table_name": wide_table_name,      # carry resolved table name
        }

        self._flush_queue.put(payload)

    # Flush worker thread to handle data writes to the database.
    def _flush_worker(self) -> None:
        """Async flush worker created during init.  Handles data appends to tables. Routing to backlog if needed.
            datacopy  -> wide dict of unaltered metrics passed from MPG or backlog
            wide_data  -> wide dict of processed datacopy for safe sql coercion.
            narrow_data  -> dict of appended new_data with deviceid and timestamp.  Needed because narrow table
            applies timestamp to individual metrics.
        """
        # open a session for the lifetime of the flush worker thread to reuse across flushes and improve performance.
        # We can do this because the flush worker is the only thread writing to the database,
        # so we don't have to worry about session concurrency.
        session: Session = self.SessionFactory()
        try:
            while True:

                # 24hr cleanup interval in seconds for stale registry entries based on transport timestamp vs last known timestamp
                # in the registry map for that transport. This is to prevent the registry map from growing indefinitely
                # with old transports that are no longer sending data.
                if time.time() - self._last_cleanup_time > 86400:
                    self._cleanup_stale_registry()
                    self._last_cleanup_time = time.time()

                try:
                    self._flush_event.wait(timeout=0)
                    #************** Drain the queue with a 1s timeout to stay responsive************
                    data_in: dict[str, Any] | None = self._flush_queue.get(block=True)

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
                    device_info_id: int = data_in["device_info_id"]
                    timestamp: datetime = data_in["m_time"]
                    # Backlog replay deserializes datetime as ISO string — convert back.
                    if isinstance(timestamp, str):
                        timestamp = datetime.fromisoformat(timestamp)
                    metrics_only: dict = data_in["metrics"]
                    transport_name: str = data_in["transport_name"]
                    protocol: str = data_in["protocol"]
                    wide_table_name: str = data_in["wide_table_name"]

                    # Check for stale data before attempting to process/write to the database. This is based on the timestamp from the transport
                    # and the previous saved timestamp.

                    is_stale: bool = self._check_is_stale(transport_name, metrics_only, timestamp)

                    if is_stale:
                        self._commit_transport_state(transport_name, metrics_only, timestamp, is_stale=True)
                        self._log.debug("Stale data detected, skipping DB write.")
                        self._flush_queue.task_done()
                        continue

                    # pre-process data to coerce floating point, integer as values (from metric_catalog definitions) for safe insertion to the wide table.
                    # Also applies SQL-safe column renaming. Only process metric values for the wide table path.
                    # The narrow table stores raw key/values as default double precision and is not subject to the same schema
                    # constraints, so we can skip processing for narrow table entries with only metric names safe SQL cleaned for
                    # consistency.

                    wide_data, narrow_data = self._process_raw_metrics(metrics_only, protocol)

                    if not wide_data and not narrow_data:
                        self._log.debug("No data detected, skipping DB write.")
                        self._flush_queue.task_done()
                        continue
                    else:
                        # Add device_info_id and timestamp to wide_data for wide table insertion.  This is needed because the narrow table
                        # applies timestamp and device_info_id to individual rows, whereas the wide table has one row per
                        # timestamp/device with multiple metric columns.

                        valid_row: bool = False
                        msg = None
                        if wide_data and wide_table_name is not None:
                            valid_row, msg = self._validate_wide_row(wide_data, wide_table_name)
                            wide_data = wide_data | {
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
                                # Further process the narrow data with the timestamp and device_info_id for insertion
                                # to the narrow table, by applying the timestamp and device_info_id to each metric/value pair.
                                self._flush_batch_narrow(narrow_data, device_info_id, timestamp, session, transport_name)

                                # Only attempt to write to the wide table if the row is valid and the table name is known.
                                # If the row fails validation, it may indicate a schema mismatch between the incoming data
                                # and the existing wide table columns — in this case we skip the wide table write to prevent data loss,
                                # but still write to the narrow table which is schema-flexible and can accept all incoming data.
                                if wide_table_name is not None:
                                    target_table: Table = Base.metadata.tables[wide_table_name]
                                    stmt: Insert = pg_insert(target_table).values(**wide_data)
                                    session.execute(stmt)
                                    if valid_row:
                                        self._log.info(f"data write complete from [{transport_name}] to timescaledb wide table for {len(metrics_only)} metrics.")
                                    else:
                                        self._log.info(f"Not a complete valid row for the wide table because {msg}, but wrote metrics anyway.")

                            self._commit_transport_state(transport_name, metrics_only, timestamp, is_stale=False)

                    except (SQLAlchemyError, ValueError) as e1:
                        session.rollback()
                        self._log.error(f"metrics data write failed.{e1}")

                        # Only backlog if setting enabled and DB is down
                        with self._reconnect_lock:
                            tsdb_connected: bool = self.tsdb_connected

                        if self.enable_persistent_storage and not tsdb_connected:
                            # Check if we can get the lock
                            acquired: bool = self._backlog_lock.acquire(blocking=False)
                            try:
                                if acquired:
                                    payload: dict[str, Any] = {
                                        "metrics": metrics_only,
                                        "device_info_id": device_info_id,
                                        "m_time": timestamp,
                                        "transport_name": transport_name,
                                        "protocol": protocol,
                                        "wide_table_name": wide_table_name,
                                    }
                                    self.backlog.enqueue(payload)

                            finally:
                                if acquired:
                                    self._backlog_lock.release()

                        # Handle recovery
                        if isinstance(e1, SQLAlchemyError):
                            self._set_tsdb_connected(conn_value = False, conn_reason = "Connection failure")
                            msg = ("Connection failure detected during flush worker write. Data has been "
                            "enqueued to backlog and will be retried when connection is restored.")
                            self._trigger_reconnect()

                    finally:

                        self._flush_queue.task_done()

                        # Clear flush event if queue is empty
                        if self._flush_queue.empty():
                            self._flush_event.clear()
                except Exception as e:
                    self._log.critical(f" Fatal Flush Worker Crash: {e}")
                    self._flush_queue.task_done()

        finally:
            session.close()

    def _process_raw_metrics(self, datacopy: dict, protocol: str) -> tuple[dict, dict]:
        try:
            processed_wide_data: dict = {}
            processed_narrow_data: dict = {}
            metric_mapping: dict[str, tuple[str, str]] = self._protocol_metric_mappings.get(protocol, {})

            for k, v in datacopy.items():
                mapping_info: tuple[str, str] | None = metric_mapping.get(k)

                # 1. Resolve naming and type info
                if mapping_info is not None:
                    clean_key, field_type = mapping_info
                    field_type_upper: str = field_type.upper()
                else:
                    clean_key = k
                    field_type_upper = "UNKNOWN"

                # 2. Prepare Narrow Data
                # We send the raw value so _flush_batch_narrow can sort it into numeric value vs ascii
                processed_narrow_data[clean_key] = v

                # 3. Prepare Wide Data (Apply coercion)
                try:
                    if v is None:
                        processed_wide_data[clean_key] = None
                        continue

                    if field_type_upper in self.INT_TYPES:
                        processed_wide_data[clean_key] = int(float(v))
                    elif field_type_upper in self.FLOAT_TYPES:
                        processed_wide_data[clean_key] = float(v)
                    elif field_type_upper == "BOOLEAN":
                        processed_wide_data[clean_key] = bool(int(v))
                    else:
                        # ascii and unknown types are stored as text in the wide table, with best effort coercion
                        # to string for non-string values.  This is to prevent data loss in the wide table for unexpected types,
                        # while still allowing all values to be stored in the narrow table.
                        processed_wide_data[clean_key] = str(v)

                except (ValueError, TypeError) as e1:
                    self._log.warning(f"Coercion failed for metric '{k}' (Value: {v}). Error: {e1}.")
                    processed_wide_data[clean_key] = v

        except Exception as e2:
            self._log.error(f"Error in _process_raw_metrics {e2}")
            return {}, {}
        else:
            self._log.debug("All metrics processed for wide and narrow paths")
            return processed_wide_data, processed_narrow_data

    def _flush_batch_narrow(self, newData: dict, device_info_id: int, timestamp: datetime, session: Session, transport_name: str) -> None:
        try:
            reading_time: datetime = timestamp
            if isinstance(reading_time, str):
                reading_time = datetime.fromisoformat(reading_time)

            narrow_mappings: list = []
            processed_descriptions: dict = {}

            # Pass 1: Process and harvest all descriptions first
            for key, value in newData.items():
                if key.endswith("_desc"):
                    clean_key = key.removesuffix("_desc")
                    processed_descriptions[clean_key] = value

            # Pass 2: Build the rows and skip the original '_desc' keys
            for key, value in newData.items():
                if key.endswith("_desc"):
                    continue  # Skips this iteration so no row is created or appended

                row = {
                    "m_time": reading_time,
                    "device_info_id": device_info_id,
                    "metric_name": key,
                    "metric_value": 0.0,
                    "metric_ascii": None
                }

                # Numeric/Bool Logic
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    row["metric_value"] = float(value)
                elif isinstance(value, bool):
                    row["metric_value"] = 1.0 if value else 0.0
                # String Logic
                else:
                    row["metric_ascii"] = str(value) if value is not None else None
                    row["metric_value"] = 0.0

                # Upsert the matching description to the ascii field from Pass 1 for the code metric
                if row["metric_name"] in processed_descriptions:
                    row["metric_ascii"] = processed_descriptions[row["metric_name"]]

                narrow_mappings.append(row)

            if narrow_mappings:
                stmt: Insert = pg_insert(DeviceMetricsNarrow).values(narrow_mappings)
                upsert_stmt: Insert = stmt.on_conflict_do_nothing(index_elements=['m_time', 'device_info_id', 'metric_name'])
                session.execute(upsert_stmt)
                self._log.info(f"data write complete from [{transport_name}] to timescaledb narrow table for {len(narrow_mappings)} metrics.")

        except SQLAlchemyError as e:
            self._log.exception(f"Narrow flush failed: {e}")
            try:
                session.rollback()
            except SQLAlchemyError as e2:
                self._log.exception(f"Narrow flush rollback failed: {e2}")

            # === Auto Reconnect handling ===
            self._set_tsdb_connected(conn_value = False, conn_reason = "Connect unsuccessful")
            self._trigger_reconnect()

            raise  # Re-raise to be caught by outer handler for potential backlog queuing


    def _check_is_stale(self, transport_id: str, row: dict, timestamp: datetime) -> bool:
        """_summary_   Stale Data Detection and Handling
            The following methods implement a mechanism to detect when incoming data from a transport has become stale
            (i.e., unchanged for a certain period) and to handle such situations by triggering reconnects and notifications.

        Args:
            transport_id (str):  the unique identifier for the transport whose data is being evaluated for staleness.
            row (dict): the latest data row received from the transport, which is compared against the last received data to determine if it has changed.
            timestamp (datetime):  the timestamp of when the latest data was received, used to calculate how long the data has been unchanged.

        Returns:
            bool: Returns True if the data is considered stale (unchanged for longer than the defined timeout),
                otherwise False.
        """
        state: dict[str, Any] | None = self._stale_registry.get(transport_id)
        if not state or state["last_row"] is None:
            return False

        # Comparison Logic (Numeric + Strict)
        for key, val in row.items():
            prev = state["last_row"].get(key)
            if isinstance(val, (int, float)) and isinstance(prev, (int, float)):
                if not math.isclose(val, prev, rel_tol=1e-4, abs_tol=1e-6):
                    return False
            elif val != prev:
                return False

        # Data is unchanged; check elapsed time
        elapsed = timestamp - state["start_ts"]
        return elapsed > timedelta(seconds=int(self.stale_data_timeout))

    def _commit_transport_state(self, transport_id: str, row: dict, timestamp: datetime, is_stale: bool) -> None:
        """_summary_ The method _commit_transport_state is responsible for tracking the state of each transport's
            data freshness. It maintains a registry (_stale_registry) that records the last received data, the timestamp of when that
            data was received, and whether the data is currently considered stale. When new data arrives, this method
            updates the registry accordingly and triggers events if stale data is detected.

        Args:
            transport_id (str): The unique identifier for the transport whose state is being committed.
            row (dict): The latest data row received from the transport, which is used to compare against previous data for staleness checks.
            timestamp (datetime): The timestamp of when the data was received.
            is_stale (bool): A flag indicating whether the data is considered stale.
        """
        # Initialize if missing — start_ts and stale_event_count begin fresh here
        if transport_id not in self._stale_registry:
            self._stale_registry[transport_id] = {
                "last_row": row.copy(), "start_ts": timestamp, "is_stale": False,
                "last_seen": timestamp, "stale_event_count": 0, "last_event_ts": None
            }

        state: dict[str, Any] = self._stale_registry[transport_id]
        state["last_seen"] = timestamp
        self._log.debug(
            f"Committing state for transport: {transport_id} | "
            f"is_stale: {is_stale} | "
            f"elapsed: {timestamp - state['start_ts']}"
        )

        if not is_stale:
            # Reset everything on fresh data/successful write
            state.update({
                "last_row": row.copy(), "start_ts": timestamp,
                "is_stale": False, "stale_event_count": 0,   # ← counter resets here on recovery
                "last_event_ts": None                        # ← also reset the throttle timer
            })
        elif is_stale and not state["is_stale"]:
            # Only trigger the event ONCE per stale period
            state["is_stale"] = True
            state["last_event_ts"] = timestamp

            elapsed = timestamp - state["start_ts"]
            self._handle_stale_event(transport_id, timestamp, elapsed)

    def _cleanup_orphaned_locks(self) -> None:
        """
        Terminates stale backend connections that are holding locks on our
        tables but have no active query — typically left by a crashed client.

        Runs once at startup after connection is verified, before any schema
        work. Targets only connections to our specific database that are idle
        or idle-in-transaction and have been so for more than 60 seconds,
        excluding our own connection and the TimescaleDB background workers.

        Non-fatal — a failure here is logged and skipped, not raised.
        """
        try:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as conn:

                # Find stale idle connections holding locks on our tables.
                # Excludes our own pid, TimescaleDB workers, and active queries.
                stale = conn.execute(text("""
                    SELECT
                        pid,
                        state,
                        application_name,
                        now() - state_change AS idle_duration,
                        query
                    FROM pg_stat_activity
                    WHERE datname = :dbname
                    AND pid <> pg_backend_pid()
                    AND application_name NOT LIKE 'TimescaleDB%'
                    AND state IN ('idle in transaction', 'idle in transaction (aborted)')
                    AND now() - state_change > INTERVAL '60 seconds'
                """), {"dbname": self.database}).fetchall()

                if not stale:
                    self._log.debug("No orphaned backend connections found.")
                    return

                for pid, state, app_name, idle_duration, query in stale:
                    self._log.warning(
                        f"Terminating orphaned connection: pid={pid}, "
                        f"state='{state}', app='{app_name}', "
                        f"idle={idle_duration}, query='{str(query)[:80]}'"
                    )
                    try:
                        conn.execute(
                            text("SELECT pg_terminate_backend(:pid);"),
                            {"pid": pid}
                        )
                    except Exception as e:
                        self._log.error(
                            f"Failed to terminate pid {pid}: {e}"
                        )
                        # Non-fatal — continue with remaining connections

                self._log.info(f"Orphaned connection cleanup complete: {len(stale)} connection(s) terminated.")

        except Exception as e:
            self._log.error(f"_cleanup_orphaned_locks failed: {e}")
            # Non-fatal — proceed with startup even if cleanup fails

    def _cleanup_stale_registry(self) -> None:
        """Removes transports that haven't been seen in 7 days."""
        now: datetime = _now_tz()
        to_delete: List[str] = [
            tid for tid, s in self._stale_registry.items()
            if (now - s["last_seen"]).days >= 7
        ]
        for tid in to_delete:
            self._log.info(f"Decommissioning stale state for transport: {tid}")
            del self._stale_registry[tid]

    def _handle_stale_event(self, transport_id: str, current_time: datetime, total_stale_elapsed: timedelta) -> None:
        """
        Triggers a reconnect for a specific transport (max X times) with a gap between attempts.
        """
        state = self._stale_registry.get(transport_id)
        if not state:
            return

        # 1. Check max attempts for this transport
        if state["stale_event_count"] >= self.max_stale_attempts:
            self._log.debug(f"[{transport_id}] Max stale retry attempts reached. No further reconnects.")
            return

        # 2. Throttling: Has enough time passed since this transport's last attempt?
        if state["last_event_ts"] is not None:
            time_since_last = current_time - state["last_event_ts"]
            if time_since_last < timedelta(minutes=int(self.retry_delay_mins)):
                return

        # 3. Increment counters
        state["stale_event_count"] += 1
        state["last_event_ts"] = current_time

        # 4. Trigger Reconnect with Transport ID
        if self.request_upstream_reconnect:
            try:
                self._log.warning(f"[{transport_id}] Data stale. Requesting reconnect (Attempt {state['stale_event_count']}/{self.max_stale_attempts}).")
                # Pass the transport ID here
                self.request_upstream_reconnect(transport_id)
            except Exception:
                self._log.exception(f"[{transport_id}] Failed requesting upstream reconnect.")

        # 5. Notify
        try:
            minutes: float = total_stale_elapsed.total_seconds() / 60
            msg: str = (f"Transport [{transport_id}] stale for {minutes:.1f} mins. "
                f"Attempt {state['stale_event_count']} of {self.max_stale_attempts}.")
            self.send_message(message=msg,title="MPG Stale Event Alert", priority=1)
        except Exception:
            self._log.exception(f"[{transport_id}] Failed sending MPG notification.")


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
            self._connection_manager.unregister()
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
class SendMessageProtocol(Protocol):
    def __call__(
        self,
        message: str,
        title: str = "",
        priority: int = 0,
        services: Optional[Union[list[str], str]] = None,
        **kwargs
    ) -> None:
        ...
class BacklogManager:
    """
    Manages persistent backlog storage and replay for TimescaleDB writes.

    Owns:
    - in-memory backlog list
    - disk persistence (sqlite)
    - backlog synchronization
    - replay into the flush queue

    Does NOT:
    - talk to the database
    - manage sessions
    - manage reconnect logic

    Design: dual in-memory / SQLite storage.
    ----------------------------------------
    The in-memory list (backlog_points) is the fast working set. enqueue() and replay_to_queue()
    operate on it directly; SQLite is updated as a side effect and acts purely as a crash-recovery
    journal. If the process dies mid-outage, the points survive on disk and are reloaded on startup
    via load_from_disk(). The dual strategy is retained because many inverters may be connected
    through a single timescaledb instance, making the in-memory working set important for throughput
    during an outage when enqueue() may be called at high frequency.

    Atomicity contract:
    - enqueue():         SQLite write first -> memory update only if disk succeeds.
    - overflow eviction: SQLite DELETE first -> memory pop only if disk succeeds.
    - replay_to_queue(): memory cleared, then SQLite wiped via _clear_disk().
    This ordering ensures SQLite is always the authoritative record on crash. At worst a point
    exists on disk but not in memory (recovered on next load_from_disk()), never the reverse.

    rowid tracking:
    A parallel list (_backlog_rowids) stores the SQLite rowid for each entry in backlog_points
    at the same index. This enables O(1) single-row deletes on overflow, avoiding a full table
    rewrite (_sync_to_disk) on every eviction — important at scale with many connected inverters.
    """

    def __init__(
        self,
        backlog_file_path: Path,
        max_backlog_age: int,
        max_backlog_size: int,
        send_message: SendMessageProtocol,
        flush_queue: queue.Queue,
        flush_event: threading.Event,
        backlog_lock: threading.RLock,
        log: logging.Logger
    ) -> None:

        self.backlog_file_path: Path = backlog_file_path
        self.max_backlog_age: int = max_backlog_age
        self.max_backlog_size: int = max_backlog_size
        self.send_message: SendMessageProtocol = send_message
        self._flush_queue: queue.Queue = flush_queue
        self._flush_event: threading.Event = flush_event
        self._backlog_lock: threading.RLock = backlog_lock
        self._log: logging.Logger = log

        self.backlog_points: list[dict] = []
        self._backlog_rowids: list[int] = []    # parallel to backlog_points: rowid[i] belongs to backlog_points[i]

    # -------------------------
    # Load Persistent backlog
    # -------------------------

    def _db_connect(self) -> sqlite3.Connection:
        """Open (and initialise if needed) the SQLite backlog database."""
        self.backlog_file_path.parent.mkdir(parents=True, exist_ok=True)
        con: sqlite3.Connection = sqlite3.connect(str(self.backlog_file_path))
        con.execute(
            """CREATE TABLE IF NOT EXISTS backlog (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                m_time  TEXT    NOT NULL,
                payload TEXT    NOT NULL
            )"""
        )
        con.commit()
        return con

    def load_from_disk(self) -> None:
        """
        Load backlog points from SQLite into memory, applying age-based expiration and removing
        corrupted entries. Valid points populate backlog_points and _backlog_rowids together so
        subsequent enqueue/eviction operations can address rows directly by rowid.
        """
        if not self.backlog_file_path:
            return

        cutoff: datetime = _now_tz() - timedelta(seconds=int(self.max_backlog_age))
        loaded_points: list[dict] = []
        loaded_rowids: list[int] = []
        expired_ids: list[int] = []

        try:
            with self._backlog_lock:
                con: sqlite3.Connection = self._db_connect()
                try:
                    rows: List[Any] = con.execute(
                        "SELECT id, m_time, payload FROM backlog ORDER BY id"
                    ).fetchall()

                    for row_id, m_time_str, payload_str in rows:
                        try:
                            m_time: datetime = datetime.fromisoformat(m_time_str)
                            if m_time < cutoff:
                                expired_ids.append(row_id)
                                continue
                            point: dict[str, Any] = json.loads(payload_str)
                            loaded_points.append(point)
                            loaded_rowids.append(row_id)
                        except (json.JSONDecodeError, ValueError) as e:
                            self._log.info("Skipping corrupted backlog row %s: %s", row_id, e)
                            expired_ids.append(row_id)

                    if expired_ids:
                        # Use a static SQL query with a single placeholder
                        # Format the IDs as a list of single-item tuples: [(1,), (2,), (3,)]
                        id_parameters: List[Tuple[int]] = [(row_id,) for row_id in expired_ids]

                        con.executemany(
                            "DELETE FROM backlog WHERE id = ?",
                            id_parameters
                        )
                        con.commit()
                finally:
                    con.close()

                self.backlog_points = loaded_points
                self._backlog_rowids = loaded_rowids

            self._log.info("Loaded %d points from disk", len(loaded_points))

        except Exception as e:
            self._log.exception(f"Failed to process backlog file: {e}")
            self.send_message(
                message="Failed to load backlog from disk. Check logs for details.",
                title="MPG Backlog Load Error",
                priority=1
            )

    def enqueue(self, point: dict) -> None:
        """
        Add a point to the backlog. SQLite is always written before memory is updated so that
        a crash mid-call leaves SQLite as the authoritative record.

        On overflow, the oldest row is deleted from SQLite first, then removed from memory.
        The new point is then inserted into SQLite first, then appended to memory. If either
        disk operation fails the in-memory state is not modified and the exception propagates.

            example point dict:
            {
                "device_info_id": 3,                          # int — FK into device_info table
                "m_time": datetime(2026, 6, 2, 14, 23, 11,    # datetime (or ISO string after SQLite round-trip)
                                tzinfo=tzlocal()),
                "transport_name": "eg4_18kpv_inverter_1",     # str — identifies the source inverter
                "protocol":       "eg4_18kpv",                # str — used to route to the right wide table
                "wide_table_name": "wide__eg4_18kpv",         # str | None — None means narrow-only protocol

                "metrics": {                                  # dict — the raw readings from that poll cycle
                    "battery_voltage":   52.4,
                    "battery_soc":       87,
                    "pv_power":          3120,
                    "grid_frequency":    60.01,
                    "output_power":      2800,
                    "inverter_temp":     42.5,
                    # ... one key/value per register in the protocol registry
                }
}
        """
        if isinstance(point, list):
            raise TypeError("enqueue() does not accept lists, only single dict or None.")

        with self._backlog_lock:
            if len(self.backlog_points) >= self.max_backlog_size:
                self._log.warning(f"Max backlog size ({self.max_backlog_size}) reached. Dropping oldest point.")
                # Delete oldest from SQLite first — memory is unchanged until disk succeeds.
                oldest_rowid: int = self._backlog_rowids[0]
                self._delete_row_from_disk(oldest_rowid)
                # Disk succeeded — safe to evict from memory.
                self.backlog_points.pop(0)
                self._backlog_rowids.pop(0)

            # Insert new point into SQLite first — memory is unchanged until disk succeeds.
            new_rowid: int = self._append_to_disk(point)
            # Disk succeeded — safe to append to memory.
            self.backlog_points.append(point)
            self._backlog_rowids.append(new_rowid)

    def replay_to_queue(self) -> int:
        """
        Transfer all backlog points to the flush queue, then clear both memory and disk.
        Returns the number of points replayed.
        """
        with self._backlog_lock:
            if not self.backlog_points:
                return 0
            count: int = len(self.backlog_points)
            self._log.debug(f"Replaying {count} points to flush queue.")
            for payload in self.backlog_points:
                self._flush_queue.put(payload)
            # Clear memory first — the flush queue holds the authoritative copy during the
            # brief window before _clear_disk() completes.
            self.backlog_points.clear()
            self._backlog_rowids.clear()
            self._clear_disk()
            self.send_message(
                message=f"Replayed {count} backlog points to flush queue.",
                title="MPG Backlog Sent to Queue",
                priority=1
            )
            self._log.info(f"Backlog replay complete: {count} points sent to flush queue.")
        return count

    # -------------------------
    # Disk helpers
    # -------------------------

    def _append_to_disk(self, point: dict) -> int:
        """
        Insert a single point into SQLite. Returns the new rowid.
        Raises on failure — caller must not update memory if this raises.
        """
        if not self.backlog_file_path:
            return -1
        m_time_str: str = str(point.get("m_time", _now_tz().isoformat()))
        payload_str: str = json.dumps(point, default=str)

        try:
            con: sqlite3.Connection = self._db_connect()
            try:
                cur: sqlite3.Cursor = con.execute(
                    "INSERT INTO backlog (m_time, payload) VALUES (?, ?)",
                    (m_time_str, payload_str)
                )
                con.commit()
                if cur.lastrowid is None:
                    raise RuntimeError("Failed to retrieve rowid after insert.")
                return cur.lastrowid
            finally:
                con.close()
        except Exception as e:
            self._log.error(f"Failed to append point to backlog disk: {e}")
            self.send_message(
                message="Failed to write point to backlog on disk. Check logs for details.",
                title="MPG Backlog Write Error",
                priority=1
            )
            raise

    def _delete_row_from_disk(self, rowid: int) -> None:
        """
        Delete a single row from SQLite by its rowid.
        Raises on failure — caller must not update memory if this raises.
        """
        if not self.backlog_file_path:
            return
        try:
            con: sqlite3.Connection = self._db_connect()
            try:
                con.execute("DELETE FROM backlog WHERE id = ?", (rowid,))
                con.commit()
            finally:
                con.close()
        except Exception as e:
            self._log.error(f"Failed to delete row {rowid} from backlog disk: {e}")
            self.send_message(
                message="Failed to delete oldest point from backlog on disk. Check logs for details.",
                title="MPG Backlog Delete Error",
                priority=1
            )
            raise

    def _clear_disk(self) -> None:
        """
        Delete all rows from the SQLite backlog table. Called after replay_to_queue()
        once memory has already been cleared.
        """
        if not self.backlog_file_path:
            return
        try:
            con: sqlite3.Connection = self._db_connect()
            try:
                con.execute("DELETE FROM backlog")
                con.commit()
            finally:
                con.close()
        except Exception as e:
            self._log.error(f"Failed to clear backlog from disk: {e}")
            self.send_message(
                message="Failed to clear backlog on disk. Check logs for details.",
                title="MPG Backlog Clear Error",
                priority=1
            )
            raise
class RollupManager:
    """ summary logic
        The RollupManger class creates the structures that enable TimescaleDB rollup views.
        After all structures are built:
        1 RollupManager wakes up.
        2 RollupManager tells BacklogManager to put everything into the _flush_queue if backlog data exists.
            The _flush_queue is the threaded queue object that accepts MPG data obtained from the source transport.
        3 RollupManager calls _flush_queue.join() (it pauses here).
        4 _flush_worker finishes writing everything to the Hypertable and calls task_done() for each.
        5 RollupManager resumes and calls refresh_continuous_aggregate.

        Backlog Safety: The _refresh_rollup_loop wraps replay_to_queue() in the _backlog_lock and waits for completion via .join().
        Attribute Persistence: All granular intervals (e.g., hourly_compress_after_interval) are mapped from the rollup_policy in __init__.
        Live State: tsdb_connected and current_metric_count are implemented as @property to track the timescaledb class state in real-time.
        SQL Execution: The SET LOCAL work_mem uses the single-quote fix to prevent f-string placeholder errors.
    """

    # This dataclass is used to describe the SQL expressions needed to create rollup continuous aggregates for a given metric.
    @dataclass(frozen=True)
    class RollupMetricDescriptor:
        """
        Describe one logical metric stream for generic rollup SQL generation.

        Narrow rollups use a single descriptor keyed by `metric_name`.
        Wide rollups use one descriptor per protocol-specific metric column.
        """

        group_key_sql: str | None
        raw_value_sql: str
        rolled_min_sql: str
        rolled_max_sql: str
        rolled_summary_sql: str
        min_alias: str
        max_alias: str
        summary_alias: str

    def __init__(
        self,
        rollup_policy: dict,
        my_session_factory: sessionmaker[Session],
        my_engine: Engine,
        migration_in_progress: threading.Event,
        log: logging.Logger,
        send_message:  SendMessageProtocol,
        backlog_lock: threading.RLock,
        flush_queue: queue.Queue,
        backlog: BacklogManager,
        reconnect_lock: threading.RLock
        ) -> None:

        self.rollup_policy: dict = rollup_policy
        self.SessionFactory: sessionmaker[Session] = my_session_factory
        self.engine: Engine = my_engine
        self.migration_in_progress: threading.Event = migration_in_progress
        self._log: logging.Logger = log
        self.send_message:  SendMessageProtocol = send_message
        self._backlog_lock: threading.RLock = backlog_lock
        self._flush_queue: queue.Queue  = flush_queue
        self.backlog: 'BacklogManager'  = backlog
        self._reconnect_lock: threading.RLock = reconnect_lock

        self._refresh_rollup_thread = threading.Thread(target=self._refresh_rollup_loop_helper, daemon=True, name="RollupAutoRefreshThread")
        self._stop_refresh_rollup_event: threading.Event = getattr(self, "_stop_refresh_rollup_event", threading.Event())

        # Performance tiers for rollup refreshes, mapped from rollup_policy or default values.
        # These settings control the computer's resource allocation and batch sizes for refreshing rollups at different granularities,
        # allowing for optimized performance based on the expected workload and data volume of each tier.
        self.performance_tiers: dict[str, dict[str, Any]] = {
        "tier_low":    {"count": 50,  "work_mem": "32MB",  "lock_timeout": "10s", "flush_batch_size": 10},
        "tier_medium": {"count": 100, "work_mem": "64MB",  "lock_timeout": "15s", "flush_batch_size": 20},
        "tier_high":   {"count": 200, "work_mem": "128MB", "lock_timeout": "30s", "flush_batch_size": 40},
        }

        # Tracks metric_count per protocol, keyed by protocol_name.
        # Populated in add_wide_rollup from protocol_registry.metric_count.
        # Used by _get_dynamic_settings to determine tier without a DB hit.
        self._protocol_wide_column_counts: dict[str, int] = {}

        """
        Rollup and Compression Timing Defaults comments only:
        Rollup Type
            refresh rollup    start_offset   compress_after   reason
            1 Hour	          3 hours	     2 days	          Allows a 3-hour window for late data before locking it via compression.
            1 Day	          3 days	     2 weeks	      Ensures daily rollups are finalized before compressing.
            1 Week	          3 weeks	     2 months	      Larger window helps capture any delayed source data updates.
            1 Month	          3 months	     6 months	      Maximum safety for long-term historical accuracy.
        """

        # hypertable defaults. These are not configurable by the user but can be overridden by changing the below rollup_policy if needed.
        # These settings are critical for ensuring that the hypertable is created with the appropriate chunking and compression
        # settings to optimize performance and storage efficiency for time-series data.
        self.hypertable_defaults: dict[str, str] = {
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

        # rollup defaults, continuous aggregate bucket sizes.
        self.rollup_defaults: dict[str, str] = {
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
        self.machine_timezone: str = self.rollup_policy.get("machine_timezone", "UTC")
        self.auto_refresh_interval: int = self.rollup_policy.get("auto_refresh_interval",21600)
        self.enable_auto_refresh:bool = self.rollup_policy.get("enable_auto_refresh", True)
        self.enable_rollups = bool(self.rollup_policy.get("enable_rollups", True))
        self.drop_after: str = self.rollup_policy.get("drop_after", "1 year")
        self.migrate_data = bool(self.rollup_policy.get("migrate_data",True))
        self.enable_compression = bool(self.rollup_policy.get("enable_compression",True))

        # Compression settings
        self.compress_segmentby_narrow: str= self.hypertable_defaults["compress_segmentby_narrow"]
        self.compress_segmentby_wide: str= self.hypertable_defaults["compress_segmentby_wide"]
        self.compress_orderby: str= self.hypertable_defaults["compress_orderby"]
        self.time_column: str= self.hypertable_defaults["time_column"]

        self.hourly_chunk_time_interval: str = self.hypertable_defaults["hourly_chunk_time_interval"]
        self.daily_chunk_time_interval: str = self.hypertable_defaults["daily_chunk_time_interval"]
        self.weekly_chunk_time_interval: str = self.hypertable_defaults["weekly_chunk_time_interval"]
        self.monthly_chunk_time_interval: str = self.hypertable_defaults["monthly_chunk_time_interval"]

        self.hourly_compress_after_interval: str = self.hypertable_defaults["hourly_compress_after_interval"]
        self.daily_compress_after_interval: str = self.hypertable_defaults["daily_compress_after_interval"]
        self.weekly_compress_after_interval: str = self.hypertable_defaults["weekly_compress_after_interval"]
        self.monthly_compress_after_interval: str = self.hypertable_defaults["monthly_compress_after_interval"]

        # Rollup view settings
        self.anchor_start_time_utc: str = self.rollup_defaults["anchor_start_time_utc"]

        self.hourly_rollup_bucket: str = self.rollup_defaults["hourly_rollup_bucket"]
        self.daily_rollup_bucket: str = self.rollup_defaults["daily_rollup_bucket"]
        self.weekly_rollup_bucket: str = self.rollup_defaults["weekly_rollup_bucket"]
        self.monthly_rollup_bucket: str = self.rollup_defaults["monthly_rollup_bucket"]

        self.hourly_rollup_start: str = self.rollup_defaults["hourly_rollup_start"]
        self.daily_rollup_start: str = self.rollup_defaults["daily_rollup_start"]
        self.weekly_rollup_start: str = self.rollup_defaults["weekly_rollup_start"]
        self.monthly_rollup_start: str = self.rollup_defaults["monthly_rollup_start"]

        # Tracks whether the shared narrow CAGG views have been created.
        # Narrow rollup views (hourly/daily/weekly/monthly_rollup_narrow) are shared
        # across all protocols, so they only need to be built once.
        self._narrow_rollups_created: bool = False

        # Registry of all CAGG view names this manager is responsible for refreshing.
        # Populated by add_wide_rollup and _ensure_narrow_cagg_views.
        # Keyed as view_name -> start_offset string, preserving insertion order
        # so the refresh loop always runs hourly -> daily -> weekly -> monthly.
        self._known_rollup_views: dict[str, str] = {}

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


    def setup_narrow_rollup(self) -> None:
        """
        Build the shared narrow-table post-creation stack.

        This method preserves the original narrow bootstrap behavior while
        routing the work through a single lifecycle helper so shared and
        protocol-specific paths use the same orchestration shape.
        """
        self.setup_rollup(
            table_name="device_metrics_narrow",
            segment_by=self.compress_segmentby_narrow,
            compression_policy_intervals=[
                self.hourly_chunk_time_interval,
                self.daily_chunk_time_interval,
                self.weekly_chunk_time_interval,
                self.monthly_chunk_time_interval,
            ],
            protocol_name=None,
            wide_table_name=None,
            use_shared_rollup_flow=True,
        )

    def setup_rollup(
        self,
        table_name: str,
        segment_by: str,
        compression_policy_intervals: list[str],
        protocol_name: str | None,
        wide_table_name: str | None,
        use_shared_rollup_flow: bool,
    ) -> None:
        """
        Run the common post-table-processing lifecycle for both narrow and wide sources.

        The shared narrow stack and protocol wide stack both apply the same
        major categories of work: hypertable setup, compression setup,
        retention policy, rollup creation, and initial refresh. This helper
        centralizes that lifecycle while still allowing narrow-specific and
        wide-specific rollup creation to diverge where required.
        """
        # 1 Hypertable & Policies
        try:
            self._ensure_hypertables([table_name])
            self._log.info("Hypertable check/creation complete")
        except Exception as e:
            self._log.error(f"Hypertable creation failed: {e}")

        # 2 Enable compression (if configured)
        try:
            if self.enable_compression:
                self._configure_compression(
                    table_name=table_name,
                    segment_by=segment_by,
                    compression_policy_intervals=compression_policy_intervals,
                )
        except Exception as e:
            self._log.error(f"Enable compression failed: {e}")

        # 3 Add retention policy
        try:
            self._apply_retention_policy(table_name)
        except Exception as e:
            self._log.error(f"Add retention policy failed: {e}")

        # 4 Setup continuous aggregate rollups
        if self.enable_rollups:
            if use_shared_rollup_flow:
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
            elif protocol_name is not None:
                # 4a
                try:
                    self._ensure_cagg_views_for_protocol(protocol_name, wide_table_name)
                except Exception as e:
                    self._log.error(f"CAGG view creation failed for protocol '{protocol_name}': {e}")

                # 4b
                try:
                    self._refresh_protocol_rollups_helper(protocol_name, wide_table_name)
                except Exception as e:
                    self._log.error(
                        f"Initial rollup refresh failed for '{protocol_name}': {e}"
                        f" — views exist but data may not be pre-aggregated."
                    )

    def _ensure_hypertables(self, tables: list[str]) -> None:
        """
        Ensure the provided source tables are TimescaleDB hypertables.

        The helper preserves the original `create_hypertable(... if_not_exists)`
        behavior while letting both narrow and wide orchestration paths pass in
        the tables they need processed.
        """
        params: dict[str, Any] = {
            "time_col": getattr(self, "time_column", "m_time"),
            "if_exists": getattr(self, "if_not_exists", True),
            "migrate": getattr(self, "migrate_data", True),
        }

        try:
            with self.SessionFactory() as session:
                with session.begin():
                    for table in tables:
                        session.execute(
                            text(
                                f"""
                                SELECT create_hypertable(
                                    '{table}',
                                    :time_col,
                                    if_not_exists => :if_exists,
                                    migrate_data => :migrate
                                )
                                """
                            ),
                            {
                                **params,
                            },
                        )
                self._log.debug(f"Hypertable creation ensured for: {', '.join(tables)}")

        except SQLAlchemyError as e:
            self._log.error("Failed to ensure hypertables: %s", e)
            raise

    def _configure_compression(
        self,
        table_name: str,
        segment_by: str,
        compression_policy_intervals: list[str],
    ) -> None:
        """
        Apply compression settings and compression policies to one source table.

        The table structure is immutable after creation, but policy scheduling
        remains configurable. This helper keeps those concerns together for
        both shared narrow and protocol-specific wide sources.
        """
        with self.SessionFactory() as session:
            self._log.info("Setting up compression policy")
            if not session:
                self._log.error("Cannot set up compression — not tsdb_connected.")
                return

            self._apply_compression_helper(session, table_name, segment_by)

            with session.begin():
                for interval in compression_policy_intervals:
                    self.ensure_compression_policy(table_name, interval)

                session.commit()

    def _apply_retention_policy(self, table_name: str) -> None:
        """
        Apply the configured retention policy to one source table.

        This preserves the previous remove-and-readd behavior so changes to the
        retention interval still take effect on restart.
        """
        with self.SessionFactory() as session:
            if not session:
                self._log.error("Cannot add retention policies — not tsdb_connected.")
                return

            try:
                # Remove and re-add policy to ensure interval updates apply
                session.execute(text(f"SELECT remove_retention_policy('{table_name}', if_exists => True);"))
                session.execute(text(f"SELECT add_retention_policy('{table_name}', INTERVAL '{self.drop_after}');"))

                session.commit()
                self._log.debug(f"Retention policy updated for {table_name}")

            except SQLAlchemyError as e:
                self._log.error(f"Error updating retention policy for {table_name}: {e}")
                try:
                    session.rollback()
                except SQLAlchemyError as e2:
                    self._log.error(f"Rollback failed for {table_name}: {e2}")
                raise


    # 5 Start the rollup thread.  Called from TimescaleDB class upon connection to the database.
    def start_auto_refresh(self) -> None:

        if self._refresh_rollup_thread.is_alive():
            self._log.debug("Auto refresh thread already running.")
            return
        else:
            # start _refresh_rollup_thread after connect completes successfully
            self._refresh_rollup_thread.start()
            self._log.debug("Auto rollup refresh thread started.")


    def _apply_compression_helper(self, session: Session, table_name: str, segment_by: str) -> None:
        """Apply compression ALTER TABLE settings to one source table."""
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
                contexts: list[dict[str, Any]] = [
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

                view_configs: list[tuple[str, str, str, str]] = [
                    ("hourly_rollup", self.hourly_rollup_bucket, self.hourly_rollup_start, self.hourly_chunk_time_interval),
                    ("daily_rollup", self.daily_rollup_bucket, self.daily_rollup_start, self.daily_chunk_time_interval),
                    ("weekly_rollup", self.weekly_rollup_bucket, self.weekly_rollup_start, self.weekly_chunk_time_interval),
                    ("monthly_rollup", self.monthly_rollup_bucket, self.monthly_rollup_start, self.monthly_chunk_time_interval),
                ]

                # 2. Scan Phase: Detect if any bucket change exists across the whole stack
                # 2. Scan Phase: Detect whether the stack is missing, mismatched, or already valid
                any_rebuild_needed: bool = False
                any_missing_views: bool = False

                for context in contexts:
                    for view_key, bucket, _, _ in view_configs:
                        view_name: str = context["segments"][view_key]
                        rollup_state: str = self.rollup_needs_rebuild(session, view_name, bucket)

                        if rollup_state == "rebuild":
                            any_rebuild_needed = True
                            break

                        if rollup_state == "missing":
                            any_missing_views = True

                    if any_rebuild_needed:
                        break

                # 3. Purge Phase: Only purge when an existing rollup stack is mismatched
                if any_rebuild_needed:
                    self._log.info("Bucket change detected. Purging all rollups for clean rebuild.")
                    # This method must drop Weekly -> Daily -> Hourly with sequential commits
                    self._drop_all_continuous_aggregates(session)
                    # Purge orphaned scheduler jobs
                    self._purge_ghost_jobs_helper(session)
                elif any_missing_views:
                    self._log.info("Missing rollups detected. Creating required views without purge.")


                # 4. Creation Phase: Build/Verify Bottom-Up (Hourly -> Daily -> Weekly)
                for context in contexts:
                    source_table: str = context["table_name"]
                    current_source: str = source_table  # Reset source for each context (Narrow vs Wide)
                    rollup_segments: dict[str, str] = context["segments"]

                    for view_key, bucket, start_offset, chunk_time_interval in view_configs:
                        # gran: str = view_key.split("_")[0]  # "hourly", "daily", etc.
                        view_name = rollup_segments[view_key]

                        # Create if missing and register into _known_rollup_views
                        # regardless, so the refresh loop picks it up.
                        self._ensure_single_cagg_view_helper(
                            session=session,
                            view_name=view_name,
                            source_table=current_source,
                            bucket_interval=bucket,
                            start_offset=start_offset,
                            protocol_name="shared_narrow"
                        )

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


    def _create_rollup_helper(self, session: Session, source: str, view_name: str, bucket_interval: str, start_offset: str, base_wide_table_name: str | None = None) -> None:
        """
        Create one continuous aggregate view using the shared narrow/wide SQL pipeline.

        Narrow and wide rollups follow the same structural flow:
        time bucket, device grouping, metric dimension, and aggregate fields.
        The real variation is the metric dimension itself, so this method builds
        one generic SELECT using typed metric descriptors and then applies the
        standard view policies.
        """
        r_settings: dict[str, Any] = self._get_dynamic_settings_helper()

        if not session:
            self._log.error("Cannot create rollup — not connected.")
            return

        # Set local lock timeout to fail fast if blocked by flush thread.  Set dynamically from settings.
        session.execute(text(f"SET LOCAL lock_timeout = '{r_settings['lock_timeout']}';"))

        # Determine Aggregation Mode
        # If source is another view, we MUST use rollup(). If it's a hypertable, use stats_agg().
        reading_from_raw: bool = (source == "device_metrics_narrow" or source.startswith("device_metrics_wide__"))

        try:
            descriptors: list[RollupManager.RollupMetricDescriptor] = self._resolve_rollup_metric_descriptors_helper(
                session=session,
                source=source,
                base_wide_table_name=base_wide_table_name,
            )

            select_clauses: list[str] = [
                f"time_bucket(INTERVAL '{bucket_interval}', m_time, '{self.machine_timezone}') AS m_time",
                "device_info_id",
            ]
            group_by_positions: list[str] = ["1", "2"]

            for descriptor in descriptors:
                if descriptor.group_key_sql is not None:
                    select_clauses.append(descriptor.group_key_sql)
                    group_by_positions.append(str(len(group_by_positions) + 1))

                if reading_from_raw:
                    select_clauses.append(f"MIN({descriptor.raw_value_sql}) AS {descriptor.min_alias}")
                    select_clauses.append(f"MAX({descriptor.raw_value_sql}) AS {descriptor.max_alias}")
                    select_clauses.append(
                        f"stats_agg({descriptor.raw_value_sql}) AS {descriptor.summary_alias}"
                    )
                else:
                    select_clauses.append(f"MIN({descriptor.rolled_min_sql}) AS {descriptor.min_alias}")
                    select_clauses.append(f"MAX({descriptor.rolled_max_sql}) AS {descriptor.max_alias}")
                    select_clauses.append(
                        f"rollup({descriptor.rolled_summary_sql}) AS {descriptor.summary_alias}"
                    )

            session.execute(text(f"""
                CREATE MATERIALIZED VIEW {view_name}
                WITH (timescaledb.continuous = true) AS
                SELECT
                    {', '.join(select_clauses)}
                FROM {source}
                GROUP BY {', '.join(group_by_positions)}
                WITH NO DATA;
            """))  # noqa: S608


            # Apply Policies & Index
            self._add_aggregate_policy_helper(session, view_name, bucket_interval, start_offset)


            # Finalize the view so it is available as a 'source' for the next view in the loop
            session.commit()
            self._log.info(f"Successfully created hierarchical rollup: {view_name}")

        except Exception as e:
            session.rollback()
            self._log.error(f"Failed to create {view_name}: {e}")
            raise

    def _add_aggregate_policy_helper(self, session: Session, view_name: str, bucket_interval: str, start_offset: str) -> None:
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


    def _resolve_rollup_metric_descriptors_helper(self, session: Session, source: str, base_wide_table_name: str | None,) -> list[RollupMetricDescriptor]:
        """
        Resolve the metric dimension for generic rollup SQL creation.

        Narrow rollups return a single descriptor keyed by `metric_name`.
        Wide rollups return one descriptor per protocol metric column, allowing
        the same creation loop to build either shape with identical aggregate
        semantics.
        """
        if "wide" not in source and base_wide_table_name is None:
            return [
                RollupManager.RollupMetricDescriptor(
                    group_key_sql="metric_name",
                    raw_value_sql="metric_value",
                    rolled_min_sql="min_value",
                    rolled_max_sql="max_value",
                    rolled_summary_sql="stats_summary",
                    min_alias="min_value",
                    max_alias="max_value",
                    summary_alias="stats_summary",
                )
            ]

        wide_table_name: str = base_wide_table_name or source
        self._log.debug(f"_resolve_rollup_metric_descriptors_helper: wide_table_name='{wide_table_name}'")

        result = session.execute(
            text(
                """
                SELECT mc.clean_column_name
                FROM metric_catalog mc
                JOIN protocol_registry pr ON mc.protocol_id = pr.protocol_id
                WHERE pr.wide_table_name = :tname
                AND mc.data_type NOT IN ('TEXT', 'BOOLEAN')
                ORDER BY mc.clean_column_name
                """
            ),
            {"tname": wide_table_name},
        )

        column_names: list[str] = list(result.scalars())

        return [
            RollupManager.RollupMetricDescriptor(
                group_key_sql=None,
                raw_value_sql=column_name,
                rolled_min_sql=f"min_{column_name}",
                rolled_max_sql=f"max_{column_name}",
                rolled_summary_sql=f"stats_summary_{column_name}",
                min_alias=f"min_{column_name}",
                max_alias=f"max_{column_name}",
                summary_alias=f"stats_summary_{column_name}",
            )
            for column_name in column_names
        ]

    def add_wide_rollup(self, protocol_name: str, wide_table_name: str | None) -> None:
        """
        Build the protocol-specific wide-table rollup stack for one protocol.

        This method now delegates the common table lifecycle work to the same
        post-processing helper used by the shared narrow stack, while
        preserving protocol-specific metric count caching and completion
        tracking.
        """

        self.migration_in_progress.set()

        try:
            if wide_table_name is None:
                self._log.info(f"Protocol '{protocol_name}' is narrow-only — skipping protocol-specific wide stack.")
                self._mark_rollup_setup_complete_helper(protocol_name, complete=True)
                return

            self.setup_rollup(
                table_name=wide_table_name,
                segment_by=self.compress_segmentby_wide,
                compression_policy_intervals=[
                    self.hourly_compress_after_interval,
                    self.daily_compress_after_interval,
                    self.weekly_compress_after_interval,
                    self.monthly_compress_after_interval,
                ],
                protocol_name=protocol_name,
                wide_table_name=wide_table_name,
                use_shared_rollup_flow=False,
            )

            # Retention and Catalog Count
            if wide_table_name is not None:
                # Record this protocol's column count for dynamic settings tuning.
                # wide_table_name is None for narrow-only and column count is irrelevant there.
                try:
                    with self.SessionFactory() as session:
                        cat_count = session.execute(
                            text("SELECT metric_count FROM protocol_registry WHERE protocol_name = :p"),
                            {"p": protocol_name}
                        ).scalar() or 0
                    self._protocol_wide_column_counts[protocol_name] = cat_count
                except SQLAlchemyError:
                    pass  # Non-fatal — _get_dynamic_settings falls back to 0

            # Mark complete — protocol_name is in scope here naturally
            self._mark_rollup_setup_complete_helper(protocol_name, complete=True)

            self._log.info(f"RollupManager.add_wide_rollup: setup complete for '{protocol_name}'")

        except Exception as e:
            self._log.error(f"RollupManager.add_wide_rollup failed for '{protocol_name}': {e}")
            # rollup_setup_complete stays False — next startup retries
            raise

        finally:
            self.migration_in_progress.clear()


    def _mark_rollup_setup_complete_helper(self, protocol: str, complete: bool = True ) -> None:
        """
        Sets rollup_setup_complete for the given protocol in protocol_registry.
        Called by RollupManager after successful setup_narrow_rollup (complete=True),
        or when a schema change requires rollup views to be rebuilt (complete=False).
        """
        with self.SessionFactory() as session:
            try:
                with session.begin():
                    session.execute(
                        text("""
                            UPDATE protocol_registry
                            SET rollup_setup_complete = :complete,
                                updated_at = :now
                            WHERE protocol_name = :p
                        """),
                        {
                            "complete": complete,
                            "p": protocol,
                            "now": _now_tz()
                        }
                    )
                self._log.debug(f"rollup_setup_complete = {complete} for protocol '{protocol}'")
            except Exception as e:
                self._log.error(f"Failed to mark rollup setup complete for '{protocol}': {e}")
                raise


    def _ensure_cagg_views_for_protocol(self, protocol_name: str, wide_table_name: str | None) -> None:
        """
        Create or rebuild the four hierarchical CAGG views for a protocol's wide table.

        This method mirrors the narrow rebuild-detection behavior:
        it scans the protocol-specific view stack for bucket drift, purges that
        stack if needed, and then recreates the views bottom-up using the shared
        generic CAGG builder.

        View names follow the pattern:
            hourly_rollup_wide__eg4_18kpv
            daily_rollup_wide__eg4_18kpv
            weekly_rollup_wide__eg4_18kpv
            monthly_rollup_wide__eg4_18kpv

        For narrow-only protocols (wide_table_name=None), creates views
        against device_metrics_narrow scoped by device_info_id instead.
        """
        if wide_table_name is None:
            self._log.info(f"Protocol '{protocol_name}' is narrow-only — skipping wide table CAGG views.")
            # Narrow rollup views are shared across all protocols —
            # only create them once
            if not self._narrow_rollups_created:
                self._ensure_narrow_cagg_views_helper()
                self._narrow_rollups_created = True
            return

        # Derive rollup prefix from wide table name
        # "device_metrics_wide__eg4_18kpv" -> "rollup_wide__eg4_18kpv"
        suffix: str = wide_table_name.removeprefix("device_metrics_")
        rollup_prefix: str = f"rollup_{suffix}"

        granularities: list[tuple[str, str, str, str]] = [
            # (granularity, bucket, start_offset, compress_after)
            ("hourly",  self.hourly_rollup_bucket,  self.hourly_rollup_start,
                        self.hourly_compress_after_interval),
            ("daily",   self.daily_rollup_bucket,   self.daily_rollup_start,
                        self.daily_compress_after_interval),
            ("weekly",  self.weekly_rollup_bucket,  self.weekly_rollup_start,
                        self.weekly_compress_after_interval),
            ("monthly", self.monthly_rollup_bucket, self.monthly_rollup_start,
                        self.monthly_compress_after_interval),
        ]

        try:
            with self.SessionFactory() as session:
                view_specs: list[tuple[str, str, str, str]] = []

                # Hierarchical — each view depends on the previous one
                # so they must be created in order
                previous_view: str = wide_table_name

                for gran, bucket, start_offset, compress_after in granularities:
                    view_name: str = f"{gran}_{rollup_prefix}"

                    # Monthly sources directly from the base wide table,
                    # not from the weekly view — a month is not a fixed
                    # multiple of 7 days so TimescaleDB rejects that hierarchy.
                    source_for_this_view: str = wide_table_name if gran == "monthly" else previous_view
                    view_specs.append((view_name, source_for_this_view, bucket, start_offset))
                    previous_view = view_name

                any_rebuild_needed: bool = False
                any_missing_views: bool = False

                for view_name, _, bucket, _ in view_specs:
                    rollup_state: str = self.rollup_needs_rebuild(session, view_name, bucket)

                    if rollup_state == "rebuild":
                        any_rebuild_needed = True
                        break

                    if rollup_state == "missing":
                        any_missing_views = True

                if any_rebuild_needed:
                    self._log.info(f"Bucket change detected for protocol '{protocol_name}'. Purging protocol rollups for clean rebuild.")
                    self._drop_protocol_rollup(session=session, view_names=[view_name for view_name, _, _, _ in view_specs])
                    self._purge_ghost_jobs_helper(session)

                elif any_missing_views:
                    self._log.info(f"Missing rollups detected for protocol '{protocol_name}'. Creating required views without purge.")

                for view_name, source_for_this_view, bucket, start_offset in view_specs:
                    self._ensure_single_cagg_view_helper(
                        session=session,
                        view_name=view_name,
                        source_table=source_for_this_view,
                        bucket_interval=bucket,
                        start_offset=start_offset,
                        protocol_name=protocol_name,
                        base_wide_table_name=wide_table_name,
                    )

                session.commit()

            self._log.info(f"CAGG views created for protocol '{protocol_name}' with prefix '{rollup_prefix}'")

        except SQLAlchemyError as e:
            self._log.error(f"CAGG view creation failed for protocol '{protocol_name}': {e}")
            raise

    def _drop_protocol_rollup(self, session: Session, view_names: list[str]) -> None:
        """
        Drop one protocol-specific rollup stack in dependency-safe reverse order.

        Only existing views are touched. If any per-view cleanup step fails, the
        transaction is rolled back before continuing so the session does not remain
        in the aborted state.
        """
        r_settings: dict[str, Any] = self._get_dynamic_settings_helper()

        ordered_view_names: list[str] = sorted(
            view_names,
            key=lambda name: 0 if "hourly" in name else 1 if "daily" in name else 2 if "weekly" in name else 3,
            reverse=True,
        )

        for view_name in ordered_view_names:
            full_name: str = f'"public"."{view_name}"'

            # Skip views that do not exist yet. This avoids poisoning the transaction
            # on ALTER / policy removal calls against missing materialized views.
            if not self._view_exists_helper(session, view_name):
                self._log.info(f"Rollup does not exist, skipping purge: {full_name}")
                continue

            self._log.info(f"Purging rollup: {full_name}")

            try:
                session.execute(text(f"SET LOCAL lock_timeout = '{r_settings['lock_timeout']}';"))

                # Disable compression first when present.
                try:
                    session.execute(text(f"ALTER MATERIALIZED VIEW {full_name} SET (timescaledb.compress = false);"))
                except Exception:
                    # IMPORTANT: clear failed transaction state before continuing
                    session.rollback()
                    self._log.info(f"View was already uncompressed: {full_name}")

                    # Re-enter a clean transaction for the remaining cleanup
                    session.execute(text(f"SET LOCAL lock_timeout = '{r_settings['lock_timeout']}';"))

                session.execute(text(f"SELECT remove_continuous_aggregate_policy('{full_name}', if_exists => true);"))
                session.execute(text(f"SELECT remove_retention_policy('{full_name}', if_exists => true);"))
                session.execute(text(f"DROP MATERIALIZED VIEW IF EXISTS {full_name} CASCADE;"))

                session.commit()
                self._known_rollup_views.pop(view_name, None)

            except Exception as e:
                session.rollback()
                self._log.error(f"Failed to purge rollup {full_name}: {e}")
                raise

    # -------------------------
    # Helpers for per-protocol CAGG management
    # -------------------------

    def _ensure_narrow_cagg_views_helper(self) -> None:
        """
        Creates the four shared narrow CAGG views (hourly/daily/weekly/monthly_rollup_narrow).
        These are shared across all protocols — device_info_id already partitions by device.
        Called at most once per RollupManager lifetime, guarded by _narrow_rollups_created.
        """
        granularities: list[tuple[str, str, str]] = [
            ("hourly",  self.hourly_rollup_bucket,  self.hourly_rollup_start),
            ("daily",   self.daily_rollup_bucket,   self.daily_rollup_start),
            ("weekly",  self.weekly_rollup_bucket,  self.weekly_rollup_start),
            ("monthly", self.monthly_rollup_bucket, self.monthly_rollup_start),
        ]

        try:
            with self.SessionFactory() as session:
                previous_source = "device_metrics_narrow"
                for gran, bucket, start_offset in granularities:
                    view_name: str = f"{gran}_rollup_narrow"

                    # Monthly sources directly from the base hypertable —
                    # 1 month is not a fixed multiple of 7 days so TimescaleDB
                    # rejects monthly-over-weekly hierarchies.
                    source_for_this_view: str = ("device_metrics_narrow" if gran == "monthly" else previous_source)

                    self._ensure_single_cagg_view_helper(
                        session=session,
                        view_name=view_name,
                        source_table=source_for_this_view,
                        bucket_interval=bucket,
                        start_offset=start_offset,
                        protocol_name="shared_narrow"
                    )
                    previous_source: str = view_name

            self._log.info("Shared narrow CAGG views created/verified.")

        except SQLAlchemyError as e:
            self._log.error(f"_ensure_narrow_cagg_views failed: {e}")
            raise

    def _ensure_single_cagg_view_helper(
        self,
        session: Session,
        view_name: str,
        source_table: str,
        bucket_interval: str,
        start_offset: str,
        protocol_name: str,
        base_wide_table_name: str | None = None,
    ) -> None:
        """
        Idempotently creates one CAGG view if it does not already exist,
        then records it in _known_rollup_views for the refresh loop.

        Delegates to _create_rollup (which branches wide vs narrow
        internally based on whether 'wide' appears in source_table).

        Args:
            session:          Active session (caller owns the transaction).
            view_name:        Target materialized view name.
            source_table:     Hypertable or parent CAGG view to aggregate from.
            bucket_interval:  Time-bucket width, e.g. '1 hour'.
            start_offset:     Refresh policy start offset, e.g. '3 hours'.
            protocol_name:    Used for log context only.
        """
        if self._view_exists_helper(session, view_name):
            self._log.debug(f"CAGG view '{view_name}' already exists for protocol '{protocol_name}' —skipping creation.")
        else:
            self._log.info(f"Creating CAGG view '{view_name}' from '{source_table}' during startup.")
            self._create_rollup_helper(session, source_table, view_name, bucket_interval, start_offset, base_wide_table_name=base_wide_table_name)

        # Register in the refresh registry regardless of whether we just created it
        # so the refresh loop picks it up on every run.
        if view_name not in self._known_rollup_views:
            self._known_rollup_views[view_name] = start_offset


    def _refresh_protocol_rollups_helper(self, protocol_name: str, wide_table_name: str | None) -> None:
        """
        Performs the initial full refresh for all CAGG views that belong to
        a specific protocol.  Called once at the end of add_wide_rollup after
        views are created.

        For wide protocols the views are named:
            hourly_rollup_wide__<suffix>
            daily_rollup_wide__<suffix>
            ...
        For narrow-only protocols the shared narrow views are refreshed:
            hourly_rollup_narrow, daily_rollup_narrow, ...

        Args:
            protocol_name:   Used to derive the rollup_prefix for wide protocols.
            wide_table_name: The wide table name, or None for narrow-only.
        """
        granularities: list[tuple[str, str]] = [
            ("hourly",  self.hourly_rollup_start),
            ("daily",   self.daily_rollup_start),
            ("weekly",  self.weekly_rollup_start),
            ("monthly", self.monthly_rollup_start),
        ]

        if wide_table_name is not None:
            suffix: str = wide_table_name.removeprefix("device_metrics_")
            rollup_prefix: str = f"rollup_{suffix}"
            view_names: List[Tuple[str, str]] = [(f"{gran}_{rollup_prefix}", start) for gran, start in granularities]
        else:
            view_names = [(f"{gran}_rollup_narrow", start) for gran, start in granularities]

        successfully_refreshed = False
        for view_name, start_offset in view_names:
            with self.SessionFactory() as session:
                if not self._view_exists_helper(session, view_name):
                    self._log.warning(f"_refresh_protocol_rollups: '{view_name}' not found, skipping.")
                    continue
                try:
                    self._refresh_single_rollup_helper(view_name, start_offset, force_full=True)
                    successfully_refreshed = True
                except Exception as e:
                    self._log.error(
                        f"Initial refresh failed for '{view_name}': {e} — background loop will retry.")
        if successfully_refreshed:
            self._update_last_refresh_helper(protocol_name)


    def rollup_needs_rebuild(self, session: Session, view_name: str, bucket_interval: str) -> str:
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

            This method compares the configured bucket interval with the actual interval used in the view definition.
            It first maps friendly terms to their equivalent PostgreSQL interval strings, then extracts the interval from
            the view definition using a regex, and finally compares the two intervals using PostgreSQL's interval comparison
            to ensure semantic correctness.  It's funky and may break if timescaledb changes their view definition format,
            but it is necessary to accurately detect when a rebuild is needed due to bucket changes,
            while avoiding unnecessary rebuilds when the intervals are semantically equivalent but textually different (e.g., '1 hour' vs '01:00:00').
        """
        # Map friendly terms to PG Interval inputs
        # This allows 'monthly' -> '1 month', while letting '2 hours' pass through as-is
        mapping: dict[str, str] = {
            "monthly": "1 month",
            "weekly": "7 days",
            "daily": "1 day",
            "hourly": "1 hour"
        }
        target_pg_val: str = mapping.get(bucket_interval.lower(), bucket_interval)

        try:

            # Query the TimescaleDB catalog for the view definition
            # Use a bind parameter :view_name for security and performance
            check_sql: TextClause = text("""
                SELECT view_definition
                FROM timescaledb_information.continuous_aggregates
                WHERE view_name = :view_name
            """)
            view_def: Optional[str] = session.scalar(check_sql, {"view_name": view_name})
            # 1. missing: not found in catalog, definitely needs build
            # 2. rebuild: exists but mismatched
            # 3. OK: exists and matches

            # If it doesn't exist, we definitely need to build it
            if not view_def:
                self._log.debug(f"Rollup {view_name} does not exist. Rebuild required.")
                return "missing"

            # If the result exists, extract the 'interval' string from the definition
            # We use Postgres regex_match to find the first argument of time_bucket()
            # Pattern looks for: time_bucket('interval_text', ...)
            extract_sql: TextClause = text("""
                SELECT (regexp_match(:vdef, 'time_bucket\\(''([^'']+)''', 'i'))[1]
            """)
            current_interval_str: Optional[str] = session.scalar(extract_sql, {"vdef": view_def})

            if not current_interval_str:
                self._log.warning(f"Could not parse time_bucket interval from {view_name} definition.")
                return "rebuild"  # Safer to rebuild if we can't verify the bucket

            # Final Comparison: Let PostgreSQL handle the semantic equality
            # This correctly recognizes that '01:00:00'::interval = '1 hour'::interval
            match_sql: TextClause = text("SELECT (:current)::interval = (:target)::interval")
            is_match: bool = session.scalar(match_sql, {"current": current_interval_str, "target": target_pg_val})

            if not is_match:
                self._log.info(
                    f"Config mismatch for {view_name}. "
                    f"Found: {current_interval_str}, Expected: {target_pg_val}. Rebuild required."
                )
                return "rebuild"
            else:
                # 4. Exists and matches config
                self._log.info(f"Rollup config matches for {view_name}. Expected: {bucket_interval} and received {target_pg_val}. No rebuild required.")
                return "OK"

        except SQLAlchemyError as e:
            self._log.error(f"Database error while checking rollup {view_name}: {e}")
            # Default to True to ensure we don't skip a necessary build on error
            return "rebuild"


    # -------------------------
    #  Determine wide vs narrow table usage for resource settings
    # -------------------------
    def _get_dynamic_settings_helper(self) -> dict[str, Any]:
        """
        Returns performance tier settings based on the actual workload drivers
        in a multi-protocol environment.

        Two axes determine lock pressure:
        1. Protocol count — more protocols = more CAGG views refreshed per cycle
            = longer total lock hold time across the refresh window.
        2. Wide table presence — wide rollups generate one stats_agg() expression
            per metric column, which is significantly more CPU and memory intensive
            than a single stats_agg(metric_value) on the narrow table.

        Tier selection:
        tier_low    — many protocols (>4) or any wide table with many columns (>100):
                        conservative work_mem and short lock_timeout to yield quickly.
        tier_medium — few protocols with wide tables, or many protocols narrow-only:
                        balanced settings.
        tier_high   — single or few protocols, narrow-only or small wide tables:
                        can afford longer lock windows and higher memory.
        """
        protocol_count: int = len(self._known_rollup_views)
        # Proxy for wide table complexity: any wide views registered means
        # at least one protocol has per-column rollup expressions in play.
        has_wide_views: bool = any("wide__" in v for v in self._known_rollup_views)

        # Max column count across all wide protocols from protocol_registry.
        # Cached after registration, so no DB hit on the hot path.
        max_wide_columns: int = self._max_wide_column_count_helper()

        if protocol_count > 4 or max_wide_columns > 100:
            return self.performance_tiers["tier_low"]
        elif has_wide_views or protocol_count > 2:
            return self.performance_tiers["tier_medium"]
        else:
            return self.performance_tiers["tier_high"]


    def _max_wide_column_count_helper(self) -> int:
        """
        Returns the highest metric_count across all registered wide-table protocols.
        Used by _get_dynamic_settings to tune lock/memory settings.
        Reads from the in-memory _protocol_wide_column_counts dict populated
        during add_wide_rollup, so there is no DB hit on the hot path.
        """
        if not self._protocol_wide_column_counts:
            return 0
        return max(self._protocol_wide_column_counts.values(), default=0)


    def _view_exists_helper(self, session: Session, view_name: str) -> bool:
        """Check to see if a continuous aggregate exists in the catalog.
        Parameters:
            session: Active database session for executing SQL commands.
            view_name: The name of the continuous aggregate view to check for existence.
        Returns:
            bool: True if the view exists, False otherwise.
        """
        check_sql: TextClause = text("SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = :name")

        try:
            # Keep ONLY the risky operation in the try block
            result = session.execute(check_sql, {"name": view_name}).fetchone()
        except Exception as e:
            # Handle the error and clean up
            self._log.error(f"Error checking view existence for {view_name}: {e}")
            session.rollback()
            return False
        else:
            # The 'else' block runs ONLY if the try block succeeded
            session.rollback()  # Resets state for your next AUTOCOMMIT call
            return result is not None

    def repopulate_known_rollup_views(self) -> None:
        """
        Rebuilds _known_rollup_views from protocol_registry after a reconnect.
        Queries all registered protocols and reconstructs view name -> start_offset
        mappings in the correct hierarchical order (hourly -> daily -> weekly -> monthly).
        """
        granularities: list[tuple[str, str]] = [
            ("hourly",  self.hourly_rollup_start),
            ("daily",   self.daily_rollup_start),
            ("weekly",  self.weekly_rollup_start),
            ("monthly", self.monthly_rollup_start),
        ]

        self._known_rollup_views.clear()

        # Always add the shared narrow views first
        for gran, start_offset in granularities:
            view_name = f"{gran}_rollup_narrow"
            self._known_rollup_views[view_name] = start_offset

        # Then per-protocol wide views
        try:
            with self.SessionFactory() as session:
                rows = session.execute(
                    text("""
                        SELECT rollup_prefix
                        FROM protocol_registry
                        WHERE rollup_enabled = true
                        AND rollup_prefix IS NOT NULL
                        ORDER BY created_at ASC
                    """)
                ).fetchall()

            for (rollup_prefix,) in rows:
                for gran, start_offset in granularities:
                    view_name: str = f"{gran}_{rollup_prefix}"
                    self._known_rollup_views[view_name] = start_offset

            self._log.info(
                f"Repopulated {len(self._known_rollup_views)} "
                f"rollup views after reconnect."
            )
        except SQLAlchemyError as e:
            self._log.error(f"repopulate_known_rollup_views failed: {e}")
            raise

    def _view_exists_conn_helper(self, conn: Connection, view_name: str) -> bool:
        """Session-free variant of _view_exists for use inside autocommit engine.connect() blocks."""
        try:
            result: Row[Any] | None = conn.execute(
                text("SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = :name"),
                {"name": view_name}
            ).fetchone()
        except Exception as e:
            self._log.error(f"_view_exists_conn error for {view_name} : {e}")
            return False
        else:
            return result is not None


    def _drop_all_continuous_aggregates(self, session: Session) -> None:
        """
        Teardown all rollups in correct dependency order (Top-Down) to
        fix the 'DependentObjectsStillExist' error.
        Process:
        1. Fetch all existing continuous aggregates from the TSDB catalog.
        2. Determine the drop order based on naming conventions (Weekly -> Daily -> Hourly).
        3. For each view:
            1) Set a short lock timeout to fail fast if blocked by flush thread,
            2) Disable compression to avoid issues with compressed CAGGs,
            3) Remove policies to prevent orphaned jobs,
            4) Acquire an exclusive lock on the view to ensure no concurrent access,
            5) Drop the view with CASCADE to clean up dependencies,
            6) Commit after each drop to release locks and allow the next drop to proceed.
        """
        r_settings: dict[str, Any] = self._get_dynamic_settings_helper()
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
            # Clear the refresh registry — views are dropped, _ensure_single_cagg_view
            # will re-populate it during the creation phase.
            self._known_rollup_views.clear()

        except Exception as e:
            session.rollback()
            self._log.error(f"Failed to purge rollup stack: {e}")
            raise

    # 10e

    def refresh_rollups(self, force_full: bool = False) -> None:
        """
        Refreshes rollups in Bottom-Up order (Hourly -> Daily -> Weekly -> Monthly).
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
                for view_name, start_offset in self._known_rollup_views.items():
                    if self._view_exists_helper(session, view_name):
                        self._refresh_single_rollup_helper(view_name, start_offset, force_full)
                    else:
                        self._log.warning(f"Skipping refresh: '{view_name}' does not exist.")
            except Exception as e:
                self._log.error(f"Rollup refresh failed: {e}")

    def _refresh_single_rollup_helper(self, view_name: str, start_offset: str, force_full: bool = False) -> None:
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
        r_settings: dict[str, Any] = self._get_dynamic_settings_helper()

        stop_signal: List[bool] = self._start_refresh_watchdog_helper(view_name)

        start_time: float = time.perf_counter()
        mode: Literal['FULL'] | Literal['INCREMENTAL'] = "FULL" if force_full else "INCREMENTAL"

        self._log.info(f"Starting {mode} refresh for {view_name}...")

        # AUTOCOMMIT is mandatory for CALL refresh_continuous_aggregate
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
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
    def _stop_existing_watchdog_helper(self) -> None:
        """Signals the existing watchdog to exit immediately.
        The watchdog thread checks the signal at short intervals and will terminate if the signal is set to True.
        """
        if hasattr(self, '_current_watchdog_signal') and self._current_watchdog_signal:
            self._current_watchdog_signal[0] = True
            self._current_watchdog_signal = None

    # watchdog thread to monitor long-running refreshes
    def _start_refresh_watchdog_helper(self, view_name: str) -> List[bool]:
        """
        This watchdog runs in a separate thread to monitor the progress of a continuous aggregate refresh.
        It periodically checks the pg_stat_activity for the refresh query and sends an MPG alert
        if it detects that the refresh is blocked by locks for an extended period (e.g., 30 seconds).
        Parameters:
            view_name (str): The name of the continuous aggregate view being refreshed, used for monitoring and alerting purposes.

        Returns:
            List[bool]: A mutable list containing a single boolean value that serves as a stop signal for the watchdog thread.
        """
        # 1. Kill any existing watchdog before starting a new one
        self._stop_existing_watchdog_helper()

        # 2. Create the new stop signal
        stop_signal: List[bool] = [False]
        self._current_watchdog_signal: List[bool] | None = stop_signal

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

                        if res.wait_event_type == Lock:
                            msg: str = (
                                f"⚠️ {view_name} is BLOCKED by a lock.\n"
                                f"The refresh for {view_name} has been running for over 30 seconds and is "
                                f"currently blocked by a lock. Please investigate the database locks to ensure "
                                f"the refresh can complete successfully."
                            )

                            self.send_message(message=msg, title="MPG Blocked View Alert", priority=1)


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

    def _refresh_rollup_loop_helper(self) -> None:
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
                    if count > 0:
                        self._log.debug(f"Replayed {count} points. Waiting for flush...")
                        # Wait for flush worker to finish writing replayed points
                        self._flush_queue.join()

                # 4. Sequential Hierarchical Refresh
                # Order: Hourly -> Daily -> Weekly -> Monthly
                with self.engine.connect() as conn:
                    conn: Connection = conn.execution_options(isolation_level="AUTOCOMMIT")
                    # Apply dynamic session settings from performance tiers
                    tier: dict[str, Any] = self._get_dynamic_settings_helper()
                    conn.execute(text(f"SET work_mem = '{tier['work_mem']}';"))
                    conn.execute(text(f"SET lock_timeout = '{tier['lock_timeout']}';"))

                    # Refresh all registered CAGG views in insertion order
                    # (hourly -> daily -> weekly -> monthly, narrow before wide per protocol).
                    for view_name in list(self._known_rollup_views):
                        if not self._view_exists_conn_helper(conn, view_name):
                            self._log.warning(f"Skipping refresh: '{view_name}' not found.")
                            continue
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

    def _update_last_refresh_helper(self, protocol: str) -> None:
        """
        Updates last_refresh_at in protocol_registry after a successful
        rollup refresh for the given protocol.
        Called by RollupManager after each refresh cycle completes.
        """
        with self.SessionFactory() as session:
            try:
                with session.begin():
                    session.execute(
                        text("""
                            UPDATE protocol_registry
                            SET last_refresh_at = :now,
                                updated_at = :now
                            WHERE protocol_name = :p
                        """),
                        {"now": _now_tz(), "p": protocol}
                    )
            except Exception as e:
                self._log.error(f"Failed to update last_refresh_at for '{protocol}': {e}")
                # Non-fatal — don't raise, refresh already completed successfully

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

    def _purge_ghost_jobs_helper(self, session: Session) -> None:
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
        """ The backend_pid subquery uses the same application_name LIKE '%job_id%' pattern as the GHOST_PROCESS check, so it's consistent
            with how TimescaleDB names its workers. If TimescaleDB changes that naming convention, both checks break together, which is easier to spot.
            The LIMIT 1 guard is important — a job could theoretically have multiple pg_stat_activity rows if it spawned subprocesses, and
            you only want to terminate the main worker.
            The backend_pid will be NULL for ORPHANED_METADATA and GHOST_PROCESS rows (by definition — ghost processes have no active PID),
            so checking if backend_pid is not None before calling pg_terminate_backend prevents a spurious error on those branches."""

        detect_sql: TextClause = text("""
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
            ),
            classified AS (
                SELECT
                    t.job_id,
                    t.application_name,
                    CASE
                        WHEN (t.application_name LIKE 'Refresh%' OR t.application_name LIKE 'Retention%')
                            AND NOT EXISTS (
                                SELECT 1 FROM timescaledb_information.continuous_aggregates c
                                JOIN timescaledb_information.jobs j2 ON t.job_id = j2.job_id
                                WHERE j2.hypertable_name = c.view_name
                            ) THEN 'ORPHANED_METADATA'
                        WHEN t.last_run_status = 'Started' AND t.last_run_started_at < now() - t.allowed_duration
                            THEN 'STALE_EXECUTION'
                        WHEN t.last_run_status = 'Started'
                            AND NOT EXISTS (
                                SELECT 1 FROM pg_stat_activity p
                                WHERE p.application_name LIKE '%' || t.job_id || '%'
                            ) THEN 'GHOST_PROCESS'
                        ELSE NULL
                    END as aberrancy_type,
                    (
                        SELECT p.pid
                        FROM pg_stat_activity p
                        WHERE p.application_name LIKE '%' || t.job_id || '%'
                        LIMIT 1
                    ) as backend_pid
                FROM job_thresholds t
            )
            SELECT job_id, application_name, aberrancy_type, backend_pid
            FROM classified
            WHERE aberrancy_type IS NOT NULL
        """)

        try:
            ghosts = session.execute(detect_sql).fetchall()

            if not ghosts:
                self._log.info("All background workers healthy.")
                return

            for job_id, app_name, issue, backend_pid in ghosts:
                self._log.warning(f"Purging {issue}: {app_name} (Job {job_id})")

                # Terminate hanging backend if it still technically exists (Stale case)
                if issue == 'STALE_EXECUTION' and backend_pid is not None:
                    session.execute(text("SELECT pg_terminate_backend(:pid);"), {"pid": backend_pid})

                # delete_job() terminates any remaining worker and removes the schedule
                session.execute(text("SELECT delete_job(:job_id);"), {"job_id": job_id})

            session.commit()
        except Exception as e:
            session.rollback()
            self._log.error(f"Sweep failed: {e}")


# ----------------------------------------------------------------------------------------------------------------------
# Wide table field (column) administration
# ----------------------------------------------------------------------------------------------------------------------

@dataclass
class WideTableField:
    """
    A single deletable field (dynamic metric column) belonging to a
    protocol's wide table, as presented to an administrative UI.

    column_name is the physical, sanitized column that lives in the wide
    table (see timescaledb._clean_column_name); metric_name is the original
    source/registry name it was derived from. The two can differ, so the UI
    should key off column_name and echo it back unchanged to
    WideTableFieldManager.delete_fields().
    """
    metric_name: str
    column_name: str
    data_type: str
    unit_mod: float | None
    notes: str | None


@dataclass
class WideTableFieldDeletionResult:
    """Outcome of a WideTableFieldManager.delete_fields() call."""
    protocol_name: str
    wide_table_name: str
    deleted: list[str]              # column_names actually dropped
    not_found: list[str]            # requested column_names that didn't match any existing field
    remaining_fields: list[str]     # column_names still present on the wide table after the operation
    rollups_rebuilt: bool           # whether the protocol's rollups were successfully rebuilt


class WideTableFieldManager:
    """
    Administrative helper for deleting dynamic metric columns ("fields")
    from a protocol's wide table. Intended to be driven by an encapsulating
    web app that manages bridge administration -- the web UI lists fields
    with checkboxes, the admin selects some for removal, and on commit the
    UI calls delete_fields() with the selected column names.

    Why this can't be a plain ALTER TABLE:
    Wide tables are dynamically shaped -- every metric a protocol reports
    becomes its own column (see timescaledb._ensure_columns_for_metrics).
    RollupManager then builds four hierarchical continuous aggregates
    (hourly/daily/weekly/monthly) on top of each wide table, and every one
    of those materialized views has a SELECT list built directly from the
    wide table's current columns (see
    RollupManager._resolve_rollup_metric_descriptors_helper). A column
    can't simply disappear out from under that -- the dependent rollups
    have to be torn down first, or TimescaleDB will either block the
    ALTER TABLE or leave the views referencing a column that no longer
    exists. This class always performs a delete as a single
    tear-down / alter / rebuild sequence, never a bare column drop.

    Typical usage from the web app:

        field_mgr = WideTableFieldManager(bridge)

        # populate the admin screen
        protocols = field_mgr.list_editable_protocols()
        fields = field_mgr.list_fields("eg4_18kpv")

        # ... admin checks some boxes and hits "Delete" ...
        result = field_mgr.delete_fields("eg4_18kpv", ["soc_percent", "grid_voltage"])
    """

    # Structural columns that exist on every wide table and can never be
    # removed through this class.
    PROTECTED_COLUMNS: frozenset[str] = frozenset({"m_time", "device_info_id"})

    def __init__(self, bridge: "timescaledb") -> None:
        if not isinstance(bridge, timescaledb):
            msg: str = f"WideTableFieldManager requires a timescaledb bridge instance, got {type(bridge)}"
            raise TypeError(msg)
        self._bridge: "timescaledb" = bridge
        self._log: logging.Logger = bridge._log
        self.engine: Engine = bridge.engine
        self.SessionFactory: sessionmaker[Session] = bridge.SessionFactory

    # -------------------------
    # Resolution helpers
    # -------------------------

    def _resolve_wide_table(self, protocol_name: str) -> tuple[int, str]:
        """
        Looks up the protocol_id and wide_table_name for protocol_name.

        Raises:
            ValueError: protocol is unknown, or is narrow-only (>200
                        metrics) and therefore has no wide table to edit.
        """
        with self.SessionFactory() as session:
            row: Row[Any] | None = session.execute(
                text("""
                    SELECT protocol_id, wide_table_name
                    FROM protocol_registry
                    WHERE protocol_name = :p
                """),
                {"p": protocol_name}
            ).fetchone()

        if row is None:
            msg: str = f"Protocol '{protocol_name}' is not registered in protocol_registry."
            raise ValueError(msg)

        protocol_id, wide_table_name = row
        if wide_table_name is None:
            msg: str = f"Protocol '{protocol_name}' is narrow-only (>200 metrics) and has no wide table to edit."
            raise ValueError(msg)

        return protocol_id, wide_table_name

    def _wide_view_names(self, wide_table_name: str) -> list[str]:
        """
        Reproduces RollupManager's per-protocol view-naming convention, e.g.
        'device_metrics_wide__eg4_18kpv' ->
            ['hourly_rollup_wide__eg4_18kpv', 'daily_rollup_wide__eg4_18kpv',
             'weekly_rollup_wide__eg4_18kpv', 'monthly_rollup_wide__eg4_18kpv']

        Must stay in sync with RollupManager._ensure_cagg_views_for_protocol.
        """
        suffix: str = wide_table_name.removeprefix("device_metrics_")
        rollup_prefix: str = f"rollup_{suffix}"
        return [f"{gran}_{rollup_prefix}" for gran in ("hourly", "daily", "weekly", "monthly")]

    # -------------------------
    # Read-only listing for the UI
    # -------------------------

    def list_editable_protocols(self) -> list[tuple[str, str]]:
        """
        Returns (protocol_name, wide_table_name) pairs for every protocol
        that has a wide table and is therefore editable via this class
        (narrow-only protocols are excluded). Useful for populating a
        wide-table selector in the admin UI without an extra round trip
        per protocol to resolve its table name.
        """
        with self.SessionFactory() as session:
            rows = session.execute(
                text("""
                    SELECT protocol_name, wide_table_name FROM protocol_registry
                    WHERE wide_table_name IS NOT NULL
                    ORDER BY protocol_name
                """)
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def resolve_wide_table_name(self, protocol_name: str) -> str:
        """
        Public wrapper around _resolve_wide_table for callers (e.g. the web
        UI layer) that only need the table name, not the protocol_id.

        Raises:
            ValueError: unknown or narrow-only protocol.
        """
        _protocol_id, wide_table_name = self._resolve_wide_table(protocol_name)
        return wide_table_name

    def list_fields(self, protocol_name: str) -> list[WideTableField]:
        """
        Returns the current set of deletable metric columns for a
        protocol's wide table, for the calling web UI to render as a
        checkbox list.

        Raises:
            ValueError: unknown or narrow-only protocol (see
                        _resolve_wide_table).
        """
        protocol_id, _wide_table_name = self._resolve_wide_table(protocol_name)

        with self.SessionFactory() as session:
            rows = session.execute(
                text("""
                    SELECT metric_name, clean_column_name, data_type, unit_mod, notes
                    FROM metric_catalog
                    WHERE protocol_id = :pid
                    ORDER BY clean_column_name
                """),
                {"pid": protocol_id}
            ).fetchall()

        return [
            WideTableField(
                metric_name=metric_name,
                column_name=column_name,
                data_type=data_type,
                unit_mod=unit_mod,
                notes=notes,
            )
            for metric_name, column_name, data_type, unit_mod, notes in rows
            if column_name not in self.PROTECTED_COLUMNS
        ]

    # -------------------------
    # Deletion
    # -------------------------

    def delete_fields(self, protocol_name: str, field_names: list[str]) -> WideTableFieldDeletionResult:
        """
        Deletes the given fields (wide-table column names, as returned by
        list_fields()) from protocol_name's wide table, then rebuilds that
        protocol's rollups, indexes, and compression/retention policies.

        Safe to call with a mix of valid/unrecognized names -- anything not
        found in metric_catalog for this protocol is reported back in
        `not_found` and simply skipped rather than raising, so a UI
        double-submit or a stale checkbox list doesn't hard-fail the whole
        request.

        Args:
            protocol_name: The protocol whose wide table is being edited.
            field_names: column_name values from list_fields(), i.e. the
                         checked boxes the admin committed for deletion.

        Returns:
            WideTableFieldDeletionResult summarizing what happened.

        Raises:
            ValueError: unknown/narrow-only protocol, empty selection, only
                        protected columns requested, or the request would
                        delete every remaining metric column (an empty wide
                        table isn't supported -- disable/remove the
                        protocol instead).
            RuntimeError: RollupManager isn't initialized yet (bridge not
                        connected to TimescaleDB).
            Exception: any failure partway through the drop/rebuild
                        sequence is logged and re-raised so the caller
                        (and the admin) know the operation did not
                        complete cleanly. rollup_setup_complete is left
                        False in that case, so the background reconnect /
                        rediscovery path will retry the rollup rebuild
                        automatically (see timescaledb._rediscover_protocols).
        """
        if not field_names:
            raise ValueError("No fields were provided to delete.")

        # de-dupe, preserve order, drop blanks
        requested: list[str] = [f for f in dict.fromkeys(field_names) if f]

        protected_requested: list[str] = [f for f in requested if f in self.PROTECTED_COLUMNS]
        if protected_requested:
            msg: str = f"Refusing to delete protected columns: {protected_requested}"
            raise ValueError(msg)

        if self._bridge.rollup_mgr is None:
            raise RuntimeError(
                "RollupManager is not initialized -- bridge must be connected before editing wide tables."
            )

        protocol_id, wide_table_name = self._resolve_wide_table(protocol_name)
        rollup_mgr: "RollupManager" = self._bridge.rollup_mgr

        # Pause the flush worker for the duration of the edit. add_wide_rollup()
        # (called below) also sets/clears this same event around its own work;
        # setting it here first just widens the window to cover the column
        # drop that happens before the rebuild.
        self._bridge.migration_in_progress.set()
        rebuilt: bool = False

        try:
            with self.SessionFactory() as session:
                # Whitelist requested names against what's actually in
                # metric_catalog for this protocol -- column names handed in
                # from the UI layer are never trusted directly in DDL.
                catalog_rows  = session.execute(
                    text("""
                        SELECT catalog_id, clean_column_name
                        FROM metric_catalog
                        WHERE protocol_id = :pid
                    """),
                    {"pid": protocol_id}
                ).fetchall()

            existing_columns: dict[str, int] = {col: cid for cid, col in catalog_rows}
            to_delete: list[str] = [f for f in requested if f in existing_columns]
            not_found: list[str] = [f for f in requested if f not in existing_columns]

            if not to_delete:
                self._log.info(
                    f"WideTableFieldManager: none of {requested} matched existing fields on "
                    f"'{wide_table_name}' — nothing to delete."
                )
                return WideTableFieldDeletionResult(
                    protocol_name=protocol_name,
                    wide_table_name=wide_table_name,
                    deleted=[],
                    not_found=not_found,
                    remaining_fields=sorted(existing_columns.keys()),
                    rollups_rebuilt=False,
                )

            if len(to_delete) >= len(existing_columns):
                raise ValueError(  # noqa: TRY301
                    "Refusing to delete every remaining field -- a wide table needs at least "
                    "one metric column. Disable or remove the protocol instead if it's no "
                    "longer needed."
                )

            self._log.info(
                f"WideTableFieldManager: deleting {len(to_delete)} field(s) from "
                f"'{wide_table_name}' (protocol='{protocol_name}'): {to_delete}"
            )

            # 1. Tear down this protocol's rollups first. They SELECT every
            #    current wide-table column by name, so they cannot survive
            #    the ALTER TABLE below and must be rebuilt afterward anyway.
            view_names: list[str] = self._wide_view_names(wide_table_name)
            with self.SessionFactory() as session:
                rollup_mgr._drop_protocol_rollup(session=session, view_names=view_names)

            # Mark the protocol's rollup setup incomplete for the duration of
            # the edit. This mirrors the crash-safety pattern used during
            # initial setup: if the process dies mid-edit, restart-time
            # rediscovery (_rediscover_protocols) will see setup_complete =
            # False and retry the rollup rebuild automatically instead of
            # silently leaving the protocol without rollups.
            rollup_mgr._mark_rollup_setup_complete_helper(protocol_name, complete=False)

            # 2. Drop the columns and their metric_catalog rows, serialized
            #    against other schema changes with the same advisory lock
            #    that column *additions* use.
            with self._bridge._schema_lock:
                with self.SessionFactory() as session:
                    with session.begin():
                        self._bridge._schema_advisory_lock(session)

                        self._decompress_chunks_best_effort(session, wide_table_name)

                        # table_name/col come from _safe_table_name() /
                        # _clean_column_name() at creation time and are
                        # re-validated against metric_catalog above, so the
                        # f-string is safe here (same pattern used by
                        # _ensure_columns_for_metrics' ADD COLUMN).
                        for col in to_delete:
                            session.execute(text(f"ALTER TABLE {wide_table_name} DROP COLUMN IF EXISTS {col};"))

                        catalog_ids: list[int] = [existing_columns[col] for col in to_delete]
                        session.execute(
                            text("DELETE FROM metric_catalog WHERE catalog_id = ANY(:ids)"),
                            {"ids": catalog_ids},
                        )

                        session.execute(
                            text("""
                                UPDATE protocol_registry
                                SET metric_count = GREATEST(metric_count - :n, 0),
                                    updated_at = :now
                                WHERE protocol_id = :pid
                            """),
                            {"n": len(to_delete), "now": _now_tz(), "pid": protocol_id}
                        )

                    # 3. Resync the ORM reflection + write-path column cache
                    #    so in-flight/next writes see the new shape
                    #    immediately, same as after a column addition.
                    self._bridge._sync_single_table_schema(wide_table_name)

            # 4. Drop any now-stale metric_name -> column mappings used to
            #    coerce incoming raw values before insert.
            mapping: dict[str, tuple[str, str]] = self._bridge._protocol_metric_mappings.get(protocol_name, {})
            for metric_name, (col, _dtype) in list(mapping.items()):
                if col in to_delete:
                    mapping.pop(metric_name, None)

            # 5. Rebuild rollups (indexes, compression policy, retention
            #    policy, and the four hierarchical CAGGs) from whatever
            #    columns remain in metric_catalog.
            rollup_mgr.add_wide_rollup(protocol_name, wide_table_name)
            rebuilt = True

            remaining_fields: list[str] = sorted(set(existing_columns.keys()) - set(to_delete))

            self._log.info(f"WideTableFieldManager: deleted {to_delete} from '{wide_table_name}' and rebuilt rollups.")

            return WideTableFieldDeletionResult(
                protocol_name=protocol_name,
                wide_table_name=wide_table_name,
                deleted=to_delete,
                not_found=not_found,
                remaining_fields=remaining_fields,
                rollups_rebuilt=rebuilt,
            )

        except Exception as e:
            self._log.error(f"WideTableFieldManager.delete_fields failed for '{protocol_name}': {e}")
            raise

        finally:
            # add_wide_rollup() clears this same event in its own finally
            # block on the success path, so this is a no-op by the time we
            # get here normally -- it's a safety net for paths that raised
            # before reaching the rebuild step.
            self._bridge.migration_in_progress.clear()

    def _decompress_chunks_best_effort(self, session: Session, table_name: str) -> None:
        """
        Decompresses any compressed chunks of table_name before an
        ALTER TABLE ... DROP COLUMN. Support for dropping columns directly
        on compressed hypertable chunks is inconsistent across TimescaleDB
        versions, so this proactively decompresses first. Newly written
        data is recompressed on the normal compression policy schedule --
        this only affects already-compressed historical chunks.

        Best-effort: a table with no compressed chunks (or not yet a
        hypertable) simply no-ops here rather than failing the edit.
        """
        try:
            session.execute(
                text("""
                    SELECT decompress_chunk(c, if_not_compressed => true)
                    FROM show_chunks(:tname) AS c;
                """),
                {"tname": table_name}
            )
        except Exception as e:
            self._log.debug(f"_decompress_chunks_best_effort: nothing to decompress for '{table_name}': {e}")
