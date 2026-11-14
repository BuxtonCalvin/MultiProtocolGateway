# Description: Scraper for canbus data; because canbus is passive, we read the bus and store results in a cache, then process the cache to return values for the protocol. This allows us to read the bus as fast
# File: canbus.py
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

# Scraper for canbus data; because canbus is passive, we read the bus and store results in a cache,
# then process the cache to return values for the protocol. This allows us to read the bus as fast
# as possible, and process the data at a more reasonable rate for the protocol.
import platform
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from threading import Lock
from typing import ClassVar

import can

from defs.common import TransportSettings, strtoint_safe

from ..protocol_settings import (
    Data_Type,
    Registry_Type,
    WriteMode,
    protocol_settings,
    registry_map_entry,
)
from .transport_base import TransportWriteMode, transport_base


class canbus(transport_base):

    transport_type = "scraper"
    ''' canbus is mostly passive: frames are cached from the bus and decoded for the protocol.
    It can also transmit. write_data() sends a frame for each registry row marked W / WO, and
    send_frame() can transmit any frame (for example to trigger a response from a device).
    Transmitting is opt-in and requires write_enabled (see enable_write). '''

    interface: str = "socketcan"
    ''' bustype / interface for canbus device '''

    port: str = ""
    ''' 'can0' '''

    baudrate: int = 500000

    bus: can.BusABC | None = None
    ''' holds canbus interface'''

    # Do not instantiate can.AsyncBufferedReader() at class definition time.
    # Class-level mutable defaults are shared across all instances and can interact badly
    # with the asyncio event loop changes in 3.14 (get_event_loop() now raises if no loop
    # exists). The reader is assigned per-instance in __init__ instead.
    reader: can.AsyncBufferedReader | None = None

    thread: threading.Thread | None = None
    ''' main thread for async loop'''

    lock: Lock | None = None

    cache: OrderedDict[int, tuple[bytes, float]] | None = None
    ''' cache, key is id, value is tuple (data, timestamp)'''

    cacheTimeout: int = 120
    ''' seconds to keep message in cache '''

    emptyTime: float | None = None
    ''' the last time values were read for watchdog'''

    watchDogTime: float = 120
    ''' number of seconds of empty cache before restarting'''

    linux: bool = True

    serial_number_can_id: int | None = None
    ''' CAN ID known to carry the serial number; read from settings, or discovered by sniffing. '''

    _tx_lock: Lock | None = None
    ''' serializes transmit so frames shared by several variables are built one at a time '''

    _tx_frames: dict[int, bytes]
    ''' last payload transmitted per CAN ID; the base for the next write to the same frame '''

    # Data types write_data can encode into a frame: (size in bytes, signed)
    _TX_INT_TYPES: ClassVar[dict[Data_Type, tuple[int, bool]]] = {
        Data_Type.BYTE: (1, False),
        Data_Type.USHORT: (2, False),
        Data_Type.SHORT: (2, True),
        Data_Type.UINT: (4, False),
        Data_Type.INT: (4, True),
    }

    def __init__(self, settings: TransportSettings, protocolSettings: protocol_settings | None = None) -> None:
        # Annotations are postponed for Python 3.11+ compatibility, so the
        # concrete protocol_settings type can be used here without import-time
        # evaluation of the union expression.
        super().__init__(settings)

        # check if running on windows or linux
        self.linux = platform.system() != "Windows"

        self.port = settings.get(["port", "channel"], "")
        if not self.port:
            raise ValueError("Port/Channel is not set")

        # get default baud from protocol settings
        if self.protocolSettings is not None:
            if "baud" in self.protocolSettings.settings:
                self.baudrate = strtoint_safe(self.protocolSettings.settings["baud"])

        self.baudrate = settings.getint(["baudrate", "bitrate"], self.baudrate)
        self.interface = settings.get(["interface", "bustype"], self.interface).lower()
        self.cacheTimeout = settings.getint(["cacheTimeout"], self.cacheTimeout)

        # Serial number: accept a pre-configured value or a pinned CAN ID.
        # Both are resolved here from settings so no attribute-scope errors
        # can occur later when these values are needed inside helper methods.
        sn_from_settings: str = settings.get(["serial_number"], "").strip()
        if sn_from_settings:
            self.device_serial_number = sn_from_settings

        raw_sn_can_id: str = settings.get(["serial_number_can_id"], "").strip()
        if raw_sn_can_id:
            try:
                self.serial_number_can_id = int(raw_sn_can_id, 0)  # accepts 0x1A2 or decimal
            except ValueError:
                self._log.warning(f"serial_number_can_id {raw_sn_can_id!r} in settings is not a valid integer; ignoring")

        # setup / configure socketcan
        if self.interface == "socketcan":
            self.setup_socketcan()
            self.port = self.port.lower()

        self.bus = can.interface.Bus(interface=self.interface, channel=self.port, bitrate=self.baudrate)

        #  Instantiate AsyncBufferedReader per-instance, not at class definition.
        self.reader = can.AsyncBufferedReader()

        self.lock = threading.Lock()
        with self.lock:
            self.cache = OrderedDict()

        # transmit state; must exist before init_after_connect() can enable writes
        self._tx_lock = threading.Lock()
        self._tx_frames = {}

        #  Assign the thread to self.thread so the instance holds a reference
        # and the thread is not silently lost. Previously, the local variable `thread` shadowed
        # the class attribute `self.thread`, leaving self.thread as None.
        self.thread = threading.Thread(target=self.start_loop, name="CANBus_Read", daemon=True)
        self.thread.start()

        self.connected = True
        self.emptyTime = time.time()

        self.init_after_connect()

    def setup_socketcan(self) -> None:
        ''' Bring the socketcan interface down, configure it, and bring it back up. '''
        if not self.linux:
            self._log.warning("setup_socketcan: not supported on Windows; skipping")
            return

        self._log.info(f"setup_socketcan: configuring {self.port} at {self.baudrate} bps")

        commands: list[list[str]] = [
            ["ip", "link", "set", self.port, "down"],
            ["ip", "link", "set", self.port, "type", "can", "restart-ms", "100"],
            ["ip", "link", "set", self.port, "up", "type", "can", "bitrate", str(self.baudrate)],
        ]

        for cmd in commands:
            try:
                result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
                    cmd,
                    shell=False,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if result.stdout:
                    self._log.debug(f"setup_socketcan: {' '.join(cmd)!r} → {result.stdout.strip()}")
            except subprocess.CalledProcessError as e:
                self._log.error(
                    f"setup_socketcan: command {' '.join(cmd)!r} failed "
                    f"(exit {e.returncode}): {e.stderr.strip()}"
                )
                raise

    def is_socketcan_up(self) -> bool:
        if not self.linux:
            self._log.error("socketcan status not implemented for windows")
            return True

        try:
            with open(f"/sys/class/net/{self.port}/operstate") as f:
                state: str = f.read().strip()
        except FileNotFoundError:
            return False
        else:
            return state == "up"

    def start_loop(self) -> None:
        self.read_bus()

    def read_bus(self) -> None:
        ''' read canbus and store results in cache '''
        msg = None  # fix scope bug

        while True:
            try:
                if self.bus is not None:
                    msg: can.Message | None = self.bus.recv()  # blocking call

            except can.CanError as e:
                self._log.error(f"CAN error: {e}")
            #  Removed the `except asyncio.CancelledError` branch.
            # read_bus() is a plain (non-async) function running in a background thread,
            # so asyncio.CancelledError can never be raised here — it is only raised inside
            # coroutines by the asyncio scheduler. Catching it here was both unreachable and
            # misleading. Interruption of this thread is handled naturally by its daemon=True
            # flag: the thread exits automatically when the main process exits.
            except Exception as e:
                self._log.error(f"An unexpected error occurred: {e}")

            if msg:
                self._log.info(f"Received message: {msg.arbitration_id:X}, data: {msg.data}")

                if self.lock is not None:
                    with self.lock:
                        if self.cache is not None:
                            # convert bytearray to bytes
                            self.cache[msg.arbitration_id] = (bytes(msg.data), time.time())

    def clean_cache(self) -> None:
        current_time: float = time.time()

        if self.lock is not None:
            with self.lock:
                if self.cache is not None:
                    # Build list of stale keys first; never delete while iterating
                    keys_to_delete = [
                        msg_id
                        for msg_id, (_, timestamp) in self.cache.items()
                        if current_time - timestamp > self.cacheTimeout
                    ]
                    for key in keys_to_delete:
                        del self.cache[key]

    def init_after_connect(self) -> bool:
        '''
        Post-connection initialization hook.

        Allows the bus a short settling window so the passive cache can
        accumulate frames, then attempts to detect the serial number.
        Write mode is enabled here if configured.
        '''
        if self.write_enabled:
            self.enable_write()

        if not self.device_serial_number:
            self.device_serial_number = self.read_serial_number()

        return True

    # ------------------------------------------------------------------
    # Serial number helpers
    # ------------------------------------------------------------------

    # Minimum printable-ASCII ratio a frame must have to be considered a
    # serial-number candidate (roughly 75 % of its payload bytes).
    _SN_ASCII_RATIO: float = 0.75

    # Minimum number of distinct alphanumeric characters required so that
    # a frame of all-zeros or all-0xFF padding doesn't score as a match.
    _SN_MIN_ALNUM: int = 4

    # How long (seconds) to wait for the bus to populate the cache before
    # scanning for a serial number on the first call.
    _SN_SETTLE_SECS: float = 2.0

    def read_serial_number(self) -> str:
        '''
        Return the device serial number, using the first source that succeeds:

          1. **Settings value** — if ``serial_number`` was present in the
             transport settings it was already stored in
             ``self.device_serial_number`` during ``__init__`` and
             ``init_after_connect`` will never call this method.  This path
             is therefore only reached when no static value was configured.

          2. **Pinned CAN ID** — if ``serial_number_can_id`` was set in
             settings, read that specific frame from the passive cache and
             decode it.  This is the fast path for production once the
             correct CAN ID has been confirmed via candump.

          3. **Heuristic sniff** — scan every cached frame for a payload
             that looks like an ASCII or packed-integer serial number and
             return the best-scoring candidate.  The log message names the
             winning CAN ID so it can be pinned in settings for future runs.

        Returns the detected serial number string, or '' if not found.
        '''
        # pinned CAN ID (resolved in __init__ from settings) ---
        if self.serial_number_can_id is not None:
            return self._sn_from_can_id(self.serial_number_can_id)

        # let the cache settle if it is still empty ------------
        deadline: float = time.monotonic() + self._SN_SETTLE_SECS
        while time.monotonic() < deadline:
            if self._sn_cache_snapshot():
                break
            time.sleep(0.1)
        else:
            self._log.warning("read_serial_number: cache still empty after settling window; giving up")
            return ""

        # heuristic scan ---------------------------------------
        snapshot: dict[int, bytes] = self._sn_cache_snapshot()
        candidates: list[tuple[float, int, str]] = []

        for can_id, payload in snapshot.items():
            score, decoded = self._sn_score_frame(can_id, payload)
            if score > 0:
                candidates.append((score, can_id, decoded))
                self._log.debug(
                    f"read_serial_number: candidate CAN ID 0x{can_id:X}  "
                    f"score={score:.2f}  value={decoded!r}  raw={payload.hex()}"
                )

        if not candidates:
            self._log.warning(
                "read_serial_number: no serial-number-like frames found in cache. "
                "Run candump and look for a frame whose payload is printable ASCII "
                "or a packed integer, then set serial_number_can_id in settings."
            )
            return ""

        candidates.sort(key=lambda t: t[0], reverse=True)
        best_score, best_id, best_value = candidates[0]
        self._log.info(
            f"read_serial_number: selected CAN ID 0x{best_id:X}  "
            f"value={best_value!r}  score={best_score:.2f}  "
            f"(pin with serial_number_can_id = 0x{best_id:X} once confirmed)"
        )
        return best_value

    def _sn_from_can_id(self, can_id: int) -> str:
        '''
        Read a serial number directly from a specific, known CAN ID in the
        cache.  Used when the operator has already identified the correct
        frame via candump and pinned it in settings.
        '''
        snapshot: dict[int, bytes] = self._sn_cache_snapshot()
        payload: bytes | None = snapshot.get(can_id)
        if payload is None:
            self._log.warning(
                f"_sn_from_can_id: pinned CAN ID 0x{can_id:X} not seen in cache yet; "
                "check that the device is broadcasting and the ID is correct"
            )
            return ""

        _, decoded = self._sn_score_frame(can_id, payload)
        self._log.info(f"_sn_from_can_id: CAN ID 0x{can_id:X} → {decoded!r}  raw={payload.hex()}")
        return decoded

    def _sn_cache_snapshot(self) -> dict[int, bytes]:
        '''
        Return a {can_id: payload_bytes} copy of the current cache,
        taken under the lock so the reader thread cannot modify it mid-scan.
        '''
        if self.lock is None or self.cache is None:
            return {}
        with self.lock:
            return {can_id: data for can_id, (data, _ts) in self.cache.items()}

    def _sn_score_frame(self, can_id: int, payload: bytes) -> tuple[float, str]:
        '''
        Heuristically score ``payload`` for serial-number likelihood.

        Scoring rationale
        -----------------
        Solar inverters typically encode serial numbers in one of two ways:

        * **ASCII string** — bytes are printable characters, often
          alphanumeric with dashes or underscores (e.g. ``SN12345678``).
        * **Packed integer** — one or more 16/32-bit big-endian integers
          whose decimal concatenation forms the serial number.

        Returns (score, decoded_string).  score=0 means "not a candidate".
        A higher score means higher confidence.
        '''
        if not payload:
            return 0.0, ""

        # --- ASCII path ---------------------------------------------------
        printable: int = sum(0x20 <= b < 0x7F for b in payload)
        ascii_ratio: float = printable / len(payload)
        alnum_count: int = sum(chr(b).isalnum() for b in payload if 0x20 <= b < 0x7F)

        if ascii_ratio >= self._SN_ASCII_RATIO and alnum_count >= self._SN_MIN_ALNUM:
            # Strip null padding and non-printable trailer bytes
            decoded: str = payload.rstrip(b"\x00\xff").decode("ascii", errors="replace").strip()
            if re.fullmatch(r"[A-Za-z0-9\-_.]{4,}", decoded):
                # Bonus if it starts with a letter (common inverter SN pattern)
                bonus: float = 0.2 if decoded[0].isalpha() else 0.0
                return round(ascii_ratio + bonus, 3), decoded

        # --- Packed-integer path ------------------------------------------
        # Try to interpret 2- or 4-byte big-endian unsigned integers and
        # concatenate their decimal representations.  Accept only if the
        # result looks like a plausible SN (all digits, reasonable length).
        for word_size in (4, 2):
            if len(payload) % word_size != 0:
                continue
            parts: list[str] = []
            for i in range(0, len(payload), word_size):
                word: int = int.from_bytes(payload[i:i + word_size], byteorder="big")
                # Skip all-zero or all-FF padding words
                if word in (0, (1 << (word_size * 8)) - 1):
                    continue
                parts.append(str(word))
            if not parts:
                continue
            candidate: str = "".join(parts)
            if re.fullmatch(r"\d{6,}", candidate):
                # Lower confidence than ASCII; score by word count / payload use
                score: float = 0.4 + 0.1 * len(parts)
                return round(score, 3), candidate

        return 0.0, ""

    @property
    def _tx_write_mode(self) -> TransportWriteMode:
        ''' write_type from the settings; the base class only defines write_mode when it was configured '''
        return getattr(self, "write_mode", TransportWriteMode.WRITE if self.write_enabled else TransportWriteMode.READ)

    @_tx_write_mode.setter
    def _tx_write_mode(self, value: TransportWriteMode) -> None:
        self.write_mode = value

    def _registry_map(self) -> list[registry_map_entry]:
        '''
        The protocol's registry map. A custom-bus map (<protocol>.registry_map.csv) wins; otherwise
        the HOLDING map (<protocol>.holding_registry_map.csv), which is how the pylon_can and
        victron_gx_generic_canbus files are named. Asking only for the custom-bus map left those
        protocols with nothing to decode.
        '''
        if self.protocolSettings is None:
            return []
        return self.protocolSettings.get_registry_map(Registry_Type.CUSTOM_BUS) or self.protocolSettings.get_registry_map(Registry_Type.HOLDING)

    def _writable_entries(self) -> list[registry_map_entry]:
        ''' registry rows that may be transmitted: marked W (read/write) or WO (write only) '''
        return [entry for entry in self._registry_map() if entry.write_mode in (WriteMode.WRITE, WriteMode.WRITEONLY)]

    def _bus_has_traffic(self) -> bool:
        ''' True if any frame arrives within the settling window; proves something is on the bus '''
        deadline: float = time.monotonic() + self._SN_SETTLE_SECS
        while time.monotonic() < deadline:
            if self._sn_cache_snapshot():
                return True
            time.sleep(0.1)
        return False

    def enable_write(self) -> None:
        '''
        Validate that transmitting is possible before allowing it. Writes stay disabled unless:

          1. the CAN interface is open,
          2. the protocol has at least one row marked W or WO, and
          3. frames are being received (a device is actually on the bus).

        ``write_type = relaxed`` waives check 3 (a device that only listens until commanded);
        ``write_type = unsafe`` skips validation entirely. Mirrors the Modbus transport.
        '''
        if self.write_enabled and self._tx_write_mode == TransportWriteMode.UNSAFE:
            self._log.warning("enable write - WARNING - UNSAFE MODE - validation SKIPPED")
            return

        self._log.info("Validating Protocol for Writing")
        self.write_enabled = False

        if self.bus is None:
            self._log.error("enable write FAILED - CAN interface is not open - WRITE DISABLED")
            return

        writable: list[registry_map_entry] = self._writable_entries()
        if not writable:
            self._log.error(
                "enable write FAILED - no registry rows are marked W or WO in this protocol - WRITE DISABLED"
            )
            return

        if self._bus_has_traffic():
            self.write_enabled = True
            self._log.info(f"enable write - validation passed ({len(writable)} writable rows)")
        elif self._tx_write_mode == TransportWriteMode.RELAXED:
            self.write_enabled = True
            self._log.warning(
                f"enable write - WARNING - RELAXED MODE - no frames seen on the bus ({len(writable)} writable rows)"
            )
        else:
            self._log.error("enable write FAILED - no frames seen on the bus - WRITE DISABLED")

    def send_frame(self, can_id: int, data: bytes | bytearray = b"", remote: bool = False, dlc: int = 0) -> bool:
        '''
        Transmit one frame. Use it for active commands, for example a remote-transmission
        request that makes a device answer. Requires write_enabled. Returns True if the
        frame was handed to the bus. IDs above 0x7FF are sent as extended (29-bit) frames.
        '''
        if not self.write_enabled:
            self._log.warning(f"send_frame: write is not enabled; frame 0x{can_id:X} not sent")
            return False
        if self.bus is None:
            self._log.error(f"send_frame: CAN interface is not open; frame 0x{can_id:X} not sent")
            return False
        if len(data) > 8 or dlc > 8:
            self._log.error(f"send_frame: a CAN frame carries at most 8 bytes; frame 0x{can_id:X} not sent")
            return False

        message: can.Message = can.Message(
            arbitration_id=can_id,
            data=None if remote else bytes(data),
            dlc=dlc if remote else None,
            is_extended_id=can_id > 0x7FF,
            is_remote_frame=remote,
        )
        try:
            self.bus.send(message)
        except can.CanError as e:
            self._log.error(f"send_frame: failed to send 0x{can_id:X}: {e}")
            return False
        return True

    def _encode_tx_value(self, entry: registry_map_entry, value: int | float | str) -> bytes | None:
        ''' convert a value into the bytes of ``entry``'s field; None (after logging) if it can't be sent safely '''
        if self.protocolSettings is None:
            return None

        spec: tuple[int, bool] | None = self._TX_INT_TYPES.get(entry.data_type)
        if spec is None:
            self._log.error(
                f"WRITE_ERROR: data type {entry.data_type.name} of '{entry.variable_name}' cannot be sent over CAN"
            )
            return None
        size, signed = spec

        if isinstance(value, str):
            value = value.strip().lower()
            value = self.protocolSettings.get_code_by_value(entry, value, fallback=value)

        if self._tx_write_mode != TransportWriteMode.UNSAFE and not self.protocolSettings.validate_registry_entry(entry, value):
            self._log.error(f"WRITE_ERROR: Invalid new value, '{value}'. Unsafe to write.")
            return None

        try:
            number: float = float(value)
            if entry.unit_mod != 1:
                number = number / entry.unit_mod
            raw: int = int(round(number))
        except (TypeError, ValueError):
            self._log.error(f"WRITE_ERROR: Cannot convert '{value}' for '{entry.variable_name}'. Unsafe to write.")
            return None

        bits: int = size * 8
        low: int = -(1 << (bits - 1)) if signed else 0
        high: int = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
        if raw < low or raw > high:
            self._log.error(
                f"WRITE_ERROR: Value {raw} is outside the {entry.data_type.name} range for '{entry.variable_name}'."
            )
            return None

        encoded: bytes = raw.to_bytes(size, byteorder="big", signed=signed)

        # Inverse of process_register_bytes: decoding reverses the words and then swaps bytes within
        # each word, so encoding swaps bytes first and then reverses the words.
        word_order = self.protocolSettings.get_entry_byteorder(entry)
        if size > 1 and word_order.bytes_reversed:
            swapped: bytearray = bytearray(encoded)
            for i in range(0, len(swapped) - 1, 2):
                swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            encoded = bytes(swapped)
        if size > 2 and word_order.word_reversed:
            words: list[bytes] = [encoded[i:i + 2] for i in range(0, len(encoded), 2)]
            encoded = b"".join(reversed(words))
        return encoded

    def _build_tx_frame(self, entry: registry_map_entry, field: bytes) -> bytes | None:
        '''
        Place ``field`` at the entry's byte offset inside its frame. Bytes outside the field come from
        the last frame we sent with this ID, else the last frame seen on the bus, else zeros.
        '''
        can_id: int = entry.register
        base: bytes | None = self._tx_frames.get(can_id)
        if base is None and self.lock is not None:
            with self.lock:
                if self.cache is not None and can_id in self.cache:
                    base = self.cache[can_id][0]

        end: int = entry.register_byte + len(field)
        if end > 8:
            self._log.error(
                f"WRITE_ERROR: '{entry.variable_name}' ends at byte {end}; a CAN frame carries at most 8 bytes."
            )
            return None

        frame: bytearray = bytearray(base or b"")
        if len(frame) < end:
            frame.extend(b"\x00" * (end - len(frame)))
        frame[entry.register_byte:end] = field
        return bytes(frame)

    def write_data(self, data: dict[str, int | float | str], from_transport: transport_base) -> None:
        ''' send a frame for each variable that matches a writable (W / WO) row; others are ignored '''
        if not self.write_enabled or self.protocolSettings is None or self._tx_lock is None:
            return

        writable: dict[str, registry_map_entry] = {entry.variable_name: entry for entry in self._writable_entries()}

        with self._tx_lock:
            for variable_name, value in data.items():
                entry: registry_map_entry | None = writable.get(variable_name)
                if entry is None:
                    continue

                field: bytes | None = self._encode_tx_value(entry, value)
                if field is None:
                    continue

                frame: bytes | None = self._build_tx_frame(entry, field)
                if frame is None:
                    continue

                if self.send_frame(entry.register, frame):
                    self._tx_frames[entry.register] = frame
                    self._log.info(f"WRITE CAN: {variable_name} => {value} in frame 0x{entry.register:X} data={frame.hex()}")

    def read_data(self) -> dict[str, int | float | str]:
        ''' because canbus is passive / broadcast, we just read from the cache '''
        info: dict[str, int | float | str] = {}

        if self.lock is not None:
            with self.lock:
                if self.cache is not None and self.protocolSettings is not None:
                    registry: dict[int, bytes] = {key: value[0] for key, value in self.cache.items()}

                    new_info: dict[str, int | float | str] = self.protocolSettings.process_registery(
                        registry,
                        self._registry_map()
                    )
                    info.update(new_info)
                    currentTime: float = time.time()

                    if not info:
                        self._log.info("Register/Cache is Empty; no new information reported.")
                        if self.emptyTime is not None:
                            if currentTime - self.emptyTime > self.watchDogTime:
                                self._log.error("Register/Cache has been empty...")
                                #  Replaced quit() with sys.exit() — see read_serial_number above.
                                sys.exit(1)
                    else:
                        self.emptyTime = currentTime

        # clean_cache() takes self.lock itself; calling it while the lock is held deadlocks,
        # because threading.Lock is not re-entrant.
        self.clean_cache()

        return info

    def read_variable(self, variable_name: str, registry_type: Registry_Type, entry: registry_map_entry | None = None) -> int | float | str | None:
        ''' reads variable from cache'''
        if variable_name:
            variable_name = variable_name.strip().lower().replace(" ", "_")

        if self.cache is not None and self.protocolSettings is not None:
            registry_map: list[registry_map_entry] = self.protocolSettings.get_registry_map(registry_type)

            if entry is None:
                for e in registry_map:
                    if e.variable_name == variable_name:
                        entry = e
                        break

            if entry:
                if self.lock is not None:
                    with self.lock:
                        if entry.register in self.cache:
                            value: int | float | str | None = self.protocolSettings.process_register_bytes(self.cache, entry)
                            return value
                        else:
                            return None  # empty

        return None
