# Description: Serial base transport class with shared client management, per-port locking, and command/response polling for request/response serial protocols
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

# Base class for scraper transports that talk a request/response protocol over a serial
# (RS485/RS232/USB) port, where every registry map entry is a command and the reply is
# decoded by protocol_settings. It plays the same role for serial protocols that
# modbus_base plays for Modbus:
#
#   * one serial client per port, shared by every config section on that port, so several
#     devices (one section per address) can sit on the same bus
#   * a per-port lock, plus the gateway's bus_lock in interleaved mode, so a request and
#     its response are never interleaved with another device's
#   * scrape_target, so the gateway schedules the transport in every read_mode
#   * read_data / read_data_iter built on a single hook, _read_command()
#
# A concrete transport (see serial_pylon) supplies the wire format: SOI/EOI markers,
# build_frame(), decode_frame() and _read_command(). Settings read here are declared to the
# web UI through the "_serial_base" entry in classes/WebServer/transport_defaults.json,
# because the UI's AST scanner reads each transport file on its own and does not follow
# Python inheritance. A concrete transport must also declare its own literal
# transport_type = "scraper" for the same reason.
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import serial

from classes.protocol_settings import (
    registry_map_entry,
)
from classes.transports.serial_frame_client import serial_frame_client
from defs.common import (
    TransportSettings,
    find_usb_serial_port,
    get_usb_serial_port_info,
)

from .transport_base import transport_base


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

    address : int = 0
    addresses : list[int] = []

    client : serial_frame_client

    def __init__(self, settings : TransportSettings) -> None:
        super().__init__(settings)

        if not self.SOI or not self.EOI:
            msg: str = f"{type(self).__name__} must define the SOI and EOI frame markers"
            raise NotImplementedError(msg)

        self.port: str = settings.get("port", fallback="/dev/ttyUSB0")
        if not self.port:
            raise ValueError("Port is not set")

        resolved_port: str | None = find_usb_serial_port(self.port)
        if not resolved_port:
            raise ValueError("Port is not valid / not found")
        self.port = resolved_port

        self._log.info("Serial Port : " + self.port + " = "+get_usb_serial_port_info(self.port)) #print for config convenience

        # "baud" and "unit_id" are the names used in the device docs.
        self.baudrate: int = settings.getint(["baudrate", "baud"], fallback=9600)

        address: int = settings.getint(["address", "unit_id"], fallback=0)
        if not self.MIN_ADDRESS <= address <= self.MAX_ADDRESS:
            msg = f"{type(self).__name__} address must be {self.MIN_ADDRESS}-{self.MAX_ADDRESS}, got {address}"
            raise ValueError(msg)
        self.address = address
        self.addresses = [address]

        self._client_key: str = f"{self.port}({self.baudrate})"
        with self._clients_lock:
            shared_client: serial_frame_client | None = self.clients.get(self._client_key)
            if shared_client is None:
                shared_client = serial_frame_client(self.port,
                                                    self.baudrate,
                                                    self.SOI,
                                                    self.EOI,
                                                    **self._serial_parameters())
                self.clients[self._client_key] = shared_client
            else:
                self._log.debug(f"Sharing serial client {self._client_key} (address {address})")
            self.client = shared_client

    def _serial_parameters(self) -> dict[str, Any]:
        ''' pyserial framing for the client; override for protocols that are not 8N1 '''
        return {
            "bytesize": 8,
            "parity": serial.PARITY_NONE,
            "stopbits": 1,
            "exclusive": True,
        }

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
        return max(default, 2.0 * commands * float(self.client.timeout))

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

    def connect(self) -> bool:
        self.client.connect()
        if self._handshake():
            self.connected = True
        return self.connected

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
