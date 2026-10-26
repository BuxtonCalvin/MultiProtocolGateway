# Description: SOI/EOI framed serial transports: the serial_frame_client wire layer and serial_frame_transport, a listen-only reader that command/response protocols such as Pylon build on
# File: serial_frame_transport.py
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

# This module is the SOI/EOI (start/end delimiter) framing family of serial_base, and holds its
# two layers:
#
#   serial_frame_client      the wire. One instance per physical serial port. Finds framed
#                            messages in the byte stream, on demand or in a background thread.
#                            Knows nothing about protocols, settings, scheduling or bridges.
#   serial_frame_transport   the transport. One instance per config section (per device). On its
#                            own it is a generic listen-only reader: it waits for a frame and
#                            decodes it through the protocol's input registry map. A protocol
#                            that sends a command and reads the reply (see serial_pylon)
#                            subclasses it with LISTEN_ONLY = False and implements the protocol.
#
# Protocols whose frames are not delimited this way (length-prefixed or fixed-length binary,
# text blocks) are not built on this module: they subclass serial_base and bring their own
# client through _create_client().
import threading
import time
from collections.abc import Callable
from typing import Any

import serial

from classes.protocol_settings import Registry_Type, registry_map_entry
from classes.transports.transport_base import TransportWriteMode, transport_base
from defs.common import TransportSettings

from .serial_base import SerialClient, serial_base


class serial_frame_client():
    '''
    The wire layer: one instance per physical serial port, shared by every transport on that
    port (see serial_base). It finds SOI/EOI framed messages in the byte stream, either on
    demand (read) or in a background thread (asynchronous), and knows nothing about
    protocols, settings, scheduling or bridges.
    '''
    client : serial.Serial
    running : bool = False
    soi : bytes
    '''start of information'''
    eoi : bytes
    '''end of information'''
    pending_frames : list[bytes]

    max_frame_size : int = 256

    port : str = "/dev/ttyUSB0"
    baud :  int = 9600

    timeout : float = 5
    ''' timeout in seconds '''

    #region asynchronous
    asynchronous : bool = False
    ''' if set, runs main loop'''

    on_message : Callable[[bytes], None] | None = None
    ''' async mode only'''

    thread : threading.Thread | None = None
    ''' main thread for read loop'''

    callback_lock : threading.Lock
    '''lock for callback'''
    #endregion asynchronous


    def __init__(self, port : str , baud : int , soi : bytes, eoi : bytes, **kwrgs: Any) -> None:
        self.soi = soi
        self.eoi = eoi
        self.port = port
        self.baud = baud
        # per instance, not class level: a class-level list and lock would be shared by the
        # clients of every serial port
        self.pending_frames = []
        self.callback_lock = threading.Lock()
        self.client = serial.Serial(port, baud, **kwrgs)

    def connect(self) -> bool:
        if self.asynchronous:
            if self.thread is not None and self.thread.is_alive():
                return True  # already listening; a second reader thread would split the frames
            self.running = True
            self.pending_frames = []
            self.thread = threading.Thread(target=self.read_thread, name="Serial_Read", daemon=True)
            self.thread.start()
        return True

    def close(self) -> None:
        ''' stop the async read loop (if one is running) and release the serial port. Safe to call more than once. '''
        self.running = False

        thread: threading.Thread | None = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

        if self.client.is_open:
            self.client.close()

    def write(self, data : bytes) -> None:
        ''' write data, excluding SOI and EOI bytes'''
        data = self.soi + data + self.eoi
        self.client.write(data)

    def read(self, reset_buffer: bool = True, frames: int = 1) -> list[bytes] | bytes | None:
        buffer = bytearray()
        self.pending_frames.clear()

        if reset_buffer:
            self.client.reset_input_buffer()

        timedout: float = time.time() + self.timeout
        self.client.timeout = self.timeout

        while time.time() < timedout:
            data: bytes = self.client.read()

            if data:
                buffer += data
                soi_index: int = buffer.find(self.soi)

                while soi_index != -1:
                    buffer: bytearray = buffer[soi_index:]
                    eoi_index: int = buffer.find(self.eoi)

                    if eoi_index != -1:
                        # Convert slice to immutable bytes right away
                        frame: bytes = bytes(buffer[len(self.soi):eoi_index])

                        if frames == 1:
                            return frame

                        # Accumulate frames and return when we have enough
                        self.pending_frames.append(frame)
                        if len(self.pending_frames) == frames:

                            return self.pending_frames

                        buffer = buffer[eoi_index + len(self.eoi):]
                        soi_index = buffer.find(self.soi)

                    else:
                        if len(buffer) > self.max_frame_size:
                            buffer.clear()
                        break

            time.sleep(0.01)

        # Timeout reached — return whatever was collected
        return self.pending_frames if self.pending_frames else None


    def read_thread(self) -> None:
        buffer = bytearray()
        self.running = True
        while self.running:
            # Read data from serial port
            data: bytes = self.client.read()

            # Check if data is available
            if data:
                # Append data to buffer
                buffer: bytearray = buffer + data

                # Find SOI index in buffer
                soi_index: int = buffer.find(self.soi)

                # Process all occurrences of SOI in buffer
                while soi_index != -1:
                    # Remove data before SOI sequence
                    buffer: bytearray = buffer[soi_index:]

                    # Find EOI index in buffer
                    eoi_index: int = buffer.find(self.eoi)

                    if eoi_index != -1:
                        # Extract and store the complete frame as immutable bytes
                        self.pending_frames.append(bytes(buffer[len(self.soi):eoi_index]))

                        # Remove the processed data from the buffer
                        buffer: bytearray = buffer[eoi_index + len(self.eoi) : ]

                        # Find next SOI index in the remaining buffer
                        soi_index: int = buffer.find(self.soi)
                    else:
                        # If no EOI is found and buffer size exceeds max_frame_size, clear buffer
                        if len(buffer) > self.max_frame_size:
                            buffer.clear()
                        break #no eoi, continue waiting

                # Process new frames
                for frame in self.pending_frames:
                    with self.callback_lock:
                        if self.on_message:
                            self.on_message(frame)

                # Clear the processed frames so they don't trigger again on the next read iteration
                self.pending_frames.clear()

            time.sleep(0.01)


