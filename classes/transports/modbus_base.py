# Description: Modbus base transport class with shared client management, register failure tracking, and protocol analysis support
# File: modbus_base.py
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

from __future__ import annotations

# Modbus base transport class with shared client management, register failure tracking, and protocol analysis support
import inspect
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, Iterator, Literal, Optional

from pymodbus.client.base import ModbusBaseClient
from pymodbus.constants import ExcCodes
from pymodbus.exceptions import ModbusIOException

from defs.common import TransportSettings, strtobool

from ..protocol_settings import (
    Data_Type,
    Registry_Type,
    WordOrder,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)
from .transport_base import TransportWriteMode, transport_base

# Modbus function codes for exception interpretation
MODBUS_FUNCTION_CODES = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
    0x14: "Read File Record",
    0x15: "Write File Record",
    0x16: "Mask Write Register",
    0x17: "Read/Write Multiple Registers",
    0x2B: "Read Device Identification"
}


MODBUS_EXCEPTION_CODES: dict[ExcCodes, str] = {
    ExcCodes.ILLEGAL_FUNCTION: "ILLEGAL_FUNCTION",
    ExcCodes.ILLEGAL_ADDRESS: "ILLEGAL_ADDRESS",
    ExcCodes.ILLEGAL_VALUE: "ILLEGAL_VALUE",
    ExcCodes.DEVICE_FAILURE: "DEVICE_FAILURE",
    ExcCodes.ACKNOWLEDGE: "ACKNOWLEDGE",
    ExcCodes.DEVICE_BUSY: "DEVICE_BUSY",
    ExcCodes.NEGATIVE_ACKNOWLEDGE: "NEGATIVE_ACKNOWLEDGE",
    ExcCodes.MEMORY_PARITY_ERROR: "MEMORY_PARITY_ERROR",
    ExcCodes.GATEWAY_PATH_UNAVIABLE: "GATEWAY_PATH_UNAVIABLE",
    ExcCodes.GATEWAY_NO_RESPONSE: "GATEWAY_NO_RESPONSE"
}

MODBUS_EXCEPTION_DESCRIPTIONS: dict[ExcCodes, str] = {
    ExcCodes.ILLEGAL_FUNCTION: "The function code received is not allowed for this device.",
    ExcCodes.ILLEGAL_ADDRESS: "The data address received is not allowed for this device.",
    ExcCodes.DEVICE_FAILURE: "An unrecoverable error occurred while performing the action.",
    ExcCodes.ILLEGAL_VALUE: "A value contained in the query data field is not allowed.",
    ExcCodes.ACKNOWLEDGE: "The device has accepted the request and is processing it.",
    ExcCodes.DEVICE_BUSY: "The device is engaged in a long-duration program command.",
    ExcCodes.NEGATIVE_ACKNOWLEDGE: "The device cannot perform the program function received.",
    ExcCodes.MEMORY_PARITY_ERROR: "The device detected a parity error in memory.",
    ExcCodes.GATEWAY_PATH_UNAVIABLE: "The gateway path is not available.",
    ExcCodes.GATEWAY_NO_RESPONSE: "The gateway target device failed to respond."
}

def interpret_modbus_exception_code(code) -> str:
    """
    Interpret a Modbus exception response code and return human-readable information.

    Args:
        code (int): The exception response code (e.g., 132)

    Returns:
        str: Human-readable description of the exception
    """
    # Extract function code (lower 7 bits)
    function_code = code & 0x7F

    # Check if this is an exception response (upper bit set)
    if code & 0x80:
        # This is an exception response
        exception_code = code & 0x7F  # The exception code is in the lower 7 bits
        function_name: str = MODBUS_FUNCTION_CODES.get(function_code, f"Unknown Function ({function_code})")
        exception_name: str = MODBUS_EXCEPTION_CODES.get(exception_code, f"Unknown Exception ({exception_code})")
        description: str = MODBUS_EXCEPTION_DESCRIPTIONS.get(exception_code, "Unknown exception code")
        return f"Modbus Exception: {function_name} failed with {exception_name} - {description}"
    else:
        # This is not an exception response
        function_name = MODBUS_FUNCTION_CODES.get(function_code, f"Unknown Function ({function_code})")
        return f"Modbus Function: {function_name} (not an exception response)"

@dataclass()
class RegisterFailureTracker:
    """Tracks register read failures and manages soft disabling"""
    register_range: tuple[int, int]  # (start, end) register range
    registry_type: Registry_Type
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    disabled_until: float = 0.0  # Unix timestamp when disabled until
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_failure(self, max_failures: int = 5, disable_duration_hours: int = 12) -> bool:
        """Record a failed read attempt"""
        with self._lock:
            current_time: float = time.time()
            self.failure_count += 1
            self.last_failure_time = current_time

            # If we've had enough failures, disable for specified duration
            if self.failure_count >= max_failures:
                self.disabled_until = current_time + (disable_duration_hours * 3600)
                return True  # Indicates this range should be disabled
            return False

    def record_success(self) -> None:
        """Record a successful read attempt"""
        with self._lock:
            current_time: float = time.time()
            self.last_success_time = current_time
            # Reset failure count on success
            self.failure_count = 0
            self.disabled_until = 0

    def is_disabled(self) -> bool:
        """Check if this register range is currently disabled"""
        with self._lock:
            if self.disabled_until == 0:
                return False
            return time.time() < self.disabled_until

    def get_remaining_disable_time(self) -> float:
        """Get remaining time until re-enabled (0 if not disabled)"""
        with self._lock:
            if self.disabled_until == 0:
                return 0
            remaining: float = self.disabled_until - time.time()
            return max(0, remaining)
