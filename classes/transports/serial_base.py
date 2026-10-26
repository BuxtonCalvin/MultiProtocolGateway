# Description: Framing-agnostic serial base transport: shared per-port client, locking, lifecycle cleanup, scheduling and command/response polling for serial protocols
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

# serial_base is the base class for scraper transports that talk over a serial
# (RS485/RS232/USB) port. It plays the same role for serial protocols that modbus_base plays
# for Modbus, and like modbus_base it provides everything that is not specific to one wire format:
#
#   * one serial client per port, shared by every config section on that port, so several
#     devices (one section per address) can sit on the same bus
#   * a per-port lock, plus the gateway's bus_lock in interleaved mode, so a request and
#     its response are never interleaved with another device's
#   * scrape_target, so the gateway schedules the transport in every read_mode
#   * cleanup(), which closes the shared client and evicts it from the registry so a
#     replacement gateway (web UI reload) opens the port afresh, exactly as modbus_base does
#   * read_data / read_data_iter built on a single hook, _read_command()
#
# What it deliberately does not know is how a protocol delimits messages on the wire. A framing
# family supplies that by implementing _create_client(), which returns the object that owns the
# port (anything with the SerialClient interface below):
#
#   serial_frame_transport   SOI/EOI delimited frames; serial_pylon is built on it
#   (other families)         length-prefixed binary, fixed-length frames, text blocks, ...
#
# A framing family is an intermediate subclass of serial_base, and a protocol is a subclass of
# its family, so adding a protocol with a new framing never touches this module.
#
# Settings read here are declared to the web UI through the "_serial_base" entry in
# classes/WebServer/transport_defaults.json, because the UI's AST scanner reads each transport
# file on its own and does not follow Python inheritance. A concrete transport must also
# declare its own literal transport_type = "scraper" for the same reason.
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Protocol

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


class SerialClient(Protocol):
    """
    What serial_base needs from the object that owns one serial port.

    A framing family implements it with whatever framing its protocols use (see
    serial_frame_client in serial_frame_transport.py); serial_base never needs to know how.
    One instance exists per physical port and is shared by every transport on that port.
    """

    timeout: float
    ''' seconds to wait for a response '''

    def connect(self) -> bool:
        ''' start the client (for example a background reader); True when it is ready '''
        ...

    def close(self) -> None:
        ''' stop the client and release the serial port; safe to call more than once '''
        ...

    def read(self, reset_buffer: bool = True, frames: int = 1) -> list[bytes] | bytes | None:
        ''' wait for a response and return it, without framing; None if nothing arrived in time '''
        ...

    def write(self, data: bytes) -> None:
        ''' send one message, adding whatever framing the protocol needs '''
        ...


