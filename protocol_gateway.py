#!/usr/bin/env python3
# Description: Main module for Inverters ModBus RTU data to MQTT
# File: protocol_gateway.py
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
Main module for Inverters ModBus RTU data to MQTT
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from enum import Enum

from classes.WebServer.main import start_webserver

# Check if Python version is greater than 3.10
if sys.version_info < (3, 10):
    print("==================================================")
    print("WARNING: python version 3.10 or higher is required")
    print("Current version: " + sys.version)
    print("Please upgrade your python version to 3.10")
    print("==================================================")
    time.sleep(4)


import argparse
import logging
import logging.handlers
import sys
from configparser import ConfigParser, NoOptionError, NoSectionError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from classes.messaging.message_handler import MessageHandler
from classes.protocol_settings import (
    protocol_settings,
    registry_map_entry,
)
from classes.transports.transport_base import transport_base
from defs.common import TransportSettings

__logo = """

███╗   ███╗██╗   ██╗██╗   ████████╗██╗
████╗ ████║██║   ██║██║   ╚══██╔══╝██║
██╔████╔██║██║   ██║██║      ██║   ██║
██║╚██╔╝██║██║   ██║██║      ██║   ██║
██║ ╚═╝ ██║╚██████╔╝███████╗ ██║   ██║
╚═╝     ╚═╝ ╚═════╝ ╚══════╝ ╚═╝   ╚═╝

██████╗ ██████╗  ██████╗ ████████╗ ██████╗  ██████╗ ██████╗ ██╗          ██████╗  █████╗ ████████╗███████╗██╗    ██╗ █████╗ ██╗   ██╗
██╔══██╗██╔══██╗██╔═══██╗╚══██╔══╝██╔═══██╗██╔════╝██╔═══██╗██║         ██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝██║    ██║██╔══██╗╚██╗ ██╔╝
██████╔╝██████╔╝██║   ██║   ██║   ██║   ██║██║     ██║   ██║██║         ██║  ███╗███████║   ██║   █████╗  ██║ █╗ ██║███████║ ╚████╔╝
██╔═══╝ ██╔══██╗██║   ██║   ██║   ██║   ██║██║     ██║   ██║██║         ██║   ██║██╔══██║   ██║   ██╔══╝  ██║███╗██║██╔══██║  ╚██╔╝
██║     ██║  ██║╚██████╔╝   ██║   ╚██████╔╝╚██████╗╚██████╔╝███████╗    ╚██████╔╝██║  ██║   ██║   ███████╗╚███╔███╔╝██║  ██║   ██║
╚═╝     ╚═╝  ╚═╝ ╚═════╝    ╚═╝    ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝     ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝

"""  # noqa: W291


class CustomConfigParser(ConfigParser):
    """
    Extends ConfigParser to support:
    - Option as list of possible names (tries each in order until one is found)
    - Detects missing options and sections with clear error messages, even when using the option list feature.
    - Fallback values handled manually to allow for the above features while still providing clear error messages when options are missing.
    - Type conversion in getint/getfloat/getboolean that also supports the option list and fallback features.
    - Strips whitespace and comments from values

    """
    def get(self, section, option, *args, **kwargs) -> str:
        """Scheduling path: N/A — config parsing, used during setup regardless of read_mode.

        Read a string value from the config, with alias support and comment stripping.

        ``option`` may be a single key name or a list of candidate names; when a
        list is given, each name is tried in order and the first match wins.
        ``fallback`` is extracted and applied manually so that a missing key
        without a fallback raises ``NoOptionError`` naming the first candidate
        rather than silently returning ``None``.  Inline ``#`` comments are
        stripped from the returned value before it is returned.
        """
        fallback = None
        value = None

        # Extract fallback to handle it manually at the end
        if "fallback" in kwargs:
            fallback = kwargs["fallback"]
            kwargs["fallback"] = None

        # Helper to safely call the parent get method
        def safe_get(sect, opt) -> str | None:
            """Scheduling path: N/A — config parsing, used during setup regardless of read_mode.

            Call the parent ``ConfigParser.get`` and return ``None`` on any missing-key error."""
            try:
                return super(CustomConfigParser, self).get(sect, opt, *args, **kwargs)
            except (NoOptionError, NoSectionError):
                print(f"Option '{opt}' not found in section '{sect}'")
                return None

        # Logic for handling list of options or a single string
        if isinstance(option, list):
            for name in option:
                value = safe_get(section, name)
                if value is not None:
                    break
        else:
            value: str | None = safe_get(section, option)

        # Apply fallback if no value was found in the config
        if value is None:
            value = fallback

        # If still None, raise the error for the user
        if value is None:
            # Check if the section exists to raise the most accurate error
            if not self.has_section(section):
                raise NoSectionError(section)

            error_opt = option[0] if isinstance(option, list) else option
            raise NoOptionError(error_opt, section)

        # Cleanup and type conversion
        value = str(value).strip()
        if '#' in value:
            value = value.split('#')[0].strip()

        return value
    # because using get, None is not reachable, so removed and type checker is happy.
    def getint(self, section: str, option: str | list[str], *args: Any, **kwargs: Any ) -> int:
        """Scheduling path: N/A — config parsing, used during setup regardless of read_mode.

        Read a config value and return it as an ``int``.

        Delegates to ``get``, inheriting alias-list and fallback support.
        Raises ``ValueError`` if the resolved string cannot be converted to an integer.
        """
        value: str = self.get(section, option, *args, **kwargs)
        return int(value)

    def getfloat(self, section: str, option: str | list[str], *args: Any, **kwargs: Any ) -> float:
        """Scheduling path: N/A — config parsing, used during setup regardless of read_mode.

        Read a config value and return it as a ``float``.

        Delegates to ``get``, inheriting alias-list and fallback support.
        Raises ``ValueError`` if the resolved string cannot be converted to a float.
        """
        value: str = self.get(section, option, *args, **kwargs)
        return float(value)

    def getboolean(self, section: str, option: str | list[str], *args: Any, **kwargs: Any) -> bool:
        """Scheduling path: N/A — config parsing, used during setup regardless of read_mode.

        Read a config value and return it as a ``bool``.

        Delegates to ``get``, inheriting alias-list and fallback support.
        Accepts ``true/yes/on/1/enable/enabled`` as ``True`` and
        ``false/no/off/0/disable/disabled`` as ``False`` (case-insensitive).
        Raises ``ValueError`` for any other string.
        """
        value: str = self.get(section, option, *args, **kwargs)
        value_str: str = value.lower().strip()

        if value_str in ('true', 'yes', 'on', '1', 'enable', 'enabled'):
            return True
        if value_str in ('false', 'no', 'off', '0', 'disable', 'disabled'):
            return False
        msg: str =f"Not a boolean: {value}"
        raise ValueError(msg)
class NetworkError(Enum):
    # Standard codes
    CONN_RESET = '104'       # Errno 104 - Connection reset by peer (common for MQTT disconnects)
    BROKEN_PIPE = '32'       # Errno 32 - Broken pipe (common for MQTT disconnects)
    TIMED_OUT = '110'        # Errno 110 - Connection timed out (common for network issues)
    # Additional common codes
    CONN_REFUSED = '111'     # ECONNREFUSED
    NET_UNREACHABLE = '101'  # ENETUNREACH
    HOST_UNREACHABLE = '113' # EHOSTUNREACH
    ADDR_IN_USE = '98'       # EADDRINUSE

