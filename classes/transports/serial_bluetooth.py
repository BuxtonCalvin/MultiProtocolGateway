# Description: Bluetooth Low Energy base transport built on serial_base: one shared GATT client per device, notification reassembly, locking, lifecycle cleanup, scheduling and command/response polling for devices that talk over a notify/write characteristic pair
# File: serial_bluetooth.py
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

# serial_bluetooth is the Bluetooth Low Energy framing family of serial_base. A great many
# battery management systems (and other sensors) put a UART behind a BLE module: the device
# exposes one characteristic that notifies (device to gateway) and one that is written
# (gateway to device), and the bytes on them are the same frames the device would speak on a wire.
# That is exactly what serial_base models, so everything that is not specific to BLE is inherited:
#
#   * the shared client per device, so several config sections can read one device
#   * the per-device lock, plus the gateway's bus_lock in interleaved mode
#   * scrape_target, startup rows, cycle bookkeeping, read_data / read_data_iter, cleanup()
#
# What this module adds is the radio side: bleak runs on its own
# asyncio loop in a daemon thread, and the synchronous transport talks to it through
# run_coroutine_threadsafe. A notification is only a slice of a frame (an ATT payload is 20 bytes
# unless the link negotiated more), so serial_bluetooth_client collects the slices and asks the protocol,
# through its frame finder, whether a whole frame has arrived.
#
# A manufacturer's protocol is a subclass of serial_bluetooth. It sets the three UUIDs, writes a
# frame finder (extract_frame), and implements build_frame() / decode_frame() / _read_command(), as
# a serial protocol does. It must also declare its own literal transport_type = "scraper", and have
# an entry in classes/WebServer/transport_defaults.json that extends "_bluetooth_base", because the
# web UI's AST scanner reads each transport file on its own and does not follow inheritance.
#
# Two things differ from a wire and are worth knowing:
#
#   * Most batteries accept one BLE connection at a time. If the manufacturer's phone app (or another
#     gateway) is connected, this transport cannot connect, and the other way round.
#   * A radio link drops now and then. A failed read marks the transport disconnected, and the gateway
#     reconnects on the next cycle; startup rows are read again after every reconnect.
#
# Settings read here are declared to the web UI through the "_bluetooth_base" entry in
# classes/WebServer/transport_defaults.json.
import asyncio
import concurrent.futures
import contextlib
import re
import threading
import time
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str

from classes.protocol_settings import registry_map_entry
from defs.common import TransportSettings

from .serial_base import SerialClient, serial_base

T = TypeVar("T")

FrameFinder = Callable[[bytearray], bytes | None]
'''
Takes the bytes received so far, removes everything it consumes (noise before a frame, then the frame
itself) and returns the frame; returns None, leaving a partial frame in place, until a whole one has arrived.
'''


