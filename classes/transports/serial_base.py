# Description: Serial base transport class and the serial_frame_client wire layer it owns: shared per-port client, locking, lifecycle cleanup, and command/response polling for serial protocols
# File: serial_base.py
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

# This module holds the two layers of the serial stack:
#
#   serial_frame_client  the wire. One instance per physical serial port. Finds SOI/EOI framed
#                        messages in the byte stream (on demand, or in a background thread).
#                        Knows nothing about protocols, settings, scheduling or bridges.
#   serial_base          the base transport. One instance per config section (per device). It
#                        owns the clients: it creates one per port, shares it between the
#                        sections on that port, and closes it on shutdown.
#
# Concrete transports (serial_pylon, serial_frame_transport) subclass serial_base. They never
# create a client themselves and nothing outside this module needs to.
#
# serial_base is the base class for scraper transports that talk over a serial
# (RS485/RS232/USB) port with SOI/EOI framed messages. It plays the same role for serial
# protocols that modbus_base plays for Modbus:
#
#   * one serial client per port, shared by every config section on that port, so several
#     devices (one section per address) can sit on the same bus
#   * a per-port lock, plus the gateway's bus_lock in interleaved mode, so a request and
#     its response are never interleaved with another device's
#   * scrape_target, so the gateway schedules the transport in every read_mode
#   * cleanup(), which closes the shared client and evicts it from the registry so a
#     replacement gateway (web UI reload) opens the port afresh, exactly as modbus_base does
#   * read_data / read_data_iter built on a single hook, _read_command(), for
#     command/response protocols
#
# A concrete transport supplies the wire format. For command/response protocols (see
# serial_pylon): SOI/EOI markers, build_frame(), decode_frame() and _read_command().
# Listen-only protocols (see serial_frame_transport) override read_data() and
# read_data_iter() instead and use the client directly. Settings read here are declared to the
# web UI through the "_serial_base" entry in classes/WebServer/transport_defaults.json,
# because the UI's AST scanner reads each transport file on its own and does not follow
# Python inheritance. A concrete transport must also declare its own literal
# transport_type = "scraper" for the same reason.
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import serial

from classes.protocol_settings import (
    registry_map_entry,
)
from defs.common import (
    TransportSettings,
    find_usb_serial_port,
    get_usb_serial_port_info,
)

from .transport_base import transport_base


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


