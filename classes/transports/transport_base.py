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

    _log : logging.Logger
    transport_type: ClassVar[Literal["scraper", "bridge", "base class", "general"]] = "base class"


    def __init__(self, settings : "TransportSettings") -> None:

        self.protocolSettings: Optional["protocol_settings"] = None
        self.type: str = self.__class__.__name__
        self.transport_name: str = ""
        self.connected: bool = False
        self._needs_reconnection: bool = False
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

        # so any early log calls before transport_name is set don't crash
        self._log: logging.Logger = logging.getLogger(__name__)

        self.transport_name: str = settings.name

        # Replace with transport-specific logger now that name is known
        self._log_level = getattr(logging, settings.get("log_level", fallback="INFO"), logging.INFO)
        self._log = logging.getLogger(self.transport_name)
        self._log.setLevel(self._log_level)

        self.on_message: Callable[["transport_base", registry_map_entry, int | float | str], None] | None = None
        ''' callback, on message received '''

        self.request_upstream_reconnect: Callable[[str], None] | None = None
        ''' callback for reconnect. transport should call this with the name of the transport it wants to reconnect to
            trigger a reconnect from the bridge. This is required for transports that have a bridge and need to trigger
            a reconnect of the bridge when the bridge's connection drops.
        '''
        # Initialize the bus lock
        self._bus_lock: threading.Lock | None = None
        self._last_cycle_result: TransportCycleResult = TransportCycleResult()
        self.transport_name = settings.name #section name

        # Bridges set this to True if they require a complete, end-of-cycle
        # batch rather than partial mid-cycle data.  The gateway will suppress
        # write_data calls for this bridge when the data is known to be partial
        # (i.e. the scrape cycle was cut short by a block timeout or too many
        # retries). Default False preserves existing behavior for MQTT etc.
        self.write_requires_complete_cycle: bool = False

        #apply log level to logger
        self._log_level = getattr(logging, settings.get("log_level", fallback="INFO"), logging.INFO)
        self._log = logging.getLogger(self.transport_name)
        self._log.setLevel(self._log_level)

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
            self.protocol_version = settings.get("protocol_version", fallback='')
            if self.protocol_version:

                self.protocolSettings = protocol_settings(self.protocol_version, transport_settings=settings)

                # Update the transport settings reference in the copy
                self.protocolSettings.transport_settings = settings

                if self.protocolSettings:
                    self.protocol_version = self.protocolSettings.protocol

            #todo, reimplement default settings from protocolsettings

        self.update_identifier()

    @property
    def registry_map(self) -> dict:
        """
        Returns this transport's registry map, or empty dict if no protocol loaded.
        Consumers should always use this rather than protocol_settings.registry_map
        directly.
        """
        if hasattr(self, "protocolSettings") and self.protocolSettings:
            return self.protocolSettings.registry_map
        return {}

    @property
    def protocol_name(self) -> str:
        if hasattr(self, "protocolSettings") and self.protocolSettings:
            return self.protocolSettings.protocol
        return ""


    def update_identifier(self):
        self.device_identifier = str(self.device_serial_number or "").strip().lower()

    def init_bridge(self, from_transport : "transport_base") -> None:
        pass

    @classmethod
    def _get_top_class_name(cls, cls_obj):
        if not cls_obj.__bases__:
            return cls_obj.__name__
        else:
            return cls._get_top_class_name(cls_obj.__bases__[0])

    def connect(self) -> bool | None:
        pass

    def cleanup(self):
        """Clean up transport resources and close connections"""
        self._log.debug(f"Cleaning up transport {self.transport_name}")
        # Base implementation - subclasses should override if needed
        # Mark that this transport needs reconnection
        self._needs_reconnection = True
        pass

    # write_data receives either the full batch dict
    # or a single-entry dict constructed in on_message, both with same value type
    def write_data( self, data: dict[str, int | float | str ], from_transport: "transport_base" ) -> None:
        ''' general purpose write function for between transports'''
        pass

    #let's convert this to dict[str, registry_map_entry]
    def read_data(self) -> dict[str, int | float | str]:
        '''
        general purpose read function for between transports;
        return type may be changed to dict[str, registry_map_entry]. still thinking about this
        '''
        return {}

    def read_group_data(self, members: list["transport_base"]) -> dict[str, int | float | str]:
        """
        Read data for a scrape group.
        The default behavior is a normal transport read; transports with
        grouped-read optimizations can override this.
        """
        self._start_cycle_tracking()
        data = self.read_data()
        self._finish_cycle_tracking(data)
        return data

    # In transport_base, alongside read_data():

    def read_data_iter(self) -> "Iterator[bool]":
        """
        Block-level generator variant of read_data for interleaved scheduling.
        Yields True after each register block attempt (success or failure),
        allowing the caller to interleave reads across transports on a shared bus.
        Default implementation wraps read_data() as a single-yield generator
        so non-modbus transports work transparently in interleaved mode.
        Modbus transports override this with true block-level yielding.
        """
        self._start_cycle_tracking()
        yield True  # non-modbus: treat the entire read as one atomic block
        self._partial_data: dict[str, int | float | str] = self.read_data()
        self._finish_cycle_tracking(self._partial_data)

    def get_partial_data(self) -> dict[str, int | float | str]:
        """
        Returns data accumulated by read_data_iter().
        Non-modbus transports return whatever read_data() produced.
        """
        return getattr(self, '_partial_data', {})

    def _start_cycle_tracking(self) -> None:
        self._last_cycle_result = TransportCycleResult()

    def _cycle_expect_unit(self, count: int = 1) -> None:
        self._last_cycle_result.expected_units += count

    def _cycle_mark_unit_complete(self, count: int = 1) -> None:
        self._last_cycle_result.completed_units += count

    def _cycle_mark_incomplete(self, skipped_units: int = 1) -> None:
        self._last_cycle_result.is_complete = False
        self._last_cycle_result.skipped_units += skipped_units

    def _finish_cycle_tracking(self, data: dict[str, int | float | str]) -> None:
        self._last_cycle_result.has_data = bool(data)

    def get_cycle_result(self) -> TransportCycleResult:
        return self._last_cycle_result

    def cycle_is_complete_for_bridge(self) -> bool:
        result = self.get_cycle_result()
        return result.has_data and result.is_complete

    def interleaved_cycle_timeout(self) -> float:
        """
        Return a reasonable full-cycle timeout for one interleaved read.
        Transports with better knowledge of their block structure can override.
        """
        return 60.0

    @property
    def scrape_target(self) -> str:
        """
        Identifies the physical device this transport reads from.
        Two transports with the same scrape_target share an endpoint
        and can be consolidated into a scrape group.
        Returns empty string for bridge transports (no scrape target).
        Override in scraper subclasses to return a normalized identifier.
        """
        return ""

    def enable_write(self) -> None:
        ''' required for sensitive / manually defined protocols '''
        pass

    # on_message helper to filter out None.
    def _emit_message( self, entry: registry_map_entry, value: int | float | str ) -> None:
        if self.on_message is not None:
            self.on_message(self, entry, value)

    def send_message(self, message: str, title: str = "", priority: int = 0, services: "list[str] | str | None" = None, **kwargs) -> None:
        """
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
    #might limit to modbus_base only. not sure; might also apply to future protocols
    def read_registers(self, start, count=1, registry_type : Registry_Type = Registry_Type.INPUT, **kwargs) -> Any:
        pass

    def write_register(self, register : int, value : int, **kwargs) -> None:
        pass

    def analyse_protocol(self) -> None:
        pass


def validate_protocol(self, registry_type: Registry_Type = Registry_Type.INPUT) -> float:
    """
    Validates the protocol by reading registers and scoring results.
    Args:
        registry_type: Which register type to validate against.
    Returns:
        Score percentage 0-100 indicating valid register reads.
    """
    return 0.0
    #endregion
