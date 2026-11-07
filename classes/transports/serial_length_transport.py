# Description: Length-prefixed serial framing family: the serial_length_client wire layer and serial_length_transport, the base for command/response protocols whose frames carry their own length (JBD, JK)
# File: serial_length_transport.py
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

# This module is the length-prefixed framing family of serial_base, and holds its two layers:
#
#   serial_length_client      the wire. One instance per physical serial port. Finds a frame in
#                             the byte stream by its start marker and its length field, then
#                             reads exactly that many bytes. It does not scan for an end marker,
#                             because in binary frames the end byte can occur inside the data.
#   serial_length_transport   the transport. One instance per config section (per device). A
#                             command/response protocol subclasses it, describes where its
#                             frames start and where the length lives, and implements
#                             build_frame() and decode_frame().
#
# The client hands back the whole frame, start to end, and sends a request exactly as the
# protocol built it, so checksums and end markers stay the protocol's business.
#
# SOI/EOI delimited protocols belong to serial_frame_transport instead.
import time
from types import SimpleNamespace
from typing import Any

import serial

from defs.common import TransportSettings

from .serial_base import SerialClient, serial_base


class serial_length_client():
    '''
    The wire layer for length-prefixed frames: one instance per physical serial port, shared
    by every transport on that port (see serial_base). Knows nothing about protocols.

    A frame is found by its start marker. The frame's total size is then
    length_offset + (value of the length field) + length_adjust, so the same code serves a
    protocol whose length counts only the data (JBD) and one whose length counts everything
    after the start marker (JK).
    '''

    timeout : float = 5
    ''' seconds to wait for a complete frame '''

    READ_SLICE : float = 0.05
    ''' seconds the port waits for data on each read, so the overall timeout is honored '''

    client : serial.Serial
    ''' the serial port '''

    def __init__(self, port : str, baud : int, start : bytes, length_offset : int, length_size : int,
                 length_adjust : int, max_frame : int, **kwrgs : Any) -> None:
        self.port: str = port
        self.baud: int = baud
        self.start: bytes = start
        self.length_offset: int = length_offset
        self.length_size: int = length_size
        self.length_adjust: int = length_adjust
        self.max_frame: int = max_frame
        self._buffer: bytearray = bytearray()
        self.client = serial.Serial(port, baud, timeout=self.READ_SLICE, **kwrgs)

    @property
    def signature(self) -> tuple[bytes, int, int, int]:
        ''' what makes two transports able to share this client: how it finds a frame '''
        return (self.start, self.length_offset, self.length_size, self.length_adjust)

    def connect(self) -> bool:
        ''' nothing to start: frames are read on demand '''
        return True

    def close(self) -> None:
        ''' release the serial port; safe to call more than once '''
        if self.client.is_open:
            self.client.close()

    def write(self, data : bytes) -> None:
        ''' send a request exactly as the protocol built it '''
        self.client.write(data)

    def read(self, reset_buffer : bool = True, frames : int = 1) -> bytes | None:
        '''
        Wait for one complete frame and return it whole (start marker to the last byte), or None
        if none arrived within the timeout. Bytes before a start marker are discarded, and a
        start marker whose length field is implausible is skipped, so a transport that joins a
        stream mid-frame resynchronizes by itself.

        reset_buffer: discard anything left over from an earlier read first. The operating
        system's input buffer is not touched, because a request has already been sent and its
        reply may be in it.
        '''
        if reset_buffer:
            self._buffer.clear()

        header_size: int = self.length_offset + self.length_size
        deadline: float = time.monotonic() + self.timeout

        while True:
            frame: bytes | None = self._take_frame(header_size)
            if frame is not None:
                return frame
            if time.monotonic() >= deadline:
                return None
            self._buffer += self.client.read(max(1, self.client.in_waiting))

    def _take_frame(self, header_size : int) -> bytes | None:
        ''' removes and returns one complete frame from the buffer, or None if more bytes are needed '''
        while True:
            position: int = self._buffer.find(self.start)
            if position < 0:
                # keep a possible partial start marker at the end, drop everything else
                keep: int = len(self.start) - 1
                del self._buffer[:max(0, len(self._buffer) - keep)]
                return None
            if position > 0:
                del self._buffer[:position]
            if len(self._buffer) < header_size:
                return None

            length: int = int.from_bytes(self._buffer[self.length_offset:header_size], "big")
            total: int = self.length_offset + length + self.length_adjust
            if total < header_size or total > self.max_frame:
                del self._buffer[:1]  # not a real frame start: look for the next marker
                continue
            if len(self._buffer) < total:
                return None

            frame: bytes = bytes(self._buffer[:total])
            del self._buffer[:total]
            return frame