class serial_base(transport_base):

    transport_type = "base class"
    ''' abstract; concrete serial transports declare their own transport_type = "scraper" '''

    # One serial client per "port(baudrate)", shared by every section on that port
    # (mirrors modbus_base.clients). Class level, so it is shared across subclasses too.
    clients : dict[str, serial_frame_client] = {}
    _clients_lock : threading.Lock = threading.Lock()
    _port_locks : dict[str, threading.RLock] = {}

    SOI : bytes = b""
    ''' start-of-frame marker; subclasses must set this '''
    EOI : bytes = b""
    ''' end-of-frame marker; subclasses must set this '''

    MIN_ADDRESS : int = 0
    MAX_ADDRESS : int = 255
    ''' inclusive range accepted for the "address" setting '''

    OPEN_ON_INIT : bool = True
    '''
    True: open the serial port while the transport is constructed, so a missing port fails
    start-up (the original serial_pylon behavior).
    False: open it in connect(), so a device that is not there yet is retried every cycle.
    '''

    address : int = 0
    addresses : list[int] = []

    _client : serial_frame_client | None = None
    _client_key : str = ""
    _markers : tuple[bytes, bytes] = (b"", b"")

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        soi, eoi = self._frame_markers(settings)
        if not soi or not eoi:
            msg: str = f"{type(self).__name__} frame markers (SOI/EOI) must not be empty"
            raise ValueError(msg)
        self._markers = (soi, eoi)

        self._port_setting: str = settings.get("port", fallback="/dev/ttyUSB0")
        if not self._port_setting:
            raise ValueError("Port is not set")
        self.port: str = self._port_setting

        # "baud" and "unit_id" are the names used in the device docs.
        self.baudrate: int = settings.getint(["baudrate", "baud"], fallback=9600)

        address: int = settings.getint(["address", "unit_id"], fallback=0)
        if not self.MIN_ADDRESS <= address <= self.MAX_ADDRESS:
            msg = f"{type(self).__name__} address must be {self.MIN_ADDRESS}-{self.MAX_ADDRESS}, got {address}"
            raise ValueError(msg)
        self.address = address
        self.addresses = [address]

        self._client_key = f"{self.port}({self.baudrate})"

        if self.OPEN_ON_INIT:
            self._acquire_client()
        else:
            try:
                self._resolve_port()
            except ValueError:
                self._log.debug(f"Serial port {self.port} not found yet; it will be retried on connect")

    def _frame_markers(self, settings : TransportSettings) -> tuple[bytes, bytes]:
        ''' the (SOI, EOI) frame markers. The default is the class constants; override to read them from settings. '''
        if not self.SOI or not self.EOI:
            msg: str = f"{type(self).__name__} must define the SOI and EOI frame markers"
            raise NotImplementedError(msg)
        return self.SOI, self.EOI

    def _serial_parameters(self) -> dict[str, Any]:
        ''' pyserial framing for the client; override for protocols that are not 8N1 '''
        return {
            "bytesize": 8,
            "parity": serial.PARITY_NONE,
            "stopbits": 1,
            "exclusive": True,
        }

    @property
    def client(self) -> serial_frame_client:
        ''' the shared serial client; only available once the port has been opened '''
        if self._client is None:
            msg: str = f"{type(self).__name__} has not opened its serial port yet"
            raise RuntimeError(msg)
        return self._client

    def _resolve_port(self) -> None:
        ''' resolves the configured port (a path or a [vendor:product:serial:location] pattern); ValueError if it is not found '''
        resolved_port: str | None = find_usb_serial_port(self._port_setting)
        if not resolved_port:
            raise ValueError("Port is not valid / not found")
        if resolved_port != self.port:
            self._log.debug(f"Serial port {self._port_setting} resolved to {resolved_port}")
        self.port = resolved_port
        self._client_key = f"{self.port}({self.baudrate})"

    def _acquire_client(self) -> serial_frame_client:
        '''
        Opens the serial port, or joins the client another section on the same port already
        opened. Raises ValueError (port not found, or frame markers that disagree with the
        other section) or OSError (the port cannot be opened).
        '''
        if self._client is not None:
            return self._client

        self._resolve_port()
        soi, eoi = self._markers

        with self._clients_lock:
            shared_client: serial_frame_client | None = self.clients.get(self._client_key)
            if shared_client is None:
                self._log.info("Serial Port : " + self.port + " = "+get_usb_serial_port_info(self.port)) #print for config convenience
                shared_client = serial_frame_client(self.port,
                                                    self.baudrate,
                                                    soi,
                                                    eoi,
                                                    **self._serial_parameters())
                self.clients[self._client_key] = shared_client
            else:
                if shared_client.soi != soi or shared_client.eoi != eoi:
                    msg: str = (f"{type(self).__name__} on {self.port} uses frame markers {soi!r}/{eoi!r}, "
                                f"but another section on the same port opened it with {shared_client.soi!r}/{shared_client.eoi!r}")
                    raise ValueError(msg)
                self._log.debug(f"Sharing serial client {self._client_key} (address {self.address})")
            self._client = shared_client
        return shared_client

    def _release_client(self) -> None:
        '''
        Closes the shared client and evicts it from the registry. Without the eviction a
        transport constructed for the same port after a gateway reload would find the old,
        closed client in the registry and reuse it. Safe to call from every section sharing
        the client: only the first one evicts, and closing twice is harmless.
        '''
        client: serial_frame_client | None = self._client
        self._client = None
        if client is None:
            return

        with self._clients_lock:
            if self.clients.get(self._client_key) is client:
                del self.clients[self._client_key]

        try:
            client.close()
            self._log.info(f"Closed serial port {self.port} for {self.transport_name}")
        except Exception as err:
            self._log.warning(f"Error closing serial port {self.port} for {self.transport_name}: {err}")

    # ------------------------------------------------------------------ #
    # scheduling                                                         #
    # ------------------------------------------------------------------ #

    @property
    def scrape_target(self) -> str:
        '''
        Scheduling path: All (Sequential, Concurrent, Interleaved).

        Identifies the physical device: serial port, protocol and bus address. The gateway
        only schedules transports with a non-empty scrape_target, and sections that read
        the same device (same port, protocol and address) are consolidated into one group.
        Sections for different addresses on the same port stay separate groups.
        '''
        protocol: str = str(getattr(self, "protocol_version", "") or "")
        return f"{self.port}:{protocol}:{self.address}"

    def interleaved_cycle_timeout(self) -> float:
        '''
        Scheduling path: Interleaved only.

        Worst case is every command timing out, so scale with the command count rather than
        using the 60 second default.
        '''
        default: float = super().interleaved_cycle_timeout()
        commands: int = len(self._poll_commands())
        client_timeout: float = float(self._client.timeout if self._client is not None else serial_frame_client.timeout)
        return max(default, 2.0 * commands * client_timeout)

    # ------------------------------------------------------------------ #
    # wire access                                                        #
    # ------------------------------------------------------------------ #

    def _get_port_lock(self) -> threading.RLock:
        with self._clients_lock:
            if self._client_key not in self._port_locks:
                self._port_locks[self._client_key] = threading.RLock()
            return self._port_locks[self._client_key]

    def build_frame(self, command: int, info: bytes = b"") -> bytes:
        ''' builds a request frame without SOI/EOI (the frame client adds them) '''
        raise NotImplementedError

    def decode_frame(self, raw_frame: bytes) -> SimpleNamespace | None:
        ''' parses a response frame (SOI/EOI already stripped); None if it is invalid '''
        raise NotImplementedError

    def send_command(self, cmd: int, info: bytes = b"") -> None:
        data: bytes = self.build_frame(cmd, info)
        self.client.write(data)

    def _exchange(self, command : int, info : bytes = b"") -> bytes | None:
        '''
        Sends a command and returns the raw response frame (SOI/EOI stripped), or None.

        Holds the gateway's bus_lock (interleaved mode) and the per-port lock for the whole
        request/response round trip, so devices sharing the bus never interleave a request
        with another device's response. The bus_lock is only held for one exchange, never
        across a yield, so other transports on the wire get their turn between commands.
        '''
        bus_lock: threading.Lock | None = self.bus_lock
        if bus_lock is not None:
            bus_lock.acquire()
        try:
            with self._get_port_lock():
                self.send_command(command, info)
                frame: list[bytes] | bytes | None = self.client.read()
        finally:
            if bus_lock is not None:
                bus_lock.release()

        if isinstance(frame, list):
            frame = b"".join(frame)
        return frame or None

    # ------------------------------------------------------------------ #
    # connection                                                         #
    # ------------------------------------------------------------------ #

    def _handshake(self) -> bool:
        '''
        Hook run by connect(): confirm the device answers and negotiate anything needed
        before polling (protocol version, device name, ...). Return True when the device
        responded. The default trusts the port, since opening a serial port proves nothing
        about the device on it.
        '''
        return True

    def _prepare_client(self, client : serial_frame_client) -> None:
        ''' hook run by connect() before the client is started; configure per-transport client settings here '''

    def connect(self) -> bool:
        try:
            client: serial_frame_client = self._acquire_client()
            self._prepare_client(client)
            client.connect()
        except (OSError, ValueError) as err:
            # a device that is not there yet (or a config clash) is retried on the next cycle
            self._log.warning(f"Failed to open serial port {self.port}: {err}")
            self.connected = False
            return False

        if self._handshake():
            self.connected = True
        return self.connected

    def cleanup(self) -> None:
        '''
        Scheduling path: N/A — shutdown, runs once regardless of read_mode.

        Closes the serial port (stopping any async reader) and evicts the shared client,
        then lets transport_base mark the transport disconnected.
        '''
        self._release_client()
        super().cleanup()

    # ------------------------------------------------------------------ #
    # polling                                                            #
    # ------------------------------------------------------------------ #

    def _read_command(self, command : int) -> bytes | None:
        '''
        Sends one registry command and returns its decoded payload as raw bytes for
        protocol_settings.process_registery(), or None if there was no valid response.
        '''
        raise NotImplementedError

    def _poll_commands(self) -> list[int]:
        ''' the distinct commands to send each cycle: the registry map's register numbers, in order '''
        if self.protocolSettings is None:
            return []

        commands: list[int] = []
        seen: set[int] = set()
        entry: registry_map_entry
        for entry in self.protocolSettings.get_registry_map():
            if entry.register not in seen:
                seen.add(entry.register)
                commands.append(entry.register)
        return commands

    def _poll_commands_iter(self) -> Iterator[tuple[int, bytes | None]]:
        '''
        Sends each command once and yields (command, payload) after every attempt, success or
        failure, keeping the cycle bookkeeping the gateway uses to decide whether a result is
        complete enough for completeness-sensitive bridges. Each command is tried once per
        cycle: a silent device must not cost one timeout per registry row, because it holds
        the bus that other devices are waiting for.
        '''
        commands: list[int] = self._poll_commands()
        self._cycle_expect_unit(len(commands))

        for command in commands:
            payload: bytes | None = self._read_command(command)
            if payload is None:
                self.cycle_mark_incomplete()
            else:
                self._cycle_mark_unit_complete()
            yield command, payload

    def read_data(self) -> dict[str, int | float | str]:
        '''
        Scheduling path: Sequential and Concurrent (also the fallback for standalone reads).

        Sends every command, then decodes all payloads in one process_registery pass.
        '''
        info: dict[str, int | float | str] = {}

        if self.protocolSettings is not None:
            raw_data: dict[int, bytes] = {
                command: payload
                for command, payload in self._poll_commands_iter()
                if payload is not None
            }

            if raw_data:
                info.update(self.protocolSettings.process_registery(raw_data, registry_map=self.protocolSettings.get_registry_map()))

        if not info:
            self._log.info(f"Data is empty; {type(self).__name__} busy or not responding?")

        return info

    def read_data_iter(self) -> Iterator[bool]:
        '''
        Scheduling path: Interleaved only (called directly for standalone transports, or as
        read_group_data_iter's per-member fallback).

        Reads one command at a time, yielding True after each (success or failure) so
        transports sharing a bus can take turns. Payloads are decoded once, after the last
        command, so decoding sees the same data as read_data(); call get_partial_data() to
        retrieve the result.

        When called from read_group_data_iter, _cycle_active is already True, so cycle
        tracking is left to the group iterator.
        '''
        _owner: bool = not getattr(self, '_cycle_active', False)
        if _owner:
            self._start_cycle_tracking()

        raw_data: dict[int, bytes] = {}
        for command, payload in self._poll_commands_iter():
            if payload is not None:
                raw_data[command] = payload
            yield True

        if raw_data and self.protocolSettings is not None:
            self._partial_info.update(
                self.protocolSettings.process_registery(raw_data, registry_map=self.protocolSettings.get_registry_map())
            )

        if _owner:
            self.finish_cycle_tracking(self._partial_info)
