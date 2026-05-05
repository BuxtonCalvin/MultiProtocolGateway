# Modbus base transport class with shared client management, register failure tracking, and protocol analysis support
import inspect
import re
import threading
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, Iterator, Optional

from pymodbus.client.base import ModbusBaseClient
from pymodbus.constants import ExcCodes
from pymodbus.exceptions import ModbusIOException

from defs.common import TransportSettings, strtobool

from ..protocol_settings import (
    Data_Type,
    Registry_Type,
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

def interpret_modbus_exception_code(code):
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
        function_name = MODBUS_FUNCTION_CODES.get(function_code, f"Unknown Function ({function_code})")
        exception_name = MODBUS_EXCEPTION_CODES.get(exception_code, f"Unknown Exception ({exception_code})")
        description = MODBUS_EXCEPTION_DESCRIPTIONS.get(exception_code, "Unknown exception code")
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
            current_time = time.time()
            self.failure_count += 1
            self.last_failure_time = current_time

            # If we've had enough failures, disable for specified duration
            if self.failure_count >= max_failures:
                self.disabled_until = current_time + (disable_duration_hours * 3600)
                return True  # Indicates this range should be disabled
            return False

    def record_success(self):
        """Record a successful read attempt"""
        with self._lock:
            current_time = time.time()
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
            remaining = self.disabled_until - time.time()
            return max(0, remaining)
class modbus_base(transport_base):

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
        '''time in between requests, unmodified'''

        self.modbus_delay : float = 0.85
        '''time in between requests'''

        # per transport tuning — batteries with known intermittent blocks can have higher retry counts;
        # a totally dead device will exhaust retries quickly and yield control
        self.max_retries_per_block: int = int(settings.get("max_retries_per_block", fallback=3))

        self.first_connect : bool = True
        self._needs_reconnection : bool = False

        self.send_holding_register : bool = True
        self.send_input_register : bool = True

        # Register failure tracking - make instance-specific
        self.enable_register_failure_tracking: bool = True
        self.max_failures_before_disable: int = 5
        self.disable_duration_hours: int = 12

        # Initialize transport-specific lock
        self._transport_lock = threading.Lock()

        # Initialize instance-specific register failure tracking
        self.register_failure_trackers: dict[str, RegisterFailureTracker] = {}
        self._failure_tracking_lock = threading.Lock()

        # Register failure tracking settings
        self.enable_register_failure_tracking = settings.getboolean("enable_register_failure_tracking", fallback=self.enable_register_failure_tracking)
        self.max_failures_before_disable = settings.getint("max_failures_before_disable", fallback=self.max_failures_before_disable)
        self.disable_duration_hours = settings.getint("disable_duration_hours", fallback=self.disable_duration_hours)

        # get defaults from protocol settings
        if "send_input_register" in self._protocol.settings:
            self.send_input_register = strtobool(self._protocol.settings["send_input_register"])

        if "send_holding_register" in self._protocol.settings:
            self.send_holding_register = strtobool(self._protocol.settings["send_holding_register"])

        if "batch_delay" in self._protocol.settings:
            self.modbus_delay = float(self._protocol.settings["batch_delay"])

        # allow enable/disable of which registers to send
        self.send_holding_register = settings.getboolean("send_holding_register", fallback=self.send_holding_register)
        self.send_input_register = settings.getboolean("send_input_register", fallback=self.send_input_register)
        self.modbus_delay = settings.getfloat(["batch_delay", "modbus_delay"], fallback=self.modbus_delay)
        self.modbus_delay_setting = self.modbus_delay

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

    def _get_correct_device_arg(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # 1. Identify which keyword the current Pymodbus version expects
        # Check the signature of a standard client method
        if self.client is None:
            msg: str = f"Transport '{self.transport_name}' has no client assigned — subclass __init__ must assign self.client before calling " \
            "_get_correct_device_arg"
            raise RuntimeError(msg)

        sig: inspect.Signature = inspect.signature(self.client.read_input_registers)

        # Priority order for Pymodbus versions:
        # v3.10+ uses 'device_id', v3.0-3.9 uses 'slave', legacy uses 'unit'
        target_arg: str = next((arg for arg in ['device_id', 'slave', 'unit'] if arg in sig.parameters), 'slave')

        # 2. Extract the unit/slave ID from kwargs (default to 1)
        val: int = kwargs.pop("unit", kwargs.pop("slave", kwargs.pop("device_id", 1)))

        # 3. Re-insert it with the correct name
        kwargs[target_arg] = val
        return kwargs

    def _entry_byte_order(self, entry: registry_map_entry) -> str:
        return entry.data_byteorder or self._protocol.byteorder

    def _register_words_to_bytes(
        self,
        register_values: list[int],
        byte_order: str,
    ) -> bytes:
        words = [value & 0xFFFF for value in register_values]
        if byte_order == "little":
            words.reverse()
        return b"".join(word.to_bytes(2, byteorder="big", signed=False) for word in words)

    def _bytes_to_register_words(self, data: bytes, byte_order: str) -> list[int]:
        if len(data) % 2 != 0:
            msg = f"Expected even byte count for register write, got {len(data)}"
            raise ValueError(msg)
        words = [int.from_bytes(data[i:i + 2], byteorder="big", signed=False) for i in range(0, len(data), 2)]
        if byte_order == "little":
            words.reverse()
        return words

    def _entry_word_count(self, entry: registry_map_entry) -> int:
        if entry.data_type in (Data_Type.UINT, Data_Type.INT, Data_Type._32BIT_FLAGS):
            return 2
        return 1

    def write_registers(self, start_register: int, values: list[int], **kwargs: Any) -> None:
        if not self.write_enabled:
            return
        if self.client is None:
            self._log.error("write_registers called before client was initialized")
            return
        kwargs = self._get_correct_device_arg(kwargs)
        port_lock = self._get_port_lock()
        with port_lock:
            self.client.write_registers(start_register, values, **kwargs)

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
        port_id = self._get_port_identifier()

        with self._clients_lock:
            if port_id not in self._client_locks:
                self._client_locks[port_id] = threading.Lock()

        return self._client_locks[port_id]

    def _get_register_range_key(self, register_range: tuple[int, int], registry_type: Registry_Type) -> str:
        """Generate a unique key for a register range"""
        return f"{registry_type.name}_{register_range[0]}_{register_range[1]}"

    def _get_or_create_failure_tracker(self, register_range: tuple[int, int], registry_type: Registry_Type) -> RegisterFailureTracker:
        """Get or create a failure tracker for a register range"""
        key = self._get_register_range_key(register_range, registry_type)

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

        tracker = self._get_or_create_failure_tracker(register_range, registry_type)
        # Only log if the last failure was after the last success (i.e., this is the first success after a failure)
        should_log_recovery = tracker.last_failure_time > tracker.last_success_time
        tracker.record_success()

        if should_log_recovery:
            self._log.info(f"Register range {registry_type.name} {register_range[0]}-{register_range[1]} is working again after previous failures")

    def _record_register_read_failure(self, register_range: tuple[int, int], registry_type: Registry_Type) -> bool:
        """Record a failed register read, returns True if range should be disabled"""
        if not self.enable_register_failure_tracking:
            return False

        tracker = self._get_or_create_failure_tracker(register_range, registry_type)
        should_disable: bool = tracker.record_failure(self.max_failures_before_disable, self.disable_duration_hours)

        if should_disable:
            self._log.warning(f"Register range {registry_type.name} {register_range[0]}-{register_range[1]} disabled for {self.disable_duration_hours} hours after {tracker.failure_count} failures")
        else:
            self._log.warning(f"Register range {registry_type.name} {register_range[0]}-{register_range[1]} failed ({tracker.failure_count}/{self.max_failures_before_disable} attempts)")

        return should_disable

    def _is_register_range_disabled(self, register_range: tuple[int, int], registry_type: Registry_Type) -> bool:
        """Check if a register range is currently disabled"""
        if not self.enable_register_failure_tracking:
            return False

        tracker = self._get_or_create_failure_tracker(register_range, registry_type)
        return tracker.is_disabled()

    def _get_disabled_ranges_info(self) -> list[str]:
        """Get information about currently disabled register ranges"""
        disabled_info = []

        with self._failure_tracking_lock:
            for tracker in self.register_failure_trackers.values():
                if tracker.is_disabled():
                    remaining_hours = tracker.get_remaining_disable_time() / 3600
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
                - `registry_type`: INPUT or HOLDING
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
        tracker = self._get_or_create_failure_tracker(register_range, registry_type)
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
            self._needs_reconnection = False

            # Reset protocol settings timestamps to ensure fresh reading
            for registry_type in [Registry_Type.INPUT, Registry_Type.HOLDING]:
                if registry_type in self._protocol.registry_map:
                    for entry in self._protocol.registry_map[registry_type]:
                        entry.next_read_timestamp = 0.0

    def cleanup(self) -> None:
        """Clean up transport resources and close connections"""
        with self._transport_lock:
            self._log.info(f"Cleaning up transport {self.transport_name}")

            # Reset register timestamps to prevent sharing issues between transports
            self._protocol.reset_register_timestamps()

            # Close the modbus client connection
            port_identifier = self._get_port_identifier()
            if port_identifier in self.clients:
                try:
                    client: ModbusBaseClient  = self.clients[port_identifier]
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

            # Mark as disconnected and reset first_connect for reconnection
            self.connected = False
            self.first_connect = False  # Reset so reconnection works properly
            self._needs_reconnection = True  # Flag that this transport needs reconnection
            self._log.info(f"Transport {self.transport_name} cleanup completed")

    def read_serial_number(self) -> str:
        """
        Attempts to read the device serial number from registers.
        Tries 'Serial_Number' variable first in INPUT then HOLDING registers,
        then falls back to reading individual 'Serial No N' holding registers.
        Returns empty string if serial number cannot be determined.
        """

        # 1. Try single-register serial number variable — INPUT then HOLDING
        for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING):
            self._log.info(
                f"Looking for serial_number variable in "
                f"{registry_type.name} registers..."
            )
            result = self.read_variable("Serial_Number", registry_type)
            serial_number: str = str(result) if result is not None else ""
            self._log.info(f"Read SN from {registry_type.name}: {serial_number}")
            if serial_number and serial_number != "None":
                return serial_number

        # 2. Fall back to concatenating Serial No 1-5 holding registers
        serial_number = ""
        sn2: str = ""
        sn3: str = ""
        fields: list[str] = [
            "Serial No 1", "Serial No 2", "Serial No 3",
            "Serial No 4", "Serial No 5"
        ]

        for reg_field in fields:
            self._log.info(f"Reading {reg_field}")
            registry_entry: registry_map_entry | None = self._protocol.get_holding_registry_entry(reg_field)

            if registry_entry is None:
                self._log.debug(f"{reg_field} not found in protocol registry — skipping")
                continue

            self._log.info(f"Reading {reg_field} (register {registry_entry.register})")

            data: dict[int, int] = self.read_modbus_registers(
                start=registry_entry.register,
                end=registry_entry.register,
                registry_type=Registry_Type.HOLDING
            )

            if not data or registry_entry.register not in data:
                self._log.critical(
                    f"Failed to get serial number register ({reg_field}) — "
                    f"no data returned"
                )
                return ""   # critical failure — return empty, let caller handle

            register_value: int = data[registry_entry.register]
            serial_number = serial_number + str(register_value)

            data_bytes: bytes = register_value.to_bytes(
                (register_value.bit_length() + 7) // 8,
                byteorder="big"
            )
            try:
                decoded: str = data_bytes.decode("utf-8")
                sn2 = sn2 + decoded
                sn3 = decoded + sn3
            except UnicodeDecodeError as e:
                self._log.warning(
                    f"Could not decode serial number bytes for {reg_field}: {e}"
                )

            time.sleep(self.modbus_delay * 2)

        self._log.debug(f"Serial number sn2: {sn2}")
        self._log.debug(f"Serial number sn3: {sn3}")

        if not re.search(r"[^a-zA-Z0-9_]", sn2):
            serial_number = sn2

        return serial_number

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

            registry_map: list[registry_map_entry] = self._protocol.get_registry_map(Registry_Type.HOLDING)

            for variable_name, value in data.items():
                entry: registry_map_entry | None = None
                for e in registry_map:
                    if e.variable_name == variable_name:
                        entry = e
                        break

                if entry is not None:
                    # Pass value through unchanged — write_variable handles
                    # int, float, and str (code values) natively
                    self.write_variable(entry, value, Registry_Type.HOLDING)

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
            #modbus - only read input/holding registries
            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING):

                #enable / disable input/holding register
                if registry_type == Registry_Type.INPUT and not self.send_input_register:
                    continue

                if registry_type == Registry_Type.HOLDING and not self.send_holding_register:
                    continue

                #calculate ranges dynamically -- for variable read timing
                ranges = self._protocol.calculate_registry_ranges(self._protocol.registry_map[registry_type],
                                                                         self._protocol.registry_map_size[registry_type],
                                                                         timestamp=self.last_read_time)

                self._log.info(f"Reading {registry_type.name} registers for {self.transport_name}: {len(ranges)} ranges")
                if len(ranges) == 0:
                    self._log.warning(f"No register ranges calculated for {self.transport_name} {registry_type.name}")
                    # Debug: show protocol settings info
                    total_entries = len(self._protocol.registry_map.get(registry_type, []))
                    self._log.info(f"Protocol settings for {self.transport_name}: {total_entries} total entries for {registry_type.name}")

                    # Count entries that would be read
                    readable_entries = 0
                    for entry in self._protocol.registry_map.get(registry_type, []):
                        if entry.write_mode != WriteMode.READDISABLED and entry.write_mode != WriteMode.WRITEONLY:
                            readable_entries += 1
                    self._log.info(f"Readable entries for {self.transport_name} {registry_type.name}: {readable_entries}")

                registry = self.read_modbus_registers(ranges=ranges, registry_type=registry_type)

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
            if self.enable_register_failure_tracking and hasattr(self, '_last_disabled_status_log') and time.time() - self._last_disabled_status_log > 600:
                disabled_ranges = self._get_disabled_ranges_info()
                if disabled_ranges:
                    self._log.info(f"Currently disabled register ranges: {len(disabled_ranges)}")
                    for range_info in disabled_ranges:
                        self._log.info(f"  - {range_info}")
                self._last_disabled_status_log = time.time()
            elif not hasattr(self, '_last_disabled_status_log'):
                self._last_disabled_status_log = time.time()

            self._finish_cycle_tracking(info)
            return info

    def read_group_data(self, members: list[transport_base]) -> dict[str, int | float | str]:
        """
        Read one consolidated payload for all transports sharing this physical
        Modbus endpoint. The gateway stays transport-agnostic; Modbus-specific
        batching lives here.
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

            for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING):
                if registry_type == Registry_Type.INPUT and not self.send_input_register:
                    continue

                if registry_type == Registry_Type.HOLDING and not self.send_holding_register:
                    continue

                union_entries: list[registry_map_entry] = []
                seen: set[tuple[int, str]] = set()
                max_register: int = 0

                for member in members:
                    member_protocol = getattr(member, "protocolSettings", None)
                    if member_protocol is None:
                        continue

                    member_entries = member_protocol.registry_map.get(registry_type, [])
                    for entry in member_entries:
                        key = (entry.register, entry.variable_name)
                        if key in seen:
                            continue
                        seen.add(key)
                        union_entries.append(entry)
                        if entry.register > max_register:
                            max_register = entry.register

                if not union_entries:
                    continue

                ranges = self._protocol.calculate_registry_ranges(
                    union_entries,
                    max_register,
                    timestamp=self.last_read_time,
                    init=True,
                )

                self._log.info(
                    f"Reading grouped {registry_type.name} registers for {self.transport_name}: "
                    f"{len(ranges)} ranges across {len(union_entries)} entries"
                )

                registry = self.read_modbus_registers(ranges=ranges, registry_type=registry_type)

                if registry:
                    info.update(self._protocol.process_registery(registry, union_entries))
                else:
                    self._log.warning(
                        f"No grouped registry data returned for {self.transport_name} {registry_type.name}"
                    )

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

    def validate_protocol(self, registry_type: Registry_Type = Registry_Type.HOLDING) -> float:
        """
        Validates the protocol by reading registers and scoring results
        against expected value ranges defined in the protocol CSV.

        Args:
            registry_type: Which register bank to validate.
                        Defaults to HOLDING since write validation
                        requires confirmed holding register access.
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
        batch_size: int = 50,
        delay: float = 0.05,
        include_holding: bool = True,
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

        self._log.info(
            "[%s] Starting analysis scan: range=%d-%d batch=%d — "
            "normal scraper reads suspended for duration",
            self.transport_name,
            start,
            end,
            batch_size,
        )

        def _normalize_register_values(values, addr: int, reg_count: int) -> list[Any]:
            if values is None:
                msg: str = f"read_registers returned None for range {addr}-{addr + reg_count - 1}"
                raise RuntimeError(msg)

            # read_registers returns a pymodbus response object whose integer
            # register values live in the .registers attribute.  Extract that
            # list before any further processing.  If the transport subclass
            # already returns a plain list (e.g. a mock or future subclass),
            # pass it through unchanged.
            if hasattr(values, "registers"):
                values = values.registers
            elif not isinstance(values, list):
                values = [values]

            if len(values) != reg_count:
                self._log.debug(
                    "[%s] Partial read: expected=%d got=%d at %d",
                    self.transport_name,
                    reg_count,
                    len(values),
                    addr,
                )

            return values

        def scan_range(registry_type: Registry_Type, result_dict: dict[int, int]) -> None:
            total_reads = 0
            failures = 0
            phase = registry_type.name.lower()
            total_batches = max(1, (end - start) // batch_size + 1)
            batches_done = 0

            for addr in range(start, end + 1, batch_size):
                range_count: int = min(batch_size, end - addr + 1)

                # NOTE: deliberately bypass the failure tracker here.
                # capture_analysis_scan is an intentional dense sweep of the
                # full address space — skipping disabled ranges would produce
                # empty results because the normal scraper marks most
                # out-of-protocol addresses as disabled after the first failed
                # read.  We also do not write back to the failure tracker so
                # this scan never pollutes the scraper's disabled-range state.

                try:
                    values = self.read_registers(
                        addr,
                        range_count,
                        registry_type=registry_type,
                    )
                    values = _normalize_register_values(values, addr, range_count)

                    for i, val in enumerate(values):
                        if val is not None:
                            result_dict[addr + i] = val

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

        return {"input": input_result, "holding": holding_result}

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

        s = (values_str or "").strip()
        if not s:
            return None

        # Continuous numeric range: handles "-10-10", "0-65535", "0.5-1.5".
        # Pattern: optional leading minus, digits/dot, dash separator,
        # optional minus, digits/dot.
        range_match = re.fullmatch(
            r"(-?\d+(?:\.\d+)?)\s*[-\u2013]\s*(-?\d+(?:\.\d+)?)", s
        )
        if range_match:
            lo = float(range_match.group(1))
            hi = float(range_match.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return (lo, hi)

        # Comma-separated discrete set: "0,1,2"
        parts = [p.strip() for p in s.split(",")]
        if len(parts) > 1:
            try:
                floats = [float(p) for p in parts if p]
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
    def _value_in_range(
        raw_value: int | float,
        constraint: tuple[float, float] | list[float] | None,
    ) -> bool:
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
        scan = self.capture_analysis_scan(progress_cb=progress_cb, batch_size=batch_size)
        raw_input = scan["input"]
        raw_holding = scan["holding"]

        protocols: dict[str, protocol_settings] = {}
        for name in protocol_names:
            try:
                protocols[name] = protocol_settings(name)
            except Exception as exc:
                self._log.warning("Failed loading protocol %s: %s", name, exc)

        results: dict[str, Any] = {}
        for name, proto in protocols.items():
            # Explicitly load both registry types before scoring.
            # get_registry_map() does a bare dict lookup with no load
            # fallback, so we must call load_registry_map() first.
            for reg_type in [Registry_Type.INPUT, Registry_Type.HOLDING]:
                if reg_type not in proto.registry_map:
                    try:
                        proto.load_registry_map(reg_type)
                    except Exception as exc:
                        self._log.warning(
                            "Could not load %s registry for %s: %s",
                            reg_type.name, name, exc
                        )

            protocol_result: dict[str, Any] = {
                "protocol_name": name,
                "is_current": name == (current_protocol or ""),
                "scores": {},
                "actions": {},
            }

            for reg_type, reg_key, raw_map in (
                (Registry_Type.INPUT, "input", raw_input),
                (Registry_Type.HOLDING, "holding", raw_holding),
            ):
                # Safe access — fall back to empty list if load failed
                entries = proto.registry_map.get(reg_type, [])
                decoded_values = proto.process_registery(raw_map, entries) if (raw_map and entries) else {}

                known_registers: set[int] = {
                    entry.register for entry in entries
                    if entry.write_mode not in (WriteMode.WRITEONLY, WriteMode.READDISABLED)
                }
                readable_entries = [
                    entry for entry in entries
                    if entry.write_mode not in (WriteMode.WRITEONLY, WriteMode.READDISABLED)
                ]

                # --- scoring ---
                # Use float accumulator so partial credit (0.5) is preserved.
                matches: float = 0.0
                for entry in readable_entries:
                    value = decoded_values.get(entry.variable_name)
                    if value is None:
                        # Register absent from scan — contributes 0
                        continue

                    # Skip continuation registers of concatenated entries;
                    # only the first register drives the decoded value.
                    if entry.concatenate and entry.register != entry.concatenate_registers[0]:
                        continue

                    base_valid = bool(proto.validate_registry_entry(entry, value))

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
                    constraint = self._parse_values_range(values_raw) if values_raw is not None else None
                    raw_int = raw_map.get(entry.register)
                    in_range = (
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

                total = len(readable_entries)
                missing_in_scan = sorted(reg for reg in known_registers if reg not in raw_map)
                unknown_in_scan = sorted(reg for reg in raw_map.keys() if reg not in known_registers)
                accuracy = round((matches / total) * 100, 2) if total else 0.0

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

                # --- addable suggestions ---
                # Include out_of_range so the UI can warn when a newly
                # discovered register's raw value is already outside the
                # default documented range.
                addable: list[dict[str, Any]] = []
                default_range_str = "0-65535"
                default_constraint = self._parse_values_range(default_range_str)
                for reg in unknown_in_scan:
                    raw_val = raw_map.get(reg)
                    out_of_range = (
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
        """ writes a value to a ModBus register; todo: registry_type to handle other write functions"""

        if isinstance(value, str):
            value = value.strip().lower()

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
            raw_word = registry.get(entry.register + offset)
            if raw_word is None:
                self._log.error(
                    f"WRITE_ERROR: Register {entry.register + offset} not found in registry "
                    f"for '{entry.variable_name}'. Unsafe to write."
                )
                return
            raw_registers.append(raw_word)

        byte_order = self._entry_byte_order(entry)
        raw_bytes = self._register_words_to_bytes(raw_registers, byte_order)
        raw_value = int.from_bytes(raw_bytes, byteorder="big", signed=False)
        total_bits = len(raw_bytes) * 8

        if entry.variable_name not in info:
            self._log.error(
                f"WRITE_ERROR: Could not decode current value for '{entry.variable_name}'. Unsafe to write."
            )
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
                byte_order,
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
                byte_order,
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
            updated_value = (raw_value & clear_mask) | ((flag_int << bit_index) & bit_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                byte_order,
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
            updated_value = (raw_value & clear_mask) | ((new_val << bit_index) & bit_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                byte_order,
            )

            check_value: int = (updated_value >> bit_index) & base_mask
            if check_value != new_val:
                msg: str = (f"Bitwise mismatch: Expected {new_val}, got {check_value}")
                raise ValueError(msg)

        elif 300 < entry.data_type.value < 400:  # signed bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            min_val = -(1 << (bit_size - 1))
            max_val = (1 << (bit_size - 1)) - 1
            signed_val = int(value)
            if signed_val < min_val or signed_val > max_val:
                self._log.error(
                    f"WRITE_ERROR: Value '{signed_val}' out of signed {bit_size}-bit range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            encoded = signed_val & ((1 << bit_size) - 1)
            bit_mask = ((1 << bit_size) - 1) << bit_index
            clear_mask = ~bit_mask & ((1 << total_bits) - 1)
            updated_value = (raw_value & clear_mask) | ((encoded << bit_index) & bit_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                byte_order,
            )

        elif entry.data_type.value > 400:  # signed magnitude bit types
            bit_size = Data_Type.getSize(entry.data_type)
            bit_index = entry.register_bit if entry.register_bit >= 0 else 0
            max_magnitude = (1 << (bit_size - 1)) - 1
            signed_val = int(value)
            if abs(signed_val) > max_magnitude:
                self._log.error(
                    f"WRITE_ERROR: Value '{signed_val}' out of signed-magnitude {bit_size}-bit range for "
                    f"'{entry.variable_name}'. Unsafe to write."
                )
                return
            magnitude = abs(signed_val)
            encoded = magnitude << (bit_index + 1)
            if signed_val < 0:
                encoded |= (1 << bit_index)
            field_mask = ((1 << bit_size) - 1) << bit_index
            clear_mask = ~field_mask & ((1 << total_bits) - 1)
            updated_value = (raw_value & clear_mask) | (encoded & field_mask)
            register_values = self._bytes_to_register_words(
                updated_value.to_bytes(len(raw_bytes), byteorder="big", signed=False),
                byte_order,
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

        bit_index_dbg = entry.register_bit if entry.register_bit > 0 else "n/a"
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
        if len(register_values) == 1:
            self.write_register(entry.register, register_values[0])
        else:
            self.write_registers(entry.register, register_values)
        #entry.next_read_timestamp = 0 #ensure is read next interval


    def read_variable(self, variable_name : str, registry_type : Registry_Type, entry : registry_map_entry | None = None) -> int | float | str | None:
        # clean for convenience
        if variable_name:
            variable_name = variable_name.strip().lower().replace(" ", "_")

        registry_map = self._protocol.get_registry_map(registry_type)

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

            registers = self.read_modbus_registers(start=start, end=end, registry_type=registry_type)
            results = self._protocol.process_registery(registers, registry_map)
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
            register_range = ranges[index]
            if register_range not in counted_ranges:
                counted_ranges.add(register_range)
                self._cycle_expect_unit()

            # Check if this register range is currently disabled
            if self._is_register_range_disabled(register_range, registry_type):
                remaining_hours = self._get_or_create_failure_tracker(register_range, registry_type).get_remaining_disable_time() / 3600
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
                        interpreted_error = interpret_modbus_exception_code(exception_code)
                        self._log.debug(f"{error_msg} - {interpreted_error}")
                    else:
                        self._log.error(error_msg)

                # Record the failure for this register range
                should_disable = self._record_register_read_failure(register_range, registry_type)
                self._log.warning("Disabled is ("+str(should_disable)+" range("+str(index)+")")

                self.modbus_delay += self.modbus_delay_increament #increase delay, error is likely due to modbus being busy

                if self.modbus_delay > 60: #max delay. 60 seconds between requests should be way over kill if it happens
                    self.modbus_delay = 60

                if retry > retries: #instead of none, attempt to continue to read. but with no retries.
                    self._cycle_mark_incomplete()
                    continue
                else:
                    #undo step in loop and retry read
                    retry = retry + 1
                    total_retries = total_retries + 1
                    self._log.warning("Retry("+str(retry)+" - ("+str(total_retries)+")) range("+str(index)+")")
                    index = index - 1
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

            # Only process registers if we have a valid response
            if register is not None and hasattr(register, 'registers') and register.registers is not None:
                # combine registers into "registry"
                i = -1
                while(i := i + 1 ) < register_range[1]:
                    #print(str(i) + " => " + str(i+range[0]))
                    registry[i+register_range[0]] = register.registers[i]

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
                register = self.read_registers(
                    register_range[0], register_range[1], registry_type=registry_type
                )
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
                result = {
                    i + register_range[0]: register.registers[i]
                    for i in range(register_range[1])
                    if register.registers is not None
                }
                yield register_range, result

    def read_data_iter(self) -> Iterator[bool]:
        """
        Generator that reads all register ranges one block at a time,
        yielding True after each block (success or failure) so the
        caller can interleave reads across transports.
        Accumulates results internally; call get_partial_data() to
        retrieve whatever has been collected so far.
        """
        self._start_cycle_tracking()
        self._partial_registry: dict[int, int] = {}
        self._partial_info: dict[str, int | float | str] = {}

        for registry_type in (Registry_Type.INPUT, Registry_Type.HOLDING):
            if registry_type == Registry_Type.INPUT and not self.send_input_register:
                continue
            if registry_type == Registry_Type.HOLDING and not self.send_holding_register:
                continue

            # Guard: skip registry types that were never loaded (no CSV file).
            # Without this, registry_map[registry_type] raises KeyError which
            # propagates out of the generator frame and surfaces as an unhandled
            # exception at the next() call site in run_transport.
            if registry_type not in self._protocol.registry_map:
                continue

            ranges = self._protocol.calculate_registry_ranges(
                self._protocol.registry_map[registry_type],
                self._protocol.registry_map_size[registry_type],
                timestamp=self.last_read_time
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
                register_range = ranges[idx]
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
                        # Do NOT advance idx — retry the same range next time in.
                    else:
                        self._log.warning(
                            f"Block {register_range} exceeded {self.max_retries_per_block} "
                            f"retries, skipping."
                        )
                        self._cycle_mark_incomplete()
                        idx += 1        # give up on this range, move to next
                        yield True      # still signal coordinator we made progress
                else:
                    self._cycle_mark_unit_complete()
                    self._partial_registry.update(result)
                    idx += 1            # success — advance to next range
                    yield True

            # Process whatever was collected for this registry type
            new_info: dict[str, int | float | str] = self._protocol.process_registery(
                self._partial_registry,
                self._protocol.get_registry_map(registry_type)
            )
            self._partial_info.update(new_info)
            self._partial_registry.clear()

        self._finish_cycle_tracking(self._partial_info)

    def get_partial_data(self) -> dict[str, int | float | str]:
        return getattr(self, '_partial_info', {})

    @property
    def scrape_target(self) -> str:
        address: str = getattr(self, 'address', '')
        port: str = getattr(self, 'port', '')
        host: str = getattr(self, 'host', '')
        return f"{address}:{port}" if address else f"{host}:{port}"

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