class modbus_base(transport_base):


    transport_type = "base class"
    #this is specifically static
    clients : dict[str, ModbusBaseClient] = {}
    ''' str is identifier, dict of clients when multiple transports use the same ports '''
    # Threading locks for concurrency control
    _clients_lock : threading.Lock = threading.Lock()
    ''' Lock for accessing the shared clients dictionary '''
    _client_locks : dict[str, threading.Lock] = {}
    ''' Port-specific locks to allow concurrent access to different ports '''

    # Connection attributes — declared here with defaults so methods
    # in modbus_base can reference them without type errors.
    # Subclasses assign real values in their __init__.
    host: str = ""
    port: str | int | None = None # serial port number or TCP port
    baudrate: int = 0       # Serial only — 0 means not applicable


    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        # modbus_base requires a protocol to function — fail early with a clear message
        # rather than letting None propagate through every method that uses protocolSettings
        if self.protocolSettings is None:
            msg: str = f"modbus_base transport '{settings.name}' requires a protocol_version to be set in config. No protocol settings were loaded."
            raise ValueError(msg)
        assert self.protocolSettings is not None  # noqa: S101

        self.client: Optional[ModbusBaseClient] = None

        # Initialize instance-specific variables (not class-level)
        self.modbus_delay_increament : float = 0.05
        ''' delay adjustment every error. todo: add a setting for this '''

        self.modbus_delay_setting : float = 0.85
        '''time in between requests, unmodified by user setting'''

        self.modbus_delay : float = 0.85
        '''time in between requests'''

        # per transport tuning — batteries with known intermittent blocks can have higher retry counts;
        # a totally dead device will exhaust retries quickly and yield control
        self.max_retries_per_block: int = int(settings.get("max_retries_per_block", fallback=3))

        self.first_connect : bool = True
        self._needs_reconnection : bool = False

        self.send_holding_register : bool = True
        self.send_input_register : bool = True
        self.send_coil_register: bool = True
        self.send_discrete_register: bool = True

        # Register failure tracking - make instance-specific
        self.enable_register_failure_tracking: bool = True
        self.max_failures_before_disable: int = 5
        self.disable_duration_hours: int = 12

        # Initialize transport-specific lock
        self._transport_lock = threading.Lock()

        # Initialize instance-specific register failure tracking
        self.register_failure_trackers: dict[str, RegisterFailureTracker] = {}
        self._failure_tracking_lock = threading.Lock()
        self._last_disabled_status_log: float = 0.0

        # Register failure tracking settings
        self.enable_register_failure_tracking = settings.getboolean("enable_register_failure_tracking", fallback=self.enable_register_failure_tracking)
        self.max_failures_before_disable = settings.getint("max_failures_before_disable", fallback=self.max_failures_before_disable)
        self.disable_duration_hours = settings.getint("disable_duration_hours", fallback=self.disable_duration_hours)

        # get defaults from protocol settings if present, then override with transport settings if present there
        if "send_input_register" in self._protocol.settings:
            self.send_input_register = strtobool(self._protocol.settings["send_input_register"])

        if "send_holding_register" in self._protocol.settings:
            self.send_holding_register = strtobool(self._protocol.settings["send_holding_register"])

        if "send_coil_register" in self._protocol.settings:
            self.send_coil_register = strtobool(self._protocol.settings["send_coil_register"])

        if "send_discrete_register" in self._protocol.settings:
            self.send_discrete_register = strtobool(self._protocol.settings["send_discrete_register"])

        if "batch_delay" in self._protocol.settings:
            self.modbus_delay = float(self._protocol.settings["batch_delay"])

        # allow enable/disable of which registers to send
        self.send_holding_register = settings.getboolean("send_holding_register", fallback=self.send_holding_register)
        self.send_input_register = settings.getboolean("send_input_register", fallback=self.send_input_register)
        self.send_coil_register = settings.getboolean("send_coil_register", fallback=self.send_coil_register)
        self.send_discrete_register = settings.getboolean("send_discrete_register", fallback=self.send_discrete_register)
        self.modbus_delay = settings.getfloat("batch_delay", fallback=self.modbus_delay)
        self.modbus_delay_setting = self.modbus_delay

        # Store slave_id for scrape group uniqueness — devices chained on a
        # shared Modbus bus are differentiated by their slave/unit address.
        # Stored as a string so scrape_target can use it directly.
        # The address fallback covers modbus_rtu which uses that config key instead of slave_id.
        self._slave_id: str = settings.get("slave_id", fallback=settings.get("address", fallback="1"))

    @property
    def _protocol(self) -> "protocol_settings":
        """
        Non-optional accessor for protocolSettings.
        Raises RuntimeError rather than propagating None through callers.
        """
        if self.protocolSettings is None:
            msg: str = f"protocolSettings is None for transport '{self.transport_name}' — protocol_version must be set in config."
            raise RuntimeError(msg)

        return self.protocolSettings

    def _should_send_registry_type(self, registry_type: Registry_Type) -> bool:
        """
        Return False when the given registry type has been disabled via the
        send_*_register flags, or when no registry map was loaded for it.
        Centralizes the repeated per-type enable/disable checks so callers
        (read_data, read_group_data, read_data_iter) stay clean.
        """
        flag_map: dict[Registry_Type, bool] = {
            Registry_Type.INPUT:    self.send_input_register,
            Registry_Type.HOLDING:  self.send_holding_register,
            Registry_Type.COIL:     self.send_coil_register,
            Registry_Type.DISCRETE: self.send_discrete_register,
        }
        if not flag_map.get(registry_type, True):
            return False
        if registry_type not in self._protocol.registry_map:
            self._log.debug(f"Skipping {registry_type.name} — no registry map loaded for {self.transport_name}")
            return False
        return True

    def _get_correct_device_arg(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # 1. Identify which keyword the current Pymodbus version expects
        # Check the signature of a standard client method
        if self.client is None:
            msg: str = f"Transport '{self.transport_name}' has no client assigned — subclass __init__ must assign self.client before calling " \
            "_get_correct_device_arg"
            raise RuntimeError(msg)

        sig: inspect.Signature = inspect.signature(self.client.read_input_registers)

        target_arg: str = next(
            (arg for arg in ("device_id", "slave", "unit")
            if arg in sig.parameters),
            "slave",
        )

        # Explicit argument wins.
        val = (
            kwargs.pop("unit", None)
            or kwargs.pop("slave", None)
            or kwargs.pop("device_id", None)
        )

        # Otherwise use the transport's configured slave.
        if val is None:
            val = int(getattr(self, "_slave_id", 1))

        kwargs[target_arg] = val

        self._log.debug("[%s] _get_correct_device_arg -> %s=%s", self.transport_name, target_arg, val)


        return kwargs


    def _entry_byte_order(self, entry: registry_map_entry) -> WordOrder:
        """Return the ``WordOrder`` for *entry* by delegating to ``DataAdjustments``."""
        return self._protocol._adjustments.get_entry_byteorder(entry)

    def _register_words_to_bytes(self, register_values: list[int], word_order: WordOrder) -> bytes:
        """Convert a list of 16-bit register words to a contiguous byte string.

        Applied on the **read** side of a write-verify cycle: the current
        register values are assembled into bytes so the existing decoded value
        can be extracted before the new value is merged in.

        ``word_order.word_reversed``
            If True the word list is reversed so the high-significance word
            is first in the output, matching big-endian integer unpacking by
            callers (CDAB / DCBA encodings).

        ``word_order.bytes_reversed``
            If True each 16-bit word is serialized in little-endian byte order
            (BADC / DCBA encodings).
        """
        words: list[int] = [value & 0xFFFF for value in register_values]
        if word_order.word_reversed:
            words.reverse()
        per_word_byteorder: str = "little" if word_order.bytes_reversed else "big"
        return b"".join(word.to_bytes(2, byteorder=per_word_byteorder, signed=False) for word in words)

    def _bytes_to_register_words(self, data: bytes, word_order: WordOrder) -> list[int]:
        """Convert a byte string back to a list of 16-bit Modbus register values.

        This is the exact inverse of ``_register_words_to_bytes`` and is used
        on the **write** side: after the new value has been merged into *data*,
        this method re-encodes it into register words ready to be sent to the
        device.

        ``word_order.bytes_reversed``
            If True each 2-byte slice is interpreted as little-endian before
            being stored as a register word (inverting the BADC/DCBA byte swap).

        ``word_order.word_reversed``
            If True the final word list is reversed so the low-significance
            word is placed back at the lower register address (inverting the
            CDAB/DCBA word reversal).
        """
        if len(data) % 2 != 0:
            msg: str = f"Expected even byte count for register write, got {len(data)}"
            raise ValueError(msg)
        per_word_byteorder: str = "little" if word_order.bytes_reversed else "big"
        words: list[int] = [
            int.from_bytes(data[i:i + 2], byteorder=per_word_byteorder, signed=False)
            for i in range(0, len(data), 2)
        ]
        if word_order.word_reversed:
            words.reverse()
        return words

    def _entry_word_count(self, entry: registry_map_entry) -> int:
        return self._protocol.entry_word_count(entry)

    def _extract_response_values(
        self,
        response: Any,
        registry_type: Registry_Type,
        register_range: tuple[int, int],
    ) -> dict[int, int] | None:
        """
        Extract register values from a pymodbus response object into a flat
        {address: value} dict, handling the two distinct response shapes:

          - INPUT / HOLDING  → response.registers  (list[int], 16-bit words)
          - COIL / DISCRETE  → response.bits       (list[bool], one bit each)

        Returns None if the response is missing the expected attribute or the
        attribute itself is None (treated as a read failure by callers).
        Coil/discrete bit values are stored as 0 or 1 so they are compatible
        with the rest of the int-keyed registry pipeline.
        """
        start_addr, count = register_range

        if registry_type in (Registry_Type.COIL, Registry_Type.DISCRETE):
            if not hasattr(response, "bits") or response.bits is None:
                return None
            return {
                start_addr + i: int(bool(response.bits[i]))
                for i in range(min(count, len(response.bits)))
            }
        else:
            if not hasattr(response, "registers") or response.registers is None:
                return None
            return {
                start_addr + i: response.registers[i]
                for i in range(min(count, len(response.registers)))
            }

    def write_registers(self, start_register: int, values: list[int], **kwargs: Any) -> None:
        if not self.write_enabled:
            return
        if self.client is None:
            self._log.error("write_registers called before client was initialized")
            return
        kwargs = self._get_correct_device_arg(kwargs)
        port_lock: Lock = self._get_port_lock()
        with port_lock:
            self.client.write_registers(start_register, values, **kwargs)

    def write_coil(self, register: int, value: bool, **kwargs: Any) -> None:
        """Write a single coil (bit) register using Modbus function code 0x05."""
        if not self.write_enabled:
            return
        if self.client is None:
            self._log.error("write_coil called before client was initialized")
            return
        kwargs = self._get_correct_device_arg(kwargs)
        port_lock: Lock = self._get_port_lock()
        with port_lock:
            self.client.write_coil(register, value, **kwargs)

    def _get_port_identifier(self) -> str:
        """
        Get a unique identifier for this transport's physical connection.
        Used to key the shared client and port lock dictionaries.
        TCP:    host_port     e.g. '192.168.1.10_502'
        Serial: port_baudrate e.g. '/dev/ttyUSB0_9600'
        """
        if self.baudrate > 0:
            # Serial/RTU transport — identified by port and baudrate
            return f"{self.port}_{self.baudrate}"
        elif self.host:
            # TCP transport — identified by host and port
            return f"{self.host}_{self.port}"
        elif self.port:
            # Fallback — port only
            return str(self.port)
        else:
            # Should never happen after successful __init__
            self._log.warning(f"No port or host set for transport '{self.transport_name}' — using transport name as port identifier")
            return self.transport_name

    def _get_port_lock(self) -> threading.Lock:
        """Get or create a lock for this transport's port"""
        port_id: str = self._get_port_identifier()

        with self._clients_lock:
            if port_id not in self._client_locks:
                self._client_locks[port_id] = threading.Lock()

        return self._client_locks[port_id]

    def _get_register_range_key(self, register_range: tuple[int, int], registry_type: Registry_Type) -> str:
        """Generate a unique key for a register range"""
        return f"{registry_type.name}_{register_range[0]}_{register_range[1]}"

    def _get_or_create_failure_tracker(self, register_range: tuple[int, int], registry_type: Registry_Type) -> RegisterFailureTracker:
        """Get or create a failure tracker for a register range"""
        key: str = self._get_register_range_key(register_range, registry_type)

        with self._failure_tracking_lock:
            if key not in self.register_failure_trackers:
                self.register_failure_trackers[key] = RegisterFailureTracker(
                    register_range=register_range,
                    registry_type=registry_type
                )

            return self.register_failure_trackers[key]

    def _record_register_read_success(self, register_range: tuple[int, int], registry_type: Registry_Type) -> None:
        """Record a successful register read"""
        if not self.enable_register_failure_tracking:
            return

        tracker: RegisterFailureTracker = self._get_or_create_failure_tracker(register_range, registry_type)
        # Only log if the last failure was after the last success (i.e., this is the first success after a failure)
        should_log_recovery: bool = tracker.last_failure_time > tracker.last_success_time
        tracker.record_success()

        if should_log_recovery:
            msg: str = (
                f"Register range {registry_type.name} {register_range[0]}-{register_range[1]} "
                f"read successfully after previous failures"
            )
            self._log.info(msg)

    def _record_register_read_failure(self, register_range: tuple[int, int], registry_type: Registry_Type) -> bool:
        """Record a failed register read, returns True if range should be disabled"""
        if not self.enable_register_failure_tracking:
            return False

        # Do not penalize register ranges for failures caused by a lost connection.
        # Offline failures are a transport problem, not a register problem — accruing
        # them would disable registers for hours after reconnection and silently stop
        # data collection.  Failure tracking is only meaningful when the transport is
        # confirmed connected so that failures reflect actual device/register issues.
        if not self.connected:
            return False

        tracker: RegisterFailureTracker = self._get_or_create_failure_tracker(register_range, registry_type)
        should_disable: bool = tracker.record_failure(self.max_failures_before_disable, self.disable_duration_hours)

        if should_disable:
            msg: str = (
                f"Register range {registry_type.name} "
                f"{register_range[0]}-{register_range[1]} "
                f"for {self.transport_name} "
                f"disabled for {self.disable_duration_hours} hours "
                f"after {tracker.failure_count} failures"
            )
            self._log.warning(msg)
        else:
            msg: str = (
                f"Register range {registry_type.name} "
                f"{register_range[0]}-{register_range[1]} "
                f"for {self.transport_name} "
                f"failed ({tracker.failure_count}/{self.max_failures_before_disable} attempts)"
            )
            self._log.warning(msg)

        return should_disable

    def _is_register_range_disabled(self, register_range: tuple[int, int], registry_type: Registry_Type) -> bool:
        """Check if a register range is currently disabled"""
        if not self.enable_register_failure_tracking:
            return False

        tracker: RegisterFailureTracker = self._get_or_create_failure_tracker(register_range, registry_type)
        return tracker.is_disabled()

    def _get_disabled_ranges_info(self) -> list[str]:
        """Get information about currently disabled register ranges"""
        disabled_info = []

        with self._failure_tracking_lock:
            for tracker in self.register_failure_trackers.values():
                if tracker.is_disabled():
                    remaining_hours: float = tracker.get_remaining_disable_time() / 3600
                    disabled_info.append(
                        f"{tracker.registry_type.name} {tracker.register_range[0]}-{tracker.register_range[1]} "
                        f"(disabled for {remaining_hours:.1f}h, {tracker.failure_count} failures)"
                    )

        return disabled_info

    def get_register_failure_status(self) -> dict:
        """Get comprehensive status of register failure tracking
            - `enabled`: Whether failure tracking is enabled
            - `max_failures_before_disable`: Configured failure threshold
            - `disable_duration_hours`: Configured disable duration
            - `total_tracked_ranges`: Total number of ranges being tracked
            - `disabled_ranges`: List of currently disabled ranges
            - `failed_ranges`: List of ranges with failures but not yet disabled
            - `successful_ranges`: List of ranges with no failures

                Each range entry contains:
                - `registry_type`: INPUT, HOLDING, COIL or DISCRETE
                - `range`: Register range (e.g., "994-999")
                - `failure_count`: Number of failures
                - `last_failure_time`: Timestamp of last failure
                - `last_success_time`: Timestamp of last success
                - `disabled_until`: Timestamp when disabled until (for disabled ranges)
                - `remaining_hours`: Hours remaining until re-enabled (for disabled ranges)
        """
        status = {
            "enabled": self.enable_register_failure_tracking,
            "max_failures_before_disable": self.max_failures_before_disable,
            "disable_duration_hours": self.disable_duration_hours,
            "total_tracked_ranges": 0,
            "disabled_ranges": [],
            "failed_ranges": [],
            "successful_ranges": []
        }

        with self._failure_tracking_lock:
            status["total_tracked_ranges"] = len(self.register_failure_trackers)

            for tracker in self.register_failure_trackers.values():
                range_info = {
                    "registry_type": tracker.registry_type.name,
                    "range": f"{tracker.register_range[0]}-{tracker.register_range[1]}",
                    "failure_count": tracker.failure_count,
                    "last_failure_time": tracker.last_failure_time,
                    "last_success_time": tracker.last_success_time
                }

                if tracker.is_disabled():
                    range_info["disabled_until"] = tracker.disabled_until
                    range_info["remaining_hours"] = tracker.get_remaining_disable_time() / 3600
                    status["disabled_ranges"].append(range_info)
                elif tracker.failure_count > 0:
                    status["failed_ranges"].append(range_info)
                else:
                    status["successful_ranges"].append(range_info)

        return status

    def reset_register_failure_tracking(self, registry_type: Optional[Registry_Type] = None, register_range: Optional[tuple[int, int]] = None) -> None:
        """Reset register failure tracking for specific ranges or all ranges"""
        with self._failure_tracking_lock:
            if registry_type is None and register_range is None:
                self.register_failure_trackers.clear()
                self._log.info("Reset all register failure tracking")
                return

            if register_range is not None:
                key: str = self._get_register_range_key(
                    register_range,
                    registry_type or Registry_Type.INPUT
                )
                if key in self.register_failure_trackers:
                    del self.register_failure_trackers[key]
                    self._log.info(
                        f"Reset failure tracking for "
                        f"{registry_type.name if registry_type else 'INPUT'} "
                        f"range {register_range[0]}-{register_range[1]}"
                    )
            else:
                # registry_type is guaranteed non-None here since both-None
                # case was handled above and register_range is None
                if registry_type is None:
                    return
                keys_to_remove: list[str] = [
                    key for key, tracker in self.register_failure_trackers.items()
                    if tracker.registry_type == registry_type
                ]
                for key in keys_to_remove:
                    del self.register_failure_trackers[key]
                self._log.info(
                    f"Reset failure tracking for all {registry_type.name} ranges "
                    f"({len(keys_to_remove)} ranges)"
                )

    def enable_register_range(self, register_range: tuple[int, int], registry_type: Registry_Type) -> None:
        """Manually enable a disabled register range"""
        tracker: RegisterFailureTracker = self._get_or_create_failure_tracker(register_range, registry_type)
        with self._failure_tracking_lock:
            tracker.disabled_until = 0
            tracker.failure_count = 0
        self._log.info(f"Manually enabled register range {registry_type.name} {register_range[0]}-{register_range[1]}")

    def init_after_connect(self) -> None:
        # Use transport lock to prevent concurrent access during initialization
        # Note: Connection-sensitive setup happens only after subclass initialization is complete.
        with self._transport_lock:
        #from transport_base settings
            if self.write_enabled:
                self.enable_write()

            #if sn is empty, attempt to auto-read it
            if not self.device_serial_number:
                self._log.info(f"Reading serial number for transport {self.transport_name} on port {getattr(self, 'port', 'unknown')}")
                self.device_serial_number = self.read_serial_number()
                self._log.info(f"Transport {self.transport_name} serial number: {self.device_serial_number}")
                self.update_identifier()
            else:
                self._log.debug(f"Transport {self.transport_name} already has serial number: {self.device_serial_number}")

    def connect(self) -> bool | None:
        """Connect to the Modbus device"""
        # Add debugging information
        port_info: Any | str = getattr(self, 'port', 'unknown')
        address_info: str = getattr(self, 'address', 'unknown')
        host_info: str = getattr(self, 'host', 'unknown')

        if address_info != 'unknown':
            self._log.info(f"Connecting to Modbus device: address={address_info} , port={port_info}")
        elif host_info != 'unknown':
            self._log.info(f"Connecting to Modbus device: host={host_info} , port={port_info}")

        # Handle first connection or reconnection
        if self.first_connect:
            self.first_connect = False
            self.init_after_connect()
        elif not self.connected:
            # Reconnection case - reinitialize after connection is established
            self._log.info(f"Reconnecting transport {self.transport_name}")
            # The actual connection is handled by subclasses (e.g., modbus_rtu)
            # We just need to reinitialize after connection
            self.init_after_connect()

        # Reset reconnection flag after successful connection
        if self.connected:
            # Reset protocol settings timestamps to ensure fresh reading
            for registry_type in [Registry_Type.INPUT, Registry_Type.HOLDING, Registry_Type.COIL, Registry_Type.DISCRETE]:
                for entry in self._protocol.registry_map.get(registry_type, []):
                    entry.next_read_timestamp = 0.0

            # Clear any failure counts and disable timers accumulated while the
            # transport was offline.  Stale strikes from a connection outage must
            # not carry over into the new session — they would unfairly disable
            # register ranges that were never actually broken.
            if self.register_failure_trackers:
                with self._failure_tracking_lock:
                    for tracker in self.register_failure_trackers.values():
                        tracker.failure_count = 0
                        tracker.disabled_until = 0.0
                        tracker.last_failure_time = 0.0
                self._log.debug(
                    f"[{self.transport_name}] Register failure counts cleared on reconnection."
                )

            # Call the post-connect read hook so subclasses can populate
            # startup caches (e.g. threshold registers, calibration values)
            # regardless of which read mode the gateway uses.  The hook runs
            # after the transport is confirmed connected and Modbus-ready.
            # On reconnect this will fire again, refreshing stale cached values.
            try:
                self.on_first_connect_read()
            except Exception:
                self._log.exception(
                    "[%s] on_first_connect_read raised an unexpected exception — "
                    "startup cache may be incomplete; defaults will be used.",
                    self.transport_name,
                )

    def cleanup(self) -> None:
        """Clean up transport resources and close connections."""
        with self._transport_lock:
            self._log.info(f"Cleaning up transport {self.transport_name}")

            # Reset register timestamps to prevent sharing issues between transports
            self._protocol.reset_register_timestamps()

            # Close the modbus client connection
            port_identifier: str = self._get_port_identifier()
            if port_identifier in self.clients:
                try:
                    client: ModbusBaseClient = self.clients[port_identifier]
                    if hasattr(client, 'close') and callable(client.close):
                        client.close()
                        self._log.info(f"Closed modbus client for {self.transport_name}")
                except Exception as e:
                    self._log.warning(f"Error closing modbus client for {self.transport_name}: {e}")

                # Remove from shared clients dict
                with self._clients_lock:
                    if port_identifier in self.clients:
                        del self.clients[port_identifier]
                        self._log.info(f"Removed client from shared dict for {self.transport_name}")

            # super().cleanup() sets connected = False which flows through the
            # property setter — handling _needs_reconnection, logging, and
            # notification automatically. Must come after the socket is closed
            # so the state transition reflects reality.
            super().cleanup()
            self.first_connect = False  # Reset so reconnection works properly
            self._log.info(f"Transport {self.transport_name} cleanup completed")

    def read_serial_number(self) -> str:
        """
        Attempts to read the device serial number from registers.
        Tries 'Serial_Number' variable first, then falls back to
        concatenating 'Serial No 1-5' for both Holding and Input registers.
        Respects the send_holding_register and send_input_register flags to determine which registry types to read from.
        """

        # Try single-register 'Serial_Number' variable
        if self.send_holding_register:
            sn: str | None = self._read_sn_from_registry(Registry_Type.HOLDING)
            if sn:
                return sn

        if self.send_input_register:
            sn: str | None = self._read_sn_from_registry(Registry_Type.INPUT)
            if sn:
                return sn

        # Fall back to concatenating Serial No 1-5
        # Checks Holding first, then Input if flags allow
        for r_type in [Registry_Type.HOLDING, Registry_Type.INPUT]:
            if r_type == Registry_Type.HOLDING and not self.send_holding_register:
                continue
            if r_type == Registry_Type.INPUT and not self.send_input_register:
                continue

            sn_result: str = self._read_concatenated_sn(r_type)
            if sn_result:
                return sn_result

        return ""

    def _read_sn_from_registry(self, registry_type) -> str | None:
        """Helper for single-variable lookup."""
        self._log.info(f"Looking for serial_number in {registry_type.name}...")
        result: int | float | str | None = self.read_variable("Serial_Number", registry_type)
        if result is not None:
            sn = str(result)
            if sn and sn != "None":
                self._log.info(f"Read SN from {registry_type.name}: {sn}")
                return sn
        return None

    def _read_concatenated_sn(self, r_type) -> str:
        """Helper to build SN from multiple registers (Serial No 1-5)."""
        sn_decoded: str = ""
        fields: list[str] = ["Serial No 1", "Serial No 2", "Serial No 3", "Serial No 4", "Serial No 5"]

        for snfield in fields:
            # Use appropriate lookup method for the registry type
            if r_type == Registry_Type.HOLDING:
                entry: registry_map_entry | None = self._protocol.get_registry_entry(snfield, registry_type=Registry_Type.HOLDING)
            else:
                entry = self._protocol.get_registry_entry(snfield, registry_type=Registry_Type.INPUT)

            if entry is None:
                continue

            data: Dict[int, int] = self.read_modbus_registers(start=entry.register, end=entry.register, registry_type=r_type)
            if not data or entry.register not in data:
                return "" # Treat partial failure as total failure for SN integrity

            val: int = data[entry.register]
            try:
                # Convert register int to bytes then decode utf-8
                chunk: str = val.to_bytes((val.bit_length() + 7) // 8, "big").decode("utf-8")
                sn_decoded += chunk
            except UnicodeDecodeError:
                self._log.warning(f"Could not decode {field} in {r_type.name}")

            time.sleep(self.modbus_delay * 2)

        # Validate the final string (alphanumeric and underscores only)
        if sn_decoded and not re.search(r"[^a-zA-Z0-9_]", sn_decoded):
            return sn_decoded
        return ""

    def enable_write(self) -> None:
        if self.write_enabled and self.write_mode == TransportWriteMode.UNSAFE:
            self._log.warning("enable write - WARNING - UNSAFE MODE - validation SKIPPED")
            return

        self._log.info("Validating Protocol for Writing")
        self.write_enabled = False

        # Add a small delay to ensure device is ready, especially during initialization
        time.sleep(self.modbus_delay * 2)

        try:
            # Explicit — validates holding registers
            score_percent: float = self.validate_protocol(Registry_Type.HOLDING)
            if(score_percent > 90):
                self.write_enabled = True
                self._log.warning("enable write - validation passed")
            elif self.write_mode == TransportWriteMode.RELAXED:
                self.write_enabled = True
                self._log.warning("enable write - WARNING - RELAXED MODE")
            else:
                self._log.error("enable write FAILED - WRITE DISABLED")
        except Exception as e:
            self._log.error(f"enable write FAILED due to error: {str(e)}")
            if self.write_mode == TransportWriteMode.RELAXED:
                self.write_enabled = True
                self._log.warning("enable write - WARNING - RELAXED MODE (due to validation error)")
            else:
                self._log.error("enable write FAILED - WRITE DISABLED")

    def write_data(self, data: dict[str, int | float | str ], from_transport: transport_base) -> None:
        with self._transport_lock:
            if not self.write_enabled:  # guard for checking inverter scraper flag to allow write back to the inverter.
                return

            # Search writable registry types in priority order.
            # COIL is included because coil registers support writes.
            # DISCRETE is intentionally excluded — discrete inputs are read-only by the Modbus spec.
            writable_registry_types = [
                (Registry_Type.HOLDING, self._protocol.get_registry_map(Registry_Type.HOLDING)),
                (Registry_Type.COIL,    self._protocol.get_registry_map(Registry_Type.COIL)),
            ]

            for variable_name, value in data.items():
                entry: registry_map_entry | None = None
                matched_registry_type: Registry_Type = Registry_Type.HOLDING

                for reg_type, registry_map in writable_registry_types:
                    for e in registry_map:
                        if e.variable_name == variable_name:
                            entry = e
                            matched_registry_type = reg_type
                            break
                    if entry is not None:
                        break

                if entry is not None:
                    # Respect the entry's write_mode — skip read-only and disabled entries.
                    if entry.write_mode in (WriteMode.READ, WriteMode.READDISABLED):
                        self._log.debug(
                            f"Skipping write for '{variable_name}' — write_mode is {entry.write_mode.name}"
                        )
                        continue
                    # Pass value through unchanged — write_variable handles
                    # int, float, and str (code values) natively
                    self.write_variable(entry, value, matched_registry_type)

            time.sleep(self.modbus_delay) #sleep in between requests so modbus can rest

    def read_data(self) -> dict[str, int | float | str ]:
        # Use transport lock to prevent concurrent access to this transport instance
        with self._transport_lock:
            self._start_cycle_tracking()
            # Add debugging information
            port_info = getattr(self, 'port', 'unknown')
            address_info = getattr(self, 'address', 'unknown')
            self._log.debug(f"Reading data from {self.transport_name}: address={address_info}, port={port_info}")

            info: dict[str, int | float | str] = {}
            #modbus - only read input/holding/coil/discrete registries
            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING, Registry_Type.COIL, Registry_Type.DISCRETE):

                if not self._should_send_registry_type(registry_type):
                    continue

                #calculate ranges dynamically -- for variable read timing
                ranges: list[tuple[int, int]] = self._protocol.calculate_registry_ranges(
                    self._protocol.registry_map[registry_type],
                    self._protocol.registry_map_size[registry_type],
                    timestamp=self.last_read_time,
                )

                self._log.info(f"Reading {registry_type.name} registers for {self.transport_name}: {len(ranges)} ranges")
                if len(ranges) == 0:
                    self._log.warning(f"No register ranges calculated for {self.transport_name} {registry_type.name}")
                    # Debug: show protocol settings info
                    total_entries: int = len(self._protocol.registry_map.get(registry_type, []))
                    self._log.info(f"Protocol settings for {self.transport_name}: {total_entries} total entries for {registry_type.name}")

                    # Count entries that would be read
                    readable_entries: int = 0
                    for entry in self._protocol.registry_map.get(registry_type, []):
                        if entry.write_mode != WriteMode.READDISABLED and entry.write_mode != WriteMode.WRITEONLY:
                            readable_entries += 1
                    self._log.info(f"Readable entries for {self.transport_name} {registry_type.name}: {readable_entries}")

                registry: Dict[int, int] = self.read_modbus_registers(ranges=ranges, registry_type=registry_type)

                if registry:
                    self._log.info(f"Got registry data for {self.transport_name} {registry_type.name}: {len(registry)} registers")
                else:
                    self._log.warning(f"No registry data returned for {self.transport_name} {registry_type.name}")

                new_info: Dict[str, int | float | str ] = self._protocol.process_registery(registry, self._protocol.get_registry_map(registry_type))

                if False:
                    new_info = {self.__input_register_prefix + key: value for key, value in new_info.items()}

                info.update(new_info)

            if not info:
                self._log.info("Register is Empty; transport busy?")

            # Log disabled ranges status periodically (every 10 minutes)
            if self.enable_register_failure_tracking and time.time() - self._last_disabled_status_log > 600:
                disabled_ranges: list[str] = self._get_disabled_ranges_info()
                if disabled_ranges:
                    self._log.info(f"Currently disabled register ranges: {len(disabled_ranges)}")
                    for range_info in disabled_ranges:
                        self._log.info(f"  - {range_info}")
                self._last_disabled_status_log = time.time()

            self._finish_cycle_tracking(info)
            return info

    def read_group_data(self, members: list[transport_base]) -> dict[str, int | float | str]:
        """
        Read one consolidated payload for all transports sharing this physical
        Modbus endpoint. The gateway stays transport-agnostic; Modbus-specific
        batching lives here.

        Each member's entries are decoded through their own protocolSettings
        so per-member adjustments, unit modifiers, and code lookups are applied
        correctly regardless of whether all members share the same protocol.
        """
        with self._transport_lock:
            self._start_cycle_tracking()
            port_info = getattr(self, 'port', 'unknown')
            address_info = getattr(self, 'address', 'unknown')
            self._log.debug(
                f"Reading grouped data from {self.transport_name}: "
                f"address={address_info}, port={port_info}, members={len(members)}"
            )

            info: dict[str, int | float | str] = {}

            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING,
                                Registry_Type.COIL, Registry_Type.DISCRETE):

                # Build union respecting each member's own send_*_register flags
                union_entries: list[registry_map_entry] = []
                seen: set[tuple[int, str]] = set()
                max_register: int = 0

                # Track which entries belong to which member's protocol for
                # per-member decoding after the physical read.
                member_entry_map: dict[int, tuple[protocol_settings, list[registry_map_entry]]] = {}

                for member in members:
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
                        continue

                    member_entries: list[registry_map_entry] = ps.registry_map.get(registry_type, [])
                    if not member_entries:
                        continue

                    member_entry_map[id(member)] = (ps, member_entries)

                    for entry in member_entries:
                        key: tuple[int, str] = (entry.register, entry.variable_name)
                        if key in seen:
                            continue
                        seen.add(key)
                        union_entries.append(entry)
                        if entry.register > max_register:
                            max_register = entry.register

                if not union_entries:
                    continue

                ranges: list[tuple[int, int]] = self._protocol.calculate_registry_ranges(
                    union_entries,
                    max_register,
                    timestamp=self.last_read_time,
                    init=True,
                )

                self._log.debug(
                    f"Reading grouped {registry_type.name} registers for {self.transport_name}: "
                    f"{len(ranges)} ranges across {len(union_entries)} entries"
                )

                registry: dict[int, int] = self.read_modbus_registers(
                    ranges=ranges, registry_type=registry_type
                )

                if not registry:
                    self._log.warning(f"No grouped registry data returned for {self.transport_name} {registry_type.name}")
                    continue

                # Decode each member's entries through their own protocolSettings
                # so adjustments and code lookups use the correct protocol context.
                for member in members:
                    entry_data = member_entry_map.get(id(member))
                    if entry_data is None:
                        continue
                    ps, member_entries = entry_data
                    info.update(ps.process_registery(registry, member_entries))

            if not info:
                self._log.info("Grouped register read returned no data; transport busy?")

            self._finish_cycle_tracking(info)
            return info

    def interleaved_cycle_timeout(self) -> float:
        """
        Estimate a realistic timeout for one interleaved full-cycle read based
        on protocol ranges, Modbus timeout, and retry policy.
        """
        total_ranges: int = sum(
            len(self._protocol.registry_map_ranges.get(registry_type, []))
            for registry_type in self.registry_map
        )
        timeout_per_block: float = getattr(self, 'modbus_timeout', 10.0) * (
            getattr(self, 'max_retries_per_block', 3) + 1
        )
        return timeout_per_block * max(total_ranges, 5)

    def validate_protocol(self, registry_type: Registry_Type) -> float:
        """
        Validates the protocol by reading registers and scoring results
        against expected value ranges defined in the protocol CSV.

        Args:
            registry_type: Which register bank to validate (required).
                           Pass Registry_Type.HOLDING for write validation,
                           Registry_Type.INPUT for read validation.
        Returns:
            Score percentage 0.0-100.0 indicating proportion of registers
            returning valid values.
        """
        return self.validate_registry(registry_type)


    def validate_registry(self, registry_type: Registry_Type = Registry_Type.INPUT) -> float:
        registry_map: list[registry_map_entry] = self._protocol.get_registry_map(registry_type)
        register_readings: dict[str, int | float | str] = self.read_registry(registry_type)

        score: float = 0.0
        for entry in registry_map:
            if entry.variable_name in register_readings:
                evaluate: bool = True
                if entry.concatenate and entry.register != entry.concatenate_registers[0]:
                    evaluate = False
                if evaluate:
                    score += self._protocol.validate_registry_entry(entry, register_readings[entry.variable_name])

        # Adjust max score to exclude write-only and disabled registers.
        # Concatenated continuation registers still count here; a valid
        # concatenated value is expected to return len(entry.concatenate_registers)
        # from validate_registry_entry so the numerator and denominator stay aligned.
        max_score: int = sum(
            1 for entry in registry_map
            if entry.write_mode not in (WriteMode.WRITEONLY, WriteMode.READDISABLED)
        )

        if max_score == 0:
            self._log.warning(
                f"validate_registry: no readable entries in "
                f"{registry_type.name} registry — returning 0"
            )
            return 0.0

        percent: float = (score * 100) / max_score
        self._log.info(
            f"Validation score: {score:.0f} of {max_score} "
            f"({round(percent)}%) for {registry_type.name}"
        )
        return percent

    def capture_analysis_scan(
        self,
        start: int = 0,
        end: int = 65535,
        batch_size: int = 40,
        delay: float = 0.05,
        include_holding: bool = True,
        include_coil: bool = False,
        include_discrete: bool = False,
        progress_cb=None,
    ) -> dict[str, dict[int, int]]:
        """
        Perform a dense raw Modbus scan for UI-driven protocol analysis.
        Returns in-memory register maps only; no _analysis files are used.

        Args:
            progress_cb: Optional callable(phase: str, done: int, total: int).
                         Called after each batch so callers can stream progress.
                         phase is "input" or "holding".
        """
        input_result: dict[int, int] = {}
        holding_result: dict[int, int] = {}
        coil_result: dict[int, int] = {}
        discrete_result: dict[int, int] = {}

        self._log.info(
            "[%s] Starting analysis scan: range=%d-%d batch=%d — "
            "normal scraper reads suspended for duration",
            self.transport_name,
            start,
            end,
            batch_size,
        )

        def scan_range(registry_type: Registry_Type, result_dict: dict[int, int]) -> None:
            total_reads = 0
            failures = 0
            phase = registry_type.name.lower()
            total_batches: int = max(1, (end - start) // batch_size + 1)
            batches_done = 0

            for addr in range(start, end + 1, batch_size):
                range_count: int = min(batch_size, end - addr + 1)
                register_range: tuple[int, int] = (addr, range_count)

                # NOTE: deliberately bypass the failure tracker here.
                # capture_analysis_scan is an intentional dense sweep of the
                # full address space — skipping disabled ranges would produce
                # empty results because the normal scraper marks most
                # out-of-protocol addresses as disabled after the first failed
                # read.  We also do not write back to the failure tracker so
                # this scan never pollutes the scraper's disabled-range state.

                try:
                    response = self.read_registers(
                        addr,
                        range_count,
                        registry_type=registry_type,
                    )
                    if response is None:
                        msg = f"read_registers returned None for range {addr}-{addr + range_count - 1}"
                        raise RuntimeError(msg)  # noqa: TRY301

                    extracted: Dict[int, int] | None = self._extract_response_values(response, registry_type, register_range)
                    if extracted is None:
                        msg: str = (f"Response for {registry_type.name} {addr}-{addr + range_count - 1} "
                            f"missing expected attribute (.registers or .bits)")

                        raise RuntimeError(msg)  # noqa: TRY301

                    result_dict.update(extracted)
                    total_reads += 1
                except Exception as exc:
                    failures += 1
                    self._log.debug(
                        "[%s] Read failed %s %d-%d (%s)",
                        self.transport_name,
                        registry_type.name,
                        addr,
                        addr + range_count - 1,
                        str(exc),
                    )

                batches_done += 1
                if progress_cb is not None:
                    try:
                        progress_cb(phase, batches_done, total_batches)
                    except Exception as exc:
                        self._log.debug(
                            "[%s] Progress callback failed (%s)",
                            self.transport_name,
                            str(exc),
                        )

                time.sleep(delay)

            self._log.info(
                "[%s] Analysis scan complete %s: reads=%d failures=%d collected=%d",
                self.transport_name,
                registry_type.name,
                total_reads,
                failures,
                len(result_dict),
            )

        # Acquire the transport lock for the entire scan so the normal scraper
        # read/write cycle cannot interleave with analysis reads on the same
        # physical connection.  read_data() and read_group_data() both acquire
        # this lock, so they will block until the scan completes.
        with self._transport_lock:
            scan_range(Registry_Type.INPUT, input_result)
            if include_holding:
                scan_range(Registry_Type.HOLDING, holding_result)
            if include_coil:
                scan_range(Registry_Type.COIL, coil_result)
            if include_discrete:
                scan_range(Registry_Type.DISCRETE, discrete_result)

        return {"input": input_result, "holding": holding_result, "coil": coil_result, "discrete": discrete_result}

    # ------------------------------------------------------------------
    # Range-validation helpers used by analyze_protocols
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_values_range(
        values_str: str,
    ) -> tuple[float, float] | list[float] | None:
        """
        Parse the 'values' column from a protocol CSV into a constraint
        that can be used to range-check a raw hardware reading.

        Supported formats
        -----------------
        Numeric range    : "0-65535"  "0.0-1.5"  "-10-10"
        Enum / set       : "0,1,2"   "0, 1, 2"
        Single value     : "42"
        Free text        : "see manual"  → returns None (cannot validate)

        Returns
        -------
        (lo, hi) for a continuous range, list[float] for a discrete set,
        or None when the string cannot be interpreted as a numeric constraint.
        """
        # Guard: the protocol parser may store the values field as a list of
        # decoded enum values rather than the raw CSV string.  If we receive
        # a list, it is already the discrete constraint we need.
        if isinstance(values_str, list):
            try:
                return [float(v) for v in values_str if v is not None]
            except (TypeError, ValueError):
                return None

        s: str = (values_str or "").strip()
        if not s:
            return None

        # Continuous numeric range: handles "-10-10", "0-65535", "0.5-1.5".
        # Pattern: optional leading minus, digits/dot, dash separator,
        # optional minus, digits/dot.
        range_match: re.Match[str] | None = re.fullmatch(
            r"(-?\d+(?:\.\d+)?)\s*[-\u2013]\s*(-?\d+(?:\.\d+)?)", s
        )
        if range_match:
            lo: float = float(range_match.group(1))
            hi: float = float(range_match.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return (lo, hi)

        # Comma-separated discrete set: "0,1,2"
        parts: list[str] = [p.strip() for p in s.split(",")]
        if len(parts) > 1:
            try:
                floats: list[float] = [float(p) for p in parts if p]
                if floats:
                    return floats
            except ValueError as exc:
                modbus_base._log.debug(
                    "Failed to parse discrete set '%s': %s",
                    s,
                    str(exc),
                )

        # Single bare number: "42"
        try:
            v = float(s)
        except ValueError as exc:
            modbus_base._log.debug(
                "Failed to parse single value '%s': %s",
                s,
                str(exc),
            )
        else:
            return (v, v)

        return None

    @staticmethod
    def _value_in_range(raw_value: int | float, constraint: tuple[float, float] | list[float] | None) -> bool:
        """
        Return True when raw_value satisfies the parsed constraint.
        Returns True (no penalty) when constraint is None so that entries
        with free-text or missing values columns are not penalized.
        """
        if constraint is None:
            return True
        if isinstance(constraint, tuple):
            lo, hi = constraint
            return lo <= raw_value <= hi
        # list — discrete set
        return float(raw_value) in constraint

    # ------------------------------------------------------------------

    def analyze_protocols(
        self,
        protocol_names: list[str],
        current_protocol: str | None = None,
        progress_cb=None,
        batch_size: int = 40,
    ) -> dict[str, Any]:
        """
        Compare a live Modbus scan against selected protocol maps and return
        scores plus add/remove suggestions for the web UI.

        Scoring
        -------
        Each readable entry contributes up to 1.0 to the raw match total:
          1.0 — register present in scan, passes type validation, AND raw
                integer value is within the documented values/range column.
          0.5 — register present, passes type validation, but raw value falls
                outside the documented range (possible wrong protocol or
                miscalibrated device).
          0.0 — register absent from scan entirely.

        Entries whose values column cannot be parsed as a numeric constraint
        (e.g. free text) are treated as in-range (no penalty) so that
        unspecified ranges do not unfairly reduce the score.
        """
        scan: Dict[str, Dict[int, int]] = self.capture_analysis_scan(progress_cb=progress_cb, batch_size=batch_size)
        raw_input: Dict[int, int] = scan["input"]
        raw_holding: Dict[int, int] = scan["holding"]
        raw_coil: Dict[int, int] = scan["coil"]
        raw_discrete: Dict[int, int] = scan["discrete"]

        protocols: dict[str, protocol_settings] = {}
        for name in protocol_names:
            try:
                protocols[name] = protocol_settings(name)
            except Exception as exc:
                self._log.warning("Failed loading protocol %s: %s", name, exc)

        results: dict[str, Any] = {}
        for name, proto in protocols.items():
            # Explicitly load registry types before scoring.
            # get_registry_map() does a bare dict lookup with no load
            # fallback, so we must call load_registry_map() first.
            for reg_type in [Registry_Type.INPUT, Registry_Type.HOLDING, Registry_Type.COIL, Registry_Type.DISCRETE]:
                if reg_type not in proto.registry_map:
                    try:
                        proto.load_registry_map(reg_type)
                    except Exception as exc:
                        self._log.warning("Could not load %s registry for %s: %s", reg_type.name, name, exc)

            protocol_result: dict[str, Any] = {
                "protocol_name": name,
                "is_current": name == (current_protocol or ""),
                "scores": {},
                "actions": {},
            }

            for reg_type, reg_key, raw_map in (
                (Registry_Type.INPUT, "input", raw_input),
                (Registry_Type.HOLDING, "holding", raw_holding),
                (Registry_Type.COIL, "coil", raw_coil),
                (Registry_Type.DISCRETE, "discrete", raw_discrete),
            ):
                # Safe access — fall back to empty list if load failed
                entries: list[registry_map_entry] = proto.registry_map.get(reg_type, [])
                decoded_values: Dict[str, int | float | str] = proto.process_registery(raw_map, entries) if (raw_map and entries) else {}

                known_registers: set[int] = {
                    entry.register for entry in entries
                    if entry.write_mode not in (WriteMode.WRITEONLY, WriteMode.READDISABLED)
                }
                readable_entries: list[registry_map_entry] = [
                    entry for entry in entries
                    if entry.write_mode not in (WriteMode.WRITEONLY, WriteMode.READDISABLED)
                ]

                # --- scoring ---
                # Use float accumulator so partial credit (0.5) is preserved.
                matches: float = 0.0
                for entry in readable_entries:
                    value: int | float | str | None = decoded_values.get(entry.variable_name)
                    if value is None:
                        # Register absent from scan — contributes 0
                        continue

                    # Skip continuation registers of concatenated entries;
                    # only the first register drives the decoded value.
                    if entry.concatenate and entry.register != entry.concatenate_registers[0]:
                        continue

                    base_valid: bool = bool(proto.validate_registry_entry(entry, value))

                    # Range-check the raw integer returned by the hardware
                    # against the values/range column declared in the CSV.
                    # entry.values may be a list (decoded enum values from the
                    # protocol parser) or a str (raw CSV text).  Pass whichever
                    # it is — _parse_values_range handles both forms.
                    # A failed range check is a genuine mismatch signal —
                    # it means the value at that register address is not what
                    # this protocol expects, which is exactly what we want to
                    # detect when comparing protocols against unknown hardware.
                    values_raw = (
                        getattr(entry, "values_range", None)
                        or getattr(entry, "values", None)
                    )
                    constraint: tuple[float, float] | list[float] | None = self._parse_values_range(values_raw) if values_raw is not None else None
                    raw_int: int | None = raw_map.get(entry.register)
                    in_range: bool = (
                        self._value_in_range(raw_int, constraint)
                        if raw_int is not None
                        else True   # register absent — no penalty
                    )

                    if base_valid and in_range:
                        matches += 1.0
                    elif base_valid and not in_range:
                        # Type-valid but wrong range — partial credit.
                        # This most commonly means a register exists at this
                        # address but belongs to a different protocol.
                        matches += 0.5
                    # base_valid False → 0.0 regardless of range

                total: int = len(readable_entries)
                missing_in_scan: list[int] = sorted(reg for reg in known_registers if reg not in raw_map)
                unknown_in_scan: list[int] = sorted(reg for reg in raw_map.keys() if reg not in known_registers)
                accuracy: float = round((matches / total) * 100, 2) if total else 0.0

                # --- removable suggestions ---
                # Bug fix: use register as the primary key so entries are
                # grouped by physical address rather than by name, avoiding
                # accidental multi-row matches on shared documented_names.
                removable: list[dict[str, Any]] = []
                entries_by_register: dict[int, list[registry_map_entry]] = {}
                for entry in entries:
                    entries_by_register.setdefault(entry.register, []).append(entry)

                for reg in missing_in_scan:
                    for entry in entries_by_register.get(reg, []):
                        removable.append({
                            "register_address": str(reg),
                            "variable_name": entry.variable_name,
                            "documented_name": entry.documented_name,
                            "data_type": entry.data_type.name if entry.data_type else "",
                            "read_interval": str(entry.read_interval) if entry.read_interval is not None else "",
                            "write_mode": {
                                WriteMode.READ: "R",
                                WriteMode.READDISABLED: "RD",
                                WriteMode.WRITE: "W",
                                WriteMode.WRITEONLY: "WO",
                            }.get(entry.write_mode, "R"),
                            "note": entry.note or "",
                        })

                # TODO
                # Include out_of_range so the UI can warn when a newly
                # discovered register's raw value is already outside the
                # default documented range.
                addable: list[dict[str, Any]] = []
                default_range_str = "0-65535"
                default_constraint: tuple[float, float] | list[float] | None = self._parse_values_range(default_range_str)
                for reg in unknown_in_scan:
                    raw_val: int | None = raw_map.get(reg)
                    out_of_range: bool = (
                        raw_val is not None
                        and not self._value_in_range(raw_val, default_constraint)
                    )
                    addable.append({
                        "register_address": str(reg),
                        "variable_name": f"register_{reg}",
                        "documented_name": f"Register {reg}",
                        "data_type": "ushort",
                        "values_range": default_range_str,
                        "unit": "",
                        "read_interval": "",
                        "write_mode": "R",
                        "note": "",
                        "raw_value": raw_val,
                        "out_of_range": out_of_range,
                    })

                protocol_result["scores"][reg_key] = {
                    "matches": matches,
                    "total": total,
                    "missing_in_scan": len(missing_in_scan),
                    "unknown_in_scan": len(unknown_in_scan),
                    "accuracy": accuracy,
                }
                protocol_result["actions"][reg_key] = {
                    "add": addable,
                    "remove": removable,
                }

            results[name] = protocol_result

        return {
            "transport_name": self.transport_name,
            "current_protocol": current_protocol or "",
            "scan_counts": {
                "input": len(raw_input),
                "holding": len(raw_holding),
            },
            "protocols": results,
        }


    def write_variable(self, entry : registry_map_entry, value :int | float | str, registry_type : Registry_Type = Registry_Type.HOLDING) -> None:
        """ writes a value to a ModBus register"""

        if isinstance(value, str):
            value = value.strip().lower()

        # ------------------------------------------------------------------ #
        # COIL fast path — booleans need no multi-word read-back, no type     #
        # encoding, and no byte-order handling.  Resolve the value to a bool  #
        # and write directly via FC 0x05.                                      #
        # ------------------------------------------------------------------ #
        if registry_type == Registry_Type.COIL:
            if isinstance(value, str):
                coil_bool: bool = value not in ("0", "false", "off", "no", "")
            else:
                coil_bool = bool(int(float(value))) if value != "" else False
            self._log.info(f"WRITE COIL: {entry.variable_name} => {coil_bool} to Register {entry.register}")
            self.write_coil(entry.register, coil_bool)
            return

        temp_map: list[registry_map_entry] = [entry]
        word_count = self._entry_word_count(entry)
        registry: Dict[int, int] = self.read_modbus_registers(
            start=entry.register,
            end=entry.register + word_count - 1,
            registry_type=registry_type,
        )
        info: Dict[str, int | float | str] = self._protocol.process_registery(registry, temp_map)

        raw_registers: list[int] = []
        for offset in range(word_count):
            raw_word: int | None = registry.get(entry.register + offset)
            if raw_word is None:
                self._log.error(
                    f"WRITE_ERROR: Register {entry.register + offset} not found in registry "
                    f"for '{entry.variable_name}'. Unsafe to write."
                )
                return
            raw_registers.append(raw_word)

        word_order: WordOrder = self._entry_byte_order(entry)
        raw_bytes: bytes = self._register_words_to_bytes(raw_registers, word_order)
        raw_value: int = int.from_bytes(raw_bytes, byteorder="big", signed=False)
        total_bits: int = len(raw_bytes) * 8

        if entry.variable_name not in info:
            self._log.error(f"WRITE_ERROR: Could not decode current value for '{entry.variable_name}'. Unsafe to write.")
            return

        #read current decoded value
        current_value: str = str(info[entry.variable_name])

        #handle codes
        if isinstance(value, str):
            value = self._protocol.get_code_by_value(entry, value, fallback=value)
            current_value = self._protocol.get_code_by_value(entry, current_value, fallback=current_value)

        if self.write_mode != TransportWriteMode.UNSAFE:
            if not self._protocol.validate_registry_entry(entry, current_value):
                return self._log.error(f"WRITE_ERROR: Invalid value in register '{current_value}'. Unsafe to write")
                #raise ValueError(err)

            if not (entry.data_type == Data_Type._16BIT_FLAGS or entry.data_type == Data_Type._8BIT_FLAGS or entry.data_type == Data_Type._32BIT_FLAGS): #skip validation for write; validate further down
                if not self._protocol.validate_registry_entry(entry, value):
                    return self._log.error(f"WRITE_ERROR: Invalid new value, '{value}'. Unsafe to write")

        #apply unit_mod before writing.
        if entry.unit_mod != 1:  # say unitmod is 0.1. 105*0.1 = 10.5. 10.5 / 0.1 = 105.
            try:
                # int(float(value) / entry.unit_mod) truncates — int(10.9) → 10.
                # This could introduce a 1-unit error for values that don't divide cleanly. round() is more accurate:
                value = int(round(float(value) / entry.unit_mod))
            except (ValueError, TypeError):
                self._log.error(
                    f"WRITE_ERROR: Cannot apply unit_mod to non-numeric value "
                    f"'{value}' for entry '{entry.variable_name}'. Unsafe to write."
                )
                return

        register_values: list[int] | None = None

        if entry.data_type == Data_Type.USHORT:
            ushort_value = int(value)
            if ushort_value < 0 or ushort_value > 65535:
                 raise ValueError("Invalid value")
            register_values = [ushort_value]

        elif entry.data_type == Data_Type.SHORT:
            int_val: int = int(float(value))
            if int_val < -32768 or int_val > 32767:
                self._log.error(
                    f"WRITE_ERROR: Value '{int_val}' out of SHORT range "
                    f"(-32768 to 32767) for '{entry.variable_name}'. Unsafe to write."
                )
                return
            register_values = [int_val & 0xFFFF]

        elif entry.data_type == Data_Type.UINT:
            int_val = int(float(value))
            if int_val < 0 or int_val > 0xFFFFFFFF:
                self._log.error(
                    f"WRITE_ERROR: Value '{int_val}' out of UINT range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            register_values = self._bytes_to_register_words(
                int_val.to_bytes(4, byteorder="big", signed=False),
                word_order,
            )

        elif entry.data_type == Data_Type.ACC32:
            int_val = int(float(value))
            if int_val < 0 or int_val > 0xFFFFFFFF:
                self._log.error(
                    f"WRITE_ERROR: Value '{int_val}' out of ACC32 range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            register_values = self._bytes_to_register_words(
                int_val.to_bytes(4, byteorder="big", signed=False),
                word_order,
            )

        elif entry.data_type == Data_Type.UINT64:
            int_val = int(float(value))
            if int_val < 0 or int_val > 0xFFFFFFFFFFFFFFFF:
                self._log.error(
                    f"WRITE_ERROR: Value '{int_val}' out of UINT64 range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            register_values = self._bytes_to_register_words(
                int_val.to_bytes(8, byteorder="big", signed=False),
                word_order,
            )

        elif entry.data_type == Data_Type.INT:
            int_val = int(float(value))
            if int_val < -2147483648 or int_val > 2147483647:
                self._log.error(
                    f"WRITE_ERROR: Value '{int_val}' out of INT range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            register_values = self._bytes_to_register_words(
                int_val.to_bytes(4, byteorder="big", signed=True),
                word_order,
            )

        elif entry.data_type == Data_Type.FLOAT32:
            register_values = self._bytes_to_register_words(
                struct.pack(">f", float(value)),
                word_order,
            )

        elif entry.data_type == Data_Type.FLOAT64:
            register_values = self._bytes_to_register_words(
                struct.pack(">d", float(value)),
                word_order,
            )

        elif entry.data_type in (Data_Type._16BIT_FLAGS, Data_Type._8BIT_FLAGS, Data_Type._32BIT_FLAGS):
            flag_size: int = Data_Type.getSize(entry.data_type)
            value_str: str = str(value)

            if not re.match(rf"^[0-1]{{{flag_size}}}$", value_str):
                self._log.error(
                    f"WRITE_ERROR: Invalid new value for bitflags, '{value_str}'. Unsafe to write")
                return

            flag_int: int = int(value_str[::-1], 2)
            bit_index: int = entry.register_bit if entry.register_bit >= 0 else 0
            bit_mask: int = ((1 << flag_size) - 1) << bit_index
            clear_mask: int = ~bit_mask & ((1 << total_bits) - 1)
            updated_value: int = (raw_value & clear_mask) | ((flag_int << bit_index) & bit_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                word_order,
            )

        elif entry.data_type == Data_Type.BYTE or (200 < entry.data_type.value < 300): # unsigned sub-register field
            bit_size: int = Data_Type.getSize(entry.data_type) # Assuming 8, 16, or 32
            bit_index: int = entry.register_bit if entry.register_bit >= 0 else 0
            base_mask: int = (1 << bit_size) - 1
            bit_mask: int = base_mask << bit_index
            new_val = int(value)
            if 0 > new_val or new_val > base_mask:
                return self._log.error(f"WRITE_ERROR: Invalid new value '{value}'. Exceeds {base_mask}.")
            clear_mask: int = ~bit_mask & ((1 << total_bits) - 1)
            updated_value: int = (raw_value & clear_mask) | ((new_val << bit_index) & bit_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                word_order,
            )

            check_value: int = (updated_value >> bit_index) & base_mask
            if check_value != new_val:
                msg: str = (f"Bitwise mismatch: Expected {new_val}, got {check_value}")
                raise ValueError(msg)

        elif 300 < entry.data_type.value < 400:  # signed bit types
            bit_size: int = Data_Type.getSize(entry.data_type)
            bit_index: int = entry.register_bit if entry.register_bit >= 0 else 0
            min_val: int = -(1 << (bit_size - 1))
            max_val: int = (1 << (bit_size - 1)) - 1
            signed_val = int(value)
            if signed_val < min_val or signed_val > max_val:
                self._log.error(
                    f"WRITE_ERROR: Value '{signed_val}' out of signed {bit_size}-bit range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            encoded: int = signed_val & ((1 << bit_size) - 1)
            bit_mask = ((1 << bit_size) - 1) << bit_index
            clear_mask = ~bit_mask & ((1 << total_bits) - 1)
            updated_value = (raw_value & clear_mask) | ((encoded << bit_index) & bit_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                word_order,
            )

        elif entry.data_type.value > 400:  # signed magnitude bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            max_magnitude: int = (1 << (bit_size - 1)) - 1
            signed_val = int(value)
            if abs(signed_val) > max_magnitude:
                self._log.error(
                    f"WRITE_ERROR: Value '{signed_val}' out of signed-magnitude {bit_size}-bit range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            magnitude: int = abs(signed_val)
            encoded: int = magnitude << (bit_index + 1)
            if signed_val < 0:
                encoded |= (1 << bit_index)
            field_mask: int = ((1 << bit_size) - 1) << bit_index
            clear_mask = ~field_mask & ((1 << total_bits) - 1)
            updated_value = (raw_value & clear_mask) | (encoded & field_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                word_order,
            )

        elif entry.data_type == Data_Type.ASCII:
            self._log.error(
                f"WRITE_ERROR: ASCII register writes require protocol-specific "
                f"character packing for '{entry.variable_name}'. "
                f"Not currently supported. Unsafe to write."
            )
            return

        elif entry.data_type == Data_Type.HEX:
            self._log.error(
                f"WRITE_ERROR: HEX register writes not currently supported for "
                f"'{entry.variable_name}'. Unsafe to write."
            )
            return

        #  A TypeError would propagate uncaught through write_data and _transport_lock would be released correctly
        #  since it's a 'with' block, but the exception would propagate to MPG's transport read loop, so just log as error.
        else:
            self._log.error(
                f"WRITE_ERROR: Unrecognized data type '{entry.data_type.name}' for "
                f"'{entry.variable_name}'. Unsafe to write."
            )
            return

        if register_values is None:
            raise ValueError("Invalid value - None")

        bit_index_dbg: int | Literal['n/a'] = entry.register_bit if entry.register_bit > 0 else "n/a"
        self._log.debug(
            "WRITE_DEBUG transport=%s var=%s reg=%s bit=%s old_raw=%s new_raw=%s requested=%s",
            self.transport_name,
            entry.variable_name,
            entry.register,
            bit_index_dbg,
            raw_value,
            register_values,
            value,
        )
        self._log.info(
            f"WRITE: {current_value} => {value} "
            f"( {raw_registers} => {register_values} ) to Register {entry.register}"
        )

        # Coil registers use a dedicated single-bit write function.
        # Holding/input registers use the standard word-oriented write path.
        if registry_type == Registry_Type.COIL:
            coil_value: bool = bool(register_values[0]) if register_values else False
            self.write_coil(entry.register, coil_value)
        elif len(register_values) == 1:
            self.write_register(entry.register, register_values[0])
        else:
            self.write_registers(entry.register, register_values)


    def read_variable(self, variable_name : str, registry_type : Registry_Type, entry : registry_map_entry | None = None) -> int | float | str | None:
        if variable_name:
            variable_name = variable_name.strip().lower().replace(" ", "_")

        registry_map: list[registry_map_entry] = self._protocol.get_registry_map(registry_type)

        if entry is None:
            for e in registry_map:
                if e.variable_name == variable_name:
                    entry = e
                    break

        if entry:
            start : int = 0
            end : int = 0
            if not entry.concatenate:
                start = entry.register
                end = entry.register
            else:
                start = entry.register
                end = max(entry.concatenate_registers)

            registers: Dict[int, int] = self.read_modbus_registers(start=start, end=end, registry_type=registry_type)
            results: Dict[str, int | float | str] = self._protocol.process_registery(registers, registry_map)
            return results.get(entry.variable_name)  # safer than direct dict access.

    def read_modbus_registers(self, ranges: list[tuple[int, int]] | None = None, start : int = 0, end : int | None = None,
                              batch_size : int | None = None, registry_type : Registry_Type = Registry_Type.INPUT ) -> dict[int, int]:

        # Get batch_size from protocol settings if not provided
        if batch_size is None:
            if hasattr(self, 'protocolSettings') and self._protocol:
                try:
                   batch_size = int(self._protocol.settings.get("batch_size", 45))
                except (ValueError, TypeError):
                    batch_size = 45
            else:
                batch_size = 45

        if not ranges: #ranges is empty, use min max
            if start == 0 and end is None:
                return {} #empty

            if end is not None:
                end = end + 1
            ranges = []
            start = start - batch_size
            if end is not None:
                while( start := start + batch_size ) < end:
                    count: int = batch_size
                    if start + batch_size > end:
                        count = end - start + 1
                    ranges.append((start, count)) ##APPEND TUPLE

        registry: dict[int, int] = {}
        retries = 7
        retry = 0
        total_retries = 0

        index = -1
        counted_ranges: set[tuple[int, int]] = set()
        while (index := index + 1) < len(ranges) :
            register_range: tuple[int, int] = ranges[index]
            if register_range not in counted_ranges:
                counted_ranges.add(register_range)
                self._cycle_expect_unit()

            # Check if this register range is currently disabled
            if self._is_register_range_disabled(register_range, registry_type):
                remaining_hours: float = self._get_or_create_failure_tracker(register_range, registry_type).get_remaining_disable_time() / 3600
                self._log.info(f"Skipping disabled register range {registry_type.name} {register_range[0]}-{register_range[0]+register_range[1]-1} (disabled for {remaining_hours:.1f}h)")
                self._cycle_mark_incomplete()
                continue

            self._log.info("get registers ("+str(index)+"): " +str(registry_type)+ " - " + str(register_range[0]) + " to " + str(register_range[0]+register_range[1]-1) + " ("+str(register_range[1])+")")
            time.sleep(self.modbus_delay) #sleep for 1ms to give bus a rest #manual recommends 1s between commands

            isError = False
            register = None  # Initialize register variable

            # Acquire the shared bus lock for this single block attempt.
            # Released immediately after the read returns (or times out)
            # so peer transports on the same physical bus can proceed
            # between our retry attempts rather than waiting for the
            # full retry chain to exhaust.
            # bus_lock is just a local variable inside this loop, so if the transport doesn't have a bus lock,
            # it will be None and skipped without error.
            bus_lock: Lock | None = self._bus_lock
            if bus_lock is not None:
                bus_lock.acquire()
            try:
                register = self.read_registers(register_range[0], register_range[1], registry_type=registry_type)

                """  TODO to handle dynamic registers
                        # Pass 1 — read base registers to get device configuration
                        base_info = self._protocol.process_registery(base_registry, base_map)

                        # Pass 2 — resolve dynamic registers using values from pass 1
                        for entry in dynamic_entries:
                            resolved_addresses = self._protocol.evaluate_expressions(
                                entry.register_expression,
                                base_info   # live values used as variables
                            )
                        # read the resolved addresses...
                        # """

            except ModbusIOException as e:
                self._log.error(f"ModbusIOException for {self.transport_name}: " + str(e))
                # In pymodbus 3.7+, ModbusIOException doesn't have error_code attribute
                # Treat all ModbusIOException as retry-able errors
                isError = True

            finally:
                if bus_lock is not None:
                    bus_lock.release()

            if register is None or isinstance(register, bytes) or (hasattr(register, 'isError') and register.isError()) or isError: #sometimes weird errors are handled incorrectly and response is an ascii error string
                if register is None:
                    self._log.error("No response received from modbus device")
                elif isinstance(register, bytes):
                    self._log.error(register.decode("utf-8"))
                else:
                    # Enhanced error logging with Modbus exception interpretation
                    error_msg = str(register)

                    # Check if this is an ExceptionResponse and extract the exception code
                    if hasattr(register, 'function_code') and hasattr(register, 'exception_code'):
                        exception_code = register.function_code | 0x80  # Convert to exception response code
                        interpreted_error: str = interpret_modbus_exception_code(exception_code)
                        self._log.debug(f"{error_msg} - {interpreted_error}")
                    else:
                        self._log.error(error_msg)

                # Record the failure for this register range
                should_disable: bool = self._record_register_read_failure(register_range, registry_type)
                self._log.warning("Disabled is ("+str(should_disable)+" range("+str(index)+")")

                self.modbus_delay += self.modbus_delay_increament #increase delay, error is likely due to modbus being busy

                if self.modbus_delay > 60: #max delay. 60 seconds between requests should be way over kill if it happens
                    self.modbus_delay = 60

                if retry > retries: #instead of none, attempt to continue to read. but with no retries.
                    self._cycle_mark_incomplete()
                    continue
                else:
                    #undo step in loop and retry read
                    retry: int = retry + 1
                    total_retries: int = total_retries + 1
                    self._log.warning("Retry("+str(retry)+" - ("+str(total_retries)+")) range("+str(index)+")")
                    index: int = index - 1
                    continue
            elif self.modbus_delay > self.modbus_delay_setting: #no error, decrease delay
                self.modbus_delay -= self.modbus_delay_increament
                if self.modbus_delay < self.modbus_delay_setting:
                    self.modbus_delay = self.modbus_delay_setting

            # Record successful read for this register range
            self._record_register_read_success(register_range, registry_type)
            self._cycle_mark_unit_complete()

            retry -= 1
            if retry < 0:
                retry = 0

            # Extract values — handles both .registers (INPUT/HOLDING) and .bits (COIL/DISCRETE)
            extracted: Dict[int, int] | None = self._extract_response_values(register, registry_type, register_range)
            if extracted is not None:
                registry.update(extracted)

        return registry

    def read_modbus_registers_iter(
        self,
        ranges: list[tuple[int, int]],
        registry_type: Registry_Type = Registry_Type.INPUT,
    ) -> Iterator[tuple[tuple[int, int], dict[int, int] | None]]:

        for register_range in ranges:
            if self._is_register_range_disabled(register_range, registry_type):
                yield register_range, {}
                continue
            self._log.debug(
                f"[{self.transport_name}] requesting {registry_type.name} "
                f"{register_range[0]}-{register_range[0]+register_range[1]-1}"
            )

            time.sleep(self.modbus_delay)
            isError = False
            register = None

            bus_lock: Lock | None = self._bus_lock
            if bus_lock is not None:
                bus_lock.acquire()
            try:
                register = self.read_registers(register_range[0], register_range[1], registry_type=registry_type)
            except Exception as e:
                self._log.error(f"Unexpected error during read: {e}")
                isError = True
                register = None
            finally:
                # Release before yield so the bus is free while the coordinator
                # processes this result and signals other transports.

                if bus_lock is not None:
                    bus_lock.release()

            # Lock is released here — yield now so other threads can acquire it
            if register is None or (hasattr(register, 'isError') and register.isError()) or isError:
                self._record_register_read_failure(register_range, registry_type)
                yield register_range, None
            else:
                self._record_register_read_success(register_range, registry_type)
                # Extract values — handles .registers (INPUT/HOLDING) and .bits (COIL/DISCRETE)
                result: Dict[int, int] | None = self._extract_response_values(register, registry_type, register_range)
                yield register_range, result if result is not None else {}

    def read_data_iter(self) -> Iterator[bool]:
        """
        Generator that reads all register ranges one block at a time,
        yielding True after each block (success or failure) so the
        caller can interleave reads across transports.
        Accumulates results internally; call get_partial_data() to
        retrieve whatever has been collected so far.

        When called as a fallback from read_group_data_iter the
        _cycle_active flag will already be True, so _start_cycle_tracking
        and _finish_cycle_tracking are skipped — the group iter owns
        the cycle lifecycle in that case.
        """
        _owner: bool = not getattr(self, '_cycle_active', False)
        if _owner:
            self._start_cycle_tracking()

        for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING,
                            Registry_Type.COIL, Registry_Type.DISCRETE):
            if not self._should_send_registry_type(registry_type):
                continue

            yield from self._read_registry_type_iter(
                registry_type,
                self._protocol.registry_map[registry_type],
                self._protocol.registry_map_size[registry_type],
            )

            # Process whatever was collected for this registry type
            new_info: dict[str, int | float | str] = self._protocol.process_registery(
                self._partial_registry,
                self._protocol.get_registry_map(registry_type)
            )
            self._partial_info.update(new_info)
            self._partial_registry.clear()

        if _owner:
            self._finish_cycle_tracking(self._partial_info)

    def _read_registry_type_iter(
        self,
        registry_type: Registry_Type,
        union_entries: list[registry_map_entry],
        max_register: int,
    ) -> Iterator[bool]:
        """
        Reads one registry type block-by-block, yielding after each block.
        Accumulates raw register values into self._partial_registry.
        Does NOT call _start_cycle_tracking or _finish_cycle_tracking —
        those are the caller's responsibility.
        """
        ranges: list[tuple[int, int]] = self._protocol.calculate_registry_ranges(
            union_entries, max_register, timestamp=self.last_read_time, init=True
        )

        # Walk ranges explicitly so we control which range is attempted next.
        # A plain `for range, result in read_modbus_registers_iter(ranges)` is
        # a forward-only iterator — once a range is yielded it is gone.
        # We drive the index ourselves and re-issue a
        # single-element call to read_modbus_registers_iter for each retry so
        # the block goes back on the wire before we advance to the next range.
        idx: int = 0
        retry_counts: dict[int, int] = {}
        counted_indices: set[int] = set()

        while idx < len(ranges):
            register_range: tuple[int, int] = ranges[idx]
            if idx not in counted_indices:
                counted_indices.add(idx)
                self._cycle_expect_unit()
            retry_count: int = retry_counts.get(idx, 0)

            # Single-range call so the iterator covers exactly this one block.
            _, result = next(iter(
                self.read_modbus_registers_iter([register_range], registry_type)
            ))

            if result == {}:
                self._cycle_mark_incomplete()
                idx += 1
                yield True
            elif result is None:
                retry_count += 1
                retry_counts[idx] = retry_count
                if retry_count < self.max_retries_per_block:
                    # Yield False to let the coordinator interleave a block
                    # from another transport before we retry this range.
                    # When the coordinator calls next() we come back here,
                    # idx is still pointing at the failed range, so the while
                    # loop head re-attempts it immediately.
                    yield False
                    # Do not advance idx — retry the same range next time in.
                else:
                    self._log.warning(f"Block {register_range} exceeded {self.max_retries_per_block} retries, skipping.")
                    self._cycle_mark_incomplete()
                    idx += 1        # give up on this range, move to next
                    yield True      # still signal coordinator we made progress
            else:
                self._cycle_mark_unit_complete()
                self._partial_registry.update(result)
                idx += 1            # success — advance to next range
                yield True

    def get_partial_data(self) -> dict[str, int | float | str]:
        return getattr(self, '_partial_info', {})

    @property
    def scrape_target(self) -> str:
        """
        Returns a string uniquely identifying the scrape target for this transport,
        used for logging and scraper tracking.
        """
        address: str = getattr(self, 'address', '')
        port: str | int = getattr(self, 'port', '')
        host: str = getattr(self, 'host', '')
        protocol: str = getattr(self, 'protocol_version', '')
        slave_id: str = getattr(self, '_slave_id', '1')
        base: str = f"{address}:{port}" if address else f"{host}:{port}"
        return f"{base}:{protocol}:{slave_id}"

    def read_registry(self, registry_type: Registry_Type = Registry_Type.INPUT) -> dict[str, int | float | str]:
        """
        Reads and processes a single registry type from the device.
        Returns processed register data keyed by variable name.
        Used internally for protocol validation and serial number reading.
        For full device data reads use read_data() instead.
        """
        registry_map: list[registry_map_entry] = self._protocol.get_registry_map(registry_type)
        if not registry_map:
            return {}

        # Read raw register values from device
        raw_registers: dict[int, int] = self.read_modbus_registers(
            ranges=self._protocol.get_registry_ranges(registry_type),
            registry_type=registry_type
        )

        # Process raw int values into named, typed register readings
        register_readings: dict[str, int | float | str] = self._protocol.process_registery(
            raw_registers,
            registry_map
        )

        return register_readings