class serial_bluetooth_client():
    '''
    The wire layer for one BLE device: one instance per device, shared by every transport that
    reads it (see serial_base). Owns the bleak connection and the asyncio loop it runs on, and knows
    nothing about what the bytes mean beyond the frame finder it is given.

    It implements the SerialClient interface, so serial_base drives it like a serial port:
    connect() brings the link up (and raises OSError when it cannot), write() sends a request and
    read() waits for one complete frame.
    '''

    timeout : float = 5.0
    ''' seconds to wait for a complete frame '''
    scan_timeout : float = 10.0
    ''' seconds to look for the device before giving up '''
    connect_timeout : float = 20.0
    ''' seconds the connection and service discovery may take '''

    MAX_BUFFER : int = 4096
    ''' bytes kept when nothing is reading, so a device that streams cannot grow the buffer without end '''
    WRITE_CHUNK : int = 20
    ''' bytes per write: the payload of an ATT packet on a link that never negotiated a bigger MTU '''
    CONNECT_MARGIN : float = 5.0
    ''' seconds added to scan + connect time before the connection attempt is abandoned '''
    DISCONNECT_TIMEOUT : float = 5.0
    ''' seconds close() waits for the link to come down '''

    def __init__(self, address : str, service_uuid : str, notify_uuid : str, write_uuid : str, find_frame : FrameFinder, adapter : str = "", extra_services : tuple[str, ...] = ()) -> None:
        self.address: str = address
        self.service_uuid: str = service_uuid
        self.notify_uuid: str = notify_uuid
        self.write_uuid: str = write_uuid
        self.adapter: str = adapter
        self.extra_services: tuple[str, ...] = extra_services
        ''' further services the characteristics live in, for a device that splits them over more than one '''
        self._find_frame: FrameFinder = find_frame

        self._buffer: bytearray = bytearray()
        self._condition: threading.Condition = threading.Condition()
        self._request_pending: bool = False
        ''' a request was written and its reply has not been read yet, so the buffer must not be discarded '''
        self._lost: bool = False
        ''' the device went away since the link was made '''

        self._loop_lock: threading.Lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

        self._ble: BleakClient | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._write_response: bool = True
        ''' write with response (True) or without (False); flipped once if the device refuses the advertised mode '''

    @property
    def signature(self) -> tuple[str, str, str, FrameFinder]:
        ''' what makes two transports able to share this client: the characteristics it uses and how it finds a frame '''
        return (self.service_uuid, self.notify_uuid, self.write_uuid, self._find_frame)

    @property
    def is_connected(self) -> bool:
        return self._ble is not None and not self._lost and self._ble.is_connected

    # ------------------------------------------------------------------ #
    # the asyncio loop                                                     #
    # ------------------------------------------------------------------ #

    def _start_loop(self) -> None:
        ''' starts the daemon thread bleak runs on, if it is not running '''
        with self._loop_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
            thread: threading.Thread = threading.Thread(target=self._run_loop, args=(loop,), name=f"bluetooth-{self.address}", daemon=True)
            self._loop = loop
            self._thread = thread
            thread.start()

    @staticmethod
    def _run_loop(loop : asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    def _run(self, coroutine : Coroutine[Any, Any, T], timeout : float) -> T:
        '''
        Runs a coroutine on the loop and waits for it. Every failure reaches the caller as OSError,
        the exception serial_base already treats as "the device is not reachable".
        '''
        loop: asyncio.AbstractEventLoop | None = self._loop
        if loop is None:
            coroutine.close()
            msg: str = f"Bluetooth client for {self.address} is not running"
            raise OSError(msg)

        future: concurrent.futures.Future[T] = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except TimeoutError as err:
            future.cancel()
            msg = f"Bluetooth operation on {self.address} timed out after {timeout:g} seconds"
            raise OSError(msg) from err
        except BleakError as err:
            msg = f"Bluetooth error on {self.address}: {err}"
            raise OSError(msg) from err

    # ------------------------------------------------------------------ #
    # SerialClient interface                                               #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        '''
        Brings the link up: finds the device, connects, checks that both characteristics exist and
        subscribes to notifications. A link that is already up is left alone. Raises OSError when the
        device cannot be reached, so serial_base retries on the next cycle.
        '''
        if self.is_connected:
            return True
        self._start_loop()
        self._run(self._connect(), self.scan_timeout + self.connect_timeout + self.CONNECT_MARGIN)
        return True

    def close(self) -> None:
        ''' drops the link and stops the loop thread; safe to call more than once '''
        loop: asyncio.AbstractEventLoop | None = self._loop
        thread: threading.Thread | None = self._thread
        if loop is None:
            return

        with contextlib.suppress(OSError):
            self._run(self._disconnect(), self.DISCONNECT_TIMEOUT)

        with self._loop_lock:
            self._loop = None
            self._thread = None
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.DISCONNECT_TIMEOUT)

    def write(self, data : bytes) -> None:
        '''
        Sends one request exactly as the protocol built it. Anything received before the request is
        stale, so the buffer is emptied first; the reply that follows is kept for read().
        '''
        if not self.is_connected:
            msg: str = f"Bluetooth device {self.address} is not connected"
            raise OSError(msg)

        with self._condition:
            self._buffer.clear()
            self._request_pending = True
        self._run(self._write(data), self.timeout)

    def read(self, reset_buffer : bool = True, frames : int = 1) -> list[bytes] | bytes | None:
        '''
        Waits for one complete frame (or `frames` of them, returned as a list) and returns it whole, or
        None if none arrived within the timeout. Raises OSError if the device disconnects while waiting.

        reset_buffer: discard whatever arrived before this call, for a device that speaks on its own and
        should be heard fresh. It does nothing after write(): the reply to that request may already be in
        the buffer, because notifications arrive on the radio thread while the request is still being sent.
        '''
        deadline: float = time.monotonic() + self.timeout
        found: list[bytes] = []

        with self._condition:
            if reset_buffer and not self._request_pending:
                self._buffer.clear()
            self._request_pending = False

            while True:
                frame: bytes | None = self._find_frame(self._buffer)
                if frame is not None:
                    found.append(frame)
                    if len(found) >= frames:
                        return found[0] if frames == 1 else found
                    continue

                if self._lost:
                    msg: str = f"Bluetooth device {self.address} disconnected"
                    raise OSError(msg)

                remaining: float = deadline - time.monotonic()
                if remaining <= 0:
                    return found or None
                self._condition.wait(remaining)

    # ------------------------------------------------------------------ #
    # bleak, on the loop thread                                            #
    # ------------------------------------------------------------------ #

    async def _find_device(self) -> BLEDevice | None:
        ''' scans for the device by address (a UUID on macOS) or, for "name:<local name>", by name '''
        by_name: bool = self.address.lower().startswith("name:")
        identifier: str = self.address[5:].strip() if by_name else self.address

        if by_name:
            if self.adapter:
                return await BleakScanner.find_device_by_name(identifier, timeout=self.scan_timeout, bluez={"adapter": self.adapter})
            return await BleakScanner.find_device_by_name(identifier, timeout=self.scan_timeout)

        if self.adapter:
            return await BleakScanner.find_device_by_address(identifier, timeout=self.scan_timeout, bluez={"adapter": self.adapter})
        return await BleakScanner.find_device_by_address(identifier, timeout=self.scan_timeout)

    def _characteristic(self, client : BleakClient, uuid : str) -> BleakGATTCharacteristic:
        characteristic: BleakGATTCharacteristic | None = client.services.get_characteristic(normalize_uuid_str(uuid))
        if characteristic is None:
            msg: str = f"Bluetooth device {self.address} has no characteristic {uuid}; is it the device this transport is for?"
            raise OSError(msg)
        return characteristic

    async def _connect(self) -> None:
        await self._disconnect()  # a half-open link from an earlier attempt would block a new one

        device: BLEDevice | None = await self._find_device()
        if device is None:
            msg: str = f"Bluetooth device {self.address} was not found; is it powered, in range and not connected to another app?"
            raise OSError(msg)

        service: list[str] = [normalize_uuid_str(uuid) for uuid in (self.service_uuid, *self.extra_services)]
        if self.adapter:
            client: BleakClient = BleakClient(device, disconnected_callback=self._on_disconnect, services=service, timeout=self.connect_timeout, bluez={"adapter": self.adapter})
        else:
            client = BleakClient(device, disconnected_callback=self._on_disconnect, services=service, timeout=self.connect_timeout)

        try:
            await client.connect()
            notify_char: BleakGATTCharacteristic = self._characteristic(client, self.notify_uuid)
            write_char: BleakGATTCharacteristic = self._characteristic(client, self.write_uuid)
            await client.start_notify(notify_char, self._on_notification)
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise

        with self._condition:
            self._buffer.clear()
            self._request_pending = False
            self._lost = False
        self._write_char = write_char
        self._write_response = "write" in write_char.properties
        self._ble = client

    async def _disconnect(self) -> None:
        client: BleakClient | None = self._ble
        self._ble = None
        self._write_char = None
        if client is None:
            return
        with contextlib.suppress(BleakError, TimeoutError, EOFError):
            await client.disconnect()

    async def _write(self, data : bytes) -> None:
        client: BleakClient | None = self._ble
        characteristic: BleakGATTCharacteristic | None = self._write_char
        if client is None or characteristic is None or not client.is_connected:
            msg: str = f"Bluetooth device {self.address} is not connected"
            raise OSError(msg)

        for start in range(0, len(data), self.WRITE_CHUNK):
            chunk: bytes = data[start:start + self.WRITE_CHUNK]
            try:
                await client.write_gatt_char(characteristic, chunk, response=self._write_response)
            except BleakError:
                # some devices advertise one write mode and accept the other: try it once, and keep it only if it works
                await client.write_gatt_char(characteristic, chunk, response=not self._write_response)
                self._write_response = not self._write_response

    def _on_notification(self, sender : BleakGATTCharacteristic, data : bytearray) -> None:
        ''' a slice of a frame arrived: keep it and wake the reader '''
        with self._condition:
            self._buffer += data
            if len(self._buffer) > self.MAX_BUFFER:
                del self._buffer[:len(self._buffer) - self.MAX_BUFFER]
            self._condition.notify_all()

    def _on_disconnect(self, client : BleakClient) -> None:
        with self._condition:
            self._lost = True
            self._condition.notify_all()


class serial_bluetooth(serial_base):

    transport_type : str = "base class"
    ''' abstract; a protocol subclass declares its own transport_type = "scraper" '''

    SERVICE_UUID : str = ""
    NOTIFY_UUID : str = ""
    WRITE_UUID : str = ""
    ''' the GATT service and the characteristics the device notifies on and is written to, as 16-bit ("ff01") or full UUIDs; a protocol must set them, and the config can override them for a rebranded device '''
    EXTRA_SERVICE_UUIDS : tuple[str, ...] = ()
    ''' further services a protocol's characteristics live in, when it splits them (Renogy writes in one service and notifies in another) '''

    DEFAULT_TIMEOUT : float = 5.0
    ''' seconds a response may take '''
    DEFAULT_SCAN_TIMEOUT : float = 10.0
    DEFAULT_CONNECT_TIMEOUT : float = 20.0
    RESPONSE_ATTEMPTS : int = 4
    ''' frames examined for the one a request is waiting for: a device that also pushes frames of its own gets that many chances '''

    OPEN_ON_INIT : bool = False
    ''' the radio is only used in connect(), so a device that is out of range at start-up is retried every cycle '''
    MAX_ADDRESS : int = 0
    ''' a BLE device is addressed by its radio address, not a bus address; a protocol with a unit id raises this '''

    ADDRESS_PATTERNS : tuple[re.Pattern[str], ...] = (
        re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$"),
        re.compile(r"^[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$"),
        re.compile(r"^name:\S.*$", re.IGNORECASE),
    )
    ''' a MAC address, the UUID macOS uses in its place, or "name:<local name>" '''

    def __init__(self, settings : TransportSettings) -> None:
        # Set first: serial_base resolves the port during construction.
        self._timeout: float = float(settings.get("timeout", fallback=str(self.DEFAULT_TIMEOUT)))
        self._scan_timeout: float = float(settings.get("scan_timeout", fallback=str(self.DEFAULT_SCAN_TIMEOUT)))
        self._connect_timeout: float = float(settings.get("connect_timeout", fallback=str(self.DEFAULT_CONNECT_TIMEOUT)))
        self._adapter: str = settings.get("adapter", fallback="").strip()
        ''' the Bluetooth adapter to use, such as hci1 on Linux; empty for the system default '''

        self._service_uuid: str = settings.get("service_uuid", fallback="").strip() or self.SERVICE_UUID
        self._notify_uuid: str = settings.get("notify_uuid", fallback="").strip() or self.NOTIFY_UUID
        self._write_uuid: str = settings.get("write_uuid", fallback="").strip() or self.WRITE_UUID
        if not (self._service_uuid and self._notify_uuid and self._write_uuid):
            msg: str = f"{type(self).__name__} needs a service, a notify and a write UUID"
            raise ValueError(msg)

        device: str = settings.get(["bluetooth_address", "port"], fallback="").strip()
        if not any(pattern.match(device) for pattern in self.ADDRESS_PATTERNS):
            msg = f"{type(self).__name__} needs bluetooth_address set to the device's address (AA:BB:CC:DD:EE:FF) or name:<local name>, got '{device}'"
            raise ValueError(msg)
        self._device: str = device

        super().__init__(settings)
        self._port_setting = self._device

    # ------------------------------------------------------------------ #
    # serial_base interface                                                #
    # ------------------------------------------------------------------ #

    def _resolve_port(self) -> None:
        ''' there is no port to look for: the "port" is the device's radio address, and finding it is the client's job '''
        self.port = self._device
        self._client_key = f"{self.port}(bluetooth)"

    def _create_client(self) -> serial_bluetooth_client:
        return serial_bluetooth_client(self._device, self._service_uuid, self._notify_uuid, self._write_uuid, type(self).extract_frame, self._adapter, self.EXTRA_SERVICE_UUIDS)

    def _check_shared_client(self, shared : SerialClient) -> None:
        if not isinstance(shared, serial_bluetooth_client):
            # ValueError, not TypeError: serial_base.connect() treats it as "cannot share this device" and retries
            msg: str = f"{type(self).__name__} on {self.port} cannot share a client that is not a Bluetooth client"
            raise ValueError(msg)  # noqa: TRY004

        if shared.signature != (self._service_uuid, self._notify_uuid, self._write_uuid, type(self).extract_frame):
            msg = f"{type(self).__name__} on {self.port} uses different characteristics or frames than the section that connected to the device"
            raise ValueError(msg)

    def _prepare_client(self, client : SerialClient) -> None:
        if not isinstance(client, serial_bluetooth_client):
            msg: str = f"{type(self).__name__} needs a serial_bluetooth_client, got {type(client).__name__}"
            raise TypeError(msg)
        client.timeout = self._timeout
        client.scan_timeout = self._scan_timeout
        client.connect_timeout = self._connect_timeout

    def _client_timeout(self) -> float:
        return self._timeout

    # ------------------------------------------------------------------ #
    # command/response                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_frame(buffer : bytearray) -> bytes | None:
        '''
        The frame finder (see FrameFinder): removes noise and one complete frame from the front of the
        bytes received so far and returns it, or returns None while a frame is still incomplete. A
        protocol must implement it, as a static method, because transports that share a device must
        agree on it.
        '''
        raise NotImplementedError

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        ''' the complete request, exactly as it is written to the device '''
        raise NotImplementedError

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        ''' validates a complete response and returns a namespace whose `payload` is the data the protocol map reads from; None if it is invalid '''
        raise NotImplementedError

    def connect(self) -> bool:
        '''
        serial_base.connect() plus one rule: if the radio link came up but the handshake did not succeed, the link
        is taken down again. Otherwise the next cycle would find the link up and read the device without the
        handshake that decides whether this transport understands it.
        '''
        connected: bool = super().connect()
        if not connected and self._link_up():
            self.connected = False
            client: SerialClient | None = self._client
            if client is not None:
                client.close()
        return connected

    def _link_up(self) -> bool:
        '''
        Whether the radio link is up right now. This is asked of the client, not of `connected`, because
        connect() runs the handshake before it sets `connected`, and the handshake exchanges frames: going by
        `connected` would reconnect from inside the connection attempt.
        '''
        client: SerialClient | None = self._client
        return isinstance(client, serial_bluetooth_client) and client.is_connected

    def _exchange_until(self, command : int, accept : Callable[[bytes], bool], info : bytes = b"") -> bytes | None:
        '''
        Sends a command and returns the first frame `accept` approves of, or None.

        A BLE device can send frames nobody asked for (a status push, an acknowledgement, the answer to an
        earlier request that timed out), so unlike a serial read this looks at up to RESPONSE_ATTEMPTS
        frames for the right one. The bus_lock (interleaved mode) and the per-device lock are held for the
        whole round trip, as in serial_base._exchange.

        A transport whose link is down connects here, so a device that was out of range at start-up
        simply returns None until it is in range. A radio failure marks the transport disconnected, and the
        gateway reconnects on the next cycle.
        '''
        if not self._link_up() and not self.connect():
            return None

        bus_lock: threading.Lock | None = self.bus_lock
        if bus_lock is not None:
            bus_lock.acquire()
        try:
            with self._get_port_lock():
                try:
                    self.send_command(command, info)
                    for attempt in range(self.RESPONSE_ATTEMPTS):
                        reply: list[bytes] | bytes | None = self.client.read(reset_buffer=attempt == 0)
                        if reply is None:
                            return None
                        frame: bytes = b"".join(reply) if isinstance(reply, list) else reply
                        if accept(frame):
                            return frame
                        self._log.debug(f"Ignored an unexpected frame from {self.port}: {frame.hex()}")
                except OSError as err:
                    self._log.warning(f"Bluetooth exchange with {self.port} failed: {err}")
                    self.connected = False
                    return None
        finally:
            if bus_lock is not None:
                bus_lock.release()
        return None

    def _exchange(self, command : int, info : bytes = b"") -> bytes | None:
        return self._exchange_until(command, lambda frame: True, info)

    @staticmethod
    def _drop_entries(entries : list[registry_map_entry], names : set[str]) -> list[registry_map_entry]:
        ''' the map rows except those named in `names`; for the rows of a cell or sensor the device does not have '''
        return [entry for entry in entries if entry.variable_name not in names]