class serial_length_transport(serial_base):

    transport_type : str = "base class"
    ''' abstract; a protocol subclass declares its own transport_type = "scraper" '''

    START : bytes = b""
    ''' marker the frame begins with; a protocol must set it '''
    LENGTH_OFFSET : int = 0
    ''' index of the length field within the frame '''
    LENGTH_SIZE : int = 1
    ''' size of the length field in bytes (big-endian) '''
    LENGTH_ADJUST : int = 0
    ''' frame size = LENGTH_OFFSET + length value + LENGTH_ADJUST '''
    MAX_FRAME : int = 512
    ''' frames larger than this are treated as noise '''

    OPEN_ON_INIT : bool = False
    PRUNE_SHORT_PAYLOADS : bool = True

    def __init__(self, settings : TransportSettings) -> None:
        if not self.START:
            msg: str = f"{type(self).__name__} must define the START marker of its frames"
            raise NotImplementedError(msg)
        if self.LENGTH_SIZE not in (1, 2):
            msg = f"{type(self).__name__} LENGTH_SIZE must be 1 or 2, got {self.LENGTH_SIZE}"
            raise ValueError(msg)

        # Set first: serial_base opens the port during construction when OPEN_ON_INIT is True.
        self._timeout: float = float(settings.get("timeout", fallback="5.0"))

        super().__init__(settings)

    # ------------------------------------------------------------------ #
    # serial_base interface                                                #
    # ------------------------------------------------------------------ #

    def _create_client(self) -> serial_length_client:
        return serial_length_client(self.port, self.baudrate, self.START, self.LENGTH_OFFSET, self.LENGTH_SIZE,
                                    self.LENGTH_ADJUST, self.MAX_FRAME, **self._serial_parameters())

    def _check_shared_client(self, shared : SerialClient) -> None:
        if isinstance(shared, serial_length_client) and shared.signature != (self.START, self.LENGTH_OFFSET, self.LENGTH_SIZE, self.LENGTH_ADJUST):
            msg: str = f"{type(self).__name__} on {self.port} frames messages differently from the section that opened the port"
            raise ValueError(msg)

    def _prepare_client(self, client : SerialClient) -> None:
        if not isinstance(client, serial_length_client):
            msg: str = f"{type(self).__name__} needs a serial_length_client, got {type(client).__name__}"
            raise TypeError(msg)
        client.timeout = self._timeout

    def _client_timeout(self) -> float:
        return self._timeout

    # ------------------------------------------------------------------ #
    # command/response                                                     #
    # ------------------------------------------------------------------ #

    def build_frame(self, command : int, info : bytes = b"") -> bytes:
        ''' the complete request, start marker to end marker, checksum included '''
        raise NotImplementedError

    def decode_frame(self, raw_frame : bytes) -> SimpleNamespace | None:
        '''
        Validates a complete response (start marker, length, end marker, checksum, status) and
        returns a namespace whose `payload` is the data the protocol map reads from; None if the
        frame is invalid or the device reported an error. If the protocol echoes the request's
        command, also set `command`, and a reply to a different command is dropped.
        '''
        raise NotImplementedError

    def _read_command(self, command : int) -> bytes | None:
        frame: bytes | None = self._exchange(command)
        if not frame:
            return None

        decoded: SimpleNamespace | None = self.decode_frame(frame)
        if decoded is None:
            return None

        # a protocol whose replies echo the command can say so, and a reply to something else is dropped
        echoed: Any = getattr(decoded, "command", None)
        if echoed is not None and echoed != command:
            self._log.warning(f"{type(self).__name__} sent command {command:#04x} but the reply is for {echoed:#04x}; ignored")
            return None

        payload: Any = decoded.payload
        return bytes(payload) if payload is not None else None
