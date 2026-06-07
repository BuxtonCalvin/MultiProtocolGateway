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
        """Read a string value from the config, with alias support and comment stripping.

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
            """Call the parent ``ConfigParser.get`` and return ``None`` on any missing-key error."""
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
        """Read a config value and return it as an ``int``.

        Delegates to ``get``, inheriting alias-list and fallback support.
        Raises ``ValueError`` if the resolved string cannot be converted to an integer.
        """
        value: str = self.get(section, option, *args, **kwargs)
        return int(value)

    def getfloat(self, section: str, option: str | list[str], *args: Any, **kwargs: Any ) -> float:
        """Read a config value and return it as a ``float``.

        Delegates to ``get``, inheriting alias-list and fallback support.
        Raises ``ValueError`` if the resolved string cannot be converted to a float.
        """
        value: str = self.get(section, option, *args, **kwargs)
        return float(value)

    def getboolean(self, section: str, option: str | list[str], *args: Any, **kwargs: Any) -> bool:
        """Read a config value and return it as a ``bool``.

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
    Represents a set of scraper transports that all read from the same
    physical device (same scrape_target) and can therefore share a single
    Modbus read cycle.

    The primary transport performs the actual scrape using the union of all
    member variable masks at the fastest read_interval among the members.
    Each member transport then receives only the metrics relevant to it,
    forwarded to its own bridge(s) at its own read_interval cadence.
    """

    def __init__(self, primary: transport_base) -> None:
        """Initialise a single-member group with ``primary`` as the initial sole member.

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
        """Add ``transport`` to this group and promote it to primary if it has the shortest read interval.

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
        """Fastest read_interval among all members — drives the scrape cadence."""
        intervals: list[float] = [m.read_interval for m in self.members if m.read_interval > 0]
        return min(intervals) if intervals else 0.0

    def members_due(self, now: float) -> list[transport_base]:
        """Returns members whose own read_interval has elapsed since last forward."""
        return [
            m for m in self.members
            if m.read_interval > 0
            and now - self._member_last_forward[m.transport_name] >= m.read_interval
        ]

    def mark_forwarded(self, transport: transport_base, now: float) -> None:
        """Record ``now`` as the last time ``transport``'s data was forwarded to its bridges.

        Used by ``members_due`` to determine when a member's ``read_interval``
        has elapsed and it is eligible to receive the next scrape result.
        """
        self._member_last_forward[transport.transport_name] = now

@dataclass
class TransportState:
    """
    Carries the result of one transport's interleaved read cycle.
    Created by _process_transports_interleaved, consumed by
    _route_interleaved_state.
    """
    transport: transport_base
    completed_cleanly: bool = False
    error: Exception | None = None


@dataclass
class InterleavedCycleState:
    """
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
        """Configure the root logger from the ``[logging]`` section of ``cfg``. Class-level no-op after first call.

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
        """Truncate ``label`` to ``max_length`` characters, appending ``...`` if truncated.

        Keeps thread names visible in debugger thread lists and OS views within
        a predictable width.  Labels at or below ``max_length`` are returned unchanged.
        """
        if len(label) <= max_length:
            return label
        return label[: max_length - 3] + "..."

    @staticmethod
    def _base_thread_name(thread_name: str) -> str:
        """Return the stable base name of a worker thread by stripping any trailing ``[task]`` suffix.

        Worker threads are temporarily renamed to ``BaseName [task_label]`` by
        ``_run_with_thread_task_name``.  This method reverses that so the
        original pool name can be restored after the task completes.
        """
        return thread_name.split(" [", 1)[0]

    @staticmethod
    def _display_transport_name(transport_name: str) -> str:
        """Strip the ``transport.`` config-section prefix from ``transport_name`` for compact display.

        Config section names follow the pattern ``transport.<name>``.  Removing
        the prefix keeps thread labels and log messages short while still
        uniquely identifying the transport.
        """
        return transport_name.removeprefix("transport.")

    def _thread_task_label(self, read_mode: str, transport_names: list[str]) -> str:
        """Build a compact, human-readable task label from a list of transport names.

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
        """Execute ``fn(*args, **kwargs)`` on the current thread with a temporary task-specific name.

        Appends ``[task_label]`` to the current worker thread's base name before
        calling ``fn``, then restores the original base name in a ``finally``
        block regardless of whether ``fn`` raises.  This makes it possible to
        identify which transport a pooled thread is serving in debugger and
        OS-level thread views without permanently renaming the worker.
        """
        thread = threading.current_thread()
        base_name = self._base_thread_name(thread.name)
        thread.name = self._compact_thread_label(f"{base_name} [{task_label}]")
        try:
            return fn(*args, **kwargs)
        finally:
            thread.name = base_name

    __log : logging.Logger
    config_file : Path

    def __init__(self, config_file : str) -> None:
        """Initialise the gateway: load config, set up logging and messaging, instantiate and connect all transports.

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

        #pymodbus_log = logging.getLogger('pymodbus')
        #pymodbus_log.setLevel(logging.DEBUG)
        #pymodbus_log.addHandler(handler)

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
            but a shared bus lock for transports that share the same physical pipe"""

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
        """Handle a single decoded register value and write it immediately to the paired bridge.

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

            if not full_data:
                self.__log.warning(f"No data from [{primary.scrape_target}] - device may be unresponsive.")
                return

            for member in group.members_due(now):
                member_data = self._filter_for_member(full_data, member)
                if not member_data:
                    continue

                for bridge_name in member.bridges:
                    bridge: transport_base | None = next(
                        (t for t in self.__transports if t.transport_name == bridge_name),
                        None,
                    )
                    if bridge is not None:
                        if (
                            getattr(bridge, 'write_requires_complete_cycle', False)
                            and not primary.cycle_is_complete_for_bridge()
                        ):
                            self.__log.warning(
                                f"Skipping '{bridge_name}' for '{member.transport_name}' - cycle incomplete."
                            )
                            continue
                        bridge.write_data(member_data, member)
                        self.__log.debug(
                            f"Forwarded {len(member_data)} metrics from "
                            f"[{primary.scrape_target}] to '{bridge_name}' "
                            f"via member '{member.transport_name}'"
                        )

                group.mark_forwarded(member, now)

        except Exception as err:
            self.__log.exception(f"Error reading group [{primary.scrape_target}]: {err}")
            err_code = str(getattr(err, 'errno', ''))
            match err_code:
                case (NetworkError.CONN_RESET.value | NetworkError.BROKEN_PIPE.value |
                    NetworkError.TIMED_OUT.value | NetworkError.CONN_REFUSED.value |
                    NetworkError.NET_UNREACHABLE.value | NetworkError.HOST_UNREACHABLE.value):
                    primary.connect()

    def _filter_for_member(self, full_data: dict[str, int | float | str], member: transport_base) -> dict[str, int | float | str]:
            """
            Filters full_data to only the metrics relevant to this member.

            Resolution order:
            1. member.protocolSettings.variable_mask  — the raw allowlist loaded
               from the mask file; always present when a mask file is configured,
               even if protocolSettings.registry_map is empty mid-cycle.
               Synthetic ``<name>_desc`` keys are also passed when their source
               ``<name>`` is in the mask — they are generated after masking and
               are never listed in the mask file itself.
            2. member.registry_map variable_names     — derived from the masked
               registry map; used when no explicit mask file was loaded.
            3. Forward everything                     — no mask configured at all.
            """
            ps = getattr(member, 'protocolSettings', None)

            # Prefer the raw variable_mask list — it is always populated from the
            # mask file at init and is not affected by mid-cycle registry state.
            if ps is not None and ps.variable_mask:
                mask: set[str] = set(ps.variable_mask)  # already lowercased by _load_filter_file
                return {
                    k: v for k, v in full_data.items()
                    if k.lower() in mask
                    # also pass synthetic _desc keys whose source variable is in the mask
                    or (k.lower().endswith('_desc') and k.lower()[:-5] in mask)
                }

            # Fall back to deriving the key set from the registry map entries.
            member_keys: set[str] = set()
            for entries in member.registry_map.values():
                for entry in entries:
                    if hasattr(entry, 'variable_name') and entry.variable_name:
                        member_keys.add(entry.variable_name)

            if not member_keys:
                return full_data  # no mask configured — forward everything

            return {k: v for k, v in full_data.items() if k in member_keys}

    def _submit_concurrent_group_read(self, group: ScrapeGroup, now: float) -> None:
        """
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
            prior = self.__concurrent_futures.get(group_key)
            if prior is not None:
                if prior.done():
                    try:
                        prior.result()
                    except Exception as exc:
                        self.__log.error(f"Concurrent read future for '{group_key}' ended with error: {exc}")
                    self.__concurrent_futures.pop(group_key, None)
                else:
                    self.__log.debug(
                        f"Concurrent read already in progress for '{group_key}' - skipping duplicate submit."
                    )
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
        """Return ``True`` if transport ``a`` and ``b`` are linked as a bridge pair in either direction.

        A bridge relationship exists when ``b``'s transport name appears in
        ``a.bridges``, or ``a``'s transport name appears in ``b.bridges``.
        """
        return (
            b.transport_name in a.bridges
            or a.transport_name in b.bridges
        )

    def reconnect_upstream_bridge(self, transport_id: str) -> None:
        """Force a reconnect of the scraper transport identified by ``transport_id``.

        Called by a bridge transport when its stale-data detection fires, passing
        the ``transport_name`` of the upstream scraper that has stopped producing
        fresh data.  Resets ``connected`` to ``False`` and ``last_read_time`` to
        ``0.0`` so the main loop treats the transport as disconnected and
        reconnects it at the next tick.  Logs a warning if ``transport_id`` does
        not match any known transport.
        """
        target: transport_base | None = next(
            (t for t in self.__transports if t.transport_name == transport_id), None )

        if target is None:
            self.__log.warning(f"Reconnect requested for unknown transport '{transport_id}'")
            return

        self.__log.warning(f"Stale data detected — reconnecting '{transport_id}'")
        target.connected = False
        target.last_read_time = 0.0

    # init the variable request_upstream_reconnect in the bridge __init__.  If it goes true during stale detection, reconnect routine triggers.
    def _wire_reconnect_hooks(self) -> None:
        """Attach the gateway's ``reconnect_upstream_bridge`` callback to every bridge transport.

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
        Reads all transports in parallel, each running its full generator
        cycle independently on the shared interleaved executor.

        Transports on separate physical endpoints (different scrape_targets)
        run entirely in parallel with no cross-transport waiting.

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

        def run_transport(state: TransportState) -> TransportState:
            """Drain ``state.transport.read_data_iter()`` to completion and update ``state`` with the outcome.

            On success sets ``state.completed_cleanly = True``.  On exception,
            records the error, marks the cycle incomplete on the transport, and
            clears ``_bus_lock`` so the lock is not held after failure.
            """
            try:
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

        states: list[TransportState] = [TransportState(transport=transport) for transport in transports]
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

    def _route_interleaved_state(self, state: TransportState, now: float, ready_groups: list["ScrapeGroup"],) -> None:
        """Forward a completed interleaved transport's data to its bridges without waiting for sibling transports.

        Called by ``_poll_interleaved_cycles`` as each worker future resolves.
        Retrieves the decoded metrics via ``get_partial_data`` and routes them
        as follows:

        - If the transport is the primary of a ``ScrapeGroup`` in ``ready_groups``,
          iterates the group's due members, filters the full data set to each
          member's variable mask via ``_filter_for_member``, and calls
          ``write_data`` on each member's configured bridges.  Bridges that
          require a complete cycle (``write_requires_complete_cycle``) are skipped
          when the cycle is only partial.  Each forwarded member is stamped via
          ``mark_forwarded``.
        - If the transport belongs to no group (standalone scraper), its data is
          written directly to its own bridges subject to the same cycle-completeness
          check.
        """
        transport: transport_base = state.transport
        data: dict[str, int | float | str] = transport.get_partial_data()
        cycle_complete: bool = transport.cycle_is_complete_for_bridge()

        if not data:
            self.__log.warning(f"'{transport.transport_name}' produced no data this cycle.")
            return

        self.__log.debug(
            f"'{transport.transport_name}' completed "
            f"({'complete' if cycle_complete else 'partial'}) "
            f"with {len(data)} metrics."
        )

        group_by_primary: dict[str, ScrapeGroup] = {
            g.primary.transport_name: g for g in ready_groups
        }

        group: ScrapeGroup | None = group_by_primary.get(transport.transport_name)
        if group is not None:
            due_members: list[transport_base] = group.members_due(now)
            self.__log.debug(f"Group members due for '{transport.transport_name}': {[m.transport_name for m in due_members]}")
            for member in due_members:
                member_data: dict[str, int | float | str] = self._filter_for_member(data, member)
                # Build a readable summary of how filtering was applied
                _ps = getattr(member, 'protocolSettings', None)
                if _ps is not None and _ps.variable_mask:
                    _mask_summary: str = f"{len(_ps.variable_mask)} mask keys (variable_mask file)"
                else:
                    _mk: set[str] = set()
                    for _entries in member.registry_map.values():
                        for _e in _entries:
                            if hasattr(_e, 'variable_name') and _e.variable_name:
                                _mk.add(_e.variable_name)
                    _mask_summary = f"{len(_mk)} mask keys (registry_map)" if _mk else "no mask — forwarding all"
                self.__log.debug(
                    f"Filtered data for '{member.transport_name}': "
                    f"{len(member_data)} keys. {_mask_summary}"
                )
                if not member_data:
                    continue
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
                        f"Writing to bridge '{bridge_name}' for member "
                        f"'{member.transport_name}' "
                        f"device_identifier='{member.device_identifier}' "
                        f"keys={list(member_data.keys())[:3]}"
                    )
                    bridge.write_data(member_data, member)
                group.mark_forwarded(member, now)
        else:
            for bridge_name in transport.bridges:
                bridge = next(
                    (t for t in self.__transports if t.transport_name == bridge_name),
                    None,
                )
                if bridge is None:
                    continue
                if (
                    getattr(bridge, 'write_requires_complete_cycle', False)
                    and not cycle_complete
                ):
                    self.__log.warning(f"Skipping '{bridge_name}' for '{transport.transport_name}' - cycle incomplete.")
                    continue
                bridge.write_data(data, transport)

    def run(self) -> None:
        """Start the main polling loop and block until the gateway is stopped.

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
    """Entry point: parse CLI arguments, resolve the config path, and start the gateway and web server.

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