class serial_frame_transport(serial_base):

    transport_type: str = "scraper"
    """
    Transport that communicates over a serial port using an SOI/EOI framing
    protocol, implemented via serial_frame_client and built on serial_base.

    Used directly it is a listen-only reader. Required config keys (in addition to
    transport_base keys):
        port        - serial device path, e.g. /dev/ttyUSB0
        baudrate    - baud rate, e.g. 9600 (also accepted as "baud")
        soi         - start-of-information byte(s) as a hex string, e.g. 0x7E or 7E
        eoi         - end-of-information byte(s) as a hex string, e.g. 0x0D or 0D

    Optional config keys:
        timeout     - read timeout in seconds (default 5.0)
        async_mode  - if true, run the background read thread (default false). The
                      port then belongs to this transport; it cannot be shared.

    The port is opened in connect() rather than at start-up, so a device that is not
    plugged in yet is retried every cycle instead of stopping the gateway.

    Subclassing for a command/response protocol: set LISTEN_ONLY = False, SOI and EOI,
    and implement build_frame(), decode_frame() and _read_command() (see serial_pylon).
    """

    LISTEN_ONLY : bool = True
    '''
    True: the device speaks on its own, so each read waits for one frame (the generic reader).
    False: a command/response protocol, polled through serial_base's command loop.
    '''

    LISTEN : int = 0
    ''' the single pseudo-command a listen-only read uses '''

    SOI : bytes = b""
    EOI : bytes = b""
    ''' frame markers. A protocol with fixed markers sets them; otherwise they come from the soi/eoi settings. '''

    OPEN_ON_INIT : bool = False
    EXCLUSIVE_PORT : bool = False

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(self, settings: TransportSettings) -> None:
        # These come first: serial_base opens the port during construction when OPEN_ON_INIT
        # is True, and the client needs the markers.
        self._markers: tuple[bytes, bytes] = self._frame_markers(settings)
        if not self._markers[0] or not self._markers[1]:
            msg: str = f"{type(self).__name__} frame markers (SOI/EOI) must not be empty"
            raise ValueError(msg)

        self._timeout: float = float(settings.get("timeout", fallback="5.0"))
        self._async_mode: bool = settings.getboolean("async_mode", fallback=False)
        if self._async_mode and not self.LISTEN_ONLY:
            msg = f"{type(self).__name__} is a command/response protocol, so async_mode cannot be used"
            raise ValueError(msg)

        # serial_base reads port and baudrate/baud; transport_base loads protocolSettings and device metadata.
        super().__init__(settings)

    # Attribute names kept for subclasses written against the earlier, standalone version.
    @property
    def _port(self) -> str:
        return self.port

    @property
    def _baud(self) -> int:
        return self.baudrate

    @property
    def _soi(self) -> bytes:
        return self._markers[0]

    @property
    def _eoi(self) -> bytes:
        return self._markers[1]

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_frame_marker(value: str) -> bytes:
        """Convert a hex string like '7E' or '0x7E' to bytes."""
        value = value.strip()
        if value.startswith("0x") or value.startswith("0X"):
            value = value[2:]
        try:
            return bytes.fromhex(value)
        except ValueError:
            # Fallback: treat as a literal ASCII/UTF-8 string.
            return value.encode("utf-8")

    def _frame_markers(self, settings: TransportSettings) -> tuple[bytes, bytes]:
        """The (SOI, EOI) markers: the class constants when a protocol fixes them, else the soi/eoi settings."""
        if self.SOI and self.EOI:
            return self.SOI, self.EOI
        # SOI / EOI may be provided as hex strings ("7E", "0x7E") or raw chars.
        return (self._parse_frame_marker(settings.get("soi", fallback="7E")),
                self._parse_frame_marker(settings.get("eoi", fallback="0D")))

    # ------------------------------------------------------------------ #
    # serial_base interface                                                #
    # ------------------------------------------------------------------ #

    def _create_client(self) -> serial_frame_client:
        soi, eoi = self._markers
        return serial_frame_client(self.port, self.baudrate, soi, eoi, **self._serial_parameters())

    def _check_shared_client(self, shared: SerialClient) -> None:
        if isinstance(shared, serial_frame_client) and (shared.soi != self._soi or shared.eoi != self._eoi):
            msg: str = (f"{type(self).__name__} on {self.port} uses frame markers {self._soi!r}/{self._eoi!r}, "
                        f"but another section on the same port opened it with {shared.soi!r}/{shared.eoi!r}")
            raise ValueError(msg)

    def _prepare_client(self, client: SerialClient) -> None:
        """Apply the read timeout and, in async mode, hook up the frame callback."""
        if not isinstance(client, serial_frame_client):
            msg: str = f"{type(self).__name__} needs a serial_frame_client, got {type(client).__name__}"
            raise TypeError(msg)

        client.timeout = self._timeout
        client.asynchronous = self._async_mode

        if self._async_mode:
            owner: Callable[[bytes], None] | None = client.on_message
            if owner is not None and owner != self._on_async_frame:
                msg = f"async_mode needs serial port {self.port} to itself, but another section is already listening on it"
                raise ValueError(msg)
            client.on_message = self._on_async_frame

    def _client_timeout(self) -> float:
        return self._timeout

    def _handshake(self) -> bool:
        self._log.info("Connected to serial port %s @ %d baud (async=%s)", self.port, self.baudrate, self._async_mode)
        return True

    # ------------------------------------------------------------------ #
    # Reading                                                              #
    # ------------------------------------------------------------------ #

    def _poll_commands(self) -> list[int]:
        """A listen-only read has no commands to send: one read is one frame, however big the map is."""
        return [self.LISTEN] if self.LISTEN_ONLY else super()._poll_commands()

    def _read_command(self, command: int) -> bytes | None:
        if not self.LISTEN_ONLY:
            return super()._read_command(command)
        return self._listen_for_frame()

    def _process_raw(self, raw_data: dict[int, bytes]) -> dict[str, int | float | str]:
        if not self.LISTEN_ONLY:
            return super()._process_raw(raw_data)
        frame: bytes | None = raw_data.get(self.LISTEN)
        return self._decode_frame(frame) if frame else {}

    def _listen_for_frame(self) -> bytes | None:
        """
        Send nothing (pure listen) and wait for one frame, opening the port first if it is
        not open yet. Override read_data()/_read_command() in a subclass when your protocol
        requires sending a request command before the device replies.
        """
        if not self.connected or self._client is None:
            if not self.connect():
                return None

        if self._client is None:
            return None

        try:
            with self._get_port_lock():
                raw: bytes | list[bytes] | None = self._client.read(reset_buffer=True, frames=1)
        except Exception:
            self._log.exception("Error reading from serial port %s", self.port)
            self.connected = False
            return None

        if not raw or isinstance(raw, list):
            self._log.debug("No frame received from %s", self.port)
            return None

        return bytes(raw)

    def write_data(self, data: dict[str, int | float | str], from_transport: "transport_base") -> None:
        """Write registry values to the device as a raw frame."""
        # write_mode only exists when write_type is configured, hence getattr
        if getattr(self, "write_mode", None) in (None, TransportWriteMode.READ):
            self._log.debug("Write skipped — transport is in READ mode.")
            return

        if not self.connected or self._client is None:
            if not self.connect():
                return

        payload: bytes | None = self._encode_frame(data)
        if payload:
            try:
                if self._client is not None:
                    with self._get_port_lock():
                        self._client.write(payload)
                    self._log.debug("Wrote %d bytes to %s", len(payload), self.port)
            except Exception:
                self._log.exception("Error writing to serial port %s", self.port)
                self.connected = False

    # ------------------------------------------------------------------ #
    # Frame encoding / decoding                                           #
    # ------------------------------------------------------------------ #

    def _decode_frame(self, raw: bytes) -> dict[str, int | float | str]:
        """
        Convert raw frame bytes into a named-value dict using protocolSettings.

        The base implementation treats the frame as a flat byte array keyed by
        byte index.  Override this in a subclass for protocol-specific parsing
        (e.g. checksum validation, command-byte stripping, etc.).
        """
        if not self.protocolSettings:
            # No protocol map — return the raw hex string under a generic key.
            return {"raw_frame": raw.hex()}

        registry: dict[int, bytes] = {i: bytes([b]) for i, b in enumerate(raw)}

        registry_type: Registry_Type = Registry_Type.INPUT
        reg_map: list[registry_map_entry] = self.protocolSettings.registry_map.get(registry_type, [])

        if not reg_map:
            return {"raw_frame": raw.hex()}

        try:
            info: dict[str, int | float | str] = self.protocolSettings.process_registery(registry, reg_map)
        except Exception:
            self._log.exception("process_registery failed for frame: %s", raw.hex())
            return {"raw_frame": raw.hex()}

        # Fire the on_message callback for each decoded entry (matching transport_base convention).
        if self.on_message:
            entry_map: dict[str, registry_map_entry] = {e.variable_name: e for e in reg_map}
            for key, value in info.items():
                entry: registry_map_entry | None = entry_map.get(key)
                if entry:
                    try:
                        self.on_message(self, entry, str(value))
                    except Exception:
                        self._log.exception("on_message callback raised for key '%s'", key)

        return info

    def _encode_frame(self, data: dict[str, int | float | str]) -> bytes | None:
        """
        Convert a registry dict to raw bytes for transmission.

        The base implementation is intentionally minimal — override this in a
        subclass to add command bytes, checksums, etc.
        """
        if not data:
            return None

        # Default: concatenate each entry's register address as a single byte.
        # This is a placeholder; real protocols need a proper encoder.
        payload = bytearray()
        for entry in data.values():
            if isinstance(entry, registry_map_entry):
                payload += entry.register.to_bytes(2, byteorder="big")
        return bytes(payload) if payload else None

    # ------------------------------------------------------------------ #
    # Async support                                                        #
    # ------------------------------------------------------------------ #

    def _on_async_frame(self, raw: bytes) -> None:
        """
        Callback wired to serial_frame_client.on_message in async mode.
        Decodes the frame and fires transport_base.on_message for each field.
        """
        self._decode_frame(raw)  # on_message callbacks are fired inside _decode_frame
