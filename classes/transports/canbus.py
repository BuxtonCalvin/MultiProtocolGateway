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

import can

from defs.common import TransportSettings, strtoint_safe

from ..protocol_settings import Registry_Type, protocol_settings, registry_map_entry
from .transport_base import transport_base


class canbus(transport_base):

    transport_type = "scraper"
    ''' canbus is a more passive protocol; todo to include active commands to trigger canbus responses '''

    interface: str = "socketcan"
    ''' bustype / interface for canbus device '''

    port: str = ""
    ''' 'can0' '''

    baudrate: int = 500000

    bus: can.BusABC | None = None
    ''' holds canbus interface'''

    #  Do NOT instantiate can.AsyncBufferedReader() at class definition time.
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

    def __init__(self, settings: TransportSettings, protocolSettings: protocol_settings | None = None) -> None:
        #  Removed the string-quoted forward reference "protocol_settings | None".
        # PEP 649 (lazy annotation evaluation) is the default in 3.14, so forward references
        # in annotations no longer need to be quoted strings. Using the bare type is cleaner
        # and consistent with the rest of the codebase.
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
        self.cacheTimeout = settings.getint(["cacheTimeout", "cache_timeout"], self.cacheTimeout)

        # Serial number: accept a pre-configured value or a pinned CAN ID.
        # Both are resolved here from settings so no attribute-scope errors
        # can occur later when these values are needed inside helper methods.
        sn_from_settings: str = settings.get(["serial_number", "sn"], "").strip()
        if sn_from_settings:
            self.device_serial_number = sn_from_settings

        raw_sn_can_id: str = settings.get(["serial_number_can_id", "sn_can_id"], "").strip()
        if raw_sn_can_id:
            try:
                self.serial_number_can_id = int(raw_sn_can_id, 0)  # accepts 0x1A2 or decimal
            except ValueError:
                self._log.warning(
                    f"serial_number_can_id {raw_sn_can_id!r} in settings is not a valid integer; ignoring"
                )

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

        commands = [
            ["ip", "link", "set", self.port, "down"],
            ["ip", "link", "set", self.port, "type", "can", "restart-ms", "100"],
            ["ip", "link", "set", self.port, "up", "type", "can", "bitrate", str(self.baudrate)],
        ]

        for cmd in commands:
            try:
                result = subprocess.run(  # noqa: S603
                    cmd,
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
                    msg = self.bus.recv()  # blocking call

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
        current_time = time.time()

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
        # --- Step 1: pinned CAN ID (resolved in __init__ from settings) ---
        if self.serial_number_can_id is not None:
            return self._sn_from_can_id(self.serial_number_can_id)

        # --- Step 2: let the cache settle if it is still empty ------------
        deadline = time.monotonic() + self._SN_SETTLE_SECS
        while time.monotonic() < deadline:
            if self._sn_cache_snapshot():
                break
            time.sleep(0.1)
        else:
            self._log.warning("read_serial_number: cache still empty after settling window; giving up")
            return ""

        # --- Step 3: heuristic scan ---------------------------------------
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
        snapshot = self._sn_cache_snapshot()
        payload = snapshot.get(can_id)
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
        printable = sum(0x20 <= b < 0x7F for b in payload)
        ascii_ratio = printable / len(payload)
        alnum_count = sum(chr(b).isalnum() for b in payload if 0x20 <= b < 0x7F)

        if ascii_ratio >= self._SN_ASCII_RATIO and alnum_count >= self._SN_MIN_ALNUM:
            # Strip null padding and non-printable trailer bytes
            decoded = payload.rstrip(b"\x00\xff").decode("ascii", errors="replace").strip()
            if re.fullmatch(r"[A-Za-z0-9\-_.]{4,}", decoded):
                # Bonus if it starts with a letter (common inverter SN pattern)
                bonus = 0.2 if decoded[0].isalpha() else 0.0
                return round(ascii_ratio + bonus, 3), decoded

        # --- Packed-integer path ------------------------------------------
        # Try to interpret 2- or 4-byte big-endian unsigned integers and
        # concatenate their decimal representations.  Accept only if the
        # result looks like a plausible SN (all digits, reasonable length).
        for word_size in (4, 2):
            if len(payload) % word_size != 0:
                continue
            parts = []
            for i in range(0, len(payload), word_size):
                word = int.from_bytes(payload[i:i + word_size], byteorder="big")
                # Skip all-zero or all-FF padding words
                if word in (0, (1 << (word_size * 8)) - 1):
                    continue
                parts.append(str(word))
            if not parts:
                continue
            candidate = "".join(parts)
            if re.fullmatch(r"\d{6,}", candidate):
                # Lower confidence than ASCII; score by word count / payload use
                score = 0.4 + 0.1 * len(parts)
                return round(score, 3), candidate

        return 0.0, ""

    def enable_write(self) -> None:
        self.write_enabled = True
        self._log.warning("enable write - validation on the todo")

    def write_data(self, data: dict[str, int | float | str], from_transport: transport_base) -> None:
        if not self.write_enabled:
            return

    def read_data(self) -> dict[str, int | float | str]:
        ''' because canbus is passive / broadcast, we just read from the cache '''
        info: dict[str, int | float | str] = {}

        if self.lock is not None:
            with self.lock:
                if self.cache is not None and self.protocolSettings is not None:
                    registry = {key: value[0] for key, value in self.cache.items()}

                    new_info: dict[str, int | float | str] = self.protocolSettings.process_registery(
                        registry,
                        self.protocolSettings.get_registry_map(Registry_Type.ZERO)
                    )
                    info.update(new_info)
                    currentTime = time.time()

                    if not info:
                        self._log.info("Register/Cache is Empty; no new information reported.")
                        if self.emptyTime is not None:
                            if currentTime - self.emptyTime > self.watchDogTime:
                                self._log.error("Register/Cache has been empty...")
                                #  Replaced quit() with sys.exit() — see read_serial_number above.
                                sys.exit(1)
                    else:
                        self.emptyTime = currentTime

                    self.clean_cache()

        return info

    def read_variable(self, variable_name: str, registry_type: Registry_Type, entry: registry_map_entry | None = None) -> int | float | str | None:
        ''' reads variable from cache'''
        if variable_name:
            variable_name = variable_name.strip().lower().replace(" ", "_")

        if self.cache is not None and self.protocolSettings is not None:
            registry_map = self.protocolSettings.get_registry_map(registry_type)

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