class serial_base(transport_base):

    transport_type = "base class"
    ''' abstract; concrete serial transports declare their own transport_type = "scraper" '''

    # One serial client per "port(baudrate)", shared by every section on that port
    # (mirrors modbus_base.clients). Class level, so it is shared across subclasses too.
    clients : dict[str, SerialClient] = {}
    _clients_lock : threading.Lock = threading.Lock()
    _port_locks : dict[str, threading.RLock] = {}

    MIN_ADDRESS : int = 0
    MAX_ADDRESS : int = 255
    ''' inclusive range accepted for the "address" setting '''

    OPEN_ON_INIT : bool = True
    '''
    True: open the serial port while the transport is constructed, so a missing port fails
    start-up (the original serial_pylon behavior).
    False: open it in connect(), so a device that is not there yet is retried every cycle.
    '''

    EXCLUSIVE_PORT : bool = True
    ''' open the port with an exclusive lock, so nothing else on the machine can open it '''

    DEFAULT_CLIENT_TIMEOUT : float = 5.0
    ''' response timeout assumed for scheduling before the client exists '''

    address : int = 0
    addresses : list[int] = []

    _client : SerialClient | None = None
    _client_key : str = ""

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        self._port_setting: str = settings.get("port", fallback="/dev/ttyUSB0")
        if not self._port_setting:
            raise ValueError("Port is not set")
        self.port: str = self._port_setting

        # "baud" and "unit_id" are the names used in the device docs.
        self.baudrate: int = settings.getint(["baudrate", "baud"], fallback=9600)

        address: int = settings.getint(["address", "unit_id"], fallback=0)
        if not self.MIN_ADDRESS <= address <= self.MAX_ADDRESS:
            msg: str = f"{type(self).__name__} address must be {self.MIN_ADDRESS}-{self.MAX_ADDRESS}, got {address}"
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

    # ------------------------------------------------------------------ #
    # the client: what a framing family provides                         #
    # ------------------------------------------------------------------ #

    def _create_client(self) -> SerialClient:
        '''
        Opens the serial port and returns the client that owns it, using self.port,
        self.baudrate and self._serial_parameters(). This is where a framing family decides
        how messages are delimited. Called once per port; later sections on the same port
        share the client (see _check_shared_client).
        '''
        msg: str = f"{type(self).__name__} must implement _create_client()"
        raise NotImplementedError(msg)

    def _check_shared_client(self, shared : SerialClient) -> None:
        '''
        Called when this section joins a client another section already opened on the same
        port. Raise ValueError if this section cannot use it (for example it expects different
        frame markers). The default accepts any client.
        '''

    def _serial_parameters(self) -> dict[str, Any]:
        ''' pyserial settings for opening the port; override for protocols that are not 8N1 '''
        parameters: dict[str, Any] = {
            "bytesize": 8,
            "parity": serial.PARITY_NONE,
            "stopbits": 1,
        }
        if self.EXCLUSIVE_PORT:
            parameters["exclusive"] = True
        return parameters

    def _prepare_client(self, client : SerialClient) -> None:
        ''' hook run by connect() before the client is started; configure per-transport client settings here '''

    @property
    def client(self) -> SerialClient:
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

    def _acquire_client(self) -> SerialClient:
        '''
        Opens the serial port, or joins the client another section on the same port already
        opened. Raises ValueError (port not found, or a client this section cannot share) or
        OSError (the port cannot be opened).
        '''
        if self._client is not None:
            return self._client

        self._resolve_port()

        with self._clients_lock:
            shared_client: SerialClient | None = self.clients.get(self._client_key)
            if shared_client is None:
                self._log.info("Serial Port : " + self.port + " = "+get_usb_serial_port_info(self.port)) #print for config convenience
                shared_client = self._create_client()
                self.clients[self._client_key] = shared_client
            else:
                self._check_shared_client(shared_client)
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
        client: SerialClient | None = self._client
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
        return max(default, 2.0 * commands * self._client_timeout())

    def _client_timeout(self) -> float:
        ''' how long one response may take: the open client's timeout, or the default before it exists '''
        return float(self._client.timeout) if self._client is not None else self.DEFAULT_CLIENT_TIMEOUT

    # ------------------------------------------------------------------ #
    # wire access                                                        #
    # ------------------------------------------------------------------ #

    def _get_port_lock(self) -> threading.RLock:
        with self._clients_lock:
            if self._client_key not in self._port_locks:
                self._port_locks[self._client_key] = threading.RLock()
            return self._port_locks[self._client_key]

    def build_frame(self, command: int, info: bytes = b"") -> bytes:
        ''' builds a request message; the client adds whatever framing the protocol uses '''
        raise NotImplementedError

    def decode_frame(self, raw_frame: bytes) -> SimpleNamespace | None:
        ''' parses a response message (the client has already removed the framing); None if it is invalid '''
        raise NotImplementedError

    def send_command(self, cmd: int, info: bytes = b"") -> None:
        data: bytes = self.build_frame(cmd, info)
        self.client.write(data)

    def _exchange(self, command : int, info : bytes = b"") -> bytes | None:
        '''
        Sends a command and returns the raw response message (framing already removed), or None.

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

    def connect(self) -> bool:
        try:
            client: SerialClient = self._acquire_client()
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

    def _process_raw(self, raw_data : dict[int, bytes]) -> dict[str, int | float | str]:
        '''
        Decodes every payload collected this cycle in one pass, through the protocol's registry
        map. A family that does not decode that way (for example a listen-only reader that
        walks the frame byte by byte) overrides this.
        '''
        if self.protocolSettings is None:
            return {}
        return self.protocolSettings.process_registery(raw_data, registry_map=self.protocolSettings.get_registry_map())

    def read_data(self) -> dict[str, int | float | str]:
        '''
        Scheduling path: Sequential and Concurrent (also the fallback for standalone reads).

        Sends every command, then decodes all payloads in one pass.
        '''
        raw_data: dict[int, bytes] = {
            command: payload
            for command, payload in self._poll_commands_iter()
            if payload is not None
        }

        info: dict[str, int | float | str] = self._process_raw(raw_data) if raw_data else {}

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

        if raw_data:
            self._partial_info.update(self._process_raw(raw_data))

        if _owner:
            self.finish_cycle_tracking(self._partial_info)
