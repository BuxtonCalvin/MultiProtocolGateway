# Description: Base transport class defining common interface and behavior for all transports, including protocol settings management, device metadata, and read/write operations.
# File: transport_base.py
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

# Base transport class defining common interface and behavior for all transports,
# including protocol settings management, device metadata, and read/write operations.
# Transports should inherit from this and implement protocol-specific logic as needed.
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Iterator, Literal, Optional

from classes.messaging.message_handler import send_message as _send_message
from classes.protocol_settings import (
    Registry_Type,
    protocol_settings,
    registry_map_entry,
)

if TYPE_CHECKING:
    from defs.common import TransportSettings

    from .transport_base import transport_base


@dataclass
class TransportCycleResult:
    """
    Scheduling path: All (Sequential, Concurrent, Interleaved).

    Transport-owned read cycle outcome used by the gateway to decide whether
    a payload is safe to forward to completeness-sensitive bridges.
    """
    has_data: bool = False
    is_complete: bool = True
    expected_units: int = 0
    completed_units: int = 0
    skipped_units: int = 0

class TransportWriteMode(Enum):
    READ = 0x00
    ''' READ ONLY '''
    WRITE = 0x01
    ''' Standard Write Mode, ALL SAFETIES IN PLACE'''
    RELAXED = 0x02
    ''' less strict - initial protocol validation skipped'''
    UNSAFE = 0x03
    ''' skip all safeties '''

    @classmethod
    def fromString(cls, name: str) -> "TransportWriteMode":
        """Scheduling path: N/A — config parsing, used during setup regardless of read_mode."""
        name = name.strip().upper()

        # Map inputs to the STRING names of the Enum members
        alias: dict[str, str] = {
            "": "READ",
            "FALSE": "READ",
            "NO": "READ",
            "READ": "READ",
            "R": "READ",

            "TRUE": "WRITE",
            "YES": "WRITE",
            "WRITE": "WRITE",
            "W": "WRITE",

            "RELAXED": "RELAXED",
            "UNSAFE": "UNSAFE"
        }

        # Get the target name, defaulting to "READ"
        target_member: str = alias.get(name, "READ")

        # Access the member via bracket notation
        return cls[target_member]