class ScrapeGroup:
    """
    Scheduling path: All (Sequential, Concurrent, Interleaved).

    Represents a set of scraper transports that all read from the same
    physical device (same scrape_target) and can therefore share a single
    Modbus read cycle.

    The primary transport performs the actual scrape using the union of all
    member variable masks at the fastest read_interval among the members.
    Each member transport then receives only the metrics relevant to it,
    forwarded to its own bridge(s) at its own read_interval cadence.
    """

    def __init__(self, primary: transport_base) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Initialise a single-member group with ``primary`` as the initial sole member.

        ``primary`` will be promoted or replaced by a faster member if
        ``add_member`` is subsequently called with a transport whose
        ``read_interval`` is shorter.
        """
        self.primary: transport_base = primary
        self.members: list[transport_base] = [primary]
        # When each member last had its data forwarded to its bridges
        self._member_last_forward: dict[str, float] = {
            primary.transport_name: 0.0
        }

    def add_member(self, transport: transport_base) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Add ``transport`` to this group and promote it to primary if it has the shortest read interval.

        A transport with a shorter ``read_interval`` drives more frequent scrapes,
        so it becomes the primary to ensure no member is starved.  Transports with
        ``read_interval <= 0`` (bridges / write-only) are appended but never
        promoted to primary.
        """
        self.members.append(transport)
        self._member_last_forward[transport.transport_name] = 0.0
        # Primary is always the member with the shortest read_interval (most frequent).
        if transport.read_interval > 0 and (
            self.primary.read_interval <= 0
            or transport.read_interval < self.primary.read_interval
        ):
            self.primary = transport

    @property
    def scrape_interval(self) -> float:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Fastest read_interval among all members — drives the scrape cadence."""
        intervals: list[float] = [m.read_interval for m in self.members if m.read_interval > 0]
        return min(intervals) if intervals else 0.0

    def members_due(self, now: float) -> list[transport_base]:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Returns members whose own read_interval has elapsed since last forward."""
        return [
            m for m in self.members
            if m.read_interval > 0
            and now - self._member_last_forward[m.transport_name] >= m.read_interval
        ]

    def mark_forwarded(self, transport: transport_base, now: float) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Record ``now`` as the last time ``transport``'s data was forwarded to its bridges.

        Used by ``members_due`` to determine when a member's ``read_interval``
        has elapsed and it is eligible to receive the next scrape result.
        """
        self._member_last_forward[transport.transport_name] = now

@dataclass
class TransportState:
    """
    Scheduling path: Interleaved only.

    Carries the result of one transport's interleaved read cycle.
    Created by _process_transports_interleaved, consumed by
    _route_interleaved_state.

    group, when set, means this state represents ONE consolidated physical
    read for an entire ScrapeGroup — transport is the group's primary,
    and the read was done via transport.read_group_data_iter(group.members)
    rather than transport.read_data_iter(). This is what makes interleaved
    mode do one read per physical device even when several scraper
    transports (e.g. a read-only scraper and a write-focused transport on
    the same inverter) share it — mirroring the non-interleaved grouped
    path's read-once-forward-to-each-member behavior instead of every
    member independently re-reading the same hardware. When group is None,
    transport was read standalone via read_data_iter() and get_partial_data()
    on transport itself is the whole story.
    """
    transport: transport_base
    group: "ScrapeGroup | None" = None
    completed_cleanly: bool = False
    error: Exception | None = None


@dataclass
class InterleavedCycleState:
    """
    Scheduling path: Interleaved only.

    Tracks one active interleaved cycle running on the shared executor.
    """
    cycle_done: threading.Event
    future_to_state: dict[Future[TransportState], TransportState]
    started_at: float
    overall_timeout: float
    cycle_now: float
    ready_groups: list["ScrapeGroup"]
    timed_out: bool = False


class Protocol_Gateway:
    """
    Main class, implementing the Inverters to MQTT/Database functionality
    """
    _logging_initialized = False
    _messaging_initialized: bool = False

    @classmethod
    def _setup_logging(cls, cfg: ConfigParser) -> None:
        """Scheduling path: N/A — setup, runs once regardless of read_mode.

        Configure the root logger from the ``[logging]`` section of ``cfg``. Class-level no-op after first call.

        Reads ``level``, ``log_dir``, ``log_file``, ``rotation``, ``backup_count``,
        ``when``, ``interval``, and ``max_bytes`` from ``[logging]``, falling back
        to ``[general].log_level`` and sensible defaults where keys are absent.
        Supports three rotation strategies: ``weekly`` (``TimedRotatingFileHandler``
        on a configurable weekday), ``daily`` (every 24 hours), and ``size``
        (``RotatingFileHandler`` with a byte cap).  Any other value falls back to
        a plain ``StreamHandler``.  Optionally adds a second console handler when
        ``[logging].console = true``.
        """
        if cls._logging_initialized:
            return

        # Read logging config
        # Single source of truth for runtime logger threshold:
        level_name: str = cfg.get(
            "logging",
            "level",
            fallback=cfg.get("general", "log_level", fallback="INFO"),
        ).strip().upper()
        level: int = getattr(logging, level_name, logging.INFO)

        log_dir = Path(cfg.get("logging", "log_dir", fallback="logs"))
        log_file: str = cfg.get("logging", "log_file", fallback="MPG.log")

        rotation: str = cfg.get("logging", "rotation", fallback="weekly").lower()
        backup_count: int = cfg.getint("logging", "backup_count", fallback=4)
        # fallback specific weekday rotation on a Monday
        when: str = cfg.get("logging", "when", fallback="W0")
        interval: int = cfg.getint("logging", "interval", fallback=1)
        max_bytes: int = cfg.getint("logging", "max_bytes", fallback=100 * 1024 * 1024)

        log_dir.mkdir(parents=True, exist_ok=True)
        log_path: Path = log_dir / log_file

        # ---- Choose handler ----
        if rotation == "weekly":
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=log_path,
                when=when,
                interval=interval,
                backupCount=backup_count,
                utc=True,
                encoding="utf-8",
            )

        elif rotation == "daily":
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=log_path,
                when="D",
                interval=1,
                backupCount=backup_count,
                utc=True,
                encoding="utf-8",
            )

        elif rotation == "size":
            handler = logging.handlers.RotatingFileHandler(
                filename=log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )

        else:
            # Fallback: console only
            handler = logging.StreamHandler()

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
        handler.setFormatter(formatter)

        # ---- Root logger wiring ----
        root: logging.Logger = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()
        root.addHandler(handler)

        # Optional console logging
        if cfg.getboolean("logging", "console", fallback=False):
            console: logging.StreamHandler[TextIO] = logging.StreamHandler()
            console.setFormatter(formatter)
            root.addHandler(console)

        cls._logging_initialized = True

    @classmethod
    def _setup_messaging(cls, cfg: ConfigParser) -> None:
        """
        Scheduling path: N/A — setup, runs once regardless of read_mode.

        Initialise the application-wide messaging subsystem.

        Reads the [messages] section of config.cfg and wires up every
        enabled notification service (Pushover, Telegram, …).  Mirrors
        _setup_logging: safe to call multiple times, no-op after first
        successful initialization.
        """
        if cls._messaging_initialized:
            return
        MessageHandler.setup(cfg)
        cls._messaging_initialized = True

    @staticmethod
    def _compact_thread_label(label: str, max_length: int = 80) -> str:
        """Scheduling path: Concurrent, Interleaved (the two modes that use thread pools).

        Truncate ``label`` to ``max_length`` characters, appending ``...`` if truncated.

        Keeps thread names visible in debugger thread lists and OS views within
        a predictable width.  Labels at or below ``max_length`` are returned unchanged.
        """
        if len(label) <= max_length:
            return label
        return label[: max_length - 3] + "..."

    @staticmethod
    def _base_thread_name(thread_name: str) -> str:
        """Scheduling path: Concurrent, Interleaved (the two modes that use thread pools).

        Return the stable base name of a worker thread by stripping any trailing ``[task]`` suffix.

        Worker threads are temporarily renamed to ``BaseName [task_label]`` by
        ``_run_with_thread_task_name``.  This method reverses that so the
        original pool name can be restored after the task completes.
        """
        return thread_name.split(" [", 1)[0]

    @staticmethod
    def _display_transport_name(transport_name: str) -> str:
        """Scheduling path: Concurrent, Interleaved (the two modes that use thread pools).

        Strip the ``transport.`` config-section prefix from ``transport_name`` for compact display.

        Config section names follow the pattern ``transport.<name>``.  Removing
        the prefix keeps thread labels and log messages short while still
        uniquely identifying the transport.
        """
        return transport_name.removeprefix("transport.")

    def _thread_task_label(self, read_mode: str, transport_names: list[str]) -> str:
        """Scheduling path: Concurrent, Interleaved (the two modes that use thread pools).

        Build a compact, human-readable task label from a list of transport names.

        Strips the ``transport.`` prefix from each name for brevity, joins them
        with commas, and truncates the result to the ``_compact_thread_label``
        limit.  Used as the bracketed suffix in worker thread names so debugger
        views show which transports a pooled thread is currently serving.
        ``read_mode`` is accepted for interface symmetry but not used.
        """
        del read_mode
        joined_names = ", ".join(
            self._display_transport_name(name) for name in transport_names
        ) if transport_names else "idle"
        return self._compact_thread_label(joined_names)

    def _run_with_thread_task_name(self, task_label: str, fn, *args, **kwargs):
        """Scheduling path: Concurrent, Interleaved (the two modes that use thread pools).

        Execute ``fn(*args, **kwargs)`` on the current thread with a temporary task-specific name.

        Appends ``[task_label]`` to the current worker thread's base name before
        calling ``fn``, then restores the original base name in a ``finally``
        block regardless of whether ``fn`` raises.  This makes it possible to
        identify which transport a pooled thread is serving in debugger and
        OS-level thread views without permanently renaming the worker.
        """
        thread: threading.Thread = threading.current_thread()
        base_name: str = self._base_thread_name(thread.name)
        thread.name = self._compact_thread_label(f"{base_name} [{task_label}]")
        try:
            return fn(*args, **kwargs)
        finally:
            thread.name = base_name

    __log : logging.Logger
    config_file : Path

    def __init__(self, config_file : str) -> None:
        """Scheduling path: N/A — setup, runs once regardless of read_mode (this is what decides read_mode).

        Initialise the gateway: load config, set up logging and messaging, instantiate and connect all transports.

        Resolves ``config_file`` relative to ``config/`` inside the project
        root, falling back to ``config/config.cfg`` if the requested file does
        not exist.  Reads the ``[general]`` section for ``read_mode``
        (``sequential`` | ``concurrent`` | ``interleaved``) and
        ``sequential_delay``, then iterates every ``[transport.*]`` section to
        dynamically import and instantiate the correct transport class.  After
        all transports are constructed and connected, bridge links are wired
        bidirectionally, reconnect hooks are attached, and scrape groups are
        built.  Thread-pool executors are created for ``concurrent`` and
        ``interleaved`` modes; ``sequential`` mode uses no executor.
        """
        self.__log: logging.Logger = logging.getLogger(__name__)

        # Establish the parent root folder where the gateway script is located.
        base_dir: Path = Path(__file__).resolve().parent

        default_cfg: Path = base_dir / "config" / "config.cfg"
        alternate_cfg: Path = base_dir /  "config" /config_file

        if alternate_cfg.is_file():
            self.config_file = alternate_cfg
        else:
            self.__log.warning(f"Config file not found {alternate_cfg}, using default: {default_cfg}")
            self.config_file = default_cfg

        self.__settings = CustomConfigParser()
        self.__settings.read(self.config_file.as_posix())

        self._setup_logging(self.__settings)
        self._setup_messaging(self.__settings)


        ##[general]
        self._read_mode_raw: str = self.__settings.get("general", "read_mode", fallback="sequential").strip().lower()

        # Read sequential delay setting
        self.__sequential_delay = float(self.__settings.getfloat("general", "sequential_delay", fallback=1.0) or 1.0)

        """
        Concurrent mode — fully parallel, correct when transports are on separate physical connections (separate IP addresses, separate serial ports)
        Sequential mode — read transports one by one with a delay in between, correct when multiple transports share a single bus (e.g. ModBus RTU over RS485)
        Interleaved mode - reads one block at a time from each transport in round-robin order, ideal for shared bus scenarios where some
            devices are significantly slower than others, preventing starvation of faster devices.  Parallel threads for I/O isolation,
            but a shared bus lock for transports that share the same physical pipe

        In an RS-485 network, the client sequentially polls servers one by one, waiting for a response before moving on
        to the next device. Only one device can talk on the RS-485 network at a time. And if slave devices are on the same
        serial line, they must be accessed sequentially because Modbus does not allow concurrent access — you query slave ID 1 first,
        then ID 2 and so forth.

        TCP-to-RTU bridge: The gateway maps the TCP Unit Identifier to the RTU slave address. So when MPG sends
        a Modbus TCP request with Unit ID 2, the Waveshare device translates that into an RTU poll directed specifically
        at slave 2 on the RS-485 bus — it does not broadcast to all slaves simultaneously. Each slave ID poll is a separate
        serial transaction. Transports that differ on slave_id are separate physical devices on the RS-485 bus and must be polled
        independently, even though they share the same Waveshare IP address.
            """

        # Validate and normalize — unknown values fall back to sequential
        if self._read_mode_raw not in ("sequential", "concurrent", "interleaved"):
            self.__log.warning(f"Unknown read_mode '{self._read_mode_raw}' — defaulting to 'sequential'.")
            self._read_mode_raw = "sequential"

        self.__read_mode: str = self._read_mode_raw
        self.__log.info(f"Transport scheduling mode: {self.__read_mode}")

        # Sequential delay only applies in sequential mode
        self.__sequential_delay: float = float(self.__settings.getfloat("general", "sequential_delay", fallback=1.0) or 1.0)
        if self.__read_mode == "sequential":
            self.__log.info(f"Sequential delay between transports: {self.__sequential_delay} seconds")

        self.__log.info("Loading...")

        self.__transports : list[transport_base] = []
        ''' transport_base is for type hinting. this can be any transport'''

        self.__running : bool = False
        ''' controls main loop'''

        for section in self.__settings.sections():
            transport_cfg: TransportSettings = cast(TransportSettings, self.__settings[section])
            transport_type: str      = transport_cfg.get("transport", fallback="")
            protocol_version: str    = transport_cfg.get("protocol_version", fallback="")

            # Process sections that either start with "transport" OR have a transport field
            if section.startswith("transport") or transport_type:
                if not transport_type and not protocol_version:
                    raise ValueError("Missing Transport / Protocol Version")

                if not transport_type and protocol_version:
                    transport_type = protocol_settings.get_transport_type(protocol_version)

                    if not transport_type:
                        # 1. Assign f-strings to variables first
                        msg: str = f"Cannot determine transport type for protocol {protocol_version}. " \
                            f"Ensure the protocol JSON contains a transport or reader key."

                        # 2. Raise the exception with the variable
                        raise ValueError(msg)


                # Import the module
                module = importlib.import_module("classes.transports."+transport_type)
                # Get the class from the module  (cls shadows built in so renamed)
                transport_cls = getattr(module, transport_type)
                transport : transport_base = transport_cls(transport_cfg)

                transport.on_message = self.on_message
                self.__transports.append(transport)

        #connect first
        for transport in self.__transports:
            self.__log.info("Connecting to "+str(transport.type)+":" +str(transport.transport_name)+"...")
            transport.connect()

        time.sleep(0.7)
        #apply links  updated to support multiple bridges per transport, and multiple transports per bridge.
        # The loop checks all combinations of transports for matching bridge/transport_name to establish links in both directions.
        for to_transport in self.__transports:
            for bridge_name in to_transport.bridges:
                for from_transport in self.__transports:
                    if bridge_name == from_transport.transport_name:
                        to_transport.init_bridge(from_transport)
                        from_transport.init_bridge(to_transport)

        self._wire_reconnect_hooks()

        # Interleaved cycle guard — keyed by frozenset of transport names.
        # Value is a threading.Event that is set when the FULL cycle
        # (both IL_Read pool threads and IL_Route thread) has completed.
        # An unset Event means a cycle is still in progress.
        self.__il_active_cycles: dict[frozenset[str], InterleavedCycleState] = {}
        self.__il_cycles_lock: threading.Lock = threading.Lock()
        self.__interleaved_executor: ThreadPoolExecutor | None = None

        # Build scrape groups — transports sharing the same physical endpoint
        # are consolidated so the device is only scraped once per cycle.
        self.__scrape_groups: list[ScrapeGroup] = self._build_scrape_groups()
        # Inform all transports how many scrapers are active. Bridges use this
        # to size any resources that scale with concurrent data sources.
        _scraper_count: int = sum(
            1 for t in self.__transports if t.read_interval > 0
        )
        for _t in self.__transports:
            _t.scraper_count = _scraper_count

        self.__concurrent_executor: ThreadPoolExecutor | None = None
        self.__concurrent_futures: dict[str, Future[None]] = {}
        self.__concurrent_futures_lock: threading.Lock = threading.Lock()
        if self.__read_mode == "concurrent" and self.__scrape_groups:
            self.__concurrent_executor = ThreadPoolExecutor(
                max_workers=len(self.__scrape_groups),
                thread_name_prefix="Con_Read",
            )
        if self.__read_mode == "interleaved":
            # number of active transports
            interleaved_workers: int = max(
                1,
                sum(1 for transport in self.__transports if transport.read_interval > 0),
            )
            self.__interleaved_executor = ThreadPoolExecutor(
                max_workers=interleaved_workers,
                thread_name_prefix="IL_Read",
            )
        self.__log.info(
            f"Scrape groups: {len(self.__scrape_groups)} "
            f"({'consolidated' if any(len(g.members) > 1 for g in self.__scrape_groups) else 'all standalone'})"
        )
        for group in self.__scrape_groups:
            if len(group.members) > 1:
                self.__log.info(
                    f"Scrape group [{group.primary.scrape_target}]: "
                    f"primary='{group.primary.transport_name}', "
                    f"members={[m.transport_name for m in group.members]}, "
                    f"scrape_interval={group.scrape_interval}s"
                )

    def on_message( self, from_transport: transport_base, entry: registry_map_entry, data: int | float | str) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — called from within any transport's decode path regardless of read_mode.

        Handle a single decoded register value and write it immediately to the paired bridge.

        Called by a scraper transport as each register is decoded, before a full
        cycle is complete.  Walks ``__transports`` to find the first transport
        that is bridged to ``from_transport`` (in either direction) and calls
        its ``write_data`` with a single-key dict ``{entry.variable_name: data}``.
        Skips ``from_transport`` itself and stops after the first matching bridge.
        """
        for to_transport in self.__transports:
            if to_transport is from_transport:
                continue

            if self._are_bridged(from_transport, to_transport):
                to_transport.write_data({entry.variable_name: data}, from_transport)
                break

    def _process_group_read(self, group: ScrapeGroup, now: float) -> None:
        """
        Scheduling path: Sequential, Concurrent (called directly by ``run()`` in
        sequential mode; called via ``_submit_concurrent_group_read`` in
        concurrent mode). Not used by interleaved mode — see
        ``_process_transports_interleaved``/``_route_interleaved_state`` for
        the interleaved equivalent.

        Performs one scrape via the group primary and routes the results
        to each member's bridge(s) for members whose read_interval is due.
        The gateway only schedules and routes; transports own the read path.
        """
        primary: transport_base = group.primary
        try:
            if not primary.connected:
                self.__log.info(f"Primary '{primary.transport_name}' not connected, connecting...")
                primary.connect()

            self.__log.debug(f"Scraping [{primary.scrape_target}] via '{primary.transport_name}'")
            full_data: dict[str, int | float | str] = primary.read_group_data(group.members)
            cycle_complete: bool = primary.cycle_is_complete_for_bridge()

            self._log_group_read_diagnostics(group, full_data)

            if not full_data:
                self.__log.warning(f"No data from [{primary.scrape_target}] - device may be unresponsive.")
                return

            due_members: list[transport_base] = group.members_due(now)
            self.__log.debug(
                f"'{primary.transport_name}' completed "
                f"({'complete' if cycle_complete else 'partial'}) "
                f"with {len(full_data)} metrics."
            )
            self.__log.debug(
                f"Group members due for '{primary.transport_name}': "
                f"{[m.transport_name for m in due_members]}"
            )

            for member in due_members:
                member_data: dict[str, int | float | str] = self._filter_for_member(full_data, member)

                self._log_mask_diagnostics(member, full_data, member_data)

                if not member_data:
                    continue

                for bridge_name in member.bridges:
                    bridge: transport_base | None = next(
                        (t for t in self.__transports if t.transport_name == bridge_name),
                        None,
                    )
                    if bridge is None:
                        self.__log.warning(
                            f"Bridge '{bridge_name}' not found for '{member.transport_name}'."
                        )
                        continue
                    if (
                        getattr(bridge, 'write_requires_complete_cycle', False)
                        and not cycle_complete
                    ):
                        self.__log.warning(
                            f"Skipping '{bridge_name}' for '{member.transport_name}' - cycle incomplete."
                        )
                        continue
                    self.__log.debug(
                        f"Writing to bridge {bridge_name} for member {member.transport_name} "
                        f"device_identifier={member.device_identifier} "
                        f"keys=[{', '.join(repr(k) for k in list(member_data.keys())[:3])}"
                        f"{', ...' if len(member_data) > 3 else ''}]"
                    )
                    self._snapshot_scraper_data(member, member_data)
                    bridge.write_data(member_data, member)

                group.mark_forwarded(member, now)

        except Exception as err:
            self.__log.exception(f"Error reading group [{primary.scrape_target}]: {err}")
            err_code = str(getattr(err, 'errno', ''))
            match err_code:
                case (NetworkError.CONN_RESET.value | NetworkError.BROKEN_PIPE.value |
                    NetworkError.TIMED_OUT.value | NetworkError.CONN_REFUSED.value |
                    NetworkError.NET_UNREACHABLE.value | NetworkError.HOST_UNREACHABLE.value):
                    # Network-related error — attempt to reconnect the primary transport on the next cycle
                    self.__log.info(f"Primary '{primary.transport_name}' not connected, trying to connect...")
                    primary.connect()

    def _log_group_read_diagnostics(
        self,
        group: ScrapeGroup,
        full_data: dict[str, int | float | str],
    ) -> None:
        """
        Scheduling path: Sequential, Concurrent (called only from
        ``_process_group_read``). Interleaved mode's consolidated reads
        aren't covered by this method — its per-member breakdown happens
        inline in ``_route_interleaved_state`` instead.

        Heavy, per-cycle DEBUG dump of exactly what a grouped read produced,
        broken down member-by-member, so a "why isn't metric X showing up"
        question can be answered by reading the log instead of re-deriving
        the read/mask/decode pipeline from source every time.

        Guarded by isEnabledFor(logging.DEBUG) since building these sets is
        real work (full registry-map scans per member, per cycle) that
        should cost nothing when DEBUG isn't enabled.

        For each member this logs three distinct sets, which correspond to
        three distinct places a metric can be lost:

        * ``mask`` — what the member's variable_mask file asks for (or, with
          no mask file, the member's own registry_map variable names). If a
          name is missing here, it's a config problem (typo in the mask
          file, or the name isn't spelled the way you think it is) — nothing
          downstream is even going to try for it.
        * ``requested`` — the subset of ``mask`` that is actually present in
          member.protocolSettings.registry_map[registry_type] for *some*
          registry_type. If a name is in ``mask`` but not ``requested``, the
          mask/screen filtering at load time (protocol_settings.load__registry)
          dropped it before it ever became a register range — check the
          member's variable_screen file, and confirm send_input_register /
          send_holding_register / send_coil_register / send_discrete_register
          aren't quietly excluding the whole registry type for this member.
        * ``decoded`` — the subset of ``requested`` that actually shows up in
          ``full_data`` this cycle. If a name is in ``requested`` but not
          ``decoded``, the register was asked for but never came back
          decoded — that's a physical read problem (Modbus exception,
          disabled range — see modbus_base's per-range DEBUG logging — or a
          decode-time skip), not a configuration problem. This is the
          "requested but never actually read" bucket most worth checking
          first for a metric that's been missing since the mask was set up.

        Names present in ``mask`` but absent from both ``requested`` and
        ``decoded`` are logged explicitly as "never even requested" to make
        that distinction impossible to miss.
        """
        if not self.__log.isEnabledFor(logging.DEBUG):
            return

        self.__log.debug(
            f"Group [{group.primary.scrape_target}] read diagnostics — "
            f"primary='{group.primary.transport_name}' "
            f"members={[m.transport_name for m in group.members]} "
            f"full_data ({len(full_data)} keys): {sorted(full_data.keys())}"
        )

        for member in group.members:
            ps: protocol_settings | None = getattr(member, 'protocolSettings', None)

            if ps is not None and ps.variable_mask:
                mask: set[str] = set(ps.variable_mask)
            else:
                mask = {
                    entry.variable_name
                    for entries in getattr(member, 'registry_map', {}).values()
                    for entry in entries
                    if getattr(entry, 'variable_name', None)
                }

            requested: set[str] = {
                entry.variable_name
                for entries in getattr(member, 'registry_map', {}).values()
                for entry in entries
                if getattr(entry, 'variable_name', None) and entry.variable_name.lower() in mask
            }

            never_requested: set[str] = mask - requested
            requested_not_decoded: set[str] = requested - full_data.keys()

            send_flags: dict[str, bool] = {
                "input":    getattr(member, 'send_input_register',    True),
                "holding":  getattr(member, 'send_holding_register',  True),
                "coil":     getattr(member, 'send_coil_register',     True),
                "discrete": getattr(member, 'send_discrete_register', True),
            }

            self.__log.debug(
                f"  member='{member.transport_name}' read_interval={member.read_interval} "
                f"mask_file={getattr(ps, 'mask_file_name', None)!r} "
                f"screen_file={getattr(ps, 'screen_file_name', None)!r} "
                f"send_flags={send_flags} "
                f"mask({len(mask)})={sorted(mask)}"
            )
            if never_requested:
                self.__log.debug(
                    f"  member='{member.transport_name}': in mask but NEVER REQUESTED "
                    f"(not in this member's own registry_map for any registry_type — "
                    f"check variable_screen_{member.transport_name}.txt and the "
                    f"send_*_register flags above) "
                    f"({len(never_requested)}): {sorted(never_requested)}"
                )
            if requested_not_decoded:
                self.__log.debug(
                    f"  member='{member.transport_name}': REQUESTED but not decoded this cycle "
                    f"(register was in range but came back with no value — physical read/"
                    f"decode issue, not a config issue; see modbus_base's per-range DEBUG log) "
                    f"({len(requested_not_decoded)}): {sorted(requested_not_decoded)}"
                )

    def _log_mask_diagnostics(
        self,
        member: "transport_base",
        full_data: dict[str, int | float | str],
        member_data: dict[str, int | float | str],
    ) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — called from ``_process_group_read`` and ``_route_interleaved_state``.

        Log mask-vs-data diagnostics for one member after filtering.

        Under the new paradigm both the variable mask and ``full_data`` use
        logical stem names (``_l``/``_h`` pairs have been merged upstream), so
        the diagnostic simply compares the mask set against the lowercased keys
        of ``full_data`` with no special suffix handling needed.

        Three paths mirror ``_filter_for_member``:

        * **Path 1** (explicit mask file) — logs a summary line and, when any
          mask entries have no corresponding key in the scraped data, a DEBUG
          warning listing the unmatched names.  Synthetic ``_desc`` keys that
          are present in the data are also noted so the caller can see that enum
          descriptions were forwarded even though they don't appear in the mask.
        * **Path 2** (registry-map fallback) — logs a brief count summary only.
        * **Path 3** (no mask) — logs that everything was forwarded as-is.

        All output is at DEBUG level; this method never warns or errors.
        """
        ps: protocol_settings | None = getattr(member, 'protocolSettings', None)

        if ps is not None and ps.variable_mask:
            # Path 1 — explicit mask file.
            mask: set[str] = set(ps.variable_mask)
            data_keys_lower: set[str] = {k.lower() for k in full_data}

            # Keys the mask asked for that don't appear in the scraped data at all.
            # Under the new paradigm both sides are stem names so no _l/_h
            # expansion is required — a genuine miss is a genuine miss.
            unmatched: set[str] = mask - data_keys_lower

            # Synthetic enum-description keys produced by the decoder
            # (e.g. "state_desc") — present in data but intentionally absent
            # from the mask; flag them so the operator knows they were forwarded.
            synthetic_desc: set[str] = {
                k for k in data_keys_lower
                if k.endswith('_desc') and k[:-5] in mask
            }

            self.__log.debug(
                f"Filtered data for '{member.transport_name}': "
                f"{len(mask)} mask keys in {ps.mask_file_name} "
                f"→ {len(member_data)} matched"
            )
            if unmatched:
                self.__log.debug(
                    f"Mask keys with no match in scraped data for "
                    f"'{member.transport_name}' "
                    f"({len(unmatched)} unmatched): {sorted(unmatched)}"
                )
            if synthetic_desc:
                self.__log.debug(
                    f"Synthetic _desc fields forwarded for "
                    f"'{member.transport_name}': {sorted(synthetic_desc)}"
                )

        elif ps is not None and any(ps.registry_map.values()):
            # Path 2 — registry-map fallback.
            member_keys: set[str] = {
                entry.variable_name
                for entries in member.registry_map.values()
                for entry in entries
                if hasattr(entry, 'variable_name') and entry.variable_name
            }
            self.__log.debug(
                f"Filtered data for '{member.transport_name}': "
                f"{len(member_keys)} registry map keys → {len(member_data)} matched"
            )

        else:
            # Path 3 — no mask configured.
            self.__log.debug(
                f"Filtered data for '{member.transport_name}': "
                f"no mask configured — forwarding all {len(member_data)} metrics"
            )

    def _filter_for_member(self, full_data: dict[str, int | float | str], member: transport_base) -> dict[str, int | float | str]:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — called from ``_process_group_read`` and ``_route_interleaved_state``.

        Filter ``full_data`` to only the metrics relevant to ``member``.

        Resolution order
        ----------------
        1. ``member.protocolSettings.variable_mask`` — the normalized allowlist
           loaded from the mask file.  By the time the mask reaches this method,
           ``protocol_settings._load_filter_file`` has already stripped any
           ``_l`` / ``_h`` suffixes so every entry is a logical (combined) stem
           name (e.g. ``echg_all`` rather than ``echg_all_l``).  Likewise,
           ``load__registry`` has already merged every ``_l`` / ``_h`` register
           pair in ``full_data`` into a single combined entry under that same
           stem name.  The filter is therefore a straightforward stem-to-stem
           comparison with no special-case expansion needed.

           Synthetic ``<name>_desc`` keys produced by enum decoders are also
           forwarded when their base name (``<name>``) is in the mask — these
           are generated after masking and never appear in the mask file itself.

        2. ``member.registry_map`` variable names — derived from the masked
           registry map when no explicit mask file was loaded.

        3. Forward everything — no mask configured at all.

        Synthetic fields
        ----------------
        Keys listed in ``member.synthetic_field_names`` are always forwarded
        on paths 1 and 2, regardless of mask or registry map contents.  These
        are fields injected by ``post_process_data`` that have no corresponding
        row in the protocol CSV and therefore cannot appear in any mask file.
        Path 3 already forwards everything, so no special handling is needed.
        """
        ps = getattr(member, 'protocolSettings', None)
        synthetic: frozenset[str] = member.synthetic_field_names

        # Path 1 — explicit variable mask.
        if ps is not None and ps.variable_mask:
            mask: set[str] = set(ps.variable_mask)
            return {
                k: v for k, v in full_data.items()
                if k.lower() in mask
                or (k.lower().endswith('_desc') and k.lower()[:-5] in mask)
                or k.lower() in synthetic
            }

        # Path 2 — fall back to the registry map variable names.
        member_keys: set[str] = set()
        for entries in member.registry_map.values():
            for entry in entries:
                if hasattr(entry, 'variable_name') and entry.variable_name:
                    member_keys.add(entry.variable_name)

        if not member_keys:
            return full_data  # Path 3 — no mask at all, forward everything

        return {
            k: v for k, v in full_data.items()
            if k in member_keys
            or k.lower() in synthetic
        }

    def _submit_concurrent_group_read(self, group: ScrapeGroup, now: float) -> None:
        """
        Scheduling path: Concurrent only.

        Submit one group read to the persistent concurrent executor.
        Reuses worker threads between successful cycles and prevents duplicate
        submissions while a prior cycle for the same scrape target is still
        running.
        """
        if self.__concurrent_executor is None:
            self._process_group_read(group, now)
            return

        group_key: str = group.primary.scrape_target or group.primary.transport_name
        task_label: str = self._thread_task_label(
            "concurrent",
            [member.transport_name for member in group.members],
        )
        with self.__concurrent_futures_lock:
            prior: Future[None] | None = self.__concurrent_futures.get(group_key)
            if prior is not None:
                if prior.done():
                    try:
                        prior.result()
                    except Exception as exc:
                        self.__log.error(f"Concurrent read future for '{group_key}' ended with error: {exc}")
                    self.__concurrent_futures.pop(group_key, None)
                else:
                    self.__log.debug(f"Concurrent read already in progress for '{group_key}' - skipping duplicate submit.")
                    return

            future: Future[None] = self.__concurrent_executor.submit(
                self._run_with_thread_task_name,
                task_label,
                self._process_group_read,
                group,
                now,
            )
            self.__concurrent_futures[group_key] = future

    def _are_bridged(self, a: transport_base, b: transport_base) -> bool:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — called from ``on_message``, independent of read_mode.

        Return ``True`` if transport ``a`` and ``b`` are linked as a bridge pair in either direction.

        A bridge relationship exists when ``b``'s transport name appears in
        ``a.bridges``, or ``a``'s transport name appears in ``b.bridges``.
        """
        return (
            b.transport_name in a.bridges
            or a.transport_name in b.bridges
        )

    def reconnect_upstream_bridge(self, transport_id: str) -> None:
        """
        Scheduling path: N/A — not part of the read-scheduling loop; a bridge-triggered callback, independent of read_mode.

        Callback for bridge transports to trigger a reconnect of their primary scraper when stale data is detected.
        """
        target: transport_base | None = next(
            (t for t in self.__transports if t.transport_name == transport_id), None
        )
        if target is None:
            self.__log.warning(f"Reconnect requested for unknown transport '{transport_id}'")
            return
        # Setting connected = False flows through the property setter which
        # handles logging, notification, and _needs_reconnection automatically.
        target.connected = False
        target.last_read_time = 0.0

    def _snapshot_scraper_data(
        self,
        scraper: "transport_base",
        data: dict[str, int | float | str],
    ) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — called from ``_process_group_read`` and ``_forward_to_bridges``.

        Cache the bridge-bound data on the scraper transport.

        Called immediately before every ``bridge.write_data(data, scraper)``
        call so ``scraper._last_known_data`` always reflects the most recent
        complete, bridge-confirmed cycle result — not a mid-cycle partial.

        Also sets and immediately clears ``scraper._values_ready_event`` so
        any thread blocked in ``/api/device/{name}/last-values/wait`` is
        woken and receives the fresh snapshot.
        """
        if not data:
            return
        scraper._last_known_data = dict(data)
        scraper._values_ready_event.set()
        # Clear immediately so the next wait() blocks until the next cycle.
        scraper._values_ready_event.clear()

    def get_transport(self, transport_name: str) -> "transport_base | None":
        """Scheduling path: N/A — not part of the read-scheduling loop; used by the web server, independent of read_mode.

        Return the transport instance with the given fully-qualified name.

        ``transport_name`` must include the ``transport.`` prefix as it appears
        in config.cfg (e.g. ``'transport.eg4_ll_s_2'``).  Used by the web
        server to access ``synthetic_fields_metadata`` from a live scraper
        transport so the protocol table can display synthetic metrics alongside
        CSV-derived register rows.

        Returns ``None`` if no transport with that name exists or has connected.
        """
        return next(
            (t for t in self.__transports if t.transport_name == transport_name),
            None,
        )

    # init the variable request_upstream_reconnect in the bridge __init__.  If it goes true during stale
    # detection, reconnect routine triggers.
    def _wire_reconnect_hooks(self) -> None:
        """Scheduling path: N/A — setup, runs once regardless of read_mode.

        Attach the gateway's ``reconnect_upstream_bridge`` callback to every bridge transport.

        Iterates all transports and identifies those acting as bridges — i.e.
        other transports list them by name in their ``bridges`` attribute.  For
        each such transport that exposes a ``request_upstream_reconnect``
        attribute (set to a placeholder in the bridge ``__init__``), replaces
        that placeholder with the gateway's ``reconnect_upstream_bridge`` method
        so the bridge can trigger a scraper reconnect when it detects stale data.
        """
        for transport in self.__transports:
            # Only wire on transports that declare themselves as bridges
            # i.e. they have no read_interval and other transports point at them
            is_bridge: bool = any(
                transport.transport_name in t.bridges
                for t in self.__transports
                if t is not transport
            )
            if is_bridge and hasattr(transport, "request_upstream_reconnect"):
                transport.request_upstream_reconnect = self.reconnect_upstream_bridge

    """
    The gateway detects that Device1 and Device2 share the same address (and same protocol_version),
    groups them into a scrape group, designates one as the primary (lowest read_interval), and the
    others as subscribers that receive forwarded data from the primary's scrape at their own read_interval cadence.
    """
    def _build_scrape_groups(self) -> list[ScrapeGroup]:
            """
            Scheduling path: All (Sequential, Concurrent, Interleaved) — setup, runs
            once regardless of read_mode; the resulting groups are what all three
            paths schedule against.

            Groups scraper transports by scrape_target.
            Transports with no scrape_target (bridges) are excluded.
            Each unique scrape_target gets one ScrapeGroup. The primary is
            the member with the shortest read_interval.
            """
            groups: dict[str, ScrapeGroup] = {}

            for transport in self.__transports:
                target: str = transport.scrape_target
                if not target:
                    continue  # bridge transport, not a scraper

                if target not in groups:
                    groups[target] = ScrapeGroup(transport)
                else:
                    groups[target].add_member(transport)

            # Wrap single-member targets as groups too for uniform handling
            return list(groups.values())

    def _poll_interleaved_cycles(self) -> None:
            """
            Scheduling path: Interleaved only.

            Route completed interleaved reads and retire cycles only after
            all worker futures have really finished.
            """
            with self.__il_cycles_lock:
                active_cycles: list[tuple[frozenset[str], InterleavedCycleState]] = list(
                    self.__il_active_cycles.items()
                )

            if not active_cycles:
                return

            current_time: float = time.time()

            for cycle_key, cycle in active_cycles:
                completed_futures: list[tuple[Future[TransportState], TransportState]] = []
                pending_futures: list[tuple[Future[TransportState], TransportState]] = []

                for future, state in list(cycle.future_to_state.items()):
                    if future.done():
                        completed_futures.append((future, state))
                    else:
                        pending_futures.append((future, state))

                for future, _state in completed_futures:
                    try:
                        completed_state: TransportState = future.result()
                        if not cycle.timed_out:
                            self._route_interleaved_state(
                                completed_state,
                                cycle.cycle_now,
                                cycle.ready_groups,
                            )
                    except CancelledError:
                        pass
                    except Exception as e:
                        self.__log.error(f"Error routing interleaved state: {e}")
                    finally:
                        cycle.future_to_state.pop(future, None)

                if (
                    not cycle.timed_out
                    and pending_futures
                    and current_time - cycle.started_at > cycle.overall_timeout
                ):
                    cycle.timed_out = True
                    for future, state in pending_futures:
                        if not future.done():
                            future.cancel()
                            self.__log.warning(
                                f"Transport '{state.transport.transport_name}' cycle timed out - evicting."
                            )
                            state.transport._bus_lock = None

                if cycle.future_to_state:
                    continue

                cycle.cycle_done.set()
                with self.__il_cycles_lock:
                    self.__il_active_cycles.pop(cycle_key, None)

    def _process_transports_interleaved(self, transports: list[transport_base], now: float, ready_groups: list[ScrapeGroup],) -> None:
        """
        Scheduling path: Interleaved only.

        Reads transports in parallel on the shared interleaved executor,
        consolidating multi-member ScrapeGroups into one physical read each.

        Transports on separate physical endpoints (different scrape_targets)
        run entirely in parallel with no cross-transport waiting.

        A transport that is the sole member of its own group, or belongs to
        no group at all, is read standalone via read_data_iter() — one task,
        one transport, same as before.

        A transport that shares a multi-member ScrapeGroup with at least one
        other due transport is NOT read on its own. Instead, exactly one task
        per such group is submitted, calling group.primary.read_group_data_iter
        (group.members) — the same "read once via the primary, decode into
        every member's own state" consolidation the non-interleaved grouped
        path (read_group_data) already does. This is what interleaved mode's
        second design goal (one physical read per piece of hardware, however
        many scraper transports reference it, to avoid extra wear/collisions)
        actually requires; reading every member independently defeats that
        goal even though it happens to still produce correct-looking data for
        whichever member does its own read successfully.

        Transports sharing a physical bus (same scrape_target) serialize
        their individual block reads automatically via _bus_lock inside
        read_modbus_registers_iter.

        Completed reads are routed from the main loop poller so worker
        threads can be reused across cycles and the guard is only released
        after the submitted futures have actually finished.
        """
        if not transports:
            return

        cycle_key: frozenset[str] = frozenset(t.transport_name for t in transports)
        with self.__il_cycles_lock:
            for active_key, prior_cycle in self.__il_active_cycles.items():
                if prior_cycle.cycle_done.is_set():
                    continue
                overlapping_names: list[str] = sorted(active_key & cycle_key)
                if not overlapping_names:
                    continue
                self.__log.warning(
                    f"Interleaved cycle still running for [{', '.join(overlapping_names)}] - "
                    f"skipping this tick to avoid overlapping reads and thread accumulation. "
                    f"Consider increasing read_interval if this recurs."
                )
                return

            cycle_done = threading.Event()

        bus_locks: dict[str, threading.Lock] = {}
        for transport in transports:
            wire_key: str = getattr(transport, 'host', '') + ':' + str(getattr(transport, 'port', ''))
            if not wire_key.strip(':'):
                wire_key = transport.transport_name
            if wire_key not in bus_locks:
                bus_locks[wire_key] = threading.Lock()

        for transport in transports:
            wire_key = getattr(transport, 'host', '') + ':' + str(getattr(transport, 'port', ''))
            if not wire_key.strip(':'):
                wire_key = transport.transport_name
            transport._bus_lock = bus_locks[wire_key]

        # Partition this tick's due transports into consolidated group
        # work-units vs standalone reads. A group only counts as a
        # consolidation target here if it has more than one member — a
        # "group" of one is just the primary, and read_group_data_iter would
        # do nothing read_data_iter doesn't already do, so there's no reason
        # to take the extra code path for it.
        groups_to_read: list[ScrapeGroup] = []
        seen_group_ids: set[int] = set()
        standalone: list[transport_base] = []
        for t in transports:
            owning_group: ScrapeGroup | None = next(
                (g for g in ready_groups if len(g.members) > 1 and t in g.members),
                None,
            )
            if owning_group is None:
                standalone.append(t)
                continue
            if id(owning_group) not in seen_group_ids:
                seen_group_ids.add(id(owning_group))
                groups_to_read.append(owning_group)

        states: list[TransportState] = (
            [TransportState(transport=g.primary, group=g) for g in groups_to_read]
            + [TransportState(transport=t) for t in standalone]
        )
        if groups_to_read:
            self.__log.debug(
                f"Interleaved tick: {len(groups_to_read)} consolidated group read(s) "
                f"{[(g.primary.transport_name, [m.transport_name for m in g.members]) for g in groups_to_read]}, "
                f"{len(standalone)} standalone: {[t.transport_name for t in standalone]}"
            )

        def run_transport(state: TransportState) -> TransportState:
            """Scheduling path: Interleaved only.

            Drain ``state.transport``'s read generator to completion and update ``state`` with the outcome.

            Runs ``read_group_data_iter(state.group.members)`` when ``state.group``
            is set (one consolidated physical read decoded into every member's
            own partial data), otherwise the plain ``read_data_iter()``.  On
            success sets ``state.completed_cleanly = True``.  On exception,
            records the error, marks the cycle incomplete on the transport, and
            clears ``_bus_lock`` so the lock is not held after failure.
            """
            try:
                # Mirror the connection guard from _process_group_read so that
                # disconnected transports attempt reconnect before reading rather
                # than grinding through a full cycle of dead reads.  Without this
                # guard interleaved mode would spin through every register range
                # on a dead connection — wasting the cycle and relying entirely on
                # modbus_base to suppress the resulting register failure counts.
                if not state.transport.connected:
                    self.__log.info(
                        f"'{state.transport.transport_name}' not connected "
                        f"— attempting reconnect before interleaved read."
                    )
                    state.transport.connect()
                    if not state.transport.connected:
                        # Still not connected — skip this cycle entirely.
                        # The main loop will retry on the next scrape_interval tick.
                        self.__log.warning(
                            f"'{state.transport.transport_name}' reconnect failed "
                            f"— skipping read cycle."
                        )
                        return state

                if state.group is not None:
                    for _ in state.transport.read_group_data_iter(state.group.members):
                        pass
                else:
                    for _ in state.transport.read_data_iter():
                        pass
                state.completed_cleanly = True
            except Exception as exc:
                state.error = exc
                state.transport._cycle_mark_incomplete()
                state.transport._finish_cycle_tracking(state.transport.get_partial_data())
                self.__log.error(f"Unhandled error in '{state.transport.transport_name}': {exc}")
            finally:
                state.transport._bus_lock = None
            return state

        overall_timeout: float = max(
            (state.transport.interleaved_cycle_timeout() for state in states),
            default=60.0,
        )
        # Human-readable suffix for thread names visible in debugger
        name_suffix: str = (
            f"{states[0].transport.transport_name} +{len(states) - 1}"
            if len(states) > 1
            else states[0].transport.transport_name
        )

        if self.__interleaved_executor is None:
            self.__interleaved_executor = ThreadPoolExecutor(
                max_workers=max(1, len(states)),
                thread_name_prefix=f"IL_Read [{name_suffix}]",
            )

        future_to_state: dict[Future[TransportState], TransportState] = {
            self.__interleaved_executor.submit(
                self._run_with_thread_task_name,
                self._thread_task_label("interleaved", [state.transport.transport_name]),
                run_transport,
                state,
            ): state
            for state in states
        }
        with self.__il_cycles_lock:
            self.__il_active_cycles[cycle_key] = InterleavedCycleState(
                cycle_done=cycle_done,
                future_to_state=future_to_state,
                started_at=time.time(),
                overall_timeout=overall_timeout,
                cycle_now=now,
                ready_groups=ready_groups,
            )

    def _forward_to_bridges(
        self,
        member: transport_base,
        member_data: dict[str, int | float | str],
        cycle_complete: bool,
    ) -> None:
        """Scheduling path: Interleaved only.

        Writes member_data to every bridge configured on member.

        Shared by both branches of _route_interleaved_state (consolidated
        group member and standalone transport) so the bridge lookup,
        write_requires_complete_cycle gating, and logging stay identical
        regardless of which read path produced member_data.
        """
        for bridge_name in member.bridges:
            bridge: transport_base | None = next(
                (t for t in self.__transports if t.transport_name == bridge_name),
                None,
            )
            if bridge is None:
                self.__log.warning(f"Bridge '{bridge_name}' not found for '{member.transport_name}'.")
                continue
            if (
                getattr(bridge, 'write_requires_complete_cycle', False)
                and not cycle_complete
            ):
                self.__log.warning(f"Skipping '{bridge_name}' for '{member.transport_name}' - cycle incomplete.")
                continue
            self.__log.debug(
                f"Writing to bridge {bridge_name} for member {member.transport_name} "
                f"device_identifier={member.device_identifier} "
                f"keys=[{', '.join(repr(k) for k in list(member_data.keys())[:3])}"
                f"{', ...' if len(member_data) > 3 else ''}]"
            )
            self._snapshot_scraper_data(member, member_data)
            bridge.write_data(member_data, member)

    def _route_interleaved_state(self, state: TransportState, now: float, ready_groups: list["ScrapeGroup"],) -> None:
        """Scheduling path: Interleaved only.

        Forward one completed interleaved read to its bridge(s).

        Called by ``_poll_interleaved_cycles`` as each worker future resolves.

        state.group set (consolidated group read): state.transport (the
        group's primary) just ran ONE physical read via
        read_group_data_iter(), which decoded every member's own data into
        that member's own get_partial_data() (see transport_base.
        read_group_data_iter — it updates member._partial_info per member,
        not just the primary's). So each due member is filtered and
        forwarded against ITS OWN data here — never the primary's reused
        wholesale — which is what makes a metric that only exists on a
        non-primary member's mask (e.g. a write-focused transport's own
        holding-register setpoints the primary never reads at all) match
        correctly instead of being reported "unmatched" forever regardless
        of whether that member's own read actually succeeded.

        state.group is None (standalone read): state.transport ran its own
        read_data_iter() with nothing to consolidate; filter and forward its
        own data the same way.

        cycle_complete is read from state.transport (the primary, for a
        consolidated read) and reused for every member's bridge-write
        gating, mirroring _process_group_read's non-interleaved behavior —
        the group shares one physical read, so it shares one completeness
        verdict.
        """
        transport: transport_base = state.transport
        cycle_complete: bool = transport.cycle_is_complete_for_bridge()

        if state.group is not None:
            due_members: list[transport_base] = state.group.members_due(now)
            self.__log.debug(
                f"Group [{transport.scrape_target}] consolidated read via "
                f"'{transport.transport_name}' "
                f"({'complete' if cycle_complete else 'partial'}) - "
                f"due members: {[m.transport_name for m in due_members]}"
            )
            for member in due_members:
                member_own_data: dict[str, int | float | str] = member.get_partial_data()
                if not member_own_data:
                    self.__log.warning(f"'{member.transport_name}' produced no data this cycle.")
                    continue
                member_data: dict[str, int | float | str] = self._filter_for_member(member_own_data, member)
                self._log_mask_diagnostics(member, member_own_data, member_data)
                if not member_data:
                    continue
                self._forward_to_bridges(member, member_data, cycle_complete)
                state.group.mark_forwarded(member, now)
        else:
            data: dict[str, int | float | str] = transport.get_partial_data()
            if not data:
                self.__log.warning(f"'{transport.transport_name}' produced no data this cycle.")
                return

            self.__log.debug(
                f"'{transport.transport_name}' completed "
                f"({'complete' if cycle_complete else 'partial'}) "
                f"with {len(data)} metrics."
            )

            member_data = self._filter_for_member(data, transport)
            self._log_mask_diagnostics(transport, data, member_data)

            if not member_data:
                return

            self._forward_to_bridges(transport, member_data, cycle_complete)

            # transport wasn't part of a consolidated group read this cycle,
            # but it may still nominally belong to a group (a solo-member
            # group, or a group where none of its siblings happened to be
            # due) — update its own forward timestamp either way so
            # members_due() reflects it was just serviced.
            for g in ready_groups:
                if transport in g.members:
                    g.mark_forwarded(transport, now)
                    break

    def run(self) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — this is the dispatcher that reads __read_mode and picks one.

        Start the main polling loop and block until the gateway is stopped.

        On each tick, calls ``_poll_interleaved_cycles`` to service any
        in-flight interleaved futures, then identifies scrape groups whose
        ``scrape_interval`` has elapsed and dispatches them according to
        ``__read_mode``:

        - ``concurrent``   — submits each group to the persistent thread pool
          via ``_submit_concurrent_group_read``.
        - ``interleaved``  — runs due group members directly on the interleaved
          executor via ``_process_transports_interleaved``.
        - ``sequential``   — reads each group in order via ``_process_group_read``
          with a configurable ``__sequential_delay`` between groups.

        Exceptions inside the inner loop are caught and logged so a single
        failing transport does not kill the process.  On exit (normal or
        exception), both thread-pool executors are shut down without waiting for
        pending futures.
        """
        self.__running = True

        if False:
            self.enable_write()

        try:
            while self.__running:
                        try:
                            self._poll_interleaved_cycles()
                            now: float = time.time()
                            ready_groups: list[ScrapeGroup] = []

                            for group in self.__scrape_groups:
                                if (group.scrape_interval > 0
                                        and now - group.primary.last_read_time >= group.scrape_interval):
                                    group.primary.last_read_time = now
                                    ready_groups.append(group)

                            match self.__read_mode:
                                case "concurrent":
                                    for group in ready_groups:
                                        self._submit_concurrent_group_read(group, now)

                                case "interleaved":
                                    # Each ScrapeGroup gets its own independent IL cycle.
                                    # Run due members directly in IL mode so member-specific
                                    # variable masks (e.g. write-focused holding registers)
                                    # are preserved and routed to each member's bridge.
                                    for group in ready_groups:
                                        due_members: list[transport_base] = group.members_due(now)
                                        if not due_members:
                                            continue
                                        self._process_transports_interleaved(
                                            due_members, now, ready_groups
                                        )

                                case _:  # sequential
                                    for i, group in enumerate(ready_groups):
                                        self._process_group_read(group, now)
                                        if i < len(ready_groups) - 1:
                                            time.sleep(self.__sequential_delay)

                        except Exception as err:
                            self.__log.exception("Unhandled exception in main loop")
                            self.__log.error(err)

                        time.sleep(0.07) #change this in future. probably reduce to allow faster reads.
        finally:
            if self.__concurrent_executor is not None:
                self.__concurrent_executor.shutdown(wait=False, cancel_futures=True)
            if self.__interleaved_executor is not None:
                self.__interleaved_executor.shutdown(wait=False, cancel_futures=True)


def main(args=None) -> None:
    """Scheduling path: N/A — CLI entry point, runs once before any read_mode is dispatched.

    Entry point: parse CLI arguments, resolve the config path, and start the gateway and web server.

    Accepts ``--config``/``-c <file>`` or a bare positional argument to name the
    config file; defaults to ``config.cfg``.  Resolves the path by walking up
    from ``__file__`` to find the project root (the directory containing
    ``protocol_gateway.py``), then reads ``[logging].log_file`` and
    ``[logging].log_dir`` to pass to ``start_webserver``.  The web server is
    started before ``mpg.run()`` so the HTTP interface is available immediately,
    even during the initial transport connection phase.
    """
    # Create ArgumentParser object
    parser = argparse.ArgumentParser(description="Multi Protocol Gateway")

    # Add arguments
    parser.add_argument("--config", "-c", type=str, help="Specify Config File")

    # Add a positional argument with default
    parser.add_argument("positional_config", type=str, help="Specify Config File", nargs="?", default="config.cfg")

    # Renamed this variable to 'parsed_args'
    parsed_args: argparse.Namespace = parser.parse_args(args)

    # Use the new variable name
    config_file: str = parsed_args.config if parsed_args.config else parsed_args.positional_config

    print(__logo)

    mpg = Protocol_Gateway(config_file)

    current_path: Path = Path(__file__).resolve()
    root: Path = current_path
    # Walk up the directory tree until we find a folder containing protocol_gateway.py, which we consider the project root
    # should already be in the correct folder but this is just a sanity check to ensure it works even if launched from a different CWD

    for parent in current_path.parents:
        if (parent / "protocol_gateway.py").exists():
            root = parent
            break

    config_path: Path = root / "config" / config_file
    config_parser = CustomConfigParser()
    config_parser.read(config_path.as_posix())
    log_file: str = config_parser.get("logging", "log_file", fallback="MPG.log")
    log_dir: str = config_parser.get("logging", "log_dir", fallback="logs")

    start_webserver(config_path, log_file, log_dir, gateway_instance=mpg)
    mpg.run()


if __name__ == "__main__":
    main()