class transport_base:
    """Scheduling path: All (Sequential, Concurrent, Interleaved) — base class for every transport regardless of read_mode."""

    @property
    def connected(self) -> bool:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        """
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Centralized connection state manager. Intercepts every assignment to
        self.connected anywhere in the transport hierarchy — subclasses set
        self.connected = True/False exactly as before and this fires automatically.

        Responsibilities:
        - Detects genuine state transitions (ignores no-op assignments)
        - Keeps _needs_reconnection in sync so the main loop can act on it
        - Emits a structured log entry on every transition
        - Sends a push notification on loss or recovery, but not on the
        initial startup connect (expected, non-actionable)
        - Does not call connect() itself — that remains the caller's
        responsibility to avoid re-entrance on the setter
        """
        previous: bool = self._connected
        self._connected = value

        # Ignore no-op assignments — subclass code sets
        # self.connected = False in multiple error handlers and that's fine;
        # we only care about genuine transitions.
        if value == previous:
            return

        if value:
            # False → True: connection established or re-established
            self._needs_reconnection = False
            was_previously_connected: bool = self._ever_connected
            self._ever_connected = True

            self._log.info(f"[CONNECTED] {self.transport_name} connection established.")

            if was_previously_connected:
                # This is a recovery after a known loss — worth alerting.
                # Suppressed on first startup connect since that's expected.
                self.send_message(
                    f"Connection restored: {self.transport_name} "
                    f"({getattr(self, 'host', getattr(self, 'port', ''))})",
                    title=f"MPG Connection Restored for {self.transport_name}",
                    priority=1,
                )
        else:
            # True → False: connection lost
            self._needs_reconnection = True

            if self._ever_connected:
                # Only alert if we were genuinely connected before.
                # Guards against subclass __init__ code that sets
                # self.connected = False before any connection is attempted.
                self._log.error(f"[DISCONNECTED] {self.transport_name} connection lost.")
                self.send_message(
                    f"Connection lost: {self.transport_name} "
                    f"({getattr(self, 'host', '')}:{getattr(self, 'port', '')})",
                    title=f"MPG Connection Alert for {self.transport_name}",
                    priority=1,
                )
    _log : logging.Logger
    transport_type: ClassVar[Literal["scraper", "bridge", "base class", "general"]] = "base class"


    def __init__(self, settings : "TransportSettings") -> None:
        """Scheduling path: N/A — setup, runs once regardless of read_mode."""

        self.protocolSettings: Optional["protocol_settings"] = None
        self.type: str = self.__class__.__name__
        self.transport_name: str = ""
        self._connected: bool = False
        self._ever_connected: bool = False
        # Flag to indicate if the bridge transport needs reconnection; set to True in cleanup() and checked by the gateway to trigger a reconnect.
        self._needs_reconnection: bool = False
        self._connection_reported: bool = False  # suppresses duplicate messages on first connect
        self.last_read_time: float = 0.0
        self.read_interval: float = 0.0
        self.write_enabled: bool = False
        self.max_precision: int = 2
        self.bridge: str = ""
        # device metadata
        self.device_name: str = ""
        self.device_serial_number: str = ""
        self.device_manufacturer: str = "MPG"
        self.device_model: str = ""
        self.device_identifier: str = ""
        self.device_location: str = ""
        # Populated by the gateway after scrape groups are built.
        # Bridges can use this to size resources (e.g. connection pools) that
        # scale with the number of concurrent data sources.
        self.scraper_count: int = 1

        # so any early log calls before transport_name is set don't crash
        self._log: logging.Logger = logging.getLogger(__name__)

        self.transport_name: str = settings.name

        # Replace with transport-specific logger now that name is known
        if "log_level" in settings:
            level = getattr(logging, (settings.get("log_level") or "").strip().upper(), logging.INFO)
            self._log.setLevel(level)
        # else: leave the named logger's level unset so it inherits from the root

        self.on_message: Callable[["transport_base", registry_map_entry, int | float | str], None] | None = None
        ''' callback, on message received '''

        self.request_upstream_reconnect: Callable[[str], None] | None = None
        ''' callback for reconnect. transport should call this with the name of the transport it wants to reconnect to
            trigger a reconnect from the bridge. This is required for transports that have a bridge and need to trigger
            a reconnect of the bridge when the bridge's connection drops.
        '''
        # Initialize the bus lock
        self.bus_lock: threading.Lock | None = None
        self._last_cycle_result: TransportCycleResult = TransportCycleResult()
        self.transport_name = settings.name #section name

        # Last-known scrape values — populated in write_data() so the snapshot
        # is taken at the point data is confirmed complete and bridge-bound.
        # The web UI refresh button reads from this via /api/device/{name}/last-values.
        self.last_known_data: dict[str, int | float | str] = {}
        # Event that fires each time write_data() stores a new snapshot.
        # /api/device/{name}/last-values/wait blocks on this event so the
        # refresh button waits for the next real cycle rather than polling.
        self.values_ready_event: threading.Event = threading.Event()

        # Bridges set this to True if they require a complete, end-of-cycle
        # batch rather than partial mid-cycle data.  The gateway will suppress
        # write_data calls for this bridge when the data is known to be partial
        # (i.e. the scrape cycle was cut short by a block timeout or too many
        # retries). Default False preserves existing behavior for MQTT etc.
        self.write_requires_complete_cycle: bool = False

        self.type = self.__class__.__name__

        if settings:
            self.device_serial_number = settings.get(["device_serial_number"], self.device_serial_number)
            self.device_manufacturer = settings.get(["device_manufacturer"], self.device_manufacturer)
            self.device_model = settings.get(["device_model"], self.device_model)
            self.device_location = settings.get(["device_location"], self.device_location)
            self.device_name = settings.get(["device_name"], fallback=self.device_manufacturer+"_"+self.device_serial_number)

            bridge_raw: str = settings.get("bridge", "")
            self.bridges: list[str] = [b.strip() for b in bridge_raw.split(",") if b.strip()]
            self.bridge: str = self.bridges[0] if self.bridges else ""  # backward compatibility with single "bridge" setting

            self.read_interval = settings.getfloat("read_interval", self.read_interval)
            self.max_precision = settings.getint(["max_precision"], fallback=self.max_precision)

            if "write_enabled" in settings:
                self.write_enabled = settings.getboolean(["write_enabled"], self.write_enabled)

            if "write_type" in settings:  #  relaxed write etc
                self.write_mode: TransportWriteMode = TransportWriteMode.fromString(settings.get("write_type", ""))
                if self.write_mode != TransportWriteMode.READ:
                    self.write_enabled = True

            #load a protocol_settings class for every transport; required for adv features. ie, variable timing.
            #must load after settings
            self.protocol_version: str = settings.get("protocol_version", fallback='')
            if self.protocol_version:

                self.protocolSettings = protocol_settings(self.protocol_version, transport_settings=settings)
                self.protocolSettings.transport = self.__class__.__name__  # e.g. "modbus_tcp"

                # Update the transport settings reference in the copy
                self.protocolSettings.transport_settings = settings

                if self.protocolSettings:
                    self.protocol_version = self.protocolSettings.protocol

        self.update_identifier()

    @property
    def registry_map(self) -> dict[Registry_Type, list[registry_map_entry]] :
        """
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Returns this transport's registry map, or empty dict if no protocol loaded.
        Consumers should always use this rather than protocol_settings.registry_map
        directly.
        """
        if hasattr(self, "protocolSettings") and self.protocolSettings:
            return self.protocolSettings.registry_map
        return {}

    @property
    def protocol_name(self) -> str:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        if hasattr(self, "protocolSettings") and self.protocolSettings:
            return self.protocolSettings.protocol
        return ""


    def update_identifier(self) -> None:
        """Scheduling path: N/A — setup, runs once regardless of read_mode."""
        self.device_identifier = str(self.device_serial_number or "").strip().lower()

    def init_bridge(self, from_transport : "transport_base") -> None:
        """Scheduling path: N/A — setup, runs once regardless of read_mode."""
        pass


    @classmethod
    def _get_top_class_name(cls, cls_obj: Any) -> str:
        """Finds the root class name in the inheritance chain."""
        # Ensure it is a class and has bases, and that the bases tuple is not empty
        if not hasattr(cls_obj, "__bases__") or not cls_obj.__bases__:
            return getattr(cls_obj, "__name__", str(cls_obj))

        # Safely extract the primary base class
        base: Any = cls_obj.__bases__[0]

        # Stop recursion if the base is 'object' (the ultimate root of all Python classes)
        if base is object:
            return cls_obj.__name__

        return cls._get_top_class_name(base)


    def connect(self) -> bool | None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — base stub; modbus_base overrides with the real implementation."""
        pass

    def cleanup(self) -> None:
        """
        Scheduling path: N/A — shutdown, runs once regardless of read_mode.

        Clean up transport resources and close connections.
        Sets connected = False which flows through the property setter,
        handling _needs_reconnection, logging, and notification automatically.
        Subclasses should call super().cleanup() after their own resource
        teardown so the state transition fires after the connection is
        actually closed, not before.
        """
        self._log.debug(f"Cleaning up transport {self.transport_name}")
        self.connected = False

    # write_data receives either the full batch dict
    # or a single-entry dict constructed in on_message, both with same value type
    def write_data( self, data: dict[str, int | float | str ], from_transport: "transport_base" ) -> None:
        '''Scheduling path: All (Sequential, Concurrent, Interleaved) — base stub; bridge subclasses override with their real implementation.

        general purpose write function for between transports'''
        pass

    #let's convert this to dict[str, registry_map_entry]
    def read_data(self) -> dict[str, int | float | str]:
        '''
        Scheduling path: Sequential, Concurrent — base stub; modbus_base overrides
        with the real implementation. Not used by interleaved mode (see read_data_iter).

        general purpose read function for between transports;
        return type may be changed to dict[str, registry_map_entry]. still thinking about this
        '''
        return {}

    def read_group_data(self, members: list["transport_base"]) -> dict[str, int | float | str]:
        """
        Scheduling path: Sequential, Concurrent — base default; modbus_base overrides
        with its own consolidated-union implementation (see modbus_base.read_group_data).
        Not used by interleaved mode (see read_group_data_iter below).

        Read data for a scrape group.
        The default behavior is a normal transport read; transports with
        grouped-read optimizations can override this.
        """
        self._start_cycle_tracking()
        data: dict[str, int | float | str] = self.read_data()
        self.finish_cycle_tracking(data)
        return data

    def read_group_data_iter(self, members: list["transport_base"]) -> Iterator[bool]:
        """
        Scheduling path: Interleaved only (called from
        _process_transports_interleaved for any group with more than one
        member — see protocol_gateway.py).

        Interleaved variant of read_group_data. Builds the union registry
        across all group members, reads one block at a time via the primary
        transport's _read_registry_type_iter(), then decodes and stores the
        result per member so get_partial_data() returns the correct payload
        for each member when _route_interleaved_state processes them.

        Resolution order for each member mirrors _filter_for_member:
        1. member.protocolSettings.registry_map  — post-mask entries only,
        respecting each member's send_*_register flags.
        2. Forward everything                    — no protocolSettings loaded.

        Non-modbus transports fall back to read_data_iter() for the wire
        read since _read_registry_type_iter is modbus-specific. The
        _cycle_active guard on _start_cycle_tracking prevents the fallback
        from resetting state that read_group_data_iter already initialized.
        """
        self._start_cycle_tracking()

        for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING,
                            Registry_Type.COIL, Registry_Type.DISCRETE):

            # Gate on primary's send flags first — if the primary has
            # explicitly disabled a registry type, skip it regardless of
            # what members have loaded.
            primary_flag_map: dict[Registry_Type, bool] = {
                Registry_Type.INPUT:    getattr(self, 'send_input_register',    True),
                Registry_Type.HOLDING:  getattr(self, 'send_holding_register',  True),
                Registry_Type.COIL:     getattr(self, 'send_coil_register',     True),
                Registry_Type.DISCRETE: getattr(self, 'send_discrete_register', True),
            }
            if not primary_flag_map.get(registry_type, True):
                continue

            # Build the union of registry entries across all members,
            # respecting each member's own send_*_register flags and
            # using their post-mask registry map so masked-out entries
            # are never included in the physical read.
            union_entries: list[registry_map_entry] = []
            seen: set[tuple[int, str]] = set()
            max_register: int = 0

            for member in members:
                # Respect each member's send flags independently
                member_flag_map: dict[Registry_Type, bool] = {
                    Registry_Type.INPUT:    getattr(member, 'send_input_register',    True),
                    Registry_Type.HOLDING:  getattr(member, 'send_holding_register',  True),
                    Registry_Type.COIL:     getattr(member, 'send_coil_register',     True),
                    Registry_Type.DISCRETE: getattr(member, 'send_discrete_register', True),
                }
                if not member_flag_map.get(registry_type, True):
                    continue

                ps: protocol_settings | None = getattr(member, 'protocolSettings', None)
                if ps is None:
                    # Path 3: no protocolSettings — nothing to contribute to union
                    continue
                for entry in ps.registry_map.get(registry_type, []):
                    key: tuple[int, str] = (entry.register, entry.variable_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    union_entries.append(entry)
                    if entry.register > max_register:
                        max_register = entry.register

            if not union_entries:
                continue

            # Delegate wire reading to the modbus block iterator if available
            # (modbus_base provides _read_registry_type_iter), otherwise fall
            # back to a single atomic read via read_data_iter. The _cycle_active
            # guard ensures read_data_iter does not reset tracking state.
            read_iter = getattr(self, '_read_registry_type_iter', None)
            if read_iter is not None:
                yield from read_iter(registry_type, union_entries, max_register)
            else:
                yield from self.read_data_iter()

            # Decode the raw registers against each member's own protocolSettings
            # so per-member adjustments and code lookups are applied correctly.
            # Each member gets its own _partial_info populated so get_partial_data()
            # returns the right payload per member in _route_interleaved_state.
            for member in members:
                ps = getattr(member, 'protocolSettings', None)
                if ps is None:
                    # Path 3: no protocolSettings — member contributes nothing to
                    # the union decode; the primary's _partial_info carries the full
                    # payload and _route_interleaved_state will forward everything.
                    continue
                member_entries: list[registry_map_entry] = ps.registry_map.get(registry_type, [])
                if not member_entries:
                    continue
                new_info: dict[str, int | float | str] = ps.process_registery(
                    self._partial_registry, member_entries
                )
                if not hasattr(member, '_partial_info'):
                    member._partial_info = {}
                member._partial_info.update(new_info)

            # Accumulate into primary's _partial_info for get_partial_data()
            for member in members:
                self._partial_info.update(getattr(member, '_partial_info', {}))

            self._partial_registry.clear()

        self.finish_cycle_tracking(self._partial_info)

    def read_data_iter(self) -> "Iterator[bool]":
        """
        Scheduling path: Interleaved only (called directly for standalone transports,
        or as read_group_data_iter's per-solo-member fallback). modbus_base overrides
        this with true block-level yielding; this default is for non-modbus transports.

        Block-level generator variant of read_data for interleaved scheduling.
        Yields True after each register block attempt (success or failure),
        allowing the caller to interleave reads across transports on a shared bus.
        Default implementation wraps read_data() as a single-yield generator
        so non-modbus transports work transparently in interleaved mode.
        Modbus transports override this with true block-level yielding.

        When called as a fallback from read_group_data_iter the _cycle_active
        flag will already be True, so _start_cycle_tracking and
        finish_cycle_tracking are skipped — the group iter owns the cycle
        lifecycle in that case.
        """
        _owner: bool = not getattr(self, '_cycle_active', False)
        if _owner:
            self._start_cycle_tracking()

        yield True  # non-modbus: treat the entire read as one atomic block
        self._partial_info.update(self.read_data())

        if _owner:
            self.finish_cycle_tracking(self._partial_info)

    def get_partial_data(self) -> dict[str, int | float | str]:
        """
        Scheduling path: Interleaved only.

        Returns data accumulated so far this cycle — by read_data_iter() for a
        standalone transport, or by read_group_data_iter() for a group member
        (each member's own _partial_info is populated directly by the
        consolidated read; see that method's docstring). Both write to the
        same _partial_info dict, so this one accessor is correct for either
        path with no override needed.
        """
        return getattr(self, '_partial_info', {})

    def _start_cycle_tracking(self) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        self._cycle_active: bool = True
        self._last_cycle_result = TransportCycleResult()
        self._partial_info: dict[str, int | float | str] = {}
        self._partial_registry: dict[int, int] = {}

    def finish_cycle_tracking(self, data: dict[str, int | float | str]) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Finalize a scrape cycle.

        Calls ``post_process_data`` before marking the cycle complete so
        synthetic metrics injected by subclasses are present in the data
        regardless of whether the caller used ``read_data()``,
        ``read_group_data()``, or ``read_data_iter()``.
        """
        processed: dict[str, int | float | str] = self.post_process_data(data)
        # Reflect any mutations back into the original dict so callers
        # that hold a reference to it see the enriched version.
        if processed is not data:
            data.clear()
            data.update(processed)
        self._cycle_active = False
        self._last_cycle_result.has_data = bool(data)

    @property
    def synthetic_fields_metadata(self) -> list[tuple[str, str, float, str, Registry_Type]]:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — consumed via post_process_data/finish_cycle_tracking regardless of read_mode; also read directly by TimescaleDB's init_bridge (setup, not per-cycle).

        Rich metadata for fields injected by ``post_process_data``.

        Used by TimescaleDB's ``init_bridge`` to register synthetic fields
        as first-class columns in the wide table schema alongside CSV-derived
        metrics.  This ensures the bridge knows the correct data type and unit
        for each synthetic field at schema-creation time, avoiding the
        type-inference ambiguity that would arise if columns were created on
        the fly during ``_validate_wide_row``.

        Returns a list of ``(variable_name, data_type, unit_mod, note, Registry_Type)``
        tuples matching the signature of ``_extract_metric_names`` output so
        the two sources can be concatenated directly before being passed to
        ``_ensure_columns_for_metrics``.

        Data type strings must match the ``Data_Type`` enum names used
        elsewhere in ``protocol_settings`` (e.g. ``"FLOAT"``, ``"USHORT"``,
        ``"SHORT"``, ``"TEXT"``).

        The base implementation returns an empty list — no synthetic columns
        are registered for transports that do not override this property.

        Example::

            @property
            def synthetic_fields_metadata(self) -> list[tuple[str, str, float, str, Registry_Type]]:
                return [
                    ("cell_voltage_max_v",   "FLOAT",  1.0, "Highest cell voltage V", "HOLDING"),
                    ("cell_voltage_min_v",   "FLOAT",  1.0, "Lowest cell voltage V", "HOLDING"),
                    ("cell_voltage_diff_mv", "FLOAT",  1.0, "Cell voltage spread mV", "HOLDING"),
                    ("balancing_state",      "USHORT", 1.0, "0=Idle 1=Balancing 2=Finished", "HOLDING"),
                    ("balancing_state_text", "TEXT",   1.0, "Balancing state label", "HOLDING"),
                ]
        """
        return []

    @property
    def synthetic_field_names(self) -> frozenset[str]:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — consumed via _filter_for_member regardless of read_mode.

        Names of fields injected by ``post_process_data``.

        Derived automatically from ``synthetic_fields_metadata`` — subclasses
        should override that property only.

        ``_filter_for_member`` in ``protocol_gateway`` uses this frozenset to
        bypass the variable mask for fields that have no protocol CSV row.
        """
        return frozenset(name for name, *_ in self.synthetic_fields_metadata)

    def post_process_data(self, info: dict[str, int | float | str]) -> dict[str, int | float | str]:
        """Scheduling path: All (Sequential, Concurrent, Interleaved).

        Post-processing hook called after every complete scrape cycle.

        Invoked by ``finish_cycle_tracking`` which is the single convergence
        point for all three read paths — sequential (``read_data``), group
        (``read_group_data``), and interleaved (``read_data_iter``).  The hook
        therefore fires exactly once per cycle regardless of read mode.

        Override in subclasses to inject synthetic / derived metrics, validate
        mandatory fields, or apply device-specific transformations.

        ``info`` contains all decoded register values for this cycle after
        mask and screen filtering have been applied.  Subclasses may mutate
        the dict in place and return it, or return a new dict — both are safe.

        Returning an empty dict suppresses all bridge output for this cycle,
        which can be useful when a mandatory field is absent and publishing
        partial data would be misleading.

        The base implementation returns ``info`` unchanged.
        """
        return info

    def on_first_connect_read(self) -> None:
        """Scheduling path: N/A — setup, runs once per connect/reconnect regardless of read_mode.

        Hook called once after the first successful physical connection.

        Override in subclasses to read auxiliary registers that need to be
        cached for the lifetime of the connection — configuration thresholds,
        serial numbers, calibration constants, etc.

        The hook is called by ``modbus_base.connect()`` after
        ``self.connected`` is set to ``True``, so the transport is ready to
        issue Modbus requests when it runs.

        On reconnect, ``connect()`` clears per-connection state and calls
        this hook again, giving subclasses a chance to refresh their cache
        with current device values.

        Subclasses that add further override levels should call
        ``super().on_first_connect_read()`` first.

        The base implementation does nothing.
        """
        pass

    def _cycle_expect_unit(self, count: int = 1) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        self._last_cycle_result.expected_units += count

    def _cycle_mark_unit_complete(self, count: int = 1) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        self._last_cycle_result.completed_units += count

    def cycle_mark_incomplete(self, skipped_units: int = 1) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        self._last_cycle_result.is_complete = False
        self._last_cycle_result.skipped_units += skipped_units

    def get_cycle_result(self) -> TransportCycleResult:
        """Scheduling path: All (Sequential, Concurrent, Interleaved)."""
        return self._last_cycle_result

    def cycle_is_complete_for_bridge(self) -> bool:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — called from _process_group_read and _route_interleaved_state."""
        result: TransportCycleResult = self.get_cycle_result()
        return result.has_data and result.is_complete

    def interleaved_cycle_timeout(self) -> float:
        """
        Scheduling path: Interleaved only. modbus_base overrides this with a
        protocol-aware estimate; this default is for non-modbus transports.

        Return a reasonable full-cycle timeout for one interleaved read.
        Transports with better knowledge of their block structure can override.
        """
        return 60.0

    @property
    def scrape_target(self) -> str:
        """
        Scheduling path: All (Sequential, Concurrent, Interleaved) — base default; modbus_base overrides with the real endpoint identifier.

        Identifies the physical device this transport reads from.
        Two transports with the same scrape_target share an endpoint
        and can be consolidated into a scrape group.
        Returns empty string for bridge transports (no scrape target).
        Override in scraper subclasses to return a normalized identifier.
        """
        return ""

    def enable_write(self) -> None:
        '''Scheduling path: N/A — setup, runs once regardless of read_mode. required for sensitive / manually defined protocols '''
        pass

    # on_message helper to filter out None.
    def _emit_message( self, entry: registry_map_entry, value: int | float | str ) -> None:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — on_message helper to filter out None."""
        if self.on_message is not None:
            self.on_message(self, entry, value)

    def send_message(self, message: str, title: str = "", priority: int = 0, services: "list[str] | str | None" = None, **kwargs: Any) -> None:
        """
        Scheduling path: N/A — messaging utility, independent of read_mode.

        Send a notification through all configured messaging services
        (Pushover, Telegram, …).

        This is a convenience wrapper around the module-level
        ``send_message()`` function so any transport subclass can call:

            self.send_message("Battery critically low", title="MPG Alert", priority=1)

        Parameters
        ----------
        message:
            Notification body (required).
        title:
            Short heading.  When omitted the default_title from [messages]
            config is used.
        priority:
            Pushover-style integer priority: -2 (silent) … 2 (emergency).
            Telegram maps values > 0 to a sound-on notification, ≤ 0 to
            silent.
        **kwargs:
            Forwarded to the underlying driver for future extensibility.
        """
        _send_message(message=message, title=title, priority=priority, services=services, **kwargs)

    #region - modbus
    # keep here as methods might also apply to future protocols
    def read_registers(self, start: int, count: int = 1, registry_type : Registry_Type = Registry_Type.INPUT, **kwargs: Any) -> Any:
        """Scheduling path: All (Sequential, Concurrent, Interleaved) — base stub; modbus_base overrides with the real implementation."""
        pass

    def write_register(self, register : int, value : int, **kwargs: Any) -> None:
        """Scheduling path: N/A — write path, independent of read_mode; base stub, modbus_base overrides."""
        pass

    def write_coil(self, register: int, value: bool, **kwargs: Any) -> bool:
        """
        Scheduling path: N/A — write path, independent of read_mode.

        Write a single coil (bit) register. Modbus FC 0x05.
        Override in modbus_base; base no-op prevents AttributeError on non-modbus transports.
        Returns False — a no-op is never a confirmed write.
        """
        return False

    def validate_protocol(self, registry_type: Registry_Type) -> float:
        """
        Scheduling path: N/A — setup, called once from enable_write regardless of read_mode.

        Validates the protocol by reading registers and scoring results.
        Args:
            registry_type: Which register type to validate against (required).
                           Callers must be explicit — HOLDING for write validation,
                           INPUT for read validation.
        Returns:
            Score percentage 0-100 indicating valid register reads.
        """
        return 0.0

    def analyse_protocol(self) -> None:
        """Scheduling path: N/A — Analyze feature stub, not part of the read-scheduling loop."""
        pass
    #endregion
